#!/usr/bin/env bash
set -eu

aislado=$(mktemp -d "${TMPDIR:-/tmp}/pao-boat-pool.XXXXXX")
trap 'rmdir "$aislado/home" "$aislado/state" "$aislado/config" "$aislado"' EXIT
export HOME="$aislado/home"
export XDG_STATE_HOME="$aislado/state"
export XDG_CONFIG_HOME="$aislado/config"
mkdir -p "$HOME" "$XDG_STATE_HOME" "$XDG_CONFIG_HOME"

python3 --version
python3 -IB -m unittest discover -s fleet/boat-pool/tests -v
