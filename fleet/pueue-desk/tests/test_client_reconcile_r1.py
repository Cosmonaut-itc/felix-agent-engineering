"""R1 regressions: protective guard, waiting start, action-bound adapter evidence."""
import copy
import fcntl
import json
import multiprocessing
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import test_client_reconcile as t

control, a, cc1 = t.control, t.a, t.cc1


class R1Tests(t.ReconcileTests):
    pass


for name in dir(t.ReconcileTests):
    if name.startswith('test_'):
        setattr(R1Tests, name, None)


class Derived(t.Evidence):
    def recover(self, *args):
        out = super().recover(*args)
        out['effect_applied'] = out['observation']['identity']['state'] == 'Stashed'
        return out


class Mismatched(t.Evidence):
    field = 'action_id'

    def recover(self, r, state, reservation, process, daemon):
        out = super().recover(r, state, reservation, process, daemon)
        proof = dict((k, r[k]) for k in ('launcher', 'ref', 'action_id', 'phase'))
        proof.update(pid=process['pid'], birth=process['birth'])
        proof[self.field] = process['pid'] + 1 if self.field == 'pid' else 'other-evidence'
        out['effect_applied'] = proof
        return out


def assert_unattributed(self):
    self.run_reconcile()
    self.assertEqual(self.actions.record(self.r)['response']['status'], 'unknown')
    self.assertIsNone(self.actions.reservation(self.r)['hold_identity'])
    self.assertTrue(self.actions.reservation(self.r)['open'])
    for op in ('cancel-no-payload', 'retire-held'):
        r = dict(self.r, op=op, phase=op, expected=copy.deepcopy(self.after))
        if op == 'cancel-no-payload':
            r['acceptance'] = cc1.request('remote-run-started')['acceptance']
            self.evidence.acceptance = r['acceptance']
        response = self.act(r)
        self.assertEqual((response['status'], response['reason']), ('rejected', 'evidence_missing'))
    self.assertEqual(self.rpc_calls, [])


def test_daemon_derived_effect_never_attributes(self):
    self.evidence = Derived(self.r['expected'], self.engine.directory)
    self.engine.client_adapter = self.evidence
    self.actions = a.ClientActions(self.engine, self.evidence, rpc=lambda *args:self.rpc(*args))
    self.r['mode'] = 'own-legacy'
    self.orphan(process='gated')
    self.evidence.identity = copy.deepcopy(self.after)
    self.evidence.settled = True
    assert_unattributed(self)


def test_effect_key_and_process_must_match(self):
    for field in ('launcher', 'ref', 'action_id', 'phase', 'pid', 'birth'):
        with self.subTest(field=field):
            self.evidence = Mismatched(self.r['expected'], self.engine.directory)
            self.evidence.field = field
            self.engine.client_adapter = self.evidence
            self.actions = a.ClientActions(self.engine, self.evidence, rpc=lambda *args:self.rpc(*args))
            self.r['mode'] = 'own-legacy'
            self.orphan(process='gated')
            self.evidence.identity = copy.deepcopy(self.after)
            self.evidence.settled = True
            assert_unattributed(self)
            with self.engine.connect(write=True) as db:
                for table in ('client_actions', 'client_reservations', 'client_open'):
                    db.execute('DELETE FROM '+table)


def test_apply_checks_effect_binding_under_control_lock(self):
    self.orphan(process='gated')
    reconciler = a.Reconciler(self.engine, self.evidence, birth=lambda pid:None)
    item = reconciler.copies(None)[0]
    # Simulate an invalid/misrouted collector proposal; collection validation
    # cannot replace the authority process's check of its locked snapshot.
    proof = dict({k:self.r[k] for k in ('launcher','ref','action_id','phase')},
                 pid=item['record']['process']['pid'],birth='wrong-birth')
    proposal = dict(kind='evidence',observation=dict(group='suites',identity=self.after),
                    finish=None,effect_applied=proof)
    before = self.fingerprint()
    try:reconciler.apply(item,proposal,None)
    except a.Unavailable:pass
    self.assertEqual(self.fingerprint(),before,'apply must reject a mismatched collector proof under lock')
    self.assertIsNone(self.actions.reservation(self.r)['hold_identity'])


WAITER = """import fcntl,sys,time
with open(sys.argv[1], 'rb') as h:
 print('waiting', flush=True)
 fcntl.flock(h, fcntl.LOCK_EX)
 print('acquired', flush=True)
 time.sleep(float(sys.argv[2]))
"""


def test_closed_journal_guard_keeps_lock_with_queued_launcher(self):
    self.orphan(process=None)
    r = self.actions.reservation(self.r)
    r.update(open=False, uncertain=False)
    self.actions.save(self.actions.record(self.r), r)
    with patch.object(a.Reconciler, 'bounded_gather', side_effect=AssertionError('closed index spawned collection')), patch.object(self.engine, 'generation', side_effect=AssertionError('closed index read generation')):
        self.engine.reconcile_clients()
    for hold in (.15, .3):
        with self.subTest(hold=hold):
            calls = []
            waiter = None
            try:
                with patch.object(a.Reconciler, 'bounded_gather', side_effect=AssertionError('closed index spawned collection')), patch.object(self.engine, 'generation', side_effect=AssertionError('closed index read generation')), control.locked(self.engine.directory):
                    waiter = subprocess.Popen([sys.executable, '-c', WAITER,
                        str(self.engine.directory/'control.lock'), str(hold)],
                        stdout=subprocess.PIPE, text=True, close_fds=True)
                    self.assertEqual(waiter.stdout.readline().strip(), 'waiting')
                    time.sleep(.04)
                    def protect(*args, **kwargs):
                        with (self.engine.directory/'control.lock').open('rb') as h:
                            with self.assertRaises(BlockingIOError):
                                fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        calls.append('protective-guard')
                    with patch.object(self.engine, '_legacy_guard', protect):
                        try:self.engine.guard(dict(load=40, memory=95))
                        except RuntimeError as error:self.fail('protective guard was suppressed: '+str(error))
                    self.assertEqual(calls, ['protective-guard'])
                self.assertEqual(waiter.wait(timeout=3), 0)
                self.assertEqual(waiter.stdout.readline().strip(), 'acquired')
            finally:
                if waiter:
                    if waiter.poll() is None:waiter.terminate();waiter.wait()
                    waiter.stdout.close()


def test_productive_guard_collects_even_when_recovery_retains_admission(self):
    self.orphan(process=None)
    with patch.object(control,'MiniControl',return_value=self.engine), patch.object(control,'recover_mini',return_value=False):
        with control.locked(self.engine.directory):
            control.guard(self.p,self.root,self.engine.directory,host='mini')
            self.assertTrue(self.actions.reservation(self.r)['open'],'guard must keep its critical section intact')
    self.assertFalse(self.actions.reservation(self.r)['open'],'guard exit must execute independent collection')


R1Tests.test_productive_guard_collects_even_when_recovery_retains_admission = test_productive_guard_collects_even_when_recovery_retains_admission
R1Tests.test_apply_checks_effect_binding_under_control_lock = test_apply_checks_effect_binding_under_control_lock
R1Tests.test_daemon_derived_effect_never_attributes = test_daemon_derived_effect_never_attributes
R1Tests.test_effect_key_and_process_must_match = test_effect_key_and_process_must_match
R1Tests.test_closed_journal_guard_keeps_lock_with_queued_launcher = test_closed_journal_guard_keeps_lock_with_queued_launcher


class StartTests(unittest.TestCase):
    def setUp(self):
        self.fixture = t.test_handoff.HandoffTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.h = self.fixture.h
        self.h.m.change('install'); self.h.finish()
        self.h.p.online = False; self.h.owner = None

    def seed_unsent(self):
        h = self.h; r = cc1.request(); actions = a.ClientActions(h.engine)
        record = dict(request=r,digest=a.digest(r),revision=0,process=None,
            response=actions.reply(r,'unknown','rpc_unresolved',held=True))
        reservation = dict(launcher=r['launcher'],ref=r['ref'],action_id=r['action_id'],group='suites',
            mode=r['mode'],identity=r['expected'],acceptance=None,hold_identity=None,open=True,uncertain=True)
        with control.locked(h.directory):actions.save(record,reservation)
        return r, actions

    def test_launchd_wrapper_collects_before_preparing_and_exec(self):
        h = self.h; r, actions = self.seed_unsent(); events = []
        prepare = control.prepare_start
        def prepared(*args,**kwargs):
            self.assertFalse(actions.reservation(r)['open'],'wrapper must collect before preparation')
            events.append('prepare')
            return prepare(*args,**kwargs)
        with patch.object(control,'prepare_start',prepared):
            h.m.start(execute=lambda *args:events.append('exec'))
        self.assertEqual(events,['prepare','exec'])

    def test_preparation_cli_runs_independent_cycle(self):
        h = self.h; r, actions = self.seed_unsent()
        import io
        from contextlib import redirect_stdout
        with patch.object(sys,'argv',['control.py','preparar','--host','mini']), \
             patch.object(control.Path,'home',return_value=h.home), \
             patch.object(control,'Pueue',return_value=h.p), \
             patch.object(control,'MiniControl',return_value=h.engine), \
             patch.object(control,'validate_configuration',return_value=h.config), \
             patch.object(control,'assert_offline'), redirect_stdout(io.StringIO()):
            control.main()
        self.assertFalse(actions.reservation(r)['open'],'productive preparation CLI must run the cycle')

    def test_stalled_diagnostic_writer_cannot_delay_start_rejection(self):
        h = self.h; path = h.directory/'arranque.log'; before = path.read_bytes() if path.exists() else None
        lock = h.directory/'arranque.log.lock'; lock.touch()
        ctx = multiprocessing.get_context('fork'); parent, child = ctx.Pipe()
        holder = ctx.Process(target=t.collection_lock_holder,args=(str(lock),child,.7))
        holder.start()
        try:
            self.assertTrue(parent.poll(2));self.assertEqual(parent.recv(),'locked')
            started = time.monotonic()
            with patch.object(h.m,'_start',side_effect=ValueError('private error sentinel')):
                with self.assertRaisesRegex(ValueError,'private error sentinel'):h.m.start()
            self.assertLess(time.monotonic()-started,.45)
            self.assertEqual(path.read_bytes() if path.exists() else None,before,'a busy diagnostic is skipped')
        finally:
            holder.join(2)
            if holder.is_alive():holder.terminate();holder.join()
            self.assertEqual(holder.exitcode,0);parent.close();child.close()

    def test_start_waits_with_real_contention_and_executes_once_without_rejection(self):
        h = self.h
        r = cc1.request(); actions = a.ClientActions(h.engine)
        record = dict(request=r, digest=a.digest(r), revision=0, process=dict(state='injected'),
                      response=actions.reply(r,'unknown','rpc_unresolved',held=True))
        reservation = dict(launcher=r['launcher'],ref=r['ref'],action_id=r['action_id'],group='suites',
            mode=r['mode'],identity=r['expected'],acceptance=None,hold_identity=None,open=True,uncertain=True)
        with control.locked(h.directory):actions.save(record,reservation)
        log = h.directory/'arranque.log'
        before = log.read_bytes() if log.exists() else b''
        ctx = multiprocessing.get_context('fork'); parent, child = ctx.Pipe()
        holder = None; executed = []; gather = a.Reconciler.bounded_gather
        def collect(reconciler,copies,offline):
            nonlocal holder
            proposals = gather(reconciler,copies,offline)
            holder = ctx.Process(target=t.collection_lock_holder,args=(str(h.directory/'control.lock'),child,.7))
            holder.start();self.assertTrue(parent.poll(2));self.assertEqual(parent.recv(),'locked')
            return proposals
        try:
            start = time.monotonic()
            with patch.object(a.Reconciler,'bounded_gather',collect):
                h.m.start(execute=lambda *args:executed.append(args))
            elapsed = time.monotonic()-start
            print('whole-start-wait seconds=%.3f'%elapsed,flush=True)
            self.assertGreater(elapsed,.6,'start waits for the 0.7 second holder')
            self.assertLess(elapsed,1.4,'only the prior collection adds work to the original lock wait')
            self.assertEqual(len(executed),1)
            self.assertTrue(actions.reservation(r)['open'])
            self.assertEqual(log.read_bytes() if log.exists() else b'',before,'contention records no rejection')
        finally:
            if holder:
                holder.join(2)
                if holder.is_alive():holder.terminate();holder.join()
                self.assertEqual(holder.exitcode,0)
            parent.close();child.close()


if __name__ == '__main__':unittest.main()
