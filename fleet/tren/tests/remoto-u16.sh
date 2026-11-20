#!/usr/bin/env bash
set -eu
# Sólo desde remoto.sh en el host de pruebas; conservar evidencia y fixtures.
tren_pruebas=$(mktemp -d "${TMPDIR:-/tmp}/pao-tren-u16-antes.XXXXXX")
tren_anterior="$tren_pruebas/anterior"
mkdir -p "$tren_anterior"
git archive 85138706a6c77f0c0c76678bd147622f63bf8abb | tar -x -C "$tren_anterior"
cp fleet/tren/tests/test_u16.py "$tren_anterior/fleet/tren/tests/"
python3 -I -B - "$tren_anterior" <<'PY'
from pathlib import Path
import sys
import unittest

raiz = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(raiz / 'fleet/tren/tests'))
suite = unittest.defaultTestLoader.loadTestsFromName('test_u16.RegresionesTests')
esperados = {caso.id() for caso in suite}
resultado = unittest.TextTestRunner(verbosity=2).run(suite)
fallidos = {caso.id().split(' (')[0] for caso, _ in resultado.failures}
if resultado.errors or resultado.testsRun != 3 or fallidos != esperados:
    raise SystemExit('ANTES: no se reprodujeron exactamente las tres regresiones U16')
for nombre in sorted(esperados):
    print('ANTES 85138706 FALLO ESPERADO:', nombre, flush=True)
print('Evidencia U16 anterior:', raiz, flush=True)
PY
# Sombra de doble camino y tick --seco: comparar con el control anterior a U16.
for arbol in anterior actual; do
  if [ "$arbol" = anterior ]; then pruebas="$tren_anterior/fleet/tren/tests"; else pruebas="$PWD/fleet/tren/tests"; fi
  PAO_TREN_U16_SOMBRA="$tren_pruebas/sombra-$arbol.json" python3 -I -B - "$pruebas" <<'PY'
import sys
import unittest

sys.path.insert(0, sys.argv[1])
suite = unittest.defaultTestLoader.loadTestsFromName('test_u16.SombraTests')
resultado = unittest.TextTestRunner(verbosity=2).run(suite)
if not resultado.wasSuccessful() or resultado.testsRun != 1:
    raise SystemExit('SOMBRA: la prueba de eventos no pasó en ' + sys.argv[1])
PY
done
python3 -I -B fleet/tren/tests/comparar_eventos_sombra.py \
  "$tren_pruebas/sombra-anterior.json" "$tren_pruebas/sombra-actual.json"
echo "U16 sombra y tick --seco: eventos fuera de la formación de lotes iguales a 85138706 ($tren_pruebas)"
