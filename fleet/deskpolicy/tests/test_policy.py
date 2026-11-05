import ast
import contextlib
import fcntl
import datetime as dt
import io
import json
import multiprocessing as mp
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from deskpolicy import cli
from deskpolicy.engine import DeskPolicy
from deskpolicy.model import CONFIG, Invalid, allocate, utc_text
from deskpolicy.store import Runtime, atomic_json, mutex, open_file, process, read_json
from deskpolicy.testing import Harness


def actor(harness, request, lane, connection, barrier=None):
    os.setsid()
    engine = DeskPolicy(Runtime(harness))
    record = read_json(engine.record_path(request))
    life = open_file(engine.life_path(record["project"], lane), create=True)
    try:
        if barrier:
            barrier.wait(timeout=30)
        deadline = time.monotonic() + 180
        while True:
            try:
                with mutex(engine.root) as lock:
                    receipt = engine.admit(request, "run-" + request, lane, os.getpid(), life, lock)
            except Invalid as exc:
                policy, _ = engine.read_policy(harness.sample())
                receipt = engine.receipt(request, "run-" + request, policy, lane,
                                         reason="mutex-error" if str(exc) == "mutex-timeout" else "census-unknown")
                receipt["detail"] = str(exc)
            retry = (barrier and receipt["reason"] in ("fifo", "mutex-error", "quota-full")
                     and time.monotonic() < deadline)
            if not retry:
                break
            fcntl.flock(life, fcntl.LOCK_UN)
            time.sleep(0.05)
        connection.send(receipt)
        if not receipt["execute"]:
            return
        while True:
            message = connection.recv()
            if message[0] == "close":
                harness.bt, harness.now = message[1:]
                try:
                    with mutex(engine.root) as lock:
                        result = engine.close(request, "run-" + request, os.getpid(), life, lock,
                                              claim_token=receipt["claim_token"])
                    connection.send(result["claim"]["state"])
                    return
                except Invalid as exc:
                    connection.send(str(exc))
            elif message[0] == "child":
                child = subprocess.Popen(["/usr/bin/taskset", "-c", ",".join(map(str, receipt["cpus"])),
                                          sys.executable, "-I", "-c", "import time; time.sleep(60)"],
                                         close_fds=True)
                connection.send(child.pid)
            elif message[0] == "affinity":
                program = (
                    "import os, subprocess, sys, json; "
                    "p=[subprocess.Popen([sys.executable,'-I','-c',"
                    "'import os,json; print(json.dumps(sorted(os.sched_getaffinity(0))))'],"
                    "stdout=subprocess.PIPE,text=True) for _ in range(7)]; "
                    "print(json.dumps([json.loads(c.communicate()[0]) for c in p]))")
                result = subprocess.check_output(["/usr/bin/taskset", "-c", ",".join(map(str, receipt["cpus"])),
                                                  sys.executable, "-I", "-c", program], text=True)
                connection.send(json.loads(result))
    except BaseException as exc:
        connection.send({"error": repr(exc)})
        raise
    finally:
        os.close(life)
        connection.close()


def cli_actor(harness, arguments, connection):
    if os.getsid(0) not in (os.getpid(), os.getppid()):
        os.setsid()
    engine = DeskPolicy(Runtime(harness))
    stream = io.StringIO()
    with mock.patch.object(cli, "DeskPolicy", return_value=engine), contextlib.redirect_stdout(stream):
        code = cli.main(arguments)
    connection.send({"rc": code, "output": stream.getvalue()})
    connection.close()


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=".deskpolicy-", dir=str(Path.cwd()))
        self.h = Harness(self.tmp.name)
        self.h.cpus = tuple(range(12))
        self.engine = DeskPolicy(Runtime(self.h))
        self.h.config.parent.mkdir(mode=0o700)
        self.configuration = {**CONFIG, "night_enabled": True}
        atomic_json(self.h.config, self.configuration)
        self.engine.prepare()
        self.evidence = {"schema": "pao/desk-preflight@1", "boot_id": self.h.boot,
                         "checked_at": utc_text(self.h.now), "consumers": {"jale": "a" * 40, "hasito": "b" * 40},
                         "resources_checked": True, "legacy_drained": True}
        self.engine.preflight(self.evidence, arm=True)
        self.children = []
        self.extra_pids = []

    def tearDown(self):
        for child, connection in self.children:
            if child.is_alive():
                child.kill()
            child.join(timeout=5)
            connection.close()
        for pid, start in self.extra_pids:
            with contextlib.suppress(FileNotFoundError, ProcessLookupError):
                if process(pid)["start"] == start:
                    os.kill(pid, signal.SIGTERM)
        self.tmp.cleanup()

    def pending(self, name, project="hasito", kind="script", weight=1):
        return self.engine.register(name, project, kind, "test", weight)

    def publish(self):
        return self.engine.publish()

    def renew(self):
        for record in self.engine.records():
            if record["state"] == "pending":
                self.engine.register(record["request_id"], record["project"], record["kind"],
                                     record["owner"], record["weight"])

    def advance(self, seconds):
        # A waiting consumer pulses every 30 s, including during preparation.
        while seconds:
            step = min(seconds, 30)
            self.h.advance(step)
            self.renew()
            seconds -= step
        self.publish()

    def launch(self, name, lane=1, barrier=None, receive=True):
        parent, child = mp.get_context("fork").Pipe()
        worker = mp.get_context("fork").Process(target=actor, args=(self.h, name, lane, child, barrier))
        worker.start()
        child.close()
        self.children.append((worker, parent))
        if not receive:
            return worker, parent
        self.assertTrue(parent.poll(60), "admission timed out")
        receipt = parent.recv()
        self.assertNotIn("error", receipt)
        return receipt, worker, parent

    def close_actor(self, worker, connection):
        connection.send(("close", self.h.bt, self.h.now))
        self.assertTrue(connection.poll(10))
        self.assertEqual(connection.recv(), "closed")
        worker.join(5)
        self.assertEqual(worker.exitcode, 0)

    def run_cli(self, arguments):
        parent, child = mp.get_context("fork").Pipe()
        worker = mp.get_context("fork").Process(target=cli_actor, args=(self.h, arguments, child))
        worker.start()
        child.close()
        self.children.append((worker, parent))
        self.assertTrue(parent.poll(60), "CLI timed out")
        result = parent.recv()
        worker.join(5)
        self.assertEqual(worker.exitcode, 0)
        return result

    def test_distribution_tables_and_ties(self):
        for pending, expected in (([1, 10], [1, 5]), ([10, 1], [5, 1]),
                                  ([0, 10], [0, 6]), ([0, 0], [0, 0]), ([1, 1], [1, 1])):
            actual = allocate(6, dict(zip(("jale", "hasito"), pending)),
                              {"jale": 0, "hasito": 0}, {"jale": 1, "hasito": 2})
            self.assertEqual(list(actual.values()), expected)
        self.assertEqual(allocate(1, {"jale": 9, "hasito": 2}, {"jale": 0, "hasito": 0},
                                  {"jale": 2, "hasito": 1}), {"jale": 0, "hasito": 1})
        for budget in range(1, 13):
            for left in range(5):
                for right in range(5):
                    result = allocate(budget, {"jale": left, "hasito": right},
                                      {"jale": 3, "hasito": 0}, {"jale": 1, "hasito": 2})
                    self.assertLessEqual(sum(result.values()), budget)
                    self.assertLessEqual(result["jale"], left + 3 if left else 0)
                    self.assertLessEqual(result["hasito"], right)

    def test_race_twenty_with_three_live_budget_four(self):
        for n, project, kind, lane in ((0, "jale", "chain", 1), (1, "hasito", "script", 1),
                                        (2, "hasito", "script", 2)):
            name = "seed" + str(n)
            self.pending(name, project, kind)
            self.publish()
            receipt, _, _ = self.launch(name, lane)
            self.assertTrue(receipt["execute"])
            self.advance(120)
        for n in range(20):
            self.pending("race" + str(n), "jale" if n % 2 else "hasito",
                         "chain" if n % 2 else "script")
        self.publish()
        barrier = mp.get_context("fork").Barrier(21)
        clients = [self.launch("race" + str(n), lane=3 + n // 2 % 2,
                               barrier=barrier, receive=False) for n in range(20)]
        barrier.wait(timeout=30)
        receipts = []
        for _, connection in clients:
            for _ in range(4):
                if connection.poll(60):
                    break
            else:
                self.fail("Contendiente sin recibo tras reintentos acotados")
            receipts.append(connection.recv())
        self.assertTrue(all(r.get("state") in ("admitted", "denied") for r in receipts), receipts)
        self.assertEqual(sum(r.get("execute", False) for r in receipts), 1, receipts)
        state = self.engine.status()
        self.assertEqual(sum(state["live"].values()), 4)
        self.assertEqual(len({c for claim in state["claims"] for c in claim["cpus"]}), 4)

    def test_weight_budget_exhausted(self):
        self.pending("heavy", weight=4)
        self.publish()
        receipt, _, _ = self.launch("heavy")
        self.assertTrue(receipt["execute"])
        self.pending("heavy2", weight=4)
        self.advance(120)
        receipt, _, _ = self.launch("heavy2", 2)
        self.assertTrue(receipt["execute"])
        self.pending("next", "jale", "chain")
        self.advance(120)
        denied, _, _ = self.launch("next")
        self.assertEqual(denied["reason"], "weight-full")

    def test_spacing_records_day_and_closed_claims(self):
        self.pending("day")
        self.publish()
        (self.h.root / "pao-policy-v1.json").unlink()
        receipt, worker, connection = self.launch("day")
        self.assertEqual(receipt["mode"], "day")
        self.close_actor(worker, connection)
        self.pending("night")
        self.publish()
        denied, _, _ = self.launch("night")
        self.assertEqual(denied["reason"], "start-interval")
        self.advance(119)
        denied, _, _ = self.launch("night")
        self.assertEqual(denied["reason"], "start-interval")
        self.advance(1)
        admitted, _, _ = self.launch("night")
        self.assertTrue(admitted["execute"])

    def test_lower_budget_and_quota_keep_high_indices(self):
        for n in range(3):
            name = "seed" + str(n)
            self.pending(name)
            self.publish()
            admitted, _, _ = self.launch(name, n + 1)
            self.assertTrue(admitted["execute"])
            self.advance(120)
        self.configuration["night_budget"] = 2
        atomic_json(self.h.config, self.configuration)
        self.pending("next", "jale", "chain")
        self.assertEqual(self.publish()["mode"], "night")
        denied, _, _ = self.launch("next")
        self.assertEqual(denied["reason"], "budget-full")
        self.assertEqual(sum(self.engine.status()["live"].values()), 3)

    def test_policy_expiring_during_dispatch_requires_new_attempt(self):
        self.pending("race")
        self.publish()
        calls = 0
        real_sample = self.engine.runtime.sample

        def sample():
            nonlocal calls
            calls += 1
            if calls == 2:
                self.h.advance(60)
            return real_sample()

        # Clock and identity seams remain inside this explicit unittest harness.
        with mock.patch.object(self.engine.runtime, "sample", side_effect=sample):
            with mock.patch("deskpolicy.engine.process", return_value={
                    "pid": 10, "pgid": 10, "sid": 10, "start": 1, "zombie": False}), \
                    mock.patch("deskpolicy.engine.os.getsid", return_value=10), \
                    mock.patch("deskpolicy.engine.os.getpid", return_value=10):
                life = open_file(self.engine.life_path("hasito", 1), create=True)
                try:
                    with mutex(self.h.root) as lock:
                        denied = self.engine.admit("race", "run-race", 1, 10, life, lock)
                finally:
                    os.close(life)
        self.assertEqual(denied["reason"], "policy-expired")
        self.assertEqual(self.engine.status()["weight"], 0)
        admitted, _, _ = self.launch("race")
        self.assertEqual(admitted["mode"], "day")
        self.assertTrue(admitted["execute"])

    def test_bad_policy_selects_day_strictly(self):
        self.pending("req")
        valid = self.publish()
        invalids = [b'{"schema":1,"schema":2}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1e400}',
                    b'{"x":"\\u0000"}', b'{', b'\x00', b'\xff', b'x' * 65537]
        for field, value in (("revision", True), ("extra", 1), ("night_budget", 0),
                             ("max_run_weight", 9), ("min_start_interval_s", 0),
                             ("issued_at", "2026-02-30T06:00:00Z"),
                             ("expires_at", "2026-11-05T06:01:00+00:00"),
                             ("schema", "pao/desk-policy@2"), ("boot_id", "bad"),
                             ("load1_lt", 12.0), ("cpu_weight_budget", None)):
            altered = {**valid, field: value}
            invalids.append(json.dumps(altered).encode())
        path = self.h.root / "pao-policy-v1.json"
        for raw in invalids:
            with self.subTest(raw=raw[:150]):
                path.write_bytes(raw)
                self.assertEqual(self.engine.status()["mode"], "day")
        atomic_json(path, valid)
        path.chmod(0o644)
        self.assertEqual(self.engine.status()["mode"], "day")
        path.unlink()
        path.symlink_to(self.h.config)
        self.assertEqual(self.engine.status()["mode"], "day")
        path.unlink()
        self.assertEqual(self.engine.status()["mode"], "day")

    def test_bad_census_returns_75_and_never_partial_view(self):
        self.pending("req")
        self.publish()
        self.engine.record_path("req").write_bytes(b'{')
        stream = io.StringIO()
        with mock.patch.object(cli, "DeskPolicy", return_value=self.engine), contextlib.redirect_stdout(stream):
            code = cli.main(["estado"])
        self.assertEqual(code, 75)
        self.assertEqual(json.loads(stream.getvalue())["reason"], "census-unknown")
        life = open_file(self.engine.life_path("hasito", 1), create=True)
        try:
            with mutex(self.h.root) as lock, mock.patch.object(cli, "DeskPolicy", return_value=self.engine):
                stream = io.StringIO()
                with contextlib.redirect_stdout(stream):
                    code = cli.main(["admitir", "--request-id", "req", "--run-id", "run-req", "--lane", "1",
                                     "--pid", str(os.getpid()), "--life-fd", str(life), "--mutex-fd", str(lock)])
            self.assertEqual(code, 75)
            receipt = json.loads(stream.getvalue())
            self.assertEqual((receipt["state"], receipt["reason"], receipt["execute"]),
                             ("denied", "census-unknown", False))
        finally:
            os.close(life)

    def test_new_boot_requires_explicit_preflight(self):
        self.pending("req")
        self.publish()
        self.h.boot = "11112233-4455-6677-8899-aabbccddeeff"
        self.h.bt = 1
        self.assertEqual(self.engine.status()["mode"], "day")
        self.assertEqual(self.publish()["mode"], "day")
        self.assertFalse(self.engine.preflight()["armed"])
        self.evidence.update(boot_id=self.h.boot, checked_at=utc_text(self.h.now))
        self.assertTrue(self.engine.preflight(self.evidence, arm=True)["armed"])
        self.assertEqual(self.publish()["mode"], "night")

    def test_window_boundaries_and_lease_clipped_at_fourteen(self):
        for hour, minute, second, mode in ((4, 59, 59, "day"), (5, 0, 0, "night"),
                                          (13, 59, 59, "night"), (14, 0, 0, "day")):
            self.h.now = self.h.now.replace(hour=hour, minute=minute, second=second)
            result = self.publish()
            self.assertEqual(result["mode"], mode)
            if hour == 13:
                self.assertEqual(result["expires_at"], "2026-11-05T14:00:00Z")
                self.h.advance(1)
                self.assertEqual(self.engine.status()["mode"], "day")

    def test_clocks_backwards_and_suspend(self):
        self.pending("req")
        self.publish()
        self.h.now -= dt.timedelta(seconds=1)
        self.assertEqual(self.engine.status()["mode"], "day")
        self.h.now += dt.timedelta(seconds=1)
        self.h.bt += 60
        self.assertEqual(self.engine.read_policy(self.h.sample())[0]["mode"], "day")
        self.h.bt -= 60
        self.h.now += dt.timedelta(seconds=20)
        self.assertEqual(self.engine.read_policy(self.h.sample())[0]["mode"], "day")

    def test_sigkill_wrapper_live_child_retains_uncertain_weight(self):
        self.pending("kill")
        self.publish()
        receipt, worker, connection = self.launch("kill")
        self.assertTrue(receipt["execute"])
        connection.send(("child",))
        self.assertTrue(connection.poll(10))
        child = connection.recv()
        self.extra_pids.append((child, process(child)["start"]))
        connection.send(("close", self.h.bt, self.h.now))
        self.assertTrue(connection.poll(10))
        self.assertEqual(connection.recv(), "close-unproven")
        # The real child closed inherited locks. Killing only the wrapper frees
        # flock, while its CPU-affined child continues to exist.
        worker.kill()
        worker.join(5)
        os.kill(child, 0)
        state = self.engine.status()
        self.assertEqual((state["uncertain"], state["weight"]), (1, 1))
        self.h.advance(10000)
        self.publish()
        state = self.engine.status()
        self.assertEqual((state["uncertain"], state["weight"]), (1, 1))
        self.assertEqual(read_json(self.engine.record_path("kill"))["claim"]["state"], "uncertain")

    def test_disjoint_cpu_assignment_and_seven_real_children(self):
        self.pending("first")
        self.pending("second")
        self.publish()
        first, worker, connection = self.launch("first", 1)
        self.advance(120)
        second, _, _ = self.launch("second", 2)
        self.assertTrue(first["execute"] and second["execute"])
        self.assertFalse(set(first["cpus"]) & set(second["cpus"]))
        connection.send(("affinity",))
        self.assertTrue(connection.poll(20))
        children = connection.recv()
        self.assertEqual(len(children), 7)
        self.assertTrue(all(cpus == first["cpus"] for cpus in children), children)
        self.close_actor(worker, connection)

    def test_lost_ack_replay_never_executes_twice(self):
        self.pending("ack")
        self.publish()
        receipt, worker, connection = self.launch("ack")
        duplicate, _, _ = self.launch("ack")
        self.assertEqual(duplicate["state"], "admitted")
        self.assertTrue(duplicate["replay"])
        self.assertFalse(duplicate["execute"])
        self.assertEqual(duplicate["cpus"], receipt["cpus"])
        self.assertEqual(sum(self.engine.status()["live"].values()), 1)
        self.close_actor(worker, connection)
        terminal, _, _ = self.launch("ack")
        self.assertFalse(terminal["execute"])
        self.assertEqual(terminal["claim_state"], "closed")

    def test_expired_and_withdrawn_requests_never_resurrect(self):
        self.pending("expired")
        self.h.advance(120)
        self.assertEqual(self.pending("expired")["state"], "expired")
        self.assertEqual(self.pending("expired")["state"], "expired")
        self.pending("withdrawn")
        self.engine.register("withdrawn", "hasito", "script", "test", 1, withdraw=True)
        self.assertEqual(self.pending("withdrawn")["state"], "withdrawn")
        with self.assertRaisesRegex(Invalid, "request-conflict"):
            self.pending("withdrawn", weight=2)

    def test_day_types_load_exclusivity_and_no_hasito_load_gate(self):
        self.h.now = self.h.now.replace(hour=15)
        self.h.load = 9
        self.pending("chain", "jale", "chain")
        self.publish()
        denied, _, _ = self.launch("chain")
        self.assertEqual(denied["reason"], "load-high")
        self.h.load = 8.99
        admitted, worker, connection = self.launch("chain")
        self.assertTrue(admitted["execute"])
        self.pending("chain2", "jale", "chain")
        denied, _, _ = self.launch("chain2", 2)
        self.assertEqual(denied["reason"], "resource-busy")
        self.h.load = 100
        self.pending("hasito")
        admitted, _, _ = self.launch("hasito")
        self.assertTrue(admitted["execute"])
        self.close_actor(worker, connection)

    def test_legacy_window_remains_independent_veto(self):
        self.pending("jale", "jale", "chain")
        self.publish()
        window = self.h.root / "pao-window.json"
        atomic_json(window, {"until": utc_text(self.h.now + dt.timedelta(seconds=60)), "jale_slots": 0})
        denied, _, _ = self.launch("jale")
        self.assertEqual(denied["reason"], "legacy-freeze")
        atomic_json(window, {"until": utc_text(self.h.now - dt.timedelta(seconds=1)), "jale_slots": 0})
        admitted, _, _ = self.launch("jale")
        self.assertTrue(admitted["execute"])

    def test_estado_is_read_only_even_when_lock_lost(self):
        self.pending("req")
        self.publish()
        _, worker, _ = self.launch("req")
        worker.kill()
        worker.join(5)
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.h.root.rglob("*") if p.is_file()}
        self.assertEqual(self.engine.status()["uncertain"], 1)
        after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.h.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_untracked_or_legacy_resource_denies(self):
        self.pending("req")
        self.publish()
        (self.h.root / "slot-9").mkdir(mode=0o700)
        with self.assertRaisesRegex(Invalid, "legacy-slot-unaccounted"):
            self.engine.status()

    def test_environment_does_not_supply_policy_or_paths(self):
        with mock.patch.dict(os.environ, {"HOME": "/bad-home", "DESK_LOAD_LIMIT": "999",
                                          "PAO_POLICY": "/bad-policy", "LOCK_ROOT": "/bad-root",
                                          "NIGHT_BUDGET": "999", "PYTHONPATH": "/bad-imports"}):
            with self.assertRaisesRegex(Invalid, "fixed-installation-required"):
                Runtime()
            self.pending("req")
            self.assertEqual(self.publish()["night_budget"], 4)

    def test_mutex_ownership_and_preflight_drain(self):
        self.pending("req")
        self.publish()
        life = open_file(self.engine.life_path("hasito", 1), create=True)
        unlocked = open_file(self.h.root / "admission.lock")
        try:
            with self.assertRaisesRegex(Invalid, "mutex-not-held"):
                self.engine.admit("req", "run-req", 1, os.getpid(), life, unlocked)
        finally:
            os.close(life)
            os.close(unlocked)
        admitted, _, _ = self.launch("req")
        self.assertTrue(admitted["execute"])
        with self.assertRaisesRegex(Invalid, "preflight-requires-drain"):
            self.engine.preflight(self.evidence, arm=True)

    def test_real_cli_wrapper_receipt_exit_75_and_replay(self):
        self.pending("cli")
        self.publish()
        run = "run-cli"
        directory = self.h.directory / "runs" / run
        directory.parent.mkdir(mode=0o700)
        directory.mkdir(mode=0o700)
        executed = self.h.directory / "executed"
        command = "from pathlib import Path; p=Path({!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x'); raise SystemExit(75)".format(str(executed))
        arguments = ["ejecutar", "--request-id", "cli", "--run-id", run, "--lane", "1", "--recibo",
                     "--", sys.executable, "-I", "-c", command]
        first = self.run_cli(arguments)
        self.assertEqual(first["rc"], 75)
        receipt = read_json(directory / "pao-admission.json")
        self.assertEqual((receipt["state"], receipt["exit_code"]), ("admitted", None))
        self.assertEqual(read_json(self.engine.record_path("cli"))["claim"]["state"], "closed")
        second = self.run_cli(arguments)
        self.assertEqual(second["rc"], 0)
        self.assertFalse(json.loads(second["output"])["execute"])
        self.assertEqual(read_json(directory / "pao-admission.json"), receipt)
        self.assertEqual(executed.read_text(), "x")

    def test_real_cli_denial_never_executes_command(self):
        self.pending("denied")
        self.publish()
        self.h.load = 12.0
        marker = self.h.directory / "must-not-exist"
        result = self.run_cli(["ejecutar", "--request-id", "denied", "--run-id", "run-denied", "--lane", "1",
                               "--", "/usr/bin/touch", str(marker)])
        receipt = json.loads(result["output"])
        self.assertEqual((result["rc"], receipt["state"], receipt["reason"]), (75, "denied", "load-high"))
        self.assertFalse(marker.exists())
        self.assertEqual(self.engine.status()["weight"], 0)

    def test_publication_failure_keeps_complete_old_policy_and_skips_revision(self):
        self.pending("req")
        old = self.publish()
        replace = os.replace

        def fail_policy(source, target):
            if Path(target).name == "pao-policy-v1.json":
                raise OSError("injected-publication-failure")
            replace(source, target)

        with mock.patch("deskpolicy.store.os.replace", side_effect=fail_policy):
            with self.assertRaisesRegex(OSError, "injected-publication-failure"):
                self.publish()
        self.assertEqual(read_json(self.h.root / "pao-policy-v1.json"), old)
        self.assertEqual(self.publish()["revision"], old["revision"] + 2)

    def test_failure_after_claim_commit_never_executes_and_replay_retains_capacity(self):
        self.pending("partial")
        self.publish()
        marker = self.h.directory / "must-not-execute"
        replace = os.replace

        def fail_after_claim(source, target):
            replace(source, target)
            if Path(target).name == "partial.json":
                raise OSError("injected-after-claim-replace")

        with mock.patch("deskpolicy.store.os.replace", side_effect=fail_after_claim):
            result = self.run_cli(["ejecutar", "--request-id", "partial", "--run-id", "run-partial",
                                   "--lane", "1", "--", "/usr/bin/touch", str(marker)])
        self.assertEqual(result["rc"], 75)
        self.assertFalse(marker.exists())
        record = read_json(self.engine.record_path("partial"))
        self.assertEqual(record["state"], "admitted")
        state = self.engine.status()
        self.assertEqual((state["uncertain"], state["weight"]), (1, 1))
        replay, _, _ = self.launch("partial")
        self.assertTrue(replay["replay"])
        self.assertFalse(replay["execute"])

    def test_python39_syntax_and_shell_syntax(self):
        package = Path(__file__).resolve().parents[1]
        for path in package.rglob("*.py"):
            ast.parse(path.read_text(), filename=str(path), feature_version=(3, 9))
        for name in ("deploy-desk.sh", "withdraw-desk.sh", "tests/desk-run.sh"):
            subprocess.run(["/bin/bash", "-n", str(package / name)], check=True)

    def test_cli_constructor_failure_is_an_admission_denial(self):
        stream = io.StringIO()
        with mock.patch.object(cli, "DeskPolicy", side_effect=Invalid("fixed-installation-required")), \
                contextlib.redirect_stdout(stream):
            code = cli.main(["admitir", "--request-id", "req", "--run-id", "run-req", "--lane", "1",
                             "--pid", str(os.getpid())])
        receipt = json.loads(stream.getvalue())
        self.assertEqual((code, receipt["state"], receipt["execute"]), (75, "denied", False))

    def test_preflight_hash_and_expansion_disable_night_reduction_does_not(self):
        self.pending("req")
        self.assertEqual(self.publish()["mode"], "night")
        arm = read_json(self.h.root / "pao-arm.json")
        arm["module_sha256"] = "0" * 64
        atomic_json(self.h.root / "pao-arm.json", arm)
        self.assertEqual(self.publish()["mode"], "day")
        self.engine.preflight(self.evidence, arm=True)
        self.configuration["night_budget"] = 3
        atomic_json(self.h.config, self.configuration)
        self.assertEqual(self.publish()["mode"], "night")
        self.configuration["night_budget"] = 5
        atomic_json(self.h.config, self.configuration)
        self.assertEqual(self.publish()["mode"], "day")


if __name__ == "__main__":
    unittest.main()
