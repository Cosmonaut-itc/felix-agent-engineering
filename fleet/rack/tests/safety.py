"""Hard stop before tests could launch a shared host or installed controller."""
import os
from pathlib import Path
import sys
import tempfile

os.environ['TMPDIR'] = '/tmp'
tempfile.tempdir = '/tmp'


def audit(event, args):
    if event != 'subprocess.Popen':
        return
    executable, argv = args[:2]
    if isinstance(argv, str):
        raise AssertionError('Tests cannot launch a shell command string')
    values = [str(a) for a in argv]
    for value in values:
        name = Path(value).name.lower()
        if name in ('ssh', 'pueue', 'tailscale', 'run-control.py', 'pueue-control.py'):
            # Only generated fixtures in /tmp, never the installed binary.
            if not str(Path(value).resolve()).startswith(str(Path('/tmp').resolve()) + '/'):
                raise AssertionError('Attempt to execute a real host binary: ' + name)
    if any(Path(v).name == 'snapshot.py' for v in values):
        raise AssertionError('Snapshot CLI defaults can reach the real queue; use injected collect()')


sys.addaudithook(audit)
