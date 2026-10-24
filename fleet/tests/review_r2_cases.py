import errno
import json
import os
import re
import socket
import subprocess
import sys
import time


class ReviewR2Cases:
    def physical_case_alias(self, path):
        alias = path.with_name(path.name.swapcase())
        self.assertNotEqual(path, alias)
        self.assertTrue(alias.exists(), "requires an existing case-insensitive fixture volume; no skip")
        self.assertEqual((path.stat().st_dev, path.stat().st_ino),
                         (alias.stat().st_dev, alias.stat().st_ino))
        self.assertTrue(os.path.samefile(path, alias))
        return alias

    def test_r2_private_case_aliases_exclude_files_directories_bytes_and_fingerprint(self):
        directory = self.project / "PrivateArea"
        directory.mkdir()
        nested = directory / "access.data"
        private_file = self.project / "PrivateFile.data"
        private_paths = (directory, private_file)
        for path in (nested, private_file):
            path.write_text("fictitious-private-before-3821")
        aliases = [self.physical_case_alias(path) for path in private_paths]
        script = ("import pathlib; print(pathlib.Path('PrivateArea').exists()); "
                  "print(pathlib.Path('PrivateFile.data').exists())")
        options = [item for alias in aliases for item in ("--private", alias.name)]
        identities = []
        for marker in ("fictitious-private-before-3821", "fictitious-private-after-3821"):
            for path in (nested, private_file):
                path.write_text(marker)
            submitted = self.value("submit", *options, "--", sys.executable, "-c", script)
            final = self.wait(submitted["job"])
            identities.append(submitted["input"])
            self.assertEqual(self.output(final), "False\nFalse\n")
            for tree in (self.state / "capsules" / submitted["job"] / "payload",
                         self.state / "attempts" / submitted["attempt"] / "workspace"):
                for path in private_paths:
                    self.assertFalse((tree / path.name).exists())
                for path in tree.rglob("*"):
                    if path.is_file():
                        self.assertNotIn(marker.encode(), path.read_bytes())
            manifest = self.state / "capsules" / submitted["job"] / "manifest.json"
            self.assertNotIn(marker.encode(), manifest.read_bytes())
        self.assertEqual(*identities)

    def test_r2_private_case_sensitive_distinct_objects_remain_public(self):
        private_dir, public_dir = self.project / "PrivateArea", self.project / "privatearea"
        private_file, public_file = self.project / "PrivateFile.data", self.project / "privatefile.data"
        private_dir.mkdir()
        self.assertFalse(public_dir.exists(), "requires a case-sensitive fixture volume; no skip")
        public_dir.mkdir()
        for path in (private_dir / "data", private_file):
            path.write_text("private-before")
        for path in (public_dir / "data", public_file):
            path.write_text("public-distinct")
        for left, right in ((private_dir, public_dir), (private_file, public_file)):
            self.assertNotEqual((left.stat().st_dev, left.stat().st_ino),
                                (right.stat().st_dev, right.stat().st_ino))
        script = ("import pathlib; print(pathlib.Path('privatearea/data').read_text()); "
                  "print(pathlib.Path('privatefile.data').read_text()); "
                  "print(pathlib.Path('PrivateArea').exists()); print(pathlib.Path('PrivateFile.data').exists())")
        options = ("submit", "--private", private_dir.name, "--private", private_file.name,
                   "--", sys.executable, "-c", script)
        first = self.value(*options)
        self.assertEqual(self.output(self.wait(first["job"])), "public-distinct\npublic-distinct\nFalse\nFalse\n")
        for path in (private_dir / "data", private_file):
            path.write_text("private-after")
        second = self.value(*options)
        self.wait(second["job"])
        self.assertEqual(first["input"], second["input"])
        public_file.write_text("public-mutated")
        third = self.value(*options)
        self.assertIn("public-mutated", self.output(self.wait(third["job"])))
        self.assertNotEqual(second["input"]["effective_sha256"], third["input"]["effective_sha256"])

    def test_r2_colon_argv_checks_noninitial_physical_source_alias(self):
        tools = self.project / "tools"
        tools.mkdir()
        script = tools / "fixture-tool"
        script.write_text("#!/bin/sh\necho mutable-source\n")
        script.chmod(0o755)
        alias = self.physical_case_alias(self.project)
        self.assertTrue(os.path.samefile(tools, alias / "tools"))
        self.rejected_submission(argv=["/usr/bin/env", "PATH=/usr/bin:" + str(alias / "tools"),
                                       "fixture-tool"], error="source_reference_in_command")

    def test_r2_colon_content_checks_noninitial_physical_source_alias(self):
        alias = self.physical_case_alias(self.project)
        (self.project / "run.py").write_text("paths = '/opt/external:" + str(alias / "lib") + "'\n")
        self.rejected_submission(argv=[sys.executable, "run.py"], error="source_reference_in_content")

    def test_r2_whole_argv_paths_have_length_and_depth_limits_before_resolution(self):
        for literal in ("/" + "Q" * 18000, "/" + "x/" * 129):
            self.rejected_submission(argv=[sys.executable, "-c", "pass", literal],
                                     error="source_reference_limit")
        literal = "/" + "Q" * 18000 + ":" + str(self.project / "source")
        self.rejected_submission(argv=[sys.executable, "-c", "pass", literal],
                                 error="source_reference_in_command")

    def test_r2_unobservable_loop_identity_rejects_argv_and_content(self):
        loop = self.root / "own-loop"
        loop.symlink_to(loop.name)
        with self.assertRaises(OSError) as failure:
            (loop / "source").stat()
        self.assertEqual(failure.exception.errno, errno.ELOOP)
        self.rejected_submission(argv=[sys.executable, "-c", "pass", str(loop / "source")],
                                 error="path_identity_unavailable")
        (self.project / "reference.txt").write_text(str(loop / "source"))
        self.rejected_submission(error="path_identity_unavailable")

    def test_r2_unobservable_permission_identity_rejects_hidden_source_alias(self):
        denied = self.root / "own-denied"
        denied.mkdir()
        alias = denied / "source"
        alias.symlink_to(self.project, target_is_directory=True)
        denied.chmod(0)
        try:
            with self.assertRaises(OSError) as failure:
                alias.stat()
            self.assertIn(failure.exception.errno, (errno.EACCES, errno.EPERM),
                          "requires an unprivileged uid with effective directory permission checks")
            self.rejected_submission(argv=[sys.executable, "-c", "pass", str(alias / "data")],
                                     error="path_identity_unavailable")
            self.rejected_submission(env=dict(self.env, PYTHONPATH=str(alias)),
                                     error="path_identity_unavailable")
        finally:
            denied.chmod(0o700)

    def test_r2_handoff_actor_uses_destination_job_secrets(self):
        from test_cli import CLI
        host = socket.gethostname()
        self.assertNotIn(host, str(self.root), "fixture path must not contain the declared hostname")
        seed = self.submit([sys.executable, "-c", "pass"])
        self.wait(seed["job"])
        self.settle()
        capsules = self.state / "capsules"
        baseline = {path.name for path in capsules.iterdir()}
        log = self.state / "supervisor.log"
        log.unlink()
        os.mkfifo(log, 0o600)
        admission = self.admit(self.state)
        process = None
        reader = None
        candidate = None
        try:
            process = subprocess.Popen([sys.executable, "-B", str(CLI), "--state-dir", str(self.state),
                                        "submit", "--", sys.executable, "-c", "print('must-not-run')"],
                                       env=self.env, cwd=self.project, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True)
            self.processes.append(process)

            def registered_starting():
                nonlocal candidate
                names = {path.name for path in capsules.iterdir()} - baseline
                self.assertLessEqual(len(names), 1, "ambiguous capsule candidate; admission remains pending")
                if not names:
                    return False
                name = next(iter(names))
                self.assertTrue(re.fullmatch(r"j_[0-9a-f]{32}", name))
                result = self.call("status", name, check=False)
                if result.returncode != 0:
                    return False
                value = json.loads(result.stdout)
                if value.get("registered") and value["state"] == "starting" and value["supervisor"] is None:
                    self.assertEqual(value["job"], name)
                    candidate = value
                    return True
                return False

            self.until(registered_starting)
            queued = self.call("submit", "--client", "fixture-client", "--credential-env", "HOST_VALUE",
                               "--", sys.executable, "-c", "print('must-not-run')",
                               env=dict(self.env, HOST_VALUE=host), check=False)
            self.assertEqual(queued.returncode, 3, (queued.stdout, queued.stderr))
            second = json.loads(queued.stdout)
            self.assertTrue(second["registered"] and second["accepted"])
            self.assertEqual((second["observation"], second["state"], second["view"]),
                             ("durable_admission", "queued", "uncertain"))
            reader = os.open(log, os.O_RDONLY | os.O_NONBLOCK)
            stdout, stderr = process.communicate(timeout=15)
            first = json.loads(stdout)
            admission.attribute(first)
            self.assertEqual(process.returncode, 1, (stdout, stderr))
            self.assertEqual((first["job"], first["attempt"]), (candidate["job"], candidate["attempt"]))
            final = self.wait(second["job"], 1)
            status = self.value("status", second["job"], env=dict(self.env, HOST_VALUE="different-reader"))
            proof = self.value("evidence", second["job"], env=dict(self.env, HOST_VALUE="different-reader"))
            self.assertEqual(final["command"]["kind"], "never_created")
            self.assertTrue(proof["evidence"]["valid"])
            self.assertEqual([item["to"] for item in status["transitions"]], ["queued", "starting", "finished"])
            expected_actor = "client:" + first["client"].replace(host, "[REDACTED]")
            self.assertIn(host, first["client"], "source emitter must not declare the destination secret")
            for transition in status["transitions"][1:]:
                self.assertEqual((transition["job"], transition["attempt"], transition["actor"]),
                                 (second["job"], second["attempt"], expected_actor))
            for response in (second, final, status, proof):
                self.assertNotIn(host, json.dumps(response))
        finally:
            if process is not None:
                if reader is None:
                    reader = os.open(log, os.O_RDONLY | os.O_NONBLOCK)
                try:
                    stdout, stderr = process.communicate(timeout=15)
                    if admission.job is None:
                        admission.attribute(json.loads(stdout))
                    if candidate is not None:
                        self.assertEqual(admission.job, candidate["job"])
                finally:
                    os.close(reader)
                if admission.job is not None and process.returncode is not None:
                    self.settle()
                    log.unlink()
                    log.write_bytes(b"")
                    log.chmod(0o600)
        self.assert_publication_excludes_secret(host, second, final, status, proof)

    def bounded_cost_rejection(self, data):
        from test_cli import CLI
        limit = 256 * 1024 * 1024
        probe = ("import json,resource,sys\n"
                 "limit=int(sys.argv[1]); capped=sys.argv[2]=='capped'\n"
                 "if capped: resource.setrlimit(resource.RLIMIT_AS,(limit,limit))\n"
                 "blocks=[]; failed=False\n"
                 "try:\n"
                 "    for _ in range(320): blocks.append(bytearray(1024*1024))\n"
                 "except MemoryError: failed=True\n"
                 "print(json.dumps({'allocated':len(blocks)*1024*1024,'failed':failed,"
                 "'limits':resource.getrlimit(resource.RLIMIT_AS)}),flush=True)\n")
        reports = {}
        for mode in ("control", "capped"):
            result = subprocess.run([sys.executable, "-I", "-B", "-c", probe, str(limit), mode],
                                    env=self.env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, "memory-cap calibration failed: " + result.stderr)
            reports[mode] = json.loads(result.stdout)
        self.assertFalse(reports["control"]["failed"], reports)
        self.assertEqual(reports["control"]["allocated"], 320 * 1024 * 1024)
        self.assertTrue(reports["capped"]["failed"], "RLIMIT_AS is not effective; scanner was not launched")
        self.assertLess(reports["capped"]["allocated"], limit, reports)
        self.assertEqual(reports["capped"]["limits"], [limit, limit], reports)
        print("effective cost-fixture cap: " + json.dumps(reports), flush=True)
        (self.project / "cost.data").write_bytes(data)
        state = self.root / "cost-rejected"
        admission = self.admit(state)
        wrapper = ("import os,resource,sys\n"
                   "limit=int(sys.argv[1]); resource.setrlimit(resource.RLIMIT_AS,(limit,limit))\n"
                   "if resource.getrlimit(resource.RLIMIT_AS)!=(limit,limit): sys.exit(91)\n"
                   "os.execv(sys.executable,[sys.executable,'-B',*sys.argv[2:]])\n")
        started = time.monotonic()
        result = subprocess.run([sys.executable, "-I", "-B", "-c", wrapper, str(limit), str(CLI),
                                 "--state-dir", str(state), "submit", "--", sys.executable, "-c", "pass"],
                                env=self.env, cwd=self.project, capture_output=True, text=True, timeout=15)
        value = json.loads(result.stdout)
        if "job" in value:
            admission.attribute(value)
        else:
            admission.refute_if_absent(result)
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(value, {"error": "source_reference_limit"})
        self.assertTrue(admission.refuted)

    def test_r2_cost_slash_heavy_4mib_is_rejected_before_physical_resolution(self):
        self.bounded_cost_rejection(b"/Q" * (2 * 1024 * 1024))

    def test_r2_cost_base64_4mib_is_rejected_before_physical_resolution(self):
        self.bounded_cost_rejection((b"/" + b"A" * 63) * 65536)

    def test_r2_cost_binary_12mib_has_explicit_candidate_limit_rejection(self):
        self.bounded_cost_rejection(b"/Q\x00\xff" * (3 * 1024 * 1024))
