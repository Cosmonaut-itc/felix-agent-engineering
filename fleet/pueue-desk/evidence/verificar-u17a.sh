#!/usr/bin/env bash
set -euo pipefail
export PAO_PUEUE_PRIVATE=1
printf 'Instantánea: '
git rev-parse HEAD
printf 'Estado de trabajo:\n'
git status --short
/usr/bin/python3 --version
/opt/homebrew/bin/pueued --version
/opt/homebrew/bin/shellcheck --version
/usr/bin/python3 -I -m unittest discover -s fleet/pueue-desk/tests -v
/usr/bin/plutil -lint fleet/pueue-desk/launchagents/*.plist
/opt/homebrew/bin/shellcheck --severity=warning fleet/pueue-desk/*-desk.sh fleet/pueue-desk/*-mini.sh fleet/pueue-desk/evidence/verificar-u17a.sh
printf 'DIRIGIDA U17a: OK (unitarias, privadas, plist y shellcheck)\n'
