#!/usr/bin/python3 -I
"""Anula cada regla por separado en copias /tmp; exige fallo de aserción."""
import ast
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    baseline = subprocess.run([sys.executable, '-I', '-m', 'unittest', 'discover',
                               '-s', str(ROOT / 'tests'), '-p', 'test_client_contract.py', '-q'],
                              capture_output=True, text=True, timeout=30)
    if baseline.returncode:
        print(baseline.stdout + baseline.stderr)
        raise AssertionError('La base debe pasar antes de mutar')
    source = (ROOT / 'client_contract.py').read_text()
    tree = ast.parse(source)
    rules = sorted({node.args[1].value for node in ast.walk(tree)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == 'require'} | {'json'})
    cases = json.loads((ROOT / 'corpus/client-control-v1.json').read_text())['invalid']
    if set(rules) != {case['rule'] for case in cases}:
        raise AssertionError('Cada regla necesita caso inválido y mutación')
    for number, rule in enumerate(rules, 1):
        if rule == 'json':
            old = 'parse_constant=lambda _: (_ for _ in ()).throw(Invalid("json"))'
            new = 'parse_constant=lambda _: None'
        else:
            old = '    if not condition:\n'
            new = '    if not condition and rule != ' + repr(rule) + ':\n'
        if source.count(old) != 1:
            raise AssertionError('Mutación ambigua o caduca: ' + rule)
        with tempfile.TemporaryDirectory(prefix='cc0-mutacion-', dir='/tmp') as temporary:
            folder = Path(temporary)
            (folder / 'tests').mkdir()
            (folder / 'corpus').mkdir()
            (folder / 'client_contract.py').write_text(source.replace(old, new))
            shutil.copy2(ROOT / 'corpus/client-control-v1.json', folder / 'corpus')
            shutil.copy2(ROOT / 'tests/test_client_contract.py', folder / 'tests')
            test = 'test_reject_' + rule
            result = subprocess.run([sys.executable, '-I', '-m', 'unittest', 'discover',
                                     '-s', str(folder / 'tests'), '-p', 'test_client_contract.py',
                                     '-k', '*.' + test, '-v'], capture_output=True, text=True, timeout=30)
            output = result.stdout + result.stderr
            if not (result.returncode == 1 and 'FAIL: ' + test + ' ' in output and
                    'AssertionError:' in output and 'ERROR:' not in output and 'Ran 1 test ' in output):
                print(output, flush=True)
                raise AssertionError('Mutante vivo o fallo ajeno a la aserción: ' + rule)
            assertion = next(line for line in output.splitlines() if line.startswith('AssertionError:'))
            print('RECHAZADA %02d %s | %s | %s' % (number, rule, test, assertion), flush=True)
    print('Resultado: %d/%d mutaciones rechazadas por aserción en Python %s' %
          (len(rules), len(rules), sys.version.split()[0]), flush=True)


if __name__ == '__main__':
    main()
