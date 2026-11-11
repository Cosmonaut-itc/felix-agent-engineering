"""The creation bootstrap as a real process: ready first, then one valid byte or it never enters."""
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
    def launch(self, byte):
        root = Path(tempfile.mkdtemp(prefix='entry-'))
        self.addCleanup(shutil.rmtree, str(root), True)
        counter = root / 'counter'
        argv = [sys.executable, '-c', "import os,sys\nfd=os.open(sys.argv[1],os.O_WRONLY|os.O_APPEND|os.O_CREAT,0o600)\n"
                "os.write(fd,b'x'); os.fsync(fd); os.close(fd)", str(counter)]
        digest = hashlib.sha256(json.dumps(argv, separators=(',', ':')).encode()).hexdigest()
        go_r, go_w = os.pipe()
        ready_r, ready_w = os.pipe()
        try:
            child = subprocess.Popen([sys.executable, '-I', '-S', '-B', str(ENTRY), str(go_r), str(ready_w), digest,
                                      '--'] + argv, stdin=subprocess.DEVNULL, close_fds=True,
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
            code = child.wait(timeout=20)
        finally:
            os.close(ready_r)
            if go_w is not None:
                os.close(go_w)
            if child.poll() is None:
                child.kill()
                child.wait()
        return announced, code, len(counter.read_bytes()) if counter.exists() else 0

    def test_valid_byte_enters_once(self):
        self.assertEqual(self.launch(b'G'), (b'R', 0, 1))

    def test_eof_or_invalid_byte_never_enters(self):
        """Variante: enter_on_eof."""
        self.assertEqual(self.launch(b''), (b'R', 3, 0))
        self.assertEqual(self.launch(b'X'), (b'R', 3, 0))


if __name__ == '__main__':
    unittest.main()
