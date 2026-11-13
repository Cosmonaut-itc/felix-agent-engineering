#!/usr/bin/env bash
set -euo pipefail
origen=$(cd "$(dirname "$0")" && pwd)
label=com.cosmonaut.pao-rack
puerto=8788
destino="$HOME/.local/lib/pao-rack"
plist="$HOME/Library/LaunchAgents/$label.plist"
if [ "$#" -gt 1 ]; then echo 'Demasiados argumentos' >&2; exit 2; fi
descargar() {
  launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1 || return 0
  launchctl bootout "gui/$(id -u)/$label"
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1 || return 0
    sleep 1
  done
  echo 'rack: launchd no soltó el job en 10 s' >&2
  exit 1
}
case "${1:-}" in
  --desinstalar)
    descargar
    rm -f "$plist" "$destino/snapshot.py" "$destino/serve.py" "$destino/index.html" "$destino/hosts.json"
    rmdir "$destino" 2>/dev/null || true
    exit ;;
  '') ;;
  *) echo 'uso: instalar.sh [--desinstalar]' >&2; exit 2 ;;
esac
tailscale=/Applications/Tailscale.app/Contents/MacOS/Tailscale
ip=$("$tailscale" ip -4)
temporal=$(mktemp -d)
trap 'rm -rf "$temporal"' EXIT
"$tailscale" status --json > "$temporal/status.json"
"$tailscale" serve status --json > "$temporal/serve.json"
"$tailscale" funnel status --json > "$temporal/funnel.json"
/usr/bin/python3 - "$ip" "$puerto" "$temporal/serve.json" "$temporal/funnel.json" <<'PY'
import ipaddress
import json
import re
import sys
if ipaddress.ip_address(sys.argv[1]) not in ipaddress.ip_network('100.64.0.0/10'):
    sys.exit('rack: Tailscale no devolvió una IP del tailnet')
def exposes(value):
    if isinstance(value, dict):
        return any(exposes(k) or exposes(v) for k, v in value.items())
    if isinstance(value, list):
        return any(exposes(v) for v in value)
    return bool(re.search(r'(?<!\d)' + sys.argv[2] + r'(?!\d)', str(value)))
for path in sys.argv[3:]:
    with open(path) as stream:
        if exposes(json.load(stream)):
            sys.exit('rack: Serve o Funnel mencionan el puerto ' + sys.argv[2] + '; elimina esa exposición antes de instalar')
PY
mkdir -p "$destino" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
for archivo in snapshot.py serve.py index.html hosts.json; do
  install -m 644 "$origen/$archivo" "$destino/$archivo"
done
/usr/bin/python3 - "$destino" "$plist" "$ip" "$puerto" "$temporal/status.json" <<'PY'
import json
import os
import plistlib
import sys
root, plist, ip, port, status_path = sys.argv[1:]
with open(status_path) as stream:
    status = json.load(stream)
owner = status['User'][str(status['Self']['UserID'])]['LoginName']
if not isinstance(owner, str) or not owner:
    sys.exit('rack: no se pudo resolver el dueño de la Mini')
name = status['Self']['DNSName'].rstrip('.')
if not name.endswith('.ts.net'):
    sys.exit('rack: Tailscale no devolvió un nombre MagicDNS')
with open(plist, 'wb') as stream:
    plistlib.dump(dict(Label='com.cosmonaut.pao-rack',
        ProgramArguments=['/usr/bin/python3', '-I', root + '/serve.py', '--bind', ip, '--port', port, '--owner', owner,
                           '--name', name, '--name', name.split('.')[0]],
        KeepAlive=True, RunAtLoad=True,
        StandardOutPath=os.path.expanduser('~/Library/Logs/pao-rack.log'),
        StandardErrorPath=os.path.expanduser('~/Library/Logs/pao-rack.log')), stream)
PY
descargar
if ! launchctl bootstrap "gui/$(id -u)" "$plist"; then
  sleep 1
  launchctl bootstrap "gui/$(id -u)" "$plist"
fi
