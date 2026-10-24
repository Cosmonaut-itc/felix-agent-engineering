#!/usr/bin/env bash
set -euo pipefail
origen=$(cd "$(dirname "$0")" && pwd)
inventario="$origen/../inventario/inventario.py"
ssh_args=(-o ControlMaster=no -o ControlPath=none)
case "${1:-}" in
  --en-seco) accion=en-seco ;;
  '') accion=instalar ;;
  *) echo 'uso: instalar.sh [--en-seco]' >&2; exit 2 ;;
esac
if [ "$#" -gt 1 ]; then
  echo 'uso: instalar.sh [--en-seco]' >&2
  exit 2
fi
# El SHA registrado sólo identifica la copia instalada si coincide con el árbol.
if ! git -C "$origen" diff --quiet HEAD -- "$origen" "$inventario"; then
  echo 'instalar.sh: hay cambios sin commit en limpieza-disco o inventario' >&2
  exit 1
fi
sha=$(git -C "$origen" rev-parse HEAD)
remoto=$(ssh "${ssh_args[@]}" mini 'mktemp -d /tmp/limpieza-disco.XXXXXX')
trap 'ssh "${ssh_args[@]}" mini "rm -rf -- '\''$remoto'\''"' EXIT
scp "${ssh_args[@]}" "$origen/limpieza-disco.sh" "$origen/com.cosmonaut.limpieza-disco.plist" "mini:$remoto/"
scp "${ssh_args[@]}" "$inventario" "mini:$remoto/pao-inventario.py"
ssh "${ssh_args[@]}" mini "bash -s -- '$remoto' '$accion' '$sha'" <<'REMOTO'
set -euo pipefail
temporal=$1
accion=$2
sha=$3
trap 'rm -rf "$temporal"' EXIT
label=com.cosmonaut.limpieza-disco
if [ "$accion" = en-seco ]; then
  bash "$temporal/limpieza-disco.sh" --dry-run
  exit
fi
if [ "$HOME" != /Users/cosmonaut ]; then
  echo "limpieza-disco: el plist fija /Users/cosmonaut y HOME es $HOME" >&2
  exit 1
fi
# Un bootout a mitad de pasada cortaría un rm -rf.
if [ -d "$HOME/Library/Application Support/limpieza-disco/lock" ]; then
  echo 'limpieza-disco: hay una pasada en curso; reintenta cuando termine' >&2
  exit 1
fi
if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
  launchctl bootout "gui/$(id -u)/$label"
  transcurrido=0
  while launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; do
    if [ "$transcurrido" -ge 10 ]; then
      echo 'limpieza-disco: launchd no soltó el job en 10 s' >&2
      exit 1
    fi
    sleep 1
    transcurrido=$((transcurrido + 1))
  done
fi
respaldo=.bak-$(date +%Y%m%d)
for f in "$HOME/bin/limpieza-disco.sh" "$HOME/Library/LaunchAgents/$label.plist"; do
  if [ -e "$f" ] && [ ! -e "$f$respaldo" ]; then
    cp -p "$f" "$f$respaldo"
  fi
done
mkdir -p "$HOME/bin" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
install -m 755 "$temporal/pao-inventario.py" "$HOME/bin/pao-inventario.py"
printf 'repo=personal-agent-orchestration\nsha=%s\nruta=scripts/hosts/inventario/inventario.py\ninstalado=%s\n' \
  "$sha" "$(date -u +%FT%TZ)" > "$HOME/bin/pao-inventario.origen"
install -m 755 "$temporal/limpieza-disco.sh" "$HOME/bin/limpieza-disco.sh"
install -m 644 "$temporal/$label.plist" "$HOME/Library/LaunchAgents/$label.plist"
if ! launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$label.plist"; then
  sleep 1
  launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$label.plist"
fi
REMOTO
