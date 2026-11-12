#!/usr/bin/env bash
# GREEN de U4 en el desk: suite completa de deskpolicy y ShellCheck del CI.
set -euo pipefail
bash fleet/deskpolicy/tests/desk-run.sh
shellcheck --severity=warning fleet/deskpolicy/deploy-desk.sh fleet/deskpolicy/withdraw-desk.sh fleet/deskpolicy/tests/desk-run.sh
