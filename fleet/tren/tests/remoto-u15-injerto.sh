#!/usr/bin/env bash
set -eu
# Sólo desde remoto.sh en el host de pruebas; se conserva la evidencia.
tren_pruebas=$(mktemp -d "${TMPDIR:-/tmp}/pao-tren-u15-injerto-antes.XXXXXX")
tren_anterior="$tren_pruebas/anterior"
mkdir -p "$tren_anterior"
git archive 185e7a4638453902ce64c71813c1beccdf9d531a | tar -x -C "$tren_anterior"
cp fleet/tren/tests/test_u15_injerto.py "$tren_anterior/fleet/tren/tests/"
python3 -I -B - "$tren_anterior" <<'PY'
from pathlib import Path
import sys
import unittest

raiz = Path(sys.argv[1])
sys.path.insert(0, str(raiz / "fleet/tren/tests"))
suite = unittest.defaultTestLoader.loadTestsFromName("test_u15_injerto.RegresionesTests")
esperados = {caso.id() for caso in suite}
resultado = unittest.TextTestRunner(verbosity=2).run(suite)
fallidos = {caso.id().split(" (")[0] for caso, _ in resultado.failures}
if resultado.errors or resultado.testsRun != 3 or fallidos != esperados:
    raise SystemExit("ANTES: no se reprodujeron exactamente las tres regresiones U15 injerto")
for nombre in sorted(esperados):
    print("ANTES 185e7a46 FALLO ESPERADO:", nombre, flush=True)
print("Evidencia U15 injerto anterior:", raiz, flush=True)
PY
