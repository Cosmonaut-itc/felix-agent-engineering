#!/usr/bin/env bash
set -eu
# Sólo en el host de pruebas.
tren_pruebas=$(mktemp -d "${TMPDIR:-/tmp}/pao-tren-u15-r1-antes.XXXXXX")
trap 'rm -rf -- "$tren_pruebas"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
export TMPDIR="$tren_pruebas/tmp"
mkdir -p "$TMPDIR"
tren_anterior="$tren_pruebas/anterior"
mkdir -p "$tren_anterior"
git archive e74bfdb1e4b10caf68a66cbc56104af44f659057 | tar -x -C "$tren_anterior"
cp fleet/tren/tests/test_u15_r1.py "$tren_anterior/fleet/tren/tests/"
python3 -I -B - "$tren_anterior" <<'PY'
from pathlib import Path
import sys
import unittest

raiz = Path(sys.argv[1])
sys.path.insert(0, str(raiz / "fleet/tren/tests"))
suite = unittest.defaultTestLoader.loadTestsFromName("test_u15_r1.RegresionesTests")
esperados = {caso.id() for caso in suite}
resultado = unittest.TextTestRunner(verbosity=2).run(suite)
fallidos = {caso.id().split(" (")[0] for caso, _ in resultado.failures}
if resultado.errors or resultado.testsRun != 2 or fallidos != esperados:
    raise SystemExit("ANTES: no se reprodujeron exactamente las dos regresiones U15 R1")
for nombre in sorted(esperados):
    print("ANTES e74bfdb1 FALLO ESPERADO:", nombre, flush=True)
print("Evidencia U15 R1 anterior:", raiz, flush=True)
PY
