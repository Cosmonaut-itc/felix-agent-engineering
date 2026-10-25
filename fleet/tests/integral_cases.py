import json
import os
from pathlib import Path
import pwd
import socket
import subprocess
import sys


class IntegralCases:
    def case_alias(self, path):
        alias = path.with_name(path.name.swapcase())
        self.assertNotEqual(alias, path)
        if not alias.exists():
            self.skipTest("fixture volume has no case alias; Mini requires a case-insensitive volume")
        self.assertTrue(os.path.samefile(path, alias), "case variant is a distinct object")
        self.assertEqual((path.stat().st_dev, path.stat().st_ino),
                         (alias.stat().st_dev, alias.stat().st_ino))
        return alias

    def test_case_alias_absolute_argv_cannot_execute_source(self):
        script = self.project / "run.sh"
        script.write_text("#!/bin/sh\necho mutable-source\n")
        script.chmod(0o755)
        alias = self.case_alias(self.project)
        self.assertTrue(os.path.samefile(script, alias / script.name))
        for argv in ([str(alias / script.name)], ["/bin/sh", str(alias / script.name)],
                     [sys.executable, "-c", "pass", "--file=" + str(alias / script.name)]):
            self.rejected_submission(argv=argv, error="source_reference_in_command")

    def test_case_alias_literal_source_reference_in_content_is_rejected(self):
        alias = self.case_alias(self.project)
        (self.project / "run.py").write_text(
            "from pathlib import Path\nprint(Path(" + repr(str(alias / "data")) + ").read_text())\n")
        (self.project / "data").write_text("mutable-source")
        self.rejected_submission(argv=[sys.executable, "run.py"], error="source_reference_in_content")

    def test_literal_path_checks_preserve_binary_input_bytes(self):
        data = b"/" + b"Q" * 8192 + b"\x00\xff\x00non-path-input"
        (self.project / "binary.data").write_bytes(data)
        submitted = self.submit([sys.executable, "-c",
                                 "import pathlib,sys; sys.stdout.buffer.write(pathlib.Path('binary.data').read_bytes())"])
        final = self.wait(submitted["job"])
        self.assertEqual(Path(final["stdout_log"]).read_bytes(), data)

    def test_case_alias_search_variables_cannot_load_source(self):
        library = self.project / "Library"
        library.mkdir()
        (library / "module.py").write_text("VALUE = 'mutable-source'\n")
        alias = self.case_alias(self.project) / "library"
        self.assertTrue(os.path.samefile(library, alias))
        for name in ("PATH", "PYTHONPATH", "NODE_PATH"):
            self.rejected_submission(env=dict(self.env, **{name: str(alias)}),
                                     error="source_reference_in_environment")

    def test_case_alias_home_and_ancestors_are_rejected_before_inventory(self):
        os.mkfifo(self.home / "must-not-be-inventoried")
        home_alias, ancestor_alias = self.case_alias(self.home), self.case_alias(self.root)
        for cwd, env in ((home_alias, self.env), (ancestor_alias, self.env),
                         (self.home, dict(self.env, HOME=str(home_alias))),
                         (self.root, dict(self.env, HOME=str(home_alias)))):
            self.rejected_submission("--cwd", str(cwd), env=env, error="unsafe_input_root")
        self.assertEqual(list(self.home.iterdir()), [self.home / "must-not-be-inventoried"])

    def test_case_alias_source_inside_state_is_rejected(self):
        state = self.root / "ScopeState"
        source = state / "Source"
        source.mkdir(mode=0o700, parents=True)
        alias = self.case_alias(state)
        for cwd in (state, source):
            result = self.call("submit", "--cwd", str(cwd), "--", sys.executable, "-c", "pass",
                               state=alias, check=False, refute_if_absent=True)
            self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
            self.assertEqual(json.loads(result.stdout)["error"], "source_in_state")
            self.assertTrue(self.admissions[-1].refuted)
        self.assertEqual(list(state.iterdir()), [source])

    def test_case_alias_nested_state_stays_out_of_capsule_and_fingerprint(self):
        state = self.project / "LedgerState"
        state.mkdir(mode=0o700)
        alias = self.case_alias(state)
        cwd_alias = self.case_alias(self.project)
        script = "import pathlib; print(pathlib.Path('LedgerState').exists())"
        first = self.value("submit", "--cwd", str(cwd_alias), "--", sys.executable, "-c", script,
                           state=alias)
        self.assertEqual(self.output(self.wait(first["job"], state=alias)), "False\n")
        (state / "private-fixture").write_text("must-not-be-captured")
        (state / "private-fixture").chmod(0o600)
        second = self.value("submit", "--", sys.executable, "-c", script, state=state)
        self.assertEqual(self.output(self.wait(second["job"], state=state)), "False\n")
        self.assertEqual(first["input"], second["input"])
        for submitted in (first, second):
            payload = state / "capsules" / submitted["job"] / "payload"
            self.assertFalse((payload / "LedgerState").exists())
            self.assertNotIn(b"must-not-be-captured", (payload.parent / "manifest.json").read_bytes())

    def test_case_sensitive_distinct_source_names_remain_distinct(self):
        source, external = self.root / "CaseProject", self.root / "caseproject"
        source.mkdir()
        self.assertFalse(external.exists(), "case-sensitive fixture precondition failed; no skip")
        external.mkdir()
        self.assertFalse(os.path.samefile(source, external))
        (external / "run.py").write_text("print('external-distinct-object')\n")
        submitted = self.value("submit", "--cwd", str(source), "--", sys.executable,
                               str(external / "run.py"))
        self.assertEqual(self.output(self.wait(submitted["job"])), "external-distinct-object\n")

    def test_declared_logname_metadata_is_private_across_reader_environments(self):
        secret = "fictitious-logname-credential-3821"
        submitted = self.value("submit", "--credential-env", "LOGNAME", "--credential-env", "USER",
                               "--", sys.executable, "-c", "import os; print(os.environ['LOGNAME'])",
                               env=dict(self.env, LOGNAME=secret, USER=secret))
        reader = dict(self.env, LOGNAME="different-reader", USER="different-reader")
        final = self.value("wait", submitted["job"], "--timeout", "9", env=reader)
        status = self.value("status", submitted["job"], env=reader)
        proof = self.value("evidence", submitted["job"], env=reader)
        self.assertEqual(status["submitted_by"]["user"], pwd.getpwuid(os.getuid()).pw_name)
        self.assertEqual(status["submitted_by"]["uid"], os.getuid())
        self.assertEqual(self.output(final), "[REDACTED]\n")
        self.assert_publication_excludes_secret(secret, submitted, final, status, proof)

    def test_declared_account_and_host_metadata_are_redacted(self):
        account, host = pwd.getpwuid(os.getuid()).pw_name, socket.gethostname()
        self.assertTrue(account and host, "real account/host precondition requires nonempty values")
        for component in self.state.relative_to(self.root).parts:
            for value in (account, host):
                self.assertNotIn(os.fsencode(value), os.fsencode(component),
                                 "fixture state component conflicts with a declared account/host")
        for component in self.root.parts:
            for value in (account, host):
                self.assertNotIn(os.fsencode(value), os.fsencode(component),
                                 "external base conflicts with the absolute-state credential guard; no skip")
        submitted = self.value("submit", "--client", "fixture-client", "--credential-env", "ACCOUNT_VALUE",
                               "--credential-env", "HOST_VALUE", "--", sys.executable, "-c",
                               "import os; print(os.environ['ACCOUNT_VALUE']); print(os.environ['HOST_VALUE'])",
                               env=dict(self.env, ACCOUNT_VALUE=account, HOST_VALUE=host))
        final = self.wait(submitted["job"])
        status = self.value("status", submitted["job"])
        self.assertEqual(self.output(final), "[REDACTED]\n[REDACTED]\n")
        self.assertEqual(final["submitted_by"]["user"], "[REDACTED]")
        self.assertEqual(final["submitted_by"]["host"], "[REDACTED]")
        self.assertEqual(final["supervisor"]["host"], "[REDACTED]")
        self.assertEqual(status["submitted_by"]["user"], "[REDACTED]")
        self.assertEqual(status["submitted_by"]["host"], "[REDACTED]")
        self.assertEqual(status["supervisor"]["host"], "[REDACTED]")
        proof = self.value("evidence", submitted["job"])
        self.assertTrue(proof["evidence"]["valid"])
        for secret in (account, host):
            self.assert_publication_excludes_secret(
                secret, submitted["client"], list(final["submitted_by"].values()),
                list(final["supervisor"].values()), list(status["submitted_by"].values()),
                list(status["supervisor"].values()), proof["evidence"]["manifest"],
                [entry["path"] for entry in proof["evidence"]["files"]])

    def limited_file_cli(self, limit, *args):
        from test_cli import CLI
        admission = self.admit(self.state) if args[0] == "submit" else None
        wrapper = ("import os,resource,signal,sys\n"
                   "resource.setrlimit(resource.RLIMIT_FSIZE,(int(sys.argv[1]),int(sys.argv[1])))\n"
                   "signal.signal(signal.SIGXFSZ,signal.SIG_IGN)\n"
                   "os.execv(sys.executable,[sys.executable,'-B',*sys.argv[2:]])\n")
        result = subprocess.run([sys.executable, "-B", "-c", wrapper, str(limit), str(CLI),
                                 "--state-dir", str(self.state), *args], env=self.env,
                                cwd=self.project, capture_output=True, text=True, timeout=15)
        if admission is not None:
            value = json.loads(result.stdout)
            if "job" in value:
                admission.attribute(value)
        return result

    def test_durable_admission_receipt_survives_unavailable_reader_copy(self):
        limit = 256 * 1024
        for index in range(8):
            seed = self.submit([sys.executable, "-c", "pass"],
                               env=dict(self.env, FIXTURE_PADDING=str(index) + "P" * 49152))
            self.wait(seed["job"])
            self.settle()
        self.assertGreater((self.state / "state.db").stat().st_size, limit)
        argv, ready, gate, done = self.barrier()
        blocker = self.submit(argv)
        self.running(blocker["job"], ready)
        try:
            self.assertLess((self.state / "state.db-wal").stat().st_size, limit // 2)
            response = self.limited_file_cli(limit, "submit", "--", sys.executable, "-c", "print('once')")
            self.assertEqual(response.returncode, 0, (response.stdout, response.stderr))
            submitted = json.loads(response.stdout)
            self.assertTrue(submitted["registered"])
            self.assertTrue(submitted["accepted"])
            self.assertEqual((submitted["observation"], submitted["state"], submitted["view"]),
                             ("durable_admission", "queued", "queued"))
            before = self.state_image(self.state)
            unavailable = self.limited_file_cli(limit, "status", submitted["job"])
            self.assertEqual(unavailable.returncode, 2, (unavailable.stdout, unavailable.stderr))
            self.assertIn("error", json.loads(unavailable.stdout))
            self.assertEqual(self.state_image(self.state), before)
            self.assertFalse(done.exists(), "blocker ended before controlled release")
        finally:
            gate.touch()
        self.wait(blocker["job"])
        final = self.wait(submitted["job"])
        self.assertEqual(self.output(final), "once\n")
        self.assertEqual(final["attempt"], submitted["attempt"])
        self.assertEqual([t["to"] for t in final["transitions"]],
                         ["queued", "starting", "running", "finished"])
