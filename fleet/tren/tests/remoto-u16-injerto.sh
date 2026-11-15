#!/usr/bin/env bash
set -eu
# Sólo desde remoto.sh en el host de pruebas; conservar toda la evidencia.
tren_pruebas=$(mktemp -d "${TMPDIR:-/tmp}/pao-tren-u16-injerto.XXXXXX")
tren_anterior="$tren_pruebas/anterior"
mkdir -p "$tren_anterior"
git archive 50cb5cdaae8723cb87957decbefb9d1653aa3790 | tar -x -C "$tren_anterior"
cp fleet/tren/tests/test_u16.py fleet/tren/tests/test_u16_injerto.py "$tren_anterior/fleet/tren/tests/"
for arbol in anterior actual; do
  if [ "$arbol" = anterior ]; then pruebas="$tren_anterior/fleet/tren/tests"; else pruebas="$PWD/fleet/tren/tests"; fi
  python3 -I -B - "$pruebas" "$arbol" <<'PY'
import sys
import unittest

sys.path.insert(0, sys.argv[1])
suite = unittest.defaultTestLoader.loadTestsFromName('test_u16_injerto.RegresionesTests')
esperados = {caso.id() for caso in suite}
resultado = unittest.TextTestRunner(verbosity=2).run(suite)
if sys.argv[2] == 'anterior':
    fallidos = {caso.id().split(' (')[0] for caso, _ in resultado.failures}
    if resultado.errors or resultado.testsRun != 7 or fallidos != esperados:
        raise SystemExit('ANTES: no se reprodujeron exactamente las siete regresiones del injerto U16')
    prefijo = 'ANTES 50cb5cda FALLO ESPERADO:'
else:
    if not resultado.wasSuccessful() or resultado.testsRun != 7:
        raise SystemExit('DESPUÉS: no pasaron las siete regresiones del injerto U16')
    prefijo = 'DESPUÉS U16 injerto OK:'
for nombre in sorted(esperados):
    print(prefijo, nombre, flush=True)
print('Evidencia U16 injerto:', sys.argv[1], flush=True)
PY
done
