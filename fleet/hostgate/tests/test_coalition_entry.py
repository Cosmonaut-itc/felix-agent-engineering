"""The creation bootstrap as a real process: ready first, then one valid byte or it never enters."""
import errno
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import unittest

ENTRY = Path(__file__).resolve().parents[1] / 'coalition_entry.py'


class Bootstrap(unittest.TestCase):
    def launch(self, byte, extra=(), missing=False, exit_code=0, expected_tail=None,
               wrong_digest=False, empty=False, hold=False):
        root = Path(tempfile.mkdtemp(prefix='entry-'))
        self.addCleanup(shutil.rmtree, str(root), True)
        counter = root / 'counter'
        actual_argv = root / 'argv.json'
        argv = [sys.executable, '-c', "import json,os,sys\nfd=os.open(sys.argv[1],os.O_WRONLY|os.O_APPEND|os.O_CREAT,0o600)\n"
                "os.write(fd,b'x'); os.fsync(fd); os.close(fd)\n"
                "with open(sys.argv[2], 'w') as stream: json.dump(sys.argv[3:], stream, ensure_ascii=True)\n"
                + ("os.write(1,b'x'); os.read(0,1)\n" if hold else "")
                + "sys.exit(%d)" % exit_code, str(counter), str(actual_argv)] + list(extra)
        if missing:
            argv = [str(root / 'no-such-executable')]
        if empty:
            argv = ['']
        digest = hashlib.sha256(json.dumps(argv, separators=(',', ':')).encode()).hexdigest()
        if wrong_digest:
            digest = '0' * 64
        go_r, go_w = os.pipe()
        ready_r, ready_w = os.pipe()
        try:
            child = subprocess.Popen([sys.executable, '-I', '-S', '-B', str(ENTRY), str(go_r), str(ready_w), digest,
                                      '--'] + argv, stdin=subprocess.PIPE if hold else subprocess.DEVNULL,
                                     stdout=subprocess.PIPE if hold else None, close_fds=True,
                                     pass_fds=(go_r, ready_w), start_new_session=True)
        finally:
            os.close(go_r)
            os.close(ready_w)
        try:
            ready = select.select([ready_r], [], [], 20)[0]
            announced = os.read(ready_r, 1) if ready else b''
            self.assertFalse(counter.exists(), 'FACT: nothing enters before the byte')
            try:
                if byte:
                    os.write(go_w, byte)
            except BrokenPipeError:
                pass  # the bootstrap is already gone: it reports by its exit code and the counter
            os.close(go_w)
            go_w = None
            if hold:
                self.assertTrue(select.select([child.stdout], [], [], 20)[0], 'payload entry timeout')
                self.assertEqual(os.read(child.stdout.fileno(), 1), b'x')
                self.assertEqual(counter.read_bytes(), b'x')
                self.assertIsNone(child.poll(), 'payload must be alive before ready EOF')
            if expected_tail is not None:
                tail = b''
                while True:
                    self.assertTrue(select.select([ready_r], [], [], 20)[0], 'ready EOF timeout')
                    chunk = os.read(ready_r, 128)
                    if not chunk:
                        break
                    tail += chunk
                self.assertEqual(tail, expected_tail)
            if hold:
                self.assertIsNone(child.poll(), 'ready EOF must precede payload terminal')
                child.stdin.close()  # Libera la lectura sólo después de comprobar EOF.
            code = child.wait(timeout=20)
        finally:
            os.close(ready_r)
            if go_w is not None:
                os.close(go_w)
            if hold:
                child.stdin.close()  # También libera la carga si falla una aserción.
            if child.poll() is None:
                try:
                    child.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            if child.stdout is not None:
                child.stdout.close()
        self.actual_argv = json.loads(actual_argv.read_text()) if actual_argv.exists() else None
        return announced, code, len(counter.read_bytes()) if counter.exists() else 0

    def test_valid_byte_enters_once(self):
        self.assertEqual(self.launch(b'G'), (b'R', 0, 1))

    def test_non_ascii_and_surrogate_argv_enters_once(self):
        """Variante: digest_not_ascii, rewrite_argv_after_check."""
        self.assertEqual(self.launch(b'G', ('señal', '\udcff')), (b'R', 0, 1))
        self.assertEqual(self.actual_argv, ['señal', '\udcff'])

    def test_wrong_digest_never_announces_or_enters(self):
        """Variante: skip_argv_digest."""
        self.assertEqual(self.launch(b'G', wrong_digest=True), (b'', 2, 0))

    def test_exec_failure_differs_from_payload_exit_three(self):
        """Variante: exec_failure_silent."""
        self.assertEqual(self.launch(b'G', exit_code=3, expected_tail=b''), (b'R', 3, 1))
        self.assertEqual(self.launch(b'G', missing=True,
                                     expected_tail=('E%d\n' % errno.ENOENT).encode('ascii')), (b'R', 3, 0))

    def test_ready_eof_while_payload_is_alive(self):
        """Variante: inherit_ready."""
        self.assertEqual(self.launch(b'G', hold=True, expected_tail=b''), (b'R', 0, 1))

    def test_empty_executable_reports_einval(self):
        """Variante: exec_failure_only_oserror."""
        self.assertEqual(self.launch(b'G', empty=True,
                                     expected_tail=('E%d\n' % errno.EINVAL).encode('ascii')), (b'R', 3, 0))

    def test_eof_or_invalid_byte_never_enters(self):
        """Variante: enter_on_eof."""
        self.assertEqual(self.launch(b''), (b'R', 3, 0))
        self.assertEqual(self.launch(b'X'), (b'R', 3, 0))


if __name__ == '__main__':
    unittest.main()
