#!/usr/bin/env bash
set -uo pipefail
case "$(pwd -P)" in "$HOME"/remote-run/work/*) ;; *) exit 75 ;; esac
r8_own=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
/usr/bin/taskset -c 0-1 /usr/bin/python3 -I fleet/deskpolicy/evidence/r8-gate.py "$r8_own" || exit $?
/usr/bin/taskset -c 0-1 /usr/bin/python3 -I - <<'PY'
import os, sys, unittest
from pathlib import Path
os.chdir('.audit/r8-base')
sys.path.insert(0, str(Path.cwd()/'fleet'))
from deskpolicy.store import code_digest
print('R8 BASE', code_digest(), 'CPUs', sorted(os.sched_getaffinity(0)), flush=True)
suite = unittest.defaultTestLoader.discover('fleet/deskpolicy/tests', pattern='test_r8.py', top_level_dir='fleet')
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
PY
r8_base=$?
/usr/bin/taskset -c 0-1 /usr/bin/python3 -I - <<'PY'
import os, sys, unittest
from pathlib import Path
os.chdir('.audit/r8-r5')
sys.path.insert(0, str(Path.cwd()/'fleet'))
from deskpolicy.store import code_digest
print('R8 OLD FIXTURE R5', code_digest(), flush=True)
suite = unittest.defaultTestLoader.loadTestsFromNames([
    'deskpolicy.tests.test_r6.RoundSixTest.test_timely_ack_executes_exactly_once',
    'deskpolicy.tests.test_r6.RoundSixTest.test_late_ack_after_sigstop_never_executes',
    'deskpolicy.tests.test_r6.RoundSixTest.test_eof_closes_immediately_with_long_deadline'])
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
PY
r8_fixture=$?
/usr/bin/taskset -c 0-1 /usr/bin/python3 -I fleet/deskpolicy/evidence/r7-cleanup.py
r8_cleanup=$?
echo "R8 BEFORE base=$r8_base old_fixture_r5=$r8_fixture cleanup=$r8_cleanup"
test "$r8_base" -eq 0 && test "$r8_fixture" -eq 0 && test "$r8_cleanup" -eq 0
