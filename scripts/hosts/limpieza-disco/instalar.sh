#!/usr/bin/env bash
set -euo pipefail
origen=$(cd "$(dirname "$0")" && pwd)
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
# Los bytes salen del commit, no del árbol: así el SHA registrado los identifica.
sha=$(git -C "$origen" rev-parse HEAD)
git -C "$origen" fetch -q origin
if [ -z "$(git -C "$origen" branch -r --contains "$sha")" ]; then
  echo "instalar.sh: $sha no está en ningún remoto; publícalo antes de instalar" >&2
  exit 1
fi
local_tmp=$(mktemp -d)
remoto=$(ssh "${ssh_args[@]}" mini 'mktemp -d /tmp/limpieza-disco.XXXXXX')
trap 'rm -rf -- "$local_tmp"; ssh "${ssh_args[@]}" mini "rm -rf -- '\''$remoto'\''"' EXIT
base=scripts/hosts
git -C "$origen" show "$sha:$base/limpieza-disco/limpieza-disco.sh" > "$local_tmp/limpieza-disco.sh"
git -C "$origen" show "$sha:$base/limpieza-disco/com.cosmonaut.limpieza-disco.plist" > "$local_tmp/com.cosmonaut.limpieza-disco.plist"
git -C "$origen" show "$sha:$base/inventario/inventario.py" > "$local_tmp/pao-inventario.py"
blob=$(git -C "$origen" rev-parse "$sha:$base/inventario/inventario.py")
(cd "$local_tmp" && shasum -a 256 limpieza-disco.sh com.cosmonaut.limpieza-disco.plist pao-inventario.py > sumas)
scp "${ssh_args[@]}" "$local_tmp/limpieza-disco.sh" "$local_tmp/com.cosmonaut.limpieza-disco.plist" \
  "$local_tmp/pao-inventario.py" "$local_tmp/sumas" "mini:$remoto/"
ssh "${ssh_args[@]}" mini "bash -s -- '$remoto' '$accion' '$sha' '$blob'" <<'REMOTO'
set -euo pipefail
temporal=$1
accion=$2
sha=$3
blob=$4
label=com.cosmonaut.limpieza-disco
dominio="gui/$(id -u)"
plist="$HOME/Library/LaunchAgents/$label.plist"
estado="$HOME/Library/Application Support/limpieza-disco"
lock="$estado/lock"
(cd "$temporal" && shasum -a 256 -c sumas >/dev/null)
if [ "$accion" = en-seco ]; then
  trap 'rm -rf "$temporal"' EXIT
  bash "$temporal/limpieza-disco.sh" --dry-run
  exit
fi
if [ "$HOME" != /Users/cosmonaut ]; then
  echo "limpieza-disco: el plist fija /Users/cosmonaut y HOME es $HOME" >&2
  exit 1
fi
mkdir -p "$estado"
# El mismo lock que la limpieza: un bootout a mitad de pasada cortaría un rm -rf.
if ! mkdir "$lock" 2>/dev/null; then
  echo 'limpieza-disco: hay una pasada o una instalación en curso; reintenta cuando termine' >&2
  exit 1
fi
tengo_lock=1
descargado=0
cerrar() {
  if [ "$tengo_lock" = 1 ]; then rmdir "$lock" 2>/dev/null || true; fi
  if [ "$descargado" = 1 ] && ! launchctl print "$dominio/$label" >/dev/null 2>&1; then
    echo 'limpieza-disco: la instalación falló; recargo el plist que haya en sitio' >&2
    launchctl bootstrap "$dominio" "$plist" || echo 'limpieza-disco: el LaunchAgent quedó descargado' >&2
  fi
  rm -rf "$temporal"
}
trap cerrar EXIT
if launchctl print "$dominio/$label" >/dev/null 2>&1; then
  launchctl bootout "$dominio/$label"
  descargado=1
  transcurrido=0
  while launchctl print "$dominio/$label" >/dev/null 2>&1; do
    if [ "$transcurrido" -ge 10 ]; then
      echo 'limpieza-disco: launchd no soltó el job en 10 s' >&2
      exit 1
    fi
    sleep 1
    transcurrido=$((transcurrido + 1))
  done
fi
respaldo=.bak-$(date +%Y%m%d)
if [ -e "$HOME/bin/limpieza-disco.sh" ] && [ ! -e "$HOME/bin/limpieza-disco.sh$respaldo" ]; then
  cp -p "$HOME/bin/limpieza-disco.sh" "$HOME/bin/limpieza-disco.sh$respaldo"
fi
# Fuera de LaunchAgents, para que launchd no lo cargue como un segundo job.
if [ -e "$plist" ] && [ ! -e "$estado/$label.plist$respaldo" ]; then
  cp -p "$plist" "$estado/$label.plist$respaldo"
fi
mkdir -p "$HOME/bin" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
install -m 755 "$temporal/pao-inventario.py" "$HOME/bin/pao-inventario.py"
printf 'repo=personal-agent-orchestration\nsha=%s\nruta=scripts/hosts/inventario/inventario.py\nblob=%s\nsha256=%s\ninstalado=%s\n' \
  "$sha" "$blob" "$(shasum -a 256 "$HOME/bin/pao-inventario.py" | cut -d' ' -f1)" "$(date -u +%FT%TZ)" \
  > "$HOME/bin/pao-inventario.origen"
install -m 755 "$temporal/limpieza-disco.sh" "$HOME/bin/limpieza-disco.sh"
install -m 644 "$temporal/$label.plist" "$plist"
# Se suelta antes del bootstrap: la pasada de RunAtLoad necesita el lock.
rmdir "$lock"
tengo_lock=0
if ! launchctl bootstrap "$dominio" "$plist"; then
  sleep 1
  launchctl bootstrap "$dominio" "$plist"
fi
descargado=0
REMOTO
