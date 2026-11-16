#!/usr/bin/env bash
set -eu
# Sólo remoto. Conservar los árboles, fixtures y fallos antes/después.
tren_pruebas=$(mktemp -d "${TMPDIR:-/tmp}/pao-tren-u18-injerto.XXXXXX")
tren_anterior="$tren_pruebas/anterior"
mkdir -p "$tren_anterior"
git archive c06a9c70d2e265be47d2c16855976562a80ba678 | tar -x -C "$tren_anterior"
cp fleet/tren/tests/test_u18_injerto.py "$tren_anterior/fleet/tren/tests/"
for arbol in anterior actual; do
  if [ "$arbol" = anterior ]; then pruebas="$tren_anterior/fleet/tren/tests"; else pruebas="$PWD/fleet/tren/tests"; fi
  python3 -I -B - "$pruebas" "$arbol" <<'PY'
import sys
import unittest

sys.path.insert(0, sys.argv[1])
suite = unittest.defaultTestLoader.loadTestsFromName('test_u18_injerto.RegresionesTests')
esperados = {caso.id() for caso in suite}
resultado = unittest.TextTestRunner(verbosity=2).run(suite)
if sys.argv[2] == 'anterior':
    fallidos = {caso.id().split(' (')[0] for caso, _ in resultado.failures}
    if resultado.errors or resultado.testsRun != 6 or fallidos != esperados:
        raise SystemExit('ANTES: no se reprodujeron exactamente las seis regresiones del injerto U18')
    prefijo = 'ANTES c06a9c70 FALLO ESPERADO:'
else:
    if not resultado.wasSuccessful() or resultado.testsRun != 6:
        raise SystemExit('DESPUÉS: no pasaron las seis regresiones del injerto U18')
    prefijo = 'DESPUÉS U18 injerto OK:'
for nombre in sorted(esperados):
    print(prefijo, nombre, flush=True)
print('Evidencia U18 injerto:', sys.argv[1], flush=True)
PY
done
