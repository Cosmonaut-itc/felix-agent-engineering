"""Run the corrected observer against the exact r5 runtime and its real bugs."""
import os
from pathlib import Path
import sys
import unittest

os.chdir('.audit/r8-r5-fixed')
sys.path.insert(0, str(Path.cwd() / 'fleet'))
from deskpolicy.store import code_digest

digest = code_digest()
print('R8 FIXED FIXTURE R5 module_sha256', digest, flush=True)
assert digest == '77a369608532867e785aa7eff8f496e9a2480ff7a3b8d3b98a4d826901e7200d', digest
prefix = 'deskpolicy.tests.test_r6.RoundSixTest.'
names = ['test_timely_ack_executes_exactly_once', 'test_late_ack_after_sigstop_never_executes',
         'test_eof_closes_immediately_with_long_deadline']
suite = unittest.defaultTestLoader.loadTestsFromNames([prefix + name for name in names])
result = unittest.TextTestRunner(verbosity=2).run(suite)
failed = {test.id().rsplit('.', 1)[1] for test, _ in result.failures}
assert result.testsRun == 3 and not result.errors, (result.testsRun, result.errors)
assert failed == set(names[1:]), failed
for _, failure in result.failures:
    assert 'preparation consumed' not in failure and 'sync fixture:' not in failure, failure
assert any('0 != 75' in failure for _, failure in result.failures)
assert any('EOF waited until the sync deadline' in failure for _, failure in result.failures)
print('R8 R5 EXPECTED: timely control PASS; late ACK executes and EOF waits until deadline, 2 behavior failures, 0 errors',
      flush=True)
