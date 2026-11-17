#!/usr/bin/env bash
set -euo pipefail
origen=$(cd "$(dirname "$0")" && pwd)
case "${1:-}" in
  --en-seco) accion=en-seco ;;
  '') accion=instalar ;;
  *) echo 'uso: instalar.sh [--en-seco]' >&2; exit 2 ;;
esac
[ "$#" -le 1 ] || exit 2
sha=$(git -C "$origen" rev-parse HEAD)
git -C "$origen" fetch -q --prune origin
if [ -z "$(git -C "$origen" branch -r --contains "$sha")" ]; then
  echo "publica $sha antes de instalar" >&2; exit 1
fi
en_main=no
if git -C "$origen" merge-base --is-ancestor "$sha" origin/main; then en_main=si; fi
base=scripts/hosts/fseventsd-guardia
if [ "$accion" = en-seco ]; then
  git -C "$origen" show "$sha:$base/guardia.py" |
    ssh -o ControlMaster=no -o ControlPath=none mini '/usr/bin/python3 -I - --dry-run'
  exit
fi
# Imprimir comandos evita transportar o almacenar una contraseña sudo.
local_tmp=$(mktemp -d)
label=com.cosmonaut.fseventsd-guardia
for archivo in guardia.py "$label.plist"; do
  git -C "$origen" show "$sha:$base/$archivo" > "$local_tmp/$archivo"
done
(cd "$local_tmp" && shasum -a 256 guardia.py "$label.plist" > sumas)
blob=$(git -C "$origen" rev-parse "$sha:$base/guardia.py")
printf 'sha=%s\nenMain=%s\nruta=%s/guardia.py\nblob=%s\n' "$sha" "$en_main" "$base" "$blob" > "$local_tmp/origen"
cat "$local_tmp/sumas" >> "$local_tmp/origen"
/usr/bin/python3 -I - "$local_tmp" <<'PY'
import base64
import hashlib
from pathlib import Path
import shlex
import sys
fuente = Path(sys.argv[1])
raiz = '/Library/Application Support/pao/fseventsd-guardia'
label = 'com.cosmonaut.fseventsd-guardia'
q = shlex.quote
print('# Ejecutar este bloque en una terminal de la Mini; sudo pide la contraseña allí.')
print('set -e')
print('if sudo launchctl print system/' + label + ' >/dev/null 2>&1; then sudo launchctl bootout system/' + label + '; fi')
print('sudo install -d -o root -g wheel -m 755 ' + q('/Library/Application Support/pao') + ' ' + q(raiz))
for nombre in ('guardia.py', label + '.plist', 'origen'):
    datos = (fuente / nombre).read_bytes()
    destino = raiz + '/' + nombre
    print('printf %s ' + q(base64.b64encode(datos).decode()) + ' | /usr/bin/base64 -D | sudo tee ' + q(destino) + ' >/dev/null')
    print('sudo chown root:wheel ' + q(destino))
    print('sudo chmod ' + ('755' if nombre == 'guardia.py' else '644') + ' ' + q(destino))
    print('printf ' + q(hashlib.sha256(datos).hexdigest() + '  ' + destino + '\n') + ' | shasum -a 256 -c -')
plist = '/Library/LaunchDaemons/' + label + '.plist'
print('sudo install -o root -g wheel -m 644 ' + q(raiz + '/' + label + '.plist') + ' ' + q(plist))
print('sudo launchctl bootstrap system ' + q(plist))
PY
