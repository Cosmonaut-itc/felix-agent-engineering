import fcntl
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import pwd
import sqlite3
import stat
import subprocess
import sys


class CorrectionCases:
    def rejected_submission(self, *options, argv=None, env=None, error=None, job_environment=None):
        state = self.root / ("rejected-" + str(len(self.admissions)))
        self.assertFalse((state / "state.db").exists())
        result = self.call("submit", *options, "--", *(argv or [sys.executable, "-c", "pass"]),
                           state=state, env=env, check=False, refute_if_absent=True,
                           job_environment=job_environment)
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertTrue(self.admissions[-1].refuted)
        self.assertFalse((state / "state.db").exists())
        if error:
            self.assertEqual(json.loads(result.stdout)["error"], error)
        return result

    def blocked_queue(self):
        argv, ready, gate, _ = self.barrier()
        blocker = self.submit(argv)
        self.running(blocker["job"], ready)
        return blocker, gate

    def test_pythonpath_absolute_alias_and_relative_escape_are_rejected(self):
        library = self.project / "lib"
        library.mkdir()
        (library / "modulo.py").write_text("VALUE = 'source'\n")
        alias = self.root / "library-alias"
        alias.symlink_to(library, target_is_directory=True)
        for path, error in ((str(library), "source_reference_in_environment"),
                            (str(alias), "source_reference_in_environment"),
                            (str(alias / ".." / "lib"), "source_reference_in_environment"),
                            (str(self.project.parent), "source_reference_in_environment"),
                            ("../outside", "search_path_escapes_capture")):
            self.rejected_submission(env=dict(self.env, PYTHONPATH=path), error=error)
        external = self.root / "external-library"
        external.mkdir()
        (self.project / "escape-alias").symlink_to(external, target_is_directory=True)
        self.rejected_submission(env=dict(self.env, PYTHONPATH="escape-alias"),
                                 error="search_path_escapes_capture")

    def test_relative_pythonpath_in_queue_uses_captured_module(self):
        library = self.project / "lib"
        library.mkdir()
        module = library / "modulo.py"
        module.write_text("VALUE = 'captured-module'\n")
        (self.project / "run.py").write_text("import modulo\nprint(modulo.VALUE)\n")
        blocker, gate = self.blocked_queue()
        captured = self.submit([sys.executable, "-B", "run.py"], env=dict(self.env, PYTHONPATH="lib"))
        self.assertEqual(captured["state"], "queued")
        module.write_text("VALUE = 'mutated-origin'\n")
        gate.touch()
        self.wait(blocker["job"])
        final = self.wait(captured["job"])
        self.assertEqual(self.output(final), "captured-module\n")
        self.assertEqual(final["input"], captured["input"])

    def test_ordinary_pwd_in_queue_is_materialized(self):
        data = self.project / "data.txt"
        data.write_text("captured-pwd-data")
        script = ("import os,pathlib\n"
                  "print(os.environ['PWD'] == os.getcwd())\n"
                  "print((pathlib.Path(os.environ['PWD']) / 'data.txt').read_text())\n")
        blocker, gate = self.blocked_queue()
        submitted = self.submit([sys.executable, "-c", script], env=dict(self.env, PWD=str(self.project)))
        self.assertEqual(submitted["state"], "queued")
        data.write_text("mutated-pwd-origin")
        gate.touch()
        self.wait(blocker["job"])
        final = self.wait(submitted["job"])
        self.assertEqual(self.output(final), "True\ncaptured-pwd-data\n")
        context = final["input_policy"]["execution_context"]
        self.assertEqual(context["version"], "fleet-context@2")
        self.assertEqual(context["PWD"], "materialized-cwd")
        self.assertNotIn(str(self.project), json.dumps(context))

    def test_external_pythonpath_remains_available_to_queued_job(self):
        library = self.root / "external-valid-library"
        library.mkdir()
        (library / "external_modulo.py").write_text("VALUE = 'external-dependency'\n")
        data = self.project / "data.txt"
        data.write_text("captured-input")
        blocker, gate = self.blocked_queue()
        captured = self.submit([sys.executable, "-B", "-c",
                                "import external_modulo,pathlib; print(external_modulo.VALUE); "
                                "print(pathlib.Path('data.txt').read_text())"],
                               env=dict(self.env, PYTHONPATH=str(library)))
        self.assertEqual(captured["state"], "queued")
        data.write_text("mutated-input")
        gate.touch()
        self.wait(blocker["job"])
        self.assertEqual(self.output(self.wait(captured["job"])), "external-dependency\ncaptured-input\n")

    def test_path_rejects_secondary_source_tools_and_aliases(self):
        tools = self.project / "tools"
        tools.mkdir()
        secondary = tools / "secondary"
        secondary.write_text(f"#!{sys.executable}\nprint('mutable-secondary')\n")
        secondary.chmod(0o755)
        alias = self.root / "tools-alias"
        alias.symlink_to(tools, target_is_directory=True)
        external = self.root / "external-tools"
        external.mkdir()
        (external / "secondary").symlink_to(secondary)
        primary = [sys.executable, "-c", "import subprocess; subprocess.run(['secondary'],check=True)"]
        for path in (str(tools), str(alias), str(external)):
            result = self.rejected_submission(argv=primary, env=dict(self.env, PATH=path),
                                              error="source_reference_in_environment")
            self.assertNotIn("mutable-secondary", result.stdout + result.stderr)
        self.rejected_submission(argv=primary, env=dict(self.env, PATH="../outside"),
                                 error="search_path_escapes_capture")

    def test_relative_path_secondary_tool_in_queue_uses_capture(self):
        self.initialize_git()
        package, tools = self.project / "package", self.project / "tools"
        package.mkdir()
        tools.mkdir()
        (package / "keep").write_text("package")
        secondary = tools / "secondary"
        secondary.write_text(f"#!{sys.executable}\nprint('captured-secondary')\n")
        secondary.chmod(0o755)
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        blocker, gate = self.blocked_queue()
        captured = self.value("submit", "--cwd", str(package), "--", sys.executable, "-c",
                              "import subprocess; subprocess.run(['secondary'],check=True)",
                              env=dict(self.env, PATH="../tools:" + os.defpath))
        self.assertEqual(captured["state"], "queued")
        secondary.write_text(f"#!{sys.executable}\nprint('mutated-secondary')\n")
        gate.touch()
        self.wait(blocker["job"])
        self.assertEqual(self.output(self.wait(captured["job"])), "captured-secondary\n")

    def test_empty_path_component_searches_materialized_cwd(self):
        secondary = self.project / "secondary"
        secondary.write_text(f"#!{sys.executable}\nprint('captured-current-directory')\n")
        secondary.chmod(0o755)
        blocker, gate = self.blocked_queue()
        captured = self.submit([sys.executable, "-c",
                                "import subprocess; subprocess.run(['secondary'],check=True)"],
                               env=dict(self.env, PATH=""))
        self.assertEqual(captured["state"], "queued")
        secondary.unlink()
        gate.touch()
        self.wait(blocker["job"])
        self.assertEqual(self.output(self.wait(captured["job"])), "captured-current-directory\n")

    def test_declared_loader_table_rejects_source_components(self):
        variables = ("LD_LIBRARY_PATH", "LD_PRELOAD", "DYLD_LIBRARY_PATH",
                     "DYLD_FALLBACK_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH",
                     "DYLD_FALLBACK_FRAMEWORK_PATH", "DYLD_INSERT_LIBRARIES",
                     "NODE_PATH", "RUBYLIB", "PERL5LIB")
        for name in variables:
            result = self.rejected_submission(job_environment={name: str(self.project)},
                                              error="source_reference_in_environment")
            self.assertNotIn(str(self.project), result.stdout)
        self.rejected_submission(job_environment={"LD_LIBRARY_PATH": "/nonexistent;" + str(self.project)},
                                 error="source_reference_in_environment")
        self.rejected_submission(job_environment={"LD_PRELOAD": "/nonexistent " + str(self.project)},
                                 error="source_reference_in_environment")

    def test_broad_roots_are_rejected_before_personal_inventory(self):
        os.mkfifo(self.home / "must-not-be-inventoried")
        uid_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        for root in (self.home, self.root, uid_home, uid_home.parent, Path("/")):
            self.rejected_submission("--cwd", str(root), error="unsafe_input_root")
        self.assertEqual(list(self.home.iterdir()), [self.home / "must-not-be-inventoried"])

    def test_source_in_state_is_rejected_and_nested_state_is_excluded(self):
        state = self.root / "source-state"
        source = state / "source"
        source.mkdir(mode=0o700, parents=True)
        for cwd in (state, source):
            result = self.call("submit", "--cwd", str(cwd), "--", sys.executable, "-c", "pass",
                               state=state, check=False, refute_if_absent=True)
            self.assertEqual(json.loads(result.stdout)["error"], "source_in_state")
            self.assertTrue(self.admissions[-1].refuted)
        self.assertEqual(list(state.iterdir()), [source])
        nested = self.project / "nested-state"
        submitted = self.submit([sys.executable, "-c",
                                 "import pathlib; print(pathlib.Path('nested-state').exists())"], state=nested)
        self.assertEqual(self.output(self.wait(submitted["job"], state=nested)), "False\n")

    def test_nested_git_selection_and_private_paths_use_capture_root(self):
        self.initialize_git()
        package = self.project / "package"
        package.mkdir()
        (package / "run.py").write_text(
            "from pathlib import Path\nprint(Path('data').read_text())\n"
            "print(Path('private.data').exists())\nprint(Path('../excluded').exists())\n"
            "Path('reports/result').write_text('nested-artifact')\n")
        (package / "data").write_text("nested-captured")
        (package / "private.data").write_text("private")
        (self.project / "excluded").write_text("unselected")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        blocker, gate = self.blocked_queue()
        submitted = self.value("submit", "--cwd", str(package), "--input", "package",
                               "--private", "package/private.data", "--artifact", "reports/result", "--",
                               sys.executable, "run.py")
        self.assertEqual(submitted["state"], "queued")
        (package / "data").write_text("mutated")
        gate.touch()
        self.wait(blocker["job"])
        final = self.wait(submitted["job"])
        self.assertEqual(self.output(final), "nested-captured\nFalse\nFalse\n")
        _, files = self.artifacts(submitted["job"])
        self.assertEqual(Path(files[0]["path"]).read_text(), "nested-artifact")
        self.assertFalse((package / "reports").exists())

    def assert_publication_excludes_secret(self, secret, *responses):
        forbidden = (secret, hashlib.sha256(secret.encode()).hexdigest())
        for response in responses:
            for value in forbidden:
                self.assertNotIn(value, json.dumps(response))
        for path in (self.state / "capsules").rglob("*"):
            for value in forbidden:
                self.assertNotIn(value, str(path.relative_to(self.state)))
                if path.is_file():
                    self.assertNotIn(value.encode(), path.read_bytes())
        for path in (self.state / "attempts").glob("*/bundle/**/*"):
            for value in forbidden:
                self.assertNotIn(value, str(path.relative_to(self.state)))
                if path.is_file():
                    self.assertNotIn(value.encode(), path.read_bytes())
        for value in forbidden:
            self.assertNotIn(value, (self.state / "supervisor.log").read_text())

    def test_declared_secret_in_client_and_private_metadata_is_redacted(self):
        secret = "fictitious-metadata-credential-3821"
        private = "private-" + secret
        (self.project / private).write_text(secret)
        submitted = self.value("submit", "--client", "client-" + secret, "--private", private,
                               "--credential-env", "SERVICE_ACCESS", "--", sys.executable, "-c",
                               "import os; print(os.environ['SERVICE_ACCESS'])",
                               env=dict(self.env, SERVICE_ACCESS=secret))
        final = self.wait(submitted["job"])
        self.assertEqual(submitted["client"], "client-[REDACTED]")
        self.assertEqual(submitted["input_policy"]["consumer_private"], ["private-[REDACTED]"])
        self.assertEqual(self.output(final), "[REDACTED]\n")
        self.assert_publication_excludes_secret(secret, submitted, final, self.value("evidence", submitted["job"]))

    def test_declared_secret_in_names_selection_artifacts_and_errors_is_not_published(self):
        secret = "fictitious-path-credential-3821"
        filename = "file-" + secret
        (self.project / filename).write_text("innocuous-content")
        env = dict(self.env, SERVICE_TOKEN=secret)
        for options in ((), ("--input", filename), ("--artifact", filename + ".report"),
                        ("--cwd", str(self.root / filename)), ("--private", "../" + filename)):
            result = self.rejected_submission(*options, env=env)
            for value in (secret, hashlib.sha256(secret.encode()).hexdigest()):
                self.assertNotIn(value, result.stdout + result.stderr)

    def test_concurrent_large_streams_redact_secrets_longer_than_read_blocks(self):
        secret = "fictitious-concurrent-stream-credential" * 300
        script = ("import os,threading\n"
                  "secret=os.environ['BULK_CREDENTIAL'].encode()\n"
                  "def emit(fd,byte):\n"
                  "    for _ in range(64):\n"
                  "        data=memoryview(byte*8192 + secret + b'\\n')\n"
                  "        while data:\n"
                  "            data=data[os.write(fd,data):]\n"
                  "a=threading.Thread(target=emit,args=(1,b'O'))\n"
                  "b=threading.Thread(target=emit,args=(2,b'E'))\n"
                  "a.start(); b.start(); a.join(); b.join()\n")
        submitted = self.value("submit", "--credential-env", "BULK_CREDENTIAL", "--",
                               sys.executable, "-c", script, env=dict(self.env, BULK_CREDENTIAL=secret))
        final = self.wait(submitted["job"])
        self.assertEqual(Path(final["stdout_log"]).read_bytes(), (b'O'*8192 + b'[REDACTED]\n')*64)
        self.assertEqual(Path(final["stderr_log"]).read_bytes(), (b'E'*8192 + b'[REDACTED]\n')*64)
        self.assert_publication_excludes_secret(secret, submitted, final)

    def state_image(self, state):
        paths = [state, *sorted(state.rglob("*"))] if state.exists() else []
        return {str(p.relative_to(state)): (p.lstat().st_dev, p.lstat().st_ino,
                p.lstat().st_mode, p.lstat().st_uid, p.lstat().st_gid, p.lstat().st_nlink,
                p.lstat().st_size, p.lstat().st_mtime_ns, p.lstat().st_ctime_ns,
                hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None) for p in paths}

    def quiet_database_image(self):
        """Observe a closed fixture under its directory EX lock; never repair it."""
        files = {}
        for name in ("state.db", "state.db-wal", "state.db-shm"):
            try:
                info = (self.state / name).lstat()
            except FileNotFoundError:
                if name == "state.db":
                    raise
                continue
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise ValueError("quiet database files must be regular, private, owned and have one link: " + name)
            files[name] = (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
                           info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        sidecars = set(files) - {"state.db"}
        if sidecars and (sidecars != {"state.db-wal", "state.db-shm"}
                         or files["state.db-wal"][6] != 0):
            raise ValueError("quiet state requires no sidecars or an empty WAL with SHM present")
        image = self.state_image(self.state)
        if any(image.get(name, ())[:9] != identity for name, identity in files.items()):
            raise ValueError("quiet database files changed during observation")
        if {name for name in ("state.db-wal", "state.db-shm") if name in image} != sidecars:
            raise ValueError("quiet sidecar presence changed during observation")
        return image

    def closed_database_bytes(self, submitted):
        fd = None
        try:
            result = self.call("status", submitted["job"], check=False)
            observed = json.loads(result.stdout)
            if (result.returncode != 0
                    or (observed.get("job"), observed.get("attempt"), observed.get("state"))
                    != (submitted["job"], submitted["attempt"], "finished")):
                raise ValueError("terminal identity not confirmed: rc={}, stdout={!r}, stderr={!r}".format(
                    result.returncode, result.stdout, result.stderr))
            self.settle()
            self.guard_admissions()
            fd = os.open(self.state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            directory = os.fstat(fd)
            current = self.state.lstat()
            if (directory.st_uid != os.getuid() or directory.st_mode & 0o077
                    or (directory.st_dev, directory.st_ino)
                    != (current.st_dev, current.st_ino)):
                raise ValueError("closed database directory is not private, owned and stable")
            before = self.quiet_database_image()
            with os.fdopen(os.open("state.db", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd), "rb") as source:
                opened = os.fstat(source.fileno())
                if (opened.st_dev, opened.st_ino) != before["state.db"][:2]:
                    raise ValueError("closed main file identity changed before reading")
                data = source.read()
            if hashlib.sha256(data).hexdigest() != before["state.db"][-1]:
                raise ValueError("closed main file bytes changed during reading")
            if (len(data) < 100 or data[:16] != b"SQLite format 3\x00"
                    or int.from_bytes(data[60:64], "big") != 3
                    or data[68:72] != b"FLT1"):
                raise ValueError("closed main file does not contain the expected public Fleet schema header")
            # Sidecar shape is not checkpoint/integrity proof. Validate only a
            # standalone byte copy, without WAL/SHM or SQLite on the original.
            copy = self.root / "closed-main.db"
            with os.fdopen(os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as output:
                output.write(data)
            self.assertEqual(copy.read_bytes(), data)
            conn = sqlite3.connect(copy.as_uri() + "?mode=ro&immutable=1", uri=True)
            try:
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
                self.assertEqual(conn.execute("PRAGMA application_id").fetchone(), (0x464C5431,))
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone(), (3,))
                row = conn.execute("SELECT j.id,a.id,a.state,a.outcome FROM jobs j "
                                   "JOIN attempts a ON a.job_id=j.id WHERE j.id=? AND a.id=?",
                                   (submitted["job"], submitted["attempt"])).fetchone()
                self.assertEqual(row, (submitted["job"], submitted["attempt"], "finished",
                                       observed["recorded_outcome"]))
                transitions = conn.execute("SELECT seq,job_id,attempt_id,from_state,to_state,at "
                                           "FROM transitions WHERE job_id=? ORDER BY seq",
                                           (submitted["job"],)).fetchall()
                self.assertEqual(transitions, [(t["seq"], t["job"], t["attempt"], t["from"], t["to"], t["at"])
                                                for t in observed["transitions"]])
                self.assertTrue(transitions, "closed main file has no terminal history")
                self.assertEqual(transitions[-1][4], "finished")
            finally:
                conn.close()
            self.assertEqual(self.quiet_database_image(), before)
            return data
        except (AssertionError, OSError, ValueError, sqlite3.Error) as exc:
            report = self.root / "closed-database-preparation.json"
            self.diagnostics.append(report)
            report.write_text(json.dumps({"cause": str(exc), "job": submitted["job"],
                                          "attempt": submitted["attempt"], "state": str(self.state),
                                          "image": self.state_image(self.state)}) + "\n")
            report.chmod(0o600)
            self.fail("closed database preparation unconfirmed; {}; retained {}".format(exc, report))
        finally:
            if fd is not None:
                os.close(fd)

    def test_reads_preserve_active_wal_shm_and_checkpointed_state(self):
        blocker, gate = self.blocked_queue()
        self.assertGreater((self.state / "state.db-wal").stat().st_size, 32)
        self.assertTrue((self.state / "state.db-shm").is_file())
        before = self.state_image(self.state)
        status = self.value("status", blocker["job"])
        self.assertEqual(status["state"], "running")
        self.assertEqual(self.call("wait", blocker["job"], "--timeout", "0", check=False).returncode, 4)
        self.value("evidence", blocker["job"])
        self.assertEqual(self.state_image(self.state), before)
        gate.touch()
        final = self.wait(blocker["job"])
        self.closed_database_bytes(blocker)
        before = self.state_image(self.state)
        for operation in ("status", "wait", "evidence"):
            value = self.value(operation, blocker["job"])
            self.assertEqual(value["outcome"], "succeeded")
        self.assertEqual(self.state_image(self.state), before)
        self.assertEqual(self.value("status", blocker["job"])["transitions"], final["transitions"])

    def incompatible_rollback_state(self, submitted, observed):
        """Convert only a new private copy; retain every preparation failure."""
        preparation = self.root / "incompatible-preparation"
        validation = self.root / "incompatible-validation"
        incompatible = self.root / "incompatible"
        report = self.root / "incompatible-preparation.json"
        returns = {}
        original = None

        @contextmanager
        def closing(resource):
            try:
                yield resource
            except BaseException as exc:
                try:
                    resource.close()
                except BaseException as close_error:
                    raise exc from close_error
                raise
            else:
                resource.close()

        def connection(database, mode):
            return closing(sqlite3.connect(database.as_uri() + "?mode=" + mode, uri=True,
                                           timeout=10, isolation_level=None))

        def write_private(database, data):
            with closing(os.fdopen(os.open(database, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                           0o600), "wb")) as output:
                output.write(data)
            self.assertEqual(stat.S_IMODE(database.lstat().st_mode), 0o600)
            self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(),
                             hashlib.sha256(data).hexdigest())

        try:
            data = self.closed_database_bytes(submitted)
            original = self.state_image(self.state)
            self.assertEqual((observed["job"], observed["attempt"], observed["state"]),
                             (submitted["job"], submitted["attempt"], "finished"))
            for directory in (preparation, validation, incompatible):
                directory.mkdir(mode=0o700)
                self.assertEqual(stat.S_IMODE(directory.lstat().st_mode), 0o700)
            copy = preparation / "state.db"
            write_private(copy, data)
            with connection(copy, "rw") as conn:
                returns["initial_mode"] = conn.execute("PRAGMA journal_mode").fetchall()
                self.assertIn(returns["initial_mode"], ([("wal",)], [("delete",)]))
                # Drain/reset WAL before leaving WAL mode. TRUNCATE must finish
                # with busy=0 and zero frames; non-WAL reports -1/-1 instead.
                returns["checkpoint"] = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
                expected = [(0, 0, 0)] if returns["initial_mode"] == [("wal",)] else [(0, -1, -1)]
                self.assertEqual(returns["checkpoint"], expected)
                returns["rollback_mode"] = conn.execute("PRAGMA journal_mode=DELETE").fetchall()
                self.assertEqual(returns["rollback_mode"], [("delete",)])
            # Read only after SQLite has closed. Validate a standalone main,
            # never sidecars from the converter, and never the original ledger.
            data = copy.read_bytes()
            self.assertTrue(len(data) >= 100 and data[:16] == b"SQLite format 3\x00"
                            and data[18:20] == b"\x01\x01" and data[68:72] == b"FLT1"
                            and int.from_bytes(data[60:64], "big") == 3,
                            "converted main must have SQLite/FLT1/schema3 and rollback header 1/1")
            for version in (3, 999):
                if version == 999:
                    # Derive only after the closed rollback main passed all
                    # integrity, identity, outcome and history checks below.
                    data = bytearray(data)
                    data[60:64] = (999).to_bytes(4, "big")
                standalone = validation / ("schema-{}.db".format(version))
                write_private(standalone, data)
                before = self.state_image(validation)
                with connection(standalone, "ro") as conn:
                    conn.execute("PRAGMA foreign_keys=ON")
                    self.assertEqual(conn.execute("PRAGMA application_id").fetchall(), [(0x464C5431,)])
                    self.assertEqual(conn.execute("PRAGMA user_version").fetchall(), [(version,)])
                    self.assertEqual(conn.execute("PRAGMA journal_mode").fetchall(), [("delete",)])
                    self.assertEqual(conn.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
                    row = conn.execute("SELECT j.id,a.id,a.state,a.outcome FROM jobs j "
                                       "JOIN attempts a ON a.job_id=j.id WHERE j.id=? AND a.id=?",
                                       (submitted["job"], submitted["attempt"])).fetchone()
                    self.assertEqual(row, (submitted["job"], submitted["attempt"], "finished",
                                           observed["recorded_outcome"]))
                    transitions = conn.execute("SELECT seq,job_id,attempt_id,from_state,to_state,at "
                                               "FROM transitions WHERE job_id=? ORDER BY seq",
                                               (submitted["job"],)).fetchall()
                    self.assertEqual(transitions, [(t["seq"], t["job"], t["attempt"], t["from"], t["to"], t["at"])
                                                    for t in observed["transitions"]])
                    self.assertTrue(transitions, "rollback main has no terminal history")
                    self.assertEqual(transitions[-1][4], "finished")
                self.assertEqual(self.state_image(validation), before)
            write_private(incompatible / "state.db", data)
            self.assertEqual(self.state_image(self.state), original)
            return incompatible
        except BaseException as exc:
            # Mark retention before any fallible observation/persistence. Never
            # let a secondary close/image/report error replace the primary.
            self.diagnostics.append(report)
            diagnostic = {"cause": {"type": type(exc).__name__, "message": str(exc)},
                          "job": submitted["job"], "attempt": submitted["attempt"],
                          "returns": returns, "original_before": original,
                          "private_copies_not_for_publication": True, "images": {}}
            secondary = [exc.__cause__] if exc.__cause__ is not None else []
            for directory in (self.state, preparation, validation, incompatible):
                try:
                    diagnostic["images"][str(directory)] = self.state_image(directory)
                except BaseException as image_error:
                    secondary.append(image_error)
                    diagnostic["images"][str(directory)] = {"unavailable": repr(image_error)}
            exc.incompatible_preparation = diagnostic
            diagnostic["secondary_causes"] = [repr(error) for error in secondary]
            try:
                with closing(os.fdopen(os.open(report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                               0o600), "w")) as output:
                    output.write(json.dumps(diagnostic) + "\n")
            except BaseException as report_error:
                secondary.append(report_error)
                diagnostic["secondary_causes"].append(repr(report_error))
            exc.incompatible_preparation_errors = tuple(secondary)
            if secondary:
                raise exc from secondary[0]
            raise

    def test_readers_reject_unsafe_incompatible_and_empty_state_without_repair(self):
        submitted = self.submit([sys.executable, "-c", "pass"])
        observed = self.wait(submitted["job"])
        incompatible = self.incompatible_rollback_state(submitted, observed)
        unsafe, empty = self.root / "unsafe-reader", self.root / "empty-reader"
        unsafe.mkdir(mode=0o755)
        unsafe.chmod(0o755)
        empty.mkdir(mode=0o700)
        for state in (unsafe, empty, incompatible, self.root / "absent-reader"):
            before = self.state_image(state)
            for operation in ("status", "wait", "evidence"):
                args = (operation, submitted["job"]) + (("--timeout", "0") if operation == "wait" else ())
                result = self.call(*args, state=state, check=False)
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                if state == incompatible:
                    self.assertEqual(json.loads(result.stdout)["error"],
                                     "unsupported Fleet pilot schema; no migration")
            self.assertEqual(self.state_image(state), before)

    def test_missing_payload_and_truncated_manifest_degrade_only_the_view(self):
        for missing in (True, False):
            submitted = self.artifact_job("from pathlib import Path; Path('report.txt').write_text('report')")
            final = self.wait(submitted["job"])
            if missing:
                _, files = self.artifacts(submitted["job"])
                Path(files[0]["path"]).unlink()
            else:
                manifest = Path(final["evidence"]["manifest"])
                manifest.write_bytes(manifest.read_bytes()[:17])
            degraded = self.wait(submitted["job"], 3)
            self.assertEqual(degraded["recorded_outcome"], "succeeded")
            self.assertFalse(degraded["evidence"]["valid"])
            self.assertEqual(degraded["transitions"], final["transitions"])

    def test_external_process_directory_lock_blocks_bootstrap(self):
        self.state.mkdir(mode=0o700)
        fixture = ("import fcntl,os,select,sys\n"
                   "fd=os.open(sys.argv[1],os.O_RDONLY|os.O_DIRECTORY)\n"
                   "fcntl.flock(fd,fcntl.LOCK_EX)\n"
                   "print('locked',flush=True)\n"
                   "ready=select.select([sys.stdin],[],[],20)[0]\n"
                   "sys.exit(0 if ready and sys.stdin.readline() == 'release\\n' else 91)\n")
        process = subprocess.Popen([sys.executable, "-c", fixture, str(self.state)], env=self.env,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        self.processes.append(process)
        self.client_groups.append(process.pid)
        self.assertEqual(self.response_line(process), "locked")
        try:
            result = self.call("submit", "--", sys.executable, "-c", "print('must-not-run')",
                               check=False, refute_if_absent=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["error"], "invalid_request_or_state")
            self.assertTrue(self.admissions[-1].refuted)
            self.assertEqual(list(self.state.iterdir()), [])
        finally:
            stdout, stderr = process.communicate("release\n", timeout=3)
        self.assertEqual(process.returncode, 0, (stdout, stderr))
        submitted = self.submit([sys.executable, "-c", "print('after-release')"])
        self.assertEqual(self.output(self.wait(submitted["job"])), "after-release\n")
