#!/usr/bin/env bash
set -eu
operador_r1=$(mktemp -d "${TMPDIR:-/tmp}/pao-tren-sen-7032-r1.XXXXXX")
mkdir -p "$operador_r1/base"
git archive 1d8e118f6f5f504d012578bda79f4ea5658f4688 | tar -x -C "$operador_r1/base"
cp fleet/tren/tests/test_operador.py fleet/tren/tests/test_operador_r1.py "$operador_r1/base/fleet/tren/tests/"
for arbol in base actual; do
  if [ "$arbol" = base ]; then pruebas="$operador_r1/base/fleet/tren/tests"; else pruebas="$PWD/fleet/tren/tests"; fi
  python3 -I -B - "$pruebas" "$arbol" <<'PY'
import sys, unittest
sys.path.insert(0, sys.argv[1])
from test_operador_r1 import OperadorR1Tests
suite = unittest.defaultTestLoader.loadTestsFromTestCase(OperadorR1Tests)
esperados = {c.id() for c in suite}
r = unittest.TextTestRunner(verbosity=2).run(suite)
print('CODIGO_SUITE', sys.argv[2], int(not r.wasSuccessful()), flush=True)
if sys.argv[2] == 'base':
    fallidos = {getattr(c, 'test_case', c).id() for c, _ in r.failures}
    if r.errors or r.testsRun != len(esperados) or fallidos != esperados:
        raise SystemExit('BASE: cada regresión debe fallar como assertion, sin errores')
else:
    if not r.wasSuccessful(): raise SystemExit('ACTUAL: fallaron regresiones')
print('EVIDENCIA SEN-7032 R1', sys.argv[2], r.testsRun, sys.argv[1], flush=True)
PY
done
