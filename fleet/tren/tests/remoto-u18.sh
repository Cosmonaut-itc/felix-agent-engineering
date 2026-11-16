#!/usr/bin/env bash
set -eu
# Injerto de regresiones en main previo a U18; sólo host remoto, sin retirar evidencia.
tren_pruebas=$(mktemp -d "${TMPDIR:-/tmp}/pao-tren-u18.XXXXXX")
tren_anterior="$tren_pruebas/anterior"
mkdir -p "$tren_anterior"
git archive a1d24ec11e1bf1f169910aa417d76432ff40a3c8 | tar -x -C "$tren_anterior"
cp fleet/tren/tests/test_u18.py "$tren_anterior/fleet/tren/tests/"
for arbol in anterior actual; do
  if [ "$arbol" = anterior ]; then pruebas="$tren_anterior/fleet/tren/tests"; else pruebas="$PWD/fleet/tren/tests"; fi
  python3 -I -B - "$pruebas" "$arbol" <<'PY'
import sys
import unittest
sys.path.insert(0, sys.argv[1])
suite = unittest.defaultTestLoader.loadTestsFromName('test_u18.RegresionesTests')
esperados = {caso.id() for caso in suite}
resultado = unittest.TextTestRunner(verbosity=2).run(suite)
if sys.argv[2] == 'anterior':
    fallidos = {caso.id().split(' (')[0] for caso, _ in resultado.failures}
    if resultado.errors or resultado.testsRun != 6 or fallidos != esperados:
        raise SystemExit('ANTES: no se reprodujeron exactamente las seis regresiones U18')
    prefijo = 'ANTES a1d24ec1 FALLO ESPERADO:'
else:
    if not resultado.wasSuccessful() or resultado.testsRun != 6:
        raise SystemExit('DESPUÉS: no pasaron las seis regresiones U18')
    prefijo = 'DESPUÉS U18 OK:'
for nombre in sorted(esperados):
    print(prefijo, nombre, flush=True)
print('Evidencia U18:', sys.argv[1], flush=True)
PY
  PAO_TREN_U17_SOMBRA="$tren_pruebas/sombra-$arbol.json" python3 -I -B - "$pruebas" <<'PY'
import sys
import unittest
sys.path.insert(0, sys.argv[1])
suite = unittest.defaultTestLoader.loadTestsFromName('test_u18.SombraTests')
resultado = unittest.TextTestRunner(verbosity=2).run(suite)
if not resultado.wasSuccessful() or resultado.testsRun != 1:
    raise SystemExit('SOMBRA U18: eventos o aislamiento incorrectos')
PY
done
cmp "$tren_pruebas/sombra-anterior.json" "$tren_pruebas/sombra-actual.json"
echo "U18 sombra, entregar y tick --seco: eventos idénticos a a1d24ec1 ($tren_pruebas)"
