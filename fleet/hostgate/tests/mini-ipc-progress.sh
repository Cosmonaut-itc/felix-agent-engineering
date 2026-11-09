#!/bin/bash
set -eu
git rev-parse HEAD
mkdir -p "$PWD/.t" "$PWD/.ipc-cache"
export TMPDIR="$PWD/.t" PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX="$PWD/.ipc-cache" PYTHONPATH="$PWD/fleet"
result=0
for python in /usr/bin/python3 /opt/homebrew/bin/python3.14; do
  "$python" --version
  "$python" -B - <<'PY' || result=1
import os
import unittest
from pathlib import Path
from hostgate.tests.test_admin import fixture_policy
from hostgate.tests.test_ipc_progress import Progress
with fixture_policy(Path(os.environ['TMPDIR'])):
    suite = unittest.TestSuite(Progress(name) for name in Progress.__dict__ if name.startswith('test_'))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
PY
done
exit "$result"
