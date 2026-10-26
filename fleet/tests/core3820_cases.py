"""SEN-3820 CLI contracts, written only. No Fleet imports or private DB access."""
import concurrent.futures
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from operator3820_cases import Operator3820Cases

COUNTED = r"""
import os, pathlib, sys, time
counter, ready, gate = map(pathlib.Path, sys.argv[1:4])
fd = os.open(counter, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
os.write(fd, (sys.argv[4] + '\n').encode()); os.fsync(fd); os.close(fd)
ready.write_text('ready')
end = time.monotonic() + 30
while not gate.exists() and time.monotonic() < end:
    time.sleep(0.01)
if not gate.exists():
    raise SystemExit(90)
raise SystemExit(int(sys.argv[5]))
"""

# Single-threaded child preparation, never preexec_fn or a parent signal policy.
# exec preserves the PID/session and the defaults; the new Python interpreter
# then installs its normal SIGINT handler instead of inheriting SIG_IGN.
SIGNAL_CLIENT = r"""
import os, signal, sys
for name in (signal.SIGHUP, signal.SIGINT):
    signal.signal(name, signal.SIG_DFL)
    if signal.getsignal(name) != signal.SIG_DFL:
        raise SystemExit(91)
os.write(2, b'fleet-test-signal-defaults-before-exec\n')
os.execv(sys.argv[1], sys.argv[1:])
"""


class Core3820Cases(Operator3820Cases):
    def raw(self, *args, code=0, **kwargs):
        result = self.call(*args, check=False, **kwargs)
        self.assertEqual(result.returncode, code, (result.stdout, result.stderr))
        value = json.loads(result.stdout)
        self.assertEqual(value["api"], "fleet-job-api@1")
        return value

    def send(self, key, argv, options=(), code=0, **kwargs):
        return self.raw("submit", "--client", "contract", "--idempotency-key", key,
                        *options, "--", *argv, code=code, **kwargs)

    def status_key(self, key, **kwargs):
        return self.raw("status", "--idempotency-key", key, **kwargs)

    def counted(self, label="entry", exit_code=0):
        directory = self.root / ("counted-" + str(len(self.gates)))
        directory.mkdir()
        counter, ready, gate, done = [directory / name for name in ("counter", "ready", "gate", "done")]
        self.gates.append((gate, done))
        return [sys.executable, "-B", "-c", COUNTED, str(counter), str(ready), str(gate), label,
                str(exit_code)], counter, ready, gate

    def entries(self, counter):
        return counter.read_text().splitlines() if counter.exists() else []

    def ready(self, key, path, **kwargs):
        return self.until(lambda: path.exists() and
                          self.status_key(key, **kwargs)["observation"]["state"] == "running")

    def terminal(self, key, **kwargs):
        def settled():
            value = self.status_key(key, **kwargs)
            observation = value["observation"]
            return value if (observation["state"] == "finished" and
                             observation["publication"]["state"] != "pending") else None
        return self.until(settled)

    def wait_key(self, key, code=0, **kwargs):
        return self.raw("wait", "--idempotency-key", key, "--timeout", "0", code=code, **kwargs)

    def same_receipt(self, first, replay):
        for field in ("authority_id", "principal_uid", "job", "attempt", "admission", "input"):
            self.assertEqual(first[field], replay[field])
        self.assertIs(replay["accepted"], True)
        self.assertIsNone(replay["observation"])

    def inventory(self, root):
        result = {}
        for base, directories, files in os.walk(root, followlinks=False):
            for name in [*directories, *files]:
                path = Path(base) / name
                info = path.lstat()
                content = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
                result[str(path.relative_to(root))] = (info.st_mode, info.st_size, info.st_mtime_ns,
                                                       info.st_ctime_ns, hashlib.sha256(content).hexdigest())
        return result

    def test_c01_equal_concurrent_admission_and_external_single_entry(self):
        argv, counter, ready, gate = self.counted()
        start = self.root / "clients-start"

        def submit():
            self.until(start.exists)
            return self.send("c01", argv)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit) for _ in range(2)]
            start.touch()
            responses = [future.result() for future in futures]
        self.assertEqual(sorted(r["binding"] for r in responses), ["new", "replay"])
        self.same_receipt(responses[0], responses[1])
        self.ready("c01", ready)
        self.assertEqual(self.entries(counter), ["entry"])
        gate.touch()
        self.terminal("c01")
        self.wait_key("c01")
        self.assertEqual(self.entries(counter), ["entry"])

    def test_c02_different_concurrent_content_conflicts_without_second_entry(self):
        first, counter, ready, gate = self.counted("left")
        first[3] = COUNTED.replace("raise SystemExit(int(sys.argv[5]))",
                                   "print(sys.argv[4], flush=True)\n"
                                   "pathlib.Path('winner.txt').write_text(sys.argv[4] + '\\n')\n"
                                   "raise SystemExit(int(sys.argv[5]))")
        second = list(first)
        second[-2] = "right"
        start = self.root / "different-start"

        def submit(argv):
            self.until(start.exists)
            return self.call("submit", "--client", "contract", "--idempotency-key", "c02",
                             "--artifact", "winner.txt", "--", *argv, check=False)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [(argv, pool.submit(submit, argv)) for argv in (first, second)]
            start.touch()
            results = [(argv, future.result()) for argv, future in futures]
        self.assertEqual(sorted(result.returncode for argv, result in results), [0, 5])
        winner_argv, winner_process = next(pair for pair in results if pair[1].returncode == 0)
        loser_argv, loser_process = next(pair for pair in results if pair[1].returncode == 5)
        winner, loser = json.loads(winner_process.stdout), json.loads(loser_process.stdout)
        winner_label, loser_label = winner_argv[-2], loser_argv[-2]
        self.assertNotEqual(winner_label, loser_label)
        for argv, result in results:
            self.assertEqual(result.args[-len(argv):], argv)
        self.external_evidence.append({"case": "C02", "clients": [
            {"argv": argv, "client_argv": result.args, "label": argv[-2], "returncode": result.returncode,
             "response": json.loads(result.stdout)} for argv, result in results]})
        self.assertEqual(loser["existing"], {"job": winner["job"], "attempt": winner["attempt"]})
        self.assertEqual(loser["error"], {"code": "idempotency_conflict", "effects": "no_new_admission"})
        self.ready("c02", ready)
        self.assertEqual(self.entries(counter), [winner_label])
        gate.touch()
        terminal = self.terminal("c02")
        self.wait_key("c02")
        proof = self.raw("evidence", winner["job"], "--attempt", winner["attempt"])
        for value in (terminal, proof):
            for field in ("job", "attempt", "input", "admission"):
                self.assertEqual(value[field], winner[field])
            evidence = value["observation"]["evidence"]
            self.assertIs(evidence["valid"], True)
            manifest = json.loads(Path(evidence["manifest"]).read_bytes())
            for field in ("job", "attempt", "input"):
                self.assertEqual(manifest[field], winner[field])
            self.assertEqual({entry["path"] for entry in manifest["entries"]},
                             {"stdout", "stderr", "artifacts/winner.txt"})
            self.assertEqual(len(evidence["files"]), 3)
            self.assertEqual({(entry["kind"], Path(entry["path"]).name) for entry in evidence["files"]},
                             {("log", "stdout"), ("log", "stderr"), ("artifact", "winner.txt")})
            for entry in evidence["files"]:
                self.assertIs(entry["valid"], True)
                content = Path(entry["path"]).read_bytes()
                expected = b"" if entry["path"].endswith("/stderr") else (winner_label + "\n").encode()
                self.assertEqual(content, expected)
                self.assertNotIn(loser_label.encode(), content)
                self.assertEqual(hashlib.sha256(content).hexdigest(), entry["sha256"])
        self.assertEqual(self.entries(counter), [winner_label])

    def test_r2_a1_complete_selections_validation_and_true_equivalences(self):
        (self.project / "foo").write_text("same captured bytes")
        (self.project / "bar").write_text("also captured")
        (self.project / ".env").write_text("private fixture")
        argv, counter, ready, gate = self.counted("A1")
        gate.touch()
        first = self.send("r2-a1", argv, options=("--input", ".",))
        before = self.terminal("r2-a1")
        for options in ((), ("--input", ".", "--input", ".")):
            with self.subTest(equivalent=options):
                replay = self.send("r2-a1", argv, options=options)
                self.same_receipt(first, replay)
                self.assertEqual(replay["binding"], "replay")
        conflict = self.send("r2-a1", argv, options=("--input", ".", "--input", "foo"), code=5)
        self.assertEqual(conflict["error"], {"code": "idempotency_conflict", "effects": "no_new_admission"})
        self.assertEqual(conflict["existing"], {"job": first["job"], "attempt": first["attempt"]})
        for additional, error_code in (("missing", "input_not_admitted"), (".env", "private_input")):
            with self.subTest(invalid_selection=additional):
                rejected = self.send("r2-a1", argv,
                                     options=("--input", ".", "--input", additional), code=2)
                self.assertEqual(rejected["error"]["code"], error_code)
                self.assertIs(rejected["candidate"]["accepted"], False)
                self.assertEqual(rejected["key_binding"], "not_evaluated")
        current = self.status_key("r2-a1")
        for field in ("job", "attempt", "admission", "input"):
            self.assertEqual(current[field], first[field])
        for field in ("transitions", "reservation", "command"):
            self.assertEqual(current["observation"][field], before["observation"][field])
        self.assertEqual(self.entries(counter), ["A1"])
        ordered = self.send("r2-a1-order", [sys.executable, "-c", "pass"],
                            options=("--input", "foo", "--input", "bar"))
        self.terminal("r2-a1-order")
        reordered = self.send("r2-a1-order", [sys.executable, "-c", "pass"],
                              options=("--input", "bar", "--input", "foo", "--input", "foo"))
        self.same_receipt(ordered, reordered)
        self.assertEqual(reordered["binding"], "replay")

    def test_r3_a02_empty_inventory_automatic_and_dot_replay_without_dispatch(self):
        # counted's resources are outside the source. No admitted file is planted
        # to make an allegedly empty inventory pass explicit-dot validation.
        self.assertEqual(list(self.project.iterdir()), [])
        self.assertFalse(any((p / ".git").exists() for p in (self.project, *self.project.parents)))
        argv, counter, ready, gate = self.counted("r3-empty")
        first = self.send("r3-a02-empty", argv, options=("--enqueue-only",))
        self.assertEqual(first["binding"], "new")
        original = self.status_key("r3-a02-empty")
        self.assertIsNone(first["input"]["commit_sha"])
        self.assertEqual(first["input"]["effective_sha256"], hashlib.sha256(b"[]").hexdigest())
        for options in (("--input", "."), ("--input", ".", "--input", "."), ()):
            with self.subTest(empty_equivalent=options):
                # Deliberately omit enqueue-only: a replay must not dispatch.
                replay = self.send("r3-a02-empty", argv, options=options)
                self.same_receipt(first, replay)
                self.assertEqual(replay["binding"], "replay")
        for extra, expected in (("missing", "input_not_admitted"), (".env", "private_input"),
                                ("..", "invalid_relative_path"), ("./", "invalid_relative_path")):
            with self.subTest(empty_invalid_extra=extra):
                rejected = self.send("r3-a02-empty", argv,
                                     options=("--input", ".", "--input", extra), code=2)
                self.assertEqual(rejected["error"]["code"], expected)
                self.assertIs(rejected["candidate"]["accepted"], False)
                self.assertEqual(rejected["key_binding"], "not_evaluated")
        # Default-excluded bytes also leave the admitted inventory truly empty.
        (self.project / ".env").write_text("fictitious excluded bytes")
        self.assertEqual([p.name for p in self.project.iterdir()], [".env"])
        for options in ((), ("--input", ".")):
            with self.subTest(excluded_only_equivalent=options):
                replay = self.send("r3-a02-empty", argv, options=options)
                self.same_receipt(first, replay)
                self.assertEqual(replay["binding"], "replay")
        # Same empty bytes do not erase a different contractual declaration.
        conflict = self.send("r3-a02-empty", argv,
                             options=("--input", ".", "--private", "other-private"), code=5)
        self.assertEqual(conflict["error"], {"code": "idempotency_conflict", "effects": "no_new_admission"})
        self.assertEqual(conflict["existing"], {"job": first["job"], "attempt": first["attempt"]})
        current = self.status_key("r3-a02-empty")
        for field in ("job", "attempt", "admission", "input", "observation"):
            if field == "observation":
                for name in ("state", "view", "dispatch", "reservation", "supervisor", "child_pid",
                             "transitions", "command", "control", "completion"):
                    self.assertEqual(current[field][name], original[field][name])
            else:
                self.assertEqual(current[field], original[field])
        self.assertEqual(current["observation"]["state"], "queued")
        self.assertIs(current["observation"]["dispatch"]["safe_pending"], True)
        self.assertEqual(self.entries(counter), [])
        self.assertFalse(ready.exists())
        self.assertFalse((self.state / "supervisor.log").exists())
        self.assertEqual(list(self.project.iterdir()), [self.project / ".env"])

    def test_r2_a4_reader_open_and_selector_failures_are_read_only(self):
        first = self.send("r2-a4", [sys.executable, "-c", "pass"])
        final = self.terminal("r2-a4")
        self.until(lambda: not self.process_present(final["observation"]["supervisor"]["pid"]))
        original = self.inventory(self.state)
        readers = (("status", first["job"]), ("evidence", first["job"]),
                   ("wait", first["job"], "--timeout", "0"), ("operator", "status"))
        missing = self.root / "r2-absent-state"
        for args in readers:
            with self.subTest(reader=args, condition="absent"):
                value = self.raw(*args, state=missing, code=2)
                self.assertEqual(value["error"], {"code": "state_not_found", "effects": "read_only"})
                self.assertNotIn("observation", value)
                self.assertFalse(missing.exists())
        try:
            self.state.chmod(0o755)
            for args in readers:
                with self.subTest(reader=args, condition="unsafe"):
                    value = self.raw(*args, code=2)
                    self.assertEqual(value["error"], {"code": "state_directory_not_private", "effects": "read_only"})
                    self.assertNotIn("observation", value)
        finally:
            self.state.chmod(0o700)
        self.assertEqual(self.inventory(self.state), original)
        for command in ("status", "evidence", "wait"):
            for selector in (("--idempotency-key", "r2-missing"),
                             (first["job"], "--attempt", "a_" + "f" * 32), ("invalid",)):
                with self.subTest(reader=command, selector=selector):
                    value = self.raw(command, *selector, code=2)
                    self.assertEqual(value["error"]["effects"], "read_only")
                    self.assertNotIn("observation", value)
        for options, expected in ((("--operation-id", "r2-missing"), "operation_not_found"),
                                  (("--after-seq", "-1"), "invalid_pagination")):
            value = self.raw("operator", "status", *options, code=2)
            self.assertEqual(value["error"], {"code": expected, "effects": "read_only"})
            self.assertNotIn("observation", value)
        self.assertEqual(self.inventory(self.state), original)

    def test_r2_a4_real_directory_lock_distinguishes_readers_and_writer(self):
        from test_cli import CLI
        first = self.send("r2-lock", [sys.executable, "-c", "pass"], options=("--enqueue-only",))
        before = self.inventory(self.state)
        directory = self.root / "r2-directory-lock"
        directory.mkdir()
        ready, gate, done = [directory / name for name in ("ready", "gate", "done")]
        self.gates.append((gate, done))
        fixture = r"""
import fcntl, os, pathlib, sys, time
state, ready, gate, done = map(pathlib.Path, sys.argv[1:])
fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    ready.write_text('exclusive directory lock acquired')
    deadline = time.monotonic() + 60
    while not gate.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not gate.exists():
        raise SystemExit(90)
finally:
    os.close(fd)
done.write_text('lock closed')
"""
        holder = subprocess.Popen([sys.executable, "-B", "-c", fixture, str(self.state),
                                   str(ready), str(gate), str(done)], cwd=self.project, env=self.env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(holder)
        try:
            self.until(ready.exists)
            readers = (("status", first["job"]), ("evidence", first["job"]),
                       ("wait", first["job"], "--timeout", "0"), ("operator", "status"))
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                futures = [(args, pool.submit(self.raw, *args, code=2)) for args in readers]
                for args, future in futures:
                    value = future.result()
                    self.assertEqual(value["error"], {"code": "state_lock_timeout", "effects": "read_only"})
                    self.assertNotIn("observation", value)
                    self.external_evidence.append({"case": "r2-A4-lock", "reader": args, "response": value})
            # Writers can wait again while closing a failed open. Bound this
            # genuine client separately; do not reclassify its unknown effects.
            writer = subprocess.Popen([sys.executable, "-B", str(CLI), "--state-dir", str(self.state),
                                       "operator", "drain", "--operation-id", "r2-locked-writer",
                                       "--reason", "own lock fixture"], cwd=self.project, env=self.env,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.processes.append(writer)
            stdout, stderr = writer.communicate(timeout=30)
            self.assertEqual(writer.returncode, 3, (stdout, stderr))
            value = json.loads(stdout)
            self.assertEqual(value["error"], {"code": "state_lock_timeout", "effects": "unknown"})
            self.external_evidence.append({"case": "r2-A4-lock", "writer": value})
            self.assertFalse(gate.exists(), "watchdog/release cannot substitute for contention")
            self.assertIsNone(holder.poll(), "fixture must still hold the lock")
            self.assertEqual(self.inventory(self.state), before)
        finally:
            gate.touch()
            stdout, stderr = holder.communicate(timeout=5)
            self.assertEqual(holder.returncode, 0, (stdout, stderr))
        self.assertTrue(done.exists())
        self.assertEqual(self.inventory(self.state), before)
        absent = self.raw("operator", "status", "--operation-id", "r2-locked-writer", code=2)
        self.assertEqual(absent["error"], {"code": "operation_not_found", "effects": "read_only"})

    def discarded_submit(self, key, argv):
        from test_cli import CLI
        admission = self.admit(self.state)
        result = subprocess.run([sys.executable, "-B", str(CLI), "--state-dir", str(self.state),
                                 "submit", "--client", "contract", "--idempotency-key", key, "--", *argv],
                                cwd=self.project, env=self.env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = self.status_key(key)
        admission.attribute(value)
        return value

    def test_c03_discarded_submit_stdout_recovers_by_key_and_replays_once(self):
        argv, counter, ready, gate = self.counted()
        recovered = self.discarded_submit("c03", argv)
        self.ready("c03", ready)
        replay = self.send("c03", argv)
        self.assertEqual((replay["job"], replay["attempt"]), (recovered["job"], recovered["attempt"]))
        self.assertEqual(replay["binding"], "replay")
        self.assertEqual(self.entries(counter), ["entry"])
        gate.touch()
        self.terminal("c03")
        self.wait_key("c03")

    def test_c04_lookup_after_origin_removed_and_candidate_rejection_is_scoped(self):
        argv, counter, ready, gate = self.counted()
        original = self.discarded_submit("c04", argv)
        self.ready("c04", ready)
        source = self.root / "removed-origin"
        self.project.rename(source)
        self.project.mkdir()
        shutil.rmtree(source)
        changed = dict(self.env, VOLATILE="different")
        current = self.status_key("c04", env=changed)
        self.assertEqual(current["job"], original["job"])
        rejected = self.send("c04", argv, options=("--cwd", str(source)), code=2)
        self.assertIs(rejected["candidate"]["accepted"], False)
        self.assertEqual(rejected["key_binding"], "not_evaluated")
        self.assertEqual(self.status_key("c04")["job"], original["job"])
        gate.touch()
        self.terminal("c04")
        self.wait_key("c04", env=changed)
        self.raw("evidence", "--idempotency-key", "c04", env=changed)
        self.assertEqual(self.entries(counter), ["entry"])

    def test_c05_finished_success_and_failure_replay_never_execute_again(self):
        for code in (0, 7):
            with self.subTest(exit_code=code):
                key = "c05-" + str(code)
                argv, counter, ready, gate = self.counted(exit_code=code)
                gate.touch()
                first = self.send(key, argv)
                self.terminal(key)
                self.wait_key(key, 0 if code == 0 else 1)
                replay = self.send(key, argv)
                self.same_receipt(first, replay)
                self.assertEqual(replay["binding"], "replay")
                self.assertEqual(self.entries(counter), ["entry"])

    def lost_owner(self, key):
        argv, counter, ready, gate = self.counted()
        first = self.send(key, argv)
        self.ready(key, ready)
        running = self.status_key(key)["observation"]
        os.kill(running["supervisor"]["pid"], signal.SIGKILL)
        self.until(lambda: self.status_key(key)["observation"]["view"] == "uncertain")
        gate.touch()
        self.until(lambda: not self.group_present(running["child_pid"]))
        return first, argv, counter

    def group_present(self, pgid):
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False

    def test_c06_lost_supervisor_and_empty_group_do_not_prove_terminal(self):
        first, argv, counter = self.lost_owner("c06")
        next_argv, next_counter, ready, gate = self.counted("next")
        self.send("c06-next", next_argv)
        replay = self.send("c06", argv)
        self.same_receipt(first, replay)
        self.wait_key("c06", 3)
        self.wait_key("c06-next", 3)
        self.raw("recover", code=3)
        self.assertFalse(ready.exists())
        self.assertEqual(self.entries(counter), ["entry"])
        self.assertEqual(self.entries(next_counter), [])
        self.assertEqual(self.status_key("c06-next")["observation"]["blocked_by"]["reason"], "owner_uncertain")

    def test_c07_sigint_wait_only_interrupts_reader(self):
        from test_cli import CLI
        import select
        argv, counter, ready, gate = self.counted()
        self.send("c07", argv)
        self.ready("c07", ready)
        before = self.status_key("c07")["observation"]
        process = subprocess.Popen([sys.executable, "-B", "-c", SIGNAL_CLIENT,
                                    sys.executable, "-B", "-X", "importtime", str(CLI), "--state-dir", str(self.state),
                                    "wait", "--idempotency-key", "c07"], cwd=self.project, env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(process)
        trace = b""
        deadline = time.monotonic() + 5
        while not any(line.rsplit(b"|", 1)[-1].strip() == b"fleet.cli" for line in trace.splitlines()):
            remaining = deadline - time.monotonic()
            self.assertGreater(remaining, 0, "reader import acknowledgement missing")
            self.assertTrue(select.select([process.stderr], [], [], remaining)[0])
            chunk = os.read(process.stderr.fileno(), 4096)
            self.assertTrue(chunk)
            trace += chunk
        self.assertIn(b"fleet-test-signal-defaults-before-exec\n", trace)
        # This is an import acknowledgement, not proof of entering main's try.
        # A pre-main interruption must fail the JSON/130 oracle, never pass as 0.
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 130, (stdout, stderr))
        self.assertEqual(json.loads(stdout)["error"]["code"], "client_interrupted")
        current = self.status_key("c07")["observation"]
        for field in ("state", "view", "revision", "transitions", "reservation", "capacity_resolution",
                      "control", "dispatch", "supervisor", "child_pid", "command"):
            self.assertEqual(current[field], before[field])
        self.external_evidence.append({"case": "C07", "signals_default_before_exec": True,
                                       "readiness": "fleet.cli import only; main entry not proven",
                                       "client_returncode": process.returncode, "response": json.loads(stdout)})
        self.assertEqual(self.entries(counter), ["entry"])
        gate.touch()
        self.terminal("c07")
        self.wait_key("c07")

    def test_c08_client_group_loss_preserves_original_supervisor(self):
        from test_cli import CLI
        argv, counter, ready, gate = self.counted()
        admission = self.admit(self.state)
        wrapper = "import subprocess,sys,time; subprocess.run(sys.argv[1:]); time.sleep(30)"
        process = subprocess.Popen([sys.executable, "-B", "-c", SIGNAL_CLIENT,
                                    sys.executable, "-c", wrapper, sys.executable, "-B", str(CLI),
                                    "--state-dir", str(self.state), "submit", "--client", "contract",
                                    "--idempotency-key", "c08", "--", *argv], cwd=self.project, env=self.env,
                                   start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.processes.append(process)
        self.until(ready.exists)
        admitted = self.status_key("c08")
        admission.attribute(admitted)
        original = admitted["observation"]["supervisor"]
        os.killpg(process.pid, signal.SIGHUP)
        _, stderr = process.communicate(timeout=5)
        self.assertIn(b"fleet-test-signal-defaults-before-exec\n", stderr)
        self.assertEqual(process.returncode, -signal.SIGHUP, stderr)
        current = self.status_key("c08")
        self.assertEqual(current["observation"]["supervisor"], original)
        self.assertEqual(current["observation"]["state"], "running")
        self.assertEqual(current["observation"]["reservation"], admitted["observation"]["reservation"])
        self.assertEqual(current["observation"]["transitions"], admitted["observation"]["transitions"])
        self.external_evidence.append({"case": "C08", "signals_default_before_exec": True,
                                       "client_returncode": process.returncode, "original_supervisor": original})
        gate.touch()
        self.terminal("c08")
        self.wait_key("c08")
        self.assertEqual(self.entries(counter), ["entry"])

    def test_c09_timeout_only_for_safe_pending_and_uncertainty_precedes_it(self):
        argv, counter, ready, gate = self.counted()
        self.send("c09", argv)
        self.ready("c09", ready)
        self.wait_key("c09", 4)
        gate.touch()
        self.terminal("c09")
        self.wait_key("c09")
        self.lost_owner("c09-uncertain")
        self.wait_key("c09-uncertain", 3)
        # Exact terminal-publication/release pending requires C20/O07 conditions.

    def test_c10_enqueue_only_survives_and_two_recoverers_claim_once(self):
        argv, counter, ready, gate = self.counted()
        first = self.send("c10", argv, options=("--enqueue-only",))
        queued = self.status_key("c10")["observation"]
        self.assertEqual(queued["state"], "queued")
        self.assertIs(queued["dispatch"]["safe_pending"], True)
        self.assertIn("dispatch_not_requested", queued["dispatch"]["reasons"])
        replay = self.send("c10", argv)
        self.same_receipt(first, replay)
        self.assertFalse(ready.exists(), "replay must not dispatch enqueue-only")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            processes = list(pool.map(lambda _: self.call("recover", check=False), range(2)))
        for process in processes:
            self.assertIn(process.returncode, (0, 3), (process.stdout, process.stderr))
        recoveries = [json.loads(process.stdout) for process in processes]
        self.ready("c10", ready)
        self.assertEqual(sum(len(r["dispatch_claimed"]) for r in recoveries), 1)
        gate.touch()
        self.terminal("c10")
        self.wait_key("c10")
        self.assertEqual(self.entries(counter), ["entry"])

    def test_c11_results_survive_new_coordinator_processes(self):
        for exit_code in (0, 7):
            key = "c11-" + str(exit_code)
            argv, counter, ready, gate = self.counted(exit_code=exit_code)
            gate.touch()
            first = self.send(key, argv)
            final = self.terminal(key)
            self.raw("recover")
            current = self.status_key(key)
            self.assertEqual(current["input"], first["input"])
            self.assertEqual(current["observation"]["command"], final["observation"]["command"])
            proof = self.raw("evidence", first["job"], "--attempt", first["attempt"])
            self.assertIs(proof["observation"]["evidence"]["valid"], True)
            self.wait_key(key, 0 if exit_code == 0 else 1)
            self.assertEqual(self.entries(counter), ["entry"])

    def test_c12_recover_preserves_live_owner_and_does_not_adopt_lost_owner(self):
        argv, counter, ready, gate = self.counted()
        self.send("c12", argv)
        self.ready("c12", ready)
        owner = self.status_key("c12")["observation"]["supervisor"]
        recovery = self.raw("recover")
        self.assertEqual(len(recovery["live_confirmed"]), 1)
        self.assertEqual(recovery["dispatch_claimed"], [])
        self.assertEqual(self.status_key("c12")["observation"]["supervisor"], owner)
        gate.touch()
        self.terminal("c12")
        self.lost_owner("c12-lost")
        self.raw("recover", code=3)
        self.assertEqual(self.entries(counter), ["entry"])

    def test_c13_effective_bytes_catalog_mode_head_and_index(self):
        env = dict(self.env, GIT_AUTHOR_NAME="fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                   GIT_COMMITTER_NAME="fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")

        def git(*args):
            result = subprocess.run(["git", "--no-optional-locks", *args], cwd=self.project, env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)

        git("init", "-q")
        path = self.project / "input.txt"
        path.write_text("original")
        git("add", "input.txt")
        git("-c", "core.hooksPath=/dev/null", "commit", "-qm", "fixture")
        argv, counter, ready, gate = self.counted()
        gate.touch()
        first = self.send("c13", argv, env=env)
        self.terminal("c13")
        path.write_text("different")
        self.send("c13", argv, code=5, env=env)
        path.write_text("original")
        extra = self.project / "extra"
        extra.write_text("catalog")
        self.send("c13", argv, code=5, env=env)
        extra.unlink()
        path.chmod(0o755)
        self.send("c13", argv, code=5, env=env)
        path.chmod(0o644)
        # Index differs; working-tree bytes and effective catalogue do not.
        path.write_text("staged version")
        git("add", "input.txt")
        path.write_text("original")
        self.same_receipt(first, self.send("c13", argv, env=env))
        git("add", "input.txt")
        git("-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-qm", "head only")
        self.send("c13", argv, code=5, env=env)
        self.assertEqual(self.entries(counter), ["entry"])

    def test_c14_private_exclusion_secret_equality_and_undeclared_negative(self):
        private = self.project / "private"
        private.mkdir()
        (private / "excluded").write_text("private first")
        (self.project / "ordinary").write_text("undeclared-fictitious-secret")
        argv, counter, ready, gate = self.counted()
        gate.touch()
        env = dict(self.env, FICTITIOUS_SECRET="declared-first")
        options = ("--private", "private", "--credential-env", "FICTITIOUS_SECRET")
        first = self.send("c14", argv, options, env=env)
        self.terminal("c14")
        (private / "excluded").write_text("private changed")
        self.same_receipt(first, self.send("c14", argv, options, env=env))
        changed = self.send("c14", argv, options, env=dict(env, FICTITIOUS_SECRET="declared-second"), code=5)
        serialized = json.dumps(changed)
        for value in ("declared-first", "declared-second", hashlib.sha256(b"declared-first").hexdigest()):
            self.assertNotIn(value, serialized)
        (self.project / "ordinary").write_text("undeclared-secret-changed")
        self.send("c14", argv, options, env=env, code=5)
        # Explicit negative: undeclared ordinary content is captured and read by the command.
        negative = self.send("c14-negative", [sys.executable, "-c", "print(open('ordinary').read())"],
                             options, env=env)
        final = self.terminal("c14-negative")["observation"]
        logs = [f for f in final["evidence"]["files"] if f["kind"] == "log" and f["path"].endswith("stdout")]
        self.assertEqual(Path(logs[0]["path"]).read_text(), "undeclared-secret-changed\n")
        self.assertNotIn("declared-first", json.dumps(final))
        secret_output = self.send("c14-stream-secret", [sys.executable, "-c",
                                  "import os,sys; print(os.environ['FICTITIOUS_SECRET']); print(sys.argv[1])",
                                  "declared-first"], options, env=env)
        secret_final = self.terminal("c14-stream-secret")["observation"]
        for entry in secret_final["evidence"]["files"]:
            self.assertNotIn(b"declared-first", Path(entry["path"]).read_bytes())
        self.assertNotIn("declared-first", json.dumps(secret_output))
        self.assertEqual(self.entries(counter), ["entry"])

    def test_c15_argv_environment_outputs_declarations_client_and_pwd(self):
        argv, counter, ready, gate = self.counted()
        gate.touch()
        env = dict(self.env, PWD="/fictitious/origin-one", VOLATILE="one")
        first = self.send("c15", argv, env=env)
        self.terminal("c15")
        self.same_receipt(first, self.send("c15", argv, env=dict(env, PWD="/fictitious/origin-two")))
        variants = [dict(env, VOLATILE="two"), dict(env, PATH=env.get("PATH", os.defpath) + ":/nonexistent"),
                    dict(env, EXTRA_EMPTY="")]
        for changed in variants:
            self.send("c15", argv, env=changed, code=5)
        self.send("c15", [*argv, "extra"], env=env, code=5)
        self.send("c15", argv, options=("--artifact", "report"), env=env, code=5)
        self.send("c15", argv, options=("--credential-env", "UNSET_DECLARATION"), env=env, code=5)
        self.raw("submit", "--client", "different", "--idempotency-key", "c15", "--", *argv, env=env, code=5)
        secret_env = dict(self.env, PWD="pwd-credential-one")
        self.send("c15-pwd", argv, options=("--credential-env", "PWD"), env=secret_env)
        self.terminal("c15-pwd")
        self.send("c15-pwd", argv, options=("--credential-env", "PWD"),
                  env=dict(secret_env, PWD="pwd-credential-two"), code=5)

    def test_c16_precommit_rejection_does_not_bind_key(self):
        self.send("initialize", [sys.executable, "-c", "pass"])
        self.terminal("initialize")
        rejected = self.send("c16", [sys.executable, "-c", "pass"], options=("--input", "../invalid"), code=2)
        self.assertEqual(rejected["key_binding"], "not_evaluated")
        absent = self.status_key("c16", code=2)
        self.assertEqual(absent["error"]["code"], "key_not_found")
        self.assertIn("as_of_revision", absent)
        accepted = self.send("c16", [sys.executable, "-c", "pass"])
        self.assertEqual(accepted["binding"], "new")
        self.terminal("c16")

    def test_c17_absence_is_revision_and_authority_scoped(self):
        self.send("initialized", [sys.executable, "-c", "pass"])
        self.terminal("initialized")
        absent = self.status_key("c17", code=2)
        self.assertEqual(absent["error"]["code"], "key_not_found")
        first = self.send("c17", [sys.executable, "-c", "pass"])
        self.assertGreater(first["admission"]["revision"], absent["as_of_revision"])
        other = self.root / "other-authority"
        self.status_key("c17", state=other, code=2)
        self.assertFalse(other.exists(), "reader must not initialize an authority")
        second = self.send("c17", [sys.executable, "-c", "pass"], state=other)
        self.assertNotEqual(first["authority_id"], second["authority_id"])
        self.terminal("c17")
        self.terminal("c17", state=other)

    def incomplete_job(self, key="incomplete"):
        argv, counter, ready, gate = self.counted()
        gate.touch()
        first = self.send(key, argv, options=("--artifact", "required/report.json"))
        final = self.terminal(key)
        self.assertEqual(final["observation"]["command"]["exit_code"], 0)
        self.assertEqual(final["observation"]["collection"]["state"], "incomplete")
        self.assertEqual(final["observation"]["reservation"]["disposition"], "retained")
        self.wait_key(key, 3)
        return first, final, argv, counter

    def test_c18_missing_artifact_signal_and_positive_never_created(self):
        first, final, argv, counter = self.incomplete_job("c18")
        self.raw("recover", code=3)
        self.same_receipt(first, self.send("c18", argv, options=("--artifact", "required/report.json")))
        other = self.root / "signal-state"
        self.send("signal", [sys.executable, "-c", "import os,signal; os.kill(os.getpid(),signal.SIGKILL)"], state=other)
        signalled = self.terminal("signal", state=other)["observation"]
        self.assertEqual(signalled["command"], {"kind": "signalled", "exit_code": None,
                                               "signal": signal.SIGKILL, "reason": None})
        self.wait_key("signal", 3, state=other)
        self.assertEqual(signalled["reservation"]["disposition"], "released")
        missing = self.send("missing", [str(self.root / "nonexistent-executable")], state=other)
        never = self.terminal("missing", state=other)["observation"]
        self.assertEqual(never["command"]["kind"], "never_created")
        self.assertEqual(never["closure"]["status"], "never_created")
        self.assertIs(never["evidence"]["valid"], True)
        self.assertEqual(never["evidence"]["files"], [])
        self.assertEqual(never["completion"], "complete")
        self.wait_key("missing", 1, state=other)
        self.assertEqual(self.entries(counter), ["entry"])

    def test_c19_post_completion_payload_loss_does_not_rewrite_history_or_reacquire(self):
        self.send("c19", [sys.executable, "-c", "open('report','w').write('result')"], options=("--artifact", "report"))
        final = self.terminal("c19")["observation"]
        self.wait_key("c19")
        artifact = next(f for f in final["evidence"]["files"] if f["kind"] == "artifact")
        path = Path(artifact["path"])
        retained = self.root / "original-public-artifact"
        retained.write_bytes(path.read_bytes())
        path.unlink()
        current = self.status_key("c19")["observation"]
        self.assertEqual(current["recorded_outcome"], "succeeded")
        self.assertEqual(current["outcome"], "incomplete")
        self.assertEqual(current["reservation"]["disposition"], "released")
        self.wait_key("c19", 3)
        self.raw("recover")
        self.send("c19-next", [sys.executable, "-c", "pass"])
        second = self.terminal("c19-next")["observation"]
        other_manifest = Path(second["evidence"]["manifest"])
        saved = self.root / "original-public-manifest"
        saved.write_bytes(other_manifest.read_bytes())
        other_manifest.write_bytes(Path(final["evidence"]["manifest"]).read_bytes())
        substituted = self.status_key("c19-next")["observation"]
        self.assertEqual(substituted["recorded_outcome"], "succeeded")
        self.assertEqual(substituted["outcome"], "incomplete")
        self.assertFalse(substituted["evidence"]["valid"])
        self.assertEqual(substituted["reservation"]["disposition"], "released")
        self.wait_key("c19-next", 3)

    def test_c20_real_quota_failure_at_terminal_collection(self):
        self.quota_case("c20")

    def test_c21_host_backend_rejected_without_claiming_positive_generation_coverage(self):
        result = self.send("c21", [sys.executable, "-c", "pass"], options=("--backend", "host-reservation@1"), code=2)
        self.assertEqual(result["error"]["code"], "backend_unsupported")
        self.assertEqual(result["key_binding"], "not_evaluated")
        self.assertFalse(self.state.exists())

    def test_c22_readers_and_replay_leave_original_authority_unchanged(self):
        argv, counter, ready, gate = self.counted()
        first = self.send("c22", argv)
        self.ready("c22", ready)
        before = self.inventory(self.state)
        original = self.status_key("c22")["observation"]["transitions"]
        alias = self.root / "state-alias"
        alias.symlink_to(self.state, target_is_directory=True)
        for temp in (self.state, alias):
            env = dict(self.env, TMPDIR=str(temp), TEMP=str(temp), TMP=str(temp))
            self.status_key("c22", env=env)
            self.raw("evidence", first["job"], "--attempt", first["attempt"], env=env)
            self.raw("status", "j_" + "f" * 32, env=env, code=2)
            self.wait_key("c22", 4, env=env)
        self.assertEqual(self.inventory(self.state), before)
        self.same_receipt(first, self.send("c22", argv))
        after = self.status_key("c22")["observation"]
        self.assertEqual(after["transitions"], original)
        self.assertEqual(self.entries(counter), ["entry"])
        # Replay may create/retire only its own preparation, never original payloads.
        for name, metadata in before.items():
            if name.startswith("attempts/") or name.startswith("capsules/") and name.count("/") > 1:
                self.assertEqual(self.inventory(self.state)[name], metadata)
        gate.touch()
        self.terminal("c22")
        lost, lost_argv, lost_counter = self.lost_owner("c22-uncertain")
        before_uncertain = self.inventory(self.state)
        original_transitions = self.status_key("c22-uncertain")["observation"]["transitions"]
        for temp in (self.state, alias):
            env = dict(self.env, TMPDIR=str(temp), TEMP=str(temp), TMP=str(temp))
            self.status_key("c22-uncertain", env=env)
            self.raw("evidence", lost["job"], "--attempt", lost["attempt"], env=env)
            self.wait_key("c22-uncertain", 3, env=env)
            self.raw("status", "j_" + "f" * 32, env=env, code=2)
        self.assertEqual(self.inventory(self.state), before_uncertain)
        self.same_receipt(lost, self.send("c22-uncertain", lost_argv))
        self.assertEqual(self.status_key("c22-uncertain")["observation"]["transitions"], original_transitions)
        self.assertEqual(self.entries(lost_counter), ["entry"])

    def test_c23_authentic_incompatible_cli_states_and_retention_preconditions(self):
        self.authentic_schema_case()
        history = os.environ.get("FLEET_TEST_AGED_AUTHORITY")
        aged_key = os.environ.get("FLEET_TEST_AGED_KEY")
        self.assertTrue(history and aged_key,
                        "BLOCKED C23 age: authentic >7-day owned authority/history is required; no clock or SQL fixture")
        state = Path(history).resolve(strict=True)
        self.assertEqual(state.stat().st_uid, os.getuid())
        value = self.status_key(aged_key, state=state)
        accepted = datetime.fromisoformat(value["admission"]["accepted_at"].replace("Z", "+00:00"))
        self.assertGreater((datetime.now(timezone.utc) - accepted).total_seconds(), 7 * 86400)
        self.assertIs(value["accepted"], True)

    def authentic_schema_case(self):
        from test_cli import CLI
        for schema in (2, 3):
            legacy = os.environ.get("FLEET_TEST_SCHEMA{}_CLI".format(schema))
            self.assertTrue(legacy, "BLOCKED C23: authentic schema{} CLI copy required".format(schema))
            self.assertTrue(Path(legacy).is_file())
            state = self.root / ("authentic-schema" + str(schema))
            record = {"case": "C23", "schema": schema, "state": str(state),
                      "closure_confirmed": False, "job": None, "attempt": None}
            self.legacy_admissions.append(record)
            self.external_evidence.append(record)
            argv, counter, ready, gate = self.counted("legacy-" + str(schema))
            result = subprocess.run([sys.executable, "-B", legacy, "--state-dir", str(state),
                                     "submit", "--", *argv], env=self.env,
                                    cwd=self.project, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
            value = json.loads(result.stdout)
            record.update(job=value["job"], attempt=value["attempt"])
            self.until(ready.exists)
            try:
                self.reject_legacy_snapshot(CLI, state, value, "active")
            finally:
                gate.touch()
            waited = subprocess.run([sys.executable, "-B", legacy, "--state-dir", str(state),
                                     "wait", value["job"], "--timeout", "10"], env=self.env,
                                    cwd=self.project, capture_output=True, text=True, timeout=15)
            self.assertEqual(waited.returncode, 0, (waited.stdout, waited.stderr))
            observation = json.loads(waited.stdout)
            supervisor = observation.get("supervisor")
            if supervisor:
                self.until(lambda: not self.process_present(supervisor["pid"]))
            if observation.get("child_pid"):
                self.until(lambda: not self.group_present(observation["child_pid"]))
            record["closure_confirmed"] = True
            self.reject_legacy_snapshot(CLI, state, value, "closed")
            self.assertEqual(self.entries(counter), ["legacy-" + str(schema)])
            self.external_evidence.append({"case": "C23", "schema": schema,
                                           "legacy_terminal": observation})

    def reject_legacy_snapshot(self, cli, state, value, phase):
        before = self.inventory(state)
        for args in (("status", value["job"]), ("status", "--idempotency-key", "none"),
                     ("wait", value["job"], "--timeout", "0"), ("evidence", value["job"]), ("recover",),
                     ("submit", "--idempotency-key", "key", "--", sys.executable, "-c", "pass"),
                     ("submit", "--", sys.executable, "-c", "pass")):
            response = subprocess.run([sys.executable, "-B", str(cli), "--state-dir", str(state), *args],
                                      env=self.env, cwd=self.project, capture_output=True, text=True, timeout=15)
            self.assertEqual(response.returncode, 2, (phase, response.stdout, response.stderr))
            self.assertEqual(json.loads(response.stdout)["error"]["code"], "unsupported_schema")
            self.assertEqual(self.inventory(state), before)
        self.external_evidence.append({"case": "C23", "phase": phase, "inventory": before})

    def process_present(self, pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    def test_c24_public_queued_and_running_crash_campaign_preserves_one_permission(self):
        argv, counter, ready, gate = self.counted()
        first = self.send("c24", argv, options=("--enqueue-only",))
        self.assertEqual(self.status_key("c24")["observation"]["state"], "queued")
        self.raw("recover")
        self.ready("c24", ready)
        self.raw("recover")
        self.same_receipt(first, self.send("c24", argv))
        gate.touch()
        self.terminal("c24")
        self.assertEqual(self.entries(counter), ["entry"])
        self.lost_owner("c24-lost")
        self.raw("recover", code=3)
        # Other exact claim/bind/gate/Popen/fsync windows remain explicitly unvalidated.
