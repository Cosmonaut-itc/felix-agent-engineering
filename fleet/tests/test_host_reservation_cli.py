"""host-reservation@1 through the public CLI against a private effective hostgate fixture."""
import json
import os
from pathlib import Path
import select
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from test_cli import fixture_base

FLEET = Path(__file__).resolve().parents[1]
SUPPORT = FLEET / "tests/support/host_reservation.py"
HOST = "host-reservation@1"
if str(FLEET) not in sys.path:
    sys.path.insert(0, str(FLEET))


class HostHarness(unittest.TestCase):
    def setUp(self):
        from hostgate.tests.support.effective import EffectiveFixture
        from hostgate.tests.test_admin import fixture_policy
        self.root = Path(tempfile.mkdtemp(prefix="fleet-host-", dir=fixture_base())).resolve()
        self.addCleanup(shutil.rmtree, str(self.root), True)
        self.hroot, self.state, self.project = self.root / "h", self.root / "state", self.root / "project"
        for path in (self.hroot, self.project, self.root / "home"):
            path.mkdir(mode=0o700)
        umask = os.umask(0o022)  # the fixture's modes (lock 0440, config 0644) assume the default umask
        try:
            with fixture_policy(self.hroot):
                self.fixture = EffectiveFixture(self.hroot)
                self.fixture.doc["principals"][str(os.getuid())]["roles"].append("requester")
                self.fixture.provision()
        finally:
            os.umask(umask)
        self.addCleanup(self.fixture.writer.close)
        self.env = dict(os.environ, HOME=str(self.root / "home"), XDG_STATE_HOME=str(self.root / "xdg"),
                        TMPDIR=str(self.root), PYTHONDONTWRITEBYTECODE="1")
        for name in ("FLEET_STATE_DIR", "FLEET_CLIENT"):
            self.env.pop(name, None)
        self.server = None
        self.addCleanup(self.reap)
        if os.environ.get("FLEET_HOST_DIAG"):
            self.addCleanup(self.diagnose)

    def invoke(self, *args, state=None, timeout=90):
        command = [sys.executable, "-I", "-B", str(SUPPORT), str(self.hroot), "fleet",
                   "--state-dir", str(state or self.state)] + list(args)
        result = subprocess.run(command, cwd=str(self.project), env=self.env, capture_output=True,
                                text=True, timeout=timeout)
        try:
            return result.returncode, json.loads(result.stdout), result.stderr
        except ValueError:
            return result.returncode, None, result.stderr

    def call(self, *args, state=None, timeout=90):
        code, value, stderr = self.invoke(*args, state=state, timeout=timeout)
        if value is None:
            self.fail("CLI stdout is not JSON; rc={}; stderr={!r}".format(code, stderr))
        return code, value

    def submit(self, *options, counter=None, state=None, key=None, enqueue_only=False, host=True, raw=False):
        counter = counter or self.root / "counter"
        flags = (["--backend", HOST, "--profile", "fixture"] if host else []) + list(options)
        flags += (["--idempotency-key", key] if key else []) + (["--enqueue-only"] if enqueue_only else [])
        return (self.invoke if raw else self.call)("submit", *flags, "--", sys.executable, "-c",
                         "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('ran')", str(counter),
                         state=state)

    def status(self, job, state=None):
        code, result = self.call("status", job, state=state)
        self.assertEqual(code, 0, result)
        return result["observation"]

    def ledger(self, sql, parameters=(), state=None):
        # Byte copy only: never open SQLite on the live ledger or its sidecars.
        copy = Path(tempfile.mkdtemp(prefix="ledger-", dir=str(self.root)))
        for name in ("state.db", "state.db-wal"):
            if (( state or self.state) / name).exists():
                shutil.copyfile(str((state or self.state) / name), str(copy / name))
        conn = sqlite3.connect(str(copy / "state.db"))
        try:
            return conn.execute(sql, parameters).fetchall()
        finally:
            conn.close()
            shutil.rmtree(str(copy))

    def read_round(self, job, expected, state=None):
        observation = self.status(job, state=state)
        host = observation["reservation"]
        self.assertIsNone(host.get("error"), observation)
        self.assertEqual((host.get("round") or {}).get("state"), expected,
                         "FACT: status reads the product-written round: " + repr(observation))
        return observation

    def rounds(self, state=None):
        names = ("attempt", "ordinal", "operation_id", "state", "key", "receipt", "source", "reason", "heartbeat_seq")
        rows = self.ledger("SELECT attempt,ordinal,operation_id,state,key_json,receipt_json,confirmation_source,"
                           "reason,heartbeat_seq FROM host_rounds ORDER BY rowid", state=state)
        return [dict(zip(names, row), key=row[4] and json.loads(row[4]), receipt=row[5] and json.loads(row[5]))
                for row in rows]

    def attempt(self, job, state=None):
        names = ("id", "state", "gate", "command", "disposition", "generation")
        row = self.ledger("SELECT id,state,gate,command,disposition,generation FROM attempts WHERE job=?",
                          (job,), state=state)[0]
        return dict(zip(names, row), command=row[3] and json.loads(row[3]))

    def start_server(self):
        log = open(str(self.root / "server.log"), "ab")
        self.addCleanup(log.close)
        proc = subprocess.Popen([sys.executable, "-I", "-B", str(SUPPORT), str(self.hroot), "server"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log, env=self.env,
                                cwd=str(self.project), text=True)
        self.server = proc
        self.addCleanup(self.stop_server, proc)
        ready, _, _ = select.select([proc.stdout], [], [], 30)
        self.assertEqual(proc.stdout.readline().strip() if ready else None, "listening",
                         (self.root / "server.log").read_text(errors="replace"))

    def stop_server(self, proc=None):
        proc = proc or self.server
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=8)

    def hstate(self):
        result = subprocess.run([sys.executable, "-I", "-B", str(SUPPORT), str(self.hroot), "query"],
                                cwd=str(self.project), env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def log(self, name):
        path = self.hroot / name
        return path.read_text().split() if path.exists() else []

    def trace(self):
        return [tuple(line.split()) for line in (self.hroot / ".trace").read_text().splitlines()] \
            if (self.hroot / ".trace").exists() else []

    def exits(self):
        return sum(1 for _, name in self.trace() if name == "exit")

    def until(self, predicate, timeout=60, message="condition"):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                self.fail("timed out waiting for " + message)
            time.sleep(0.05)

    def reap(self):
        """Every S this test caused exits before the fixture is removed."""
        for held in self.hroot.glob(".held-*"):
            held.unlink()
        from fleet import native
        for pid in {int(pid) for pid, _ in self.trace()}:
            deadline = time.monotonic() + 70
            while time.monotonic() < deadline:
                try:
                    if not native.identity(pid)["alive"]:
                        break
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                os.kill(pid, 9)

    def diagnose(self):
        for path in (self.root / "server.log", self.state / "supervisor.log", self.hroot / ".trace",
                     self.hroot / ".reservar", self.hroot / ".offers"):
            if path.exists():
                print("DIAG {} {}:\n{}".format(self.id(), path.name, path.read_text(errors="replace")[-3000:]),
                      file=sys.stderr, flush=True)

    def op(self, action, operation_id, *options, state=None):
        return self.call("operator", action, "--operation-id", operation_id, "--reason", "fixture host check",
                         *options, state=state)

    def reserved(self, state=None, counter=None, **options):
        """Positive: one effective reservation confirmed, correlated and retained behind the barrier."""
        exits = self.exits()
        code, submitted, stderr = self.submit(state=state, counter=counter, raw=True, **options)
        rounds = self.rounds(state=state) if (state or self.state).exists() else []
        self.assertTrue(rounds, "FACT: dispatch wrote a host round")
        if code == 0 and submitted and submitted.get("accepted"):
            self.until(lambda: self.exits() > exits, message="the supervisor of " + submitted["job"])
        reservations = self.hstate()["reservations"]
        rounds = self.rounds(state=state) if (state or self.state).exists() else []
        keys = [r["key"] for r in rounds if r["key"]]
        self.assertEqual(len(keys), 1, "FACT: one confirmed key in Fleet; rc={} {}".format(code, stderr[-400:]))
        self.assertIn(keys[0], [r["key"] for r in reservations], "FACT: the Fleet key is retained in hostgate")
        current = rounds[-1]
        self.assertEqual((current["state"], current["reason"], current["heartbeat_seq"]),
                         ("held", "enforcement_unavailable", 1), "FACT: round held at the creation barrier")
        self.assertEqual(keys[0]["storeId"], self.fixture.doc["storeId"])
        self.assertTrue(keys[0]["reservationId"].startswith("e_"))
        self.assertEqual(current["receipt"]["reservationKey"], keys[0])
        self.read_round(submitted["job"], "held", state=state)
        return submitted, current


    def downgrade(self):
        self.assertEqual(self.ledger("PRAGMA user_version"), [(6,)], "FACT: a new ledger is v6")
        conn = sqlite3.connect(str(self.state / "state.db"))
        try:
            conn.execute("DROP INDEX host_rounds_open")
            conn.execute("DROP TABLE host_rounds")
            conn.execute("PRAGMA user_version=4")
            conn.commit()
        finally:
            conn.close()
        for name in ("state.db", "state.db-wal", "state.db-shm"):
            if (self.state / name).exists():
                (self.state / name).chmod(0o600)


class HostAdmission(HostHarness):
    def test_new_admission_requires_effective_authority_and_replay_keeps_receipt(self):
        code, first = self.submit(key="k1", enqueue_only=True)
        self.assertEqual((code, first.get("accepted"), first.get("binding")), (0, True, "new"), first)
        content = json.loads(self.ledger("SELECT content FROM jobs WHERE id=?", (first["job"],))[0][0])
        self.assertEqual(content["execution"], {"backend": HOST, "hostSpec": {"profileId": "fixture"}})
        observation = self.status(first["job"])
        self.assertEqual((observation["state"], observation["reservation"]["kind"]), ("queued", HOST))
        # FIFO fence: a pilot behind a host head is accepted, never claimed and never run.
        code, pilot = self.submit(counter=self.root / "pilot-ran", host=False)
        self.assertEqual((code, pilot.get("accepted")), (0, True), pilot)
        self.assertEqual(self.status(pilot["job"])["state"], "queued")
        self.assertFalse((self.root / "pilot-ran").exists())
        self.assertEqual(self.ledger("SELECT attempt FROM slot"), [(None,)])
        self.assertEqual(self.ledger("SELECT count(*) FROM transitions WHERE cause='claim'"), [(0,)])

        self.fixture.locator.config_path.rename(self.root / "node.off")
        code, replay = self.submit(key="k1", enqueue_only=True)
        self.assertEqual((code, replay["binding"], replay["job"]), (0, "replay", first["job"]), replay)
        jobs = self.ledger("SELECT id,content,execution FROM jobs ORDER BY seq")
        code, missing = self.submit(key="k2", enqueue_only=True)
        self.assertEqual((code, missing.get("error", {}).get("code"), missing.get("accepted")),
                         (2, "backend_unavailable", False), missing)
        fresh = self.root / "fresh-state"
        code, missing = self.submit(state=fresh)
        self.assertEqual((code, missing["error"]["code"]), (2, "backend_unavailable"), missing)
        self.assertFalse(fresh.exists())
        code, conflict = self.submit(key="k1", enqueue_only=True, host=False)
        self.assertEqual((code, conflict["error"]["code"]), (5, "idempotency_conflict"), conflict)
        for flags in (["--backend", "unknown-backend@9"], ["--backend", HOST], ["--profile", "fixture"]):
            code, rejected = self.call("submit", *flags, "--", sys.executable, "-c", "pass")
            self.assertEqual(code, 2, rejected)
            self.assertIn(rejected["error"]["code"], ("backend_unsupported", "invalid_profile"))
        self.assertEqual(self.ledger("SELECT id,content,execution FROM jobs ORDER BY seq"), jobs)

    def test_v4_ledger_is_read_without_migration_and_migrated_once_by_writer(self):
        code, pilot = self.submit(host=False)
        self.assertEqual(code, 0, pilot)
        code, done = self.call("wait", pilot["job"], "--timeout", "30")
        self.assertEqual(code, 0, done)
        self.downgrade()
        jobs = self.ledger("SELECT * FROM jobs ORDER BY seq")
        self.assertEqual(self.ledger("PRAGMA user_version"), [(4,)])
        self.assertEqual(self.status(pilot["job"])["completion"], "complete")
        self.assertEqual(self.ledger("PRAGMA user_version"), [(4,)])
        code, recovered = self.call("recover")
        self.assertEqual(code, 0, recovered)
        self.assertEqual(self.ledger("PRAGMA user_version"), [(6,)])
        self.assertEqual(self.ledger("SELECT name FROM sqlite_master WHERE name LIKE 'host_rounds%' ORDER BY name"),
                         [("host_rounds",), ("host_rounds_open",)])
        self.assertEqual(self.ledger("SELECT * FROM jobs ORDER BY seq"), jobs)

    def test_v4_ledger_with_an_owned_slot_is_migrated_only_after_its_release(self):
        (self.root / "hold").write_text("")  # removing the fixture root also lets the command end
        code, pilot = self.call("submit", "--", sys.executable, "-c", "import os,sys,time\n"
                                "while os.path.exists(sys.argv[1]): time.sleep(0.05)", str(self.root / "hold"))
        self.assertEqual((code, pilot.get("accepted")), (0, True), pilot)
        self.downgrade()
        self.call("recover")
        code, host = self.submit(key="k1", enqueue_only=True)
        self.assertEqual((code, host.get("error", {}).get("code")), (2, "backend_unavailable"), host)
        self.assertEqual(self.ledger("PRAGMA user_version"), [(4,)], "FACT: an owned slot defers the migration")
        (self.root / "hold").unlink()
        code, done = self.call("wait", pilot["job"], "--timeout", "30")
        self.assertEqual((code, done["observation"]["completion"]), (0, "complete"), done)
        self.call("recover")
        self.assertEqual(self.ledger("PRAGMA user_version"), [(6,)])


class HostReservation(HostHarness):
    def setUp(self):
        super().setUp()
        self.start_server()

    def test_confirmed_then_creation_closed(self):
        counter = self.root / "counter"
        for name in ("confirmed", "barrier"):
            (self.hroot / (".pause-" + name)).touch()
        code, submitted = self.submit(counter=counter)
        self.assertEqual((code, submitted.get("accepted")), (0, True), submitted)
        self.assertTrue(self.rounds(), "FACT: dispatch wrote a host round")
        self.until(lambda: (self.hroot / ".at-confirmed").exists() or self.exits() > 0, message="S at confirmed")
        self.assertTrue((self.hroot / ".at-confirmed").exists(), "FACT: confirmation persisted before creation")
        confirmed, = self.rounds()
        self.read_round(submitted["job"], "confirmed")
        attempt = self.attempt(submitted["job"])
        self.assertEqual((confirmed["state"], confirmed["source"], confirmed["heartbeat_seq"]),
                         ("confirmed", "reply", 0), "FACT: confirmed before any start")
        self.assertEqual((attempt["state"], attempt["gate"], attempt["generation"]), ("starting", "available", 1))
        hstate = self.hstate()
        self.assertEqual([r["key"] for r in hstate["reservations"]], [confirmed["key"]])
        self.assertFalse(counter.exists())
        (self.hroot / ".held-confirmed").unlink()
        self.until(lambda: (self.hroot / ".at-barrier").exists() or self.exits() > 0, message="S at the barrier")
        self.assertTrue((self.hroot / ".at-barrier").exists(), "FACT: creation barrier closed the permit")
        held, = self.rounds()
        self.read_round(submitted["job"], "held")
        attempt = self.attempt(submitted["job"])
        self.assertEqual((held["state"], held["reason"], held["heartbeat_seq"]),
                         ("held", "enforcement_unavailable", 1), "FACT: held at the barrier")
        self.assertEqual((held["key"], held["receipt"], held["source"]),
                         (confirmed["key"], confirmed["receipt"], "reply"))
        self.assertEqual((attempt["state"], attempt["gate"], attempt["command"]["kind"]),
                         ("finished", "closed", "never_created"))
        self.assertEqual([r["key"] for r in self.hstate()["reservations"]], [confirmed["key"]])
        self.assertFalse(counter.exists())
        (self.hroot / ".held-barrier").unlink()
        self.until(lambda: self.exits() == 1, message="S exit")
        code, waited = self.call("wait", submitted["job"], "--timeout", "10")
        self.assertEqual((code, waited["execution_error"]["code"]), (3, "enforcement_unavailable"), waited)
        final, = self.rounds()
        self.assertEqual((final["key"], final["receipt"]), (confirmed["key"], confirmed["receipt"]))
        self.assertEqual(self.attempt(submitted["job"])["gate"], "closed")
        self.assertFalse(counter.exists())
        # admission made the attempt directory; only execute() would materialize a workspace in it
        self.assertEqual(list((self.state / "attempts" / attempt["id"]).iterdir()), [])
        hstate = self.hstate()
        self.assertEqual([r["key"] for r in hstate["reservations"]], [confirmed["key"]])
        self.assertEqual(hstate["budget"]["retained"]["slots"], 1)
        self.assertEqual(self.ledger("SELECT attempt,generation FROM slot"), [(attempt["id"], 1)])

    def test_two_ledgers_one_budget(self):
        first, _ = self.reserved()
        other, counter = self.root / "other-state", self.root / "other-ran"
        exits = self.exits()
        code, second = self.submit(state=other, counter=counter)
        self.assertEqual((code, second.get("accepted")), (0, True), second)
        self.until(lambda: self.exits() > exits, message="the second supervisor")
        rejected, = self.rounds(state=other)
        self.read_round(second["job"], "rejected", state=other)
        self.assertEqual((rejected["state"], rejected["key"]), ("rejected", None), "FACT: second ledger rejected")
        self.assertTrue(rejected["receipt"]["reasons"])
        self.assertEqual(self.status(second["job"], state=other)["state"], "queued")
        self.assertEqual(self.ledger("SELECT attempt FROM slot", state=other), [(None,)])
        self.assertFalse(counter.exists())
        hstate = self.hstate()
        self.assertEqual(len(hstate["reservations"]), 1)
        self.assertEqual(hstate["budget"]["retained"]["slots"], 1)

    def test_shadow_reply_never_confirms_nor_falls_back_to_pilot(self):
        (self.hroot / ".shadow-reply").touch()
        counter = self.root / "shadow-ran"
        code, shadow = self.submit(counter=counter)
        self.assertEqual((code, shadow.get("accepted")), (0, True), shadow)
        self.assertTrue(self.rounds(), "FACT: shadow request has a host round")
        self.until(lambda: self.exits() == 1, message="the shadow supervisor")
        self.assertEqual([(r["state"], r["reason"], r["key"]) for r in self.rounds()],
                         [("held", "response_not_effective", None)], "FACT: shadow reply held")
        self.read_round(shadow["job"], "held")
        self.call("recover")
        self.assertEqual(self.status(shadow["job"])["state"], "queued")
        self.assertEqual(self.ledger("SELECT attempt FROM slot"), [(None,)])
        self.assertEqual(self.ledger("SELECT count(*) FROM transitions WHERE cause='claim'"), [(0,)])
        self.assertFalse(counter.exists())
        self.assertEqual((self.log(".reservar"), self.hstate()["reservations"]), ([], []))
        (self.hroot / ".shadow-reply").unlink()
        self.reserved(state=self.root / "positive-state", counter=self.root / "positive-ran")
        self.assertFalse((self.root / "positive-ran").exists())

    def test_operator_declared_pilot_does_not_release_host_job(self):
        submitted, current = self.reserved()
        drain = self.op("drain", "host-drain")[1]["receipt"]["drain_id"]
        local = self.status(submitted["job"])["local_slot"]
        selectors = (submitted["job"], "--attempt", submitted["attempt"], "--reservation", local["id"],
                     "--generation", str(local["generation"]), "--drain-id", drain)
        for action, extra in (("assess", ()), ("release", ("--proof", "p_" + "0" * 32))):
            code, refused = self.op(action, "host-" + action, *selectors, *extra)
            self.assertEqual((code, refused["error"]["code"]), (2, "operator_backend_unsupported"), refused)
        self.assertEqual(self.ledger("SELECT attempt,generation FROM slot"), [(submitted["attempt"], 1)])
        self.assertEqual(self.attempt(submitted["job"])["disposition"], "retained")
        self.assertEqual([r["key"] for r in self.hstate()["reservations"]], [current["key"]])

    def test_pilot_claim_with_host_head_returns_none(self):
        code, head = self.submit(key="head", enqueue_only=True)
        self.assertEqual(code, 0, head)
        pilot_ran = self.root / "pilot-ran"
        code, pilot = self.submit(counter=pilot_ran, host=False)
        self.assertEqual((code, pilot.get("accepted")), (0, True), pilot)
        # The pilot submit dispatched the host head through its own round, never the pilot.
        self.assertTrue(self.rounds(), "FACT: the host FIFO head has a round")
        self.until(lambda: self.exits() == 1, message="the head supervisor")
        current, = self.rounds()
        self.read_round(head["job"], "held")
        self.assertEqual((current["attempt"], current["state"], current["source"]),
                         (head["attempt"], "held", "reply"), "FACT: the host head was reserved")
        self.assertEqual([r["key"] for r in self.hstate()["reservations"]], [current["key"]])
        self.assertEqual(self.status(pilot["job"])["state"], "queued")
        self.assertEqual(self.ledger("SELECT attempt FROM transitions WHERE cause='claim'"), [(head["attempt"],)])
        self.assertFalse(pilot_ran.exists())

    def test_outcome_unknown_then_drain_retains_grant_without_claim(self):
        (self.hroot / ".pause-outcome_unknown").touch()
        code, submitted = self.submit(enqueue_only=True)
        recovery = subprocess.Popen([sys.executable, "-I", "-B", str(SUPPORT), str(self.hroot), "fleet",
                                     "--state-dir", str(self.state), "recover"], cwd=str(self.project), env=self.env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop_server, recovery)
        self.until(lambda: bool(self.rounds()) or recovery.poll() is not None, message="round or recover exit")
        self.assertTrue(self.rounds(), "FACT: dispatch wrote a host round")
        self.until(lambda: (self.hroot / ".at-outcome_unknown").exists(), message="CAS before send")
        self.read_round(submitted["job"], "outcome_unknown")
        self.assertEqual(self.log(".offers"), [self.rounds()[0]["operation_id"]], "H paired S before the CAS")
        self.assertEqual((self.log(".reservar"), self.hstate()["reservations"]), ([], []))
        self.assertEqual(self.op("drain", "before-grant")[0], 0)
        (self.hroot / ".held-outcome_unknown").unlink()
        recovery.communicate(timeout=60)
        self.until(lambda: self.exits() == 1, message="S retained grant after drain")
        held, = self.rounds()
        observation = self.read_round(submitted["job"], "held")
        self.assertEqual((held["reason"], held["source"], observation["state"]),
                         ("claim_unavailable", "reply", "queued"))
        self.assertEqual(held["receipt"]["reservationKey"], held["key"])
        self.assertEqual([r["key"] for r in self.hstate()["reservations"]], [held["key"]])
        self.assertEqual(self.ledger("SELECT attempt FROM slot"), [(None,)])
        self.assertEqual(self.ledger("SELECT count(*) FROM transitions WHERE cause='claim'"), [(0,)])
        self.assertFalse((self.root / "counter").exists())

    def test_altered_receipt_on_a_real_confirmation_fails_closed(self):
        (self.hroot / ".pause-confirmed").touch()
        code, submitted = self.submit()
        self.assertTrue(self.rounds(), "FACT: dispatch wrote a host round")
        self.until(lambda: (self.hroot / ".at-confirmed").exists(), message="real confirmation")
        self.read_round(submitted["job"], "confirmed")
        confirmed, = self.rounds()
        self.assertEqual([r["key"] for r in self.hstate()["reservations"]], [confirmed["key"]])
        original = json.dumps(confirmed["receipt"], sort_keys=True, separators=(",", ":"))
        altered = json.dumps(dict(confirmed["receipt"], operationId="other-operation"),
                             sort_keys=True, separators=(",", ":"))
        def write(value):
            with sqlite3.connect(str(self.state / "state.db")) as conn:
                conn.execute("UPDATE host_rounds SET receipt_json=?", (value,))
            for path in self.state.glob("state.db*"):
                path.chmod(0o600)
        try:
            write(altered)
            result = self.call("status", submitted["job"])[1]
            self.assertEqual(result["observation"]["reservation"]["error"], "host_correlation_invalid")
            self.assertEqual(result["execution_error"], {"code": "host_correlation_invalid"})
        finally:
            write(original)
            (self.hroot / ".held-confirmed").unlink()
        self.until(lambda: self.exits() == 1, message="S exit after receipt restored")
        self.read_round(submitted["job"], "held")

    def test_deferred_migration_blocks_host_dispatch(self):
        positive, retained = self.reserved(state=self.root / "positive-state")
        offers = self.log(".offers")
        hold = self.root / "pilot-hold"
        hold.touch()
        code, pilot = self.call("submit", "--", sys.executable, "-c", "import os,sys,time\n"
                                "while os.path.exists(sys.argv[1]): time.sleep(0.05)", str(hold))
        self.assertEqual(code, 0, pilot)
        # Always let the real pilot finish, including assertion failures.
        def finish():
            if hold.exists():
                hold.unlink()
            self.call("wait", pilot["job"], "--timeout", "30")
        self.addCleanup(finish)
        code, host = self.submit(enqueue_only=True)
        self.assertEqual(code, 0, host)
        self.downgrade()
        code, rejected = self.submit(key="blocked", enqueue_only=True)
        self.assertEqual((code, rejected.get("error", {}).get("code")), (2, "backend_unavailable"), rejected)
        code, recovered = self.call("recover")
        self.assertEqual((code, recovered.get("error", {}).get("code")), (3, "backend_unavailable"), recovered)
        self.assertEqual(self.ledger("PRAGMA user_version"), [(4,)])
        self.assertEqual(self.ledger("SELECT name FROM sqlite_master WHERE name='host_rounds'"), [])
        self.assertEqual(self.log(".offers"), offers)
        self.assertEqual([r["key"] for r in self.hstate()["reservations"]], [retained["key"]])
        finish()
        self.call("recover")
        self.assertTrue(self.rounds(), "FACT: the migrated ledger dispatched its host head")
        self.until(lambda: self.exits() == 2, message="S exit after migration")
        self.read_round(host["job"], "rejected")
        self.assertEqual([r["key"] for r in self.hstate()["reservations"]], [retained["key"]])


if __name__ == "__main__":
    unittest.main()
