#!/usr/bin/env bash
set -eu
# Sólo desde remoto.sh en el host de pruebas; se conserva la evidencia.
tren_pruebas=$(mktemp -d "${TMPDIR:-/tmp}/pao-tren-u14-r2-antes.XXXXXX")
tren_anterior="$tren_pruebas/anterior"
mkdir -p "$tren_anterior"
git archive 0a1aabd8b735e3f885d16be45dd9586ce8386090 | tar -x -C "$tren_anterior"
cp fleet/tren/tests/test_u14_r2.py "$tren_anterior/fleet/tren/tests/"
python3 -I -B - "$tren_anterior" <<'PY'
from pathlib import Path
import sys
import unittest

raiz = Path(sys.argv[1])
sys.path.insert(0, str(raiz / "fleet/tren/tests"))
suite = unittest.defaultTestLoader.loadTestsFromName("test_u14_r2.U14R2Tests")
esperados = {caso.id() for caso in suite}
resultado = unittest.TextTestRunner(verbosity=2).run(suite)
fallidos = {caso.id().split(" (")[0] for caso, _ in resultado.failures}
if resultado.errors or resultado.testsRun != 2 or fallidos != esperados:
    raise SystemExit("ANTES: no se reprodujeron exactamente las dos regresiones U14 R2")
for nombre in sorted(esperados):
    print("ANTES 0a1aabd8 FALLO ESPERADO:", nombre, flush=True)
print("Evidencia U14 R2 anterior:", raiz, flush=True)
PY
