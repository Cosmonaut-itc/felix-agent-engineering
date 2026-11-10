#!/usr/bin/env bash
set -euo pipefail
case "$(pwd -P)" in "$HOME"/remote-run/work/*) ;; *) exit 75 ;; esac
r8_own=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
/usr/bin/taskset -c 0-1 /usr/bin/python3 -I fleet/deskpolicy/evidence/r8-gate.py "$r8_own"
exec /usr/bin/taskset -c 0-1 /bin/bash -c '
set -euo pipefail
sha256sum --quiet -c fleet/deskpolicy/evidence/r8-inputs.sha256
/usr/bin/python3 -I fleet/deskpolicy/evidence/r8-r5-fixture.py
/usr/bin/python3 -I fleet/deskpolicy/evidence/r7-fixture-schedules.py
/usr/bin/python3 -I fleet/deskpolicy/tests/run.py
bin/pao check
/usr/bin/python3 -I fleet/deskpolicy/evidence/r7-cleanup.py
if command -v shellcheck >/dev/null; then
    shellcheck --severity=warning fleet/deskpolicy/deploy-desk.sh fleet/deskpolicy/withdraw-desk.sh fleet/deskpolicy/tests/desk-run.sh fleet/deskpolicy/evidence/r8-full.sh fleet/deskpolicy/evidence/r8-before.sh
fi
'
