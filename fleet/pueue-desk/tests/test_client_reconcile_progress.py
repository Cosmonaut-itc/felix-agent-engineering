"""CC2 review obligations survive independent lock collisions and CLI lifetimes."""
import copy
import io
import json
import multiprocessing
from pathlib import Path
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
import test_client_reconcile as t
import test_client_reconcile_r1 as r1

control, a, cc1 = t.control, t.a, t.cc1


class Holders:
    def __init__(self, test):
        self.test = test
        self.children = []
        test.addCleanup(self.close)

    def start(self, path, seconds=.15):
        ctx = multiprocessing.get_context('fork'); parent, child = ctx.Pipe()
        worker = ctx.Process(target=t.collection_lock_holder, args=(str(path), child, seconds))
        worker.start(); child.close(); self.children.append((worker, parent))
        self.test.assertTrue(parent.poll(2)); self.test.assertEqual(parent.recv(), 'locked')
        return worker

    def close(self):
        for worker, pipe in self.children:
            worker.join(3)
            if worker.is_alive(): worker.terminate(); worker.join()
            pipe.close()
            self.test.assertEqual(worker.exitcode, 0)
        self.children = []


class ProgressTests(t.ReconcileTests):
    def collisions(self):
        holders = Holders(self); original = a.Reconciler.lock
        def collide(rec):
            holders.start(self.engine.directory/'control.lock')
            return original(rec)
        return holders, collide

    def tick(self, calls):
        # A new engine per tick models separate productive CLI invocations.
        engine = control.MiniControl(self.p, self.engine.directory, clock=self.engine.clock,
                                     generation=self.engine.generation_source)
        engine.client_adapter = self.evidence
        with patch.object(engine, '_legacy_guard', side_effect=lambda *x, **k:calls.append('protect')):
            with control.locked(engine.directory): engine.guard(dict(load=40, memory=95))

    def test_repeated_contention_reviews_both_journal_closures(self):
        for kind in ('never-sent', 'rejected'):
            with self.subTest(kind=kind):
                r = copy.deepcopy(self.r); r['action_id'] = 'journal-'+kind+'-0001'
                self.orphan(r, process=None)
                if kind == 'rejected':
                    record, reserve = self.actions.record(r), self.actions.reservation(r)
                    record['response'] = self.actions.reply(r, 'rejected', 'locked', held=True)
                    reserve['uncertain'] = False; self.actions.save(record, reserve)
                holders, collide = self.collisions(); calls = []
                with patch.object(a.Reconciler, 'lock', collide):
                    self.tick(calls); holders.close()
                    self.tick(calls); holders.close()
                self.assertEqual(calls, ['protect', 'protect'])
                self.assertFalse(self.actions.reservation(r)['open'], 'journal-only closure by tick N+1')
                self.assertIsNone(self.actions.reservation(r)['hold_identity'])
        self.assertEqual(self.rpc_calls, [])

    def test_evidence_survives_processes_and_cursor_until_next_protected_turn(self):
        # More than one batch: rotating at copy time would lose the N+1 duty.
        requests = []
        for n in range(a.Reconciler.limit + 2):
            r = copy.deepcopy(self.r); r['action_id'] = 'progress-%08d' % n
            requests.append(r); self.orphan(r)
        self.evidence.identity = copy.deepcopy(self.after)
        self.evidence.settled = True; self.evidence.effect_applied = True
        holders, collide = self.collisions(); calls = []
        with patch.object(a.Reconciler, 'lock', collide):
            self.tick(calls); holders.close()
            self.assertEqual([self.actions.record(r)['response']['status'] for r in requests], ['unknown']*len(requests))
            # Re-create Reconciler/engine; no in-memory evidence can satisfy this.
            self.tick(calls); holders.close()
            self.assertEqual([self.actions.record(r)['response']['status'] for r in requests[:8]], ['held']*8,
                             'every action reached in N is reviewed in N+1')
            self.tick(calls); holders.close()
            self.tick(calls); holders.close()
        self.assertEqual(calls, ['protect']*4)
        self.assertEqual([self.actions.record(r)['response']['status'] for r in requests], ['held']*len(requests))
        self.assertEqual(self.rpc_calls, [])

    def test_evidence_retains_with_reason_by_next_tick(self):
        self.orphan(); self.engine.client_adapter = None
        # Installed adapter is unsupported; no generation/daemon evidence invented.
        self.evidence = a.InstalledAdapters()
        holders, collide = self.collisions(); calls = []
        with patch.object(a.Reconciler, 'lock', collide):
            self.tick(calls); holders.close(); self.tick(calls); holders.close()
        record, reserve = self.actions.record(self.r), self.actions.reservation(self.r)
        self.assertEqual(record['response']['status'], 'unknown')
        self.assertEqual(reserve.get('reconcile_reason'), 'unsupported')
        self.assertTrue(reserve['open']); self.assertEqual(calls, ['protect']*2)

    def test_unavailable_generation_retains_with_startup_reason_next_tick(self):
        self.orphan(); self.evidence.identity = copy.deepcopy(self.after)
        self.evidence.settled = True; self.evidence.effect_applied = True
        def gone(): raise OSError('synthetic absent daemon')
        self.engine.generation_source = gone
        holders, collide = self.collisions(); calls = []
        with patch.object(a.Reconciler, 'lock', collide):
            self.tick(calls); holders.close(); self.tick(calls); holders.close()
        self.assertEqual(self.actions.record(self.r)['response']['status'], 'unknown')
        self.assertEqual(self.actions.reservation(self.r).get('reconcile_reason'), 'startup')
        self.assertIsNone(self.actions.reservation(self.r)['hold_identity'])

    def test_cached_evidence_discarded_for_every_binding_change(self):
        for field in ('generation', 'authority', 'revision', 'digest', 'reservation_revision',
                      'reservation_digest', 'phase', 'key', 'process'):
            with self.subTest(field=field):
                self.orphan(); self.evidence.identity = copy.deepcopy(self.after)
                self.evidence.settled = True; self.evidence.effect_applied = True
                rec = a.Reconciler(self.engine, self.evidence, birth=lambda p:None)
                item = rec.copies(None)[0]; proposal = rec.bounded_gather([item], None)[0]
                # Let the collector persist a real, bound proposal, then invalidate
                # one dimension of its snapshot. The live authority stays fixed.
                rec.cache(item, proposal)
                path = rec.evidence_path(item); value = json.loads(path.read_text())
                value['binding'][field] = 'stale'; path.write_text(json.dumps(value))
                with patch.object(a.Reconciler, 'bounded_gather', return_value=[]):
                    with control.locked(self.engine.directory): self.engine.reconcile_clients()
                self.assertEqual(self.actions.record(self.r)['response']['status'], 'unknown', field)
                self.assertIsNone(self.actions.reservation(self.r)['hold_identity'])
                # The cached binding is also rejected when actual authority changes.
                # Existing CC2 CAS tests exercise those live revision/digest changes.
        self.assertEqual(self.rpc_calls, [])

    def test_daemon_dies_between_actions_of_same_batch(self):
        r1 = self.orphan(); r2 = copy.deepcopy(self.r)
        r2['action_id'] = 'batch-second-0001'; r2['expected']['task_id'] = 78
        self.orphan(r2); reserve = self.actions.reservation(r2); reserve['group'] = 'rapidas'
        self.actions.save(self.actions.record(r2), reserve)
        self.evidence.settled = True; self.evidence.effect_applied = True
        recover = self.evidence.recover
        def evidence(r, *args):
            out = recover(r, *args); out['observation']['identity'] = dict(self.after, task_id=r['expected']['task_id'])
            return out
        self.evidence.recover = evidence
        original = a.Reconciler.apply; seen = []
        def gone(*args): raise OSError('synthetic dead daemon')
        def apply(rec, *args, **kwargs):
            result = original(rec, *args, **kwargs); seen.append(result)
            if len(seen) == 1: self.engine.generation_source = gone
            return result
        with patch.object(a.Reconciler, 'apply', apply): self.run_reconcile()
        self.assertEqual([self.actions.record(r)['response']['status'] for r in (r1,r2)], ['held','unknown'])
        self.assertEqual(self.rpc_calls, [])

    def test_native_generation_is_rechecked_per_action_without_process_wait(self):
        self.orphan(); self.evidence.identity = copy.deepcopy(self.after)
        self.evidence.settled = True; self.evidence.effect_applied = True
        rec = a.Reconciler(self.engine, self.evidence, birth=lambda p:'native-birth')
        pidfile = self.root/'synthetic-daemon.pid'; pidfile.write_text('12345')
        with self.engine.connect(write=True) as db:
            state = self.engine.state(db); state['pid_file'] = str(pidfile); self.engine.store(db, state)
        self.engine.generation_source = None
        # Generation authentication still brackets native identity when copying.
        with patch.object(self.engine, 'generation', return_value='daemon-1'):
            item = rec.copies(None)[0]
        proposal = rec.bounded_gather([item], None)[0]
        with patch.object(subprocess, 'Popen', side_effect=AssertionError('application must not wait for a process')), \
             patch.object(self.engine, 'generation', side_effect=AssertionError('application must use fresh native identity')):
            with control.locked(self.engine.directory):
                self.assertTrue(rec.apply(item, proposal, None, protected=True))
        self.assertEqual(self.actions.record(self.r)['response']['status'], 'held')
        # Matching copied generation is insufficient when the native life changes.
        self.orphan(); item['record'] = self.actions.record(self.r); item['reservation'] = self.actions.reservation(self.r)
        rec.birth = lambda p:None
        try:
            with control.locked(self.engine.directory): result = rec.apply(item, proposal, None, protected=True)
        except (OSError, ValueError): result = False
        self.assertFalse(result, 'dead native generation cannot promote a second phase')
        self.assertEqual(self.actions.record(self.r)['response']['status'], 'unknown')



for name in dir(t.ReconcileTests):
    if name.startswith('test_') and name not in ProgressTests.__dict__: setattr(ProgressTests, name, None)


class EntryTests(r1.StartTests):
    def held(self, path, seconds=.7):
        return Holders(self).start(path, seconds)

    def test_start_waits_on_preexisting_lock_without_journal_or_rejections(self):
        h = self.h; executed = []; elapsed = []
        log = h.directory/'arranque.log'; before = log.read_bytes() if log.exists() else b''
        for n in range(3):
            self.held(h.directory/'control.lock')
            start = time.monotonic()
            try: h.m.start(execute=lambda *x:executed.append(x))
            except control.ClientCollectionBusy: self.fail('start must wait, never reject because control.lock is occupied')
            elapsed.append(time.monotonic()-start)
            self.assertEqual(len(executed), n+1, 'one exec per public start after holder releases')
        self.assertTrue(all(.55 < duration < 1.4 for duration in elapsed), elapsed)
        self.assertEqual(log.read_bytes() if log.exists() else b'', before, 'contention is no rejection')
        print('preexisting-start-holder=.7 elapsed='+repr([round(v,3) for v in elapsed]), flush=True)

    def test_log_contention_omits_exact_sanitized_diagnostic_without_control_wait(self):
        h = self.h; log = h.directory/'arranque.log'; before = log.read_bytes() if log.exists() else b''
        self.held(h.directory/'control.lock'); lock = h.directory/'arranque.log.lock'; lock.touch()
        log_holder = self.held(lock, .3)
        start = time.monotonic()
        with patch.object(h.m, '_start', side_effect=ValueError('private sentinel')):
            with self.assertRaisesRegex(ValueError, 'private sentinel'): h.m.start()
        self.assertLess(time.monotonic()-start, .4)
        self.assertEqual(log.read_bytes() if log.exists() else b'', before)
        # Control is still occupied: the independent log can record another reason.
        log_holder.join(2)
        h.m.log_start_rejection(ValueError('private sentinel'))
        row = json.loads(log.read_text().splitlines()[-1])
        self.assertEqual(row['motivo'], 'Preparación o autorización rechazada; revisar cola, PID y autoridad')

    def test_all_four_productive_entries_and_dry_run_have_expected_order(self):
        h = self.h; seen = {}; entry = ['']; r, actions = self.seed_unsent()
        def reset():
            self.seed_unsent()
        def protected(tag):
            # The guard/preparer/recovery work precedes the end-of-turn review.
            self.assertTrue(actions.reservation(r)['open']); seen[tag] = True
        prepare = control.prepare_start
        def prepared(*args, **kwargs):
            if entry[0] == 'arrancar':
                self.assertFalse(actions.reservation(r)['open']); seen['arrancar'] = True
            else: protected(entry[0])
            return prepare(*args, **kwargs)
        common = [patch.object(control.Path, 'home', return_value=h.home), patch.object(control, 'Pueue', return_value=h.p),
                  patch.object(control, 'MiniControl', return_value=h.engine), patch.object(control, 'assert_offline')]
        for p in common: p.start(); self.addCleanup(p.stop)
        import test_handoff
        mini = test_handoff.mini
        reset(); entry[0] = 'guardia'
        with patch.object(control, 'recover_mini', side_effect=lambda *x:protected('guardia') or False), \
             patch.object(control, 'prune_history'), patch.object(sys, 'argv', ['control.py','guardia','--host','mini']):
            control.main()
        self.assertFalse(actions.reservation(r)['open'])
        for dry in (True, False):
            reset(); entry[0] = 'preparar-seco' if dry else 'preparar'
            before = (self.actions_image(actions), (h.data/'state.json').read_bytes(), h.engine.path.read_bytes(),
                      sorted(str(p.relative_to(h.directory)) for p in h.directory.rglob('*')))
            with patch.object(control, 'prepare_start', prepared), patch.object(control, 'validate_configuration', return_value=h.config), \
                 patch.object(sys, 'argv', ['control.py','preparar','--host','mini']+(['--seco'] if dry else [])), redirect_stdout(io.StringIO()):
                control.main()
            if dry:
                after = (self.actions_image(actions), (h.data/'state.json').read_bytes(), h.engine.path.read_bytes(),
                         sorted(str(p.relative_to(h.directory)) for p in h.directory.rglob('*')))
                self.assertEqual(after, before, 'dry run writes neither authority, queue nor evidence')
            self.assertEqual(actions.reservation(r)['open'], dry)
        reset(); entry[0] = 'arrancar'; executed = []
        with patch.object(control, 'prepare_start', prepared): h.m.start(execute=lambda *x:executed.append(x))
        self.assertEqual(len(executed), 1)
        reset(); entry[0] = 'recuperar'
        with patch.object(mini, 'Manager', return_value=h.m), patch.object(h.m, '_recover', side_effect=lambda:protected('recuperar') or False), \
             patch.object(sys, 'argv', ['mini.py','recuperar']): mini.main()
        self.assertFalse(actions.reservation(r)['open'])
        self.assertEqual(seen, dict.fromkeys(('guardia','preparar-seco','preparar','arrancar','recuperar'), True))

    @staticmethod
    def actions_image(actions):
        with actions.engine.connect() as db:
            return list(db.execute('SELECT body FROM client_actions')), list(db.execute('SELECT body FROM client_reservations'))


for name in dir(r1.StartTests):
    if name.startswith('test_') and name not in EntryTests.__dict__: setattr(EntryTests, name, None)


if __name__ == '__main__': unittest.main()
