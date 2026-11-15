#!/usr/bin/env bash
set -eu
# Fixtures retenidas; nunca usar el HOME/config/estado del operador.
tren_pruebas=$(mktemp -d)
export HOME="$tren_pruebas/home"
export XDG_CONFIG_HOME="$tren_pruebas/config"
export XDG_STATE_HOME="$tren_pruebas/state"
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
mkdir -p "$HOME" "$XDG_CONFIG_HOME" "$XDG_STATE_HOME"
bash fleet/tren/tests/remoto.sh
bash tests/felix-mode-scripts-test.sh
