"""host-reservation@1 through the public CLI against a private effective hostgate fixture."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
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
        with fixture_policy(self.hroot):
            self.fixture = EffectiveFixture(self.hroot)
            self.fixture.doc["principals"][str(os.getuid())]["roles"].append("requester")
            self.fixture.provision()
        self.addCleanup(self.fixture.writer.close)
        self.env = dict(os.environ, HOME=str(self.root / "home"), XDG_STATE_HOME=str(self.root / "xdg"),
                        TMPDIR=str(self.root), PYTHONDONTWRITEBYTECODE="1")
        for name in ("FLEET_STATE_DIR", "FLEET_CLIENT"):
            self.env.pop(name, None)

    def call(self, *args, state=None, timeout=60):
        command = [sys.executable, "-I", "-B", str(SUPPORT), str(self.hroot), "fleet",
                   "--state-dir", str(state or self.state)] + list(args)
        result = subprocess.run(command, cwd=str(self.project), env=self.env, capture_output=True,
                                text=True, timeout=timeout)
        try:
            return result.returncode, json.loads(result.stdout)
        except ValueError:
            self.fail("CLI stdout is not JSON; rc={}; stdout={!r}; stderr={!r}".format(
                result.returncode, result.stdout, result.stderr))

    def submit(self, *options, counter=None, state=None, key=None, enqueue_only=False, host=True):
        counter = counter or self.root / "counter"
        flags = (["--backend", HOST, "--profile", "fixture"] if host else []) + list(options)
        flags += (["--idempotency-key", key] if key else []) + (["--enqueue-only"] if enqueue_only else [])
        return self.call("submit", *flags, "--", sys.executable, "-c",
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
        self.assertEqual((code, missing["error"]["code"], missing["accepted"]), (2, "backend_unavailable", False))
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
        self.assertEqual(self.ledger("PRAGMA user_version"), [(5,)], "FACT: a new ledger is v5")
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
        jobs = self.ledger("SELECT * FROM jobs ORDER BY seq")
        self.assertEqual(self.ledger("PRAGMA user_version"), [(4,)])
        self.assertEqual(self.status(pilot["job"])["completion"], "complete")
        self.assertEqual(self.ledger("PRAGMA user_version"), [(4,)])
        code, recovered = self.call("recover")
        self.assertEqual(code, 0, recovered)
        self.assertEqual(self.ledger("PRAGMA user_version"), [(5,)])
        self.assertEqual(self.ledger("SELECT name FROM sqlite_master WHERE name LIKE 'host_rounds%' ORDER BY name"),
                         [("host_rounds",), ("host_rounds_open",)])
        self.assertEqual(self.ledger("SELECT * FROM jobs ORDER BY seq"), jobs)


if __name__ == "__main__":
    unittest.main()
