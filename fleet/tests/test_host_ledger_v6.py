"""SEN-5650 4d: ledger v6. creation_json by variant with real 4c receipts, read by the public status CLI."""
from dataclasses import replace
import sqlite3
import unittest

from host_creation_cases import CreationHarness
from fleet import storage
from fleet.model import Creation, Step


class Shapes(CreationHarness):
    def setUp(self):
        super().setUp()
        self.granted = self.grant()
        self.steps = s = self.walk(self.granted)
        a, c, st = s["authorize"], s["consume"], s["start"]
        self.phases = {
            "authorizing": Creation("authorizing", authorize=replace(a, receipt=None)),
            "authorized": Creation("authorized", authorize=a),
            "spawning": Creation("spawning", authorize=a, consume=replace(c, receipt=None)),
            "running": Creation("running", authorize=a, consume=c, start=st),
            "closing": Creation("closing", authorize=a, consume=c, start=st, release=s["busy"]),
            "released": Creation("released", authorize=a, consume=c, start=st, release=s["release"],
                                 closure=s["closure"]),
        }

    def accepted(self, state, creation, reason=None):
        self.plant(state, creation, reason)
        view = self.round_view()
        self.assertIsNone(view["error"], view)
        self.assertEqual((view["round"]["state"], view["round"]["reason"]), (state, reason))
        return view

    def refused(self, state, creation, reason=None):
        self.plant(state, creation, reason)
        view = self.round_view()
        self.assertEqual((view["error"], view["round"]), ("host_correlation_invalid", None), view)

    def test_each_phase_accepts_only_its_own_variant(self):
        """Variante: free_shape."""
        for phase, creation in self.phases.items():
            with self.subTest(phase=phase):
                self.accepted(phase, creation)
        s, phases = self.steps, self.phases
        a, c, st = s["authorize"], s["consume"], s["start"]
        wrong = {
            "an extra receipt": ("authorized", Creation("authorized", authorize=a, consume=c)),
            "a closure without its release": ("released", Creation("released", authorize=a, consume=c, start=st,
                                                                   closure=s["closure"])),
            "a missing receipt": ("running", replace(phases["running"], start=replace(st, receipt=None))),
            "a receipt in authorizing": ("authorizing", Creation("authorizing", authorize=a)),
            "another phase's variant": ("running", replace(phases["authorized"], phase="running")),
            "another phase's label": ("authorized", replace(phases["authorized"], phase="running")),
            "another step's receipt": ("authorized", Creation("authorized", authorize=replace(a, receipt=c.receipt))),
            "a busy reply as closure": ("released", replace(phases["released"], release=s["busy"],
                                                           closure=replace(s["closure"], permit=s["busy"].receipt["permit"]))),
            "held from a final phase": ("held", phases["released"]),
        }
        for name, (state, creation) in wrong.items():
            with self.subTest(case=name):
                self.refused(state, creation, "supervisor_lost" if state == "held" else None)
        for state, creation in (("launching", phases["running"]), ("running", None),
                                ("confirmed", phases["authorized"])):
            with self.subTest(check=state), self.assertRaises(sqlite3.IntegrityError):
                self.plant(state, creation)

    def test_open_index_does_not_count_released_rounds(self):
        """Variante: open_index_counts_released."""
        self.plant("running", self.phases["running"])
        self.assertFalse(self.second_round(), "FACT: an open round admits no other")
        self.plant("released", self.phases["released"])
        self.assertTrue(self.second_round(), "FACT: released does not count as open")
        self.assertEqual([state for state, _ in self.raw()], ["released", "prepared"])
        view = self.round_view()
        self.assertEqual((view["error"], view["round"]["ordinal"], view["round"]["state"]), (None, 2, "prepared"), view)

    def test_held_keeps_the_exact_prefix_and_permit(self):
        """Variante: held_drops_permit."""
        for phase in ("authorizing", "authorized", "spawning", "running", "closing"):
            with self.subTest(phase=phase):
                self.plant(phase, self.phases[phase])
                before = self.raw()[0][1]
                with self.db() as db, db.transaction():
                    self.assertTrue(storage.hold_host(db, self.current(db).intent, "supervisor_lost"))
                self.assertEqual(self.raw(), [("held", before)])
                self.assertEqual(before["phase"], phase)
                self.accepted("held", self.phases[phase], "supervisor_lost")


class UnusedAndRejected(CreationHarness):
    def test_closed_unused_and_discovered_rejection_shapes(self):
        """A grant never claimed seals unissued; a reservar estado shows rejected. Variante: open_index_counts_released."""
        held = self.grant(claim=False)
        self.assertEqual((held.intent.state, held.reason), ("held", "claim_unavailable"))
        seal = self.request(held, "release", evidenceIds=[])
        sealed = Step(seal, self.authority.send(seal))
        closure = self.closure(held)
        self.assertEqual((closure.permit["state"], closure.evidence["kind"]), ("sealed", "never-created"))
        for creation in (Creation("closed_unused", release=sealed, closure=closure),
                         Creation("closed_unused", closure=closure)):
            with self.subTest(variant=sorted(creation.__dict__)):
                self.plant("closed_unused", creation, "claim_unavailable")
                view = self.round_view()
                self.assertEqual((view["error"], view["round"]["state"]), (None, "closed_unused"), view)
        authorize = Step(self.requests(held)["authorize"], None)
        self.plant("closed_unused", Creation("closed_unused", authorize=authorize, release=sealed, closure=closure),
                   "claim_unavailable")
        self.assertEqual(self.round_view()["error"], "host_correlation_invalid")
        self.plant("closed_unused", Creation("closed_unused", closure=closure), "claim_unavailable")
        self.assertTrue(self.second_round(), "FACT: closed_unused does not count as open")
        self.assertEqual([state for state, _ in self.raw()], ["closed_unused", "prepared"])

    def test_discovered_rejection_shape(self):
        """estado's pending entry of this exact reservar: rejected keeps the held reason."""
        intent = self.intent()
        intent = replace(intent, request=dict(intent.request, demand=dict(cpuMilli=99999, memoryBytes=0,
                                                                          diskBytes={}, slots=1)))
        with self.db() as db:
            with db.transaction():
                storage.put_host_intent(db, intent)
            with db.transaction():
                self.assertTrue(storage.mark_host_unknown(db, intent, self.me))
            self.assertEqual(self.authority.send(intent.request)["exitCode"], 1)
            with db.transaction():
                self.assertTrue(storage.hold_host(db, intent, "channel_lost"))
        pending = next(p for p in self.authority.estado()["pending"] if p["operationId"] == intent.operation_id)
        self.plant("rejected", Creation("rejected", discovered=pending), "channel_lost")
        view = self.round_view()
        self.assertEqual((view["error"], view["round"]["state"], view["round"]["reason"]),
                         (None, "rejected", "channel_lost"), view)
        for name, change in (("digest", dict(requestDigest="0" * 64)), ("attempt", dict(attemptId="a_other")),
                             ("granted", dict(state="granted", effectiveState="granted"))):
            with self.subTest(case=name):
                self.plant("rejected", Creation("rejected", discovered=dict(pending, **change)), "channel_lost")
                self.assertEqual(self.round_view()["error"], "host_correlation_invalid")


if __name__ == "__main__":
    unittest.main()
