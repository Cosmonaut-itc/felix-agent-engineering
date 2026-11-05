"""Reproduce the r6 timing nit with five seconds of receipt-storage delay.

Run from either the exact baseline (old tests) or the corrected checkout.
Only the first receipt publication of each run is delayed. No clock, sync
operation, protocol result or assertion is replaced.
"""
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path.cwd() / 'fleet'))
from deskpolicy import cli
from deskpolicy.tests.test_r6 import RoundSixTest

atomic = cli.atomic_json
seen = set()

def slow_receipt(path, value, **kwargs):
    if path.name == 'pao-admission.json' and path not in seen:
        seen.add(path)
        time.sleep(5)
    return atomic(path, value, **kwargs)

names = ['test_eof_closes_immediately_with_long_deadline', 'test_late_ack_after_sigstop_never_executes']
with mock.patch.object(cli, 'atomic_json', side_effect=slow_receipt):
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(RoundSixTest(n) for n in names))
raise SystemExit(not result.wasSuccessful())
