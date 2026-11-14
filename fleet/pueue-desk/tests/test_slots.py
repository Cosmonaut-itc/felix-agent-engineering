"""U3.1: in-memory Pueue, injected clock/generation, SQLite only in our /tmp."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('control_slots', BASE / 'control.py')
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)


class FakePueue:
    def __init__(self):
        self.groups = {g: dict(parallel_tasks=n, status='Running') for g, n in control.MINI_PROFILE.items()}
        self.groups['ajeno'] = dict(parallel_tasks=7, status='Paused')
        self.calls, self.rpc, self.fail_at, self.unreadable = [], 0, None, False

    def boundary(self):
        self.rpc += 1
        if self.rpc == self.fail_at:
            raise subprocess.CalledProcessError(1, ['fake-rpc'])

    def status(self):
        self.boundary()
        if self.unreadable:
            raise ValueError('Estado ilegible')
        return dict(groups=copy.deepcopy(self.groups), tasks={})

    def parallel(self, group, number):
        self.calls.append(('parallel', group, number))
        self.groups[group]['parallel_tasks'] = number
        self.boundary()  # RPC effect can succeed even when its answer is lost

    def call(self, *args):
        self.calls.append(args)
        if args[0] not in ('pause', 'start'):
            raise AssertionError('No fallback to real Pueue')
        self.groups[args[-1]]['status'] = 'Paused' if args[0] == 'pause' else 'Running'
        self.boundary()


class SlotsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='u31-sol-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.p = FakePueue()
        self.gen = 'daemon-1'
        self.now = 1234.0
        self.engine = control.MiniControl(self.p, self.root, clock=lambda: self.now, generation=lambda: self.gen)
        with control.locked(self.root):
            self.engine.adopt()
        self.p.calls.clear()

    def request(self, ident='slots-00000001', delta=-1, group='suites', op='slots', **extra):
        view = self.engine.view()
        e = {k: view[k] for k in ('revision', 'generation', 'policy_hash', 'mode', 'slots')}
        return dict(schema=control.HOST_SCHEMA, op=op, action_id=ident, dry_run=False,
                    expected=e, **({'change': dict(group=group, delta=delta)} if op == 'slots' else {}), **extra)

    def change_state(self, **fields):
        if 'desired' in fields and 'overrides' not in fields:
            fields['overrides'] = self.overrides(fields['desired'])
        with self.engine.connect(write=True) as db:
            state = self.engine.state(db)
            state.update(fields)
            self.engine.store(db, state)

    def overrides(self, target):
        return {g:dict(slots=n,actor='Felix',action_id='fixture-00000001',requested_at=1000,expires_at=None)
                for g,n in target.items() if n != control.MINI_PROFILE[g]}

    def fingerprint(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def test_host_limits_boolean_zero_nine_and_complete_vector(self):
        for value in (True, False, 0, 9, -1, 1.5, '2'):
            with self.subTest(value=value):
                r = self.request()
                r['expected']['slots']['suites'] = value
                with self.assertRaises(ValueError):
                    self.engine.handle(r)
        for value in (True, 0, 9, 2, '-1'):
            r = self.request(); r['change']['delta'] = value
            with self.assertRaises(ValueError): self.engine.handle(r)
        r = self.request(); del r['expected']['slots']['default']
        with self.assertRaises(ValueError): self.engine.handle(r)
        self.assertEqual(self.p.calls, [])

    def test_host_cap_counts_paused_groups(self):
        self.p.groups['rapidas']['status'] = 'Paused'
        result = self.engine.handle(self.request(delta=1))
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(result['error']['code'], 'invalid_request')
        self.assertEqual(self.p.calls, [])

    def test_served_profile_includes_default_and_seven_places(self):
        view=self.engine.view()
        self.assertEqual(view['desired'],dict(suites=3,default=1,rapidas=3))
        self.assertEqual(sum(view['slots'].values()),7)
        self.assertEqual(view['cap'],7)

    def test_minimum_maximum_and_monotonic_repair(self):
        self.assertEqual(self.engine.handle(self.request(group='default'))['status'], 'rejected')
        for old, new in ((dict(suites=8, default=1, rapidas=1), dict(suites=7, default=1, rapidas=1)),
                         (dict(suites=4, default=2, rapidas=3), dict(suites=3, default=2, rapidas=3))):
            control.validate_target(old, new)
        for old, new in ((dict(suites=8, default=1, rapidas=1), dict(suites=9, default=1, rapidas=1)),
                         (dict(suites=4, default=2, rapidas=3), dict(suites=3, default=3, rapidas=3))):
            with self.assertRaises(ValueError): control.validate_target(old, new)
        target = dict(suites=4, default=2, rapidas=3)
        for g, n in target.items(): self.p.groups[g]['parallel_tasks'] = n
        self.change_state(desired=target)
        result = self.engine.handle(self.request('repair-00000001'))
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(sum(result['result']['desired'].values()), 8)

    def test_revision_vector_policy_generation_and_reset_checked_on_host(self):
        for key, value in (('revision', 99), ('policy_hash', 'f'*64), ('generation', 'other'),
                           ('slots', dict(suites=2, default=1, rapidas=3))):
            r = self.request('conflict-'+key.replace('_','-')); r['expected'][key] = value
            self.assertEqual(self.engine.handle(r)['status'], 'conflict')
        self.p.groups['default']['status'] = 'Reset'
        r = dict(schema=control.HOST_SCHEMA, op='slots', action_id='reset-00000001', dry_run=False,
                 expected=dict(revision=1, generation=self.gen, policy_hash=control.POLICY_HASH,
                               mode='fijo', slots=dict(control.MINI_PROFILE)), change=dict(group='suites',delta=-1))
        self.assertEqual(self.engine.handle(r)['status'], 'unknown')
        self.assertEqual(self.p.calls, [])

    def test_new_generation_requires_reconciliation_before_accepting_fresh_request(self):
        self.gen = 'daemon-2'
        self.assertEqual(self.engine.handle(self.request())['status'], 'conflict')
        self.assertEqual(self.p.calls, [])

    def test_host_switch_rejects_new_effect_but_accepted_recovery_continues(self):
        # Keep the test switch inside this fixture, using the real sibling layout.
        directory = self.root/'host'
        directory.mkdir()
        engine = control.MiniControl(self.p,directory,generation=lambda:self.gen)
        with control.locked(directory): engine.adopt()
        r = self.request();r['expected'] = {k:engine.view()[k] for k in ('revision','policy_hash','generation','mode','slots')}
        self.assertEqual(engine.handle(r)['status'],'applied')
        self.p.groups['suites']['parallel_tasks']=3
        self.p.calls.clear()
        rack = self.root/'rack';rack.mkdir();(rack/'actions.disabled').symlink_to(self.root/'absent-switch-target')
        self.assertTrue(engine.reconcile())
        self.assertEqual(self.p.calls,[('parallel','suites',2)])
        self.p.calls.clear();r['action_id']='disabled-0000001'
        self.assertEqual(engine.handle(r)['status'], 'rejected')
        self.assertEqual(self.p.calls, [])

    def test_invalid_desire_metadata_does_not_apply_default(self):
        with self.engine.connect(write=True) as db:
            state=self.engine.state(db);state['overrides']['suites']=dict(slots=2,actor='Felix',action_id='corrupt-000001',requested_at=1000,expires_at=None)
            self.engine.store(db,state)
        self.assertEqual(self.engine.handle(dict(schema=control.HOST_SCHEMA,op='capabilities'))['status'], 'recovery_error')
        self.assertEqual(self.p.calls, [])

    def test_apply_persists_until_next_manual_change_and_undo_restores_overrides(self):
        one = self.engine.handle(self.request())
        self.assertEqual(one['status'], 'applied')
        self.assertEqual(one['result']['applied'], ['suites'])
        self.assertEqual(one['result']['unconfirmed'], [])
        self.now += 86400 * 20
        new = control.MiniControl(self.p, self.root, clock=lambda: self.now, generation=lambda: self.gen)
        new.reconcile()
        self.assertEqual(new.view()['desired']['suites'], 2)
        undo = self.request('undo-00000001', op='undo', undo_of='slots-00000001')
        answer = new.handle(undo)
        self.assertEqual(answer['status'], 'applied')
        self.assertEqual(answer['result']['observed'], control.MINI_PROFILE)
        with new.connect() as db:
            self.assertEqual(new.state(db)['overrides'], {})

    def test_undo_strict_revision_and_no_inverse_when_state_changed(self):
        self.engine.handle(self.request())
        self.engine.handle(self.request('slots-00000002', group='rapidas'))
        self.engine.handle(self.request('slots-00000003', group='rapidas', delta=1))
        answer = self.engine.handle(self.request('undo-00000001', op='undo', undo_of='slots-00000001'))
        self.assertEqual((answer.get('error') or {}).get('code'), 'undo_conflict')
        self.assertEqual(len(self.p.calls), 3)

    def test_dry_run_plan_queries_and_unadopted_have_zero_filesystem_or_rpc_effects(self):
        request = self.request(); request['dry_run'] = True
        before = self.fingerprint()
        result = self.engine.handle(request)
        self.assertEqual(result['status'], 'dry_run')
        self.assertEqual(result['result']['decreases'], [dict(group='suites', slots=2)])
        self.assertEqual(result['result']['increases'], [])
        self.assertEqual(result['result']['rpc'], [['parallel', '2', '-g', 'suites']])
        self.engine.handle(dict(schema=control.HOST_SCHEMA, op='capabilities'))
        self.engine.handle(dict(schema=control.HOST_SCHEMA, op='status', action_id='unknown-000001'))
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual(self.p.calls, [])
        absent = self.root/'not-adopted'
        other = control.MiniControl(self.p, absent, generation=lambda: self.gen)
        self.assertEqual(other.handle(request)['status'], 'recovery_error')
        self.assertFalse(absent.exists())

    def test_actual_rack_cli_has_readonly_ensayo_and_bounded_lock_contention(self):
        home=self.root/'home'
        directory=home/'.local/state/pao/pueue-mini'
        engine=control.MiniControl(self.p,directory,generation=lambda:self.gen)
        with control.locked(directory):engine.adopt()
        request=self.request();request['dry_run']=True
        harness='''
import importlib.util,json,sys
spec=importlib.util.spec_from_file_location('cli',sys.argv[1]);c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
class Fake:
 def status(self):return dict(tasks={},groups={g:dict(status='Running',parallel_tasks=n) for g,n in c.MINI_PROFILE.items()})
 def parallel(self,*a):raise AssertionError('ensayo wrote')
def factory(binary):
 assert binary=='/opt/homebrew/bin/pueue'
 return Fake()
c.Pueue=factory;c.mini_generation=lambda p:'daemon-1'
sys.argv=['control.py','rack','--host','mini'];c.main()
'''
        def run(body):
            result=subprocess.run(['/usr/bin/python3','-I','-c',harness,str(BASE/'control.py')],
                input=json.dumps(body),text=True,capture_output=True,timeout=3,
                env=dict(os.environ,HOME=str(home)))
            self.assertEqual(result.returncode,0,result.stderr)
            return json.loads(result.stdout)
        before=self.fingerprint()
        self.assertEqual(run(request)['status'],'dry_run')
        self.assertEqual(self.fingerprint(),before)
        with control.locked(directory):
            query=dict(schema=control.HOST_SCHEMA,op='status',action_id=request['action_id'])
            self.assertEqual(run(query)['status'],'unknown')

    def prepare_pending(self):
        before = dict(suites=2, default=2, rapidas=3)
        target = dict(suites=3, default=1, rapidas=3)
        for g, n in before.items(): self.p.groups[g]['parallel_tasks'] = n
        self.change_state(desired=before)
        request = self.request()
        response = self.engine.reply(request, 'applying', revision=2,
            result=dict(before=before, desired=target, observed=before))
        action = dict(request=request, before=before, before_overrides={}, target=target,
                      revision=2, response=response)
        with self.engine.connect(write=True) as db:
            self.engine.store(db, dict(self.engine.state(db), desired=target, overrides=self.overrides(target), revision=2))
            db.execute('INSERT INTO host_actions VALUES (?,?,?)', (request['action_id'], 'digest', control.canonical(action)))
        return request, target

    def test_reductions_verified_before_increases(self):
        _, target = self.prepare_pending()
        self.assertTrue(self.engine.reconcile())
        self.assertEqual(self.p.calls, [('parallel', 'default', 1), ('parallel', 'suites', 3)])
        self.assertEqual(self.engine.view()['slots'], target)

    def test_unconfirmed_reduction_never_runs_increases(self):
        self.prepare_pending()
        def ignored(group, number):
            self.p.calls.append(('parallel', group, number))
        self.p.parallel = ignored
        self.assertFalse(self.engine.reconcile())
        self.assertEqual(self.p.calls, [('parallel', 'default', 1)])

    def test_recovery_never_increases_above_cap_even_for_grandfathered_desire(self):
        self.change_state(desired=dict(suites=4,default=1,rapidas=3))
        self.assertFalse(self.engine.reconcile())
        self.assertEqual(self.p.calls, [])

    def test_atomic_desire_and_intent_when_receipt_insert_fails(self):
        request = self.request()
        with self.engine.connect(write=True) as db:
            db.execute("CREATE TRIGGER fail_intent BEFORE INSERT ON host_actions BEGIN SELECT RAISE(ABORT, 'fake-disk'); END")
        answer = self.engine.handle(request)
        self.assertEqual(answer['status'], 'unknown')
        self.assertEqual(self.engine.view()['desired'], control.MINI_PROFILE)
        self.assertEqual(self.engine.view()['revision'], 1)
        self.assertEqual(self.p.calls, [])

    def test_crash_after_accept_before_first_rpc_is_recovered_by_same_guard(self):
        request=self.request()
        with patch.object(self.engine,'reconcile',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):self.engine.handle(request)
        self.assertEqual(self.p.calls, [])
        reopened=control.MiniControl(self.p,self.root,generation=lambda:self.gen)
        self.assertEqual(reopened.view()['desired']['suites'],2)
        query=dict(schema=control.HOST_SCHEMA,op='status',action_id=request['action_id'])
        self.assertEqual(reopened.handle(query)['status'],'applying')
        self.assertTrue(reopened.reconcile())
        self.assertEqual(reopened.handle(query)['status'],'applied')
        self.assertEqual(self.p.calls,[('parallel','suites',2)])

    def test_pending_queries_and_ensayo_do_not_recover(self):
        request, _ = self.prepare_pending()
        before = self.fingerprint()
        self.engine.handle(dict(schema=control.HOST_SCHEMA,op='capabilities'))
        self.engine.handle(dict(schema=control.HOST_SCHEMA,op='status',action_id=request['action_id']))
        r = self.request('ensayo-000001');r['dry_run'] = True
        self.assertEqual(self.engine.handle(r)['status'], 'conflict')
        self.assertEqual(self.p.calls, [])
        self.assertEqual(self.fingerprint(), before)

    def test_actual_guard_dispatch_never_falls_back_when_authority_corrupt(self):
        self.engine.path.write_bytes(b'corrupt')
        with patch.object(control, 'pressure_mini', return_value=dict(load=0,memory=1)):
            with self.assertRaises(sqlite3.DatabaseError):
                control.guard(self.p, None, self.root, host='mini')
        self.assertEqual(self.p.calls, [])

    def test_broken_authority_marker_never_restores_legacy_guard(self):
        directory=self.root/'broken';directory.mkdir()
        (directory/'control.adopted').symlink_to(directory/'missing')
        (directory/'guardia-state.json').write_text('["suites"]')
        self.p.groups['suites']['status']='Paused'
        with patch.object(control,'pressure_mini',return_value=dict(load=0,memory=1)):
            with self.assertRaises(sqlite3.Error):control.guard(self.p,None,directory,host='mini')
        self.assertEqual(self.p.calls,[])
        with self.assertRaises(ValueError):control.MiniControl(self.p,directory).adopt()

    def test_failure_after_each_rpc_is_visible_and_single_reconciler_completes(self):
        for boundary in range(1, 7):
            with self.subTest(boundary=boundary):
                with self.engine.connect(write=True) as db: db.execute('DELETE FROM host_actions')
                request, target = self.prepare_pending()
                self.p.rpc = 0; self.p.fail_at = boundary; self.p.calls.clear()
                okay = self.engine.reconcile()
                answer = self.engine.handle(dict(schema=control.HOST_SCHEMA, op='status', action_id=request['action_id']))
                self.assertFalse(okay)
                self.assertIn(answer['status'], ('partial', 'unknown'))
                self.assertEqual(answer['result']['desired'], target)
                self.assertEqual(set(answer['result']['applied']) | set(answer['result']['unconfirmed']), {'suites','default'})
                if self.p.calls and self.p.calls[-1][1] == 'suites':
                    self.assertEqual(self.p.groups['default']['parallel_tasks'], 1)
                self.p.fail_at = None
                self.assertTrue(self.engine.reconcile())
                answer = self.engine.handle(dict(schema=control.HOST_SCHEMA, op='status', action_id=request['action_id']))
                self.assertEqual(answer['status'], 'applied')

    def test_unknown_unreadable_observation_not_repeated_by_status_or_same_id(self):
        request, _ = self.prepare_pending()
        self.p.unreadable = True
        self.assertFalse(self.engine.reconcile())
        self.assertEqual(self.engine.handle(dict(schema=control.HOST_SCHEMA,op='status',action_id=request['action_id']))['status'], 'unknown')
        self.assertEqual(self.p.calls, [])
        self.p.unreadable = False

    def test_id_digest_duplicate_and_rejections_are_durable(self):
        request = self.request()
        one = self.engine.handle(request)
        self.assertEqual(self.engine.handle(request), one)
        changed = copy.deepcopy(request); changed['change']['group'] = 'rapidas'
        self.assertEqual((self.engine.handle(changed).get('error') or {}).get('code'), 'idempotency_conflict')
        self.assertEqual(len(self.p.calls), 1)
        request = self.request('rejected-000001', delta=1, group='default')
        request['expected']['revision'] = 500
        self.assertEqual(self.engine.handle(request)['status'], 'conflict')
        request['expected']['revision'] = 2
        self.assertEqual((self.engine.handle(request).get('error') or {}).get('code'), 'idempotency_conflict')

    def test_drift_visible_immediately_and_dry_run_does_not_reconcile(self):
        self.p.groups['suites']['parallel_tasks'] = 2
        view = self.engine.view()
        self.assertTrue(view['drift'])
        self.assertEqual(view['slots']['suites'], 2)
        self.assertEqual(view['desired']['suites'], 3)
        request = self.request(); request['dry_run'] = True
        before = self.fingerprint()
        answer = self.engine.handle(request)
        self.assertEqual(answer['status'], 'conflict')
        self.assertEqual(self.p.calls, [])
        self.assertEqual(self.fingerprint(), before)

    def test_restart_lost_parallel_is_reconciled_without_opening_ambiguous_or_foreign_groups(self):
        self.engine.handle(self.request())
        self.p.calls.clear(); self.gen = 'daemon-2'
        self.p.groups['suites'].update(parallel_tasks=3, status='Paused')
        self.engine.guard(dict(load=0, memory=1))
        self.assertEqual(self.p.groups['suites']['parallel_tasks'], 2)
        self.assertEqual(self.p.groups['suites']['status'], 'Paused')
        self.assertIn('ambiguous', self.engine.view()['pause_reasons']['suites'])
        self.assertIn('ambiguous', self.engine.view()['pause_reasons']['ajeno'])
        self.assertFalse(any(c[0] == 'start' for c in self.p.calls))
        with patch.object(control, 'pressure_mini', return_value=dict(load=0,memory=1)):
            self.engine.manual('suites', resume=True)
        self.assertEqual(self.p.groups['suites']['status'], 'Running')
        self.assertEqual(self.p.groups['ajeno']['status'], 'Paused')

    def test_guard_pressure_rapidas_and_manual_takeover(self):
        self.engine.guard(dict(load=2.5,memory=1))
        reasons = self.engine.view()['pause_reasons']
        self.assertEqual(reasons['suites'], ['pressure'])
        self.assertNotIn('rapidas', reasons)
        self.engine.manual('suites')
        with patch.object(control, 'pressure_mini', return_value=dict(load=0,memory=1)):
            self.engine.guard(dict(load=0,memory=1))
        self.assertEqual(self.p.groups['suites']['status'], 'Paused')
        self.assertEqual(self.p.groups['default']['status'], 'Running')
        self.engine.guard(dict(load=3.1,memory=1))
        self.assertEqual(self.engine.view()['pause_reasons']['rapidas'], ['pressure'])

    def test_pause_intent_precedes_rpc_and_failed_start_keeps_ownership(self):
        self.p.fail_at = self.p.rpc + 4  # guard reconcile+verification+observation, then pause
        try: self.engine.guard(dict(load=4,memory=1))
        except subprocess.CalledProcessError: pass
        self.p.fail_at = None
        self.engine.guard(dict(load=4,memory=1))
        self.assertEqual(self.engine.view()['pause_reasons']['suites'], ['pressure'])

        old = self.p.call
        def fail(*args):
            if args[0] == 'start': raise subprocess.CalledProcessError(1, ['fake-start'])
            return old(*args)
        with patch.object(self.p, 'call', side_effect=fail), patch.object(control,'pressure_mini',return_value=dict(load=0,memory=1)):
            with self.assertRaises(subprocess.CalledProcessError): self.engine.guard(dict(load=0,memory=1))
        self.assertEqual(self.engine.view()['pause_reasons']['suites'], ['pressure'])

    def test_pause_intention_durable_before_effect(self):
        original = self.p.call
        def checked(*args):
            if args[0] == 'pause':
                with self.engine.connect() as db:
                    self.assertIn('pressure', self.engine.state(db)['reasons'].get(args[-1], []))
            return original(*args)
        with patch.object(self.p, 'call', side_effect=checked):
            self.engine.guard(dict(load=4,memory=1))

    def test_manual_pause_intention_durable_before_effect(self):
        original=self.p.call
        def checked(*args):
            with self.engine.connect() as db:
                self.assertIn('manual',self.engine.state(db)['reasons'].get(args[-1],[]))
            return original(*args)
        with patch.object(self.p,'call',side_effect=checked):self.engine.manual('suites')

    def test_fresh_pressure_prevents_reopen_after_low_initial_sample(self):
        self.engine.guard(dict(load=4,memory=1))
        self.p.calls.clear()
        with patch.object(control, 'pressure_mini', return_value=dict(load=4,memory=1)):
            self.engine.guard(dict(load=0,memory=1))
        self.assertEqual(self.p.calls, [])
        self.assertEqual(self.engine.view()['pause_reasons']['suites'], ['pressure'])

    def test_daemon_change_between_sensor_and_start_retains_pause(self):
        self.engine.guard(dict(load=4,memory=1))
        self.p.calls.clear()
        def fresh():
            self.gen='daemon-2'
            return dict(load=0,memory=1)
        with patch.object(control, 'pressure_mini', side_effect=fresh):
            with self.assertRaises(ValueError): self.engine.guard(dict(load=0,memory=1))
        self.assertEqual(self.p.calls, [])

    def test_guard_never_opens_before_desire_verified(self):
        self.engine.guard(dict(load=4,memory=1))
        self.p.calls.clear()
        with patch.object(self.engine,'reconcile',return_value=False), patch.object(control,'pressure_mini',return_value=dict(load=0,memory=1)):
            with self.assertRaises(ValueError): self.engine.guard(dict(load=0,memory=1))
        self.assertEqual(self.p.calls, [])

    def test_missing_corrupt_authority_never_applies_default_or_admits(self):
        self.engine.handle(self.request())
        self.p.calls.clear()
        self.engine.path.write_bytes(b'not sqlite')
        self.assertEqual(self.engine.handle(self.request_without_read())['status'], 'recovery_error')
        with self.assertRaises(sqlite3.DatabaseError): self.engine.guard(dict(load=0,memory=1))
        self.engine.path.unlink()
        with self.assertRaises(sqlite3.OperationalError): self.engine.guard(dict(load=0,memory=1))
        with self.assertRaises(ValueError): self.engine.adopt()
        self.assertEqual(self.p.calls, [])


    def protective_tick(self, load=4, memory=1):
        with patch.object(control, 'pressure_mini', return_value=dict(load=load, memory=memory)), \
             patch.object(control, 'MiniControl', return_value=self.engine):
            try:
                control.guard(self.p, None, self.root, host='mini')
            except (OSError, sqlite3.Error, ValueError, KeyError, subprocess.SubprocessError):
                pass  # The diagnostic must not prevent the protective assertions below.

    def assert_closed(self, *groups):
        for group in groups:
            self.assertEqual(self.p.groups[group]['status'], 'Paused', group)
        self.assertFalse(any(c[0] in ('start', 'parallel') for c in self.p.calls), self.p.calls)

    def test_corrupt_quota_authority_still_closes_running_groups(self):
        self.engine.path.write_bytes(b'not sqlite')
        before = self.engine.path.read_bytes()
        for _ in range(3):
            self.protective_tick()
            self.assert_closed('suites', 'default', 'rapidas')
        self.assertEqual(self.engine.path.read_bytes(), before)

    def test_missing_profile_member_three_ticks_preserve_rapidas_ladder(self):
        del self.p.groups['default']
        for _ in range(3):
            self.protective_tick(load=2.5)
            self.assert_closed('suites')
            self.assertEqual(self.p.groups['rapidas']['status'], 'Running')
        self.protective_tick(load=3.1)
        self.assert_closed('suites', 'rapidas')
        self.assertNotIn('default', self.p.groups)

    def test_invalid_quota_still_closes_existing_groups_without_profile_fallback(self):
        self.p.groups['suites']['parallel_tasks'] = 0
        self.protective_tick(memory=2, load=0)
        self.assert_closed('suites', 'default', 'rapidas')
        self.assertEqual(self.p.groups['suites']['parallel_tasks'], 0)

    def test_failed_reconciliation_rpc_still_closes_and_retains_desire(self):
        self.change_state(desired=dict(suites=2, default=1, rapidas=3))
        with patch.object(self.p, 'parallel', side_effect=OSError('RPC unavailable')):
            for _ in range(3):
                self.protective_tick()
                self.assert_closed('suites', 'default', 'rapidas')
        self.assertEqual(self.engine.view()['desired']['suites'], 2)

    def test_failed_pause_rpc_does_not_skip_other_groups(self):
        original = self.p.call
        def fail(*args):
            if args[-1] == 'suites':
                raise OSError('one group unreachable')
            return original(*args)
        with patch.object(self.p, 'call', side_effect=fail):
            for _ in range(3):
                self.protective_tick()
                self.assert_closed('default', 'rapidas')
        self.assertEqual(self.p.groups['suites']['status'], 'Running')

    def test_failed_authority_write_closes_but_never_grants_future_reopen(self):
        # A stale pressure owner must not reclaim an emergency pause after repair.
        self.change_state(reasons=dict(suites=['pressure']))
        with patch.object(self.engine, 'store', side_effect=sqlite3.OperationalError('readonly authority')):
            self.protective_tick()
        self.assert_closed('suites', 'default', 'rapidas')
        self.p.calls.clear()
        with patch.object(control, 'pressure_mini', return_value=dict(load=0, memory=1)):
            self.engine.guard(dict(load=0, memory=1))
        self.assert_closed('suites', 'default', 'rapidas')
        self.assertIn('manual', self.engine.view()['pause_reasons']['suites'])
        with patch.object(control, 'pressure_mini', return_value=dict(load=0, memory=1)):
            self.engine.manual('suites', resume=True)
        self.assertEqual(self.p.groups['suites']['status'], 'Running')
        self.assertEqual(self.p.groups['default']['status'], 'Paused')

    def request_without_read(self):
        return dict(schema=control.HOST_SCHEMA,op='capabilities')

    def test_import_ownership_separate_profile_including_missing_legacy_groups(self):
        other = self.root/'migration'; other.mkdir()
        (other/'guardia-state.json').write_text(json.dumps(['suites','legacy-absent','ajeno']))
        self.p.groups['suites']['status'] = 'Paused'
        engine = control.MiniControl(self.p,other,generation=lambda:self.gen)
        with control.locked(other): engine.adopt()
        reasons = engine.view()['pause_reasons']
        self.assertEqual(reasons['suites'], ['pressure'])
        self.assertEqual(reasons['legacy-absent'], ['manual'])
        self.assertEqual(reasons['ajeno'], ['manual'])
        self.assertFalse((other/'guardia-state.json').exists())
        engine.guard(dict(load=2.5,memory=1))
        self.assertEqual(self.p.groups['ajeno']['status'], 'Paused')


if __name__ == '__main__': unittest.main()
