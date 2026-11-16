"""Paridad generada de respuestas; usa cada espécimen directo del corpus."""
import copy
import unittest

import test_client_contract as base

c, CORPUS, BY_ID = base.c, base.CORPUS, base.BY_ID
D = copy.deepcopy
ACTION_OPS = c.OPS[1:-1]


def as_status(value, request=False):
    result = dict(D(value), op='status', action_op=value['op'])
    # Sólo para reproducir las regresiones con el corpus anterior a la corrección.
    # En el corpus vigente se conserva el cuerpo completo y la fase exacta.
    if 'acceptance' not in BY_ID['status-pending']['request']:
        result['phase'] = 'action'
        if request:
            for field in ('acceptance', 'proof', 'signal'):
                result.pop(field, None)
    return result


def direct_response_cases():
    return [case for case in CORPUS['invalid'] if case['target'] == 'response'
            and case['value'].get('op') in ACTION_OPS]


def direct_valid_cases():
    return [case for case in CORPUS['valid'] if case['request']['op'] in ACTION_OPS]


class ResponseParityTests(unittest.TestCase):
    def assert_rejected(self, response, request, rule):
        try:
            c.validate_response(D(response), D(request))
        except c.Invalid as error:
            self.assertEqual(str(error), rule)
        else:
            self.fail('La regla ' + rule + ' admitió el caso inválido')

    def test_generated_invalid_response_parity(self):
        cases = direct_response_cases()
        self.assertTrue(cases)
        for case in cases:
            response, request = case['value'], case.get('request')
            with self.subTest(case=case['id'], boundary='direct'):
                self.assert_rejected(response, request, case['rule'])
            with self.subTest(case=case['id'], boundary='status'):
                self.assert_rejected(as_status(response),
                                     as_status(request, True) if request is not None else None,
                                     case['rule'])

    def test_generated_valid_response_parity(self):
        cases = direct_valid_cases()
        self.assertEqual({case['request']['op'] for case in cases}, set(ACTION_OPS))
        for case in cases:
            request, response = case['request'], case['response']
            for query in (False, True):
                r = as_status(request, True) if query else D(request)
                s = as_status(response) if query else D(response)
                for paired in (False, True):
                    with self.subTest(case=case['id'], status=query, paired=paired):
                        self.assertEqual(c.validate_response(s, r if paired else None), s)

    def test_held_pending_requires_unscheduled(self):
        for name in ('remote-run-rack-queued-held', 'remote-run-started'):
            request, response = D(BY_ID[name]['request']), D(BY_ID[name]['response'])
            response.update(status='pending', effects=False, reason='manual',
                            identity=D(BY_ID['remote-run-started']['request']['expected']))
            for query in (False, True):
                r = as_status(request, True) if query else D(request)
                s = as_status(response) if query else D(response)
                c.validate_response(s, r)
                s['identity']['enqueue_at'] = '2026-11-16T10:04:00.000000Z'
                with self.subTest(operation=name, status=query):
                    self.assert_rejected(s, r, 'unscheduled')
        # Una observación genérica de stock no afirma ninguna reserva.
        case = D(BY_ID['identity-remote-run'])
        case['response']['identity'] = D(BY_ID['remote-run-started']['request']['expected'])
        case['response']['identity']['enqueue_at'] = '2026-11-16T10:04:00.000000Z'
        c.validate_response(case['response'], case['request'])

    def test_deferred_preserves_existing_reservation(self):
        for name in ('remote-run-started', 'remote-run-finished-no-payload'):
            r, s = D(BY_ID[name]['request']), D(BY_ID[name]['response'])
            s.update(status='deferred', effects=False, reservation='held', reason='manual',
                     proof=None, identity=D(r['expected']))
            for query in (False, True):
                request = as_status(r, True) if query else D(r)
                response = as_status(s) if query else D(s)
                c.validate_response(response, request)
                response['reservation'] = 'none'
                with self.subTest(operation=name, status=query):
                    self.assert_rejected(response, request, 'deferred_reservation')

    def test_every_held_reservation_is_unscheduled(self):
        case = BY_ID['remote-run-rack-queued-held']
        for status in ('held', 'pending', 'deferred', 'unknown', 'rejected'):
            s = D(case['response'])
            s.update(status=status, effects=None if status == 'unknown' else status == 'held',
                     reason=None if status == 'held' else 'manual')
            for query in (False, True):
                r = as_status(case['request'], True) if query else D(case['request'])
                response = as_status(s) if query else D(s)
                for paired in (False, True):
                    request = r if paired else None
                    c.validate_response(response, request)
                    scheduled = D(response)
                    scheduled['identity']['enqueue_at'] = '2026-11-16T10:04:00.000000Z'
                    with self.subTest(status=status, query=query, paired=paired):
                        self.assert_rejected(scheduled, request, 'unscheduled')

    def test_signal_keeps_first_acceptance(self):
        case = BY_ID['remote-run-signal']
        for query in (False, True):
            request = as_status(case['request'], True) if query else D(case['request'])
            response = as_status(case['response']) if query else D(case['response'])
            c.validate_response(response, request)
            for change in ('missing', 'changed'):
                s = D(response)
                if change == 'missing':
                    s['acceptance'] = None
                else:
                    s['acceptance']['accepted_at'] = '2026-11-16T10:02:01.000000Z'
                with self.subTest(status=query, acceptance=change):
                    self.assert_rejected(s, request, 'acceptance_continuity')

    def test_query_uses_exact_journal_phase_and_evidence(self):
        case = BY_ID['remote-run-signal']
        request, response = as_status(case['request'], True), as_status(case['response'])
        self.assertEqual(request['phase'], 'signal:TERM')
        self.assertEqual(request['signal'], 'TERM')
        c.validate_response(response, request)
        response['phase'] = 'signal:KILL'
        self.assert_rejected(response, request, 'echo')
        for field in ('acceptance', 'proof', 'signal'):
            r = D(request); r.pop(field)
            with self.subTest(missing=field), self.assertRaisesRegex(c.Invalid, '^keys$'):
                c.validate_request(r)
        request['acceptance'] = None
        with self.assertRaisesRegex(c.Invalid, '^acceptance_required$'):
            c.validate_request(request)


if __name__ == '__main__':
    print('Paridad: %d respuestas inválidas y %d pares válidos del corpus' %
          (len(direct_response_cases()), len(direct_valid_cases())), flush=True)
    unittest.main()
