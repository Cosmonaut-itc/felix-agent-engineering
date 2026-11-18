"""Evidence arriving after a review is recovered through guard ticks alone."""
import copy
import fcntl
import json
import subprocess
import unittest
from unittest.mock import patch
import test_client_reconcile as t

control, a = t.control, t.a


class R2Tests(t.ReconcileTests):
    def tick(self):
        engine = control.MiniControl(self.p, self.engine.directory, clock=self.engine.clock,
                                     generation=self.engine.generation_source)
        engine.client_adapter = self.evidence
        with patch.object(engine, '_legacy_guard', return_value=None):
            with control.locked(engine.directory): engine.guard(dict(load=40, memory=95))

    def late_hold(self, slow=False):
        self.orphan(process='reaped')
        self.evidence.identity = copy.deepcopy(self.after)
        self.evidence.settled = slow
        self.evidence.effect_applied = slow
        self.evidence.slow = .6 if slow else 0
        gathers = []; original = a.Reconciler.bounded_gather
        def gather(rec, *args):
            gathers.append(True)
            return original(rec, *args)
        with patch.object(a.Reconciler, 'bounded_gather', gather):
            for _ in range(3): self.tick()
            self.assertEqual(self.actions.record(self.r)['response']['status'], 'unknown')
            self.evidence.settled = True; self.evidence.effect_applied = True; self.evidence.slow = 0
            self.tick(); self.tick()
            self.assertEqual(self.actions.record(self.r)['response']['status'], 'held',
                             'late evidence must be gathered and applied within two guard ticks')
            for _ in range(2): self.tick()
        self.assertEqual(len(gathers), 7, 'every reached open action needs a fresh collection each tick')
        self.assertTrue(self.actions.reservation(self.r)['open'])
        self.assertEqual(self.rpc_calls, [])

    def test_guard_recollects_late_hold_evidence(self):
        self.late_hold()

    def test_guard_retries_a_transiently_slow_read(self):
        self.late_hold(slow=True)

    def test_guard_collects_finish_after_an_unchanged_confirmed_phase(self):
        finish = t.finish_request(); self.evidence.identity = copy.deepcopy(finish['expected'])
        r = self.orphan(t.cc1.request('remote-run-started'), status='started', process='reaped')
        self.evidence.settled = True; self.evidence.effect_applied = True
        self.evidence.acceptance = finish['acceptance']; self.evidence.proof = finish['proof']
        for _ in range(3): self.tick()
        effect_receipt = self.actions.record(r)
        self.assertTrue(self.actions.reservation(r)['open']); self.assertIsNone(self.actions.record(finish))
        self.evidence.finish = finish
        self.tick(); self.tick()
        self.assertFalse(self.actions.reservation(r)['open'], 'finish must close without a rescue cycle or live client')
        self.assertEqual(self.actions.record(finish)['response']['status'], 'finished')
        self.assertEqual(self.actions.record(r)['response'], effect_receipt['response'])
        self.assertEqual(self.rpc_calls, [])

    def native_ticks(self, authenticate=True):
        self.orphan(process='reaped'); self.evidence.identity = copy.deepcopy(self.after)
        self.evidence.settled = True; self.evidence.effect_applied = True
        pidfile = self.root/'synthetic-daemon.pid'; pidfile.write_text('12345')
        waits = self.root/'process-waits.jsonl'
        def ps(argv, **kwargs):
            self.assertEqual(argv[0], '/bin/ps')
            with (self.engine.directory/'control.lock').open('rb') as handle:
                try: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB); held = False
                except BlockingIOError: held = True
            with waits.open('a') as stream: stream.write(json.dumps(dict(held=held, timeout=kwargs['timeout']))+'\n')
            return subprocess.CompletedProcess(argv, 0, stdout='Tue Nov 17 00:00:00 2026 pueued\n')
        self.engine.generation_source = None
        with patch.object(subprocess, 'run', side_effect=ps), \
             patch.object(control.priority_helper(), 'birth', side_effect=lambda p:'native-birth' if p == 12345 else None):
            with self.engine.connect(write=True) as db:
                state = self.engine.state(db); state['pid_file'] = str(pidfile)
                if authenticate: state['generation'] = control.mini_generation(pidfile)
                self.engine.store(db, state)
            waits.write_text('')
            self.tick(); self.tick(); self.tick()
        events = [json.loads(row) for row in waits.read_text().splitlines()]
        return events

    def test_guard_native_copy_and_apply_never_wait_for_a_process(self):
        events = self.native_ticks()
        self.assertTrue(events, 'generation still authenticates outside the protected turn')
        self.assertFalse(any(row['held'] for row in events), 'copy AND application must have zero process waits under control.lock')
        self.assertEqual(self.actions.record(self.r)['response']['status'], 'held')
        self.assertEqual(self.rpc_calls, [])

    def test_guard_native_copy_cannot_authenticate_a_replaced_daemon(self):
        # Native birth is coherent, but authority still names the old generation.
        self.native_ticks(authenticate=False)
        self.assertEqual(self.actions.record(self.r)['response']['status'], 'unknown')
        self.assertIsNone(self.actions.reservation(self.r)['hold_identity'])
        self.assertEqual(self.actions.reservation(self.r).get('reconcile_reason'), 'startup')
        self.assertEqual(self.rpc_calls, [])


for name in dir(t.ReconcileTests):
    if name.startswith('test_') and name not in R2Tests.__dict__: setattr(R2Tests, name, None)


if __name__ == '__main__': unittest.main()
