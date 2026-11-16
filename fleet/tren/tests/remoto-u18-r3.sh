#!/usr/bin/env bash
set -eu
tren_pruebas=$(mktemp -d "${TMPDIR:-/tmp}/pao-tren-u18-r3.XXXXXX")
tren_anterior="$tren_pruebas/anterior"
mkdir -p "$tren_anterior"
git archive c28fa524890d4e741809c935b6d3c606f7ffd918 | tar -x -C "$tren_anterior"
cp fleet/tren/tests/test_u18_r3.py "$tren_anterior/fleet/tren/tests/"
for arbol in anterior actual; do
  if [ "$arbol" = anterior ]; then pruebas="$tren_anterior/fleet/tren/tests"; else pruebas="$PWD/fleet/tren/tests"; fi
  python3 -I -B - "$pruebas" "$arbol" <<'PY'
import sys
import unittest

sys.path.insert(0, sys.argv[1])
suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(nombre)
    for nombre in ('test_u18_r3.RegresionTests', 'test_u18_r3.InvarianteTests'))
esperados = {caso.id() for grupo in suite for caso in grupo}
resultado = unittest.TextTestRunner(verbosity=2).run(suite)
if sys.argv[2] == 'anterior':
    fallidos = {caso.id().split(' (')[0] for caso, _ in resultado.failures}
    if resultado.errors or resultado.testsRun != 2 or fallidos != esperados:
        raise SystemExit('ANTES: no se reprodujeron ambos fallos U18 R3')
    prefijo = 'ANTES c28fa524 FALLO ESPERADO:'
else:
    if not resultado.wasSuccessful() or resultado.testsRun != 2:
        raise SystemExit('DESPUÉS: no pasaron ambas pruebas U18 R3')
    prefijo = 'DESPUÉS U18 R3 OK:'
for nombre in sorted(esperados):
    print(prefijo, nombre, flush=True)
print('Evidencia U18 R3:', sys.argv[1], flush=True)
PY
done
