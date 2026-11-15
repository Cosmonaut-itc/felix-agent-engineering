#!/usr/bin/env bash
set -eu
# Sólo desde remoto.sh en el host de pruebas; conservar fixtures y evidencia.
tren_pruebas=$(mktemp -d "${TMPDIR:-/tmp}/pao-tren-u17.XXXXXX")
tren_anterior="$tren_pruebas/anterior"
mkdir -p "$tren_anterior"
git archive 0f7fff27999ec7556c099f3b918bcedcfbd23720 | tar -x -C "$tren_anterior"
cp fleet/tren/tests/test_u17.py "$tren_anterior/fleet/tren/tests/"
for arbol in anterior actual; do
  if [ "$arbol" = anterior ]; then pruebas="$tren_anterior/fleet/tren/tests"; else pruebas="$PWD/fleet/tren/tests"; fi
  python3 -I -B - "$pruebas" "$arbol" <<'PY'
import sys
import unittest

sys.path.insert(0, sys.argv[1])
suite = unittest.defaultTestLoader.loadTestsFromName('test_u17.RegresionesTests')
esperados = {caso.id() for caso in suite}
resultado = unittest.TextTestRunner(verbosity=2).run(suite)
if sys.argv[2] == 'anterior':
    fallidos = {caso.id().split(' (')[0] for caso, _ in resultado.failures}
    if resultado.errors or resultado.testsRun != 2 or fallidos != esperados:
        raise SystemExit('ANTES: no se reprodujeron exactamente las dos regresiones U17')
    prefijo = 'ANTES 0f7fff27 FALLO ESPERADO:'
else:
    if not resultado.wasSuccessful() or resultado.testsRun != 2:
        raise SystemExit('DESPUÉS: no pasaron las dos regresiones U17')
    prefijo = 'DESPUÉS U17 OK:'
for nombre in sorted(esperados):
    print(prefijo, nombre, flush=True)
print('Evidencia U17:', sys.argv[1], flush=True)
PY
  PAO_TREN_U17_SOMBRA="$tren_pruebas/sombra-$arbol.json" python3 -I -B - "$pruebas" <<'PY'
import sys
import unittest

sys.path.insert(0, sys.argv[1])
resultado = unittest.TextTestRunner(verbosity=2).run(
    unittest.defaultTestLoader.loadTestsFromName('test_u17.SombraTests'))
if not resultado.wasSuccessful() or resultado.testsRun != 1:
    raise SystemExit('SOMBRA U17: la prueba de eventos no pasó en ' + sys.argv[1])
PY
done
cmp "$tren_pruebas/sombra-anterior.json" "$tren_pruebas/sombra-actual.json"
echo "U17 sombra: sombra, sombra-doble-camino y admision-entrega idénticos a 0f7fff27 ($tren_pruebas)"
