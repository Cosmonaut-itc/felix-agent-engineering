"""Contrato interno CC3-P0 del emisor stock; sólo funciones puras y datos sintéticos."""
import ast
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('client_contract', ROOT / 'client_contract.py')
c = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(c)
RPC = json.loads((ROOT / 'corpus/client-rpc-stock-v1.json').read_text())
FRAMES = json.loads((ROOT / 'corpus/pueue-404-frames.json').read_text())
VERDICTS = {case['id']: case for case in RPC['verdicts']}
D = copy.deepcopy


def request_for(history):
    spec = RPC['requests'][history]
    value = dict(schema='pueue-client-control@1', op=spec['op'], launcher=spec['launcher'],
                 ref=RPC['ref'], action_id=RPC['action_id'], mode=spec['mode'],
                 expected=D(RPC['observations'][spec['expected']]),
                 acceptance=D(RPC['acceptance']) if spec['acceptance'] else None,
                 proof=None, phase=spec['phase'])
    if 'signal' in spec:
        value['signal'] = spec['signal']
    return value


def seal_receipt(seal):
    return c.rpc_digest({k: v for k, v in seal.items() if k != 'receipt'})


def patch(docs, change):
    path = change['path']
    if not path:
        docs[change['doc']] = D(change['value'])
        return
    target = docs[change['doc']]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = D(change['value'])


def build(case):
    """Plan, claim, sello, diario, P, Q y banderas de una historia, con un solo campo cruzado."""
    history = case.get('history', 'hold')
    spec = RPC['requests'][history]
    request = request_for(history)
    emitter, daemon = RPC['emitters']['E'], RPC['lives']['D']
    plan = dict(schema=c.RPC_PLAN, dispatch=RPC['dispatch'], request=D(request), group=RPC['group'],
                verb=spec['verb'], task_id=request['expected']['task_id'], daemon=D(daemon),
                pid=emitter['pid'], birth=emitter['birth'])
    plan_digest = c.rpc_digest(plan)
    owner = dict(dispatch=RPC['dispatch'], plan_digest=plan_digest, pid=emitter['pid'],
                 birth=emitter['birth'])
    claim = dict(owner, schema=c.RPC_CLAIM)
    shape = case.get('seal', {'outcome': 'replied', 'reply': 'success'})
    seal = None
    if shape is not None:
        peer = shape.get('peer', None if shape['outcome'] == 'not-sent' else 'D')
        seal = dict(owner, schema=c.RPC_SEAL, outcome=shape['outcome'],
                    reply=shape.get('reply'), reason=shape.get('reason'),
                    peer=None if peer is None else {k: RPC['lives'][peer][k] for k in ('pid', 'birth')})
        seal['receipt'] = seal_receipt(seal)
    record = dict(request=D(request), digest=c.rpc_digest(request),
                  process=dict(pid=emitter['pid'], birth=emitter['birth'], dispatch=RPC['dispatch'],
                               plan_digest=plan_digest, state='sent'))
    pre = dict(life=D(daemon), identity=D(RPC['observations'][case.get('pre', spec['expected'])]))
    seen = case.get('post')
    post = None if seen is None else dict(life=D(RPC['lives'][seen.get('life', 'D')]),
                                          identity=D(RPC['observations'][seen['obs']]), seal=None)
    flags = dict(emitter_alive=case.get('emitter_alive', True), life_alive=case.get('life_alive', True),
                 exclusion=case.get('exclusion', 'filtered'))
    docs = dict(plan=plan, claim=claim, seal=seal, record=record, pre=pre, post=post, flags=flags)
    change = case.get('patch')
    if change:
        patch(docs, change)
        if change['doc'] == 'seal' and change['path'] not in ([], ['receipt']):
            docs['seal']['receipt'] = seal_receipt(docs['seal'])
    if docs['post'] is not None and docs['seal'] is not None and seen.get('after_seal', True):
        docs['post']['seal'] = docs['seal']['receipt']  # Q leída después de este sello.
    return docs


def verdict(docs):
    f = docs['flags']
    return list(c.rpc_verdict(docs['record'], docs['plan'], docs['claim'], docs['seal'],
                              f['emitter_alive'], f['life_alive'], docs['pre'], docs['post'],
                              f['exclusion']))


def exercise(case):
    target = case['target']
    if target == 'greeting':
        return c.pueue_greeting(bytes.fromhex(case['hex']))
    docs = build(case)
    if target == 'plan':
        return c.validate_rpc_plan(docs['plan'])
    if target == 'claim':
        return c.validate_rpc_claim(docs['claim'])
    if target == 'seal':
        return c.validate_rpc_seal(docs['seal'])
    if target == 'link':
        return c.execution_link(docs['plan'], docs['claim'], docs['seal'], docs['record'])
    if target == 'prior':
        return c.rpc_prior(docs['record'], docs['plan'], docs['pre'])
    if target == 'verdict':
        return verdict(docs)
    raise AssertionError('Target del corpus RPC desconocido')


def cbor_uint(value):
    if value < 24:
        return bytes([value])
    for code, size in ((0x18, 1), (0x19, 2), (0x1a, 4), (0x1b, 8)):
        if value < 256 ** size:
            return bytes([code]) + value.to_bytes(size, 'big')
    raise AssertionError('Entero fuera de CBOR')


class RpcContractTests(unittest.TestCase):
    def assert_invalid(self, case):
        try:
            exercise(case)
        except Exception as error:
            self.assertIsInstance(error, c.Invalid, 'La frontera debe dar un error cerrado')
            self.assertEqual(str(error), case['rule'])
        else:
            self.fail('La regla ' + case['rule'] + ' admitió el caso inválido')

    def test_valid_documents_round_trip(self):
        self.assertEqual(RPC['schema'], c.RPC_CORPUS_SCHEMA)
        for history in RPC['requests']:
            for outcome in ({'outcome': 'replied', 'reply': 'success'},
                            {'outcome': 'replied', 'reply': 'failure'},
                            {'outcome': 'replied', 'reply': 'unrecognized'},
                            {'outcome': 'closed'},
                            {'outcome': 'not-sent', 'reason': 'startup'},
                            {'outcome': 'not-sent', 'reason': 'unsupported', 'peer': 'D'}):
                docs = build({'history': history, 'seal': outcome})
                with self.subTest(history=history, outcome=outcome):
                    self.assertEqual(c.parse_rpc_plan(json.dumps(docs['plan'])), docs['plan'])
                    self.assertEqual(c.parse_rpc_claim(json.dumps(docs['claim']).encode()), docs['claim'])
                    self.assertEqual(c.parse_rpc_seal(json.dumps(docs['seal'])), docs['seal'])
                    c.execution_link(docs['plan'], docs['claim'], docs['seal'], docs['record'])
                    c.rpc_prior(docs['record'], docs['plan'], docs['pre'])
        # El emisor puede no haber reclamado ni sellado todavía.
        docs = build({'seal': None})
        c.execution_link(docs['plan'], None, None, docs['record'])
        c.execution_link(docs['plan'], docs['claim'], None, dict(docs['record'], process=dict(
            docs['record']['process'], state='gated')))
        c.pueue_greeting(bytes.fromhex(FRAMES['version']['hex']))

    def test_full_invalid_corpus(self):
        self.assertEqual(len({v['id'] for v in RPC['invalid']}), len(RPC['invalid']))
        for case in RPC['invalid']:
            with self.subTest(case=case['id']):
                self.assert_invalid(case)

    def test_every_rpc_rule_has_a_negative_case(self):
        tree = ast.parse((ROOT / 'client_contract.py').read_text())
        rules = {node.args[1].value for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                 and node.func.id == 'rpc_require'}
        self.assertEqual(rules, {v['rule'] for v in RPC['invalid'] if v['rule'].startswith('rpc_')})

    def test_verdict_table(self):
        self.assertEqual(len(VERDICTS), len(RPC['verdicts']))
        for case in RPC['verdicts']:
            with self.subTest(case=case['id']):
                self.assertEqual(verdict(build(case)), case['expect'])

    def test_verdict_invariants(self):
        for case in RPC['verdicts']:
            settled, effect, reason = case['expect']
            with self.subTest(case=case['id']):
                self.assertIn(case['exclusion'], c.EXCLUSION_PROFILES)
                # Sin liquidar nunca hay efecto; un efecto atribuido no lleva motivo.
                self.assertTrue(settled or effect is None)
                self.assertEqual(effect is True, reason is None)
                if effect is True:
                    self.assertEqual(case['exclusion'], 'filtered')
                    self.assertEqual(case['seal'], {'outcome': 'replied', 'reply': 'success'})
                    self.assertIsNotNone(case['post'])
                if case['seal'] is not None and case['seal']['outcome'] == 'closed':
                    self.assertIsNone(effect)  # S3 liquida, nunca atribuye ni prueba ausencia.
                if case.get('patch', {}).get('doc') == 'claim':
                    # Sin claim: pendiente con el emisor vivo; perdido si murió, y entonces sólo
                    # liquida por S4 (D terminada, sin sello), sin atribuir y con evidence_missing.
                    alive = case.get('emitter_alive', True)
                    s4 = case['seal'] is None and not alive and not case.get('life_alive', True)
                    self.assertEqual(settled, s4)
                    self.assertEqual(reason, 'rpc_unresolved' if alive and case['seal'] is None
                                     else 'evidence_missing')
        covered = {(case['seal'] or {}).get('outcome') for case in RPC['verdicts']}
        self.assertEqual(covered, {None, 'not-sent', 'replied', 'closed'})
        # Q sólo se exige para atribuir: hay una historia sin Q por cada clase de sello.
        without_q = {(case['seal']['outcome'], case['seal'].get('reply')) for case in RPC['verdicts']
                     if case['seal'] is not None and case['post'] is None and 'patch' not in case}
        self.assertLessEqual({('not-sent', None), ('closed', None), ('replied', 'failure'),
                              ('replied', 'success')}, without_q)

    def test_mutant_r_plus_without_exclusion(self):
        self.assert_story('hold-success-coordinated')

    def test_mutant_seal_from_non_owner(self):
        self.assert_story('hold-seal-non-owner')

    def test_mutant_negative_seal_from_non_owner(self):
        self.assert_story('hold-not-sent-non-owner')

    def test_mutant_s4_requires_daemon_death(self):
        self.assert_story('no-seal-emitter-dead-daemon-alive')

    def test_mutant_lost_claim_waits_for_daemon(self):
        self.assert_story('lost-claim-emitter-dead-daemon-alive')

    def test_mutant_lost_claim_settles_as_evidence_missing(self):
        self.assert_story('lost-claim-both-dead')

    def test_mutant_missing_q_only_holds_success(self):
        """Sin Q (vencida o no leída) S2, R− y S3 liquidan igual; sólo un success queda sin atribuir."""
        for name in ('hold-not-sent-startup', 'hold-failure-without-q', 'hold-closed-alive-without-q',
                     'hold-closed-daemon-gone', 'hold-success-without-q'):
            self.assert_story(name)

    def test_retire_reused_id_projects_absent_of_x(self):
        """collect y apply cotejan Absent de X, no la Q cruda de la tarea nueva (`@1` intacto)."""
        stable = ('generation', 'task_id', 'created_at', 'fingerprint', 'attempt')
        for name in ('retire-reused-id', 'retire-interrupted-reused-id'):
            docs = build(VERDICTS[name])
            request, before, raw = docs['record']['request'], docs['pre']['identity'], docs['post']['identity']
            with self.subTest(case=name):
                effect = c.rpc_effect_identity(request['op'], before, raw)
                if effect != RPC['observations']['absent'] or any(effect[k] != before[k] for k in stable):
                    self.fail('La retirada con id reutilizado no proyecta Absent de X')
                response = {k: request[k] for k in ('schema', 'op', 'launcher', 'ref', 'action_id', 'phase', 'mode')}
                response.update(status='retired', effects=True, reservation='held', identity=effect,
                                acceptance=request['acceptance'], proof=None, reason=None)
                self.assertEqual(c.validate_response(response, request), response)
                self.assertEqual(c.transition(request['expected'], effect, request['op']), effect)
                with self.assertRaisesRegex(c.Invalid, '^response_phase$'):
                    c.validate_response(dict(response, identity=raw), request)
        # Fuera de una retirada, o con la misma creación, Q no se proyecta.
        for name in ('hold-reused-id-not-projected', 'retire-still-present', 'retire-absent'):
            docs = build(VERDICTS[name])
            with self.subTest(case=name):
                self.assertEqual(c.rpc_effect_identity(docs['record']['request']['op'], docs['pre']['identity'],
                                                       docs['post']['identity']), docs['post']['identity'])

    def test_mutant_closed_is_not_a_reply(self):
        self.assert_story('hold-closed-alive-filtered')

    def test_mutant_q_after_timeout(self):
        self.assert_story('hold-success-q-before-seal')

    def test_mutant_reused_id_without_reply(self):
        self.assert_story('retire-no-seal-reused-id')

    def test_mutant_closed_never_not_applied(self):
        self.assert_story('hold-closed-alive-coordinated')

    def test_mutant_greeting_pins_protocol(self):
        for greeting in FRAMES['rejected_greetings']:
            try:
                c.pueue_greeting(bytes.fromhex(greeting['hex']))
            except c.Invalid as error:
                self.assertEqual(str(error), 'rpc_greeting')
            else:
                self.fail('El saludo ' + greeting['label'] + ' fue aceptado')

    def assert_story(self, name):
        case = VERDICTS[name]
        if verdict(build(case)) != case['expect']:
            self.fail('La historia ' + name + ' cambió de veredicto')

    def test_golden_frames(self):
        self.assertEqual(FRAMES['schema'], c.FRAMES_CORPUS_SCHEMA)
        self.assertEqual(FRAMES['release'], c.PUEUE_RELEASE)
        self.assertEqual(FRAMES['version']['text'], c.PUEUE_PROTOCOL)
        version = bytes.fromhex(FRAMES['version']['hex'])
        self.assertEqual(version[8:].decode('ascii'), c.PUEUE_PROTOCOL)
        self.assertEqual(int(FRAMES['secret']['header'], 16), FRAMES['secret']['length'])
        self.assertIsNone(re.fullmatch(r'[0-9a-f]+', FRAMES['secret']['payload']))
        verbs = set()
        for form in FRAMES['forms']:
            with self.subTest(verb=form['verb'], task=form['task_id']):
                request, response = bytes.fromhex(form['request']), bytes.fromhex(form['response'])
                for frame in (request, response):
                    self.assertEqual(int.from_bytes(frame[:8], 'big'), len(frame) - 8)
                body = request[8:]
                (variant, fields), = form['decoded'].items()
                self.assertEqual(body[0], 0xa1)
                self.assertEqual(body[2:2 + body[1] - 0x60].decode('ascii'), variant)
                self.assertIn(b'\x81' + cbor_uint(form['task_id']), body)
                if variant == 'Kill':
                    self.assertTrue(body.endswith(fields['signal'].encode('ascii')))
                    self.assertEqual(form['verb'], 'kill:' + fields['signal'])
                else:
                    self.assertEqual(form['verb'], variant.lower())
                self.assertEqual(response[8:10], b'\xa1\x67')
                self.assertEqual(response[10:17].decode('ascii'), form['reply'].capitalize())
                verbs.add(form['verb'])
        # Las cuatro formas cerradas de un solo id, kill siempre con señal.
        self.assertEqual(verbs, set(c.RPC_VERBS.values()))
        self.assertEqual({f['reply'] for f in FRAMES['forms']}, {'success', 'failure'})

    def test_exact_bounds(self):
        docs = build({})
        for parse, value, limit in ((c.parse_rpc_plan, docs['plan'], c.RPC_PLAN_MAX_BYTES),
                                    (c.parse_rpc_claim, docs['claim'], c.RPC_MAX_BYTES),
                                    (c.parse_rpc_seal, docs['seal'], c.RPC_MAX_BYTES)):
            raw = json.dumps(value)
            at_limit = raw + ' ' * (limit - len(raw))
            with self.subTest(limit=limit):
                self.assertEqual(parse(at_limit), value)
                with self.assertRaisesRegex(c.Invalid, '^size$'):
                    parse(at_limit + ' ')
        for raw in ('{"a":1,"a":2}', '{"x":NaN}', '[]'):
            with self.subTest(raw=raw), self.assertRaises(c.Invalid):
                c.parse_rpc_seal(raw)

    def test_wire_unchanged(self):
        self.assertEqual(c.SCHEMA, 'pueue-client-control@1')
        self.assertEqual(c.MAX_BYTES, 16 * 1024)
        self.assertEqual(c.OPS, ('capabilities', 'hold', 'cancel-no-payload', 'signal', 'signal-manual',
                                 'retire-held', 'retire-interrupted', 'finish', 'status'))
        self.assertEqual(c.REASONS, ('lock_busy', 'host_action', 'manual', 'pressure', 'startup', 'locked',
                                     'stale_state', 'not_found', 'unsupported', 'identity', 'invalid_request',
                                     'idempotency_conflict', 'rpc_unresolved', 'evidence_missing'))
        # Todo motivo del veredicto ya existe en el cable: CC3 no añade códigos.
        reasons = {case['expect'][2] for case in RPC['verdicts']} - {None}
        self.assertLessEqual(reasons | set(c.RPC_NOT_SENT), set(c.REASONS))
        request = request_for('hold')
        c.validate_request(request)
        for field in ('dispatch', 'plan_digest', 'exclusion'):
            with self.subTest(field=field), self.assertRaisesRegex(c.Invalid, '^keys$'):
                c.validate_request(dict(request, **{field: RPC['dispatch']}))
        self.assertEqual(set(c.RPC_OPS), set(c.OPS[1:-2]))
        self.assertEqual(c.EXCLUSION_PROFILES, ('coordinated', 'filtered'))

    def test_digest_is_canonical_sha256(self):
        value = {'b': [1, 'é'], 'a': None}
        text = '{"a":null,"b":[1,"\\u00e9"]}'
        self.assertEqual(c.rpc_digest(value), hashlib.sha256(text.encode()).hexdigest())
        with self.assertRaisesRegex(c.Invalid, '^json$'):
            c.rpc_digest({'x': float('nan')})

    def test_new_corpora_are_synthetic(self):
        forbidden = re.compile(r'/Users/|/home/|https?://|(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])'
                               r'|Bearer |TOKEN=', re.I)
        for name in ('client-rpc-stock-v1.json', 'pueue-404-frames.json'):
            with self.subTest(corpus=name):
                self.assertIsNone(forbidden.search((ROOT / 'corpus' / name).read_text()))


# Cada regla RPC tiene una prueba aislada: el mutador exige FAIL/AssertionError, nunca ERROR.
for _case in RPC['invalid']:
    _name = 'test_reject_' + _case['rule']
    if not hasattr(RpcContractTests, _name):
        def check(self, case=_case):
            self.assert_invalid(case)
        setattr(RpcContractTests, _name, check)


if __name__ == '__main__':
    unittest.main()
