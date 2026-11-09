#!/bin/bash
set -eu
mkdir -p "$PWD/.ipc-fixtures" "$PWD/.ipc-pycache" "$PWD/.ipc-base"
export TMPDIR="$PWD/.ipc-fixtures" PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX="$PWD/.ipc-pycache"
git archive 039fab9 fleet/hostgate | tar -x -C "$PWD/.ipc-base"
for python in /usr/bin/python3 /opt/homebrew/bin/python3.14; do
  "$python" --version
  set +e
  printf '%s' '{"schema":"fleet/host-reservation@1","operationId":"baseline","operation":"estado"}' | PYTHONPATH="$PWD/.ipc-base/fleet" "$python" -B -m hostgate estado > "$PWD/.ipc-base/reply.json"
  code=$?
  set -e
  cat "$PWD/.ipc-base/reply.json"
  "$python" -B -c 'import json,sys; r=json.load(open(sys.argv[1])); assert int(sys.argv[2]) == 5 and r["configRevision"] is None and r["result"] == "shadow"; print("RED semantic: expected exit 0 and administrative revision; got exit 5 / None")' "$PWD/.ipc-base/reply.json" "$code"
  PYTHONPATH="$PWD/fleet" "$python" -B -m unittest -v hostgate.tests.test_ipc hostgate.tests.test_darwin_peer hostgate.tests.test_cli
 done
