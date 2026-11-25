"""Hard stop before tests could launch a shared host or installed controller."""
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

os.environ['TMPDIR'] = '/tmp'
tempfile.tempdir = '/tmp'
DESK_DESTINATION = 'u@desk.example'


def write_desk_config(root):
    config_home = root / 'config'
    dest = config_home / 'pao/rack-desk-ssh'
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(DESK_DESTINATION + '\n')
    dest.chmod(0o600)
    return {'XDG_CONFIG_HOME': str(config_home)}


def desk_config(test, root):
    env = patch.dict(os.environ, write_desk_config(root))
    env.start()
    test.addCleanup(env.stop)


def audit(event, args):
    if event != 'subprocess.Popen':
        return
    executable, argv = args[:2]
    if isinstance(argv, str):
        raise AssertionError('Tests cannot launch a shell command string')
    values = [str(a) for a in argv]
    for value in values:
        name = Path(value).name.lower()
        if name in ('ssh', 'pueue', 'pueued', 'systemctl', 'systemd-run', 'tailscale', 'run-control.py', 'pueue-control.py', 'pao-rack-action', 'control.py'):
            # Only generated fixtures in /tmp, never the installed binary.
            if not str(Path(value).resolve()).startswith(str(Path('/tmp').resolve()) + '/'):
                raise AssertionError('Attempt to execute a real host binary: ' + name)
    if any(Path(v).name == 'snapshot.py' for v in values):
        raise AssertionError('Snapshot CLI defaults can reach the real queue; use injected collect()')


sys.addaudithook(audit)
