"""Corpus común CC0; no importa control.py ni usa procesos/colas productivos."""
import ast
import copy
import datetime
import importlib.util
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('client_contract', ROOT / 'client_contract.py')
c = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(c)
CORPUS = json.loads((ROOT / 'corpus/client-control-v1.json').read_text())
BY_ID = {case['id']: case for case in CORPUS['valid']}


def exercise(case):
    value = copy.deepcopy(case['value'])
    target = case['target']
    if target == 'request':
        return c.validate_request(value)
    if target == 'response':
        return c.validate_response(value, case.get('request'))
    if target == 'identity':
        return c.identity(value)
    if target == 'acceptance':
        return c.acceptance(value, 'action-0001', '2026-11-16T10:01:00.000000Z')
    if target == 'proof':
        return c.proof(value, '2026-11-16T10:01:00.000000Z', CORPUS['panel_notice']['acceptance'])
    if target == 'transition':
        return c.transition(value, case['after'], case['op'])
    if target == 'negotiation':
        return c.negotiated(value, case['exit_code'])
    if target == 'decode_bytearray':
        return c.decode(bytearray(value, 'utf-8'))
    if target == 'decode_hex':
        return c.decode(bytes.fromhex(value))
    if target == 'decode':
        return c.decode(value)
    raise AssertionError('Target del corpus desconocido')


class ContractTests(unittest.TestCase):
    def assert_invalid(self, case):
        try:
            exercise(case)
        except Exception as error:
            self.assertIsInstance(error, c.Invalid, 'La frontera debe dar un error cerrado')
            self.assertEqual(str(error), case['rule'])
        else:
            self.fail('La regla ' + case['rule'] + ' admitió el caso inválido')

    def test_full_corpus(self):
        self.assertEqual(CORPUS['schema'], c.CORPUS_SCHEMA)
        self.assertEqual(len(BY_ID), len(CORPUS['valid']))
        self.assertEqual(len({v['id'] for v in CORPUS['invalid']}), len(CORPUS['invalid']))
        for case in CORPUS['valid']:
            with self.subTest(case=case['id']):
                r, s = case['request'], case['response']
                self.assertEqual(c.parse(json.dumps(r)), r)
                self.assertEqual(c.parse(json.dumps(s).encode(), response=True, request=r), s)
        for error in CORPUS['protocol_errors']:
            self.assertEqual(c.parse(json.dumps(error), response=True), error)
        for case in CORPUS['invalid']:
            with self.subTest(case=case['id']):
                self.assert_invalid(case)

    def test_every_validator_rule_has_a_negative_case(self):
        tree = ast.parse((ROOT / 'client_contract.py').read_text())
        rules = {node.args[1].value for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                 and node.func.id == 'require'}
        # JSON also rejects malformed UTF-8/calendar values through exception boundaries.
        self.assertEqual(rules | {'json'}, {v['rule'] for v in CORPUS['invalid']})

    def test_deferred_unknown_and_terminal_are_distinct(self):
        statuses = {}
        for case in CORPUS['valid']:
            s = case['response']
            if 'status' in s:
                statuses.setdefault(s['status'], []).append(s)
        self.assertTrue(all(s['effects'] is False and s['proof'] is None
                            for s in statuses['deferred']))
        self.assertTrue(all(s['effects'] is None and s['reservation'] == 'held'
                            for s in statuses['unknown']))
        self.assertTrue(all(s['proof'] is not None and s['reservation'] == 'closed'
                            for s in statuses['finished']))
        self.assertNotIn('cancelled', statuses)  # Sólo el lanzador acredita cancelled.

    def test_stock_phase_transitions_preserve_attempt_and_id(self):
        held = BY_ID['remote-run-rack-queued-held']
        started = BY_ID['remote-run-started']
        queued, stashed = held['request']['expected'], held['response']['identity']
        running = started['response']['identity']
        self.assertIsNone(stashed['enqueued_at'])
        self.assertIn('enqueue_at', stashed)
        self.assertNotEqual(queued['enqueued_at'], running['enqueued_at'])
        self.assertEqual(queued['attempt'], running['attempt'])
        self.assertEqual(queued['task_id'], running['task_id'])
        c.transition(queued, stashed, 'hold')
        c.transition(stashed, running, 'cancel-no-payload')
        for case in CORPUS['valid']:
            if case['response'].get('mode', '').startswith('rack-') and case['response'].get('status') == 'finished':
                self.assertEqual(case['response']['identity']['state'], 'Done')
                self.assertEqual(case['response']['identity']['task_id'], 77)

    def test_locked_rejected_for_every_recovery_entry(self):
        cases = [v for v in CORPUS['invalid'] if v['id'].startswith('locked-')]
        self.assertEqual(len(cases), 4)
        for case in cases:
            self.assert_invalid(case)
        # Nunca se autoriza signal/remove/start de Locked, tampoco por own-legacy.
        for op in ('hold', 'cancel-no-payload', 'signal', 'retire-held', 'retire-interrupted'):
            r = copy.deepcopy(BY_ID['remote-run-rack-queued-held']['request'])
            r.update(op=op, mode='own-legacy', expected=copy.deepcopy(cases[0]['value']['expected']))
            if op == 'signal':
                r['signal'] = 'TERM'
            with self.assertRaisesRegex(c.Invalid, '^operation_state$'):
                c.validate_request(r)

    def test_identity_preflight_is_read_only_and_correlated(self):
        for launcher in c.LAUNCHERS:
            case = BY_ID['identity-' + launcher]
            r, s = case['request'], case['response']
            self.assertEqual(c.validate_response(s, r)['identity']['generation'], 'a' * 64)
            self.assertNotIn('action_id', r)
            self.assertNotIn('acceptance', s)
            wrong = dict(s, ref='another-0001')
            with self.assertRaisesRegex(c.Invalid, '^echo$'):
                c.validate_response(wrong, r)
            with self.assertRaisesRegex(c.Invalid, '^echo$'):
                c.validate_response(s, BY_ID['positive-version']['request'])
        missing = BY_ID['identity-unavailable']['response']
        self.assertIsNone(missing['identity'])
        self.assertEqual(missing['reason'], 'unsupported')

    def test_negotiation_never_grants_fallback(self):
        raw = json.dumps(BY_ID['positive-version']['response'])
        self.assertEqual(c.negotiated(raw, 0)['version'], 1)
        for bad, code in ((raw, 2), (raw, 64), (raw, True), ('usage', 2), ('', 0),
                          (raw.replace('@1', '@2'), 0), (raw.replace('"version": 1', '"version": 2'), 0)):
            with self.subTest(code=code, raw=bad[:40]), self.assertRaises(c.Invalid):
                c.negotiated(bad, code)

    def test_invalid_request_response_is_unattributed(self):
        result = c.invalid_request_response()
        self.assertEqual(CORPUS['protocol_errors'], [result])
        self.assertNotIn('action_id', result)
        self.assertNotIn('proof', result)
        self.assertIsNone(result['op'])
        self.assertEqual(c.parse(json.dumps(result), response=True)['reason'], 'invalid_request')
        with self.assertRaisesRegex(c.Invalid, '^negotiation$'):
            c.negotiated(json.dumps(result), 0)
        self.assertEqual(c.validate_response(result, BY_ID['remote-run-started']['request']), result)

    def test_exact_bounds_and_types(self):
        raw = json.dumps(BY_ID['positive-version']['request'])
        at_limit = raw + ' ' * (c.MAX_BYTES - len(raw))
        self.assertEqual(c.parse(at_limit)['op'], 'capabilities')
        with self.assertRaisesRegex(c.Invalid, '^size$'):
            c.parse(at_limit + ' ')
        for raw in ('[]', 'null', 'true', '1', '"text"', '\ufeff{}'):
            with self.subTest(raw=raw), self.assertRaises(c.Invalid):
                c.parse(raw)
        for field in ('task_id', 'state', 'created_at', 'enqueued_at', 'fingerprint'):
            for wrong in (False, [], {}, 1.5):
                v = copy.deepcopy(BY_ID['remote-run-rack-queued-held']['request']['expected'])
                v[field] = wrong
                with self.subTest(field=field, wrong=wrong), self.assertRaises(c.Invalid):
                    c.identity(v)
        # El límite es bytes UTF-8, no caracteres; los duplicados se detectan a toda profundidad.
        with self.assertRaisesRegex(c.Invalid, '^size$'):
            c.decode('"' + 'é' * (c.MAX_BYTES // 2) + '"')
        for raw in ('{"a":{"b":0,"b":1}}', '{"a":1,"\\u0061":2}'):
            with self.assertRaisesRegex(c.Invalid, '^duplicate$'):
                c.decode(raw)

    def test_wait_matrix_c1_to_c5(self):
        coverage = set()
        for w in CORPUS['wait']:
            with self.subTest(case=w['id']):
                coverage.add((w['launcher'], w['host'], w['observed']))
                if w['status'] == 'cancelled':
                    self.assertTrue(w['current_proof'])
                    self.assertEqual(w['exit'], 130)
                    self.assertEqual(w['presentation'], 'cancelada')
                    self.assertEqual(w['cancelled_marker'], w['launcher'] == 'jale')
                    self.assertFalse(w['retained'])
                elif w['status'] == 'already_finished':
                    self.assertFalse(w['current_proof'])
                    self.assertFalse(w['cancelled_marker'])
                    self.assertEqual(w['presentation'], 'resultado-previo')
                else:
                    self.assertIsNone(w['exit'])
                    self.assertFalse(w['current_proof'])
                    self.assertFalse(w['cancelled_marker'])
                    self.assertEqual(w['retained'], w['status'] != 'rejected')
                if w['status'] == 'cancelling':
                    self.assertIs(w['accepted'], True)
                if w['stage'] == 'before-hold':
                    self.assertIs(w['accepted'], False)
                    self.assertEqual(w['status'], 'unknown')
        self.assertEqual(coverage, {(l, h, s) for l, h in (('remote-run', 'mini'),
                         ('remote-run', 'desk'), ('jale', 'desk')) for s in ('Queued', 'Running')})
        for status in ('cancelled', 'cancelling', 'unknown', 'cancel_unconfirmed', 'rejected'):
            self.assertEqual(sum(w['status'] == status for w in CORPUS['wait']),
                             9 if status == 'unknown' else 6)
        for code in (0, 7, 124, 130, 143):
            self.assertEqual(sum(w['status'] == 'already_finished' and w['exit'] == code
                                 for w in CORPUS['wait']), 6)

    def test_c5_first_acceptance_and_reader_local_time(self):
        notice = CORPUS['panel_notice']
        accepted = notice['acceptance']
        when = datetime.datetime.strptime(accepted['accepted_at'], '%Y-%m-%dT%H:%M:%S.%fZ')
        local = when.replace(tzinfo=datetime.timezone.utc).astimezone(datetime.timezone(datetime.timedelta(hours=-6)))
        self.assertEqual(notice['reader_timezone'], 'CST6')
        self.assertEqual(notice['line'], 'Cancelada desde el panel del rack a las ' +
                         local.strftime('%H:%M') + '; no la relances sin preguntar')
        self.assertEqual(notice['emissions'], 1)
        for case in CORPUS['valid']:
            r, s = case['request'], case['response']
            if r.get('acceptance'):
                self.assertEqual(s['acceptance'], r['acceptance'])
                self.assertEqual(s['acceptance']['accepted_at'], accepted['accepted_at'])

    def test_sanitized_responses_and_contract_only_surface(self):
        forbidden = re.compile(r'(?:/Users/|/home/|https?://|cmd=|body\.sh|Bearer |SECRET|TOKEN=)', re.I)
        forbidden_keys = {'envs', 'env', 'cmd', 'command', 'argv', 'path', 'payload', 'logs', 'socket', 'group'}
        def visit(value):
            if isinstance(value, dict):
                self.assertFalse(forbidden_keys & value.keys())
                for child in value.values(): visit(child)
            elif isinstance(value, list):
                for child in value: visit(child)
            elif isinstance(value, str):
                self.assertIsNone(forbidden.search(value))
        for case in CORPUS['valid']:
            visit(case['request']); visit(case['response'])
        for error in CORPUS['protocol_errors']:
            visit(error)
        self.assertFalse(c.COORDINATED_GATE)
        self.assertEqual(c.LOCK_ORDER[-1], 'control.lock')
        self.assertEqual(c.POLICY_EXIT, 64)
        self.assertEqual(c.POLICY_MESSAGE, 'Pueue administrado: esta operación requiere el controlador coordinado; actualiza el lanzador')
        tree = ast.parse((ROOT / 'client_contract.py').read_text())
        imports = {n.names[0].name for n in tree.body if isinstance(n, ast.Import)}
        self.assertEqual(imports, {'datetime', 'hashlib', 'json', 're'})


# Cada regla tiene una prueba aislada: el mutador exige FAIL/AssertionError, nunca ERROR.
for _case in CORPUS['invalid']:
    _name = 'test_reject_' + _case['rule']
    if not hasattr(ContractTests, _name):
        def check(self, case=_case):
            self.assert_invalid(case)
        setattr(ContractTests, _name, check)


if __name__ == '__main__':
    unittest.main()
