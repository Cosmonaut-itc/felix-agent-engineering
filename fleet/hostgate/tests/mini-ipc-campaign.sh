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
from hostgate.tests.test_ipc_campaign import Campaign
with fixture_policy(Path(os.environ['TMPDIR'])):
    suite = unittest.TestSuite(Campaign(name) for name in Campaign.__dict__ if name.startswith('test_'))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromNames([
        'hostgate.tests.test_ipc', 'hostgate.tests.test_darwin_peer',
        'hostgate.tests.test_store.Storage.test_commit_and_ack_exceptions_always_replay_one_debit',
        'hostgate.tests.test_store.Storage.test_replay_persists_expiry_without_renewing_or_regranting',
        'hostgate.tests.test_contract.Contract.test_b_g2_replay_requires_exact_peer_and_channel',
        'hostgate.tests.test_contract.Contract.test_budget_dimensions_and_exclusives',
        'hostgate.tests.test_contract.Contract.test_shadow_only_and_future_interfaces_closed']))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
PY
done
exit "$result"
