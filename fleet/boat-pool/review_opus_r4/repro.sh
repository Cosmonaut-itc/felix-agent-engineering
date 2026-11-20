#!/usr/bin/env bash
# Revisor B, ronda 4: suite del autor en el head y reproducciones. Las
# reproducciones deben fallar mientras el hallazgo siga abierto.
set -u
aislado=$(mktemp -d "${TMPDIR:-/tmp}/pao-boat-pool-repro.XXXXXX")
export HOME="$aislado/home" XDG_STATE_HOME="$aislado/state" XDG_CONFIG_HOME="$aislado/config"
mkdir -p "$HOME" "$XDG_STATE_HOME" "$XDG_CONFIG_HOME"
/usr/bin/python3 --version
echo '== suite del autor =='
/usr/bin/python3 -IB -m unittest discover -s fleet/boat-pool/tests 2>&1 | tail -4
suite=$?
echo '== reproducciones =='
/usr/bin/python3 -IB fleet/boat-pool/review_opus_r4/repro.py 2>&1
echo "fin: suite=$suite"
