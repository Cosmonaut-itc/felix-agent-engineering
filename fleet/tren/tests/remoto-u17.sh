#!/usr/bin/env bash
set -eu
# Sólo en el host de pruebas.
tren_pruebas=$(mktemp -d "${TMPDIR:-/tmp}/pao-tren-u17.XXXXXX")
trap 'rm -rf -- "$tren_pruebas"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
export TMPDIR="$tren_pruebas/tmp"
mkdir -p "$TMPDIR"
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
    if resultado.errors or resultado.testsRun != 3 or fallidos != esperados:
        raise SystemExit('ANTES: no se reprodujeron exactamente las tres regresiones U17')
    prefijo = 'ANTES 0f7fff27 FALLO ESPERADO:'
else:
    if not resultado.wasSuccessful() or resultado.testsRun != 3:
        raise SystemExit('DESPUÉS: no pasaron las tres regresiones U17')
    prefijo = 'DESPUÉS U17 OK:'
for nombre in sorted(esperados):
    print(prefijo, nombre, flush=True)
print('Evidencia U17:', sys.argv[1], flush=True)
PY
  # Sombra, entregar y --seco: comparar con el control anterior a U17, sin fetch.
  PAO_TREN_U17_SOMBRA="$tren_pruebas/sombra-$arbol.json" python3 -I -B - "$pruebas" <<'PY'
import sys
import unittest

sys.path.insert(0, sys.argv[1])
suite = unittest.defaultTestLoader.loadTestsFromName('test_u17.SombraTests')
resultado = unittest.TextTestRunner(verbosity=2).run(suite)
if not resultado.wasSuccessful() or resultado.testsRun != 1:
    raise SystemExit('SOMBRA: la prueba de eventos no pasó en ' + sys.argv[1])
PY
done
python3 -I -B fleet/tren/tests/comparar_eventos_sombra.py \
  "$tren_pruebas/sombra-anterior.json" "$tren_pruebas/sombra-actual.json"
echo "U17 sombra, entregar y tick --seco: eventos fuera de la formación de lotes iguales a 0f7fff27 ($tren_pruebas)"
