#!/usr/bin/env bash
set -eu
operador_pruebas=$(mktemp -d "${TMPDIR:-/tmp}/pao-tren-sen-7032.XXXXXX")
mkdir -p "$operador_pruebas/base"
git archive 9417f6c63caf2ee5a5017355899bbfa2526aa4ff | tar -x -C "$operador_pruebas/base"
cp fleet/tren/tests/test_operador.py "$operador_pruebas/base/fleet/tren/tests/"
for arbol in base actual; do
  if [ "$arbol" = base ]; then pruebas="$operador_pruebas/base/fleet/tren/tests"; else pruebas="$PWD/fleet/tren/tests"; fi
  python3 -I -B - "$pruebas" "$arbol" <<'PY'
import sys
import unittest
sys.path.insert(0, sys.argv[1])
from test_operador import OperadorTests
nombres = unittest.defaultTestLoader.getTestCaseNames(OperadorTests)
compatibilidad = {'test_sin_clave_conserva_lanzamiento_anterior',
                 'test_config_acepta_script_interno_e_interprete_externo'}
if sys.argv[2] == 'base':
    nombres = [n for n in nombres if n not in compatibilidad]
suite = unittest.TestSuite(OperadorTests(n) for n in nombres)
esperados = {c.id() for c in suite}
r = unittest.TextTestRunner(verbosity=2).run(suite)
print('CODIGO_SUITE', sys.argv[2], int(not r.wasSuccessful()), flush=True)
if sys.argv[2] == 'base':
    fallidos = {getattr(c, 'test_case', c).id() for c, _ in r.failures}
    if r.errors or r.testsRun != len(esperados) or fallidos != esperados:
        raise SystemExit('BASE: cada regresión debe fallar como assertion, sin errores')
else:
    if not r.wasSuccessful() or r.testsRun != len(esperados):
        raise SystemExit('ACTUAL: fallaron regresiones o compatibilidad')
print('EVIDENCIA SEN-7032', sys.argv[2], r.testsRun, sys.argv[1], flush=True)
PY
done
