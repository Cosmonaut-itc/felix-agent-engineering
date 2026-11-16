#!/usr/bin/env bash
set -eu
# Sólo remoto. Conservar árboles y reproducciones de los tres hallazgos R1.
tren_pruebas=$(mktemp -d "${TMPDIR:-/tmp}/pao-tren-u18-r1.XXXXXX")
tren_anterior="$tren_pruebas/anterior"
mkdir -p "$tren_anterior"
git archive 4498b3dd6f0faf9901fe5b152c81c18ed4d0a5c5 | tar -x -C "$tren_anterior"
cp fleet/tren/tests/test_u18_r1.py "$tren_anterior/fleet/tren/tests/"
for arbol in anterior actual; do
  if [ "$arbol" = anterior ]; then pruebas="$tren_anterior/fleet/tren/tests"; else pruebas="$PWD/fleet/tren/tests"; fi
  python3 -I -B - "$pruebas" "$arbol" <<'PY'
import sys
import unittest

sys.path.insert(0, sys.argv[1])
suite = unittest.defaultTestLoader.loadTestsFromName('test_u18_r1.RegresionesTests')
esperados = {caso.id() for caso in suite}
resultado = unittest.TextTestRunner(verbosity=2).run(suite)
if sys.argv[2] == 'anterior':
    fallidos = {caso.id().split(' (')[0] for caso, _ in resultado.failures}
    if resultado.errors or resultado.testsRun != 3 or fallidos != esperados:
        raise SystemExit('ANTES: no se reprodujeron exactamente los tres hallazgos U18 R1')
    prefijo = 'ANTES 4498b3dd FALLO ESPERADO:'
else:
    if not resultado.wasSuccessful() or resultado.testsRun != 3:
        raise SystemExit('DESPUÉS: no pasaron las tres regresiones U18 R1')
    prefijo = 'DESPUÉS U18 R1 OK:'
for nombre in sorted(esperados):
    print(prefijo, nombre, flush=True)
print('Evidencia U18 R1:', sys.argv[1], flush=True)
PY
done
