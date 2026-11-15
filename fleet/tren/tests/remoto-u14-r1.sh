#!/usr/bin/env bash
set -eu
# Sólo desde remoto.sh en el host de pruebas; se conserva la evidencia.
tren_pruebas=$(mktemp -d "${TMPDIR:-/tmp}/pao-tren-u14-r1-antes.XXXXXX")
tren_anterior="$tren_pruebas/anterior"
mkdir -p "$tren_anterior"
git archive 49fd41dc70e1de943c03e731bf2a19d0a52ba4b1 | tar -x -C "$tren_anterior"
cp fleet/tren/tests/test_u14_r1.py "$tren_anterior/fleet/tren/tests/"
python3 -I -B - "$tren_anterior" <<'PY'
from pathlib import Path
import sys
import unittest

raiz = Path(sys.argv[1])
sys.path.insert(0, str(raiz / "fleet/tren/tests"))
suite = unittest.defaultTestLoader.loadTestsFromName("test_u14_r1.U14R1Tests")
esperados = {caso.id() for caso in suite}
resultado = unittest.TextTestRunner(verbosity=2).run(suite)
fallidos = {caso.id().split(" (")[0] for caso, _ in resultado.failures}
if resultado.errors or resultado.testsRun != 6 or fallidos != esperados:
    raise SystemExit("ANTES: no se reprodujeron exactamente las seis regresiones U14 R1")
for nombre in sorted(esperados):
    print("ANTES 49fd41dc FALLO ESPERADO:", nombre, flush=True)
print("Evidencia U14 R1 anterior:", raiz, flush=True)
PY
