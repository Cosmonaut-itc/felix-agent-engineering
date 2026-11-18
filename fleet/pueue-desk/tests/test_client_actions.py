"""CC1: synthetic evidence/Pueue only; real processes share the actual flock."""
import copy
import fcntl
import io
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch
import test_reorder
import test_slots

control = test_slots.control
a = control.client_module()
c = a.c
CORPUS = json.loads((Path(__file__).resolve().parents[1] / 'corpus/client-control-v1.json').read_text())
CASES = {v['id']: v for v in CORPUS['valid']}


def request(case='remote-run-rack-queued-held'):
    return copy.deepcopy(CASES[case]['request'])


class Evidence:
    """Local evidence double; request receipts alone are never authenticated."""
    def __init__(self, identity):
        self.identity = copy.deepcopy(identity)
        self.acceptance = self.proof = None
        self.normal_result = False
        self.reads = 0
        self.change_at = {}

    def observe(self, r, state, reservation):
        self.reads += 1
        if self.reads in self.change_at:
            self.identity = copy.deepcopy(self.change_at[self.reads])
        return dict(group='suites', identity=copy.deepcopy(self.identity))

    def verify(self, r, observation, reservation):
        if (r['acceptance'] != self.acceptance or r['proof'] != self.proof or
                r['op'].startswith('retire-') and self.normal_result):
            raise a.Unavailable('evidence_missing')


def worker(directory, r, pipe, die=False):
    engine = control.MiniControl(test_slots.FakePueue(), Path(directory), generation=lambda: 'daemon-1')
    actions = a.ClientActions(engine, Evidence(r['expected']), rpc=lambda *args: pipe.send(('rpc', args)))
    if die:
        actions.boundary = lambda phase: os._exit(0) if phase == 'reserved' else None
    with (Path(directory) / 'result.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        pipe.send(actions.handle(r))
    pipe.close()


def late_cli(pipe):
    pipe.send('alive')
    pipe.recv()
    pipe.send('late-effect')
    pipe.close()


def hold_lock(directory, pipe):
    with (Path(directory) / 'control.lock').open('rb') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        pipe.send('locked')
        pipe.recv()


class ActionTests(test_reorder.ReorderTests):
    pass


for _name in dir(test_reorder.ReorderTests):
    if _name.startswith('test_'):
        setattr(ActionTests, _name, None)


def setup(self):
    test_reorder.setup(self)
    self.r = request()
    self.evidence = Evidence(self.r['expected'])
    self.rpc_calls = []
    self.after = copy.deepcopy(CASES['remote-run-rack-queued-held']['response']['identity'])
    self.actions = a.ClientActions(self.engine, self.evidence, rpc=lambda *args: self.rpc(*args))


def rpc(self, *args):
    rows = a.reservations(self.engine)  # A second connection requires a committed intent.
    self.assertTrue(rows and rows[-1]['open'] and rows[-1]['uncertain'], 'reservation before RPC')
    self.assertEqual(self.actions.record(self.active)['response']['status'], 'unknown')
    self.rpc_calls.append(args)
    self.evidence.identity = copy.deepcopy(self.after)


def act(self, r=None):
    self.active = r or self.r
    result, code = self.actions.dispatch(json.dumps(self.active).encode())
    self.assertEqual(code, 0)
    c.validate_response(result, self.active)
    return result


def seed(self, r, uncertain=False):
    reservation = dict(launcher=r['launcher'], ref=r['ref'], action_id=r['action_id'], group='suites',
                       mode=r['mode'], identity=r['expected'], acceptance=None, open=True, uncertain=uncertain,
                       hold_identity=copy.deepcopy(r['expected']) if r['expected']['state'] == 'Stashed' else None)
    record = dict(request=r, digest=a.digest(r), revision=0, process=None,
                  response=self.actions.reply(r, 'unknown', 'rpc_unresolved', held=True))
    self.actions.save(record, reservation)
    with self.engine.connect(write=True) as db:
        db.execute('DELETE FROM client_actions WHERE action_id=?', (r['action_id'],))


ActionTests.setUp, ActionTests.rpc, ActionTests.act, ActionTests.seed = setup, rpc, act, seed


def test_normal_hold_before_rpc_and_restart_idempotence(self):
    result = self.act()
    self.assertEqual(result['status'], 'held', result)
    self.assertEqual(self.rpc_calls, [('stash', 77)])
    self.assertEqual(a.reservations(self.engine)[0]['hold_identity'], result['identity'])
    self.actions = a.ClientActions(self.engine, self.evidence, rpc=lambda *args: self.fail('duplicate RPC'))
    self.assertEqual(self.act(), result)
    changed = copy.deepcopy(self.r); changed['mode'] = 'own'
    self.assertEqual(self.act(changed)['reason'], 'idempotency_conflict')
    query = dict(self.r, op='status', action_op='hold')
    self.assertEqual(self.act(query)['status'], 'held')
    query['mode'] = 'own'
    self.assertEqual(self.act(query)['reason'], 'idempotency_conflict')


def test_all_business_operations_and_idempotence(self):
    for op in ('cancel-no-payload', 'signal', 'signal-manual', 'retire-held', 'retire-interrupted', 'finish'):
        case = next(v for v in CORPUS['valid'] if v['request']['op'] == op and
                    v['response'].get('status') in ('started', 'signalled', 'retired', 'finished'))
        with self.subTest(op=op):
            r = copy.deepcopy(case['request']); r['action_id'] = 'operation-' + op
            if r['acceptance']: r['acceptance']['action_id'] = r['action_id']
            self.evidence = Evidence(r['expected'])
            self.evidence.acceptance, self.evidence.proof = r['acceptance'], r['proof']
            self.actions = a.ClientActions(self.engine, self.evidence, rpc=lambda *args: self.rpc(*args))
            if op in ('cancel-no-payload', 'retire-held', 'finish'): self.seed(r)
            self.after = copy.deepcopy(case['response']['identity'])
            before = len(self.rpc_calls)
            result = self.act(r)
            self.assertEqual(result['status'], case['response']['status'], result)
            self.assertEqual(len(self.rpc_calls) - before, 0 if op == 'finish' else 1)
            self.assertEqual(self.act(r), result)
            self.actions = a.ClientActions(self.engine, self.evidence, rpc=lambda *args: self.fail('restart duplicate'))
            self.assertEqual(self.act(r), result)
            changed = copy.deepcopy(r); changed['expected']['fingerprint'] = 'f' * 64
            self.assertEqual(self.act(changed)['reason'], 'idempotency_conflict')
            with self.engine.connect(write=True) as db: db.execute('DELETE FROM client_reservations')


def test_hold_start_finish_authenticated_evidence(self):
    self.assertEqual(self.act()['status'], 'held')
    r = request('remote-run-started')
    self.evidence.acceptance = copy.deepcopy(r['acceptance'])
    self.after = copy.deepcopy(CASES['remote-run-started']['response']['identity'])
    self.assertEqual(self.act(r)['status'], 'started')
    finish = next(copy.deepcopy(v['request']) for v in CORPUS['valid'] if
                  v['request']['op'] == 'finish' and v['request']['mode'] == 'rack-queued')
    self.evidence.identity, self.evidence.proof = copy.deepcopy(finish['expected']), copy.deepcopy(finish['proof'])
    self.assertEqual(self.act(finish)['status'], 'finished')
    self.assertFalse(self.engine.client_blocked('suites'))
    self.assertEqual([x[0] for x in self.rpc_calls], ['stash', 'start'])
    self.assertEqual(self.act(r)['status'], 'started')


def test_untrusted_acceptance_proof_and_prior_normal_result(self):
    for op in ('cancel-no-payload', 'finish', 'retire-interrupted'):
        r = next(copy.deepcopy(v['request']) for v in CORPUS['valid'] if v['request']['op'] == op)
        self.evidence = Evidence(r['expected']); self.evidence.normal_result = op == 'retire-interrupted'
        self.actions = a.ClientActions(self.engine, self.evidence, rpc=lambda *args: self.fail('untrusted effect'))
        if op != 'retire-interrupted': self.seed(r)
        self.assertEqual(self.act(r)['reason'], 'evidence_missing')
        with self.engine.connect(write=True) as db: db.execute('DELETE FROM client_reservations')


def test_locked_every_mutator(self):
    for op in ('hold', 'cancel-no-payload', 'signal', 'signal-manual', 'retire-held', 'retire-interrupted', 'finish'):
        r = next(copy.deepcopy(v['request']) for v in CORPUS['valid'] if v['request']['op'] == op)
        self.evidence.identity = dict(r['expected'], state='Locked', enqueued_at=None, started_at=None, enqueue_at=None)
        self.assertEqual(self.act(r)['reason'], 'locked', op)
    self.assertEqual(self.rpc_calls, [])


def test_races_at_each_read_and_stash_never_escalate(self):
    running = copy.deepcopy(CASES['remote-run-started']['response']['identity'])
    for read in (1, 2, 3):
        r = copy.deepcopy(self.r); r['action_id'] = 'race-case-' + str(read)
        self.evidence = Evidence(r['expected']); self.evidence.change_at[read] = running
        self.actions = a.ClientActions(self.engine, self.evidence, rpc=lambda *args: self.rpc(*args))
        self.after = running
        self.assertEqual(self.act(r)['reason'], 'stale_state')
        self.assertFalse(any(args[0] == 'kill' for args in self.rpc_calls))
        with self.engine.connect(write=True) as db: db.execute('DELETE FROM client_reservations')


def test_lost_response_live_cli_never_retries_or_expires(self):
    def lost(*args):
        self.rpc(*args)
        raise subprocess.TimeoutExpired('SECRET-sentinel', 1)
    self.actions.rpc = lost
    result = self.act()
    self.assertEqual(result['status'], 'unknown')
    self.assertEqual(result['reservation'], 'held')
    self.assertIsNone(a.reservations(self.engine)[0]['hold_identity'])
    self.now += 10**9
    self.actions = a.ClientActions(self.engine, self.evidence, rpc=lambda *args: self.fail('second RPC'))
    for _ in range(2):
        self.assertEqual(self.act(), result)
        self.assertEqual(self.act(dict(self.r, op='status', action_op='hold'))['status'], 'unknown')
    other = copy.deepcopy(self.r); other['action_id'] = 'another-action'
    self.evidence.identity = other['expected']
    self.assertEqual(self.act(other)['status'], 'deferred')
    self.assertTrue(self.engine.client_blocked('suites'))
    self.assertTrue(self.engine.client_blocked('suites', opening=True))
    self.assertEqual(len(self.rpc_calls), 1)


def test_hold_blocks_reorder_with_visible_reason(self):
    self.assertEqual(self.act()['status'], 'held')
    capabilities = self.engine.view()
    view = capabilities['reorder_groups']['suites']
    self.assertEqual(capabilities['capabilities']['reason_reorder'], view['reason'])
    self.assertFalse(view['enabled']); self.assertIn('reserva cliente', view['reason'])
    result = self.engine.handle(self.order_request())
    self.assertEqual((result.get('error') or {}).get('code'), 'client_reserved')
    self.assertEqual(self.p.calls, [])


def test_dead_reorder_blocks_clients_without_rpc(self):
    for phase in ('prepared', 'pause_effect'):
        r = self.order_request('dead-reorder-' + phase.replace('_', '-'))
        self.engine.reorder_boundary = lambda p: (_ for _ in ()).throw(test_reorder.Crash()) if p == phase else None
        with self.assertRaises(test_reorder.Crash): self.engine.handle(r)
        self.engine.reorder_boundary = lambda p: None
        for op in ('hold', 'cancel-no-payload', 'signal'):
            action = next(copy.deepcopy(v['request']) for v in CORPUS['valid'] if v['request']['op'] == op)
            action['action_id'] = 'wait-reorder-' + op + '-' + phase.replace('_', '-')
            if action['acceptance']: action['acceptance']['action_id'] = action['action_id']
            self.evidence.identity = action['expected']
            if op == 'cancel-no-payload': self.seed(action)
            self.assertEqual(self.act(action)['status'], 'deferred')
            if op == 'cancel-no-payload':
                with self.engine.connect(write=True) as db: db.execute('DELETE FROM client_reservations')
        self.assertEqual(self.rpc_calls, [])
        self.assertTrue(self.engine.reconcile())


def test_owner_death_after_reservation_with_free_flock(self):
    ctx = multiprocessing.get_context('spawn'); parent, child = ctx.Pipe()
    process = ctx.Process(target=worker, args=(str(self.root), self.r, child, True))
    process.start(); process.join(8)
    if process.is_alive():
        process.terminate(); process.join(); self.fail('crash boundary not reached')
    self.assertEqual(process.exitcode, 0)
    with (self.root / 'control.lock').open('rb') as lock: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    self.assertEqual(self.act()['status'], 'unknown')
    self.assertTrue(self.engine.client_blocked('suites')); self.assertEqual(self.rpc_calls, [])
    parent.close(); child.close()


def test_real_processes_control_lock_vs_result_lock(self):
    ctx = multiprocessing.get_context('spawn'); parent, child = ctx.Pipe()
    holder = ctx.Process(target=hold_lock, args=(str(self.root), child)); holder.start()
    self.assertTrue(parent.poll(8)); self.assertEqual(parent.recv(), 'locked')
    receiver, sender = ctx.Pipe()
    client = ctx.Process(target=worker, args=(str(self.root), self.r, sender)); client.start()
    try:
        self.assertTrue(receiver.poll(8), 'result lock deadlock')
        result = receiver.recv()
        self.assertIsInstance(result, dict, 'RPC escaped real lock')
        self.assertEqual((result['status'], result['reason']), ('deferred', 'lock_busy'))
        client.join(8); self.assertFalse(client.is_alive())
    finally:
        parent.send('release'); holder.join(8)
        for process in (client, holder):
            if process.is_alive(): process.terminate(); process.join()
        for conn in (parent, child, receiver, sender): conn.close()
    self.assertEqual(holder.exitcode, 0); self.assertEqual(client.exitcode, 0)


def test_manual_pressure_and_cancel_guard_reopening(self):
    self.assertEqual(self.act()['status'], 'held')
    r = request('remote-run-started'); self.evidence.acceptance = r['acceptance']
    self.change_state(startup_version=1, startup=dict(phase='ready', statuses={}, groups={}, holds={}, interruptions={}, source_hash='a'*64, target_hash='b'*64))
    for reason in ('manual', 'pressure', 'startup'):
        self.change_state(reasons={'suites': [reason]}); self.p.groups['suites']['status'] = 'Paused'
        with self.engine.connect(write=True) as db: db.execute("DELETE FROM client_actions WHERE phase='cancel-no-payload'")
        self.assertEqual(self.act(r)['reason'], reason)
    self.assertFalse(self.engine.client_blocked('suites', opening=True))
    self.change_state(reasons={'suites': ['pressure']})
    self.engine.guard(dict(load=0, memory=1))
    self.assertEqual(self.p.groups['suites']['status'], 'Running')
    self.assertTrue(self.engine.client_blocked('suites'))
    self.assertEqual(self.rpc_calls, [('stash', 77)])


def test_fence_real_reason_and_no_manual_open(self):
    self.assertEqual(self.act()['status'], 'held')
    r = request('remote-run-started'); self.evidence.acceptance = r['acceptance']
    self.engine.fence_pause('suites', 'pressure')
    self.assertEqual(self.act(r)['reason'], 'pressure')
    with self.engine.connect(write=True) as db: db.execute("DELETE FROM client_actions WHERE phase='cancel-no-payload'")
    self.engine.fence_pause('suites')
    self.assertEqual(self.act(r)['reason'], 'manual')
    self.assertEqual(self.rpc_calls, [('stash', 77)])


def test_phase_mode_cannot_change(self):
    self.assertEqual(self.act()['status'], 'held')
    r = request('remote-run-started'); r['mode'] = 'own'
    self.evidence.identity, self.evidence.acceptance = r['expected'], r['acceptance']
    self.assertEqual(self.act(r)['reason'], 'identity')
    self.assertEqual(len(self.rpc_calls), 1)


def test_prune_keeps_done_identity_until_finish(self):
    self.assertEqual(self.act()['status'], 'held')
    self.assertIn('77', control.admission_evidence(self.root))
    self.assertEqual(control.select_prune({'77': dict(status={'Done': dict(
        end='2020-01-01T00:00:00Z', result='Success')}, dependencies=[])},
        2000000000, protected=control.admission_evidence(self.root))[0], [])


def test_foreign_stash_after_rejected_hold_is_never_adopted(self):
    for op, mode in (('cancel-no-payload', 'rack-queued'), ('retire-held', 'own-legacy')):
        with self.subTest(op=op):
            with self.engine.connect(write=True) as db: db.execute('DROP TABLE IF EXISTS client_reservations')
            self.rpc_calls.clear()
            hold = dict(self.r, mode=mode, action_id='foreign-stash-' + op)
            stashed = dict(hold['expected'], state='Stashed', enqueued_at=None, started_at=None)
            self.evidence = Evidence(hold['expected'])
            self.evidence.change_at[2] = stashed
            self.actions = a.ClientActions(self.engine, self.evidence, rpc=lambda *args: self.rpc(*args))
            self.assertEqual(self.act(hold)['reason'], 'stale_state')
            follow = request('remote-run-started') if op == 'cancel-no-payload' else dict(hold)
            follow.update(op=op, phase=op, mode=mode, action_id=hold['action_id'], expected=stashed)
            if follow['acceptance']: follow['acceptance']['action_id'] = hold['action_id']
            self.evidence.acceptance = follow['acceptance']
            self.after = copy.deepcopy(CASES['remote-run-started']['response']['identity']) if op == 'cancel-no-payload' else dict(stashed, state='Absent')
            before = copy.deepcopy(a.reservations(self.engine))
            # Attribution must survive a fresh coordinator, independently of its receipt cache.
            self.actions = a.ClientActions(self.engine, self.evidence, rpc=lambda *args: self.rpc(*args))
            result = self.act(follow)
            self.assertEqual((result['status'], result['reason'], result['reservation']),
                             ('rejected', 'evidence_missing', 'held'))
            self.assertEqual(self.rpc_calls, [])
            self.assertEqual(a.reservations(self.engine), before)
            with self.engine.connect(write=True) as db: db.execute('DELETE FROM client_reservations')


def test_physical_missing_client_modules_preserve_host_and_orphan_journal(self):
    source = Path(control.__file__).resolve().parent
    for missing in (('client_actions.py',), ('client_contract.py',), ('editor_prioridad.py',), ('client_actions.py', 'client_contract.py')):
        with self.subTest(missing=missing), tempfile.TemporaryDirectory(prefix='cc1-install-', dir='/tmp') as folder:
            with self.engine.connect(write=True) as db: db.execute('DROP TABLE IF EXISTS client_reservations')
            install = Path(folder)
            for name in ('control.py', 'editor_prioridad.py', 'client_actions.py', 'client_contract.py'):
                if name not in missing: shutil.copy2(source / name, install / name)
            module = types.ModuleType('isolated_control')
            module.__file__ = str(install / 'control.py')
            exec(compile((install / 'control.py').read_bytes(), module.__file__, 'exec'), module.__dict__)
            module.pressure_mini = lambda: dict(load=0, memory=1)
            engine = module.MiniControl(self.p, self.root, clock=lambda: self.now, generation=lambda: self.gen)
            engine.coordinated_reorder = True
            engine.reorder_host_ready = lambda state: True
            engine.reorder_aliases = lambda state: {}
            inode = (self.root / 'control.lock').stat().st_ino
            # A pre-CC1 authority needs no companion file for ordinary host reads/writes.
            self.assertFalse(engine.client_blocked('suites'))
            self.assertFalse(engine.client_blocked('suites', opening=True))
            self.assertEqual(engine.view()['status'], 'ok')
            self.assertEqual(module.admission_evidence(self.root), set())
            self.change_state(startup_version=1, startup=dict(phase='ready', statuses={}, groups={},
                              holds={}, interruptions={}, source_hash='a'*64, target_hash='b'*64))
            self.change_state(reasons={'suites': ['pressure']})
            self.p.groups['suites']['status'] = 'Paused'
            engine.guard(dict(load=0, memory=1))
            self.assertEqual(self.p.groups['suites']['status'], 'Running')
            self.change_state(reasons={})
            self.p.calls.clear()
            unavailable = subprocess.run([os.sys.executable, '-B', module.__file__, 'client'],
                                         input=b'{"schema":"pueue-client-control@1","op":"capabilities"}',
                                         capture_output=True, timeout=10)
            self.assertEqual(unavailable.returncode, 2)
            self.assertIn(b'faltan m', unavailable.stderr)
            self.assertEqual(unavailable.stdout, b'')
            for uncertain in (False, True):
                self.seed(self.r, uncertain=uncertain)
                before = a.reservations(self.engine)
                try:
                    view = engine.view()
                except FileNotFoundError:
                    self.fail('host view must survive physically absent client modules')
                self.assertEqual(view['status'], 'ok')
                self.assertIs(view['capabilities'].get('client'), False)
                for group in engine.profile:
                    self.assertTrue(engine.client_blocked(group, opening=True))
                    self.assertEqual(view['reorder_groups'][group]['reason'], engine.client_reservation_reason)
                self.assertEqual(engine.handle(self.order_request('orphan-' + str(uncertain)))['error']['code'], 'client_reserved')
                done = dict(test_reorder.task(90), status={'Done': dict(end='2020-01-01T00:00:00Z', result='Success')})
                self.tasks['90'] = done
                self.p.calls.clear()
                module.prune_history(self.p, self.root, now=2000000000 + int(uncertain) * 4000)
                self.assertEqual(self.p.calls, [])
                self.change_state(reasons={'suites': ['pressure']})
                self.p.groups['suites']['status'] = 'Paused'
                for _ in range(2):
                    engine.guard(dict(load=0, memory=1))
                    self.assertEqual(self.p.groups['suites']['status'], 'Paused')
                    with self.engine.connect() as db: self.assertEqual(self.engine.state(db)['reasons']['suites'], ['pressure'])
                self.change_state(reasons={'suites': ['manual']})
                with self.assertRaises(ValueError): engine.manual('suites', resume=True)
                self.assertFalse(any(call[0] in ('start', 'remove') for call in self.p.calls))
                self.assertEqual(a.reservations(self.engine), before)
                self.assertEqual((self.root / 'control.lock').stat().st_ino, inode)
                with self.engine.connect(write=True) as db: db.execute('DROP TABLE client_reservations')
                self.change_state(reasons={})
                self.p.groups['suites']['status'] = 'Running'


def test_prune_history_removes_only_unreserved_done_task(self):
    self.assertEqual(self.act()['status'], 'held')
    old = dict(status={'Done': dict(end='2020-01-01T00:00:00Z', result='Killed')}, dependencies=[])
    self.tasks['77'], self.tasks['90'] = copy.deepcopy(old), copy.deepcopy(old)
    removed = []
    self.p.call = lambda *args: removed.append(args) or 'Tasks removed from list: 90\n'
    control.prune_history(self.p, self.root, now=2000000000)
    self.assertEqual(removed, [('remove', '90')])
    self.assertTrue(a.reservations(self.engine)[0]['open'])


def test_payload_env_hygiene_and_closed_failure(self):
    sentinel = 'SECRET-CLIENT-ACTIONS-DO-NOT-PERSIST'
    self.tasks['1']['envs']['PRIVATE'] = sentinel; self.tasks['1']['command'] = sentinel
    self.actions.rpc = lambda *args: (_ for _ in ()).throw(OSError(sentinel))
    result = self.act(); self.assertEqual(result['status'], 'unknown')
    for path in self.root.rglob('*'):
        if path.is_file(): self.assertNotIn(sentinel.encode(), path.read_bytes(), str(path))
    self.assertNotIn(sentinel, json.dumps(result))


def test_installed_adapters_fail_closed_capabilities_readonly(self):
    actions = a.ClientActions(self.engine, rpc=lambda *args: self.fail('raw fallback'))
    before = self.fingerprint()
    result, code = actions.dispatch(json.dumps(dict(schema=c.SCHEMA, op='capabilities')).encode())
    self.assertEqual((code, result['ops']), (0, list(c.OPS)))
    for launcher in c.LAUNCHERS:
        r = dict(schema=c.SCHEMA, op='capabilities', launcher=launcher, ref='test-ref-0001')
        result, code = actions.dispatch(json.dumps(r).encode())
        self.assertEqual((code, result['identity'], result['reason']), (0, None, 'unsupported'))
    self.assertEqual(self.fingerprint(), before)
    result, code = actions.dispatch(json.dumps(self.r).encode())
    self.assertEqual((code, result['status'], result['reason']), (0, 'rejected', 'unsupported'))


def test_full_corpus_through_coordinator_and_syntax_exit(self):
    actions = a.ClientActions(self.engine)
    for case in CORPUS['valid']:
        r = copy.deepcopy(case['request'])
        with self.subTest(case=case['id']):
            response, code = actions.dispatch(json.dumps(r).encode())
            self.assertEqual(code, 0); self.assertEqual(c.validate_response(response, r), response)
    for raw in (b'{}', b'\xff', b'{"a":1,"a":2}', b'[' * 2000, b'x' * (c.MAX_BYTES + 1)):
        self.assertEqual(actions.dispatch(raw), (c.invalid_request_response(), 2))
    class Bounded(io.BytesIO):
        def read(stream, size=-1):
            self.assertEqual(size, c.MAX_BYTES + 1)
            return super().read(size)
    output = io.StringIO()
    self.assertEqual(a.main(self.engine, Bounded(b'x' * 100000), output), 2)
    self.assertEqual(json.loads(output.getvalue()), c.invalid_request_response())


def test_child_intent_precedes_gate_timeout_unknown(self):
    class Pipe:
        closed = False
        def write(pipe, data):
            process = self.actions.record(self.r)['process']
            self.assertIsNotNone(process, 'child identity must be durable before gate')
            self.assertEqual(process['state'], 'gated')
            self.assertTrue(a.reservations(self.engine)[0]['uncertain']); self.assertEqual(data, b'1')
        def close(pipe): pipe.closed = True
    class Child:
        pid = 123
        stdin = Pipe()
        killed = False
        def wait(child, timeout=None):
            if timeout is not None: raise subprocess.TimeoutExpired('closed', timeout)
            return -9
        def poll(child): return None if not child.killed else -9
        def kill(child): child.killed = True
    child = Child()
    self.engine.pueue.binary, self.engine.pueue.context = '/synthetic/pueue', {}
    self.actions.rpc = None; self.actions.birth = lambda pid: '123.456'
    with patch.object(a.subprocess, 'Popen', return_value=child) as popen: result = self.act()
    self.assertEqual(result['status'], 'unknown'); self.assertTrue(child.killed)
    self.assertTrue(a.reservations(self.engine)[0]['uncertain']); self.assertEqual(popen.call_count, 1)
    self.assertEqual(self.actions.record(self.r)['process']['birth'], '123.456')


def test_surviving_cli_late_effect_cannot_overlap_new_action(self):
    ctx = multiprocessing.get_context('spawn')
    parent, child = ctx.Pipe()
    cli = ctx.Process(target=late_cli, args=(child,))
    cli.start()
    try:
        self.assertTrue(parent.poll(8)); self.assertEqual(parent.recv(), 'alive')
        def lost(*args):
            self.rpc_calls.append(args)
            raise subprocess.TimeoutExpired('private-cli', 1)
        self.actions.rpc = lost
        self.assertEqual(self.act()['status'], 'unknown')
        self.assertTrue(cli.is_alive())
        other = copy.deepcopy(self.r); other['action_id'] = 'late-effect-0001'
        self.assertEqual(self.act(other)['status'], 'deferred')
        self.assertEqual((self.engine.handle(self.order_request()).get('error') or {}).get('code'), 'client_reserved')
        parent.send('release')
        self.assertTrue(parent.poll(8)); self.assertEqual(parent.recv(), 'late-effect')
        cli.join(8)
        self.assertTrue(self.engine.client_blocked('suites'), 'child exit is not reconciliation')
        self.assertEqual(len(self.rpc_calls), 1)
    finally:
        if cli.is_alive(): cli.terminate(); cli.join()
        parent.close(); child.close()
    self.assertEqual(cli.exitcode, 0)


def test_identity_replaced_between_phases_is_not_adopted(self):
    self.assertEqual(self.act()['status'], 'held')
    r = request('remote-run-started')
    self.evidence.identity = dict(r['expected'], fingerprint='d'*64)
    self.evidence.acceptance = r['acceptance']
    self.assertEqual(self.act(r)['reason'], 'identity')
    self.assertEqual(len(self.rpc_calls), 1)


def test_unknown_prevents_guard_opening(self):
    self.actions.rpc = lambda *args: (_ for _ in ()).throw(OSError('private'))
    self.assertEqual(self.act()['status'], 'unknown')
    self.change_state(reasons={'suites': ['pressure']})
    self.p.groups['suites']['status'] = 'Paused'
    for _ in range(2):
        self.engine.guard(dict(load=0, memory=1))
        self.assertEqual(self.p.groups['suites']['status'], 'Paused')
        # The client block must not erase pressure into a later manual pause.
        with self.engine.connect() as db: self.assertEqual(self.engine.state(db)['reasons']['suites'], ['pressure'])
    self.assertFalse(any(call[0] == 'start' for call in self.p.calls))


def test_lock_busy_keeps_an_existing_reservation_held(self):
    r = request('remote-run-signal')
    self.evidence = Evidence(r['expected']); self.evidence.acceptance = r['acceptance']
    self.actions = a.ClientActions(self.engine, self.evidence, rpc=lambda *args: self.rpc(*args))
    self.after = copy.deepcopy(CASES['remote-run-signal']['response']['identity'])
    self.assertEqual(self.act(r)['reservation'], 'held')
    kill = copy.deepcopy(r); kill.update(signal='KILL', phase='signal:KILL')
    calls = list(self.rpc_calls)
    ctx = multiprocessing.get_context('spawn'); parent, child = ctx.Pipe()
    holder = ctx.Process(target=hold_lock, args=(str(self.root), child)); holder.start()
    try:
        self.assertTrue(parent.poll(8) and parent.recv() == 'locked')
        busy = self.act(kill)
    finally:
        parent.send('release'); holder.join(8)
    self.assertEqual((busy['status'], busy['reason'], busy['reservation']), ('deferred', 'lock_busy', 'held'))
    self.assertEqual(self.rpc_calls, calls)
    self.assertTrue(a.reservations(self.engine)[0]['open'])


def test_client_cli_refuses_desk_without_journal(self):
    class Input:
        buffer = io.BytesIO(json.dumps(dict(schema=c.SCHEMA, op='capabilities')).encode())
    output = io.StringIO()
    with patch('sys.argv', ['control.py', 'client']), patch.object(control.sys, 'platform', 'linux'), \
            patch.object(control, 'Pueue', return_value=self.p), patch('sys.stdin', Input()), \
            patch.object(control.Path, 'home', return_value=self.root), patch('sys.stdout', output), \
            patch('sys.stderr', io.StringIO()) as error:
        with self.assertRaises(SystemExit) as exit_:
            control.main()
    self.assertEqual(exit_.exception.code, 2)
    self.assertIn('desk', error.getvalue())
    self.assertEqual(output.getvalue(), '')
    with self.engine.connect() as db:
        self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='client_actions'").fetchone())


def test_cli_uses_installation_and_bounds_stdin(self):
    raw = json.dumps(dict(schema=c.SCHEMA, op='capabilities')).encode()
    class Input:
        buffer = io.BytesIO(raw)
    output = io.StringIO()
    with patch('sys.argv', ['control.py', 'client']), patch.object(control.sys, 'platform', 'darwin'), patch.object(control, 'Pueue', return_value=self.p), patch.object(control.Path, 'home', return_value=self.root), patch('sys.stdin', Input()), patch('sys.stdout', output):
        self.assertEqual(control.main(), 0)
    self.assertEqual(c.parse(output.getvalue(), response=True)['ops'], list(c.OPS))
    self.assertEqual(self.p.timeout, 1.0)
    output = io.StringIO()
    with patch('sys.argv', ['control.py', 'client', '--host', 'mini']), patch.object(control.sys, 'platform', 'darwin'), patch('sys.stdout', output):
        self.assertEqual(control.main(), 2)
    self.assertEqual(json.loads(output.getvalue()), c.invalid_request_response())


def test_unknown_prevents_manual_resume_without_losing_manual_reason(self):
    self.actions.rpc = lambda *args: (_ for _ in ()).throw(OSError('private'))
    self.assertEqual(self.act()['status'], 'unknown')
    self.change_state(reasons={'suites': ['manual']})
    self.p.groups['suites']['status'] = 'Paused'
    with self.assertRaises(ValueError): self.engine.manual('suites', resume=True)
    with self.engine.connect() as db:
        self.assertEqual(self.engine.state(db)['reasons']['suites'], ['manual'])
    self.assertFalse(any(call[0] == 'start' for call in self.p.calls))


def test_locked_second_read_and_changed_generation_preserve_intent(self):
    self.evidence.change_at[2] = dict(self.r['expected'], state='Locked', enqueued_at=None)
    self.assertEqual(self.act()['reason'], 'locked')
    self.assertEqual(self.rpc_calls, [])
    self.assertTrue(self.engine.client_blocked('suites'))
    self.gen = 'another-daemon'
    self.actions = a.ClientActions(self.engine, self.evidence, rpc=lambda *args: self.fail('generation RPC'))
    other = copy.deepcopy(self.r); other['action_id'] = 'changed-generation'
    self.assertEqual(self.act(other)['reason'], 'startup')


def test_busy_lock_returns_history_conflict_and_no_identity_callback(self):
    original = self.act()
    ctx = multiprocessing.get_context('spawn'); parent, child = ctx.Pipe()
    holder = ctx.Process(target=hold_lock, args=(str(self.root), child)); holder.start()
    try:
        self.assertTrue(parent.poll(8)); self.assertEqual(parent.recv(), 'locked')
        self.assertEqual(self.act(), original)
        changed = copy.deepcopy(self.r); changed['mode'] = 'own'
        self.assertEqual(self.act(changed)['reason'], 'idempotency_conflict')
        scoped = dict(schema=c.SCHEMA, op='capabilities', launcher=self.r['launcher'], ref=self.r['ref'])
        with patch.object(self.evidence, 'observe', side_effect=AssertionError('identity without lock')):
            result = self.actions.handle(scoped)
        self.assertEqual((result['identity'], result['reason']), (None, 'unsupported'))
    finally:
        parent.send('release'); holder.join(8)
        if holder.is_alive(): holder.terminate(); holder.join()
        parent.close(); child.close()
    self.assertEqual(holder.exitcode, 0)
    self.assertEqual(len(self.rpc_calls), 1)


def test_term_kill_preserve_first_acceptance(self):
    case = next(v for v in CORPUS['valid'] if v['request']['op'] == 'signal' and
                v['response'].get('status') == 'signalled' and v['response']['identity']['state'] == 'Running')
    r = copy.deepcopy(case['request'])
    self.evidence = Evidence(r['expected']); self.evidence.acceptance = r['acceptance']
    self.actions = a.ClientActions(self.engine, self.evidence, rpc=lambda *args: self.rpc(*args))
    self.after = copy.deepcopy(case['response']['identity'])
    self.assertEqual(self.act(r)['status'], 'signalled')
    kill = copy.deepcopy(r); kill.update(signal='KILL', phase='signal:KILL')
    kill['acceptance']['receipt'] = 'f'*64
    self.evidence.acceptance = kill['acceptance']
    self.assertEqual(self.act(kill)['reason'], 'identity')
    self.assertEqual(len(self.rpc_calls), 1)


for _name, _value in list(globals().items()):
    if _name.startswith('test_') and callable(_value): setattr(ActionTests, _name, _value)


class CorpusTests(unittest.TestCase):
    def test_expected_states_reservations_reasons_and_rpc(self):
        # CC0 also contains valid response controls which CC1 does not produce:
        # pending is a recovered journal state; finish is local, not deferred by manual.
        controls = {'hold-pending-scheduled-control', 'cancel-pending-scheduled-control',
                    'remote-run-finished-no-payload-deferred-none-control'}
        matched = 0
        for case in CORPUS['valid']:
            with self.subTest(case=case['id']):
                x = ActionTests()
                x.setUp()
                try:
                    r, expected = copy.deepcopy(case['request']), case['response']
                    original = c.action_projection(r) if r['op'] == 'status' else r
                    op, reason = original['op'], expected.get('reason')
                    if op == 'capabilities':
                        if expected.get('identity'):
                            x.evidence.identity = copy.deepcopy(expected['identity'])
                        elif 'launcher' in r:
                            x.actions.adapter = a.InstalledAdapters()
                        self.assertEqual(x.act(r), expected)
                        self.assertEqual(x.rpc_calls, [])
                        matched += 1
                        continue
                    x.evidence = Evidence(original['expected'])
                    x.evidence.acceptance, x.evidence.proof = original['acceptance'], original['proof']
                    x.actions = a.ClientActions(x.engine, x.evidence, rpc=lambda *args: x.rpc(*args))
                    if reason == 'host_action':
                        x.engine.reorder_boundary = lambda phase: (_ for _ in ()).throw(test_reorder.Crash()) if phase == 'prepared' else None
                        with self.assertRaises(test_reorder.Crash): x.engine.handle(x.order_request())
                    if op in ('cancel-no-payload', 'retire-held', 'finish'):
                        x.seed(original)
                    if reason in ('manual', 'pressure', 'startup'):
                        x.change_state(startup_version=1, startup=dict(phase='ready', statuses={}, groups={},
                                       holds={}, interruptions={}, source_hash='a'*64, target_hash='b'*64))
                        x.change_state(reasons={'suites': [reason]})
                        x.p.groups['suites']['status'] = 'Paused'
                    if expected['status'] == 'unknown':
                        def lost(*args):
                            x.rpc(*args)
                            raise subprocess.TimeoutExpired('synthetic', 1)
                        x.actions.rpc = lost
                    if expected['status'] == 'rejected' and reason == 'stale_state':
                        x.evidence.identity = copy.deepcopy(expected['identity'])
                    x.after = copy.deepcopy(expected['identity'])
                    if r['op'] == 'status' and expected['status'] == 'pending':
                        response = dict(expected, op=op)
                        response.pop('action_op')
                        x.actions.save(dict(request=original, digest=a.digest(original), revision=0,
                                            process=None, response=response))
                    else:
                        with (x.root / 'control.lock').open('rb') as lock:
                            if reason == 'lock_busy': fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            response = x.act(original)
                    answer = x.act(r) if r['op'] == 'status' else response
                    if case['id'] in controls:
                        # They still exercise the coordinator and CC0 response validation.
                        continue
                    self.assertEqual(tuple(answer[k] for k in ('status', 'reservation', 'reason')),
                                     tuple(expected[k] for k in ('status', 'reservation', 'reason')))
                    if expected['effects'] is False:
                        self.assertEqual(x.rpc_calls, [])
                    elif expected['effects'] is True:
                        self.assertEqual(len(x.rpc_calls), 1)
                    matched += 1
                finally:
                    x.doCleanups()
        self.assertEqual(matched, len(CORPUS['valid']) - len(controls))
        for case in CORPUS['invalid']:
            if case['target'] == 'request':
                with self.subTest(case=case['id']):
                    raw = json.dumps(case['value'], ensure_ascii=False).encode()
                    self.assertEqual(a.ClientActions(None).dispatch(raw), (c.invalid_request_response(), 2))


if __name__ == '__main__': unittest.main()
