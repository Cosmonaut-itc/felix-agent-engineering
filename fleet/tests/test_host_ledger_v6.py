"""SEN-5650 4d: ledger v6. creation_json by variant with real 4c receipts, read by the public status CLI."""
from dataclasses import replace
import sqlite3
import unittest

from host_creation_cases import CreationHarness
from fleet import storage
from fleet.model import Creation, Step

# The v5 host schema exactly as origin/main 433e1ff9 created it: history, not code under test.
V5_SCHEMA = (
    "CREATE TABLE host_rounds (attempt TEXT NOT NULL REFERENCES attempts(id), "
    "ordinal INTEGER NOT NULL CHECK(ordinal>=1), operation_id TEXT UNIQUE NOT NULL "
    "CHECK(length(operation_id) BETWEEN 1 AND 256), request_json TEXT NOT NULL, "
    "request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64), physical_host_id TEXT NOT NULL, "
    "executor_id TEXT NOT NULL, store_id TEXT NOT NULL, config_revision TEXT NOT NULL, "
    "requester_identity TEXT NOT NULL, supervisor_identity TEXT, state TEXT NOT NULL CHECK(state IN "
    "('prepared','outcome_unknown','confirmed','held','rejected','retired')), key_json TEXT, "
    "receipt_json TEXT, confirmation_source TEXT CHECK(confirmation_source IN ('reply','same-channel-replay')), "
    "reason TEXT, heartbeat_seq INTEGER NOT NULL DEFAULT 0 CHECK(heartbeat_seq>=0), "
    "PRIMARY KEY(attempt,ordinal), "
    "CHECK((supervisor_identity IS NULL)=(state IN ('prepared','retired'))), "
    "CHECK((key_json IS NULL)=(confirmation_source IS NULL)), "
    "CHECK(state NOT IN ('prepared','outcome_unknown','retired') OR "
    "(key_json IS NULL AND receipt_json IS NULL)), "
    "CHECK(state<>'confirmed' OR (key_json IS NOT NULL AND receipt_json IS NOT NULL AND reason IS NULL)), "
    "CHECK(state<>'rejected' OR (key_json IS NULL AND receipt_json IS NOT NULL)), "
    "CHECK(state<>'held' OR reason IS NOT NULL))",
    "CREATE UNIQUE INDEX host_rounds_open ON host_rounds(attempt) "
    "WHERE state NOT IN ('rejected','retired')",
)


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
        """Variantes: free_shape y spawning_takes_start_receipt."""
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
            "running's receipts under spawning": ("spawning", replace(phases["running"], phase="spawning")),
            "held from spawning with running's receipts": ("held", replace(phases["running"], phase="spawning")),
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
        """Variantes: open_index_counts_released y open_index_skips_creation."""
        for phase in storage.CREATION_PHASES:
            with self.subTest(phase=phase):
                self.plant(phase, self.phases[phase])
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


class Migration(CreationHarness):
    """v4 and v5 history through the public CLI: status reads it as is, recover migrates it once."""

    def downgrade(self, version):
        """Rewrite this ledger offline as v4 (no host rounds) or v5 (every v5 column kept)."""
        conn = sqlite3.connect(str(self.state / "state.db"))
        try:
            with conn:
                conn.execute("DROP INDEX host_rounds_open")
                if version == 5:
                    conn.execute("ALTER TABLE host_rounds RENAME TO host_rounds_v6")
                    conn.execute(V5_SCHEMA[0])
                    conn.execute("INSERT INTO host_rounds ({0}) SELECT {0} FROM host_rounds_v6 ORDER BY rowid"
                                 .format(storage.HOST_COLUMNS))
                    conn.execute("DROP TABLE host_rounds_v6")
                    conn.execute(V5_SCHEMA[1])
                else:
                    conn.execute("DROP TABLE host_rounds")
                conn.execute("PRAGMA user_version={}".format(version))
        finally:
            conn.close()
        for path in self.state.glob("state.db*"):
            path.chmod(0o600)
        self.assertEqual(self.ledger("PRAGMA user_version"), [(version,)])

    def history(self):
        tables = {name: self.ledger("SELECT * FROM {} ORDER BY rowid".format(name))
                  for name in ("jobs", "attempts", "operations", "transitions", "slot")}
        if self.ledger("SELECT 1 FROM sqlite_master WHERE name='host_rounds'"):
            tables["host_rounds"] = self.ledger("SELECT {} FROM host_rounds ORDER BY rowid".format(storage.HOST_COLUMNS))
        return tables

    def files(self):
        return {path.name: path.read_bytes() for path in sorted(self.state.glob("state.db*"))}

    def columns(self):
        return [row[1] for row in self.ledger("PRAGMA table_info(host_rounds)")]

    def test_v5_receipts_and_held_migrate_with_the_slot_free(self):
        """Variantes: rewrite_history, reader_migrates."""
        held = self.grant(claim=False)
        self.assertEqual((held.intent.state, held.reason), ("held", "claim_unavailable"))
        before, view = self.history(), self.round_view()
        self.downgrade(5)
        self.assertEqual(self.history(), before)
        image = self.files()
        self.assertEqual(self.round_view(), view)
        self.assertEqual(self.files(), image, "FACT: a reader neither migrates nor writes")
        self.call("recover")
        self.assertEqual(self.ledger("PRAGMA user_version"), [(6,)])
        self.assertEqual(self.history(), before, "FACT: jobs, receipts and held stay byte-identical")
        self.assertEqual((self.columns()[-1], self.ledger("SELECT creation_json FROM host_rounds")),
                         ("creation_json", [(None,)]))
        self.assertEqual(self.ledger("SELECT sql FROM sqlite_master WHERE name LIKE 'host_rounds%' ORDER BY name"),
                         [(sql,) for sql in storage.HOST_SCHEMA])
        self.assertEqual(self.round_view(), view)

    def test_v4_history_migrates_once_with_the_slot_free(self):
        """A v4 ledger is pilot history. Variantes: rewrite_history, reader_migrates."""
        self.state = self.root / "v4-state"
        code, pilot = self.submit(host=False)
        self.assertEqual(code, 0, pilot)
        self.job = pilot["job"]
        code, done = self.call("wait", self.job, "--timeout", "30")
        self.assertEqual(code, 0, done)
        self.downgrade(4)
        before, image = self.history(), self.files()
        self.assertEqual(self.observed()["completion"], "complete")
        self.assertEqual(self.files(), image, "FACT: a reader neither migrates nor writes")
        self.call("recover")
        self.assertEqual(self.ledger("PRAGMA user_version"), [(6,)])
        self.assertEqual(self.history(), dict(before, host_rounds=[]))
        self.assertIn("creation_json", self.columns())

    def test_a_live_slot_defers_the_v5_migration(self):
        """Variante: migrate_live_writer."""
        self.grant()
        self.assertEqual(self.ledger("SELECT attempt FROM slot"), [(self.attempt_id,)])
        self.downgrade(5)
        before = self.history()
        self.call("recover")
        self.assertEqual(self.ledger("PRAGMA user_version"), [(5,)], "FACT: an owned slot defers the migration")
        self.assertNotIn("creation_json", self.columns())
        self.assertEqual(self.history()["host_rounds"], before["host_rounds"])
        conn = sqlite3.connect(str(self.state / "state.db"))
        try:
            with conn:
                conn.execute("UPDATE slot SET attempt=NULL")
        finally:
            conn.close()
        self.call("recover")
        self.assertEqual(self.ledger("PRAGMA user_version"), [(6,)])
        self.assertEqual(self.history()["host_rounds"], before["host_rounds"])


if __name__ == "__main__":
    unittest.main()
