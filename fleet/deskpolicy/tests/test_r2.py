import contextlib
import io
import json
import multiprocessing as mp
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

from deskpolicy import cli
from deskpolicy.engine import DeskPolicy
from deskpolicy.model import Invalid, utc_text
from deskpolicy.store import Runtime, atomic_json, mutex, open_file, process, read_json
from . import test_policy as fixtures


def keeper_actor(harness, connection):
    os.setsid()
    engine = DeskPolicy(Runtime(harness))
    life = open_file(engine.life_path("jale", 1), create=True)
    with mutex(engine.root) as lock:
        receipt = engine.admit("keeper", "run-keeper", 1, os.getpid(), life, lock)
    keeper = os.fork()
    if keeper:
        os.close(life)
        os.waitpid(keeper, 0)
        return
    try:
        os.setsid()
        token = receipt.get("claim_token", "0" * 64)
        with mutex(engine.root) as lock:
            try:
                engine.close("keeper", "run-keeper", os.getpid(), life, lock,
                             claim_token="f" * 64)
            except (Invalid, TypeError) as exc:
                connection.send(("wrong-token", str(exc)))
            else:
                connection.send(("wrong-token", "accepted"))
            try:
                result = engine.close("keeper", "run-keeper", os.getpid(), life, lock,
                                      claim_token=token)
                connection.send(("close", result["claim"]["state"]))
            except (Invalid, TypeError) as exc:
                connection.send(("close", str(exc)))
    finally:
        os.close(life)
        os._exit(0)


class RegressionTest(unittest.TestCase):
    setUp = fixtures.PolicyTest.setUp
    tearDown = fixtures.PolicyTest.tearDown
    pending = fixtures.PolicyTest.pending
    publish = fixtures.PolicyTest.publish
    advance = fixtures.PolicyTest.advance
    renew = fixtures.PolicyTest.renew
    launch = fixtures.PolicyTest.launch
    run_cli = fixtures.PolicyTest.run_cli

    def test_c1_uncertain_resource_stays_reserved_for_both_projects(self):
        for project, kind in (("jale", "chain"), ("hasito", "script")):
            with self.subTest(project=project):
                old, new = project + "-old", project + "-new"
                self.pending(old, project, kind)
                self.pending(new, project, kind)
                self.publish()
                receipt, worker, _ = self.launch(old)
                self.assertTrue(receipt["execute"])
                worker.kill()
                worker.join(5)
                self.advance(120)
                denied, _, _ = self.launch(new)
                self.assertEqual((denied["state"], denied["reason"], denied["execute"]),
                                 ("denied", "resource-busy", False))
                self.assertIsNone(read_json(self.engine.record_path(new))["claim"])
                view = self.engine.status()
                self.assertEqual(view["live"][project], 1)
                self.assertNotEqual(view.get("detail"), "duplicate-resource")
                self.advance(120)

    def test_c1_death_after_census_before_flock_keeps_active_reservation(self):
        for project, kind in (("jale", "chain"), ("hasito", "script")):
            with self.subTest(project=project):
                old, new = project + "-old", project + "-new"
                self.pending(old, project, kind)
                self.pending(new, project, kind)
                self.publish()
                receipt, worker, _ = self.launch(old)
                self.assertTrue(receipt["execute"])
                self.advance(120)
                census = self.engine.census

                def kill_after_census(*args, **kwargs):
                    live = census(*args, **kwargs)
                    self.assertEqual(next(r for r in live if r["request_id"] == old)["claim"]["state"], "active")
                    worker.kill()
                    worker.join(5)
                    return live

                life = open_file(self.engine.life_path(project, 1))
                try:
                    with mutex(self.h.root) as lock, mock.patch.object(self.engine, "census", side_effect=kill_after_census):
                        denied = self.engine.admit(new, "run-" + new, 1, os.getpid(), life, lock)
                    self.assertEqual(denied["reason"], "resource-busy")
                    self.assertIsNone(read_json(self.engine.record_path(new))["claim"])
                finally:
                    os.close(life)
                self.assertEqual(self.engine.status()["live"][project], 1)
                self.advance(120)

    def test_c1_estado_reports_duplicate_without_changing_records(self):
        self.pending("old")
        self.pending("next")
        self.publish()
        _, worker, _ = self.launch("old")
        worker.kill()
        worker.join(5)
        source = read_json(self.engine.record_path("old"))
        duplicate = read_json(self.engine.record_path("next"))
        duplicate.update(state="admitted", admitted_run_id="run-next", claim={**source["claim"], "run_id": "run-next"})
        atomic_json(self.engine.record_path("next"), duplicate)
        before = {p: p.read_bytes() for p in (self.h.root / "pao-wait").iterdir()}
        result = self.run_cli(["estado"])
        self.assertEqual(result["rc"], 75)
        self.assertEqual(json.loads(result["output"])["detail"], "duplicate-resource")
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_w1_empty_boot_creates_private_state_and_admits_day(self):
        import shutil
        shutil.rmtree(self.h.root)
        self.h.now = self.h.now.replace(hour=16)
        engine = DeskPolicy(Runtime(self.h))
        engine.register("boot", "hasito", "script", "test", 1)
        for name in ("", "pao-wait", "pao-hasito"):
            self.assertEqual((self.h.root / name).stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.h.root / "admission.lock").stat().st_mode & 0o777, 0o600)
        receipt, _, _ = self.launch("boot")
        self.assertEqual((receipt["mode"], receipt["execute"]), ("day", True))
        engine.prepare()
        self.assertFalse((self.h.root / "pao-policy-v1.json").exists())

    def receipt_directory(self, name, mode=0o700):
        path = self.h.directory / "runs" / ("run-" + name)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.mkdir(mode=mode)
        path.chmod(mode)
        return path

    def execute_args(self, name, command):
        return ["ejecutar", "--request-id", name, "--run-id", "run-" + name,
                "--lane", "1", "--recibo", "--", *command]

    def test_w2_remote_run_0775_receipt_is_accepted(self):
        self.pending("receipt")
        self.publish()
        path = self.receipt_directory("receipt", 0o775)
        result = self.run_cli(self.execute_args("receipt", ["/bin/true"]))
        self.assertEqual(result["rc"], 0, result)
        self.assertIsNone(json.loads(result["output"])["claim_token"])
        self.assertEqual(json.loads((path / "pao-admission.json").read_text())["state"], "admitted")

    def test_w2_invalid_receipt_preconditions_never_commit_claim(self):
        for name, mode in (("unsafe", 0o777), ("conflict", 0o700)):
            self.pending(name)
            path = self.receipt_directory(name, mode)
            if name == "conflict":
                atomic_json(path / "pao-admission.json", {"request_id": "other", "run_id": "other", "state": "admitted"})
            self.publish()
            result = self.run_cli(self.execute_args(name, ["/bin/true"]))
            self.assertEqual(result["rc"], 75)
            self.assertIsNone(read_json(self.engine.record_path(name))["claim"])
            self.assertEqual(sum(self.engine.status()["live"].values()), 0)
            self.engine.register(name, "hasito", "script", "test", 1, withdraw=True)

    def test_w3_script_exit_is_preserved_with_live_descendant(self):
        self.pending("orphan")
        self.publish()
        directory = self.receipt_directory("orphan")
        pid_file = self.h.directory / "orphan.pid"
        result = self.run_cli(self.execute_args("orphan", ["/bin/bash", "-c",
                                  'sleep 45 & echo $! > "$1"; exit 3', "test", str(pid_file)]))
        pid = int(pid_file.read_text())
        self.extra_pids.append((pid, process(pid)["start"]))
        self.assertEqual(result["rc"], 3, result)
        receipt = read_json(directory / "pao-admission.json")
        self.assertEqual((receipt["state"], receipt["script_exit_code"], receipt["claim_state"]),
                         ("admitted", 3, "uncertain"))
        self.assertEqual(self.engine.status()["weight"], 1)

    def group_stop(self, stop_signal):
        self.pending("stop")
        self.publish()
        directory = self.receipt_directory("stop")
        pid_file = self.h.directory / "script.pid"
        parent, child = mp.get_context("fork").Pipe()

        def runner():
            os.setsid()
            wrapper = os.fork()
            if wrapper == 0:
                fixtures.cli_actor(self.h, self.execute_args("stop", ["/bin/bash", "-c",
                                   'echo $$ > "$1"; exec sleep 45', "test", str(pid_file)]), child)
                os._exit(0)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            os.waitpid(wrapper, 0)

        worker = mp.get_context("fork").Process(target=runner)
        worker.start()
        child.close()
        self.children.append((worker, parent))
        deadline = time.monotonic() + 30
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(pid_file.exists())
        script = int(pid_file.read_text())
        self.extra_pids.append((script, process(script)["start"]))
        os.killpg(worker.pid, stop_signal)
        self.assertTrue(parent.poll(10), "wrapper survived remote-run stop")
        result = parent.recv()
        worker.join(5)
        expected = 128 + stop_signal
        self.assertEqual(result["rc"], expected, result)
        claim = read_json(self.engine.record_path("stop"))["claim"]
        self.assertEqual((claim["state"], claim["close_cause"]), ("closed", "stopped"))
        self.assertEqual(read_json(directory / "pao-admission.json")["script_exit_code"], expected)

    def test_w4_remote_runner_group_term_reaches_script_and_closes(self):
        self.group_stop(signal.SIGTERM)

    def test_w4_remote_runner_group_kill_reaches_script_and_closes(self):
        self.group_stop(signal.SIGKILL)

    def test_w5_keeper_closes_in_own_session_with_inherited_token(self):
        self.pending("keeper", "jale", "verify", 2)
        self.publish()
        parent, child = mp.get_context("fork").Pipe()
        worker = mp.get_context("fork").Process(target=keeper_actor, args=(self.h, child))
        worker.start()
        child.close()
        self.children.append((worker, parent))
        self.assertTrue(parent.poll(30))
        self.assertEqual(parent.recv(), ("wrong-token", "claim-token"))
        self.assertTrue(parent.poll(30))
        self.assertEqual(parent.recv(), ("close", "closed"))
        worker.join(5)
        self.assertEqual(self.engine.status()["live"]["jale"], 0)

    def test_w6_expired_window_modes_and_documented_writer_temporary(self):
        self.pending("window", "jale", "chain")
        self.publish()
        path = self.h.root / "pao-window.json"
        for mode in (0o600, 0o644, 0o664, 0o777):
            atomic_json(path, {"until": utc_text(self.h.now), "jale_slots": 0})
            path.chmod(mode)
            self.assertFalse(self.engine.legacy_veto("chain", self.h.sample(), 0), oct(mode))
        temporary = self.h.root / ".pao-window-publishing"
        temporary.write_bytes(b'{')
        admitted, _, _ = self.launch("window")
        self.assertTrue(admitted["execute"])

    def test_w7_oversize_rejected_before_fifo_and_reduction_expires_head(self):
        self.publish()
        result = self.run_cli(["pendiente", "--request-id", "oversize", "--project", "hasito",
                               "--kind", "script", "--owner", "test", "--weight", "10"])
        self.assertEqual(result["rc"], 75)
        self.assertEqual(json.loads(result["output"])["reason"], "weight-too-large")
        self.assertFalse(self.engine.record_path("oversize").exists())
        self.pending("head", weight=4)
        self.pending("next")
        self.configuration["max_run_weight"] = 2
        atomic_json(self.h.config, self.configuration)
        self.publish()
        self.assertEqual(self.engine.status()["head"]["hasito"], "next")
        admitted, _, _ = self.launch("next")
        self.assertTrue(admitted["execute"])

    def test_w8_day_projects_are_independent_of_weight(self):
        self.h.now = self.h.now.replace(hour=16)
        for name, project, kind, weight, lane in (("chain", "jale", "chain", 4, 1),
                ("verify", "jale", "verify", 2, 2), ("hasito1", "hasito", "script", 4, 1),
                ("hasito2", "hasito", "script", 4, 2)):
            self.pending(name, project, kind, weight)
            self.publish()
            receipt, _, _ = self.launch(name, lane)
            self.assertTrue(receipt["execute"], receipt)
            self.assertEqual(receipt["cpus"], list(range(12)))
        view = self.engine.status()
        self.assertEqual((view["live"], view["weight"]), ({"jale": 2, "hasito": 2}, 14))

    def test_w9_explicit_cli_harness_reaches_real_entrypoint(self):
        clock = self.h.directory / "clock.json"
        atomic_json(clock, {"utc": utc_text(self.h.now), "bt": self.h.bt, "boot": self.h.boot})
        load = self.h.directory / "loadavg"
        load.write_text("0.0 0.0 0.0 1/1 1\n")
        environment = {**os.environ, "RUN_SUITE_HARNESS": "1", "LOCK_ROOT": str(self.h.root),
                       "PAO_DESK_HARNESS": str(self.h.directory), "LOADAVG_FILE": str(load),
                       "PAO_DESK_CLOCK_FILE": str(clock)}
        entrypoint = Path(cli.__file__).resolve()
        arguments = [sys.executable, "-I", str(entrypoint), "estado"]
        result = subprocess.run(arguments, env=environment, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["boot_id"], self.h.boot)
        listeners = self.h.directory / "listeners"
        listeners.write_text("#!/bin/sh\nexit 0\n")
        listeners.chmod(0o700)
        environment["PORT_LISTENERS"] = str(listeners)
        command = [sys.executable, "-I", str(entrypoint)]
        pending = ["pendiente", "--request-id", "harness", "--project", "jale", "--kind", "chain",
                   "--owner", "test", "--weight", "4"]
        result = subprocess.run(command + pending, env=environment, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        load.write_text("9.0 0.0 0.0 1/1 1\n")
        result = subprocess.run(command + ["ejecutar", "--request-id", "harness", "--run-id", "run-harness",
                                "--lane", "1", "--", "/bin/true"], env=environment,
                                text=True, capture_output=True, start_new_session=True)
        self.assertEqual((result.returncode, json.loads(result.stdout)["reason"]), (75, "load-high"))
        self.assertIsNone(read_json(self.engine.record_path("harness"))["claim"])
        load.write_text("0.0 0.0 0.0 1/1 1\n")
        program = '''import fcntl,json,os,pathlib,subprocess,sys
root=pathlib.Path(os.environ["LOCK_ROOT"])
lock=os.open(root/"admission.lock",os.O_RDWR)
life=os.open(root/"slot-0.lock",os.O_RDWR|os.O_CREAT,0o600)
fcntl.flock(lock,fcntl.LOCK_EX)
base=[sys.executable,"-I",sys.argv[1]]
identity=["--request-id","harness","--run-id","run-harness","--pid",str(os.getpid()),
          "--mutex-fd",str(lock),"--life-fd",str(life)]
admit=subprocess.run(base+["admitir",*identity,"--lane","1"],pass_fds=(lock,life),capture_output=True,text=True)
assert admit.returncode==0,admit.stdout+admit.stderr
receipt=json.loads(admit.stdout)
assert receipt["execute"],receipt
reader,writer=os.pipe()
os.write(writer,receipt["claim_token"].encode())
os.close(writer)
close=subprocess.run(base+["cerrar",*identity,"--claim-token-fd",str(reader)],
                     pass_fds=(lock,life,reader),capture_output=True,text=True)
assert close.returncode==0,close.stdout+close.stderr
assert json.loads(close.stdout)["claim"]["state"]=="closed"
'''
        result = subprocess.run([sys.executable, "-I", "-c", program, str(entrypoint)], env=environment,
                                text=True, capture_output=True, start_new_session=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        environment.pop("RUN_SUITE_HARNESS")
        result = subprocess.run(arguments, env=environment, text=True, capture_output=True)
        self.assertEqual(result.returncode, 75)
        self.assertIn("fixed-installation-required", result.stdout)

    def test_w10_cutover_prevalidates_historical_locks_and_is_idempotent(self):
        from deskpolicy.installation import install
        self.configuration["night_enabled"] = False
        atomic_json(self.h.config, self.configuration)
        destination = self.h.directory / "installation"
        for name in ("admission.lock", "slot-0.lock", "slot-1.lock"):
            path = self.h.root / name
            path.touch(mode=0o600, exist_ok=True)
            path.chmod(0o644)
        before = {p.name: p.stat().st_ino for p in self.h.root.glob("*.lock")}
        source = Path(cli.__file__).resolve().parent
        with self.assertRaisesRegex(Invalid, "file-security"):
            install(self.engine, source, destination, "r2")
        self.assertFalse(destination.exists())
        for path in self.h.root.glob("*.lock"):
            path.chmod(0o600)
        first = install(self.engine, source, destination, "r2")
        second = install(self.engine, source, destination, "r2")
        self.assertEqual(first, second)
        self.assertEqual(before, {p.name: p.stat().st_ino for p in self.h.root.glob("*.lock")})
        self.assertFalse(read_json(self.h.config)["night_enabled"])

    def test_w10_failed_publication_rolls_back_and_retry_preserves_state(self):
        from deskpolicy import installation
        self.h.config.unlink()
        destination = self.h.directory / "installation"
        source = Path(cli.__file__).resolve().parent
        real_sync = installation.sync_directory

        def fail_after_config(path):
            if path == self.h.config.parent and self.h.config.exists():
                raise OSError("injected-after-config-commit")
            real_sync(path)

        with mock.patch.object(installation, "sync_directory", side_effect=fail_after_config):
            with self.assertRaisesRegex(OSError, "injected-after-config-commit"):
                installation.install(self.engine, source, destination, "r2")
        self.assertFalse(destination.exists())
        self.assertFalse(self.h.config.exists())
        self.assertEqual(installation.install(self.engine, source, destination, "r2")["mode"], "day")
        self.assertFalse(read_json(self.h.config)["night_enabled"])
        manifest = (destination / "SHA256SUMS").read_bytes()
        with self.assertRaisesRegex(Invalid, "installation-conflict"):
            installation.install(self.engine, source, destination, "different-reference")
        self.assertEqual((destination / "SHA256SUMS").read_bytes(), manifest)
