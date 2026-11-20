#!/usr/bin/env bash
set -u

aislado=$(mktemp -d "${TMPDIR:-/tmp}/pao-boat-pool-opus.XXXXXX")
export HOME="$aislado/home"
export XDG_STATE_HOME="$aislado/state"
export XDG_CONFIG_HOME="$aislado/config"
mkdir -p "$HOME" "$XDG_STATE_HOME" "$XDG_CONFIG_HOME"

python3 --version
python3 -IB fleet/boat-pool/tests/repro_opus.py
