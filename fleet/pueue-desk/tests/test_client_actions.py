"""CC1: coordinador de acciones cliente con Pueue, reloj y disco falsos; nunca el binario real."""
import copy
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

import test_slots
import test_reorder

control = test_slots.control
actions = control.client_actions()
contract = actions.contract
SCHEMA = contract.SCHEMA
REF = '20261116-120000-abcdef'
APO = '20261116-120500-0a0b0c'
JOB = 'job-Abcd1234'
STAMP = test_reorder.STAMP
LATER = '2026-11-16T12:03:00+00:00'
ACCEPTED = datetime(2026, 11, 16, 12, 1, tzinfo=timezone.utc).timestamp()
SECRET = 'CENTINELA-SECRETO-cc1'
CORPUS = json.loads((test_slots.BASE / 'corpus/client-control-v1.json').read_text())


class Crash(BaseException):
    pass


def write_json(path, value):
    path.write_text(json.dumps(value))


class ClientTests(test_slots.SlotsTests):
    pass


for _name in dir(test_slots.SlotsTests):
    if _name.startswith('test_'):
        setattr(ClientTests, _name, None)


def setup(self):
    test_reorder.setup(self)
    self.home = self.root / 'home'
    self.coord = actions.Coordinator(self.engine, home=self.home)
    self.late = []  # late effects of a lost RPC, applied by the test
    base_call = self.p.call

    def call(*args):
        verb = args[0]
        if verb in ('pause', 'start', 'enqueue') and '-g' in args or verb == 'enqueue':
            return base_call(*args)
        self.p.calls.append(args)
        if self.rpc_hook:
            self.rpc_hook(args)
        ident = args[-1]
        task = self.tasks.get(ident)
        name = next(iter(task['status'])) if task else None
        if verb == 'stash':
            if name not in ('Queued', 'Locked'):
                raise subprocess.CalledProcessError(1, ['fake-pueue'])
            task['status'] = {'Stashed': dict(enqueue_at=None)}
        elif verb == 'start':
            if name not in ('Queued', 'Stashed', 'Locked'):
                raise subprocess.CalledProcessError(1, ['fake-pueue'])
            task['status'] = {'Running': dict(enqueued_at=LATER, start=LATER)}
        elif verb == 'kill':
            if name not in ('Running', 'Paused'):
                raise subprocess.CalledProcessError(1, ['fake-pueue'])
            if args[2] == 'SIGKILL':
                task['status'] = {'Done': dict(enqueued_at=task['status'][name]['enqueued_at'],
                                               start=task['status'][name]['start'], end=LATER, result='Killed')}
        elif verb == 'remove':
            if name in (None, 'Running', 'Paused', 'Locked'):
                raise subprocess.CalledProcessError(1, ['fake-pueue'])
            del self.tasks[ident]
        else:
            raise AssertionError('No fallback to real Pueue')
        return ''
    self.rpc_hook = None
    self.p.call = call
    self.make_run(REF, 3)


ClientTests.setUp = setup


def order_request(self, ident):
    expected = self.engine.view()['reorder_groups']['suites']['expected']
    order = [r['id'] for r in expected['queue']]
    order.insert(0, order.pop())
    return dict(schema=control.HOST_SCHEMA, op='reorder', action_id=ident, dry_run=False, expected=expected,
                change=dict(order=order))


ClientTests.order_request = order_request


def startup_ready():
    return dict(startup_version=1, startup=dict(phase='ready', statuses={}, groups={}, holds={}, interruptions={},
                                                source_hash='0' * 64, target_hash='0' * 64))


def set_reasons(engine, reasons):
    with engine.connect(write=True) as db:
        state = engine.state(db)
        state['reasons'] = reasons
        if any('startup' in r for r in reasons.values()):
            state.update(startup_ready())
        engine.store(db, state)


ClientTests.set_reasons = lambda self, reasons: set_reasons(self.engine, reasons)


def make_run(self, ref, task_id, group='suites', manifest=True):
    folder = self.home / 'remote-run/runs' / ref
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'pueue.id').write_text(str(task_id) + '\n')
    if manifest:
        write_json(folder / 'run-control.json', dict(schema='rack-cancel@1', consumer='remote-run@2', ref=ref,
                   host='mini', group=group, task_id=task_id, mirror='remote-run/work/x', cancel_origin=True,
                   created_at=STAMP))
    return folder


ClientTests.make_run = make_run


def accept(self, ref=REF, action='action-0001', origin='rack-panel'):
    folder = self.home / 'remote-run/runs' / ref
    write_json(folder / 'pueue.attempt.json', dict(id='attempt-uuid-1', enqueued_at=STAMP, state='Stashed'))
    value = dict(action_id=action, started=ACCEPTED, attempt='attempt-uuid-1', kind='queued', enqueued_at=STAMP)
    if origin:
        value['origin'] = origin
    write_json(folder / 'pueue.cancellation.json', value)


def prove(self, ref=REF, no_payload=True):
    folder = self.home / 'remote-run/runs' / ref
    write_json(folder / 'pueue.worker-result.json', dict(exit=130, cancelled=True, attempt='attempt-uuid-1',
               payload_complete=not no_payload, no_payload=no_payload))
    write_json(folder / 'pueue.result.json', dict(exit=130, cancelled=True, attempt='attempt-uuid-1'))


ClientTests.accept = accept
ClientTests.prove = prove


def identity(self, ref=REF, launcher='remote-run'):
    answer = self.coord.handle(dict(schema=SCHEMA, op='capabilities', launcher=launcher, ref=ref))
    self.assertIsNotNone(answer['identity'], answer)
    return answer['identity']


def req(self, op, expected, action='action-0001', mode='rack-queued', acceptance=None, proof=None,
        signal=None, launcher='remote-run', ref=REF):
    body = dict(schema=SCHEMA, op=op, launcher=launcher, ref=ref, action_id=action,
                phase=op + (':' + signal if signal else ''), mode=mode, expected=expected,
                acceptance=acceptance, proof=proof)
    if signal:
        body['signal'] = signal
    return contract.validate_request(body)


def status_of(self, original):
    return contract.validate_request(dict(original, op='status', action_op=original['op']))


def act(self, request, locked=True):
    response = self.coord.handle(request, locked=locked)
    contract.validate_response(response, request)
    return response


ClientTests.identity = identity
ClientTests.req = req
ClientTests.status_of = status_of
ClientTests.act = act


def acceptance_of(self, ref=REF):
    adapter = self.coord.adapters['remote-run']
    run_ = adapter.resolve(ref, 'rack-queued')
    return adapter.acceptance(run_, self.identity(ref)['attempt'])


ClientTests.acceptance_of = acceptance_of


def hold(self, action='action-0001', ref=REF, mode='rack-queued'):
    request = self.req('hold', self.identity(ref), action=action, ref=ref, mode=mode)
    return request, self.act(request)


ClientTests.hold = hold


def journal(self):
    with self.engine.connect() as db:
        if not actions.tables_exist(db):
            return [], []
        return ([json.loads(r[0]) for r in db.execute('SELECT body FROM client_actions')],
                [json.loads(r[0]) for r in db.execute('SELECT body FROM client_reservations')])


ClientTests.journal = journal


def rpc_calls(self):
    return [c for c in self.p.calls if c[0] in ('stash', 'start', 'kill', 'remove') and '-g' not in c]


ClientTests.rpc_calls = rpc_calls


# ---------------------------------------------------------------- secuencias normales

def test_hold_cancel_finish_sequence_and_idempotent_receipts(self):
    request, held = self.hold()
    self.assertEqual((held['status'], held['reservation'], held['identity']['state']), ('held', 'held', 'Stashed'))
    self.assertEqual(self.rpc_calls(), [('stash', '3')])
    self.assertEqual(self.act(request), held)
    self.assertEqual(self.act(request, locked=False), held)  # lock busy: same receipt, no new effect
    other = dict(request, mode='own')
    self.assertEqual(self.act(contract.validate_request(other))['reason'], 'idempotency_conflict')
    # Restart: a new engine and coordinator read the same durable journal.
    self.engine = control.MiniControl(self.p, self.root, clock=lambda: self.now, generation=lambda: self.gen)
    self.coord = actions.Coordinator(self.engine, home=self.home)
    self.engine.coordinated_reorder = True
    self.assertEqual(self.act(request), held)
    self.assertEqual(self.act(contract.validate_request(other))['reason'], 'idempotency_conflict')
    self.assertEqual(self.rpc_calls(), [('stash', '3')])
    self.accept()
    stashed = self.identity()
    cancel = self.req('cancel-no-payload', stashed, acceptance=self.acceptance_of())
    started = self.act(cancel)
    self.assertEqual((started['status'], started['identity']['state']), ('started', 'Running'), started)
    self.assertEqual(self.rpc_calls(), [('stash', '3'), ('start', '3')])
    self.assertEqual(self.act(cancel), started)
    self.tasks['3']['status'] = {'Done': dict(enqueued_at=LATER, start=LATER, end=LATER, result={'Failed': 130})}
    self.prove()
    done = self.identity()
    adapter = self.coord.adapters['remote-run']
    proof = adapter.proof(adapter.resolve(REF, 'rack-queued'), done['attempt'], cancel['acceptance'])
    finish = self.req('finish', done, acceptance=cancel['acceptance'], proof=proof)
    finished = self.act(finish)
    self.assertEqual((finished['status'], finished['reservation']), ('finished', 'closed'))
    self.assertEqual(self.act(finish), finished)
    self.assertEqual(actions.blocked_groups(self.engine), {})
    self.assertEqual(self.act(self.status_of(request))['status'], 'held')
    self.assertEqual(self.act(self.status_of(finish))['status'], 'finished')
    self.assertEqual(len(self.rpc_calls()), 2)


def test_signal_term_kill_and_finish_close_running_reservation(self):
    self.tasks['3']['status'] = {'Running': dict(enqueued_at=STAMP, start=LATER)}
    current = self.identity()
    term = self.req('signal', current, mode='own', signal='TERM')
    self.assertEqual(self.act(term)['status'], 'signalled')
    self.assertEqual(actions.blocked_groups(self.engine), {'suites': 'reservation'})
    kill = self.req('signal', current, mode='own', signal='KILL')
    killed = self.act(kill)
    self.assertEqual((killed['status'], killed['identity']['state']), ('signalled', 'Done'))
    self.assertEqual(self.rpc_calls(), [('kill', '-s', 'SIGTERM', '3'), ('kill', '-s', 'SIGKILL', '3')])
    # rack-running needs the launcher's durable acceptance, never the requester's claim.
    self.make_run('20261116-120100-bbbbbb', 4)
    self.tasks['4']['status'] = {'Running': dict(enqueued_at=STAMP, start=LATER)}
    other = self.identity('20261116-120100-bbbbbb')
    forged = dict(action_id='action-0009', attempt=other['attempt'], accepted_at=other['attempt'],
                  origin='rack-panel', receipt='c' * 64)
    rejected = self.act(self.req('signal', other, action='action-0009', mode='rack-running', signal='TERM',
                                 acceptance=forged, ref='20261116-120100-bbbbbb'))
    self.assertEqual((rejected['status'], rejected['reason']), ('rejected', 'evidence_missing'))
    self.assertEqual(len(self.rpc_calls()), 2)


def test_retire_held_and_interrupted_close_only_with_host_receipt(self):
    request, held = self.hold(mode='own-legacy')
    stashed = self.identity()
    retire = self.req('retire-held', stashed, mode='own-legacy')
    retired = self.act(retire)
    self.assertEqual((retired['status'], retired['identity']['state']), ('retired', 'Absent'))
    absent = retired['identity']
    good = dict(kind='interrupted', attempt=absent['attempt'], receipt=actions.receipt_digest(retired),
                acceptance_receipt=None, exit=143)
    finished = self.act(self.req('finish', absent, mode='own-legacy', proof=good))
    self.assertEqual((finished['status'], finished['reservation']), ('finished', 'closed'))
    # apotheca: Done interrupted without published exit, three-field pueue.id.
    folder = self.home / 'remote-run/runs' / APO
    folder.mkdir(parents=True)
    (folder / 'pueue.id').write_text('5 suites relevo\n')
    self.tasks['5']['status'] = {'Done': dict(enqueued_at=STAMP, start=LATER, end=LATER, result='Killed')}
    done = self.identity(APO, 'apotheca')
    out = self.act(self.req('retire-interrupted', done, mode='own', launcher='apotheca', ref=APO, action='action-0005'))
    self.assertEqual(out['status'], 'retired', out)
    proof = dict(kind='interrupted', attempt=done['attempt'], receipt=actions.receipt_digest(out),
                 acceptance_receipt=None, exit=143)
    # A receipt the host did not sign never closes the reservation.
    bogus = self.act(self.req('finish', out['identity'], mode='own', launcher='apotheca', ref=APO,
                              action='action-0005', proof=dict(proof, receipt='d' * 64)))
    self.assertEqual((bogus['status'], bogus['reason'], bogus['reservation']), ('rejected', 'evidence_missing', 'held'))
    self.assertEqual(actions.blocked_groups(self.engine), {'suites': 'reservation'})
    # The finish phase key is now bound to that body (CC0 idempotency); only CC2 recovery closes it.
    late = self.act(self.req('finish', out['identity'], mode='own', launcher='apotheca', ref=APO,
                             action='action-0005', proof=proof))
    self.assertEqual((late['reason'], late['reservation']), ('idempotency_conflict', 'held'))
    # A published normal result is never retired.
    self.make_run('20261116-120100-cccccc', 4)
    (self.home / 'remote-run/runs/20261116-120100-cccccc/exit').write_text('0\n')
    self.tasks['4']['status'] = {'Done': dict(enqueued_at=STAMP, start=LATER, end=LATER, result='Killed')}
    published = self.identity('20261116-120100-cccccc')
    self.assertEqual(self.act(self.req('retire-interrupted', published, mode='own', ref='20261116-120100-cccccc',
                                       action='action-0004'))['reason'], 'stale_state')
    self.assertNotIn(('remove', '4'), self.p.calls)


def test_intent_and_reservation_are_durable_before_every_rpc(self):
    seen = []

    def check(args):
        records, reservations = self.journal()
        mine = [r for r in records if r['state'] == 'intent']
        seen.append((args[0], len(mine), [r['state'] for r in reservations]))
        self.assertEqual(len(mine), 1, args)
        self.assertEqual(mine[0]['rpc'], args[0])
        self.assertIn('held', [r['state'] for r in reservations])
        self.assertEqual(actions.blocked_groups(self.engine), {'suites': 'unknown'})
    self.rpc_hook = check
    request, held = self.hold()
    self.accept()
    self.act(self.req('cancel-no-payload', self.identity(), acceptance=self.acceptance_of()))
    self.tasks['5']['status'] = {'Running': dict(enqueued_at=STAMP, start=LATER)}
    self.make_run('20261116-120100-dddddd', 5)
    self.act(self.req('signal', self.identity('20261116-120100-dddddd'), mode='own', signal='TERM',
                      ref='20261116-120100-dddddd', action='action-0005'))
    self.assertEqual([s[0] for s in seen], ['stash', 'start', 'kill'])


# ---------------------------------------------------------------- candados y procesos

LOCK_DRIVER = textwrap.dedent('''
    import fcntl, json, sys, time
    from pathlib import Path
    root, role = Path(sys.argv[1]), sys.argv[2]
    sys.path.insert(0, sys.argv[3])
    import test_slots
    control = test_slots.control
    if role == 'owner':
        with (root/'control.lock').open('r') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            (root/'owner.ready').write_text('1')
            while not (root/'client.done').exists():
                time.sleep(.01)
            # With control.lock held: read the launcher's files while it holds result.lock.
            print(json.dumps(dict(read=(root/'run'/'pueue.id').read_text().strip())))
    else:
        with (root/'run'/'pueue.control').open('a') as result:
            fcntl.flock(result, fcntl.LOCK_EX)
            while not (root/'owner.ready').exists():
                time.sleep(.01)
            class Refuse:
                calls = []
                def call(self, *a): raise AssertionError('client RPC with control.lock held elsewhere')
                def status(self): raise AssertionError('client read before lock')
            engine = control.MiniControl(Refuse(), root, generation=lambda: 'daemon-1')
            body = json.loads(sys.argv[4])
            try:
                code, answer = control.client_cli(__import__('io').BytesIO(json.dumps(body).encode()), engine, root)
            finally:
                (root/'client.done').write_text('1')
            print(json.dumps(dict(code=code, answer=answer)))
''')


def test_real_lock_between_processes_without_effect_or_deadlock(self):
    request, _ = self.hold(action='action-0010')  # creates the journal tables under this test's lock
    (self.root / 'run').mkdir()
    (self.root / 'run/pueue.id').write_text('3\n')
    driver = self.root / 'driver.py'
    driver.write_text(LOCK_DRIVER)
    self.make_run('20261116-120100-121212', 2)
    body = self.req('hold', self.identity('20261116-120100-121212'), action='action-0011',
                    ref='20261116-120100-121212')
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', TMPDIR='/tmp')
    tests = str(Path(__file__).resolve().parent)
    owner = subprocess.Popen([sys.executable, str(driver), str(self.root), 'owner', tests], stdout=subprocess.PIPE,
                             env=env, text=True)
    client = subprocess.Popen([sys.executable, str(driver), str(self.root), 'client', tests, json.dumps(body)],
                              stdout=subprocess.PIPE, env=env, text=True)
    started = time.monotonic()
    out_client, _ = client.communicate(timeout=20)
    out_owner, _ = owner.communicate(timeout=20)
    self.assertLess(time.monotonic() - started, 15)
    self.assertEqual(client.returncode, 0, out_client)
    self.assertEqual(owner.returncode, 0, out_owner)
    answer = json.loads(out_client)
    self.assertEqual(answer['code'], 0)
    self.assertEqual((answer['answer']['status'], answer['answer']['reason'], answer['answer']['reservation']),
                     ('deferred', 'lock_busy', 'none'))
    self.assertEqual(json.loads(out_owner)['read'], '3')
    records, _ = self.journal()
    self.assertNotIn('action-0011', json.dumps(records))


def test_lock_free_but_dead_owner_after_reservation_defers_without_rpc(self):
    self.coord.boundary = lambda phase: (_ for _ in ()).throw(Crash()) if phase == 'intent' else None
    request = self.req('hold', self.identity())
    with self.assertRaises(Crash):
        self.coord.handle(request)
    self.coord.boundary = lambda phase: None
    self.assertEqual(self.rpc_calls(), [])
    # Same body: unknown, never a second stash.
    again = self.act(request)
    self.assertEqual((again['status'], again['reservation']), ('unknown', 'held'))
    self.make_run('20261116-120100-eeeeee', 2)
    other = self.act(self.req('hold', self.identity('20261116-120100-eeeeee'), action='action-0002',
                              ref='20261116-120100-eeeeee'))
    self.assertEqual((other['status'], other['reason'], other['reservation']), ('deferred', 'rpc_unresolved', 'none'))
    self.assertEqual(self.rpc_calls(), [])
    reorder = self.engine.handle(self.order_request('reorder-blocked-01'))
    self.assertEqual(reorder['error']['code'], 'client_action', reorder)
    self.assertNotIn(('pause', '--wait', '-g', 'suites'), self.p.calls)
    view = self.engine.view()['reorder_groups']['suites']
    self.assertEqual((view['enabled'], view['reason']), (False, control.CLIENT_BLOCK_MESSAGE))
    self.assertEqual(self.act(self.status_of(request))['status'], 'unknown')


def test_dead_reorder_after_pause_defers_hold_and_cancel_without_rpc(self):
    request, held = self.hold(action='action-0003')
    self.accept(action='action-0003')
    acceptance = self.acceptance_of()
    stashed = self.identity()
    for phase in ('prepared', 'paused'):
        self.p.groups['suites']['status'] = 'Running'
        self.tasks['3']['status'] = {'Queued': dict(enqueued_at=STAMP)}
        self.journal_reset()
        self.engine.reorder_boundary = lambda p, phase=phase: (_ for _ in ()).throw(Crash()) if p == phase else None
        with self.assertRaises(Crash):
            self.engine.handle(self.order_request('reorder-crash-' + phase[:4]))
        self.engine.reorder_boundary = lambda p: None
        calls = len(self.rpc_calls())
        out = self.act(self.req('hold', self.identity(), action='action-00' + phase[:2]))
        self.assertEqual((out['status'], out['reason'], out['reservation']), ('deferred', 'host_action', 'none'))
        self.assertEqual(len(self.rpc_calls()), calls)
        self.assertTrue(self.engine.reconcile())
    self.assertEqual(stashed['state'], 'Stashed')


def journal_reset(self):
    with self.engine.connect(write=True) as db:
        actions.ensure_tables(db)
        db.execute('DELETE FROM client_actions')
        db.execute('DELETE FROM client_reservations')


ClientTests.journal_reset = journal_reset


def test_both_orders_hold_versus_reorder(self):
    # Order 1: hold wins, reorder is rejected before any pause.
    request, held = self.hold()
    self.assertEqual(held['status'], 'held')
    calls = len(self.p.calls)
    answer = self.engine.handle(self.order_request('reorder-after-hold'))
    self.assertEqual(answer['error']['code'], 'client_action', answer)
    self.assertEqual(len(self.p.calls), calls)
    # Order 2: reorder owns the group (pause in progress); hold and cancel defer, no RPC.
    self.journal_reset()
    self.tasks['3']['status'] = {'Queued': dict(enqueued_at=STAMP)}
    results = {}

    def boundary(phase):
        if phase == 'pause_effect':
            calls = len(self.rpc_calls())
            r = self.req('hold', self.identity(), action='action-0020')
            results['hold'] = self.act(r)
            results['rpc'] = len(self.rpc_calls()) - calls
    self.engine.reorder_boundary = boundary
    done = self.engine.handle(self.order_request('reorder-first-0001'))
    self.engine.reorder_boundary = lambda p: None
    self.assertEqual(done['status'], 'applied', done)
    self.assertEqual((results['hold']['status'], results['hold']['reason']), ('deferred', 'host_action'))
    self.assertEqual(results['rpc'], 0)
    # After the reorder ends the same body re-evaluates and wins.
    self.assertEqual(self.act(self.req('hold', self.identity(), action='action-0020'))['status'], 'held')
    # Death between reservation and acceptance leaves a blocking reservation, not a free window.
    self.assertEqual(self.engine.handle(self.order_request('reorder-after-dead'))['error']['code'], 'client_action')


def test_cancel_defers_while_reorder_recovery_open(self):
    request, held = self.hold(action='action-0030')
    self.accept(action='action-0030')
    cancel = self.req('cancel-no-payload', self.identity(), action='action-0030', acceptance=self.acceptance_of())
    self.set_reasons({'suites': ['reorder']})
    out = self.act(cancel)
    self.assertEqual((out['status'], out['reason'], out['reservation']), ('deferred', 'host_action', 'held'))
    self.assertEqual(self.rpc_calls(), [('stash', '3')])
    self.assertEqual(self.act(self.status_of(cancel))['status'], 'pending')
    self.set_reasons({})
    self.assertEqual(self.act(cancel)['status'], 'started')


def test_queued_to_running_race_on_each_read_and_on_stash_never_signals(self):
    expected = self.identity()
    self.tasks['3']['status'] = {'Running': dict(enqueued_at=STAMP, start=LATER)}
    out = self.act(self.req('hold', expected))
    self.assertEqual((out['status'], out['reason'], out['reservation']), ('rejected', 'stale_state', 'none'))
    self.assertEqual(self.rpc_calls(), [])
    self.tasks['3']['status'] = {'Queued': dict(enqueued_at=STAMP)}

    def race(args):
        if args[0] == 'stash':
            self.tasks['3']['status'] = {'Running': dict(enqueued_at=STAMP, start=LATER)}
    self.rpc_hook = race
    out = self.act(self.req('hold', expected, action='action-0002'))
    self.assertEqual((out['status'], out['reason'], out['reservation']), ('rejected', 'stale_state', 'none'))
    self.assertEqual(self.rpc_calls(), [('stash', '3')])
    self.assertEqual(actions.blocked_groups(self.engine), {})
    self.assertFalse(any(c[0] == 'kill' for c in self.p.calls))


def test_locked_task_is_never_touched_by_any_mutating_operation(self):
    base = {'Queued': ('hold', 'own'), 'Stashed': ('cancel-no-payload', 'rack-queued'),
            'Running': ('signal', 'own'), 'Done': ('retire-interrupted', 'own')}
    for n, (state, (op, mode)) in enumerate(base.items()):
        self.journal_reset()
        self.tasks['3']['status'] = {'Queued': dict(enqueued_at=STAMP)}
        if op == 'cancel-no-payload':
            self.hold(action='action-10' + str(n) + '0')
            self.accept(action='action-10' + str(n) + '0')
        elif state == 'Running':
            self.tasks['3']['status'] = {'Running': dict(enqueued_at=STAMP, start=LATER)}
        elif state == 'Done':
            self.tasks['3']['status'] = {'Done': dict(enqueued_at=STAMP, start=LATER, end=LATER, result='Killed')}
        expected = self.identity()
        acceptance = self.acceptance_of() if op == 'cancel-no-payload' else None
        previous = self.tasks['3']['status']
        self.tasks['3']['status'] = {'Locked': dict(previous_status=previous)}
        calls = len(self.rpc_calls())
        out = self.act(self.req(op, expected, mode=mode, acceptance=acceptance, action='action-10' + str(n) + '0',
                                signal='TERM' if op == 'signal' else None))
        self.assertEqual((out['status'], out['reason']), ('rejected', 'locked'), (op, out))
        self.assertEqual(len(self.rpc_calls()), calls, op)
        self.assertIn('Locked', self.tasks['3']['status'])


def test_lost_response_and_surviving_cli_retain_unknown_and_block(self):
    def lost(args):
        if args[0] == 'stash':
            raise subprocess.TimeoutExpired(['fake-pueue'], 3)
    self.rpc_hook = lost
    request = self.req('hold', self.identity())
    out = self.act(request)
    self.assertEqual((out['status'], out['effects'], out['reservation'], out['reason']),
                     ('unknown', None, 'held', 'rpc_unresolved'))
    self.rpc_hook = None
    # The surviving CLI's late effect arrives; nothing reconciles it in CC1.
    self.tasks['3']['status'] = {'Stashed': dict(enqueue_at=None)}
    calls = len(self.rpc_calls())
    self.assertEqual(self.act(request), out)
    self.assertEqual(self.act(self.status_of(request))['status'], 'unknown')
    self.make_run('20261116-120100-ffffff', 2)
    other = self.act(self.req('hold', self.identity('20261116-120100-ffffff'), action='action-0002',
                              ref='20261116-120100-ffffff'))
    self.assertEqual((other['status'], other['reason']), ('deferred', 'rpc_unresolved'))
    self.assertEqual(len(self.rpc_calls()), calls)
    self.assertEqual(self.engine.handle(self.order_request('reorder-unknown-01'))['error']['code'], 'client_action')
    self.assertEqual(actions.blocked_groups(self.engine), {'suites': 'unknown'})


def test_manual_and_pressure_never_open_and_guard_reopens_with_own_reservation(self):
    request, held = self.hold(action='action-0040')
    self.accept(action='action-0040')
    cancel = self.req('cancel-no-payload', self.identity(), action='action-0040', acceptance=self.acceptance_of())
    for reason in ('manual', 'pressure', 'startup'):
        for status in ('Paused', 'Running'):  # the pueue group may still run before the guard closes it
            self.set_reasons({'suites': [reason]})
            self.p.groups['suites']['status'] = status
            out = self.act(cancel)
            self.assertEqual((out['status'], out['reason'], out['reservation']), ('deferred', reason, 'held'))
            self.assertEqual(self.p.groups['suites']['status'], status)
    self.p.groups['suites']['status'] = 'Paused'
    # The marker file is read with fence_reason, never downgraded (R6-sol-1).
    self.set_reasons({})
    self.engine.pause_fence('suites').write_text('pressure\n')
    self.assertEqual(self.act(cancel)['reason'], 'pressure')
    self.engine.pause_fence('suites').write_text('otra cosa\n')
    self.assertEqual(self.act(cancel)['reason'], 'manual')
    self.engine.pause_fence('suites').unlink()
    self.assertEqual(self.rpc_calls(), [('stash', '3')])
    # Pressure eases: the guard reopens despite our own cancel reservation.
    self.set_reasons({'suites': ['pressure']})
    for _ in range(3):
        self.engine.guard(dict(load=0, memory=1))
    self.assertEqual(self.p.groups['suites']['status'], 'Running')
    self.assertEqual(self.act(cancel)['status'], 'started')
    # Manual is never reopened by the guard, so a pending cancel stays deferred.
    self.set_reasons({'suites': ['manual']})
    self.p.groups['suites']['status'] = 'Paused'
    for _ in range(3):
        self.engine.guard(dict(load=0, memory=1))
    self.assertEqual(self.p.groups['suites']['status'], 'Paused')


def test_hygiene_no_env_command_or_payload_in_journal_receipts_or_logs(self):
    for task in self.tasks.values():
        task['envs'] = {'TOKEN': SECRET}
        task['command'] = task['original_command'] = 'echo ' + SECRET
        task['path'] = '/tmp/' + SECRET
    (self.home / 'remote-run/runs' / REF / 'meta').write_text('cmd=' + SECRET + '\n')
    (self.home / 'remote-run/runs' / REF / 'body.sh').write_text(SECRET)
    outputs = []
    request, held = self.hold()
    outputs.append(held)
    self.accept()
    outputs.append(self.act(self.req('cancel-no-payload', self.identity(), acceptance=self.acceptance_of())))
    outputs.append(self.act(self.status_of(request)))
    outputs.append(self.coord.handle(dict(schema=SCHEMA, op='capabilities', launcher='remote-run', ref=REF)))
    self.assertNotIn(SECRET, json.dumps(outputs))
    for path in self.root.rglob('*'):
        if path.is_file() and self.home not in path.parents:
            self.assertNotIn(SECRET.encode(), path.read_bytes(), path)
    with sqlite3.connect(str(self.root / 'control.sqlite3')) as db:
        dump = '\n'.join(db.iterdump())
    self.assertNotIn(SECRET, dump)
    self.assertNotIn('/remote-run/', dump)


def test_reservation_never_expires_by_clock(self):
    request, held = self.hold()
    self.now += 365 * 24 * 3600
    with patch.object(time, 'time', return_value=time.time() + 365 * 24 * 3600):
        self.assertEqual(actions.blocked_groups(self.engine), {'suites': 'reservation'})
        self.assertEqual(self.engine.handle(self.order_request('reorder-years-later'))['error']['code'],
                         'client_action')
        self.assertEqual(actions.reserved_task_ids(self.root), {'3'})


def test_prune_keeps_done_task_of_open_reservation(self):
    self.tasks['3']['status'] = {'Running': dict(enqueued_at=STAMP, start=LATER)}
    self.act(self.req('signal', self.identity(), mode='own', signal='KILL'))
    self.assertEqual(actions.reserved_task_ids(self.root), {'3'})
    old = '2026-01-01T00:00:00+00:00'
    self.tasks['3']['status'] = {'Done': dict(enqueued_at=STAMP, start=LATER, end=old, result='Killed')}
    self.tasks['5']['status'] = {'Done': dict(enqueued_at=STAMP, start=LATER, end=old, result='Killed')}
    removed = []
    self.p.call = lambda *args: removed.append(args) or 'Tasks removed from list: 5\n'
    control.prune_history(self.p, self.root, now=datetime(2026, 11, 17, tzinfo=timezone.utc).timestamp())
    self.assertEqual(removed, [('remove', '5')])


def test_gate_stays_off_and_reason_is_visible(self):
    self.assertFalse(control.MiniControl.coordinated_reorder)
    self.assertFalse(contract.COORDINATED_GATE)
    self.hold()
    view = self.engine.view()['reorder_groups']['suites']
    self.assertEqual((view['enabled'], view['reason']), (False, control.CLIENT_BLOCK_MESSAGE))
    self.engine.coordinated_reorder = False
    self.assertEqual(self.engine.view()['reorder_groups']['suites']['reason'], control.CLIENT_BLOCK_MESSAGE)
    self.assertEqual(self.engine.view()['reorder_groups']['default']['reason'],
                     'La coordinación de pausas de los clientes no está acreditada.')


def test_control_installed_alone_runs_and_orphan_journal_fails_closed(self):
    # Today's installers publish control.py alone; the module arrives with CC6.
    with patch.object(control, 'client_actions', return_value=None):
        self.assertEqual(control.client_blocks(self.engine), {})
        self.assertEqual(control.client_reserved_ids(self.root), set())
        self.assertNotEqual(self.engine.view()['reorder_groups']['suites']['reason'], control.CLIENT_BLOCK_MESSAGE)
    self.hold()
    with patch.object(control, 'client_actions', return_value=None):
        self.assertEqual(control.client_blocks(self.engine), {g: 'unknown' for g in self.engine.profile})
        with self.assertRaises(ValueError):
            control.client_reserved_ids(self.root)
        self.assertEqual(self.engine.handle(self.order_request('reorder-orphan-01'))['error']['code'], 'client_action')
    path = Path(control.__file__).with_name('client_actions.py')
    self.assertTrue(path.is_file())


def test_cli_bounded_read_and_exit_codes(self):
    lock = self.root / 'control.lock'
    self.assertTrue(lock.exists())
    over = io.BytesIO(b' ' * (contract.MAX_BYTES + 1) + b'{}')
    code, answer = control.client_cli(over, self.engine, self.root)
    self.assertEqual((code, answer), (2, contract.invalid_request_response()))
    reads = []

    class Spy(io.BytesIO):
        def read(self, n=-1):
            reads.append(n)
            return super().read(n)
    for raw in (b'{"schema":"pueue-client-control@1","op":"capabilities","op":"capabilities"}', b'\xff', b'[]'):
        self.assertEqual(control.client_cli(Spy(raw), self.engine, self.root)[0], 2)
    self.assertEqual(set(reads), {contract.MAX_BYTES + 1})
    code, answer = control.client_cli(io.BytesIO(b'{"schema":"pueue-client-control@1","op":"capabilities"}'),
                                      self.engine, self.root)
    self.assertEqual(code, 0)
    contract.negotiated(json.dumps(answer), code)
    body = json.dumps(self.req('hold', self.identity())).encode()
    code, answer = control.client_cli(io.BytesIO(body), self.engine, self.root, self.coord)
    self.assertEqual((code, answer['status']), (0, 'held'), answer)
    desk = control.MiniControl(self.p, self.root, host='desk', generation=lambda: 'x')
    code, answer = control.client_cli(io.BytesIO(body), desk, self.root)
    self.assertEqual((code, answer['status'], answer['reason']), (0, 'rejected', 'unsupported'))


def test_adapters_fail_closed_without_reading_payload(self):
    # Vigilante's manual classification needs task command and path: unsupported.
    self.assertEqual(self.coord.handle(dict(schema=SCHEMA, op='capabilities', launcher='vigilante',
                                            ref=REF))['reason'], 'unsupported')
    # Jale: manifest identity works, acceptance has no attempt link yet.
    job = self.home / '.local/state/jale/pueue' / JOB
    job.mkdir(parents=True)
    write_json(job / 'run-control.json', dict(contract='rack-cancel@1', protocol=2, ref=JOB, group='suites',
                                              task_id=4, cancel_origin=True))
    adapter = self.coord.adapters['jale']
    run_ = adapter.resolve(JOB, 'rack-queued')
    self.assertEqual((run_.task_id, run_.group), (4, 'suites'))
    with self.assertRaises(actions.Refused) as caught:
        adapter.acceptance(run_, STAMP)
    self.assertEqual(caught.exception.reason, 'unsupported')
    # apotheca: one-field id needs meta for its group: unsupported, meta untouched.
    folder = self.home / 'remote-run/runs' / APO
    folder.mkdir(parents=True)
    (folder / 'pueue.id').write_text('5\n')
    meta = folder / 'meta'
    meta.write_text('carril=normal\n')
    os.chmod(meta, 0)
    try:
        self.assertEqual(self.coord.handle(dict(schema=SCHEMA, op='capabilities', launcher='apotheca',
                                                ref=APO))['reason'], 'unsupported')
    finally:
        os.chmod(meta, 0o600)
    # A symlinked run directory never resolves.
    target = self.home / 'elsewhere'
    target.mkdir()
    (target / 'pueue.id').write_text('3\n')
    (self.home / 'remote-run/runs/20261116-120100-999999').symlink_to(target)
    self.assertEqual(self.coord.handle(dict(schema=SCHEMA, op='capabilities', launcher='remote-run',
                                            ref='20261116-120100-999999'))['reason'], 'identity')
    # Own-legacy: no manifest is fine for stop, never for the panel.
    legacy = self.make_run('20261116-120100-aaaaaa', 4, manifest=False)
    self.assertEqual(self.act(self.req('hold', self.identity('20261116-120100-aaaaaa'), mode='rack-queued',
                                       ref='20261116-120100-aaaaaa', action='action-0050'))['reason'], 'unsupported')
    self.assertEqual(self.act(self.req('hold', self.identity('20261116-120100-aaaaaa'), mode='own-legacy',
                                       ref='20261116-120100-aaaaaa', action='action-0051'))['status'], 'held')
    self.assertTrue(legacy.exists())


for _name, _value in list(globals().items()):
    if _name.startswith('test_'):
        setattr(ClientTests, _name, _value)


# ---------------------------------------------------------------- corpus CC0

class CorpusCoordinator(actions.Coordinator):
    def generation_digest(self, generation):
        return 'a' * 64

    def fingerprint(self, task):
        return 'b' * 64


class FixedAdapter(actions.Adapter):
    def __init__(self, request):
        self.request = request

    def resolve(self, ref, mode):
        return actions.Run(Path('/nonexistent'), 77, 'suites', None)

    def acceptance(self, run, attempt):
        return self.request['acceptance']

    def proof(self, run, attempt, acceptance):
        return self.request['proof']

    def interrupted(self, run):
        return True

    def manual(self, run):
        return True

    def attempt(self, run):
        return self.request['expected'].get('attempt')


def stock(identity):
    """Inverse projection: a stock-shaped task for a corpus identity."""
    s = identity['state']
    if s == 'Absent':
        return None
    status = {'Queued': lambda: {'Queued': dict(enqueued_at=identity['enqueued_at'])},
              'Stashed': lambda: {'Stashed': dict(enqueue_at=identity['enqueue_at'])},
              'Running': lambda: {'Running': dict(enqueued_at=identity['enqueued_at'], start=identity['started_at'])},
              'Paused': lambda: {'Paused': dict(enqueued_at=identity['enqueued_at'], start=identity['started_at'])},
              'Done': lambda: {'Done': dict(enqueued_at=identity['enqueued_at'], start=identity['started_at'],
                                            end=identity['started_at'], result='Killed')},
              'Locked': lambda: {'Locked': dict(previous_status='Queued')}}[s]()
    return dict(test_reorder.task(77), created_at=identity['created_at'], status=status)


class CorpusTests(unittest.TestCase):
    """Every CC0 request goes through the coordinator with doubles; answers stay contract-valid."""

    def world(self, request, response):
        tmp = tempfile.TemporaryDirectory(prefix='cc1-corpus-', dir='/tmp')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        p = test_slots.FakePueue()
        engine = control.MiniControl(p, root, clock=lambda: 1234.0, generation=lambda: 'daemon-1')
        with control.locked(root):
            engine.adopt()
        tasks = {}
        before = stock(request['expected'])
        if before is not None:
            tasks['77'] = before
        after = response.get('identity')
        status = response['status']
        p.status = lambda: dict(groups=copy.deepcopy(p.groups), tasks=copy.deepcopy(tasks))
        sent = []

        def call(*args):
            sent.append(args)
            if status == 'unknown':
                raise subprocess.TimeoutExpired(['fake-pueue'], 3)
            if status == 'rejected' and response['reason'] == 'stale_state':
                raise subprocess.CalledProcessError(1, ['fake-pueue'])
            if after is not None and after['state'] == 'Absent':
                tasks.pop('77', None)
            elif after is not None:
                tasks['77'] = stock(after)
            return ''
        p.call = call
        coord = CorpusCoordinator(engine, home=root / 'home', adapters={l: FixedAdapter(request) for l in contract.LAUNCHERS})
        reserved = request['op'] not in ('hold', 'capabilities') and not (request['op'].startswith('signal') and
                                                           request['phase'].endswith('TERM')) \
            and request['op'] != 'retire-interrupted'
        if reserved:
            with engine.connect(write=True) as db:
                actions.ensure_tables(db)
                db.execute('INSERT INTO client_reservations VALUES (?,?,?)', (
                    actions.action_key(request), 'suites', actions.canonical(dict(
                        launcher=request['launcher'], ref=request['ref'], action_id=request['action_id'],
                        group='suites', task_id=77, mode=request['mode'],
                        identity=list(actions.Coordinator.stable(request['expected'])), state='held',
                        stashed=True, at=0))))
        reason = response.get('reason')
        if status == 'deferred' and reason in ('manual', 'pressure', 'startup'):
            p.groups['suites']['status'] = 'Paused'
            set_reasons(engine, {'suites': [reason]})
        if status == 'deferred' and reason == 'host_action':
            set_reasons(engine, {'suites': ['reorder']})
        if status == 'rejected' and reason == 'stale_state' and request['op'] == 'hold':
            tasks['77'] = stock(after)
        return coord, sent

    def test_full_corpus_through_coordinator(self):
        matched, checked = 0, 0
        for case in CORPUS['valid']:
            request, expected = case['request'], case['response']
            with self.subTest(case=case['id']):
                original = contract.action_projection(request) if request['op'] == 'status' else request
                if request['op'] == 'capabilities':
                    coord, _ = self.world(dict(op='capabilities', expected=expected.get('identity') or
                                               dict(state='Absent')), dict(status='none'))
                    answer = coord.handle(request)
                    contract.validate_response(answer, request)
                    if 'launcher' not in request or expected['identity'] is not None:
                        self.assertEqual(answer, expected)
                    checked += 1
                    continue
                original_response = dict(expected, op=original['op'])
                original_response.pop('action_op', None)
                if original_response['status'] == 'pending':
                    original_response['status'] = 'deferred'
                coord, sent = self.world(original, original_response)
                locked = not (original_response['status'] == 'deferred' and original_response['reason'] == 'lock_busy')
                answer = coord.handle(original, locked=locked)
                contract.validate_response(answer, original)
                if request['op'] == 'status':
                    answer = coord.handle(request)
                    contract.validate_response(answer, request)
                checked += 1
                if case['id'].endswith('-control') or original['proof'] and original['proof']['kind'] == 'interrupted':
                    continue
                self.assertEqual((answer['status'], answer['reservation'], answer['reason']),
                                 (expected['status'], expected['reservation'], expected['reason']))
                matched += 1
                if expected['status'] in ('deferred', 'rejected') and expected['effects'] is False:
                    self.assertEqual(sent, [], case['id'])
        self.assertEqual(checked, len(CORPUS['valid']))
        self.assertGreaterEqual(matched, 50)
        for case in CORPUS['invalid']:
            if case['target'] != 'request':
                continue
            with self.subTest(case=case['id']):
                raw = json.dumps(case['value'], ensure_ascii=False).encode()
                code, answer = control.client_cli(io.BytesIO(raw), None, Path('/nonexistent'))
                self.assertEqual((code, answer), (2, contract.invalid_request_response()))


if __name__ == '__main__':
    unittest.main()
