import json
import os
import sys


class ReaderTempCases:
    def assert_hostile_temp_reads_preserve_state(self, submitted, active=False, done=None):
        trap = self.state / "reader-temp-trap"
        trap.mkdir(mode=0o700)
        alias = self.root / "state-temp-alias"
        alias.symlink_to(self.state, target_is_directory=True)
        nested_alias = self.root / "nested-temp-alias"
        nested_alias.symlink_to(alias / trap.name, target_is_directory=True)
        for directory in (self.state, trap):
            before = self.state_image(self.state)
            probe = directory / "fixture-metadata-probe"
            probe.touch()
            probe.unlink()
            self.assertNotEqual(self.state_image(self.state), before,
                                "filesystem did not expose a transient directory entry")
        for variable in ("TMPDIR", "TEMP", "TMP"):
            for target in (self.state, trap, alias, nested_alias):
                env = dict(self.env)
                for name in ("TMPDIR", "TEMP", "TMP"):
                    env.pop(name, None)
                env[variable] = str(target)
                for job in (submitted["job"], "j_" + "0" * 32):
                    for operation in ("status", "wait", "evidence"):
                        with self.subTest(variable=variable, target=target, job=job, operation=operation):
                            before = self.state_image(self.state)
                            args = (operation, job) + (("--timeout", "0") if operation == "wait" else ())
                            result = self.call(*args, env=env, check=False)
                            self.assertEqual(self.state_image(self.state), before)
                            if done is not None:
                                self.assertFalse(done.exists(), "reader fixture ended before release")
                            if job != submitted["job"]:
                                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                                self.assertIn("error", json.loads(result.stdout))
                                continue
                            self.assertEqual(result.returncode, 4 if active and operation == "wait" else 0,
                                             (result.stdout, result.stderr))
                            value = json.loads(result.stdout)
                            self.assertEqual(value["job"], job)
                            self.assertEqual(value["attempt"], submitted["attempt"])
                            if active:
                                if operation != "evidence":
                                    self.assertEqual((value["state"], value["view"]), ("running", "running"))
                                self.assertEqual(value["evidence"]["errors"], ["attempt_not_finished"])
                            else:
                                self.assertEqual(value["outcome"], "succeeded")
                                self.assertTrue(value["evidence"]["valid"])

    def test_reader_temp_environment_preserves_quiet_state(self):
        submitted = self.submit([sys.executable, "-c", "print('quiet-reader-fixture')"])
        self.wait(submitted["job"])
        self.closed_database_bytes(submitted)
        self.assert_hostile_temp_reads_preserve_state(submitted)

    def test_reader_temp_environment_preserves_live_wal(self):
        control = self.root / "reader-control"
        ready, done = self.root / "reader-ready", self.root / "reader-done"
        os.mkfifo(control, 0o600)
        fd = os.open(control, os.O_RDWR | os.O_NONBLOCK)
        script = ("import os,pathlib,select,sys\n"
                  "control,ready,done=map(pathlib.Path,sys.argv[1:])\n"
                  "fd=os.open(control,os.O_RDWR|os.O_NONBLOCK)\n"
                  "ready.write_text('waiting')\n"
                  "readable=select.select([fd],[],[],30)[0]\n"
                  "released=bool(readable) and os.read(fd,1)==b'R'\n"
                  "os.close(fd)\n"
                  "done.write_text('released' if released else 'watchdog')\n"
                  "sys.exit(0 if released else 91)\n")
        try:
            submitted = self.submit([sys.executable, "-B", "-c", script,
                                     str(control), str(ready), str(done)])
            self.running(submitted["job"], ready)
            self.assertGreater((self.state / "state.db-wal").stat().st_size, 32)
            self.assertTrue((self.state / "state.db-shm").is_file())
            self.assert_hostile_temp_reads_preserve_state(submitted, active=True, done=done)
        finally:
            try:
                os.write(fd, b"R")
            finally:
                os.close(fd)
        final = self.wait(submitted["job"])
        self.assertEqual(final["outcome"], "succeeded")
        self.assertEqual(done.read_text(), "released")
        self.closed_database_bytes(submitted)
