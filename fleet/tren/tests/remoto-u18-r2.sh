#!/usr/bin/env bash
set -eu
# Sólo remoto. Conservar evidencia de recuperación sin decisión y su control.
tren_pruebas=$(mktemp -d "${TMPDIR:-/tmp}/pao-tren-u18-r2.XXXXXX")
tren_anterior="$tren_pruebas/anterior"
mkdir -p "$tren_anterior"
git archive 21c93c128d823af27693e916de343d11f2c1ae5a | tar -x -C "$tren_anterior"
cp fleet/tren/tests/test_u18_r2.py "$tren_anterior/fleet/tren/tests/"
for arbol in anterior actual; do
  if [ "$arbol" = anterior ]; then pruebas="$tren_anterior/fleet/tren/tests"; else pruebas="$PWD/fleet/tren/tests"; fi
  python3 -I -B - "$pruebas" "$arbol" <<'PY'
import sys
import unittest

sys.path.insert(0, sys.argv[1])
suite = unittest.defaultTestLoader.loadTestsFromName('test_u18_r2.RegresionesTests')
esperados = {caso.id() for caso in suite}
resultado = unittest.TextTestRunner(verbosity=2).run(suite)
if sys.argv[2] == 'anterior':
    fallidos = {caso.id().split(' (')[0] for caso, _ in resultado.failures}
    if resultado.errors or resultado.testsRun != 2 or fallidos != esperados:
        raise SystemExit('ANTES: no se reprodujeron exactamente los dos hallazgos U18 R2')
    prefijo = 'ANTES 21c93c12 FALLO ESPERADO:'
else:
    if not resultado.wasSuccessful() or resultado.testsRun != 2:
        raise SystemExit('DESPUÉS: no pasaron las dos regresiones U18 R2')
    prefijo = 'DESPUÉS U18 R2 OK:'
for nombre in sorted(esperados):
    print(prefijo, nombre, flush=True)
control = unittest.defaultTestLoader.loadTestsFromName('test_u18_r2.ControlTests')
resultado = unittest.TextTestRunner(verbosity=2).run(control)
if not resultado.wasSuccessful() or resultado.testsRun != 1:
    raise SystemExit('CONTROL: lectura incierta sin gancho no se recuperó sola')
print('U18 R2 control sin gancho OK:', sys.argv[2], flush=True)
print('Evidencia U18 R2:', sys.argv[1], flush=True)
PY
done
