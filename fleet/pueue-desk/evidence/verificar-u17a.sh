#!/usr/bin/env bash
# Corrida dirigida de U17a en la Mini (remote-run --name dirigida-u17a-<modelo>).
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX="$PWD/.u17a-pycache"
mkdir -p "$PWD/.u17a-tmp"
export TMPDIR="$PWD/.u17a-tmp"
printf '%s\n' 'COMANDO: /usr/bin/python3 -I -m unittest discover -s fleet/pueue-desk/tests -v'
/usr/bin/python3 -I -m unittest discover -s fleet/pueue-desk/tests -v
printf '%s\n' 'COMANDO: plutil -lint fleet/pueue-desk/launchagents/*.plist'
plutil -lint fleet/pueue-desk/launchagents/*.plist
printf '%s\n' 'COMANDO: shellcheck --severity=warning fleet/pueue-desk/*.sh fleet/pueue-desk/evidence/*.sh'
shellcheck --severity=warning fleet/pueue-desk/*.sh fleet/pueue-desk/evidence/*.sh
printf '%s\n' 'ShellCheck: OK (código 0)'
printf 'DURACIÓN: %s s\n' "$SECONDS"
printf '%s\n' 'VALIDACIÓN U17a: OK'
