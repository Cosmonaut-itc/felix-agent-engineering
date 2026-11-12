#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
mkdir -p "$PWD/.u17-tmp"
export TMPDIR="$PWD/.u17-tmp"
printf '%s\n' 'COMANDO: /usr/bin/python3 -I -m unittest discover -s fleet/pueue-desk/tests -v'
/usr/bin/python3 -I -m unittest discover -s fleet/pueue-desk/tests -v
printf '%s\n' 'COMANDO: plutil -lint fleet/pueue-desk/launchagents/io.pao.pueue.plist'
plutil -lint fleet/pueue-desk/launchagents/io.pao.pueue.plist
printf '%s\n' 'COMANDO: ShellCheck de CI y de U17'
shellcheck --severity=warning skills/felix-mode/scripts/pstack-refs tests/pstack-refs-test.sh bin/pao tests/pao-test.sh skills/felix-mode/scripts/codex-run skills/felix-mode/scripts/laptop-guard skills/felix-mode/scripts/merge-guard skills/felix-mode/scripts/store-guard skills/felix-mode/scripts/risk-classify skills/felix-mode/scripts/apto-vigente tests/felix-mode-scripts-test.sh configs/claude/hooks/duplicate-session-guard tests/duplicate-session-guard-test.sh fleet/deskpolicy/deploy-desk.sh fleet/deskpolicy/withdraw-desk.sh fleet/deskpolicy/tests/desk-run.sh
shellcheck --severity=warning fleet/pueue-desk/*.sh fleet/pueue-desk/evidence/*.sh
printf '%s\n' 'ShellCheck CI + U17: OK (código 0)'
printf '%s\n' 'COMANDO: bin/pao check (validación de CI)'
XDG_STATE_HOME="$PWD/.u17-tmp/ci-state" bin/pao check
printf '%s\n' 'COMANDO: /usr/bin/python3 -I fleet/pueue-desk/evidence/verificar-supervisor.py'
/usr/bin/python3 -I fleet/pueue-desk/evidence/verificar-supervisor.py
printf '%s\n' 'VALIDACIÓN U17 R2: OK'
