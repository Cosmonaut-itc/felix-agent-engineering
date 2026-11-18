"""CC2: autonomous reconciliation. Synthetic adapters, fake Pueue/clock, private /tmp only."""
import contextlib
import copy
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import time
import types
import unittest
from unittest.mock import patch
import test_client_actions as t
import test_reorder
import test_trickle
import test_handoff

control, a, c = t.control, t.a, t.c
mini = test_handoff.mini
STASHED = copy.deepcopy(t.CASES['remote-run-rack-queued-held']['response']['identity'])


def lock_free(directory):
    """A fresh open file description: blocked iff someone (even us) holds control.lock."""
    with (Path(directory) / 'control.lock').open('rb') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(handle, fcntl.LOCK_UN)
        return True


class Launcher(t.Evidence):
    """Fixed synthetic adapter; records whether control.lock was held on each call."""
    def __init__(self, identity, directory, published=None):
        super().__init__(identity)
        self.directory, self.published, self.held_calls, self.calls = directory, published, 0, 0
        self.tick = None

    def check(self):
        self.calls += 1
        if self.tick:
            self.tick()
        if not lock_free(self.directory):
            self.held_calls += 1

    def observe(self, r, state, reservation):
        self.check()
        return super().observe(r, state, reservation)

    def verify(self, r, observation, reservation):
        self.check()
        if r['op'] == 'finish':
            if self.published is None or r['proof'] != self.published['proof']:
                raise a.Unavailable('evidence_missing')
            return
        return super().verify(r, observation, reservation)

    def evidence(self, reservation):
        self.check()
        if self.published is None:
            raise a.Unavailable('unsupported')
        return copy.deepcopy(self.published)


def slow_reconciler(directory, pipe):
    """Real process: the collect step waits for the parent while a client runs."""
    engine = control.MiniControl(test_reorder.test_slots.FakePueue(), Path(directory), generation=lambda: 'daemon-1')
    class Slow(Launcher):
        def observe(self, r, state, reservation):
            pipe.send(('collecting', lock_free(directory)))
            pipe.recv()
            return super().observe(r, state, reservation)
    adapter = Slow(STASHED, directory)
    pipe.send(('report', a.Reconciler(engine, adapter).run()))
    pipe.close()


def hold_result(directory, pipe):
    with (Path(directory) / 'result.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        pipe.send('result-locked')
        pipe.recv()


class ReconcileTests(test_reorder.ReorderTests):
    pass


for _name in dir(test_reorder.ReorderTests):
    if _name.startswith('test_'):
        setattr(ReconcileTests, _name, None)


def setup(self):
    t.setup(self)
    self.launcher = Launcher(self.r['expected'], self.root)
    self.alive = {}
    self.addCleanup(lambda: setattr(a.Reconciler, 'boundary', staticmethod(lambda phase: None)))


def reconciler(self, **kwargs):
    kwargs.setdefault('birth', lambda pid: self.alive.get(pid))
    return a.Reconciler(self.engine, kwargs.pop('adapter', self.launcher), **kwargs)


def crash_at(self, phase, r=None, process=None):
    """The owner (client CLI) dies at a CC1 frontier with control.lock released."""
    r = r or self.r
    def boundary(p):
        if p == phase:
            raise test_reorder.Crash()
    self.actions.boundary = boundary
    with self.assertRaises(test_reorder.Crash):
        self.act(r)
    self.actions.boundary = lambda p: None
    record = self.actions.record(r)
    if process is not None:
        record['process'] = dict(process)
        self.actions.save(record)
    return record


def reservation(self, r=None):
    r = r or self.r
    return next(v for v in a.reservations(self.engine) if v['action_id'] == r['action_id'])


def restart_daemon(self, generation='daemon-2'):
    """Supervised restart: a new daemon generation reaches the authority."""
    self.gen = generation
    self.change_state(generation=generation)


for _f in (setup, reconciler, crash_at, reservation, restart_daemon):
    setattr(ReconcileTests, 'setUp' if _f is setup else _f.__name__, _f)
ReconcileTests.rpc, ReconcileTests.act, ReconcileTests.seed = t.ActionTests.rpc, t.ActionTests.act, t.ActionTests.seed


def test_unsent_stash_closes_from_journal_and_deferred_retry_is_one_rpc(self):
    for phase in ('reserved', 'before_rpc'):
        with self.subTest(phase=phase):
            r = dict(copy.deepcopy(self.r), action_id='unsent-' + phase.replace('_', '-'))
            self.evidence.identity = copy.deepcopy(r['expected'])
            record = self.crash_at(phase, r)
            self.assertIsNone(record['process'])
            self.assertTrue(self.reservation(r)['uncertain'])
            report = self.reconciler().run()
            self.assertEqual((report['cerradas'], report['retenidas']), (1, 0), report)
            self.assertFalse(self.reservation(r)['open'])
            self.assertFalse(self.engine.client_blocked('suites'))
            receipt = self.actions.record(r)
            self.assertEqual(receipt['response']['status'], 'deferred')
            self.assertEqual(self.rpc_calls, [])
            # Deferred: the same body is evaluated again, exactly one RPC, then fixed.
            seen = []
            self.actions.boundary = lambda p: seen.append(self.actions.record(r)['revision']) if p == 'reserved' else None
            held = self.act(r)
            self.actions.boundary = lambda p: None
            self.assertEqual(held['status'], 'held', held)
            self.assertGreater(seen[0], receipt['revision'], 'revision went back on re-evaluation (ABA)')
            self.assertEqual(len(self.rpc_calls), 1)
            self.assertEqual(self.act(r), held)
            self.assertEqual(self.act(dict(r, op='status', action_op='hold'))['status'], 'held')
            self.assertEqual(len(self.rpc_calls), 1, 'an effectful receipt is never re-executed')
            with self.engine.connect(write=True) as db: db.execute('DELETE FROM client_reservations')
            self.rpc_calls.clear()


def test_deferred_status_query_does_not_reevaluate_and_effects_stay_fixed(self):
    with self.engine.connect(write=True) as db: db.execute('DROP TABLE IF EXISTS client_reservations')
    self.engine.fence_pause('suites')
    other = dict(copy.deepcopy(self.r), action_id='holder-of-group')
    self.seed(other)  # another action holds the group: deferred host_action
    first = self.act()
    self.assertEqual(first['status'], 'deferred')
    query = dict(self.r, op='status', action_op='hold')
    self.assertEqual(self.act(query)['status'], 'deferred')
    self.assertEqual(self.rpc_calls, [], 'a status query never re-evaluates')
    with self.engine.connect(write=True) as db: db.execute('DELETE FROM client_reservations')
    self.assertEqual(self.act()['status'], 'held')
    # unknown is not re-evaluated either: no second message for one action.
    lost = dict(copy.deepcopy(self.r), action_id='lost-unknown-0001')
    self.evidence.identity = copy.deepcopy(lost['expected'])
    with self.engine.connect(write=True) as db: db.execute('DELETE FROM client_reservations')
    self.actions.rpc = lambda *args: (self.rpc_calls.append(args), (_ for _ in ()).throw(OSError('lost')))
    self.assertEqual(self.act(lost)['status'], 'unknown')
    calls = len(self.rpc_calls)
    self.assertEqual(self.act(lost)['status'], 'unknown')
    self.assertEqual(len(self.rpc_calls), calls)


def test_child_frontiers_never_close_by_exit_clock_or_equal_reads(self):
    for state in ('gated', 'sent', 'reaped'):
        with self.subTest(state=state):
            r = dict(copy.deepcopy(self.r), action_id='child-' + state)
            self.evidence.identity = copy.deepcopy(r['expected'])
            self.launcher.identity = copy.deepcopy(r['expected'])
            self.crash_at('before_rpc', r, process=dict(pid=4242, birth='b-1', state=state))
            self.alive[4242] = 'b-1'
            for _ in range(2):  # two identical observations prove nothing
                self.assertEqual(self.reconciler().run()['retenidas'], 1)
            self.alive.pop(4242)  # the child exits
            for _ in range(2):
                self.now += 10**9  # no clock closes a reservation
                self.assertEqual(self.reconciler().run()['retenidas'], 1)
            row = self.reservation(r)
            self.assertTrue(row['open'] and row['uncertain'])
            self.assertEqual(row['retained'], 'rpc_unresolved')
            self.assertEqual(self.actions.record(r)['response']['status'], 'unknown')
            self.assertEqual(self.actions.record(r)['process']['dead_generation'], 'daemon-1')
            self.assertTrue(self.engine.client_blocked('suites', opening=True))
            self.assertEqual(self.launcher.calls, 0, 'no observation before a late message is excluded')
            with self.engine.connect(write=True) as db: db.execute('DELETE FROM client_reservations')
    self.assertEqual(self.rpc_calls, [])


def test_exclusion_after_supervised_restart_then_observed_effect(self):
    self.crash_at('before_rpc', process=dict(pid=4242, birth='b-1', state='sent'))
    self.reconciler().run()  # child dead under daemon-1: durable fact
    self.restart_daemon()
    # The synthetic adapter maps the identity to the new generation.
    self.launcher.identity = dict(STASHED, generation=self.r['expected']['generation'])
    report = self.reconciler().run()
    self.assertEqual(report['avanzadas'], 1, report)
    row = self.reservation()
    self.assertTrue(row['open']); self.assertFalse(row['uncertain'])
    self.assertEqual(row['hold_identity'], self.launcher.identity)
    self.assertEqual(self.actions.record(self.r)['response']['status'], 'held')
    self.assertEqual(self.actions.record(self.r)['process']['state'], 'excluded')
    self.assertEqual(self.rpc_calls, [])
    self.assertEqual(self.launcher.held_calls, 0)


def test_exclusion_without_effect_proof_keeps_unknown_and_closed_groups(self):
    self.crash_at('before_rpc', process=dict(pid=4242, birth='b-1', state='gated'))
    self.reconciler().run()
    self.restart_daemon()
    self.launcher.identity = dict(self.r['expected'], fingerprint='e' * 64)  # not our attempt
    self.change_state(reasons={'suites': ['pressure']}); self.p.groups['suites']['status'] = 'Paused'
    for _ in range(2):
        self.assertEqual(self.reconciler().run()['retenidas'], 1)
        self.engine.guard(dict(load=0, memory=1))
    row = self.reservation()
    self.assertTrue(row['open'] and row['uncertain'])
    self.assertEqual(row['retained'], 'evidence_missing')
    self.assertEqual(self.actions.record(self.r)['response']['status'], 'unknown')
    self.assertEqual(self.p.groups['suites']['status'], 'Paused')
    self.assertFalse(any(call[0] == 'start' for call in self.p.calls))


def test_known_reservation_closes_only_with_published_result(self):
    self.assertEqual(self.act()['status'], 'held')
    self.launcher.identity = copy.deepcopy(STASHED)
    for reason, published in (('unsupported', None), ('evidence_missing', dict(acceptance=None, proof=None))):
        self.launcher.published = published
        self.assertEqual(self.reconciler().run()['retenidas'], 1)
        self.assertEqual(self.reservation()['retained'], reason)  # acceptance pending
        self.assertTrue(self.reservation()['open'])
    finish = next(copy.deepcopy(v['request']) for v in t.CORPUS['valid'] if
                  v['request']['op'] == 'finish' and v['request']['mode'] == 'rack-queued')
    self.launcher.identity = dict(finish['expected'], **{k: STASHED[k] for k in
                                  ('generation', 'task_id', 'created_at', 'fingerprint', 'attempt')})
    self.launcher.published = dict(acceptance=finish['acceptance'], proof=finish['proof'])
    report = self.reconciler().run()
    self.assertEqual(report['cerradas'], 1, report)
    self.assertFalse(self.reservation()['open'])
    done = dict(self.r, phase='finish', op='finish')
    self.assertEqual(self.actions.record(done)['response']['status'], 'finished')
    self.assertFalse(self.engine.client_blocked('suites'))
    self.assertEqual(self.launcher.held_calls, 0)
    # Terminal: a later pass never reopens, rewrites or re-finishes it.
    before = self.reservation()
    self.assertEqual(self.reconciler().run()['abiertas'], 0)
    self.assertEqual(self.reservation(), before)
    self.assertEqual(len(self.rpc_calls), 1)


def test_rejected_hold_after_reservation_is_released(self):
    self.evidence.change_at[2] = dict(self.r['expected'], state='Locked', enqueued_at=None)
    self.assertEqual(self.act()['reason'], 'locked')
    self.assertTrue(self.reservation()['open'])
    self.assertEqual(self.reconciler(adapter=a.InstalledAdapters()).run()['cerradas'], 1)
    self.assertFalse(self.reservation()['open'])
    self.assertEqual(self.rpc_calls, [])


def test_installed_adapters_retain_with_reason_and_no_rpc(self):
    self.assertEqual(self.act()['status'], 'held')
    calls = len(self.p.calls)
    report = control.reconcile_clients(self.p, self.root, engine=self.engine)
    self.assertEqual(report['retenidas'], 1, report)
    self.assertEqual(self.reservation()['retained'], 'unsupported')
    view = self.engine.view()
    self.assertEqual(view['reorder_groups']['suites']['reason'], control.CLIENT_RETAINED['unsupported'])
    self.assertEqual(view['capabilities']['reason_reorder'], control.CLIENT_RETAINED['unsupported'])
    self.assertEqual(len(self.p.calls), calls)
    self.assertEqual((self.engine.handle(self.order_request()).get('error') or {}).get('code'), 'client_reserved')
    # A second tick writes nothing new.
    revision = self.reservation()['revision']
    control.reconcile_clients(self.p, self.root, engine=self.engine)
    self.assertEqual(self.reservation()['revision'], revision)


def test_rack_capabilities_accept_retained_reasons(self):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'rack'))
    try:
        import host_action
    finally:
        sys.path.pop(0)
    self.assertEqual(self.act()['status'], 'held')
    for reason, text in (('unsupported', control.CLIENT_RETAINED['unsupported']),
                         ('rpc_unresolved', control.CLIENT_RETAINED['rpc_unresolved']),
                         ('evidence_missing', control.CLIENT_RETAINED[None]),
                         (None, control.MiniControl.client_reservation_reason)):
        with self.engine.connect(write=True) as db:
            row = self.reservation(); row['retained'] = reason
            db.execute('UPDATE client_reservations SET body=?', (a.canonical(row),))
        value = self.engine.view()
        self.assertEqual(value['capabilities']['reason_reorder'], text)
        self.assertEqual(value['reorder_groups']['suites']['reason'], text)
        self.assertEqual(host_action.control(value, 'mini')['capabilities']['reason_reorder'], text)


def test_three_versions_compared_after_collection(self):
    def bump_revision():
        with self.engine.connect(write=True) as db:
            row = self.reservation(); row['revision'] += 1
            db.execute('UPDATE client_reservations SET body=?', (a.canonical(row),))
    def swap_digest():
        with self.engine.connect(write=True) as db:
            row = self.reservation(); row['digest'] = 'f' * 64  # same revision, other body
            db.execute('UPDATE client_reservations SET body=?', (a.canonical(row),))
    def new_generation():
        self.restart_daemon()
    for name, change in (('revision', bump_revision), ('digest', swap_digest), ('generation', new_generation)):
        for boundary in ('copied', 'collected'):
            with self.subTest(version=name, boundary=boundary):
                r = dict(copy.deepcopy(self.r), action_id='race-' + name + '-' + boundary)
                self.evidence.identity = copy.deepcopy(r['expected'])
                with self.engine.connect(write=True) as db: db.execute('DROP TABLE IF EXISTS client_reservations')
                self.r = r
                self.crash_at('reserved', r)
                a.Reconciler.boundary = staticmethod(lambda p, b=boundary, f=change: f() if p == b else None)
                report = self.reconciler().run()
                a.Reconciler.boundary = staticmethod(lambda p: None)
                self.assertEqual((report['cambiadas'], report['cerradas']), (1, 0), report)
                row = self.reservation(r)
                self.assertTrue(row['open'] and row['uncertain'])
                self.assertIsNone(row.get('retained'))
                self.assertEqual(self.actions.record(r)['response']['status'], 'unknown')
                self.restart_daemon('daemon-1')


def test_collection_never_holds_control_lock(self):
    self.assertEqual(self.act()['status'], 'held')
    self.launcher.identity = copy.deepcopy(STASHED)
    self.launcher.published = dict(acceptance=None, proof=None)
    self.reconciler().run()
    self.assertGreater(self.launcher.calls, 0)
    self.assertEqual(self.launcher.held_calls, 0)


def test_real_processes_reconciler_client_and_launcher_locks(self):
    self.assertEqual(self.act()['status'], 'held')
    ctx = multiprocessing.get_context('spawn')
    # 1) A launcher holds result.lock: the reconciler never waits for it.
    p1, c1 = ctx.Pipe()
    launcher = ctx.Process(target=hold_result, args=(str(self.root), c1)); launcher.start()
    try:
        self.assertTrue(p1.poll(8)); self.assertEqual(p1.recv(), 'result-locked')
        started = time.monotonic()
        report = self.reconciler(adapter=a.InstalledAdapters()).run()
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(report['ocupado'])
    finally:
        p1.send('release'); launcher.join(8)
    # 2) A client holds control.lock: bounded wait, nothing decided.
    p2, c2 = ctx.Pipe()
    holder = ctx.Process(target=t.hold_lock, args=(str(self.root), c2)); holder.start()
    try:
        self.assertTrue(p2.poll(8)); self.assertEqual(p2.recv(), 'locked')
        started = time.monotonic()
        report = self.reconciler().run()
        self.assertTrue(report['ocupado'])
        self.assertLess(time.monotonic() - started, a.LOCK_WAIT + 2)
    finally:
        p2.send('release'); holder.join(8)
    # 3) Reconciler collecting in its own process; a launcher+client runs meanwhile.
    p3, c3 = ctx.Pipe()
    rec = ctx.Process(target=slow_reconciler, args=(str(self.root), c3)); rec.start()
    receiver, sender = ctx.Pipe()
    other = dict(copy.deepcopy(self.r), action_id='concurrent-client')
    client = None
    try:
        self.assertTrue(p3.poll(8)); kind, free = p3.recv()
        self.assertEqual(kind, 'collecting'); self.assertTrue(free, 'collect ran under control.lock')
        client = ctx.Process(target=t.worker, args=(str(self.root), other, sender)); client.start()
        self.assertTrue(receiver.poll(8), 'client blocked by a collecting reconciler')
        result = receiver.recv()
        self.assertEqual((result['status'], result['reason']), ('deferred', 'host_action'))
        p3.send('go')
        self.assertTrue(p3.poll(8)); kind, report = p3.recv()
        self.assertEqual(kind, 'report'); self.assertFalse(report['ocupado'])
    finally:
        for process in (rec, client):
            if process is not None:
                process.join(8)
                if process.is_alive(): process.terminate(); process.join()
        for conn in (p1, c1, p2, c2, p3, c3, receiver, sender): conn.close()
    self.assertEqual((launcher.exitcode, holder.exitcode, rec.exitcode, client.exitcode), (0, 0, 0, 0))


def test_tick_budget_and_limit_with_many_open_actions(self):
    with self.engine.connect(write=True) as db: db.execute('DROP TABLE IF EXISTS client_reservations')
    for n in range(40):
        r = dict(copy.deepcopy(self.r), action_id='many-%04d' % n)
        self.seed(r)
        self.actions.save(dict(request=r, digest=a.digest(r), revision=0, process=None,
                               response=self.actions.reply(r, 'held', identity=STASHED, held=True)), self.reservation(r))
    clock = [0.0]
    self.launcher.tick = lambda: clock.__setitem__(0, clock[0] + 1.0)
    report = self.reconciler(budget=3.0, clock=lambda: clock[0]).run()
    self.assertEqual(report['abiertas'], a.Reconciler.LIMIT)
    self.assertLessEqual(clock[0], 3.0 + 3)  # one adapter round may straddle the deadline
    self.assertGreater(report['pendientes'], 0)
    self.assertEqual(report['retenidas'] + report['pendientes'], a.Reconciler.LIMIT)
    # Rotation: a later minute starts elsewhere, so no action starves.
    first = {v['action_id'] for v in a.reservations(self.engine) if v.get('retained')}
    self.now += 60 * 7
    clock[0] = 0.0
    self.reconciler(budget=3.0, clock=lambda: clock[0]).run()
    later = {v['action_id'] for v in a.reservations(self.engine) if v.get('retained')}
    self.assertGreater(len(later), len(first))


def test_fences_manual_pressure_startup_keep_retaining(self):
    self.crash_at('before_rpc', process=dict(pid=4242, birth='b-1', state='sent'))
    self.change_state(startup_version=1, startup=dict(phase='ready', statuses={}, groups={}, holds={},
                      interruptions={}, source_hash='a'*64, target_hash='b'*64))
    for reason in ('manual', 'pressure', 'startup', 'fence'):
        with self.subTest(reason=reason):
            if reason == 'fence':
                self.change_state(reasons={'suites': []}); self.engine.fence_pause('suites')
            else:
                self.change_state(reasons={'suites': [reason]})
            self.p.groups['suites']['status'] = 'Paused'
            self.reconciler().run()
            self.engine.guard(dict(load=0, memory=1))
            self.assertEqual(self.p.groups['suites']['status'], 'Paused')
            with self.assertRaises(ValueError):
                self.engine.manual('suites', resume=True)
            self.assertTrue(self.reservation()['uncertain'])
    self.assertFalse(any(call[0] == 'start' for call in self.p.calls))


def test_guard_main_reconciles_after_releasing_the_lock(self):
    events = []
    @contextlib.contextmanager
    def locked(directory, create=False):
        events.append('lock'); yield; events.append('unlock')
    def reconcile(pueue, directory, engine=None):
        events.append('reconcile')
    with patch.object(control, 'locked', locked), patch.object(control, 'guard', lambda *a, **k: events.append('guard')), \
            patch.object(control, 'reconcile_clients', reconcile), patch.object(control, 'prune_history', lambda *a: events.append('prune')), \
            patch.object(control, 'Pueue', lambda *a, **k: self.p), patch.object(control.Path, 'home', return_value=self.root), \
            patch('sys.argv', ['control.py', 'guardia', '--host', 'mini']):
        control.main()
    self.assertEqual(events, ['lock', 'guard', 'unlock', 'reconcile', 'prune'])


for _name, _value in list(globals().items()):
    if _name.startswith('test_') and callable(_value):
        setattr(ReconcileTests, _name, _value)


class DeskReservationTests(test_trickle.TrickleTests):
    pass


for _name in dir(test_trickle.TrickleTests):
    if _name.startswith('test_'):
        setattr(DeskReservationTests, _name, None)


def desk_seed(self, group='jale', uncertain=True):
    r = t.request()
    actions = a.ClientActions(self.engine, t.Evidence(r['expected']))
    value = dict(launcher=r['launcher'], ref=r['ref'], action_id=r['action_id'], group=group, mode=r['mode'],
                 identity=r['expected'], acceptance=None, open=True, uncertain=uncertain, hold_identity=None)
    actions.save(dict(request=r, digest=a.digest(r), revision=0, process=None,
                      response=actions.reply(r, 'unknown', 'rpc_unresolved', held=True)), value)


def test_desk_one_and_guard_respect_client_reservation(self):
    self.desk_seed()
    self.run_guard()
    self.assertEqual(self.starts(), [], 'desk _one started by ID over an unresolved client RPC')
    self.assertIn('reason', self.causes())
    self.sample = self.low; self.run_guard(self.low)
    self.assertEqual(self.p.groups['jale']['status'], 'Paused', 'desk guard reopened over a client RPC')
    self.assertEqual(self.engine.view()['capabilities']['client'], False)
    with self.engine.connect(write=True) as db: db.execute('DELETE FROM client_reservations')
    self.ns += 120_000_000_000; self.now += 120
    self.sample = self.mid; self.run_guard()
    self.assertEqual(self.starts(), [('start', 1)])


DeskReservationTests.desk_seed = desk_seed
DeskReservationTests.test_desk_one_and_guard_respect_client_reservation = test_desk_one_and_guard_respect_client_reservation


class PreparerTests(unittest.TestCase):
    """U3.5 wrapper start: the preparer keeps client reservations and Stashed."""
    def setUp(self):
        self.ps = patch.object(control.subprocess, 'run', side_effect=test_handoff.HandoffTests.no_real_process)
        self.ps.start(); self.addCleanup(self.ps.stop)
        self.h = test_handoff.Harness(self)
        self.helper = patch.object(control, 'priority_helper', return_value=types.SimpleNamespace(birth=lambda pid: None))
        self.helper.start(); self.addCleanup(self.helper.stop)

    def seed(self, action_id, process, task_id):
        identity = dict(t.request()['expected'], task_id=task_id)
        r = dict(t.request(), action_id=action_id, expected=identity)
        actions = a.ClientActions(self.h.engine, t.Evidence(identity))
        value = dict(launcher=r['launcher'], ref=r['ref'], action_id=action_id, group='suites', mode=r['mode'],
                     identity=identity, acceptance=None, open=True, uncertain=True, hold_identity=None)
        actions.save(dict(request=r, digest=a.digest(r), revision=0, process=process,
                          response=actions.reply(r, 'unknown', 'rpc_unresolved', held=True)), value)
        return r

    def test_preparer_and_restart_keep_reservations_unknown_and_stashed(self):
        h = self.h
        h.m.change('install'); h.finish()
        stashed = test_handoff.task(5, {'Stashed': dict(enqueue_at=None)}, 'suites')
        h.p.tasks['5'] = stashed; h.save_queue()
        unsent = self.seed('preparer-unsent', None, 6)
        gated = self.seed('preparer-gated', dict(pid=4242, birth='b-1', state='gated'), 5)
        h.login(); h.recover()
        self.assertTrue(h.p.online)
        rows = {v['action_id']: v for v in a.reservations(h.engine)}
        self.assertEqual(set(rows), {'preparer-unsent', 'preparer-gated'}, 'preparer dropped a reservation')
        self.assertFalse(rows['preparer-unsent']['open'])  # journal proves it never left
        self.assertTrue(rows['preparer-gated']['open'] and rows['preparer-gated']['uncertain'])
        self.assertEqual(a.ClientActions(h.engine).record(gated)['response']['status'], 'unknown')
        self.assertEqual(a.ClientActions(h.engine).record(unsent)['response']['status'], 'deferred')
        self.assertEqual(h.p.tasks['5']['status'], {'Stashed': dict(enqueue_at=None)})
        self.assertTrue(h.engine.client_blocked('suites', opening=True))


if __name__ == '__main__':
    unittest.main()
