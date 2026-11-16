"""Regresiones de la primera revisión; sólo contrato y datos sintéticos."""
import ast
import copy
import json
import re
import types
import unittest

import test_client_contract as base

c, ROOT, CORPUS, BY_ID = base.c, base.ROOT, base.CORPUS, base.BY_ID
D = copy.deepcopy


def as_status(value):
    return dict(D(value), op='status', action_op=value['op'])


class ReviewRegressionTests(unittest.TestCase):
    def test_sol_1_stock_add_chronology(self):
        for name in ('remote-run-rack-queued-held', 'identity-remote-run',
                     'remote-run-started', 'remote-run-finished-no-payload'):
            case = D(BY_ID[name])
            for body in (case['request'], case['response']):
                for field in ('expected', 'identity'):
                    if body.get(field):
                        body[field]['created_at'] = '2026-11-16T10:01:00.000001Z'
            with self.subTest(case=name):
                try:
                    c.validate_request(case['request'])
                    c.validate_response(case['response'], case['request'])
                except c.Invalid as error:
                    self.fail('Alta stock rechazada: ' + str(error))
        for state in ('Running', 'Paused', 'Done'):
            e = D(BY_ID['remote-run-started']['response']['identity'])
            e.update(state=state, enqueued_at=e['attempt'],
                     created_at='2026-11-16T10:01:00.000001Z')
            with self.subTest(retained_enqueue=state):
                try:
                    c.identity(e)
                except c.Invalid as error:
                    self.fail('Intento stock conservado rechazado: ' + str(error))

    def test_sol_2_terminal_after_acceptance(self):
        case = D(BY_ID['remote-run-finished-no-payload'])
        for e in (case['request']['expected'], case['response']['identity']):
            e.update(enqueued_at='2026-11-16T10:01:01.000000Z',
                     started_at='2026-11-16T10:01:02.000000Z')
        checks = (
            lambda: c.validate_request(case['request']),
            lambda: c.validate_response(case['response']),
            lambda: c.validate_response(case['response'], case['request']),
            lambda: c.validate_response(as_status(case['response']),
                                        BY_ID['status-terminal']['request']),
        )
        for n, check in enumerate(checks):
            with self.subTest(boundary=n), self.assertRaisesRegex(c.Invalid, '^start_time$'):
                check()
        # El trabajo activo ya había nacido al aceptar: no imponerle esa relación.
        case = D(BY_ID['remote-run-finished-active'])
        for e in (case['request']['expected'], case['response']['identity']):
            e.update(enqueued_at='2026-11-16T10:01:01.000000Z',
                     started_at='2026-11-16T10:01:02.000000Z')
        c.validate_response(case['response'], case['request'])

    def test_opus_1_status_scope_and_identity(self):
        case = D(BY_ID['status-pending'])
        s = D(case['response'])
        e = D(BY_ID['retire-held']['response']['identity'])
        s.update(status='retired', effects=True, reason=None, identity=e)
        with self.assertRaisesRegex(c.Invalid, '^scope$'):
            c.validate_response(s, case['request'])
        for field, wrong in (('task_id', 999), ('generation', 'd' * 64),
                             ('fingerprint', 'e' * 64),
                             ('attempt', '2026-11-16T10:00:01.000000Z')):
            s = D(case['response']); s['identity'][field] = wrong
            if field == 'attempt':
                s['acceptance']['attempt'] = wrong
            # El intento también está en la aceptación, cotejada por el validador común.
            rule = 'acceptance_continuity' if field == 'attempt' else 'identity_link'
            with self.subTest(field=field), self.assertRaisesRegex(c.Invalid, '^' + rule + '$'):
                c.validate_response(s, case['request'])
        for launcher, mode in (('apotheca', 'own'), ('vigilante', 'manual')):
            s = dict(as_status(BY_ID['remote-run-started']['response']), launcher=launcher, mode=mode)
            r = dict(as_status(BY_ID['remote-run-started']['request']), launcher=launcher, mode=mode)
            with self.subTest(launcher=launcher), self.assertRaisesRegex(c.Invalid, '^scope$'):
                c.validate_response(s, r)
        # Un status válido conserva su operación y compara la transición completa.
        c.validate_response(case['response'], case['request'])
        r = as_status(BY_ID['remote-run-started']['request'])
        s = as_status(BY_ID['remote-run-started']['response'])
        c.validate_response(s, r)
        s['identity']['state'] = 'Paused'
        with self.assertRaisesRegex(c.Invalid, '^response_phase$'):
            c.validate_response(s, r)

    def test_opus_2_scheduled_stashed_is_not_held(self):
        h = D(BY_ID['remote-run-rack-queued-held'])
        h['response']['identity']['enqueue_at'] = '2026-11-16T10:04:00.000000Z'
        c.identity(h['response']['identity'])  # Observación stock legítima, sin reserva.
        with self.assertRaisesRegex(c.Invalid, '^unscheduled$'):
            c.validate_response(h['response'], h['request'])
        for name in ('remote-run-started', 'retire-held'):
            r = D(BY_ID[name]['request'])
            r['expected']['enqueue_at'] = '2026-11-16T10:04:00.000000Z'
            with self.subTest(case=name), self.assertRaisesRegex(c.Invalid, '^unscheduled$'):
                c.validate_request(r)
        self.assertNotIn('scheduled-stashed', BY_ID)

    def test_opus_3_done_requires_start_but_absent_does_not(self):
        e = D(BY_ID['remote-run-finished-no-payload']['response']['identity'])
        e['started_at'] = None
        with self.assertRaisesRegex(c.Invalid, '^phase$'):
            c.identity(e)
        case = BY_ID['legacy-finished-retired']
        self.assertEqual(case['response']['identity']['state'], 'Absent')
        self.assertIsNone(case['response']['identity']['started_at'])
        c.validate_response(case['response'], case['request'])
        doc = (ROOT / 'CLIENT-CONTROL.md').read_text()
        self.assertIn('no prueba nacimiento', doc)
        self.assertIn('FailedToSpawn', doc)

    def test_opus_4_each_rule_dies_by_its_own_assertion(self):
        source = (ROOT / 'client_contract.py').read_text()
        rules = {node.args[1].value for node in ast.walk(ast.parse(source))
                 if isinstance(node, ast.Call) and getattr(node.func, 'id', '') == 'require'}
        originals = base.c
        try:
            for rule in sorted(rules | {'json'}):
                if rule == 'json':
                    mutated = source.replace('parse_constant=lambda _: (_ for _ in ()).throw(Invalid("json"))',
                                             'parse_constant=lambda _: None')
                else:
                    mutated = source.replace('    if not condition:\n',
                                             '    if not condition and rule != %r:\n' % rule)
                module = types.ModuleType('mutant')
                exec(compile(mutated, '<mutant>', 'exec'), module.__dict__)
                base.c = module
                case = next(case for case in CORPUS['invalid'] if case['rule'] == rule)
                with self.subTest(rule=rule):
                    test = base.ContractTests('test_reject_' + rule)
                    with self.assertRaises(AssertionError) as failure:
                        getattr(test, 'test_reject_' + rule)()
                    self.assertEqual(str(failure.exception),
                                     'La regla ' + rule + ' admitió el caso inválido')
        finally:
            base.c = originals
        with self.assertRaisesRegex(c.Invalid, '^op$'):
            c.validate_request(dict(BY_ID['remote-run-own-held']['request'], op='bogus', phase='bogus'))

    def test_opus_5_hold_cannot_accept_before_stash(self):
        h = BY_ID['remote-run-rack-queued-held']
        a = D(CORPUS['panel_notice']['acceptance'])
        for target in ('request', 'response'):
            value = dict(D(h[target]), acceptance=a)
            with self.subTest(target=target), self.assertRaisesRegex(c.Invalid, '^acceptance_order$'):
                (c.validate_request if target == 'request' else c.validate_response)(value)
        r = D(BY_ID['status-unresolved']['request'])
        s = dict(as_status(h['response']), acceptance=a)
        with self.assertRaisesRegex(c.Invalid, '^acceptance_order$'):
            c.validate_response(s, r)

    def test_opus_6_wait_before_hold_is_not_accepted(self):
        before = [w for w in CORPUS['wait'] if w.get('stage') == 'before-hold']
        self.assertEqual({(w['launcher'], w['host']) for w in before},
                         {('remote-run', 'mini'), ('remote-run', 'desk'), ('jale', 'desk')})
        for w in before:
            self.assertIs(w['accepted'], False)
            self.assertEqual(w['status'], 'unknown')
            self.assertIsNone(w['exit'])
            self.assertFalse(w['current_proof'])
            self.assertTrue(w['retained'])
        accepted = [w for w in CORPUS['wait'] if w['status'] == 'cancelling']
        self.assertTrue(accepted)
        self.assertTrue(all(w['accepted'] is True for w in accepted))
        row = next(line for line in (ROOT / 'CLIENT-CONTROL.md').read_text().splitlines()
                   if line.startswith('| No ganó hold:'))
        self.assertIn('`unknown`', row)
        self.assertNotIn('cancelling', row)

    def test_opus_7_no_private_provenance_in_contract_or_corpus(self):
        # Los centinelas repetidos son datos sintéticos del schema de digest,
        # no revisiones. Sólo se excluyen en campos concretos del corpus.
        digest_fields = {'generation', 'fingerprint', 'receipt', 'acceptance_receipt'}
        def redact_fixture_digests(value):
            if isinstance(value, dict):
                return {key: '<synthetic-digest>' if key in digest_fields and
                        isinstance(child, str) and re.fullmatch(r'([a-fx])\1{63}', child)
                        else redact_fixture_digests(child) for key, child in value.items()}
            if isinstance(value, list):
                return [redact_fixture_digests(child) for child in value]
            return value
        def check(content, name):
            self.assertIsNone(re.search(r'(?<![A-Za-z0-9])[a-fA-F0-9]{7,}(?![A-Za-z0-9])|#\d+', content),
                              name + ': retirar SHA/PR de procedencia; conservarlo sólo en el informe externo')
        check((ROOT / 'CLIENT-CONTROL.md').read_text(), 'CLIENT-CONTROL.md')
        check(json.dumps(redact_fixture_digests(CORPUS)), 'corpus/client-control-v1.json')
        # Acreditar que tampoco se omiten revisiones dentro de campos de digest.
        for marker in ('123abcd', '1234567890abcdef', '#42'):
            with self.subTest(marker=marker), self.assertRaises(AssertionError):
                check(json.dumps(redact_fixture_digests({'generation': marker})), 'sonda sintética')


if __name__ == '__main__':
    unittest.main()
