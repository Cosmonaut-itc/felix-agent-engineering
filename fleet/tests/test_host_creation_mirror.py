"""SEN-5650 4d: creation CAS steps and the mirror of hostgate's original closure, with real 4c receipts.

The authority is an in-process effective Hostgate; recover reads its estado through the
product's _estado seam, and the public status CLI reads the ledger the product wrote.
"""
from dataclasses import replace
import json
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from host_creation_cases import ROOT, CreationHarness
from fleet import host_reservation, native, runner, storage
from fleet.model import Creation, Fault, Step, dump

DAY = 86400


class MirrorHarness(CreationHarness):
    def reconcile(self, estado=None, connect=None):
        """recover's host step in this process; estado defaults to the authority's own."""
        source = (lambda intent: self.authority.estado()) if estado is None else (lambda intent: estado)
        with patch.object(host_reservation, "_estado", source):
            with self.db() as db:
                return host_reservation.reconcile(db, runner.attempt_row(db, self.attempt_id))

    def slot(self):
        return self.ledger("SELECT attempt,generation FROM slot")

    def started(self, current):
        """authorize, consume and record_started in hostgate; the root stays alive."""
        authorize = self.requests(current)["authorize"]
        issued = self.authority.send(authorize)
        steps = self.requests(current, issued["permit"]["permitId"])
        consumed = self.authority.send(steps["consume"])
        self.authority.kernel.add(ROOT["pid"], ROOT["unique"], parent=10)
        started = self.authority.send(steps["start"])
        self.assertEqual(started["exitCode"], 0, started)
        return [(authorize, issued), (steps["consume"], consumed), (steps["start"], started)]

    def closing(self):
        """closing whose last soltar lost its reply, while hostgate already released the round."""
        current = self.grant()
        s = self.walk(current)
        self.plant("closing", Creation("closing", authorize=s["authorize"], consume=s["consume"],
                                       start=s["start"], release=replace(s["release"], receipt=None)))
        return current, s

    def resign(self, estado, key, observation=None, permit=None):
        """estado whose closure is re-signed with other values: integral, but of another creation."""
        from hostgate import scope
        value = json.loads(dump(estado))
        row = next(r for r in value["reservations"] if r["key"] == key)
        proof = scope.evidence_from_json(row["closureEvidence"])
        proof = replace(proof, observation=replace(proof.observation, **(observation or {}))).signed()
        row["closureEvidence"] = scope.evidence_json(proof)
        row["permit"] = dict(row["permit"], closedBy=proof.evidence_digest, **(permit or {}))
        return value


class HappyMirror(MirrorHarness):
    def test_each_step_persists_its_receipt_and_released_frees_the_slot_together(self):
        """Variante: release_without_local_cas."""
        current = self.grant()
        generation = self.slot()[0][1]
        self.assertEqual(self.slot(), [(self.attempt_id, generation)])

        def step(name, request, during, after):
            with self.db() as db:
                with db.transaction():
                    self.assertTrue(storage.begin_step(db, self.current(db), name, request))
                self.assertEqual((self.raw()[0][0], self.raw()[0][1][name]), (during, dict(request=request,
                                                                                           receipt=None)))
                receipt = self.authority.send(request)
                with db.transaction():
                    self.assertTrue(storage.record_step(db, self.current(db), name, receipt))
            state, creation = self.raw()[0]
            self.assertEqual((state, creation["phase"], creation[name]),
                             (after, after, dict(request=request, receipt=receipt)), "FACT: the original receipt")
            view = self.round_view()
            self.assertEqual((view["error"], view["round"]["state"]), (None, after), view)
            return receipt

        issued = step("authorize", self.requests(current)["authorize"], "authorizing", "authorized")
        requests = self.requests(current, issued["permit"]["permitId"])
        self.assertEqual(self.attempt(self.job)["gate"], "available")
        step("consume", requests["consume"], "spawning", "spawning")
        self.assertEqual(self.attempt(self.job)["gate"], "consumed", "FACT: consume closes the local gate once")
        self.authority.kernel.add(ROOT["pid"], ROOT["unique"], parent=10)
        started = step("start", requests["start"], "spawning", "running")
        self.assertEqual(self.raw()[0][1]["start"]["request"]["root"], ROOT)
        self.assertTrue(started["permit"]["scopeReceipt"].startswith("scp_"))
        busy = step("release", requests["release"], "closing", "closing")
        self.assertEqual(busy["exitCode"], 1, busy)
        self.authority.kernel.exit(ROOT["pid"])
        final = self.requests(current, issued["permit"]["permitId"], seq=2)["release"]
        closed = step("release", final, "closing", "closing")
        self.assertEqual((closed["exitCode"], closed["state"]), (0, "released"), closed)
        self.assertEqual(self.slot(), [(self.attempt_id, generation)], "FACT: closing still owns the slot")

        closure = self.closure(current)
        with self.db() as db:
            with self.assertRaises(Fault), db.transaction():
                db.execute("UPDATE slot SET attempt=NULL")  # the local CAS fails: nothing commits
                storage.release_host(db, self.current(db), closure)
        self.assertEqual(self.raw()[0][0], "closing", "FACT: released and the local CAS commit together")
        with self.db() as db:
            with db.transaction():
                self.assertTrue(storage.release_host(db, self.current(db), closure))
        state, creation = self.raw()[0]
        self.assertEqual((state, creation["closure"]), ("released", dict(permit=closure.permit,
                                                                        evidence=closure.evidence)))
        self.assertEqual(creation["release"]["receipt"], closed)
        self.assertEqual(self.slot(), [(None, generation)], "FACT: the local slot is free in the same commit")
        attempt = self.attempt(self.job)
        self.assertEqual((attempt["state"], attempt["gate"], attempt["disposition"]), ("finished", "closed", "released"))
        observed = self.observed()
        self.assertEqual((observed["reservation"]["error"], observed["reservation"]["round"]["state"]),
                         (None, "released"), observed)
        self.assertEqual(observed["completion"], "incomplete", observed)
        self.assertTrue(self.second_round(), "FACT: a released round leaves the attempt's index free")


class Cas(MirrorHarness):
    def test_a_step_wins_only_from_the_state_it_read(self):
        """Variante: cas_any_source."""
        _, s = self.closing()
        read = self.current()
        with self.db() as db:
            with db.transaction():
                self.assertTrue(storage.hold_host(db, read.intent, "supervisor_lost"))
            with db.transaction():
                self.assertFalse(storage.release_host(db, read, s["closure"]), "FACT: closing was read, held is there")
        self.assertEqual((self.raw()[0][0], self.slot()[0][0]), ("held", self.attempt_id))
        with self.db() as db:
            with db.transaction():
                self.assertTrue(storage.release_host(db, self.current(db), s["closure"]))
        self.assertEqual((self.raw()[0][0], self.slot()[0][0]), ("released", None))

    def test_two_writers_with_one_read_have_one_winner(self):
        """Variante: cas_noop_wins."""
        current = self.grant()
        authorize = self.requests(current)["authorize"]
        with self.db() as db:
            with db.transaction():
                self.assertTrue(storage.begin_step(db, self.current(db), "authorize", authorize))
            issued = self.authority.send(authorize)
            with db.transaction():
                self.assertTrue(storage.record_step(db, self.current(db), "authorize", issued))
            steps = self.requests(current, issued["permit"]["permitId"])
            with db.transaction():
                self.assertTrue(storage.begin_step(db, self.current(db), "consume", steps["consume"]))
            consumed = self.authority.send(steps["consume"])
            with db.transaction():
                self.assertTrue(storage.record_step(db, self.current(db), "consume", consumed))
            for read in ("before start", "start pending"):
                snapshot, winners = self.current(db), []
                for _ in range(2):
                    with db.transaction():
                        winners.append(storage.begin_step(db, snapshot, "start", steps["start"]))
                self.assertEqual(winners, [True, False] if read == "before start" else [False, False],
                                 "FACT: {}: a write that changes nothing wins nothing".format(read))
        self.assertEqual(self.raw()[0], ("spawning", json.loads(storage.creation_json(Creation(
            "spawning", authorize=Step(authorize, issued), consume=Step(steps["consume"], consumed),
            start=Step(steps["start"], None))))))


class Recover(MirrorHarness):
    def test_closing_converges_only_with_the_correlated_original_closure(self):
        """Variante: mirror_without_correlation."""
        current, s = self.closing()
        estado, generation = self.authority.estado(), self.slot()[0][1]
        obs = json.loads(dump(s["closure"].evidence))["observation"]
        other = "0" * 64
        wrong = {
            "another permit": self.resign(estado, current.key, dict(permit_id="prm_" + "0" * 32),
                                          dict(permitId="prm_" + "0" * 32)),
            "another root": self.resign(estado, current.key, dict(root=dict(obs["root"], pid=301))),
            "another baseline": self.resign(estado, current.key, dict(baseline_digest=other),
                                            dict(baselineDigest=other)),
            "another revision": dict(json.loads(dump(estado)), configRevision="r2"),
            "another attempt": json.loads(dump(estado).replace(self.attempt_id, "a_" + "0" * 32)),
        }
        for name, altered in wrong.items():
            with self.subTest(case=name):
                self.assertNotEqual(self.reconcile(altered).action, "released")
                self.assertEqual(self.raw()[0][0], "closing", "FACT: a closure that does not correlate is not copied")
                self.assertEqual(self.slot(), [(self.attempt_id, generation)])
        self.assertEqual(self.reconcile().action, "released")
        self.assertEqual((self.raw()[0][0], self.slot()), ("released", [(None, generation)]))
        self.assertEqual(self.reconcile().action, "none", "FACT: the mirror runs once")

    def test_held_from_closing_with_a_lost_supervisor_converges(self):
        """A dead S in closing is held first, then hostgate's original closure releases it."""
        self.closing()
        self.plant("closing", self.current().creation, supervisor_identity=dump(dict(self.me, boot_id="another-boot")))
        observation = self.reconcile()
        self.assertEqual(observation.action, "released")
        self.assertEqual(self.raw()[0][0], "released")
        self.assertEqual(self.attempt(self.job)["disposition"], "released")

    def test_a_finished_attempt_is_still_examined(self):
        """Variante: terminal_skips_recover."""
        self.closing()
        conn = sqlite3.connect(str(self.state / "state.db"))
        try:
            with conn:
                conn.execute("UPDATE attempts SET state='finished',gate='closed',recorded_outcome='succeeded' "
                             "WHERE id=?", (self.attempt_id,))
        finally:
            conn.close()
        with patch.object(host_reservation, "_estado", lambda intent: self.authority.estado()):
            result, code = runner.recover(self.state)
        self.assertEqual(code, 0, result)
        self.assertEqual(self.raw()[0][0], "released", "FACT: recover examined the finished attempt's round")
        attempt = self.attempt(self.job)
        self.assertEqual((attempt["state"], attempt["disposition"]), ("finished", "released"))
        self.assertEqual(self.ledger("SELECT recorded_outcome FROM attempts"), [("succeeded",)],
                         "FACT: the recorded outcome stays")
        self.assertEqual(self.slot()[0][0], None)

    def foreign_rejection(self, operation_id):
        """Another principal's reservar under `operation_id`, which hostgate rejects for good."""
        from hostgate.admission import Hostgate
        from hostgate.tests.test_admin import fixture_policy
        from host_creation_cases import default_umask
        authority, other = self.authority, self.me["uid"] + 1
        with fixture_policy(authority.config.state_dir.parent):
            config = replace(authority.config, acl={p: ("supervisor", "query")
                                                    for p in (authority.principal, "uid:{}".format(other))})
        authority.gate = Hostgate(config, authority.world)
        auth = authority.world.auth(pid=200, principal="uid:{}".format(other), start=(1, 2, 20, 7))
        auth = replace(auth, peer=replace(auth.peer, uid=other), anchor=replace(auth.anchor, uid=other))
        authority.world.processes[auth.peer.pid] = auth.peer
        authority.world.processes[auth.anchor.pid] = auth.anchor
        request = dict(self.current().intent.request, operationId=operation_id,
                       demand=dict(cpuMilli=99999, memoryBytes=0, diskBytes={}, slots=1))
        with default_umask():
            self.assertEqual(authority.gate.reservar(request, auth)["exitCode"], 1)

    def converges_beside(self, same_id):
        current, _ = self.closing()
        self.foreign_rejection(current.intent.operation_id if same_id else "another-operation")
        estado = self.authority.estado()
        self.assertEqual(len([p for p in estado["pending"] if p["operationId"] == current.intent.operation_id]),
                         2 if same_id else 1)
        self.assertEqual((self.reconcile(estado).action, self.raw()[0][0], self.slot()[0][0]),
                         ("released", "released", None),
                         "FACT: another principal's operation does not hide this round's original closure")

    def test_another_principals_other_operation_leaves_the_closure_alone(self):
        self.converges_beside(False)

    def test_another_principals_operation_with_this_id_leaves_the_closure_alone(self):
        """Variante: mirror_any_principal."""
        self.converges_beside(True)


class UnusedAndSeal(MirrorHarness):
    def test_an_unclaimed_grant_closes_unused_only_with_the_original_release(self):
        """Variantes: unused_without_cas, queued_to_finished."""
        held = self.grant(claim=False)
        self.assertEqual((held.intent.state, held.reason), ("held", "claim_unavailable"))
        transitions = self.ledger("SELECT count(*) FROM transitions")
        for _ in range(2):
            self.assertEqual(self.reconcile().action, "none")
            self.assertEqual(self.raw(), [("held", None)], "FACT: no closed_unused without hostgate's release")
        seal = self.request(held, "release", evidenceIds=[])
        self.assertEqual(self.authority.send(seal)["exitCode"], 0)
        self.assertEqual(self.reconcile().action, "closed_unused")
        state, creation = self.raw()[0]
        self.assertEqual((state, sorted(creation)), ("closed_unused", ["closure", "phase"]))
        self.assertEqual(creation["closure"]["evidence"]["kind"], "never-created")
        attempt = self.attempt(self.job)
        self.assertEqual(attempt["state"], "queued", "FACT: never queued->finished")
        self.assertEqual(self.ledger("SELECT count(*) FROM transitions"), transitions)
        self.assertEqual(self.slot()[0][0], None)
        view = self.round_view()
        self.assertEqual((view["error"], view["round"]["state"]), (None, "closed_unused"), view)
        self.assertTrue(self.second_round(), "FACT: a later round may compete")

    def test_a_grant_discovered_without_its_key_closes_unused_only_with_the_original_seal(self):
        """Variante: discovered_grant_stays_held."""
        intent = self.intent()
        with self.db() as db:
            with db.transaction():
                storage.put_host_intent(db, intent)
            with db.transaction():
                self.assertTrue(storage.mark_host_unknown(db, intent, self.me))
            receipt = self.authority.send(intent.request)
            self.assertTrue(receipt["granted"], receipt)
            with db.transaction():
                self.assertTrue(storage.hold_host(db, intent, "channel_lost"))
        self.assertEqual(self.reconcile().action, "none")
        self.assertEqual(self.raw(), [("held", None)], "FACT: an open grant discovered without its key keeps held")
        seal = dict(self.request(self.current(), "release", evidenceIds=[]), key=receipt["reservationKey"])
        sealed = self.authority.send(seal)
        self.assertEqual((sealed["exitCode"], sealed["state"]), (0, "released"), sealed)
        self.assertEqual(self.reconcile().action, "closed_unused")
        state, creation = self.raw()[0]
        self.assertEqual((state, sorted(creation)), ("closed_unused", ["closure", "discovered", "phase"]))
        self.assertEqual((creation["discovered"]["reservationKey"], creation["closure"]["evidence"]["kind"]),
                         (receipt["reservationKey"], "never-created"))
        self.assertEqual(self.attempt(self.job)["state"], "queued", "FACT: never queued->finished")
        view = self.round_view()
        self.assertEqual((view["error"], view["round"]["state"]), (None, "closed_unused"), view)
        self.assertTrue(self.second_round(), "FACT: a later round may compete")

    def test_a_claimed_grant_sealed_by_hostgate_is_not_closed_unused(self):
        """The slot of a claimed attempt is never released through closed_unused."""
        current = self.grant()
        self.plant("held", None, "supervisor_lost")
        self.assertEqual(self.authority.send(self.request(current, "release", evidenceIds=[]))["exitCode"], 0)
        self.assertNotEqual(self.reconcile().action, "closed_unused")
        self.assertEqual(self.raw(), [("held", None)])
        self.assertEqual(self.slot()[0][0], self.attempt_id)

    def test_consumed_without_root_stays_held_without_never_created(self):
        """A consumed permit without its root: soltar proves nothing, the round keeps held and its prefix."""
        current = self.grant()
        issued = self.authority.send(self.requests(current)["authorize"])
        consume = self.requests(current, issued["permit"]["permitId"])["consume"]
        consumed = self.authority.send(consume)
        self.assertEqual(consumed["exitCode"], 0, consumed)
        authorize = self.requests(current)["authorize"]
        creation = Creation("spawning", authorize=Step(authorize, issued), consume=Step(consume, consumed))
        self.plant("held", creation, "supervisor_lost")
        reply = self.authority.send(self.request(current, "release", evidenceIds=[]))
        self.assertNotEqual((reply["exitCode"], reply.get("state")), (0, "released"), reply)
        row = next(r for r in self.authority.estado()["reservations"] if r["key"] == current.key)
        self.assertNotEqual((row.get("closureEvidence") or {}).get("kind"), "never-created", row)
        self.reconcile()
        state, kept = self.raw()[0]
        self.assertEqual((state, kept), ("held", json.loads(storage.creation_json(creation))))
        self.assertEqual(self.slot()[0][0], self.attempt_id)


class Rejection(MirrorHarness):
    def rejected(self, principal):
        """A reservar of `principal` that hostgate rejects for good, while this round keeps held without key."""
        from hostgate.admission import Hostgate
        from hostgate.tests.test_admin import fixture_policy
        from host_creation_cases import default_umask
        authority, auth = self.authority, self.authority.auth
        with fixture_policy(authority.config.state_dir.parent):
            config = replace(authority.config, acl={p: ("supervisor", "query") for p in (authority.principal, principal)})
        authority.gate = Hostgate(config, authority.world)
        if principal != authority.principal:
            peer = replace(auth.peer, uid=auth.peer.uid + 1)
            auth = replace(auth, principal=principal, peer=peer)
            authority.world.processes[peer.pid] = peer
        intent = self.intent()
        intent = replace(intent, request=dict(intent.request, demand=dict(cpuMilli=99999, memoryBytes=0,
                                                                          diskBytes={}, slots=1)))
        with self.db() as db:
            with db.transaction():
                storage.put_host_intent(db, intent)
            with db.transaction():
                self.assertTrue(storage.mark_host_unknown(db, intent, self.me))
            with default_umask():
                self.assertEqual(authority.gate.reservar(intent.request, auth)["exitCode"], 1)
            with db.transaction():
                self.assertTrue(storage.hold_host(db, intent, "channel_lost"))
        with default_umask():
            estado = authority.gate.estado({}, auth)
        return self.reconcile(estado)

    def test_this_principals_durable_rejection_closes_held(self):
        observation = self.rejected(self.authority.principal)
        self.assertEqual((observation.action, self.raw()[0][0]), ("rejected", "rejected"))
        view = self.round_view()
        self.assertEqual((view["error"], view["round"]["state"]), (None, "rejected"), view)

    def test_another_principals_rejection_keeps_held(self):
        """Variante: rejection_any_principal."""
        observation = self.rejected("uid:{}".format(self.me["uid"] + 1))
        self.assertEqual((observation.action, self.raw()), ("none", [("held", None)]),
                         "FACT: another principal's rejection proves nothing about this reservar")


class Doubt(MirrorHarness):
    def lost(self):
        """Identity of a process that existed and was reaped: the owner is gone."""
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
        try:
            identity = native.identity(child.pid)
        finally:
            child.stdin.close()
            child.wait(timeout=30)
        self.assertFalse(native.confirmed(identity))
        return identity

    def test_held_and_uncertain_are_released_by_neither_time_nor_operator(self):
        """Variante: timeout_releases."""
        current = self.grant()
        a, c, st = self.started(current)
        running = Creation("running", authorize=Step(*a), consume=Step(*c), start=Step(*st))
        cases = {
            "inherited held": ("held", None, "enforcement_unavailable", self.me),
            "uncertain": ("running", running, None, dict(self.me, birth="another-birth")),
            "new boot": ("held", running, "supervisor_lost", dict(self.me, boot_id="another-boot")),
            "lost owner": ("held", running, "supervisor_lost", self.lost()),
        }
        later = time.time() + 30 * DAY
        for name, (state, creation, reason, supervisor) in cases.items():
            with self.subTest(case=name):
                self.plant(state, creation, reason, supervisor_identity=dump(supervisor))
                with patch("time.time", lambda: later):
                    for _ in range(2):
                        self.assertNotIn(self.reconcile().action, ("released", "closed_unused"))
                self.assertEqual(self.raw()[0][0], "held")
                self.assertEqual(self.observed()["view"], "uncertain")
                self.assertEqual(self.slot()[0][0], self.attempt_id)
        code, drained = self.op("drain", "doubt-drain")
        self.assertEqual(code, 0, drained)
        local = self.status(self.job)["local_slot"]
        selectors = (self.job, "--attempt", self.attempt_id, "--reservation", local["id"],
                     "--generation", str(local["generation"]), "--drain-id", drained["receipt"]["drain_id"])
        for action, extra in (("assess", ()), ("release", ("--proof", "p_" + "0" * 32))):
            code, refused = self.op(action, "doubt-" + action, *selectors, *extra)
            self.assertEqual((code, refused["error"]["code"]), (2, "operator_backend_unsupported"), refused)
        self.assertEqual(self.raw()[0][0], "held")
        self.assertEqual(self.slot()[0][0], self.attempt_id)
        self.assertEqual(self.attempt(self.job)["disposition"], "retained")

    def test_transport_error_is_an_unknown_result(self):
        """estado lost on the channel: the round keeps its doubt even when hostgate already released."""
        from hostgate.client import HostgateClient
        from hostgate.wire import TransportError
        self.closing()
        with patch.object(HostgateClient, "connect", side_effect=TransportError("sesión cancelada; resultado desconocido")):
            with self.db() as db:
                observation = host_reservation.reconcile(db, runner.attempt_row(db, self.attempt_id))
        self.assertEqual(observation.action, "none")
        self.assertEqual(self.raw()[0][0], "closing")
        self.assertEqual(self.slot()[0][0], self.attempt_id)
        self.plant("held", self.current().creation, "supervisor_lost")
        with patch.object(HostgateClient, "connect", side_effect=TransportError("canal perdido")):
            with self.db() as db:
                host_reservation.reconcile(db, runner.attempt_row(db, self.attempt_id))
        self.assertEqual(self.raw()[0][0], "held")
        self.assertEqual(self.observed()["view"], "uncertain")


if __name__ == "__main__":
    unittest.main()
