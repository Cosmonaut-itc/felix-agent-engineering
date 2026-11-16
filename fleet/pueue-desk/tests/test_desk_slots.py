"""Desk quota authority: fake time, Pueue, sensors and private /tmp filesystem."""
import copy
import fcntl
from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from contextlib import contextmanager, redirect_stdout
from unittest.mock import patch
from test_slots import control, FakePueue


def utc(value):
    return datetime.fromisoformat(value + '+00:00').timestamp()


def changed_files(before, after):
    """Paths whose bytes differ. A byte diff of the whole tree is slow and unreadable (SEN-6417)."""
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


class DeskFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='u32-sol-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'pid').write_text('321\n')
        self.now = utc('2026-11-14T13:59:59')
        self.gen = 'linux-proc-v1:1111111111111111111111111111111111111111111111111111111111111111'
        self.p = FakePueue()
        self.p.groups = {g: dict(parallel_tasks=n, status='Running') for g, n in control.desk_profile(self.now).items()}
        self.p.groups.update(default=dict(parallel_tasks=1, status='Paused'), ajeno=dict(parallel_tasks=6, status='Paused'))
        self.engine = control.MiniControl(self.p, self.root/'host', clock=lambda:self.now,
                                          generation=lambda:self.gen, host='desk')
        with control.locked(self.engine.directory):
            self.engine.adopt(self.root/'pid')
        self.low = dict(io=0, cpu=0, memory=8*1024**3)

    def request(self, ident='desk-slots-0001', group='jale', delta=-1, undo_of=None, dry_run=False):
        v = self.engine.view()
        return dict(schema=control.HOST_SCHEMA, op='undo' if undo_of else 'slots', action_id=ident, dry_run=dry_run,
                    expected={k:v[k] for k in ('revision','policy_hash','generation','mode','slots')},
                    **(dict(undo_of=undo_of) if undo_of else dict(change=dict(group=group, delta=delta))))

    def status(self, request):
        return self.engine.handle(dict(schema=control.HOST_SCHEMA, op='status', action_id=request['action_id']))

    def fingerprint(self):
        return {p.relative_to(self.root).as_posix():p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def state(self):
        with self.engine.connect() as db:
            return self.engine.state(db)

    def set_desire(self, target):
        s = self.state()
        s['desired'] = target
        s['overrides'] = {g:dict(slots=n, actor='Felix', action_id='fixture-000001', requested_at=self.now,
                                expires_at=control.schedule_at(self.now)[1]) for g,n in target.items()}
        with self.engine.connect(write=True) as db:
            self.engine.store(db, s)


class DeskSlotsTests(DeskFixture):
    def test_verified_table_caps_and_reserved_default(self):
        self.assertEqual(control.GROUP_LIMITS, {'hasito':{'dia':1,'noche':2}, 'jale':{'dia':5,'noche':7},
                         'jale-verify':{'dia':1,'noche':1}, 'otros':{'dia':2,'noche':2},
                         'pesadas':{'dia':2,'noche':3}, 'rapidas':{'dia':3,'noche':3}})
        for when, mode, cap in (('2026-11-14T13:59:59','noche',18), ('2026-11-14T14:00:00','dia',14)):
            self.now = utc(when)
            self.assertTrue(self.engine.reconcile())
            v = self.engine.view()
            self.assertEqual((v['mode'], v['cap'], sum(v['slots'].values())), (mode, cap, cap))
            self.assertTrue(v['capabilities']['mode'])
            self.assertNotIn('default', v['slots'])
            self.assertFalse(any(v['capabilities'][k] for k in ('reorder','restart_queue')))
            r = self.request(group='default')
            with self.assertRaises(ValueError):self.engine.handle(r)
            self.assertEqual(self.p.groups['default']['parallel_tasks'],1)

    def test_host_validates_limits_booleans_vector_revision_hash_generation_mode(self):
        for value in (True,False,0,9,-1,2.0,'2'):
            r = self.request(); r['expected']['slots']['jale'] = value
            with self.assertRaises(ValueError):self.engine.handle(r)
        for value in (True,False,0,9,2,'-1'):
            r = self.request(); r['change']['delta'] = value
            with self.assertRaises(ValueError):self.engine.handle(r)
        r = self.request(); del r['expected']['slots']['otros']
        with self.assertRaises(ValueError):self.engine.handle(r)
        for key,value in (('revision',99), ('policy_hash','0'*64), ('generation','other'), ('mode','dia'),
                          ('slots',dict(control.desk_profile(self.now), jale=6))):
            r = self.request('stale-'+key.replace('_','-')); r['expected'][key] = value
            self.assertEqual(self.engine.handle(r)['status'],'conflict')
        self.assertEqual(self.p.calls,[])

    def test_min_max_caps_count_paused_in_both_modes_and_monotonic_repair(self):
        for when in ('2026-11-14T13:59:59','2026-11-14T14:00:00'):
            self.now = utc(when); self.assertTrue(self.engine.reconcile()); self.p.calls.clear()
            self.p.groups['jale']['status'] = 'Paused'
            self.assertEqual(self.engine.handle(self.request('full-'+str(int(self.now)),delta=1))['status'],'rejected')
            self.assertEqual(self.engine.handle(self.request('min-'+str(int(self.now)),group='jale-verify'))['status'],'rejected')
            self.assertEqual(self.p.calls,[])
        target = dict(control.desk_profile(self.now), jale=8, otros=3)
        self.set_desire(target)
        for g,n in target.items():self.p.groups[g]['parallel_tasks']=n
        self.assertEqual(self.engine.handle(self.request('max-00000001',delta=1))['status'],'rejected')
        result = self.engine.handle(self.request('repair-000001'))
        self.assertEqual(result['status'],'applied')
        self.assertGreater(sum(result['result']['desired'].values()),14)
        with self.assertRaises(ValueError):
            control.validate_target(target, dict(target,jale=7,otros=4), 'desk',14)

    def test_before_event_exact_undo_and_original_expirations(self):
        first = self.request(); self.assertEqual(self.engine.handle(first)['status'],'applied')
        before = self.state()
        second = self.request('desk-second-01', group='pesadas')
        result = self.engine.handle(second)
        self.assertEqual(result['result']['expires_at'],utc('2026-11-14T14:00:00'))
        undo = self.engine.handle(self.request('desk-undo-001',undo_of=second['action_id']))
        self.assertEqual(undo['status'],'applied')
        after = self.state()
        self.assertEqual(after['desired'], before['desired'])
        self.assertEqual(after['overrides'], before['overrides'])
        self.assertEqual(after['schedule_event'],before['schedule_event'])

    def test_undo_after_event_rejected_without_rpc_even_if_timer_missed(self):
        r = self.request(); self.engine.handle(r)
        undo = self.request('expired-undo-01',undo_of=r['action_id'])
        self.now = utc('2026-11-14T14:00:00')
        self.p.calls.clear()
        rpc_before = self.p.rpc
        result = self.engine.handle(undo)
        self.assertEqual(self.p.rpc, rpc_before)
        self.assertEqual((result['status'],result['error']['code'],result.get('effects')),('conflict','undo_conflict',False))
        self.assertEqual(self.p.calls,[])
        self.assertEqual(self.status(undo)['error']['code'],'undo_conflict')
        self.assertTrue(self.engine.reconcile())
        self.assertEqual(self.engine.handle(self.request('expired-undo-02',undo_of=r['action_id']))['error']['code'],'undo_conflict')

    def test_missed_whole_day_expires_by_event_identity_even_same_mode(self):
        r = self.request(); self.engine.handle(r); self.p.calls.clear()
        old = self.state()
        self.now += 24*3600
        view = self.engine.view()
        self.assertEqual(view['mode'],'noche')
        self.assertEqual(view['desired'],control.desk_profile(self.now))
        self.assertEqual(view['revision'],old['revision']+1)
        self.assertTrue(view['drift'])
        self.assertIsNone(view['expires_at'])
        self.assertTrue(self.engine.reconcile())
        self.assertEqual(self.state()['overrides'],{})
        self.assertEqual(self.p.groups['jale']['parallel_tasks'],7)

    def test_queries_ensayo_and_stale_event_request_never_recover_or_write(self):
        r=self.request(); self.engine.handle(r)
        stale=self.request('stale-event-01')
        self.now=utc('2026-11-14T14:00:00')
        self.p.calls.clear(); before=self.fingerprint()
        v=self.engine.handle(dict(schema=control.HOST_SCHEMA,op='capabilities'))
        self.assertEqual(v['mode'],'dia');self.assertTrue(v['drift'])
        self.status(r)
        dry=self.request('ensayo-event-01',dry_run=True)
        self.assertEqual(self.engine.handle(dry)['status'],'conflict')
        self.assertEqual(changed_files(before, self.fingerprint()), [])
        self.assertEqual(self.p.calls,[])
        self.assertEqual(self.state()['overrides']['jale']['slots'],6)
        self.assertEqual(self.engine.handle(stale)['status'],'conflict')
        self.assertEqual(self.engine.view()['slots'],control.desk_profile(self.now))
        self.assertEqual(self.state()['overrides'],{})

    def foreign_status(self, tasks):
        original = self.p.status
        def status():
            snapshot = original()
            snapshot['tasks'] = copy.deepcopy(tasks)
            return snapshot
        return patch.object(self.p, 'status', side_effect=status)

    def test_foreign_work_blocks_increases_but_allows_safe_reductions(self):
        self.assertEqual(self.engine.handle(self.request())['status'], 'applied')
        self.p.groups['ajeno']['parallel_tasks'] = 8
        tasks = {'1': dict(group='ajeno', status={'Running': {}}, envs={'secret': 'never-publish'},
                           command='never-publish')}
        untouched = copy.deepcopy(self.p.groups['ajeno'])
        with self.foreign_status(tasks):
            view = self.engine.view()
            self.p.calls.clear()
            answer = self.engine.handle(self.request('foreign-up-0001', delta=1))
            self.assertEqual((answer['status'], (answer.get('error') or {}).get('code'), answer.get('effects')),
                             ('rejected', 'foreign_work', False))
            self.assertEqual(view.get('foreign_work'), 1)
            self.assertNotIn('never-publish', json.dumps(view))
            self.assertEqual(self.p.calls, [])
            down = self.engine.handle(self.request('foreign-down-01'))
            self.assertEqual(down['status'], 'applied')
            self.assertEqual(down['result']['observed']['jale'], 5)
            undo = self.engine.handle(self.request('foreign-undo-01', undo_of='foreign-down-01'))
            self.assertEqual((undo.get('error') or {}).get('code'), 'foreign_work')
        self.assertEqual(self.p.groups['ajeno'], untouched)
        self.assertEqual(self.engine.view().get('foreign_work'), 0)
        self.assertEqual(self.engine.handle(self.request('foreign-gone-01', delta=1))['status'], 'applied')

    def test_foreign_work_does_not_veto_accepted_recovery_increase(self):
        self.set_desire(dict(control.desk_profile(self.now), jale=6, otros=3))
        tasks = {}
        original = self.p.parallel
        def arriving(group, value):
            original(group, value)
            tasks['2'] = dict(group='default', status='Queued')
        with self.foreign_status(tasks), patch.object(self.p, 'parallel', side_effect=arriving):
            self.assertTrue(self.engine.reconcile())
        self.assertEqual(self.p.calls, [('parallel', 'jale', 6), ('parallel', 'otros', 3)])
        self.assertEqual(self.p.groups['otros']['parallel_tasks'], 3)
        self.assertFalse(self.engine.view()['drift'])

    def test_real_slots_recovers_missed_timer_before_judging_fresh_projection(self):
        self.engine.handle(self.request())
        self.now = utc('2026-11-14T14:00:00')
        fresh = self.request('missed-timer-01')
        self.p.calls.clear()
        answer = self.engine.handle(fresh)
        self.assertEqual(answer['status'], 'conflict')
        self.assertEqual(self.engine.view()['slots'], control.desk_profile(self.now))
        self.assertEqual(self.state()['overrides'], {})
        self.assertFalse(self.engine.view()['drift'])
        self.assertTrue(self.p.calls)
        self.assertTrue(answer.get('effects'))
        calls = copy.deepcopy(self.p.calls)
        self.assertEqual(self.engine.handle(fresh), answer)
        self.assertEqual(self.p.calls, calls)

    def test_unadopted_cli_is_unsupported_but_lost_authority_stays_closed(self):
        absent = self.root/'never-adopted'
        caps = dict(schema=control.HOST_SCHEMA, op='capabilities')
        status = dict(schema=control.HOST_SCHEMA, op='status', action_id='absent-000001')
        request = self.request()
        def invoke(body):
            out = io.StringIO()
            with patch.object(control, 'STATE_DIR', absent), patch.object(control, 'Pueue', return_value=self.p), \
                 patch.object(control.sys, 'argv', ['control.py', 'rack', '--host', 'desk']), \
                 patch.object(control.sys, 'stdin', io.TextIOWrapper(io.BytesIO(json.dumps(body).encode()))), redirect_stdout(out):
                control.main()
            return json.loads(out.getvalue())
        before = self.fingerprint()
        for body in (caps, request, dict(request, dry_run=True)):
            answer = invoke(body)
            self.assertEqual((answer['status'], (answer.get('error') or {}).get('code'), answer.get('effects')),
                             ('unsupported', 'unsupported', False))
        self.assertEqual(invoke(status)['status'], 'unknown')
        self.assertEqual(changed_files(before, self.fingerprint()), [])
        self.assertFalse(absent.exists())
        absent.mkdir()
        for filename in ('control.adopted', 'control.sqlite3'):
            marker = absent/filename
            marker.write_bytes(b'corrupt-or-lost')
            self.assertEqual(invoke(caps)['status'], 'recovery_error')
            self.assertNotEqual(invoke(request)['status'], 'unsupported')
            marker.unlink()
        self.assertEqual(self.p.calls, [])

    def test_expired_partial_never_becomes_terminal_before_new_table_verified(self):
        r = self.pending()
        self.now = utc('2026-11-14T14:00:00')
        with patch.object(self.p, 'parallel', side_effect=OSError('still unavailable')):
            self.assertFalse(self.engine.reconcile())
        answer = self.status(r)
        self.assertEqual(answer['status'], 'partial')
        self.assertTrue(answer['result']['expired'])
        self.assertEqual(answer['result']['desired'], control.desk_profile(self.now))
        self.assertNotEqual(answer['result']['observed'], answer['result']['desired'])
        self.assertTrue(self.engine.reconcile())
        self.assertEqual(self.status(r)['status'], 'aborted')

    def test_guard_event_crossed_in_final_status_or_generation_never_starts(self):
        for day, source in ((14, 'observed'), (15, 'generation')):
            with self.subTest(source=source):
                self.now = utc('2026-11-%dT13:59:59' % day)
                self.assertTrue(self.engine.reconcile())
                self.engine.guard(dict(self.low, io=60))
                self.p.calls.clear()
                original = getattr(self.engine, source)
                reads = [0]
                def crossing(*args):
                    value = original(*args)
                    reads[0] += 1
                    if reads[0] == 3:
                        self.now = utc('2026-11-%dT14:00:00' % day)
                    return value
                with patch.object(self.engine, source, side_effect=crossing), \
                     patch.object(control, 'pressure', return_value=self.low):
                    with self.assertRaises(ValueError):
                        self.engine.guard(self.low)
                self.assertFalse(any(c[0] == 'start' for c in self.p.calls))
                self.assertEqual(self.p.groups['hasito']['status'], 'Paused')
                self.assertTrue(self.engine.reconcile())

    def test_confirmation_event_crossed_in_final_daemon_identity(self):
        request = self.request()
        original = self.engine.generation
        reads = [0]
        def crossing(state):
            value = original(state)
            reads[0] += 1
            if reads[0] == 4:
                self.now = utc('2026-11-14T14:00:00')
            return value
        with patch.object(self.engine, 'generation', side_effect=crossing):
            answer = self.engine.handle(request)
        self.assertEqual(answer['status'], 'aborted')
        self.assertEqual(answer['error']['code'], 'expired')
        self.assertIsNone(answer['result']['undo_of'])
        self.assertEqual(answer['result']['observed'], control.desk_profile(self.now))
        self.assertEqual(answer['result']['observed'], answer['result']['desired'])

    def test_reconcile_event_crossed_in_final_identity_without_receipts(self):
        original = self.engine.generation
        reads = [0]
        def crossing(state):
            value = original(state)
            reads[0] += 1
            if reads[0] == 2:
                self.now = utc('2026-11-14T14:00:00')
            return value
        with patch.object(self.engine, 'generation', side_effect=crossing):
            self.assertTrue(self.engine.reconcile())
        self.assertEqual(self.engine.view()['slots'], control.desk_profile(self.now))
        self.assertEqual(self.state()['schedule_event'], control.schedule_at(self.now)[0])

    def test_confirmation_event_crossed_in_final_receipt_read(self):
        request = self.pending()
        original = self.engine.connect
        @contextmanager
        def connect(write=False):
            with original(write) as db:
                class Proxy:
                    def __getattr__(proxy, name):
                        return getattr(db, name)
                    def execute(proxy, sql, *args):
                        cursor = db.execute(sql, *args)
                        if sql == 'SELECT action_id,body FROM host_actions':
                            class Rows:
                                def fetchall(inner):
                                    rows = cursor.fetchall()
                                    self.now = utc('2026-11-14T14:00:00')
                                    return rows
                            return Rows()
                        return cursor
                yield Proxy()
        with patch.object(self.engine, 'connect', side_effect=connect):
            self.assertTrue(self.engine.reconcile())
        answer = self.status(request)
        self.assertEqual(answer['status'], 'aborted')
        self.assertIsNone(answer['result']['undo_of'])
        self.assertEqual(answer['result']['observed'], control.desk_profile(self.now))

    def test_manual_resume_event_crossed_in_final_status_or_generation(self):
        for day, source in ((14, 'observed'), (15, 'generation')):
            with self.subTest(source=source):
                self.now = utc('2026-11-%dT13:59:59' % day)
                self.assertTrue(self.engine.reconcile())
                self.engine.manual('hasito')
                self.p.calls.clear()
                original = getattr(self.engine, source)
                def crossing(*args):
                    value = original(*args)
                    # The event crosses right after manual's own final read.
                    caller = sys._getframe(1)
                    while caller.f_code.co_filename.endswith('mock.py'):
                        caller = caller.f_back
                    if caller.f_code.co_name == 'manual':
                        self.now = utc('2026-11-%dT14:00:00' % day)
                    return value
                with patch.object(self.engine, source, side_effect=crossing), \
                     patch.object(control, 'pressure', return_value=self.low):
                    with self.assertRaises(ValueError):
                        self.engine.manual('hasito', resume=True)
                self.assertFalse(any(c[0] == 'start' for c in self.p.calls))
                self.assertEqual(self.p.groups['hasito']['status'], 'Paused')
                self.assertIn('manual', self.state()['reasons']['hasito'])
                self.assertTrue(self.engine.reconcile())

    def test_night_event_with_foreign_work_preserves_pressure_reopening(self):
        # Match the review: day at 04:58Z, high pressure, default queued, 05:00Z.
        self.now = utc('2026-11-15T04:58:00')
        self.assertTrue(self.engine.reconcile())
        high = dict(self.low, io=90, cpu=90, memory=1024**3)
        self.engine.guard(high)
        tasks = {'7': dict(group='default', status={'Queued': {}})}
        with self.foreign_status(tasks):
            self.now = utc('2026-11-15T05:00:30')
            self.assertTrue(self.engine.reconcile())
            self.assertEqual(self.engine.view()['slots'], control.desk_profile(self.now))
            with patch.object(control, 'pressure', return_value=self.low):
                self.engine.guard(self.low)
            self.assertTrue(all(self.p.groups[g]['status'] == 'Running' for g in control.GROUP_LIMITS))
            self.engine.guard(high)
            self.assertEqual(list(self.engine.directory.glob('pause-*.manual')), [])
            for g in control.GROUP_LIMITS:
                self.assertEqual(self.state()['reasons'][g], ['pressure'])
            with patch.object(control, 'pressure', return_value=self.low):
                self.engine.guard(self.low)
            self.assertTrue(all(self.p.groups[g]['status'] == 'Running' for g in control.GROUP_LIMITS))
            self.assertEqual(self.engine.view()['foreign_work'], 1)
        self.assertEqual(self.p.groups['default']['status'], 'Paused')

    def test_guard_crossing_event_in_fresh_sample_reconciles_before_opening(self):
        self.engine.guard(dict(self.low, io=60))
        self.p.calls.clear()
        def sample(*a, **k):
            self.now = utc('2026-11-14T14:00:00')
            return self.low
        original = self.p.call
        def call(*args):
            if args[0] == 'start':
                self.assertEqual({g:self.p.groups[g]['parallel_tasks'] for g in control.GROUP_LIMITS},
                                 control.desk_profile(self.now))
            return original(*args)
        with patch.object(control, 'pressure', side_effect=sample), patch.object(self.p, 'call', side_effect=call):
            with self.assertRaises(ValueError):
                self.engine.guard(self.low)
            self.assertFalse(any(c[0] == 'start' for c in self.p.calls))
            self.engine.guard(self.low)
        self.assertEqual(self.engine.view()['slots'], control.desk_profile(self.now))

    def test_ensayo_valid_plan_has_expiry_but_no_receipts_or_effects(self):
        before=self.fingerprint()
        answer=self.engine.handle(self.request(dry_run=True))
        self.assertEqual(answer['status'],'dry_run')
        self.assertEqual(answer['result']['expires_at'],utc('2026-11-14T14:00:00'))
        self.assertEqual(answer['result']['rpc'],[['parallel','6','-g','jale']])
        self.assertEqual(changed_files(before, self.fingerprint()), []);self.assertEqual(self.p.calls,[])

    def pending(self):
        r = self.request()
        # A crash after the single atomic desire/intent commit, before first RPC.
        with patch.object(self.engine,'reconcile',side_effect=OSError('crash before RPC')):
            self.assertEqual(self.engine.handle(r)['status'],'unknown')
        return r

    def test_failure_after_every_rpc_is_visible_and_recovery_completes(self):
        target=dict(control.desk_profile(self.now),jale=6,otros=3)
        # Reconciliation moves two components, reduction first, verified after each.
        for boundary in range(1,7):
            self.set_desire(target)
            self.p.groups['jale']['parallel_tasks']=7;self.p.groups['otros']['parallel_tasks']=2
            self.p.calls.clear();self.p.rpc=0;self.p.fail_at=boundary
            self.assertFalse(self.engine.reconcile(),boundary)
            self.p.fail_at=None
            if any(c[1]=='otros' for c in self.p.calls):self.assertEqual(self.p.groups['jale']['parallel_tasks'],6)
            self.assertTrue(self.engine.reconcile())
            self.assertEqual(self.engine.view()['slots'],target)
        # Accepted intent is reported as partial/unknown and read without retry.
        for boundary in range(1,5):
            with self.engine.connect(write=True) as db:db.execute('DELETE FROM host_actions')
            self.set_desire(control.desk_profile(self.now))
            for g,n in control.desk_profile(self.now).items():self.p.groups[g]['parallel_tasks']=n
            r=self.pending();self.p.rpc=0;self.p.fail_at=boundary;self.p.calls.clear()
            self.assertFalse(self.engine.reconcile())
            self.p.fail_at=None
            answer=self.status(r)
            self.assertIn(answer['status'],('partial','unknown'))
            self.assertEqual(answer['result']['desired']['jale'],6)
            calls=copy.deepcopy(self.p.calls)
            self.assertEqual(self.engine.handle(r)['status'],answer['status'])
            self.assertEqual(self.p.calls,calls)
            self.assertTrue(self.engine.reconcile());self.assertEqual(self.status(r)['status'],'applied')

    def test_expiration_supersedes_pending_intent_and_never_undoes_new_table(self):
        r=self.pending();self.now=utc('2026-11-14T14:00:00')
        self.assertTrue(self.engine.reconcile())
        answer=self.status(r)
        self.assertEqual(answer['status'],'aborted')
        self.assertEqual(answer['error']['code'],'expired')
        self.assertIsNone(answer['result']['undo_of'])
        self.assertEqual(answer['result']['desired'],control.desk_profile(self.now))

    def test_event_crossed_during_rpc_converges_without_finishing_expired_target(self):
        r=self.pending()
        parallel=self.p.parallel
        def crossing(g,n):
            parallel(g,n);self.now=utc('2026-11-14T14:00:00')
        with patch.object(self.p,'parallel',side_effect=crossing):self.assertTrue(self.engine.reconcile())
        self.assertEqual(self.engine.view()['slots'],control.desk_profile(self.now))
        self.assertEqual(self.status(r)['status'],'aborted')

    def test_id_digest_unknown_queries_and_host_binding(self):
        r=self.request();one=self.engine.handle(r);calls=copy.deepcopy(self.p.calls)
        self.assertEqual(self.engine.handle(r),one)
        changed=copy.deepcopy(r);changed['change']['group']='otros'
        self.assertEqual(self.engine.handle(changed)['error']['code'],'idempotency_conflict')
        missing=self.engine.handle(dict(schema=control.HOST_SCHEMA,op='status',action_id='missing-000001'))
        self.assertEqual(missing['status'],'unknown');self.assertEqual(self.p.calls,calls)
        import hashlib
        self.assertEqual(self.status(r)['receipt_digest'],hashlib.sha256(control.canonical(dict(host='desk',request=r)).encode()).hexdigest())

    def test_lost_parallel_reconciles_without_opening_reserved_foreign_or_ambiguous(self):
        self.engine.handle(self.request());self.gen='linux-proc-v1:2222222222222222222222222222222222222222222222222222222222222222'
        self.p.groups['jale'].update(parallel_tasks=7,status='Paused');self.p.calls.clear()
        self.engine.guard(self.low)
        self.assertEqual(self.p.groups['jale']['parallel_tasks'],6)
        reasons=self.engine.view()['pause_reasons']
        for g in ('jale','default','ajeno'):self.assertIn('ambiguous',reasons[g])
        self.assertFalse(any(c[0]=='start' for c in self.p.calls))

    def test_pressure_guard_rapidas_ladder_manual_pause_and_external_start(self):
        high=dict(self.low,io=60)
        self.engine.guard(high)
        self.assertEqual(self.p.groups['rapidas']['status'],'Running')
        self.assertEqual(self.p.groups['jale']['status'],'Paused')
        self.engine.manual('otros')
        self.p.groups['jale']['status']='Running'
        self.engine.guard(dict(self.low,io=30,memory=5*1024**3))
        self.assertNotIn('pressure',self.state()['reasons']['jale'])
        self.p.groups['jale']['status']='Paused'
        with patch.object(control,'pressure',return_value=self.low):self.engine.guard(self.low)
        for g in ('jale','otros','default','ajeno'):self.assertEqual(self.p.groups[g]['status'],'Paused')
        self.engine.guard(dict(self.low,memory=3*1024**3))
        self.assertEqual(self.p.groups['rapidas']['status'],'Paused')
        with patch.object(control,'pressure',return_value=high):self.engine.guard(high)
        self.assertEqual(self.p.groups['rapidas']['status'],'Running')
        self.assertEqual(self.p.groups['hasito']['status'],'Paused')

    def test_unreadable_authority_never_applies_table_or_opens_but_pressure_still_protects(self):
        self.engine.path.write_bytes(b'not sqlite');before=self.engine.path.read_bytes()
        with patch.object(control,'pressure',return_value=self.low):
            with self.assertRaises(sqlite3.Error):control.guard(self.p,None,self.engine.directory,host='desk')
        self.assertEqual(self.p.calls,[])
        with self.assertRaises(sqlite3.Error):self.engine.guard(dict(self.low,memory=3*1024**3))
        self.assertFalse(any(c[0] in ('parallel','start') for c in self.p.calls))
        self.assertTrue(all(self.p.groups[g]['status']=='Paused' for g in control.GROUP_LIMITS))
        self.assertEqual(self.engine.path.read_bytes(),before)
        with patch.object(control,'STATE_DIR',self.engine.directory), patch.object(control,'Pueue',return_value=self.p), \
             patch.object(control.sys,'argv',['control.py','aplicar']):
            with self.assertRaises(sqlite3.Error):control.main()
        self.assertFalse(any(c[0]=='parallel' for c in self.p.calls))

    def test_authority_with_overflowing_number_still_closes_under_pressure(self):
        state=self.state();state['at']=10**400
        with self.engine.connect(write=True) as db:self.engine.store(db,state)
        self.p.calls.clear();high=dict(self.low,io=90,memory=1024**3)
        with control.locked(self.engine.directory),patch.object(control,'pressure',return_value=high):
            with self.assertRaises(OverflowError):self.engine.guard(high)
        self.assertEqual([list(c) for c in self.p.calls],[['pause','--wait','-g',g] for g in control.GROUP_LIMITS])
        self.assertTrue(all(self.p.groups[g]['status']=='Paused' for g in control.GROUP_LIMITS))

    def test_timer_entry_preserves_live_override_then_expires(self):
        self.engine.handle(self.request());self.p.calls.clear()
        def run_timer():
            with patch.object(control,'STATE_DIR',self.engine.directory),patch.object(control,'Pueue',return_value=self.p), \
                 patch.object(control,'MiniControl',return_value=self.engine),patch.object(control.sys,'argv',['control.py','aplicar']):
                control.main()
        run_timer();self.assertEqual(self.p.calls,[])
        self.now=utc('2026-11-14T14:00:00');run_timer()
        self.assertEqual(self.engine.view()['slots'],control.desk_profile(self.now))
        self.assertEqual(self.state()['overrides'],{})
        self.assertIsNone(getattr(self.p,'timeout',None))

    def test_maintenance_blocks_mutation_recovery_and_opening(self):
        self.engine.handle(self.request());self.p.groups['jale']['parallel_tasks']=7;self.p.calls.clear()
        (self.engine.directory/'maintenance').touch()
        self.assertFalse(self.engine.reconcile())
        self.assertEqual(self.engine.handle(self.request('maintenance-01'))['error']['code'],'disabled')
        with self.assertRaises(ValueError):self.engine.guard(self.low)
        self.assertEqual(self.p.calls,[])

    def test_import_ownership_separate_from_six_group_profile(self):
        other=self.root/'migration';other.mkdir()
        (other/'guardia-state.json').write_text(json.dumps(['default','pg5434','jale']))
        self.p.groups['jale']['status']='Paused'
        engine=control.MiniControl(self.p,other,clock=lambda:self.now,generation=lambda:self.gen,host='desk')
        with control.locked(other):engine.adopt()
        reasons=engine.view()['pause_reasons']
        self.assertEqual(reasons['default'],['manual']);self.assertEqual(reasons['pg5434'],['manual'])
        self.assertEqual(reasons['jale'],['pressure'])

    def test_atomic_desire_and_intent_rollback_and_unverified_reduction(self):
        before=self.state()
        with self.engine.connect(write=True) as db:
            db.execute("CREATE TRIGGER refuse BEFORE INSERT ON host_actions BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        self.assertEqual(self.engine.handle(self.request())['status'],'unknown')
        self.assertEqual(self.state(),before);self.assertEqual(self.p.calls,[])
        with self.engine.connect(write=True) as db:db.execute('DROP TRIGGER refuse')
        target=dict(before['desired'],jale=6,otros=3);self.set_desire(target)
        with patch.object(self.p,'parallel',side_effect=lambda g,n:self.p.calls.append(('ignored',g,n))):
            self.assertFalse(self.engine.reconcile())
        self.assertEqual(self.p.calls,[('ignored','jale',6)])
        self.assertEqual(self.p.groups['otros']['parallel_tasks'],2)

    def test_panel_lock_wait_is_terminal_and_guard_rpc_timeout_remains_unbounded(self):
        self.p.calls.clear()
        request=self.request()
        output=io.StringIO()
        with control.locked(self.engine.directory),patch.object(control,'STATE_DIR',self.engine.directory), \
             patch.object(control,'Pueue',return_value=self.p),patch.object(control,'RACK_LOCK_SECONDS',.03), \
             patch.object(control.sys,'argv',['control.py','rack','--host','desk']), \
             patch.object(control.sys,'stdin',io.TextIOWrapper(io.BytesIO(json.dumps(request).encode()))),redirect_stdout(output):
            control.main()
        answer=json.loads(output.getvalue())
        self.assertEqual((answer['status'],answer['error']['code'],answer.get('effects')),('rejected','lock_busy',False))
        self.assertEqual(self.p.calls,[])
        self.assertEqual(self.p.timeout,3)
        # A new Pueue instance for timer/guard has no panel deadline.
        self.assertIsNone(control.Pueue('/tmp/never-execute').timeout)


class DeskInstallTests(DeskFixture):
    def systemctl(self, home, commands, *args):
        commands.append(args)
        values=dict(MainPID='321',ActiveState='active',NeedDaemonReload='no',Restart='always',KillMode='mixed',OOMPolicy='continue',StartLimitIntervalUSec='0',
                    ExecStartPre='/usr/bin/python3 -I '+str(home/'.local/lib/pao-pueue-desk/control.py')+' preparar --host desk')
        text=''.join(k+'='+v+'\n' for k,v in values.items()) if args[2]=='show' else ''
        return subprocess.CompletedProcess(args,0,text,'')

    def setUp(self):
        super().setUp()
        # Installation tests inject the new read-only binary/config/queue gate.
        # test_startup.ConfigurationTests exercises the real parser separately.
        self.enter_context(patch.object(control, 'validate_configuration', return_value=dict(data=self.root, pid_file=self.root/'pid')))
        self.enter_context(patch.object(control, 'read_queue', return_value=dict(tasks={},groups={})))

    def enter_context(self, manager):
        value = manager.start(); self.addCleanup(manager.stop); return value

    def migration_fixture(self):
        spec = importlib.util.spec_from_file_location('install_desk_r1', Path(control.__file__).with_name('install-desk.py'))
        installer = importlib.util.module_from_spec(spec); spec.loader.exec_module(installer)
        home = self.root/'table-home'
        library = home/'.local/lib/pao-pueue-desk'; library.mkdir(parents=True)
        target = library/'control.py'; target.write_text('# installed old controller\n')
        directory = home/'.local/state/pao/pueue-desk'
        self.now = utc('2026-11-15T16:00:00')
        for g, n in control.desk_profile(self.now).items():
            self.p.groups[g]['parallel_tasks'] = n
        engine = control.MiniControl(self.p, directory, clock=lambda:self.now, generation=lambda:self.gen, host='desk')
        with control.locked(directory):
            engine.adopt(self.root/'pid')
        v = engine.view()
        request = dict(schema=control.HOST_SCHEMA, op='slots', action_id='migration-old-001', dry_run=False,
                       expected={k:v[k] for k in ('revision','policy_hash','generation','mode','slots')},
                       change=dict(group='otros', delta=-1))
        self.assertEqual(engine.handle(request)['status'], 'applied')
        source = Path(control.__file__).read_text().replace('"jale": {"dia": 5, "noche": 7}',
                                                            '"jale": {"dia": 4, "noche": 7}').replace(
                            '"otros": {"dia": 2, "noche": 2}', '"otros": {"dia": 3, "noche": 2}')
        candidate = self.root/'candidate'; candidate.mkdir()
        (candidate/'control.py').write_text(source)
        import shutil
        shutil.copytree(Path(control.__file__).parent/'units', candidate/'units')
        spec = importlib.util.spec_from_file_location('candidate_r1', candidate/'control.py')
        new = importlib.util.module_from_spec(spec); spec.loader.exec_module(new)
        Original = new.MiniControl
        factory = lambda p,d,**kw: Original(p,d,clock=lambda:self.now,generation=lambda:self.gen,**kw)
        commands = []
        # addCleanup works on both supported interpreters.
        for manager in (patch.object(new, 'validate_configuration', return_value=dict(data=self.root, pid_file=self.root/'pid')),
                        patch.object(new, 'read_queue', return_value=dict(tasks={},groups={})),
                        patch.object(new, 'Pueue', return_value=self.p),
                        patch.object(new, 'MiniControl', side_effect=factory),
                        patch.object(new, 'desk_generation', return_value=self.gen),
                        patch.object(installer, 'SOURCE', candidate),
                        patch.object(installer, 'run', side_effect=lambda *a:self.systemctl(home,commands,*a))):
            manager.start(); self.addCleanup(manager.stop)
        return installer, new, home, engine, factory(self.p, directory, host='desk'), target, commands

    def test_table_migration_seco_and_install_retire_overrides_atomically(self):
        installer, new, home, old, engine, target, commands = self.migration_fixture()
        with old.connect() as db:
            before = old.state(db)
            receipt = db.execute('SELECT * FROM host_actions').fetchall()
        self.p.calls.clear()
        with self.assertRaises(ValueError):
            engine.view()  # runtime must keep failing closed on the old hash
        fingerprint = self.fingerprint()
        error = None
        with redirect_stdout(io.StringIO()) as out:
            try:
                installer.install(home, self.root/'pid', new, dry_run=True)
            except (ValueError, TypeError) as caught:
                error = caught
        self.assertIsNone(error, 'La tabla nueva debe tener preflight de migración: ' + str(error))
        self.assertIn('migración de tabla', out.getvalue())
        self.assertIn(new.DESK_POLICY_HASH, out.getvalue())
        self.assertEqual(changed_files(fingerprint, self.fingerprint()), [])
        self.assertEqual(self.p.calls, [])
        stores = []
        store = engine.__class__.store
        def inspect_store(instance, db, state):
            if state['policy_hash'] == new.DESK_POLICY_HASH:
                stores.append(copy.deepcopy(state))
                self.assertEqual(state['overrides'], {})
                self.assertEqual(state['desired'], new.desk_profile(self.now))
                self.assertEqual(state['revision'], before['revision'] + 1)
            return store(instance, db, state)
        error = None
        with patch.object(engine.__class__, 'store', inspect_store), redirect_stdout(io.StringIO()):
            try:
                installer.install(home, self.root/'pid', new)
            except (ValueError, TypeError) as caught:
                error = caught
        self.assertIsNone(error, 'La migración debe terminar: ' + str(error))
        self.assertTrue(stores)
        self.assertEqual(engine.view()['slots'], new.desk_profile(self.now))
        self.assertEqual(self.p.calls, [('parallel','jale',4), ('parallel','otros',3)])
        with engine.connect() as db:
            self.assertEqual(db.execute('SELECT * FROM host_actions').fetchall(), receipt)
        self.assertEqual(target.read_bytes(), (installer.SOURCE/'control.py').read_bytes())
        self.assertFalse((engine.directory/'slots.disabled').exists())
        self.assertFalse(any('pueued.service' in c and c[2]!='show' for c in commands))

    def test_table_migration_rejects_open_receipts_and_corrupt_authority_without_effects(self):
        installer, new, home, old, engine, target, commands = self.migration_fixture()
        with old.connect() as db:
            valid = old.state(db)
        for invalid in ('applying','partial','unknown','revision','reasons','desired','event','override','pid','hash','missing'):
            with self.subTest(invalid=invalid):
                state = copy.deepcopy(valid)
                with old.connect(write=True) as db:
                    if invalid in ('applying','partial','unknown'):
                        db.execute('INSERT INTO host_actions VALUES (?,?,?)',
                                   ('open-000001','digest',json.dumps(dict(response=dict(status=invalid)))))
                    elif invalid == 'revision': state['revision'] = 0
                    elif invalid == 'reasons': state['reasons'] = {'jale':['alien']}
                    elif invalid == 'desired': state['desired']['jale'] = True
                    elif invalid == 'event': state['schedule_event'] += 1
                    elif invalid == 'override': state['overrides']['otros']['expires_at'] += 1
                    elif invalid == 'pid': state['pid_file'] = 'relative'
                    elif invalid == 'hash': state['policy_hash'] = 'broken'
                    if invalid == 'missing': db.execute('DELETE FROM policy')
                    else: old.store(db, state)
                try:
                    fingerprint = self.fingerprint()
                    self.p.calls.clear(); commands.clear()
                    for dry_run in (True,False):
                        with self.assertRaises((ValueError, TypeError, sqlite3.Error)), redirect_stdout(io.StringIO()):
                            installer.install(home, self.root/'pid', new, dry_run=dry_run)
                        self.assertEqual(changed_files(fingerprint, self.fingerprint()), [])
                        self.assertEqual(self.p.calls, [])
                        self.assertTrue(all(c[2] in ('is-active','show-environment','show') for c in commands))
                finally:
                    with old.connect(write=True) as db:
                        db.execute('INSERT OR REPLACE INTO policy VALUES (1,?)', (control.canonical(valid),))
                        db.execute('DELETE FROM host_actions WHERE action_id=?', ('open-000001',))

    def test_table_migration_preflight_waits_on_existing_control_lock(self):
        installer, new, home, old, engine, target, commands = self.migration_fixture()
        entering, finished = threading.Event(), threading.Event()
        original = installer.authority_lock
        @contextmanager
        def lock(directory):
            entering.set()
            with original(directory):
                yield
        errors = []
        def install():
            try:
                installer.install(home, self.root/'pid', new, dry_run=True)
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()
        with patch.object(installer, 'authority_lock', lock):
            with (engine.directory/'control.lock').open('rb') as held:
                fcntl.flock(held, fcntl.LOCK_EX)
                worker = threading.Thread(target=install)
                worker.start()
                try:
                    self.assertTrue(entering.wait(1))
                    self.assertFalse(finished.wait(.05), 'El preflight debe esperar el candado existente')
                finally:
                    fcntl.flock(held, fcntl.LOCK_UN)
                    worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(finished.is_set())

    def test_table_migration_transaction_failure_keeps_original_policy(self):
        installer, new, home, old, engine, target, commands = self.migration_fixture()
        with old.connect(write=True) as db:
            before = old.state(db)
            db.execute("CREATE TRIGGER refuse_policy BEFORE UPDATE ON policy WHEN instr(NEW.body, '\"overrides\":{}') > 0 BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        self.p.calls.clear()
        with self.assertRaises(sqlite3.Error), redirect_stdout(io.StringIO()):
            installer.install(home, self.root/'pid', new)
        with old.connect() as db:
            self.assertEqual(json.loads(db.execute('SELECT body FROM policy WHERE id=1').fetchone()[0]), before)
        self.assertEqual(self.p.calls, [])
        self.assertTrue((engine.directory/'slots.disabled').exists())

    def test_seco_runs_readonly_preflight_and_rejects_invalid_adoption(self):
        spec = importlib.util.spec_from_file_location('install_desk_seco', Path(control.__file__).with_name('install-desk.py'))
        installer = importlib.util.module_from_spec(spec); spec.loader.exec_module(installer)
        home = self.root/'seco-home'
        library = home/'.local/lib/pao-pueue-desk'; library.mkdir(parents=True)
        target = library/'control.py'; target.write_text('# old\n')
        directory = home/'.local/state/pao/pueue-desk'
        Original = control.MiniControl
        factory = lambda p,d,**kw: Original(p,d,clock=lambda:self.now,generation=lambda:self.gen,**kw)
        commands = []
        loader = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda m: None))
        def invoke():
            with patch.object(installer.importlib.util, 'spec_from_file_location', return_value=loader), \
                 patch.object(installer.importlib.util, 'module_from_spec', return_value=control), \
                 patch.object(installer.Path, 'home', return_value=home), \
                 patch.object(control.sys, 'argv',
                              ['install-desk.py', '--pid-file', str(self.root/'pid'), '--seco']):
                with redirect_stdout(io.StringIO()) as out:
                    installer.main()
                return out.getvalue()
        with patch.object(control, 'Pueue', return_value=self.p), patch.object(control, 'MiniControl', side_effect=factory), \
             patch.object(control, 'desk_generation', return_value=self.gen) as pid, \
             patch.object(installer, 'run', side_effect=lambda *a:self.systemctl(home,commands,*a)) as run:
            for invalid in ('table', 'pid', 'timer', 'corrupt', 'lost'):
                with self.subTest(invalid=invalid):
                    self.p.groups['jale']['parallel_tasks'] = 6 if invalid == 'table' else 7
                    pid.side_effect = ValueError('wrong pid') if invalid == 'pid' else None
                    run.side_effect = subprocess.CalledProcessError(3, ['fake-systemctl']) if invalid == 'timer' else lambda *a:self.systemctl(home,commands,*a)
                    if invalid in ('corrupt', 'lost'):
                        directory.mkdir(parents=True, exist_ok=True)
                        (directory/('control.sqlite3' if invalid == 'corrupt' else 'control.adopted')).write_bytes(b'invalid')
                    before = self.fingerprint()
                    with self.assertRaises((ValueError, sqlite3.Error, subprocess.SubprocessError)):
                        invoke()
                    self.assertEqual(changed_files(before, self.fingerprint()), [])
                    for f in directory.glob('*'):
                        f.unlink()
            self.p.groups['jale']['parallel_tasks'] = 7
            pid.side_effect = None; run.side_effect = lambda *a:self.systemctl(home,commands,*a)
            commands.clear()
            before = self.fingerprint()
            output = invoke()
            self.assertEqual([c for c in commands if c[2]=='is-active'], [('systemctl','--user','is-active',n) for n in installer.TIMERS])
            self.assertTrue(all(c[2] in ('is-active','show-environment','show') for c in commands))
            pid.assert_called_with(self.root/'pid')
            self.assertIn('Comprobado (lectura)', output)
            self.assertIn('pendiente:', output)
            self.assertEqual(changed_files(before, self.fingerprint()), [])
            self.assertEqual(self.p.calls, [])

    def test_installer_retires_old_worker_waiting_before_lock_before_adoption(self):
        spec = importlib.util.spec_from_file_location('install_desk_worker', Path(control.__file__).with_name('install-desk.py'))
        installer = importlib.util.module_from_spec(spec); spec.loader.exec_module(installer)
        home = self.root/'worker-home'; library = home/'.local/lib/pao-pueue-desk'; library.mkdir(parents=True)
        target = library/'control.py'; target.write_text('# old\n')
        directory = home/'.local/state/pao/pueue-desk'
        waiting, retired, release = threading.Event(), threading.Event(), threading.Event()
        effects = []
        def old_worker():
            waiting.set(); release.wait(3)
            if not retired.is_set():
                with control.locked(directory):
                    effects.append('old apply_mode')
        worker = threading.Thread(target=old_worker); worker.start()
        self.assertTrue(waiting.wait(1))
        Original = control.MiniControl
        factory = lambda p,d,**kw: Original(p,d,clock=lambda:self.now,generation=lambda:self.gen,**kw)
        def run(*argv):
            if argv[2] == 'stop':
                self.assertIn(argv[3:], (installer.TIMERS, installer.SERVICES))
                self.assertEqual(target.read_text(), '# old\n')
                self.assertFalse(control.has_authority(directory))
                self.assertTrue((directory/'slots.disabled').exists())
                if argv[3:] == installer.SERVICES:
                    retired.set(); release.set(); worker.join(2)
                    self.assertFalse(worker.is_alive())
            return self.systemctl(home, [], *argv)
        try:
            with patch.object(control, 'Pueue', return_value=self.p), patch.object(control, 'MiniControl', side_effect=factory), \
                 patch.object(control, 'desk_generation', return_value=self.gen), patch.object(installer, 'run', side_effect=run):
                installer.install(home, self.root/'pid', control)
        finally:
            release.set(); worker.join(3)
        self.assertTrue(retired.is_set())
        self.assertEqual(effects, [])
        self.assertTrue(control.has_authority(directory))

    def test_legacy_deploy_withdraw_cannot_discard_adopted_desire(self):
        scope=dict(control.__dict__,STATE_DIR=self.engine.directory,FILES={})
        exec(Path(control.__file__).with_name('remote.py').read_text(),scope)
        commands=[];before=self.engine.path.read_bytes()
        with patch.object(scope['subprocess'],'run',side_effect=lambda argv,**kw:commands.append(argv)), \
             patch.dict(scope,Pueue=lambda:self.p),redirect_stdout(io.StringIO()):
            scope['remote']('withdraw',True)
            self.assertEqual(commands,[])
            scope['remote']('withdraw',False)
        self.assertEqual(self.engine.path.read_bytes(),before)
        self.assertTrue((self.engine.directory/'maintenance').exists())
        self.assertEqual(commands[0][3:],['pueue-desk-mode.timer','pueue-desk-guard.timer'])
        self.assertEqual(commands[-1],['systemctl','--user','stop','pueued.service'])
        self.assertEqual(self.p.calls,[])

    def test_installer_migrates_under_lock_preserves_switch_receipts_and_restart_free(self):
        spec=importlib.util.spec_from_file_location('install_desk_test',Path(control.__file__).with_name('install-desk.py'))
        installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)
        home=self.root/'installed';library=home/'.local/lib/pao-pueue-desk';library.mkdir(parents=True)
        target=library/'control.py';target.write_text('# previous\n')
        Original=control.MiniControl
        factory=lambda p,d,**kw:Original(p,d,clock=lambda:self.now,generation=lambda:self.gen,**kw)
        commands=[]
        with patch.object(control,'Pueue',return_value=self.p),patch.object(control,'MiniControl',side_effect=factory), \
             patch.object(control,'desk_generation',return_value=self.gen),patch.object(installer,'run',side_effect=lambda *a:self.systemctl(home,commands,*a)):
            installer.install(home,self.root/'pid',control)
            directory=home/'.local/state/pao/pueue-desk'
            engine=factory(self.p,directory,host='desk')
            v=engine.view();r=dict(schema=control.HOST_SCHEMA,op='slots',action_id='installed-00001',dry_run=False,
                 expected={k:v[k] for k in ('revision','policy_hash','generation','mode','slots')},change=dict(group='jale',delta=-1))
            self.assertEqual(engine.handle(r)['status'],'applied')
            switch=directory/'slots.disabled';switch.symlink_to(self.root/'absent')
            installer.install(home,self.root/'pid',control)
            self.assertTrue(switch.is_symlink())
            self.assertEqual(engine.view()['desired']['jale'],6)
            self.assertEqual(engine.handle(dict(schema=control.HOST_SCHEMA,op='status',action_id=r['action_id']))['status'],'applied')
            before=target.read_bytes();engine.path.write_bytes(b'corrupt')
            commands.clear()
            with self.assertRaises(sqlite3.Error):installer.install(home,self.root/'pid',control)
            self.assertEqual([c for c in commands if c[2]=='is-active'],[('systemctl','--user','is-active',name) for name in installer.TIMERS])
            self.assertTrue(all(c[2] in ('is-active','show-environment','show') for c in commands))
            self.assertEqual(target.read_bytes(),before)
        self.assertFalse(any('pueued.service' in c and c[2]!='show' for c in commands))
        self.assertEqual((library/'control.pre-slots.py').read_text(),'# previous\n')

    def test_installer_preflight_mismatch_and_failure_keep_closed_recovery_path(self):
        spec=importlib.util.spec_from_file_location('install_desk_test',Path(control.__file__).with_name('install-desk.py'))
        installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)
        home=self.root/'installed';library=home/'.local/lib/pao-pueue-desk';library.mkdir(parents=True)
        target=library/'control.py';target.write_text('# previous\n')
        Original=control.MiniControl
        factory=lambda p,d,**kw:Original(p,d,clock=lambda:self.now,generation=lambda:self.gen,**kw)
        self.p.groups['jale']['parallel_tasks']=6
        with patch.object(control,'Pueue',return_value=self.p),patch.object(control,'MiniControl',side_effect=factory), \
             patch.object(control,'desk_generation',return_value=self.gen),patch.object(installer,'run',side_effect=lambda *a:self.systemctl(home,[],*a)) as run:
            with self.assertRaises(ValueError):installer.install(home,self.root/'pid',control)
            self.assertFalse((home/'.local/state').exists());self.assertEqual(target.read_text(),'# previous\n')
            self.p.groups['jale']['parallel_tasks']=7
            with patch.object(Original,'adopt',side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):installer.install(home,self.root/'pid',control)
            self.assertTrue((home/'.local/state/pao/pueue-desk/slots.disabled').exists())
            self.assertFalse(any(c.args[:3]==('systemctl','--user','start') for c in run.call_args_list))
            self.assertTrue((home/'.local/state/pao/pueue-desk/maintenance').exists())


if __name__=='__main__':unittest.main()
