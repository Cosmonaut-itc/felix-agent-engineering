import json
from pathlib import Path
import shutil
import tempfile
import unittest

root = Path.cwd()
(root / '.u17-tmp').mkdir(exist_ok=True)
base = Path(tempfile.mkdtemp(prefix='baseline-8d4ed90f-', dir=root / '.u17-tmp'))
product = base / 'fleet/pueue-desk'
shutil.copytree(root / 'fleet/pueue-desk', product)
for name in ('control.py', 'host.py', 'mini.py'):
    shutil.copy2(root / '.u17-baseline' / name, product / name)
suite = unittest.defaultTestLoader.discover(str(product / 'tests'), pattern='test_review_r2.py')
result = unittest.TextTestRunner(verbosity=2).run(suite)
failed = [t.id() for t, _ in result.failures + result.errors]
expected = ('C1', 'W1', 'W2', 'W3', 'W4', 'W5')
covered = {item: any('test_' + item + '_' in name for name in failed) for item in expected}
print('BASELINE 8d4ed90f:', json.dumps({'regresiones_rojas': covered, 'tests': result.testsRun,
                                       'fallos': len(result.failures), 'errores': len(result.errors)}, sort_keys=True))
assert all(covered.values()), covered
assert not result.errors, result.errors
print('BASELINE: cada hallazgo falla sobre 8d4ed90f (OK)')
