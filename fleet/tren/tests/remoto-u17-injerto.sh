#!/usr/bin/env bash
set -eu
# Sólo en el host de pruebas.
tren_pruebas=$(mktemp -d "${TMPDIR:-/tmp}/pao-tren-u17-injerto.XXXXXX")
trap 'rm -rf -- "$tren_pruebas"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
export TMPDIR="$tren_pruebas/tmp"
mkdir -p "$TMPDIR"
tren_anterior="$tren_pruebas/anterior"
mkdir -p "$tren_anterior"
git archive d0019396a7452a36b3505d7c36ef37869ba40aa2 | tar -x -C "$tren_anterior"
cp fleet/tren/tests/test_u17.py "$tren_anterior/fleet/tren/tests/"
for arbol in anterior actual; do
  if [ "$arbol" = anterior ]; then pruebas="$tren_anterior/fleet/tren/tests"; else pruebas="$PWD/fleet/tren/tests"; fi
  python3 -I -B - "$pruebas" "$arbol" <<'PY'
import sys
import unittest

sys.path.insert(0, sys.argv[1])
suite = unittest.defaultTestLoader.loadTestsFromName('test_u17.PresupuestoTests')
esperados = {caso.id() for caso in suite}
resultado = unittest.TextTestRunner(verbosity=2).run(suite)
if sys.argv[2] == 'anterior':
    fallidos = {caso.id().split(' (')[0] for caso, _ in resultado.failures}
    if resultado.errors or resultado.testsRun != 2 or fallidos != esperados:
        raise SystemExit('ANTES: no se reprodujeron exactamente las dos regresiones del injerto U17')
    prefijo = 'ANTES d0019396 FALLO ESPERADO:'
else:
    if not resultado.wasSuccessful() or resultado.testsRun != 2:
        raise SystemExit('DESPUÉS: no pasaron las dos regresiones del injerto U17')
    prefijo = 'DESPUÉS U17 injerto OK:'
for nombre in sorted(esperados):
    print(prefijo, nombre, flush=True)
print('Evidencia U17 injerto:', sys.argv[1], flush=True)
PY
done
