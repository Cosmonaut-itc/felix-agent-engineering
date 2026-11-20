#!/bin/sh
set -eu
umask 077

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
lib="$HOME/.local/lib/boat-pool"
bin="$HOME/.local/bin"
agent="$HOME/Library/LaunchAgents/local.pao.boat-pool.plist"
domain="gui/$(id -u)"
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
if launchctl print "$domain/local.pao.boat-pool" >/dev/null 2>&1; then
  launchctl bootout "$domain" "$agent"
fi
launchctl bootstrap "$domain" "$agent"
