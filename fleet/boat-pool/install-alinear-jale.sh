#!/bin/sh
set -eu
umask 077

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
bin="$HOME/.local/bin"
label=local.pao.boat-alinear-jale
agent="$HOME/Library/LaunchAgents/$label.plist"
domain="gui/$(id -u)"
job="$domain/$label"
state="${XDG_STATE_HOME:-$HOME/.local/state}/boat-pool/alinear-jale"
config="${XDG_CONFIG_HOME:-$HOME/.config}/boat-pool"

mkdir -p "$bin" "${agent%/*}" "$state" "$config"
cp "$source_dir/alinear-jale" "$bin/"
cp "$source_dir/launchagents/$label.plist" "$agent"
plutil -lint "$agent"

if [ ! -e "$config/alinear-jale.json" ] && [ ! -e "$config/alinear-jale.example.json" ]; then
  cp "$source_dir/alinear-jale.example.json" "$config/"
fi

descargar() {
  launchctl print "$job" >/dev/null 2>&1 || return 0
  launchctl bootout "$domain" "$agent"
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    launchctl print "$job" >/dev/null 2>&1 || return 0
    sleep 1
  done
  echo 'alinear-jale: launchd no soltó el job en 10 s' >&2
  exit 1
}
descargar
launchctl bootstrap "$domain" "$agent"
