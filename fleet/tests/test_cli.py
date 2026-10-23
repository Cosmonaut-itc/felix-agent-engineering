import concurrent.futures
import getpass
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

CLI = Path(__file__).resolve().parents[1] / "bin/fleet"
BARRIER = """
import pathlib, sys, time
ready, gate, done = map(pathlib.Path, sys.argv[1:])
ready.write_text('ready')
deadline = time.monotonic() + 6
while not gate.exists() and time.monotonic() < deadline:
    time.sleep(0.02)
print('ok', flush=True)
done.write_text('done')
sys.exit(0 if gate.exists() else 90)
"""


@unittest.skipUnless(os.name == "posix", "local pilot requires POSIX")
class FleetCLI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fleet-cli-")
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.project = self.root / "project-without-profile"
        self.project.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = os.environ.copy()
        self.env.pop("FLEET_STATE_DIR", None)
        self.env.pop("FLEET_CLIENT", None)
        self.env.update(HOME=str(self.home), XDG_STATE_HOME=str(self.root / "xdg"),
                        TMPDIR=str(self.root), PYTHONDONTWRITEBYTECODE="1")
        self.gates = []
        self.jobs = []
        self.processes = []

    def tearDown(self):
        for gate, done in self.gates:
            gate.touch()
        deadline = time.monotonic() + 9
        for job, state in self.jobs:
            while time.monotonic() < deadline:
                result = self.call("status", job, state=state, check=False)
                if result.returncode != 0:
                    break
                value = json.loads(result.stdout)
                if value["state"] == "finished":
                    break
                if value["view"] == "uncertain":
                    break
                time.sleep(0.05)
        for process in self.processes:
            if process.poll() is None:
                process.communicate(timeout=10)
        for gate, done in self.gates:
            ready = gate.with_name("ready")
            while ready.exists() and not done.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
        time.sleep(0.05)
        self.temp.cleanup()

    def call(self, *args, state=None, check=True, env=None):
        command = [sys.executable, "-B", str(CLI), "--state-dir", str(state or self.state)]
        result = subprocess.run(command + list(args), env=env or self.env,
                                cwd=str(self.project), capture_output=True, text=True, timeout=15)
        if check:
            self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        return result

    def value(self, *args, **kwargs):
        return json.loads(self.call(*args, **kwargs).stdout)

    def submit(self, argv, state=None, client=None, env=None):
        options = ["submit"] + (["--client", client] if client is not None else [])
        value = self.value(*options, "--", *argv, state=state, env=env)
        self.assertTrue(value["accepted"])
        self.jobs.append((value["job"], state or self.state))
        return value

    def wait(self, job, code=0, state=None):
        result = self.call("wait", job, "--timeout", "9", state=state, check=False)
        self.assertEqual(result.returncode, code, (result.stdout, result.stderr))
        return json.loads(result.stdout)

    def until(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.02)
        self.fail("owned fixture did not reach its barrier")

    def barrier(self):
        directory = self.root / ("fixture-" + str(len(self.gates)))
        directory.mkdir()
        ready, gate, done = [directory / name for name in ("ready", "gate", "done")]
        self.gates.append((gate, done))
        return [sys.executable, "-B", "-c", BARRIER, str(ready), str(gate), str(done)], ready, gate, done

    def running(self, job, ready, state=None):
        self.until(lambda: ready.exists() and self.value("status", job, state=state)["state"] == "running")

    def test_acceptance_precedes_controlled_completion_and_records_identity(self):
        argv, ready, gate, done = self.barrier()
        submitted = self.submit(argv, client="declared-label")
        self.assertTrue(submitted["job"].startswith("j_"))
        self.assertTrue(submitted["attempt"].startswith("a_"))
        self.running(submitted["job"], ready)
        current = self.value("status", submitted["job"])
        self.assertEqual(current["state"], "running")
        self.assertFalse(done.exists(), "acceptance must precede releasing the command")
        self.assertFalse(current["interruptible"])
        self.assertEqual(current["client"], "declared-label")
        self.assertEqual(current["submitted_by"]["uid"], os.getuid())
        self.assertEqual(current["submitted_by"]["host"], socket.gethostname())
        gate.touch()
        final = self.wait(submitted["job"])
        self.assertEqual(final["outcome"], "succeeded")
        self.assertEqual(final["exit_code"], 0)
        self.assertEqual(Path(final["stdout_log"]).read_text(), "ok\n")
        self.assertEqual(Path(final["stderr_log"]).read_text(), "")
        transitions = final["transitions"]
        self.assertEqual([t["to"] for t in transitions], ["queued", "starting", "running", "finished"])
        self.assertEqual(transitions[0]["actor"], "client:declared-label")
        self.assertTrue(transitions[-1]["actor"].startswith("supervisor:"))
        self.assertTrue(all(t["attempt"] == submitted["attempt"] and
                            t["job"] == submitted["job"] for t in transitions))

    def test_client_precedence_and_default(self):
        env = dict(self.env, FLEET_CLIENT="environment-label")
        first = self.submit([sys.executable, "-c", "pass"], env=env)
        self.assertEqual(first["client"], "environment-label")
        self.wait(first["job"])
        second = self.submit([sys.executable, "-c", "pass"], client="explicit", env=env)
        self.assertEqual(second["client"], "explicit")
        self.wait(second["job"])
        third = self.submit([sys.executable, "-c", "pass"])
        self.assertEqual(third["client"], "{}@{}".format(getpass.getuser(), socket.gethostname()))
        self.wait(third["job"])

    def test_literal_argv_cwd_stdin_and_logs(self):
        literal = "$(touch SHOULD_NOT_EXIST); * ' spaces"
        script = "import os,sys; print(os.getcwd()); print(repr(sys.stdin.read())); print(sys.argv[1]); print('err',file=sys.stderr)"
        job = self.submit([sys.executable, "-c", script, literal])["job"]
        final = self.wait(job)
        self.assertEqual(Path(final["stdout_log"]).read_text().splitlines(),
                         [str(self.project), "''", literal])
        self.assertEqual(Path(final["stderr_log"]).read_text(), "err\n")
        self.assertFalse((self.project / "SHOULD_NOT_EXIST").exists())

    def test_command_failures_and_signal_are_distinct(self):
        failed = self.submit([sys.executable, "-c", "raise SystemExit(7)"])
        result = self.wait(failed["job"], 1)
        self.assertEqual((result["outcome"], result["exit_code"]), ("failed", 7))
        missing = self.submit([str(self.root / "no-such-command")])
        result = self.wait(missing["job"], 1)
        self.assertEqual(result["outcome"], "failed")
        self.assertIn("command spawn failed", result["detail"])
        killed = self.submit([sys.executable, "-c", "import os,signal; os.kill(os.getpid(),signal.SIGKILL)"])
        result = self.wait(killed["job"], 3)
        self.assertEqual((result["outcome"], result["signal"]), ("incomplete", signal.SIGKILL))
        self.assertIsNone(result["exit_code"])

    def test_fifo_and_concurrent_admission(self):
        argv, ready, gate, done = self.barrier()
        first = self.submit(argv)
        self.running(first["job"], ready)
        def enqueue(index):
            return self.value("submit", "--client", str(index), "--", sys.executable, "-c", "print('next')")
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            following = list(pool.map(enqueue, range(4)))
        self.jobs.extend((item["job"], self.state) for item in following)
        views = [self.value("status", item["job"]) for item in following]
        self.assertTrue(all(v["state"] == "queued" for v in views))
        gate.touch()
        finals = [self.wait(first["job"])] + [self.wait(item["job"]) for item in following]
        ordered = sorted(finals, key=lambda v: v["seq"])
        for left, right in zip(ordered, ordered[1:]):
            self.assertLessEqual(left["ended_at"], right["started_at"])
            self.assertLess(left["transitions"][-1]["seq"], right["transitions"][-2]["seq"])

    def test_wait_timeout_and_interrupt_do_not_change_execution(self):
        argv, ready, gate, done = self.barrier()
        job = self.submit(argv)["job"]
        self.running(job, ready)
        before = self.value("status", job)["transitions"]
        result = self.call("wait", job, "--timeout", "0", check=False)
        self.assertEqual(result.returncode, 4)
        self.assertTrue(json.loads(result.stdout)["timed_out"])
        process = subprocess.Popen([sys.executable, "-B", str(CLI), "--state-dir", str(self.state),
                                    "wait", job], env=self.env, cwd=self.project,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(process)
        time.sleep(0.3)
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=3)
        self.assertEqual(process.returncode, 130, (stdout, stderr))
        self.assertIn("interrupted", json.loads(stdout)["error"])
        self.assertEqual(self.value("status", job)["transitions"], before)
        gate.touch()
        self.assertEqual(self.wait(job)["outcome"], "succeeded")

    def test_client_group_signals_and_supervisor_interrupts_are_isolated(self):
        argv, ready, gate, done = self.barrier()
        wrapper = "import subprocess,sys,time; r=subprocess.run(sys.argv[1:]); time.sleep(2); sys.exit(r.returncode)"
        process = subprocess.Popen([sys.executable, "-c", wrapper, sys.executable, "-B", str(CLI),
                                    "--state-dir", str(self.state), "submit", "--", *argv],
                                   start_new_session=True, env=self.env, cwd=self.project,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(process)
        line = process.stdout.readline()
        submitted = json.loads(line)
        self.jobs.append((submitted["job"], self.state))
        self.running(submitted["job"], ready)
        os.killpg(process.pid, signal.SIGHUP)
        process.communicate(timeout=3)
        current = self.value("status", submitted["job"])
        self.assertEqual(current["state"], "running")
        supervisor = current["supervisor"]["pid"]
        os.kill(supervisor, signal.SIGINT)
        os.kill(supervisor, signal.SIGHUP)
        self.assertFalse(done.exists())
        gate.touch()
        self.assertEqual(self.wait(submitted["job"])["outcome"], "succeeded")

    def test_lost_supervisor_retains_slot_without_duplicate(self):
        argv, ready, gate, done = self.barrier()
        job = self.submit(argv)["job"]
        self.running(job, ready)
        current = self.value("status", job)
        os.kill(current["supervisor"]["pid"], signal.SIGKILL)
        uncertain = self.until(lambda: self.value("status", job)["view"] == "uncertain")
        self.assertTrue(uncertain)
        self.assertEqual(self.wait(job, 3)["state"], "running")
        next_argv, next_ready, next_gate, next_done = self.barrier()
        queued = self.submit(next_argv)
        self.assertEqual(queued["state"], "queued")
        self.assertEqual(self.wait(queued["job"], 3)["view"], "uncertain")
        gate.touch()
        self.until(done.exists)
        current = self.value("status", queued["job"])
        self.assertEqual(current["state"], "queued")
        self.assertFalse(next_ready.exists())
        self.assertEqual([t["to"] for t in current["transitions"]], ["queued"])

    def test_supervisor_launch_failure_is_not_accepted(self):
        first = self.submit([sys.executable, "-c", "pass"])
        self.wait(first["job"])
        log = self.state / "supervisor.log"
        log.unlink()
        log.mkdir()
        result = self.call("submit", "--", sys.executable, "-c", "print('must not run')", check=False)
        self.assertEqual(result.returncode, 1)
        value = json.loads(result.stdout)
        self.assertFalse(value["accepted"])
        self.assertEqual(value["state"], "finished")
        final = self.wait(value["job"], 1)
        self.assertEqual(final["outcome"], "failed")
        self.assertIn("supervisor spawn failed", final["detail"])
        log.rmdir()
        next_job = self.submit([sys.executable, "-c", "pass"])
        self.wait(next_job["job"])

    def test_two_private_states_can_run_together(self):
        other = self.root / "other-state"
        first_argv, first_ready, first_gate, _ = self.barrier()
        second_argv, second_ready, second_gate, _ = self.barrier()
        first = self.submit(first_argv)
        second = self.submit(second_argv, state=other)
        self.running(first["job"], first_ready)
        self.running(second["job"], second_ready, state=other)
        self.assertEqual(self.value("status", first["job"])["state"], "running")
        self.assertEqual(self.value("status", second["job"], state=other)["state"], "running")
        self.assertEqual(self.call("status", first["job"], state=other, check=False).returncode, 2)
        first_gate.touch()
        second_gate.touch()
        self.wait(first["job"])
        self.wait(second["job"], state=other)
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertFalse((self.root / "xdg").exists())
        self.assertEqual(list(self.project.iterdir()), [])

    def test_json_errors_missing_logs_and_state_selection(self):
        for args in (("nonsense",), ("submit", "--"), ("status", "unknown"),
                     ("wait", "unknown", "--timeout", "nan")):
            result = self.call(*args, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn("error", json.loads(result.stdout))
        job = self.submit([sys.executable, "-c", "pass"])["job"]
        final = self.wait(job)
        Path(final["stdout_log"]).unlink()
        self.assertEqual(self.value("status", job)["outcome"], "incomplete")
        self.wait(job, 3)
        env = dict(self.env, FLEET_STATE_DIR=str(self.root / "environment-state"))
        process = subprocess.run([sys.executable, "-B", str(CLI), "submit", "--", sys.executable,
                                  "-c", "pass"], cwd=self.project, env=env,
                                 capture_output=True, text=True, timeout=15)
        self.assertEqual(process.returncode, 0, process.stdout)
        value = json.loads(process.stdout)
        environment_state = self.root / "environment-state"
        self.jobs.append((value["job"], environment_state))
        self.wait(value["job"], state=environment_state)
        self.assertEqual(self.call("status", value["job"], env=env, check=False).returncode, 2)

    def test_default_state_and_reserved_paths(self):
        result = subprocess.run([sys.executable, "-B", str(CLI), "submit", "--",
                                 sys.executable, "-c", "pass"], cwd=self.project, env=self.env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout)
        job = json.loads(result.stdout)["job"]
        default_state = self.root / "xdg/fleet-pilot"
        self.jobs.append((job, default_state))
        self.wait(job, state=default_state)
        reserved = self.root / "xdg/pao"
        refused = self.call("submit", "--", sys.executable, "-c", "pass",
                            state=reserved, check=False)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("reserved", json.loads(refused.stdout)["error"])
        self.assertFalse(reserved.exists())


if __name__ == "__main__":
    unittest.main()
