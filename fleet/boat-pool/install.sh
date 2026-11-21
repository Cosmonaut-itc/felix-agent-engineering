#!/bin/sh
set -eu
umask 077

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
lib="$HOME/.local/lib/boat-pool"
bin="$HOME/.local/bin"
agent="$HOME/Library/LaunchAgents/local.pao.boat-pool.plist"
domain="gui/$(id -u)"
job="$domain/local.pao.boat-pool"
state="${XDG_STATE_HOME:-$HOME/.local/state}/boat-pool"
config="${XDG_CONFIG_HOME:-$HOME/.config}/boat-pool"

mkdir -p "$lib" "$bin" "${agent%/*}" "$state" "$config"
cp "$source_dir/boat-pool" "$source_dir/boat_api.py" "$source_dir/pool.py" "$lib/"
ln -sfn "$lib/boat-pool" "$bin/boat-pool"
cp "$source_dir/launchagents/local.pao.boat-pool.plist" "$agent"
plutil -lint "$agent"

if [ ! -f "$config/pool.json" ]; then
  printf 'Configura %s antes de cargar el LaunchAgent.\n' "$config/pool.json" >&2
  exit 64
fi
descargar() {
  launchctl print "$job" >/dev/null 2>&1 || return 0
  launchctl bootout "$domain" "$agent"
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    launchctl print "$job" >/dev/null 2>&1 || return 0
    sleep 1
  done
  echo 'boat-pool: launchd no soltó el job en 10 s' >&2
  exit 1
}
descargar
launchctl bootstrap "$domain" "$agent"
