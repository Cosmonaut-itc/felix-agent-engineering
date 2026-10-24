import concurrent.futures
import getpass
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from content_cases import ContentCases
from correction_cases import CorrectionCases
from integral_cases import IntegralCases
from reader_temp_cases import ReaderTempCases
from review_r2_cases import ReviewR2Cases

CLI = Path(__file__).resolve().parents[1] / "bin/fleet"
BARRIER = """
import pathlib, sys, time
ready, gate, done = map(pathlib.Path, sys.argv[1:])
ready.write_text('ready')
deadline = time.monotonic() + 6
while not gate.exists() and time.monotonic() < deadline:
    time.sleep(0.02)
try:
    print('ok', flush=True)
except BrokenPipeError:
    pass
done.write_text('done')
sys.exit(0 if gate.exists() else 90)
"""


class Admission:
    def __init__(self, state):
        self.state = state
        self.database_absent_before = not (state / "state.db").exists()
        self.job = self.attempt = None
        self.refuted = False

    def attribute(self, response):
        job, attempt = response.get("job"), response.get("attempt")
        if (not isinstance(job, str) or not re.fullmatch(r"j_[0-9a-f]{32}", job)
                or not isinstance(attempt, str) or not re.fullmatch(r"a_[0-9a-f]{32}", attempt)):
            raise AssertionError("submit response has no attributable identity")
        self.job, self.attempt = job, attempt

    def refute_if_absent(self, result):
        if (result.returncode is not None and self.database_absent_before
                and not (self.state / "state.db").exists()):
            self.refuted = True


@unittest.skipUnless(os.name == "posix", "local pilot requires POSIX")
class FleetCLI(ContentCases, CorrectionCases, ReaderTempCases, IntegralCases, ReviewR2Cases, unittest.TestCase):
    def setUp(self):
        fixture_base = (os.environ.get("FLEET_CASE_SENSITIVE_TMPDIR")
                        if "case_sensitive_distinct" in self._testMethodName else None)
        self.root = Path(tempfile.mkdtemp(prefix="fleet-cli-", dir=fixture_base)).resolve()
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
        self.admissions = []
        self.processes = []
        self.client_groups = []

    def tearDown(self):
        self.cleanup()

    def cleanup(self):
        self.settle()
        self.guard_admissions()
        shutil.rmtree(self.root)

    def guard_admissions(self):
        unresolved = [str(a.state) for a in self.admissions if a.job is None and not a.refuted]
        if unresolved:
            self.fail("unattributed admission; retained {}: {}".format(self.root, unresolved))

    def settle(self):
        for gate, done in self.gates:
            gate.touch()
        for process in self.processes:
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                self.fail("owned CLI process did not exit; retained {}".format(self.root))
        supervisors, groups = set(), set(self.client_groups)
        deadline = time.monotonic() + 9
        while True:
            pending = []
            for admission in self.admissions:
                if admission.job is None:
                    continue
                job = admission.job
                result = self.call("status", job, state=admission.state, check=False)
                if result.returncode != 0:
                    pending.append((job, result.stdout, result.stderr))
                    continue
                value = json.loads(result.stdout)
                if (value["job"], value["attempt"]) != (job, admission.attempt):
                    pending.append((job, "public identity mismatch"))
                    continue
                if value["supervisor"]:
                    supervisors.add(value["supervisor"]["pid"])
                if value["child_pid"] is not None:
                    groups.add(value["child_pid"])
                if value["state"] in ("starting", "running") and value["child_pid"] is None:
                    pending.append((job, "command creation unconfirmed"))
                if value["state"] == "running" and not value["supervisor"]:
                    pending.append((job, "supervisor identity missing"))
            for kind, pids, probe in (("supervisor", supervisors, os.kill),
                                      ("command group", groups, os.killpg)):
                for pid in pids:
                    try:
                        probe(pid, 0)
                    except ProcessLookupError:
                        continue
                    except OSError as exc:
                        pending.append((kind, pid, str(exc)))
                    else:
                        pending.append((kind, pid, "still present"))
            if not pending:
                return
            if time.monotonic() >= deadline:
                self.fail("owned process absence unconfirmed; retained {}: {}".format(
                    self.root, pending))
            time.sleep(0.05)

    def admit(self, state):
        self.assertIn(self.root, state.parents)
        admission = Admission(state)
        self.admissions.append(admission)
        return admission

    def call(self, *args, state=None, check=True, env=None, refute_if_absent=False):
        admission = self.admit(state or self.state) if args and args[0] == "submit" else None
        command = [sys.executable, "-B", str(CLI), "--state-dir", str(state or self.state)]
        result = subprocess.run(command + list(args), env=env or self.env,
                                cwd=str(self.project), capture_output=True, text=True, timeout=15)
        if admission is not None:
            value = json.loads(result.stdout)
            if "job" in value:
                admission.attribute(value)
            elif refute_if_absent:
                admission.refute_if_absent(result)
        if check:
            self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        return result

    def value(self, *args, **kwargs):
        return json.loads(self.call(*args, **kwargs).stdout)

    def submit(self, argv, state=None, client=None, env=None):
        options = ["submit"] + (["--client", client] if client is not None else [])
        response = self.call(*options, "--", *argv, state=state, env=env, check=False)
        self.assertIn(response.returncode, (0, 3), (response.stdout, response.stderr))
        value = json.loads(response.stdout)
        self.assertTrue(value["accepted"])
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

    def response_line(self, process):
        result = bytearray()
        deadline = time.monotonic() + 5
        while b"\n" not in result:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                self.fail("submit response timed out; admission remains pending")
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk:
                self.fail("submit response ended without a complete line; admission remains pending")
            result.extend(chunk)
            if len(result) > 65536:
                self.fail("submit response limit exceeded; admission remains pending")
        return result.split(b"\n", 1)[0].decode()

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
        (self.project / "identity.txt").write_text("materialized-content")
        script = "import os,sys,pathlib; print(os.getcwd()); print(repr(sys.stdin.read())); print(sys.argv[1]); print(pathlib.Path('identity.txt').read_text()); print('err',file=sys.stderr)"
        job = self.submit([sys.executable, "-c", script, literal])["job"]
        final = self.wait(job)
        lines = Path(final["stdout_log"]).read_text().splitlines()
        self.assertNotEqual(lines[0], str(self.project))
        self.assertEqual(lines[1:], ["''", literal, "materialized-content"])
        self.assertEqual(final["input"], self.value("status", job)["input"])
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
        views = [self.value("status", item["job"]) for item in following]
        self.assertTrue(all(v["state"] == "queued" for v in views))
        gate.touch()
        finals = [self.wait(first["job"])] + [self.wait(item["job"]) for item in following]
        ordered = sorted(finals, key=lambda v: v["seq"])
        for left, right in zip(ordered, ordered[1:]):
            self.assertLessEqual(left["ended_at"], right["started_at"])
            self.assertLess(left["transitions"][-1]["seq"], right["transitions"][-2]["seq"])

    def test_queued_job_uses_its_submitters_environment_and_path(self):
        responses, control_output, jobs, fixtures, expected = [], [], [], [], []

        def control_call(*args, env=None):
            result = self.call(*args, env=env)
            control_output.extend((result.stdout, result.stderr))
            value = json.loads(result.stdout)
            responses.append(value)
            return value

        script = ("import json, os\n"
                  "print(json.dumps([os.environ['FLEET_TEST_MARK'], os.environ['PATH']]), "
                  "flush=True)\n" + BARRIER)
        for label in ("a", "b"):
            directory = self.root / ("env-bin-" + label)
            directory.mkdir()
            executable = directory / "fleet-env-python"
            executable_marker = "fictitious-fleet-executable-" + label
            executable.write_text(
                f"#!{sys.executable}\n"
                "import os, sys\n"
                f"print({executable_marker!r}, flush=True)\n"
                f"os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])\n")
            executable.chmod(0o700)
            marker = "fictitious-fleet-environment-" + label
            env = dict(self.env, FLEET_TEST_MARK=marker, PATH=str(directory))
            argv, ready, gate, done = self.barrier()
            argv[:4] = ["fleet-env-python", "-B", "-c", script]
            submitted = control_call("submit", "--", *argv, env=env)
            self.assertTrue(submitted["accepted"])
            jobs.append(submitted["job"])
            fixtures.append((ready, gate, done))
            expected.append([marker, str(directory), executable_marker])
            if label == "a":
                self.running(jobs[0], ready)
        queued = control_call("status", jobs[1])
        self.assertEqual(queued["state"], "queued")
        self.assertFalse(fixtures[1][0].exists())
        control_call("status", jobs[0])
        fixtures[0][1].touch()
        first = control_call("wait", jobs[0], "--timeout", "9")
        self.running(jobs[1], fixtures[1][0])
        control_call("status", jobs[1])
        fixtures[1][1].touch()
        second = control_call("wait", jobs[1], "--timeout", "9")
        for final, report in zip((first, second), expected):
            lines = Path(final["stdout_log"]).read_text().splitlines()
            self.assertEqual(lines[0], report[2])
            self.assertEqual(json.loads(lines[1]), report[:2])
            self.assertEqual(lines[2:], ["ok"])
        control = "".join(control_output) + (self.state / "supervisor.log").read_text()
        for report in expected:
            self.assertFalse(report[0] in control, "fictitious environment marker leaked")
            self.assertFalse(report[1] in control, "fictitious PATH leaked")
            self.assertFalse(report[2] in control, "fictitious executable marker leaked")
        self.assertTrue(all("env" not in response for response in responses))

    def test_descendant_holds_slot_after_root_exits(self):
        argv, ready, gate, done = self.barrier()
        child_script = BARRIER.replace("import pathlib, sys, time", "import json, os, pathlib, sys, time")
        child_script = child_script.replace("ready.write_text('ready')",
                                            "ready.write_text(json.dumps({'pgid': os.getpgrp()}))")
        root_script = ("import subprocess, sys\n"
                       "subprocess.Popen([sys.executable, '-B', '-c', sys.argv[1], *sys.argv[2:]])\n")
        first = self.submit([sys.executable, "-B", "-c", root_script, child_script, *argv[4:]])
        self.running(first["job"], ready)
        current = self.value("status", first["job"])
        root_pid = current["child_pid"]
        self.assertEqual(json.loads(ready.read_text())["pgid"], root_pid)

        def root_absent():
            try:
                os.kill(root_pid, 0)
            except ProcessLookupError:
                return True
            return False

        self.until(root_absent)
        self.until(lambda: self.value("status", first["job"])["view"] == "uncertain")
        os.killpg(root_pid, 0)
        next_argv, next_ready, next_gate, _ = self.barrier()
        second = self.submit(next_argv)
        self.assertEqual(second["state"], "queued")
        queued = self.wait(second["job"], 3)
        self.assertEqual((queued["state"], queued["view"]), ("queued", "uncertain"))
        self.assertFalse(next_ready.exists())
        self.assertFalse(done.exists())
        gate.touch()
        self.until(lambda: self.value("status", first["job"])["state"] == "finished")
        self.assertEqual(self.wait(first["job"])["outcome"], "succeeded")
        self.until(done.exists)
        self.running(second["job"], next_ready)
        next_gate.touch()
        self.assertEqual(self.wait(second["job"])["outcome"], "succeeded")

    def test_cleanup_retains_accepted_but_unattributed_admission(self):
        argv, ready, gate, done = self.barrier()
        response = self.root / "unattributed-submit.json"
        admission = self.admit(self.state)
        with response.open("w") as output:
            result = subprocess.run([sys.executable, "-B", str(CLI), "--state-dir", str(self.state),
                                     "submit", "--", *argv], env=self.env, cwd=self.project,
                                    stdout=output, stderr=subprocess.PIPE, text=True, timeout=15)
        try:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.until(ready.exists)
            self.assertIsNone(admission.job)
            with self.assertRaisesRegex(AssertionError, "unattributed admission"):
                self.guard_admissions()
            self.assertTrue(self.root.is_dir())
            self.assertTrue(response.is_file())
            self.assertFalse(gate.exists())
            self.assertFalse(done.exists())
        finally:
            value = json.loads(response.read_text())
            admission.attribute(value)
            gate.touch()
        self.assertTrue(value["accepted"])
        self.assertEqual(self.wait(value["job"])["outcome"], "succeeded")
        self.settle()

    def test_public_state_directory_is_refused_without_changes(self):
        public = self.root / "public-state"
        public.mkdir(mode=0o755)
        public.chmod(0o755)
        mode = public.stat().st_mode
        result = self.call("submit", "--", sys.executable, "-c", "pass", state=public,
                           check=False, refute_if_absent=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("private", json.loads(result.stdout)["error"])
        self.assertFalse((public / "state.db").exists())
        self.assertEqual(public.stat().st_mode, mode)
        self.assertEqual(list(public.iterdir()), [])
        self.assertTrue(self.admissions[-1].refuted)

    def test_wait_timeout_and_interrupt_do_not_change_execution(self):
        argv, ready, gate, done = self.barrier()
        job = self.submit(argv)["job"]
        self.running(job, ready)
        before = self.value("status", job)["transitions"]
        result = self.call("wait", job, "--timeout", "0", check=False)
        self.assertEqual(result.returncode, 4)
        self.assertTrue(json.loads(result.stdout)["timed_out"])
        original_sigint = signal.signal(signal.SIGINT, signal.SIG_DFL)
        try:
            process = subprocess.Popen([sys.executable, "-B", "-X", "importtime", str(CLI),
                                        "--state-dir", str(self.state),
                                        "wait", job], env=self.env, cwd=self.project,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.processes.append(process)
        finally:
            signal.signal(signal.SIGINT, original_sigint)
        import_trace = bytearray()
        deadline = time.monotonic() + 3
        while not any(line.startswith(b"import time:") and
                      line.rsplit(b"|", 1)[-1].strip() == b"fleet.cli"
                      for line in import_trace.split(b"\n")[:-1]):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([process.stderr], [], [], remaining)[0]:
                self.fail("fleet.cli importtime marker timed out: {}".format(
                    import_trace.decode(errors="replace")))
            chunk = os.read(process.stderr.fileno(), 4096)
            if not chunk:
                self.fail("reader ended before fleet.cli importtime marker: {}".format(
                    import_trace.decode(errors="replace")))
            import_trace.extend(chunk)
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=3)
        stderr = import_trace.decode(errors="replace") + stderr
        self.assertEqual(process.returncode, 130, (stdout, stderr))
        self.assertIn("interrupted", json.loads(stdout)["error"])
        self.assertEqual(self.value("status", job)["transitions"], before)
        gate.touch()
        self.assertEqual(self.wait(job)["outcome"], "succeeded")

    def test_client_group_signals_and_supervisor_interrupts_are_isolated(self):
        argv, ready, gate, done = self.barrier()
        wrapper = "import subprocess,sys,time; r=subprocess.run(sys.argv[1:]); time.sleep(2); sys.exit(r.returncode)"
        admission = self.admit(self.state)
        process = subprocess.Popen([sys.executable, "-c", wrapper, sys.executable, "-B", str(CLI),
                                    "--state-dir", str(self.state), "submit", "--", *argv],
                                   start_new_session=True, env=self.env, cwd=self.project,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(process)
        self.client_groups.append(process.pid)
        line = self.response_line(process)
        submitted = json.loads(line)
        admission.attribute(submitted)
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
        response = self.call("submit", "--", *next_argv, check=False)
        self.assertEqual(response.returncode, 3, response.stdout)
        queued = json.loads(response.stdout)
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
        for args in (("nonsense",), ("status", "unknown"),
                     ("wait", "unknown", "--timeout", "nan")):
            result = self.call(*args, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn("error", json.loads(result.stdout))
        job = self.submit([sys.executable, "-c", "pass"])["job"]
        final = self.wait(job)
        Path(final["stdout_log"]).unlink()
        self.assertEqual(self.value("status", job)["outcome"], "incomplete")
        self.wait(job, 3)
        environment_state = self.root / "environment-state"
        env = dict(self.env, FLEET_STATE_DIR=str(environment_state))
        admission = self.admit(environment_state)
        process = subprocess.run([sys.executable, "-B", str(CLI), "submit", "--", sys.executable,
                                  "-c", "pass"], cwd=self.project, env=env,
                                 capture_output=True, text=True, timeout=15)
        value = json.loads(process.stdout)
        admission.attribute(value)
        self.assertEqual(process.returncode, 0, process.stdout)
        self.wait(value["job"], state=environment_state)
        self.assertEqual(self.call("status", value["job"], env=env, check=False).returncode, 2)

    def test_default_state_and_reserved_paths(self):
        default_state = self.root / "xdg/fleet-pilot"
        admission = self.admit(default_state)
        result = subprocess.run([sys.executable, "-B", str(CLI), "submit", "--",
                                 sys.executable, "-c", "pass"], cwd=self.project, env=self.env,
                                capture_output=True, text=True, timeout=15)
        value = json.loads(result.stdout)
        admission.attribute(value)
        job = value["job"]
        self.assertEqual(result.returncode, 0, result.stdout)
        self.wait(job, state=default_state)
        reserved = self.root / "xdg/pao"
        refused = self.call("submit", "--", sys.executable, "-c", "pass",
                            state=reserved, check=False, refute_if_absent=True)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("reserved", json.loads(refused.stdout)["error"])
        self.assertFalse(reserved.exists())


if __name__ == "__main__":
    unittest.main()
