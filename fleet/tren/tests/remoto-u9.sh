#!/usr/bin/env bash
set -eu
tren_u9=$(mktemp -d "${TMPDIR:-/tmp}/pao-tren-u9.XXXXXX")
mkdir -p "$tren_u9/base"
git archive 0806170d11805e228da5ca1c94c64473336c32a9 | tar -x -C "$tren_u9/base"
cp fleet/tren/tests/test_jale.py "$tren_u9/base/fleet/tren/tests/"
for arbol in base actual; do
  if [ "$arbol" = base ]; then pruebas="$tren_u9/base/fleet/tren/tests"; else pruebas="$PWD/fleet/tren/tests"; fi
  python3 -I -B - "$pruebas" "$arbol" <<'PY'
import sys
import unittest
sys.path.insert(0, sys.argv[1])
from test_jale import JaleTests
nombres = unittest.defaultTestLoader.getTestCaseNames(JaleTests)
nuevos = {JaleTests(n).id() for n in nombres if n.startswith('test_r1_')}
suite = unittest.TestSuite(JaleTests(n) for n in nombres
                           if sys.argv[2] != 'base' or n.startswith('test_r1_'))
esperados = {caso.id() for caso in suite}
if not nuevos:
    raise SystemExit('No se encontraron regresiones R1')
r = unittest.TextTestRunner(verbosity=2).run(suite)
if sys.argv[2] == 'base':
    fallidos = {getattr(caso, 'test_case', caso).id() for caso, _ in r.failures}
    if r.errors or r.testsRun != len(esperados) or fallidos != esperados:
        raise SystemExit('BASE: no falló cada caso nuevo como se esperaba')
    etiqueta = 'BASE 0806170d FALLO ESPERADO'
else:
    if not r.wasSuccessful() or r.testsRun != len(esperados):
        raise SystemExit('ACTUAL: fallaron pruebas nuevas')
    etiqueta = 'ACTUAL OK'
for nombre in sorted(nuevos):
    print(etiqueta, nombre, flush=True)
print('EVIDENCIA', sys.argv[1], flush=True)
PY
done
# Dos procesos con fixtures aisladas: partición exhaustiva de discovery sin
# recortar casos ni reservar otro turno del host.
if [ "${1:-}" = --completa ]; then
  tren_u9_pids=()
  for grupo in 0 1; do
    python3 -I -B - "$grupo" > "$tren_u9/suite-$grupo.log" 2>&1 <<'PY' &
import sys
import unittest

def casos(suite):
    for caso in suite:
        if isinstance(caso, unittest.TestSuite):
            yield from casos(caso)
        else:
            yield caso

todos = list(casos(unittest.defaultTestLoader.discover('fleet/tren/tests')))
grupo = int(sys.argv[1])
elegidos = todos[grupo::2]
print('SUITE COMPLETA grupo={} casos={} total={}'.format(grupo, len(elegidos), len(todos)), flush=True)
r = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(elegidos))
raise SystemExit(not r.wasSuccessful())
PY
    tren_u9_pids+=("$!")
  done
  resultado=0
  for pid in "${tren_u9_pids[@]}"; do
    wait "$pid" || resultado=1
  done
  cat "$tren_u9/suite-0.log" "$tren_u9/suite-1.log"
  exit "$resultado"
fi
