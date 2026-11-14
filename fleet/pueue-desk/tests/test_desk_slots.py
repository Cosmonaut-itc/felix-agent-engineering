"""Desk quota authority: fake time, Pueue, sensors and private /tmp filesystem."""
import copy
from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from test_slots import control, FakePueue


def utc(value):
    return datetime.fromisoformat(value + '+00:00').timestamp()


class DeskFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='u32-sol-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = utc('2026-11-14T13:59:59')
        self.gen = 'desk-daemon-1'
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
            self.assertNotIn('default', v['slots'])
            self.assertFalse(any(v['capabilities'][k] for k in ('mode','reorder','restart_queue')))
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
        result = self.engine.handle(undo)
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
        self.assertEqual(self.fingerprint(),before)
        self.assertEqual(self.engine.handle(stale)['status'],'conflict')
        self.assertEqual(self.p.calls,[])
        self.assertEqual(self.state()['overrides']['jale']['slots'],6)

    def test_ensayo_valid_plan_has_expiry_but_no_receipts_or_effects(self):
        before=self.fingerprint()
        answer=self.engine.handle(self.request(dry_run=True))
        self.assertEqual(answer['status'],'dry_run')
        self.assertEqual(answer['result']['expires_at'],utc('2026-11-14T14:00:00'))
        self.assertEqual(answer['result']['rpc'],[['parallel','6','-g','jale']])
        self.assertEqual(self.fingerprint(),before);self.assertEqual(self.p.calls,[])

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
        self.engine.handle(self.request());self.gen='desk-daemon-2'
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
    def test_legacy_deploy_withdraw_cannot_discard_adopted_desire(self):
        scope=dict(control.__dict__,STATE_DIR=self.engine.directory,FILES={})
        exec(Path(control.__file__).with_name('remote.py').read_text(),scope)
        with patch.object(scope['subprocess'],'run',side_effect=AssertionError('No external commands')), \
             patch.dict(scope,Pueue=lambda:self.p):
            for action in ('install','withdraw'):
                for dry in (True,False):
                    with self.assertRaisesRegex(RuntimeError,'Autoridad durable'):scope['remote'](action,dry)
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
             patch.object(control,'mini_generation',return_value=self.gen),patch.object(installer,'run',side_effect=lambda *a:commands.append(a)):
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
            self.assertEqual(commands,[('systemctl','--user','is-active',name) for name in installer.TIMERS])
            self.assertEqual(target.read_bytes(),before)
        self.assertFalse(any('pueued.service' in c for c in commands))
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
             patch.object(control,'mini_generation',return_value=self.gen),patch.object(installer,'run') as run:
            with self.assertRaises(ValueError):installer.install(home,self.root/'pid',control)
            self.assertFalse((home/'.local/state').exists());self.assertEqual(target.read_text(),'# previous\n')
            self.p.groups['jale']['parallel_tasks']=7
            with patch.object(Original,'adopt',side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):installer.install(home,self.root/'pid',control)
            self.assertTrue((home/'.local/state/pao/pueue-desk/slots.disabled').exists())
            self.assertEqual(run.call_args.args,('systemctl','--user','start',*installer.TIMERS))


if __name__=='__main__':unittest.main()
