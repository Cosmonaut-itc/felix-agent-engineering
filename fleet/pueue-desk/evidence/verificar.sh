#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX="$PWD/.u17-pycache"
mkdir -p "$PWD/.u17-tmp"
export TMPDIR="$PWD/.u17-tmp"
printf '%s\n' 'COMANDO: /usr/bin/python3 -I -m unittest discover -s fleet/pueue-desk/tests -v'
/usr/bin/python3 -I -m unittest discover -s fleet/pueue-desk/tests -v
printf '%s\n' 'COMANDO: plutil -lint fleet/pueue-desk/launchagents/io.pao.pueue.plist'
plutil -lint fleet/pueue-desk/launchagents/io.pao.pueue.plist
printf '%s\n' 'COMANDO: shellcheck --severity=warning fleet/pueue-desk/*.sh fleet/pueue-desk/evidence/*.sh'
shellcheck --severity=warning fleet/pueue-desk/*.sh fleet/pueue-desk/evidence/*.sh
printf '%s\n' 'ShellCheck: OK (código 0)'
printf '%s\n' 'COMANDO: /usr/bin/python3 -I fleet/pueue-desk/evidence/verificar-supervisor.py'
/usr/bin/python3 -I fleet/pueue-desk/evidence/verificar-supervisor.py
printf '%s\n' 'VALIDACIÓN U17: OK'
