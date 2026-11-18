#!/usr/bin/env bash
set -euo pipefail
origen=$(cd "$(dirname "$0")" && pwd)
ssh_args=(-o ControlMaster=no -o ControlPath=none)
case "${1:-}" in
  --en-seco) accion=en-seco ;;
  '') accion=instalar ;;
  *) echo 'uso: instalar.sh [--en-seco]' >&2; exit 2 ;;
esac
[ "$#" -le 1 ] || { echo 'uso: instalar.sh [--en-seco]' >&2; exit 2; }
sha=$(git -C "$origen" rev-parse HEAD)
git -C "$origen" fetch -q --prune origin
if [ -z "$(git -C "$origen" branch -r --contains "$sha")" ]; then
  echo "publica $sha antes de instalar" >&2; exit 1
fi
en_main=no
if git -C "$origen" merge-base --is-ancestor "$sha" origin/main; then en_main=si; fi
base=scripts/hosts/gate-noche-disparo
if [ "$accion" = en-seco ]; then
  git -C "$origen" show "$sha:$base/disparar.sh" |
    ssh "${ssh_args[@]}" mini '/bin/bash -s -- --en-seco'
  exit
fi

# Se conservan los temporales para auditar los bytes publicados.
local_tmp=$(mktemp -d)
label=com.cosmonaut.gate-noche-disparo
for archivo in disparar.sh "$label.plist"; do
  git -C "$origen" show "$sha:$base/$archivo" > "$local_tmp/$archivo"
done
(cd "$local_tmp" && shasum -a 256 disparar.sh "$label.plist" > sumas)
blob=$(git -C "$origen" rev-parse "$sha:$base/disparar.sh")
printf 'sha=%s\nenMain=%s\nruta=%s/disparar.sh\nblob=%s\n' "$sha" "$en_main" "$base" "$blob" > "$local_tmp/origen"
remoto=$(ssh "${ssh_args[@]}" mini 'mktemp -d /tmp/gate-noche-disparo.XXXXXX' </dev/null)
scp "${ssh_args[@]}" "$local_tmp/disparar.sh" "$local_tmp/$label.plist" "$local_tmp/sumas" "$local_tmp/origen" "mini:$remoto/"
ssh "${ssh_args[@]}" mini "bash -s -- '$remoto'" <<'REMOTO'
set -euo pipefail
temporal=$1
label=com.cosmonaut.gate-noche-disparo
dominio="gui/$(id -u)"
plist="$HOME/Library/LaunchAgents/$label.plist"
(cd "$temporal" && shasum -a 256 -c sumas)
mkdir -p "$HOME/bin" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
/usr/bin/python3 -I - "$temporal/$label.plist" "$temporal/preparado.plist" <<'PY'
import os
import plistlib
import sys
with open(sys.argv[1], 'rb') as entrada:
    datos = plistlib.load(entrada)
datos['ProgramArguments'] = [s.replace('__HOME__', os.path.expanduser('~')) for s in datos['ProgramArguments']]
for clave in ('StandardOutPath', 'StandardErrorPath'):
    datos[clave] = datos[clave].replace('__HOME__', os.path.expanduser('~'))
with open(sys.argv[2], 'wb') as salida:
    plistlib.dump(datos, salida)
PY
# Sin acceso no se instala nada: launchd carga en el siguiente arranque
# cualquier plist que encuentre, y ese agente fallaría cada noche en silencio.
if ! /bin/bash "$temporal/disparar.sh" --en-seco; then
  echo 'No se instala: gh no puede ver el workflow activo.' >&2
  exit 1
fi
install -m 755 "$temporal/disparar.sh" "$HOME/bin/gate-noche-disparo.sh"
install -m 644 "$temporal/preparado.plist" "$plist"
cat "$temporal/origen" "$temporal/sumas" > "$HOME/bin/gate-noche-disparo.origen"
if launchctl print "$dominio/$label" >/dev/null 2>&1; then
  launchctl bootout "$dominio/$label"
fi
launchctl bootstrap "$dominio" "$plist"
REMOTO
