#!/usr/bin/env bash
set -euo pipefail
git rev-parse HEAD
/usr/bin/python3 -B - <<'PY'
import io
import os
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
import tempfile
import unittest

root = Path.cwd()
porcelain = subprocess.check_output(['git', 'status', '--porcelain'], text=True).splitlines()
if any(line != '?? .remote-run.lock' for line in porcelain):
    raise SystemExit('arbol sucio: ' + repr(porcelain))
print('ARBOL_LIMPIO tolerancia exacta SEN-3885', flush=True)
print('SHA_CORREGIDO ' + subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(), flush=True)
print('PYTHON ' + sys.version + ' PLATAFORMA ' + platform.platform(), flush=True)
red_sha = 'c23cc0fe6feda40b5383670554910011d303f4d2'
red_code = r'''
import unittest
names = ['test_a2_i1_foreign_rejection_persists_uncertainty_and_audit',
         'test_a2_i1_foreign_reserve_persists_preparation_expiry',
         'test_n2_foreign_operations_cannot_consume_prepared_id',
         'test_n2_owner_other_operation_does_not_consume_prepared_id']
suite = unittest.defaultTestLoader.loadTestsFromNames(['hostgate.tests.test_contract.Contract.' + n for n in names])
result = unittest.TextTestRunner(verbosity=2).run(suite)
failed = [test.id() for test, _ in result.failures]
print('ROJO_COMPROBADO ' + str(failed), flush=True)
if result.errors or not all(any(name in test for test in failed) for name in names):
    raise SystemExit('las cuatro regresiones A2-I1/N-2 deben fallar sin errores de entorno sobre c23cc0f')
'''
print('BASE_ROJA ' + red_sha, flush=True)
with tempfile.TemporaryDirectory(prefix='hostgate-red-') as tmp:
    snapshot = subprocess.check_output(['git', 'archive', red_sha, 'fleet/hostgate'])
    with tarfile.open(fileobj=io.BytesIO(snapshot)) as archive:
        archive.extractall(tmp)
    (Path(tmp) / 'fleet/hostgate/tests/test_contract.py').write_bytes(
        (root / 'fleet/hostgate/tests/test_contract.py').read_bytes())
    subprocess.run([sys.executable, '-B', '-c', red_code], cwd=str(Path(tmp) / 'fleet'), check=True)

os.chdir(root / 'fleet')
sys.path.insert(0, str(root / 'fleet'))
families = (('test_cli',), ('test_contract', 'test_lector_desk_adoptado'),
            ('test_identity_scope',), ('test_lector_desk',), ('test_lector_mini',), ('test_store',))
for modules in families:
    count = sum(unittest.defaultTestLoader.loadTestsFromName('hostgate.tests.' + module).countTestCases()
                for module in modules)
    print('DESCUBRIMIENTO %s: %d' % ('+'.join(modules), count), flush=True)
    if count == 0 or (modules == ('test_lector_desk',) and count != 113):
        raise SystemExit('descubrimiento incompleto: ' + repr(modules))
suite = unittest.defaultTestLoader.discover('hostgate/tests', top_level_dir='.')
result = unittest.TextTestRunner(verbosity=2).run(suite)
print('RESULTADO pruebas=%d fallos=%d errores=%d omitidas=%d' %
      (result.testsRun, len(result.failures), len(result.errors), len(result.skipped)), flush=True)
raise SystemExit(0 if result.wasSuccessful() else 1)
PY
