#!/usr/bin/env bash
set -eu
# Sólo desde remoto.sh en el host de pruebas; conservar evidencia y fixtures.
tren_pruebas=$(mktemp -d "${TMPDIR:-/tmp}/pao-tren-u16-r1.XXXXXX")
tren_anterior="$tren_pruebas/anterior"
mkdir -p "$tren_anterior"
git archive 48748009039838b270f24b37d16b2d5a63bee455 | tar -x -C "$tren_anterior"
cp fleet/tren/tests/test_u16_r1.py "$tren_anterior/fleet/tren/tests/"
for arbol in anterior actual; do
  if [ "$arbol" = anterior ]; then pruebas="$tren_anterior/fleet/tren/tests"; else pruebas="$PWD/fleet/tren/tests"; fi
  python3 -I -B - "$pruebas" "$arbol" <<'PY'
import sys
import unittest

sys.path.insert(0, sys.argv[1])
suite = unittest.defaultTestLoader.loadTestsFromName('test_u16_r1.RegresionesTests')
esperados = {caso.id() for caso in suite}
resultado = unittest.TextTestRunner(verbosity=2).run(suite)
if sys.argv[2] == 'anterior':
    fallidos = {caso.id().split(' (')[0] for caso, _ in resultado.failures}
    if resultado.errors or resultado.testsRun != 4 or fallidos != esperados:
        raise SystemExit('ANTES: no se reprodujeron exactamente las cuatro regresiones U16 R1')
    prefijo = 'ANTES 48748009 FALLO ESPERADO:'
else:
    if not resultado.wasSuccessful() or resultado.testsRun != 4:
        raise SystemExit('DESPUÉS: no pasaron las cuatro regresiones U16 R1')
    prefijo = 'DESPUÉS U16 R1 OK:'
for nombre in sorted(esperados):
    print(prefijo, nombre, flush=True)
print('Evidencia U16 R1:', sys.argv[1], flush=True)
PY
done
