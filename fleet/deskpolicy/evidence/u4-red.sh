#!/usr/bin/env bash
# Run against the e056155b snapshot with only test_u4.py overlaid.
set -euo pipefail
exec taskset -c 0-1 python3 -I - <<'PY'
import sys
import unittest

sys.path.insert(0, 'fleet')
suite = unittest.defaultTestLoader.loadTestsFromName('deskpolicy.tests.test_u4.DayBridgeTest')
sys.exit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
PY
