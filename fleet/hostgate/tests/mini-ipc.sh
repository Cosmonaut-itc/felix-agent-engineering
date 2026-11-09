#!/bin/bash
set -eu
mkdir -p "$PWD/.ipc-fixtures" "$PWD/.ipc-pycache" "$PWD/.ipc-base"
export TMPDIR="$PWD/.ipc-fixtures" PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX="$PWD/.ipc-pycache"
git archive 039fab9 fleet/hostgate | tar -x -C "$PWD/.ipc-base"
result=0
for python in /usr/bin/python3 /opt/homebrew/bin/python3.14; do
  "$python" --version
  set +e
  printf '%s' '{"schema":"fleet/host-reservation@1","operationId":"baseline","operation":"estado"}' | PYTHONPATH="$PWD/.ipc-base/fleet" "$python" -B -m hostgate estado > "$PWD/.ipc-base/reply.json"
  code=$?
  set -e
  cat "$PWD/.ipc-base/reply.json"
  set +e
  "$python" -B -c 'import json,sys; r=json.load(open(sys.argv[1])); assert int(sys.argv[2]) == 5 and r["result"] == "shadow"; assert r["exitCode"] == 0 and r["configRevision"] is not None, ("authority observable", r["exitCode"], r["configRevision"])' "$PWD/.ipc-base/reply.json" "$code"
  red=$?
  set -e
  test "$red" = 1
  PYTHONPATH="$PWD/fleet" "$python" -B -c 'import os,unittest; from pathlib import Path; from hostgate.tests.test_admin import fixture_policy
with fixture_policy(Path(os.environ["TMPDIR"])):
    suite=unittest.defaultTestLoader.loadTestsFromNames(["hostgate.tests.test_ipc", "hostgate.tests.test_darwin_peer", "hostgate.tests.test_cli"])
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())' || result=1
 done
exit "$result"
