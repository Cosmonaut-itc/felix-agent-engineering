import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading


class ContentCases:
    def output(self, final):
        return Path(final["stdout_log"]).read_text()

    def artifact_job(self, script, artifacts=("report.txt",), env=None):
        args = ["submit"]
        for artifact in artifacts:
            args += ["--artifact", artifact]
        return self.value(*args, "--", sys.executable, "-B", "-c", script, env=env)

    def artifacts(self, job):
        value = self.value("evidence", job)
        return value, [f for f in value["evidence"]["files"] if f["kind"] == "artifact"]

    def git(self, *args):
        result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                                 "-C", str(self.project), *args], env=self.env,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        return result.stdout.strip()

    def initialize_git(self):
        self.git("init", "-q")
        self.git("config", "user.name", "Fleet fixture")
        self.git("config", "user.email", "fleet@example.invalid")
        self.git("config", "core.filemode", "true")

    def test_no_git_relative_script_is_captured_before_source_changes(self):
        script = self.project / "check.py"
        script.write_text("print('captured-before-submit')\n")
        argv, ready, gate, _ = self.barrier()
        blocker = self.submit(argv)
        self.running(blocker["job"], ready)
        captured = self.submit([sys.executable, "check.py"])
        self.assertEqual(captured["state"], "queued")
        self.assertIsNone(captured["input"]["commit_sha"])
        script.write_text("print('changed-after-submit')\n")
        shutil.rmtree(self.project)
        self.project.mkdir()
        gate.touch()
        self.wait(blocker["job"])
        final = self.wait(captured["job"])
        self.assertEqual(self.output(final), "captured-before-submit\n")
        self.assertEqual(final["input"], captured["input"])
        self.assertTrue(final["evidence"]["valid"])
        self.assertNotIn("argv", final)
        self.assertNotIn("environment", final)

    def test_head_index_worktree_untracked_and_executable_identity(self):
        self.initialize_git()
        script = self.project / "check.sh"
        script.write_text("#!/bin/sh\nprintf 'head\\n'\n")
        script.chmod(0o755)
        (self.project / ".gitignore").write_text("ignored.txt\n")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        head = self.git("rev-parse", "HEAD")
        script.write_text("#!/bin/sh\nprintf 'index\\n'\n")
        self.git("add", "check.sh")
        script.write_text("#!/bin/sh\nprintf 'working\\n'; cat untracked.txt\n")
        (self.project / "untracked.txt").write_text("untracked\n")
        (self.project / "ignored.txt").write_text("ignored\n")
        first = self.submit(["./check.sh"])
        final = self.wait(first["job"])
        self.assertEqual(self.output(final), "working\nuntracked\n")
        self.assertEqual(first["input"]["commit_sha"], head)
        (self.project / "untracked.txt").write_text("new-untracked\n")
        second = self.submit(["./check.sh"])
        self.assertEqual(self.output(self.wait(second["job"])), "working\nnew-untracked\n")
        self.assertNotEqual(first["input"]["effective_sha256"], second["input"]["effective_sha256"])
        self.assertEqual(first["input"]["commit_sha"], second["input"]["commit_sha"])
        script.chmod(0o644)
        third = self.submit(["sh", "check.sh"])
        self.wait(third["job"])
        self.assertNotEqual(second["input"]["effective_sha256"], third["input"]["effective_sha256"])
        script.chmod(0o600)
        os.utime(script, (123456, 123456))
        normalized = self.submit(["sh", "check.sh"])
        self.wait(normalized["job"])
        self.assertEqual(third["input"], normalized["input"])
        inspect = self.submit([sys.executable, "-c", "import pathlib; print(pathlib.Path('ignored.txt').exists())"])
        self.assertEqual(self.output(self.wait(inspect["job"])), "False\n")
        result = self.call("submit", "--input", "ignored.txt", "--", sys.executable, "-c", "pass",
                           state=self.root / "ignored-state", check=False, refute_if_absent=True)
        self.assertEqual(result.returncode, 2)
        self.assertTrue(self.admissions[-1].refuted)

    def test_default_and_consumer_private_paths_do_not_affect_inputs(self):
        (self.project / ".env").write_text("fictitious-default-private-a")
        custom = self.project / "ordinary-looking.data"
        custom.write_text("fictitious-consumer-private-a")
        script = ("import pathlib; print(pathlib.Path('.env').exists()); "
                  "print(pathlib.Path('ordinary-looking.data').exists()); print(pathlib.Path('public.txt').read_text())")
        (self.project / "public.txt").write_text("public-input")
        first = self.value("submit", "--private", custom.name, "--", sys.executable, "-c", script)
        self.assertEqual(self.output(self.wait(first["job"])), "False\nFalse\npublic-input\n")
        (self.project / ".env").write_text("fictitious-default-private-b")
        custom.write_text("fictitious-consumer-private-b")
        second = self.value("submit", "--private", custom.name, "--", sys.executable, "-c", script)
        self.wait(second["job"])
        self.assertEqual(first["input"], second["input"])
        policy = second["input_policy"]
        self.assertIn(".env", policy["default_private"])
        self.assertEqual(policy["consumer_private"], [custom.name])
        self.assertIn("not inspected", policy["limit"])
        self.assertIn("private", policy["redaction"])

    def test_declared_argv_environment_and_split_stream_secrets_are_redacted(self):
        secret = "fictitious-declared-credential-3821" * 300
        env = dict(self.env, TEST_CREDENTIAL=secret)
        script = ("import os,sys,pathlib; s=sys.argv[1].encode(); "
                  "print(pathlib.Path('normal.txt').read_text(),flush=True); "
                  "print(any(s in p.read_bytes() for p in pathlib.Path('.').rglob('*') if p.is_file()),flush=True); "
                  "print(os.environ['TEST_CREDENTIAL'],flush=True); "
                  "os.write(1,s[:9]); os.write(1,s[9:]); "
                  "os.write(2,s[:5]); os.write(2,s[5:])")
        (self.project / "normal.txt").write_text("admitted")
        submitted = self.value("submit", "--credential-env", "TEST_CREDENTIAL", "--",
                               sys.executable, "-c", script, secret, env=env)
        final = self.wait(submitted["job"])
        for text in (json.dumps(submitted), json.dumps(final), self.output(final),
                     Path(final["stderr_log"]).read_text(), (self.state / "supervisor.log").read_text()):
            self.assertNotIn(secret, text)
            self.assertNotIn(hashlib.sha256(secret.encode()).hexdigest(), text)
        self.assertEqual(self.output(final), "admitted\nFalse\n[REDACTED]\n[REDACTED]")
        self.assertEqual(Path(final["stderr_log"]).read_text(), "[REDACTED]")
        check = self.submit([sys.executable, "-c", "import pathlib; print(pathlib.Path('normal.txt').read_text())"])
        self.assertEqual(self.output(self.wait(check["job"])), "admitted\n")

    def test_declared_credentials_in_staged_inputs_are_rejected_without_leaking(self):
        secret = "fictitious-known-staged-credential-3821"
        self.initialize_git()
        (self.project / "safe.txt").write_text("safe")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        (self.project / "config.txt").write_text(secret)
        self.git("add", "config.txt")
        result = self.call("submit", "--", sys.executable, "-c", "pass", check=False,
                           env=dict(self.env, SERVICE_TOKEN=secret), refute_if_absent=True)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertIn("credential", json.loads(result.stdout)["error"])
        self.assertTrue(self.admissions[-1].refuted)

    def test_undeclared_secret_in_normal_file_is_outside_the_guarantee(self):
        secret = "fictitious-UNDECLARED-file-secret-3821"
        (self.project / "ordinary.txt").write_text(secret)
        submitted = self.submit([sys.executable, "-c", "import pathlib; print(pathlib.Path('ordinary.txt').read_text())"])
        final = self.wait(submitted["job"])
        self.assertEqual(self.output(final), secret + "\n")
        self.assertIn("undeclared secrets", final["input_policy"]["limit"])

    def test_tracked_script_preserves_literal_metacharacter_argument(self):
        self.initialize_git()
        (self.project / "argv.py").write_text("import sys\nprint(sys.argv[1])\n")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        literal = "$(touch accidental); && | * ' \""
        submitted = self.submit([sys.executable, "argv.py", literal])
        self.assertEqual(self.output(self.wait(submitted["job"])), literal + "\n")
        self.assertFalse((self.project / "accidental").exists())

    def test_artifacts_valid_identical_attempts_and_missing(self):
        finals = []
        for _ in range(2):
            submitted = self.artifact_job("from pathlib import Path; Path('report.txt').write_text('report')")
            finals.append(self.wait(submitted["job"]))
            proof, files = self.artifacts(submitted["job"])
            self.assertTrue(proof["evidence"]["valid"])
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0]["size"], 6)
            self.assertEqual(files[0]["sha256"], hashlib.sha256(b"report").hexdigest())
            self.assertEqual(Path(files[0]["path"]).read_text(), "report")
            self.assertNotIn("contents", proof)
        self.assertNotEqual(finals[0]["attempt"], finals[1]["attempt"])
        missing = self.artifact_job("pass")
        final = self.wait(missing["job"], 3)
        self.assertEqual(final["command"], {"kind": "exited", "exit_code": 0})
        self.assertEqual(final["outcome"], "incomplete")
        self.assertIn("artifact_collection_failed", final["evidence"]["errors"])

    def test_preexisting_artifact_is_not_deleted_or_admitted(self):
        report = self.project / "report.txt"
        report.write_text("old-report")
        result = self.call("submit", "--artifact", "report.txt", "--", sys.executable, "-c", "pass",
                           check=False, refute_if_absent=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(report.read_text(), "old-report")
        self.assertTrue(self.admissions[-1].refuted)

    def test_artifact_collection_rejects_special_symlink_and_declared_secret(self):
        outside = self.root / "own-external-report"
        outside.write_text("outside")
        for script in ("from pathlib import Path; Path('report.txt').mkdir()",
                       "import os; os.mkfifo('report.txt')",
                       f"import os; os.symlink({str(outside)!r},'report.txt')"):
            submitted = self.artifact_job(script)
            final = self.wait(submitted["job"], 3)
            self.assertEqual(final["command"]["exit_code"], 0)
            self.assertFalse(final["evidence"]["valid"])
        secret = "fictitious-artifact-credential-3821"
        submitted = self.artifact_job(
            "import os,pathlib; pathlib.Path('report.txt').write_text(os.environ['ARTIFACT_TOKEN'])",
            env=dict(self.env, ARTIFACT_TOKEN=secret))
        final = self.wait(submitted["job"], 3)
        proof, files = self.artifacts(submitted["job"])
        self.assertEqual(files, [])
        self.assertNotIn(secret, json.dumps(proof) + json.dumps(final))
        self.assertEqual(outside.read_text(), "outside")

    def test_evidence_detects_truncation_replacement_and_foreign_manifest(self):
        first = self.artifact_job("from pathlib import Path; Path('report.txt').write_text('same')")
        original = self.wait(first["job"])
        second = self.artifact_job("from pathlib import Path; Path('report.txt').write_text('same')")
        other = self.wait(second["job"])
        proof, files = self.artifacts(first["job"])
        report = Path(files[0]["path"])
        report.write_text("sa")
        degraded = self.wait(first["job"], 3)
        self.assertEqual(degraded["recorded_outcome"], "succeeded")
        self.assertEqual(degraded["transitions"], original["transitions"])
        _, files = self.artifacts(second["job"])
        report = Path(files[0]["path"])
        replacement = self.root / "replacement"
        replacement.write_text("same")
        os.replace(replacement, report)
        self.wait(second["job"], 3)
        third = self.artifact_job("from pathlib import Path; Path('report.txt').write_text('same')")
        third_final = self.wait(third["job"])
        manifest = Path(third_final["evidence"]["manifest"])
        manifest.write_bytes(Path(other["evidence"]["manifest"]).read_bytes())
        self.wait(third["job"], 3)
        self.assertFalse(self.value("evidence", third["job"])["evidence"]["valid"])

    def test_missing_manifest_and_log_bytes_never_accredit_success(self):
        submitted = self.submit([sys.executable, "-c", "print('complete')"])
        final = self.wait(submitted["job"])
        Path(final["stdout_log"]).write_text("truncated")
        self.wait(submitted["job"], 3)
        another = self.submit([sys.executable, "-c", "pass"])
        final = self.wait(another["job"])
        Path(final["evidence"]["manifest"]).unlink()
        self.wait(another["job"], 3)

    def test_simultaneous_large_streams_are_complete(self):
        script = ("import os,threading\n"
                  "def emit(fd,byte):\n"
                  "    for _ in range(64):\n"
                  "        os.write(fd,byte*16384)\n"
                  "a=threading.Thread(target=emit,args=(1,b'O'))\n"
                  "b=threading.Thread(target=emit,args=(2,b'E'))\n"
                  "a.start(); b.start(); a.join(); b.join()\n")
        submitted = self.submit([sys.executable, "-c", script])
        final = self.wait(submitted["job"])
        self.assertEqual(Path(final["stdout_log"]).read_bytes(), b"O" * 1048576)
        self.assertEqual(Path(final["stderr_log"]).read_bytes(), b"E" * 1048576)

    def test_public_file_size_limit_makes_stream_write_failure_incomplete(self):
        from test_cli import CLI
        response = self.root / "limited-submit.json"
        admission = self.admit(self.state)
        wrapper = ("import os,resource,sys; "
                   "resource.setrlimit(resource.RLIMIT_FSIZE,(2097152,2097152)); "
                   "os.execv(sys.executable,[sys.executable,'-B',*sys.argv[1:]])")
        script = "import os; chunk=b'X'*16384\nfor _ in range(192): os.write(1,chunk)\n"
        with response.open("w") as output:
            process = subprocess.run([sys.executable, "-c", wrapper, str(CLI), "--state-dir", str(self.state),
                                      "submit", "--", sys.executable, "-c", script],
                                     env=self.env, cwd=self.project, stdout=output,
                                     stderr=subprocess.PIPE, text=True, timeout=15)
        value = json.loads(response.read_text())
        admission.attribute(value)
        self.assertEqual(process.returncode, 0, process.stderr)
        final = self.wait(value["job"], 3)
        self.assertEqual(final["command"], {"kind": "exited", "exit_code": 0})
        self.assertIn("stream_write_failed", final["evidence"]["errors"])

    def test_first_admissions_on_nonexistent_state_are_serialized(self):
        start = threading.Barrier(4)
        argv, ready, gate, _ = self.barrier()

        def send(index):
            start.wait(timeout=5)
            return self.submit(argv, client="first-" + str(index))

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            accepted = list(pool.map(send, range(4)))
        self.assertEqual(len({v["job"] for v in accepted}), 4)
        views = [self.value("status", v["job"]) for v in accepted]
        ordered = sorted(views, key=lambda v: v["seq"])
        self.assertTrue(all(v["state"] == "queued" for v in ordered[1:]))
        gate.touch()
        finals = [self.wait(v["job"]) for v in ordered]
        for left, right in zip(finals, finals[1:]):
            self.assertLessEqual(left["ended_at"], right["started_at"])
            self.assertLess(left["transitions"][-1]["seq"], right["transitions"][-2]["seq"])

    def test_reads_do_not_bootstrap_repair_or_release(self):
        absent = self.root / "absent-state"
        for operation in ("status", "wait", "evidence"):
            args = (operation, "unknown") + (("--timeout", "0") if operation == "wait" else ())
            result = self.call(*args, state=absent, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(absent.exists())
        submitted = self.submit([sys.executable, "-c", "print('valid')"])
        final = self.wait(submitted["job"])
        self.settle()
        before = {p.relative_to(self.state): (p.lstat().st_mode, p.lstat().st_size,
                  p.lstat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
                  for p in self.state.rglob("*")}
        for operation in ("status", "wait", "evidence"):
            self.value(operation, submitted["job"])
        after = {p.relative_to(self.state): (p.lstat().st_mode, p.lstat().st_size,
                 p.lstat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
                 for p in self.state.rglob("*")}
        self.assertEqual(before, after)
        self.assertEqual(self.value("status", submitted["job"])["transitions"], final["transitions"])

    def test_invalid_inputs_paths_and_source_references_have_no_admission(self):
        (self.project / "safe.txt").write_text("safe")
        (self.project / ".env").write_text("private")
        (self.project / "custom-private").write_text("private")
        options = [("--input", ".env"), ("--private", "custom-private", "--input", "custom-private"),
                   ("--input", "../escape"), ("--artifact", "/absolute"),
                   ("--artifact", "safe.txt"), ("--input", "missing"),
                   ("--artifact", "Out", "--artifact", "out"),
                   ("--artifact", "x", "--artifact", "x/y")]
        for index, args in enumerate(options):
            state = self.root / ("invalid-" + str(index))
            result = self.call("submit", *args, "--", sys.executable, "-c", "pass",
                               state=state, check=False, refute_if_absent=True)
            self.assertEqual(result.returncode, 2)
            self.assertTrue(self.admissions[-1].refuted)
        link = self.project / "link"
        link.symlink_to(self.project / "safe.txt")
        result = self.call("submit", "--", sys.executable, "-c", "pass",
                           check=False, refute_if_absent=True)
        self.assertEqual(result.returncode, 2)
        self.assertTrue(self.admissions[-1].refuted)
        link.unlink()
        os.mkfifo(self.project / "fifo")
        result = self.call("submit", "--", sys.executable, "-c", "pass",
                           check=False, refute_if_absent=True)
        self.assertEqual(result.returncode, 2)
        self.assertTrue(self.admissions[-1].refuted)
        (self.project / "fifo").unlink()
        result = self.call("submit", "--", sys.executable, str(self.project / "safe.txt"),
                           check=False, refute_if_absent=True)
        self.assertEqual(result.returncode, 2)
        self.assertTrue(self.admissions[-1].refuted)

    def test_narrow_inputs_exclude_unselected_files_and_copy_hardlinks(self):
        code = self.project / "run.py"
        code.write_text("import pathlib; print(pathlib.Path('data').read_text()); print(pathlib.Path('other').exists())")
        (self.project / "data").write_text("copied-bytes")
        (self.project / "other").write_text("not-admitted")
        external = self.root / "hardlink-source"
        external.write_text("copied-bytes")
        (self.project / "data").unlink()
        os.link(external, self.project / "data")
        argv, ready, gate, _ = self.barrier()
        first = self.submit(argv)
        self.running(first["job"], ready)
        next_job = self.value("submit", "--input", "run.py", "--input", "data", "--",
                              sys.executable, "run.py")
        external.write_text("changed-hardlink")
        gate.touch()
        self.wait(first["job"])
        self.assertEqual(self.output(self.wait(next_job["job"])), "copied-bytes\nFalse\n")

    def test_git_subdirectory_cwd_and_artifact_are_relative_to_materialized_cwd(self):
        self.initialize_git()
        package = self.project / "package"
        package.mkdir()
        (package / "run.py").write_text(
            "from pathlib import Path\nprint(Path('data').read_text())\nPath('report.txt').write_text('artifact')\n")
        (package / "data").write_text("subdirectory-input")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        submitted = self.value("submit", "--cwd", str(package), "--artifact", "report.txt", "--",
                               sys.executable, "run.py")
        final = self.wait(submitted["job"])
        self.assertEqual(final["input"]["cwd_rel"], "package")
        self.assertEqual(self.output(final), "subdirectory-input\n")
        proof, files = self.artifacts(submitted["job"])
        self.assertTrue(proof["evidence"]["valid"])
        self.assertEqual(Path(files[0]["path"]).read_text(), "artifact")
        self.assertFalse((package / "report.txt").exists())

    def test_symlink_components_and_source_executable_from_path_are_rejected(self):
        directory = self.root / "own-directory"
        directory.mkdir()
        link = self.root / "directory-link"
        link.symlink_to(directory, target_is_directory=True)
        for state, options in ((self.root / "bad-cwd-state", ("--cwd", str(link))),
                               (link / "bad-state", ())):
            result = self.call("submit", *options, "--", sys.executable, "-c", "pass", state=state,
                               check=False, refute_if_absent=True)
            self.assertEqual(result.returncode, 2)
            self.assertTrue(self.admissions[-1].refuted)
        tool = self.project / "source-tool"
        tool.write_text("#!/bin/sh\nexit 0\n")
        tool.chmod(0o755)
        result = self.call("submit", "--", tool.name, env=dict(self.env, PATH=str(self.project)),
                           check=False, refute_if_absent=True)
        self.assertEqual(result.returncode, 2)
        self.assertTrue(self.admissions[-1].refuted)

    def test_submodule_is_rejected_without_admission(self):
        self.initialize_git()
        (self.project / "normal").write_text("normal")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        head = self.git("rev-parse", "HEAD")
        self.git("update-index", "--add", "--cacheinfo", "160000," + head + ",submodule")
        result = self.call("submit", "--", sys.executable, "-c", "pass",
                           check=False, refute_if_absent=True)
        self.assertEqual(result.returncode, 2)
        self.assertTrue(self.admissions[-1].refuted)
