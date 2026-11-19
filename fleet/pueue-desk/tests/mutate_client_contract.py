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
                               '-s', str(ROOT / 'tests'), '-p', 'test_client_contract*.py', '-q'],
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
            diagnostic = 'AssertionError: La regla ' + rule + ' admitió el caso inválido'
            if not (result.returncode == 1 and 'FAIL: ' + test + ' ' in output and
                    diagnostic in output.splitlines() and 'ERROR:' not in output and 'Ran 1 test ' in output):
                print(output, flush=True)
                raise AssertionError('Mutante vivo o fallo ajeno a la aserción: ' + rule)
            assertion = next(line for line in output.splitlines() if line.startswith('AssertionError:'))
            print('RECHAZADA %02d %s | %s | %s' % (number, rule, test, assertion), flush=True)
    print('Resultado: %d/%d mutaciones rechazadas por aserción en Python %s' %
          (len(rules), len(rules), sys.version.split()[0]), flush=True)
    # La regla ampliada y el paso compartido también se mutilan por sitio:
    # anular la familia unscheduled no prueba que cubra pending/unknown.
    sites = (
        ('held-en-toda-fase', 'reserve != "held"', 'status != "held"',
         'test_held_pending_requires_unscheduled', 'unscheduled'),
        ('status-valida-operacion', '        validate_response(projected, original)\n',
         '        pass\n', 'test_deferred_preserves_existing_reservation', 'deferred_reservation'),
    )
    for name, old, new, test, rule in sites:
        if source.count(old) != 1:
            raise AssertionError('Mutación de sitio ambigua o caduca: ' + name)
        with tempfile.TemporaryDirectory(prefix='cc0-mutacion-', dir='/tmp') as temporary:
            folder = Path(temporary)
            (folder / 'tests').mkdir()
            (folder / 'corpus').mkdir()
            (folder / 'client_contract.py').write_text(source.replace(old, new))
            shutil.copy2(ROOT / 'corpus/client-control-v1.json', folder / 'corpus')
            for file in ('test_client_contract.py', 'test_client_contract_r2.py'):
                shutil.copy2(ROOT / 'tests' / file, folder / 'tests')
            result = subprocess.run([sys.executable, '-I', '-m', 'unittest', 'discover',
                                     '-s', str(folder / 'tests'), '-p', 'test_client_contract_r2.py',
                                     '-k', '*.' + test, '-v'], capture_output=True, text=True, timeout=30)
            output = result.stdout + result.stderr
            diagnostic = 'AssertionError: La regla ' + rule + ' admitió el caso inválido'
            if not (result.returncode == 1 and 'FAIL: ' + test + ' ' in output and
                    diagnostic in output.splitlines() and 'ERROR:' not in output and 'Ran 1 test ' in output):
                print(output, flush=True)
                raise AssertionError('Mutante vivo o fallo ajeno a la aserción: ' + name)
            print('RECHAZADA sitio %s | %s | %s' % (name, test, diagnostic), flush=True)
    print('Resultado adicional: %d/%d mutaciones de sitio rechazadas por aserción' %
          (len(sites), len(sites)), flush=True)
    mutate_rpc(source)


RPC_FILES = ('client-control-v1.json', 'client-rpc-stock-v1.json', 'pueue-404-frames.json')
# CC3-P0 (alcance-stock §4.3): cada mutante semántico muere por la historia del corpus
# que lo acusa. El octavo cubre la propuesta de E-stock adoptada (S3 relajada); los tres
# últimos, los hallazgos de la review r1.
RPC_SITES = (
    ('r-plus-sin-exclusion', '    if exclusion != "filtered":\n', '    if False:\n',
     'test_mutant_r_plus_without_exclusion', 'La historia hold-success-coordinated cambió de veredicto'),
    ('sello-de-no-duenio', 'owners = (plan, process, claim, seal)', 'owners = (plan, process, claim)',
     'test_mutant_seal_from_non_owner', 'La historia hold-seal-non-owner cambió de veredicto'),
    ('s4-sin-fin-de-vida', 'if not emitter_alive and not life_alive:', 'if not emitter_alive:',
     'test_mutant_s4_requires_daemon_death',
     'La historia no-seal-emitter-dead-daemon-alive cambió de veredicto'),
    ('closed-como-replied', 'seal["outcome"] == "replied" and seal["reply"] == "success"',
     'seal["outcome"] in ("replied", "closed") and seal["reply"] in ("success", None)',
     'test_mutant_closed_is_not_a_reply', 'La historia hold-closed-alive-filtered cambió de veredicto'),
    ('q-tras-plazo-vencido', 'if post is None or post["seal"] != seal["receipt"]:', 'if post is None:',
     'test_mutant_q_after_timeout', 'La historia hold-success-q-before-seal cambió de veredicto'),
    ('id-reutilizado-sin-respuesta', 'if not emitter_alive and not life_alive:',
     'if (not emitter_alive and not life_alive) or (post is not None and '
     'post["identity"]["task_id"] == pre["identity"]["task_id"] and '
     'post["identity"]["created_at"] != pre["identity"]["created_at"]):',
     'test_mutant_reused_id_without_reply', 'La historia retire-no-seal-reused-id cambió de veredicto'),
    ('version-distinta', 'frame[8:] == PUEUE_PROTOCOL.encode("ascii")', 'True',
     'test_mutant_greeting_pins_protocol', 'El saludo 0.32.0 fue aceptado'),
    ('closed-con-par-vivo-como-no-aplicado',
     'return True, None, "evidence_missing" if life_alive else "startup"',
     'return True, (False if life_alive else None), "evidence_missing" if life_alive else "startup"',
     'test_mutant_closed_never_not_applied', 'La historia hold-closed-alive-coordinated cambió de veredicto'),
    # Review r1: claim perdido en una restauración, retirada con id reutilizado y sello negativo ajeno.
    ('claim-perdido-liquida', 'if claim is None and not emitter_alive:', 'if False:',
     'test_mutant_lost_claim_never_settles', 'La historia lost-claim-both-dead cambió de veredicto'),
    ('retirada-proyecta-q-cruda',
     'if (op in ("retire-held", "retire-interrupted") and after["state"] != "Absent" and',
     'if (False and',
     'test_retire_reused_id_projects_absent_of_x', 'La retirada con id reutilizado no proyecta Absent de X'),
    ('sello-negativo-de-no-duenio', 'owners = (plan, process, claim, seal)', 'owners = (plan, process, claim)',
     'test_mutant_negative_seal_from_non_owner', 'La historia hold-not-sent-non-owner cambió de veredicto'),
)


def run_rpc_mutant(source, old, new, test, diagnostic, name):
    if source.count(old) != 1:
        raise AssertionError('Mutación RPC ambigua o caduca: ' + name)
    with tempfile.TemporaryDirectory(prefix='cc3-mutacion-', dir='/tmp') as temporary:
        folder = Path(temporary)
        (folder / 'tests').mkdir()
        (folder / 'corpus').mkdir()
        (folder / 'client_contract.py').write_text(source.replace(old, new))
        for file in RPC_FILES:
            shutil.copy2(ROOT / 'corpus' / file, folder / 'corpus')
        shutil.copy2(ROOT / 'tests/test_client_contract_rpc.py', folder / 'tests')
        result = subprocess.run([sys.executable, '-I', '-m', 'unittest', 'discover',
                                 '-s', str(folder / 'tests'), '-p', 'test_client_contract_rpc.py',
                                 '-k', '*.' + test, '-v'], capture_output=True, text=True, timeout=30)
        output = result.stdout + result.stderr
        if not (result.returncode == 1 and 'FAIL: ' + test + ' ' in output and
                'AssertionError: ' + diagnostic in output.splitlines() and
                'ERROR:' not in output and 'Ran 1 test ' in output):
            print(output, flush=True)
            raise AssertionError('Mutante RPC vivo o fallo ajeno a la aserción: ' + name)


def mutate_rpc(source):
    tree = ast.parse(source)
    rules = sorted({node.args[1].value for node in ast.walk(tree)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == 'rpc_require'})
    cases = json.loads((ROOT / 'corpus/client-rpc-stock-v1.json').read_text())['invalid']
    if set(rules) != {case['rule'] for case in cases if case['rule'].startswith('rpc_')}:
        raise AssertionError('Cada regla RPC necesita caso inválido y mutación')
    for number, rule in enumerate(rules, 1):
        diagnostic = 'La regla ' + rule + ' admitió el caso inválido'
        run_rpc_mutant(source, '    if condition:\n        return\n',
                       '    if condition or rule == %r:\n        return\n' % rule,
                       'test_reject_' + rule, diagnostic, rule)
        print('RECHAZADA RPC %02d %s | test_reject_%s | AssertionError: %s' %
              (number, rule, rule, diagnostic), flush=True)
    print('Resultado RPC: %d/%d reglas rechazadas por aserción' % (len(rules), len(rules)), flush=True)
    for name, old, new, test, diagnostic in RPC_SITES:
        run_rpc_mutant(source, old, new, test, diagnostic, name)
        print('RECHAZADA RPC sitio %s | %s | AssertionError: %s' % (name, test, diagnostic), flush=True)
    print('Resultado RPC adicional: %d/%d mutantes semánticos rechazados por aserción en Python %s' %
          (len(RPC_SITES), len(RPC_SITES), sys.version.split()[0]), flush=True)


if __name__ == '__main__':
    main()
