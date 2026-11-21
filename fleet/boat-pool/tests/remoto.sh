#!/usr/bin/env bash
set -eu

find_python() {
    command -v "python$1" || uv python find --managed-python "$1" 2>/dev/null
}

python312=$(find_python 3.12) || python312=
python39=$(find_python 3.9) || python39=
if [[ -z "$python312" && -z "$python39" ]]; then
    python312=/usr/bin/python3
fi

aislado=$(mktemp -d "${TMPDIR:-/tmp}/pao-boat-pool.XXXXXX")
trap 'rmdir "$aislado/home" "$aislado/state" "$aislado/config" "$aislado"' EXIT
export HOME="$aislado/home"
export XDG_STATE_HOME="$aislado/state"
export XDG_CONFIG_HOME="$aislado/config"
mkdir -p "$HOME" "$XDG_STATE_HOME" "$XDG_CONFIG_HOME"

run_suite() {
    local python=$1
    shift
    "$python" --version &&
        "$python" -IB -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else "Se requiere Python 3.9 o superior")' &&
        "$python" -IB -m unittest discover -s fleet/boat-pool/tests -v "$@"
}

status=0
for python in "$python312" "$python39"; do
    if [[ -n "$python" ]]; then
        run_suite "$python" "$@" || status=1
    fi
done
exit "$status"
