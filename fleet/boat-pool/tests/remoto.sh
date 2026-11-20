#!/usr/bin/env bash
set -eu

aislado=$(mktemp -d "${TMPDIR:-/tmp}/pao-boat-pool.XXXXXX")
trap 'rmdir "$aislado/home" "$aislado/state" "$aislado/config" "$aislado"' EXIT
export HOME="$aislado/home"
export XDG_STATE_HOME="$aislado/state"
export XDG_CONFIG_HOME="$aislado/config"
mkdir -p "$HOME" "$XDG_STATE_HOME" "$XDG_CONFIG_HOME"

/usr/bin/python3 --version
/usr/bin/python3 -IB -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else "Se requiere Python 3.9 o superior")'
/usr/bin/python3 -IB -m unittest discover -s fleet/boat-pool/tests -v
