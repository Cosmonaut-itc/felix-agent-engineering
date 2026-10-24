#!/usr/bin/env bash
set -euo pipefail
origen=$(cd "$(dirname "$0")" && pwd)
ssh_args=(-o ControlMaster=no -o ControlPath=none)
case "${1:-}" in
  --desinstalar) accion=desinstalar ;;
  '') accion=instalar ;;
  *) echo 'uso: instalar.sh [--desinstalar]' >&2; exit 2 ;;
esac
if [ "$#" -gt 1 ]; then
  echo 'uso: instalar.sh [--desinstalar]' >&2
  exit 2
fi
remoto=$(ssh "${ssh_args[@]}" mini 'mktemp -d /tmp/pao-mini-vigilante.XXXXXX')
trap 'ssh "${ssh_args[@]}" mini "rm -rf -- '\''$remoto'\''"' EXIT
if [ "$accion" = instalar ]; then
  scp "${ssh_args[@]}" "$origen/muestra.py" "$origen/umbrales.json" "$origen/com.cosmonaut.pao-mini-vigilante.plist" "mini:$remoto/"
fi
ssh "${ssh_args[@]}" mini "bash -s -- '$remoto' '$accion'" <<'REMOTO'
set -euo pipefail
temporal=$1
accion=$2
trap 'rm -rf "$temporal"' EXIT
label=com.cosmonaut.pao-mini-vigilante
if [ "$accion" = instalar ]; then
  mkdir -p "$HOME/bin" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
  /usr/bin/python3 - "$temporal/$label.plist" "$temporal/preparado.plist" <<'PY'
import os
import plistlib
import sys
with open(sys.argv[1], 'rb') as stream:
    plist = plistlib.load(stream)
plist['ProgramArguments'][1] = plist['ProgramArguments'][1].replace('__HOME__', os.path.expanduser('~'))
for key in ('StandardOutPath', 'StandardErrorPath'):
    plist[key] = plist[key].replace('__HOME__', os.path.expanduser('~'))
with open(sys.argv[2], 'wb') as stream:
    plistlib.dump(plist, stream)
PY
fi
if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
  launchctl bootout "gui/$(id -u)/$label"
  transcurrido=0
  while launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; do
    if [ "$transcurrido" -ge 10 ]; then
      echo 'mini-vigilante: launchd no soltó el job en 10 s' >&2
      exit 1
    fi
    sleep 1
    transcurrido=$((transcurrido + 1))
  done
fi
if [ "$accion" = desinstalar ]; then
  rm -f "$HOME/Library/LaunchAgents/$label.plist" "$HOME/bin/pao-mini-vigilante.py" "$HOME/bin/pao-mini-vigilante-umbrales.json"
  exit
fi
install -m 755 "$temporal/muestra.py" "$HOME/bin/pao-mini-vigilante.py"
install -m 644 "$temporal/umbrales.json" "$HOME/bin/pao-mini-vigilante-umbrales.json"
install -m 644 "$temporal/preparado.plist" "$HOME/Library/LaunchAgents/$label.plist"
if ! launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$label.plist"; then
  sleep 1
  launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$label.plist"
fi
REMOTO
