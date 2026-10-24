import fcntl
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from test_cli import fixture_base


@unittest.skipUnless(os.name == "posix", "Ledger lifecycle requires POSIX flock")
class LedgerLifecycle(unittest.TestCase):
    def test_ledger_close_waits_beyond_operation_timeout_until_reader_releases(self):
        root = Path(tempfile.mkdtemp(prefix="fleet-lifecycle-", dir=fixture_base())).resolve()
        state = root / "state"
        script = ("import pathlib,sqlite3,sys\n"
                  "sys.path.insert(0,sys.argv[1])\n"
                  "from fleet import runner\n"
                  "conn=runner._connect(pathlib.Path(sys.argv[2]),create=True)\n"
                  "print('connected',flush=True)\n"
                  "if sys.stdin.readline() != 'close\\n': sys.exit(91)\n"
                  "print('closing',flush=True)\n"
                  "conn.close()\n"
                  "try: conn.execute('PRAGMA user_version')\n"
                  "except sqlite3.ProgrammingError: print('closed',flush=True)\n"
                  "else: sys.exit(92)\n")
        process = subprocess.Popen([sys.executable, "-I", "-B", "-c", script,
                                    str(Path(__file__).resolve().parents[1]), str(state)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        receipt = root / "lifecycle-client.json"
        receipt.write_text(json.dumps({"pid": process.pid, "state": str(state),
                                       "returncode": None}) + "\n")
        receipt.chmod(0o600)
        lock_fd = None
        settled = False

        def line():
            self.assertTrue(select.select([process.stdout], [], [], 5)[0], "lifecycle barrier timed out")
            return process.stdout.readline().strip()

        try:
            self.assertEqual(line(), "connected")
            lock_fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
            fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            before = {name: (state / name).read_bytes() for name in ("state.db", "state.db-wal")}
            process.stdin.write("close\n")
            process.stdin.flush()
            self.assertEqual(line(), "closing")
            started = time.monotonic()
            premature = select.select([process.stdout], [], [], 12)[0]
            self.assertGreater(time.monotonic() - started, 10)
            self.assertFalse(premature, "close completed or failed while the reader still held flock")
            self.assertIsNone(process.poll(), "connection owner abandoned terminal close")
            for name, data in before.items():
                self.assertEqual((state / name).read_bytes(), data)
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
            lock_fd = None
            stdout, stderr = process.communicate(timeout=5)
            receipt.write_text(json.dumps({"pid": process.pid, "state": str(state),
                                           "returncode": process.returncode,
                                           "stdout": stdout, "stderr": stderr}) + "\n")
            settled = True
            self.assertEqual(process.returncode, 0, (stdout, stderr))
            self.assertEqual(stdout.strip(), "closed")
            self.assertFalse((state / "state.db-wal").exists())
            self.assertFalse((state / "state.db-shm").exists())
        finally:
            if lock_fd is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            if not settled:
                try:
                    stdout, stderr = process.communicate(
                        "close\n" if not process.stdin.closed else None, timeout=5)
                    receipt.write_text(json.dumps({"pid": process.pid, "state": str(state),
                                                   "returncode": process.returncode,
                                                   "stdout": stdout, "stderr": stderr}) + "\n")
                    settled = True
                except subprocess.TimeoutExpired as exc:
                    receipt.write_text(json.dumps({"pid": process.pid, "state": str(state),
                                                   "returncode": process.poll(),
                                                   "stdout": (exc.stdout or b"").decode(errors="replace"),
                                                   "stderr": (exc.stderr or b"").decode(errors="replace")}) + "\n")
                    self.fail("lifecycle process absence unconfirmed; retained {}".format(root))
            if settled:
                shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main()
