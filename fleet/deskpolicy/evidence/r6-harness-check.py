"""Verify baseline error discrimination and the public lane-registration CLI."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path.cwd() / 'fleet'))
from deskpolicy.tests.test_policy import PolicyTest
from deskpolicy.store import read_json

wrapper = Path('fleet/deskpolicy/evidence/r6-full.sh').read_text()
block = wrapper[wrapper.index('if [ -f .audit/r6-baseline/'):wrapper.index('exec /usr/bin/taskset')]
subprocess.run(['/bin/bash', '-n', 'fleet/deskpolicy/evidence/r6-full.sh'], check=True)
with tempfile.TemporaryDirectory(prefix='.r6-wrapper-', dir=Path.cwd()) as directory:
    root = Path(directory)
    baseline = root / '.audit/r6-baseline/fleet/deskpolicy/tests'
    (baseline / '__pycache__').mkdir(parents=True)
    skipped = subprocess.run(['/bin/bash', '-e', '-c', block], cwd=root, capture_output=True, text=True)
    assert skipped.returncode == 0 and not skipped.stdout and not skipped.stderr, skipped
    print('HARNESS cache-only baseline skipped, rc 0', flush=True)
    (baseline / 'test_r6.py').write_text('raise RuntimeError("fixture-import")\n')
    failed = subprocess.run(['/bin/bash', '-e', '-c', block], cwd=root, capture_output=True, text=True)
    assert failed.returncode != 0 and 'R6 BASELINE RC=2' in failed.stdout, failed
    print('HARNESS missing runtime rejected as baseline rc 2', flush=True)
    package = baseline.parent
    (package / '__init__.py').write_text('')
    (baseline / '__init__.py').write_text('')
    (package / 'store.py').write_text('def code_digest():\n    return "77a369608532867e785aa7eff8f496e9a2480ff7a3b8d3b98a4d826901e7200d"\n')
    failed_test = subprocess.run(['/bin/bash', '-e', '-c', block], cwd=root, capture_output=True, text=True)
    assert failed_test.returncode != 0 and 'R6 BASELINE RC=2' in failed_test.stdout, failed_test
    assert 'fixture-import' in failed_test.stderr, failed_test
    print('HARNESS test import error rejected as baseline rc 2', flush=True)

case = PolicyTest()
case.setUp()
try:
    args = ['pendiente', '--request-id', 'cli-lane', '--project', 'hasito', '--kind', 'script',
            '--owner', 'test', '--weight', '1', '--lane', '2']
    admitted = case.run_cli(args)
    assert admitted['rc'] == 0 and json.loads(admitted['output'])['lane'] == 2, admitted
    assert case.run_cli(args)['rc'] == 0
    wrong = case.run_cli(args[:-1] + ['1'])
    assert wrong['rc'] == 75 and json.loads(wrong['output'])['detail'] == 'request-conflict', wrong
    record = read_json(case.engine.record_path('cli-lane'))
    assert record['lane'] == 2 and record['claim'] is None, record
    print('CLI lane 2 registered and renewed; change to lane 1 rejected without claim', flush=True)
finally:
    case.tearDown()
