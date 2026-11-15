"""SEN-6417 contracts: private SQLite, realistic explicit-start double, no host RPC."""
import copy
import json
import subprocess
from unittest.mock import patch
import unittest
from test_desk_slots import DeskFixture, control

BOOT = '12345678-1234-1234-1234-123456789abc'

class TrickleTests(DeskFixture):
    def setUp(self):
        super().setUp()
        self.now -= 3600  # leave schedule boundaries out of cooldown contracts
        self.engine.reconcile()
        self.ns = 100_000_000_000
        self.tasks = {}
        self.mid = dict(self.low, cpu=22)
        self.sample = self.mid
        self.rpc_error = False
        self.after = None
        self.before = None
        self.real_call = self.p.call
        self.real_status = self.p.status
        def status():
            row = self.real_status()
            row['tasks'] = copy.deepcopy(self.tasks)
            return row
        def call(*args):
            if args[0] == 'start' and len(args) == 2:
                self.p.calls.append(args)
                task = self.tasks[str(args[1])]
                task['status'] = {'Running':dict(enqueued_at=task['status']['Queued']['enqueued_at'],
                                                start=self.iso())}
                if self.after:self.after(task)
                if self.rpc_error:raise subprocess.CalledProcessError(1, ['double'], stderr='SECRET-SENTINEL')
            else:self.real_call(*args)
        self.p.status, self.p.call = status, call
        self.engine.boot_clock = lambda:(BOOT, self.ns)
        self.sensor = patch.object(control, 'pressure', side_effect=lambda p:self.sample)
        self.sensor.start(); self.addCleanup(self.sensor.stop)
        for g in ('jale','pesadas'):
            self.p.groups[g]['status'] = 'Paused'
        s=self.state(); s['reasons'].update(jale=['pressure'], pesadas=['pressure'])
        with self.engine.connect(write=True) as db:self.engine.store(db,s)
        self.add_task(1,'jale'); self.add_task(2,'pesadas',offset=1)
        self.p.calls.clear()

    def iso(self, offset=0):
        return control.datetime.fromtimestamp(self.now+offset,control.timezone.utc).isoformat()

    def add_task(self, ident, group, offset=0, status=None):
        self.tasks[str(ident)] = dict(id=ident,group=group,created_at=self.iso(-100+offset),
            dependencies=[],status=status or {'Queued':{'enqueued_at':self.iso(-90+offset)}},
            envs={'PRIVATE':'SECRET-SENTINEL'},command='SECRET-SENTINEL')

    def run_guard(self, action=None):
        with control.locked(self.engine.directory):
            try:self.engine.guard(action or self.mid)
            except (ValueError,OSError,subprocess.SubprocessError):pass

    def starts(self):return [c for c in self.p.calls if c[0]=='start' and len(c)==2]
    def rows(self):return [json.loads(x) for x in (self.engine.directory/'guardia.log').read_text().splitlines()]
    def causes(self):return [b['causa'] for r in self.rows() for b in r.get('bloqueos',[])]

    def test_band_positive_fifo_and_property_then_low(self):
        self.run_guard()
        self.assertEqual(self.starts(), [('start',1)])
        self.assertEqual(self.p.groups['jale']['status'],'Paused')
        self.ns += 10_000_000_000
        self.run_guard()
        self.assertEqual(self.state()['reasons']['jale'],['pressure'])
        self.assertEqual(len(self.starts()),1)
        self.sample=self.low; self.run_guard(self.low)
        self.assertEqual(self.p.groups['jale']['status'],'Running')

    def test_global_cooldown_instances_59999_60(self):
        self.run_guard()
        self.assertEqual(len(self.starts()),1)
        self.engine=control.MiniControl(self.p,self.engine.directory,clock=lambda:self.now,generation=lambda:self.gen,host='desk')
        self.engine.boot_clock=lambda:(BOOT,self.ns)
        self.ns+=59_999_000_000;self.run_guard()
        self.assertEqual(len(self.starts()),1)
        self.assertIn('cooldown',self.causes())
        self.ns+=1_000_000;self.run_guard()
        self.assertEqual(len(self.starts()),2)

    def test_quota_full_plus_other_free_counts_paused_locked(self):
        limit=self.p.groups['jale']['parallel_tasks']
        for i in range(limit):self.add_task(10+i,'jale',status={'Paused' if i%2 else 'Locked':{}})
        self.run_guard()
        self.assertEqual(self.starts(),[('start',2)])
        self.assertIn('quota',self.causes())

    def test_retention_fresh_sample(self):
        self.sample=dict(self.low,cpu=40);self.run_guard(self.low)
        self.assertIn('fresh_sample',self.causes())
        self.assertEqual(self.starts(),[])

    def test_retention_docker(self):
        s=self.state();s['startup_version']=1
        with self.engine.connect(write=True) as db:self.engine.store(db,s)
        with patch.object(control.subprocess,'run',side_effect=OSError('SECRET-SENTINEL')):self.run_guard()
        self.assertIn('docker',self.causes())
        self.assertEqual(self.starts(),[])

    def test_retention_generation(self):
        with patch.object(self.engine,'reconcile',return_value=True):
            self.gen='linux-proc-v1:'+'2'*64;self.run_guard()
        self.assertIn('generation',self.causes())

    def test_retention_desired(self):
        with patch.object(self.engine,'reconcile',return_value=True):
            self.p.groups['jale']['parallel_tasks']-=1;self.run_guard()
        self.assertIn('desired',self.causes())

    def test_retention_event(self):
        with patch.object(self.engine,'reconcile',return_value=True):
            self.now+=7200;self.run_guard()
        self.assertIn('event',self.causes())

    def test_retention_admission(self):
        with patch.object(self.engine,'reconcile',return_value=False):self.run_guard()
        self.assertIn('admission',self.causes())

    def test_dependencies_and_mixed_reasons(self):
        self.tasks['1']['dependencies']=[999]
        s=self.state();s['reasons']['pesadas']=['pressure','manual']
        with self.engine.connect(write=True) as db:self.engine.store(db,s)
        self.run_guard()
        self.assertEqual(self.starts(),[])
        self.assertIn('dependency',self.causes())
        self.assertIn('reason',self.causes())

    def test_lost_response_confirmed_only_by_observation(self):
        self.rpc_error=True;self.run_guard()
        self.assertEqual(self.starts(),[('start',1)])
        self.assertTrue(any(r['evento']=='admision_una' for r in self.rows()))
        self.assertNotIn('SECRET-SENTINEL',(self.engine.directory/'guardia.log').read_text())

    def test_slow_rpc_cooldown_starts_at_completion(self):
        self.after=lambda t:setattr(self,'ns',self.ns+5_000_000_000)
        self.run_guard();self.assertEqual(len(self.starts()),1)
        self.ns=160_000_000_000;self.run_guard();self.assertEqual(len(self.starts()),1)
        self.ns=165_000_000_000;self.run_guard();self.assertEqual(len(self.starts()),2)

    def test_reused_identity_unknown_does_not_expire(self):
        self.after=lambda t:t.update(created_at=self.iso(-999))
        self.run_guard();self.assertEqual(len(self.starts()),1)
        self.ns+=1000_000_000_000;self.run_guard()
        self.assertEqual(len(self.starts()),1)
        self.assertIn('admission_unknown',self.causes())

    def test_high_no_start_boundaries_and_memory_band(self):
        self.sample=dict(self.mid,cpu=31);self.run_guard(self.sample)
        self.assertEqual(self.starts(),[])
        self.assertIn('pressure_high',self.causes())
        for readings in (dict(self.low,cpu=15),dict(self.low,cpu=30),dict(self.low,memory=5*1024**3)):
            self.assertEqual(control.decide('desk',readings,control.PRESSURE_THRESHOLDS),'mantener')

    def test_reboot_and_civil_clock_do_not_reset_cooldown(self):
        self.run_guard();self.assertEqual(len(self.starts()),1)
        self.now-=60;self.ns+=59_999_000_000;self.run_guard();self.assertEqual(len(self.starts()),1)
        self.engine.boot_clock=lambda:('87654321-1234-1234-1234-123456789abc',self.ns)
        self.ns=59_999_000_000;self.run_guard();self.assertEqual(len(self.starts()),1)
        self.ns=60_000_000_000;self.run_guard();self.assertEqual(len(self.starts()),2)

    def test_reserved_crash_and_unknown_survive_reinstall_migration(self):
        real=self._save_method()
        def interrupted(value):
            real(value)
            if value['last_attempt']['outcome']=='reserved':raise OSError('crash')
        with patch.object(self.engine,'_save_admission',side_effect=interrupted):self.run_guard()
        self.assertEqual(self.starts(),[])
        self.ns+=1000_000_000_000
        with self.engine.connect(write=True) as db:control.admission_state(db,migrate=True)
        self.run_guard();self.assertEqual(self.starts(),[])
        self.assertIn('admission_unknown',self.causes())
        self.sample=self.low;self.run_guard(self.low)
        self.assertEqual(self.p.groups['jale']['status'],'Running')

    def _save_method(self):
        self.assertTrue(hasattr(self.engine,'_save_admission'),'Reserva durable requerida antes del efecto')
        return self.engine._save_admission

    def test_unknown_can_recover_only_same_executed_identity(self):
        original=self.p.status;calls=[0]
        def status():
            if self.starts():
                calls[0]+=1
                if calls[0]==1:raise OSError('response unavailable')
            return original()
        with patch.object(self.p,'status',side_effect=status):self.run_guard()
        self.assertEqual(len(self.starts()),1)
        self.ns-=1;self.run_guard()
        self.assertEqual(len(self.starts()),1)
        with self.engine.connect() as db:v=control.admission_state(db)
        self.assertEqual(v['last_attempt']['outcome'],'unknown')
        self.ns+=1000_000_000_000;self.run_guard()
        self.assertEqual(len(self.starts()),1)
        with self.engine.connect() as db:v=control.admission_state(db)
        self.assertEqual(v['last_attempt']['outcome'],'confirmed')
        self.assertEqual(v['last_attempt']['completed_ns'],self.ns)

    def test_final_gate_failure_consumes_permission_without_replacement(self):
        save=self._save_method()
        def change(value):
            save(value)
            if value['last_attempt']['outcome']=='reserved':self.p.groups['jale']['status']='Running'
        with patch.object(self.engine,'_save_admission',side_effect=change):self.run_guard()
        self.assertEqual(self.starts(),[])
        self.assertIn('task_changed',self.causes())
        self.run_guard();self.assertEqual(self.starts(),[])
        self.assertIn('cooldown',self.causes())

    def test_log_failure_still_closes_but_never_opens(self):
        def broken(*args,**kwargs):raise OSError('SECRET-SENTINEL')
        with patch.object(control,'log',side_effect=broken):
            self.run_guard()
            self.assertEqual(self.starts(),[])
            for row in self.p.groups.values():row['status']='Running'
            self.run_guard(dict(self.low,cpu=40))
        self.assertEqual(self.p.groups['jale']['status'],'Paused')

    def test_done_immediate_and_failed_without_effect(self):
        def finish(task):
            detail=task['status']['Running'];task['status']={'Done':dict(detail,end=self.iso(),result='Success')}
        self.after=finish;self.run_guard()
        self.assertTrue(any(r['evento']=='admision_una' for r in self.rows()))
        self.ns+=60_000_000_000
        def failed(*args):
            self.p.calls.append(args);raise OSError('SECRET-SENTINEL')
        with patch.object(self.p,'call',side_effect=failed):self.run_guard()
        self.assertEqual(len(self.starts()),2)
        self.assertIn('admission_unknown',self.causes())
        self.ns+=1000_000_000_000;self.run_guard();self.assertEqual(len(self.starts()),2)

    def test_freshness_budget_and_clock_rollback(self):
        with patch.object(control.time,'monotonic',side_effect=[0,11]):self.run_guard()
        self.assertEqual(self.starts(),[])
        self.assertIn('fresh_sample',self.causes())
        self.run_guard();self.assertEqual(len(self.starts()),1)
        self.ns-=1;self.run_guard();self.assertEqual(len(self.starts()),1)
        self.assertIn('clock',self.causes())
        for clock in ((BOOT,True),(BOOT,float('nan')),('bad',self.ns)):
            self.engine.boot_clock=lambda:clock
            self.run_guard();self.assertEqual(len(self.starts()),1)
            self.assertIn('clock',self.causes())

    def test_manual_tombstone_startup_and_pending_receipt(self):
        for filename,cause in (('maintenance','maintenance'),):
            (self.engine.directory/filename).touch();self.run_guard()
            self.assertEqual(self.starts(),[]);self.assertIn(cause,self.causes())
            (self.engine.directory/filename).unlink()
        original=self.state()
        s=copy.deepcopy(original);s.update(startup_version=1,startup=dict(phase='prepared',statuses={},groups={},holds={},interruptions={},source_hash='a'*64,target_hash='b'*64))
        with self.engine.connect(write=True) as db:self.engine.store(db,s)
        with patch.object(self.engine,'reconcile',return_value=True):self.run_guard()
        self.assertEqual(self.starts(),[]);self.assertIn('startup',self.causes())
        with self.engine.connect(write=True) as db:
            self.engine.store(db,original)
            db.execute('INSERT INTO host_actions VALUES (?,?,?)',('pending-fixture','fixture',json.dumps(dict(response=dict(status='unknown')))))
        with patch.object(self.engine,'reconcile',return_value=True):self.run_guard()
        self.assertEqual(self.starts(),[]);self.assertIn('admission',self.causes())
        with self.engine.connect(write=True) as db:db.execute('DELETE FROM host_actions')
        for g in ('jale','pesadas'):
            self.engine.fence_pause(g)
        self.run_guard();self.assertEqual(self.starts(),[]);self.assertIn('reason',self.causes())

    def test_nan_boolean_or_incoherent_permission_is_rejected(self):
        self.run_guard();self.assertEqual(len(self.starts()),1)
        with self.engine.connect() as db:original=control.admission_state(db)
        for key,value in (('reserved_ns',True),('completed_ns',float('nan')),('created_at','bad'),('outcome','free')):
            row=copy.deepcopy(original);row['last_attempt'][key]=value
            with self.subTest(key=key),self.assertRaises((ValueError,TypeError)):control.validate_admission(row)

    def test_numeric_id_tie_and_utc_fifo(self):
        self.tasks.clear();self.add_task(10,'jale');self.add_task(2,'pesadas')
        self.run_guard();self.assertEqual(self.starts(),[('start',2)])

    def test_low_and_rapidas_never_use_incremental_permission(self):
        self.tasks.clear();self.add_task(1,'rapidas')
        self.p.groups['rapidas']['status']='Paused'
        s=self.state();s['reasons']['rapidas']=['pressure']
        with self.engine.connect(write=True) as db:self.engine.store(db,s)
        self.run_guard();self.assertEqual(self.starts(),[])
        self.assertEqual(self.p.groups['rapidas']['status'],'Running')

    def test_running_task_keeps_pressure_on_next_pass_already_green(self):
        self.tasks['1']['status']={'Running':dict(enqueued_at=self.iso(-90),start=self.iso(-60))}
        self.tasks['2']['status']={'Running':dict(enqueued_at=self.iso(-90),start=self.iso(-60))}
        self.run_guard();self.assertEqual(self.state()['reasons']['jale'],['pressure'])
        self.sample=self.low;self.run_guard(self.low)
        self.assertEqual(self.p.groups['jale']['status'],'Running')

    def test_initial_sensor_failure_has_no_inferred_operation(self):
        with patch.object(control,'pressure',side_effect=OSError('SECRET-SENTINEL')):
            with self.assertRaises(OSError):self.engine.guard(None)
        self.assertEqual(self.rows()[-1]['bloqueos'],[
            dict(grupo=None,operacion='unknown',causa='fresh_sample')])
        self.assertEqual(self.starts(),[])
        self.assertNotIn('SECRET-SENTINEL',(self.engine.directory/'guardia.log').read_text())

    def test_final_unknown_state_finishes_not_sent(self):
        save=self._save_method()
        def change(value):
            save(value)
            if value['last_attempt']['outcome']=='reserved':
                self.add_task(99,'jale',status={'FutureState':{}})
        with patch.object(self.engine,'_save_admission',side_effect=change):self.run_guard()
        self.assertEqual(self.starts(),[])
        with self.engine.connect() as db:value=control.admission_state(db)
        self.assertEqual(value['last_attempt']['outcome'],'not_sent')
        self.assertIn('task_changed',self.causes())
        self.tasks.pop('99');self.run_guard()
        self.assertEqual(self.starts(),[])
        self.assertIn('cooldown',self.causes())

    def uncertain(self, executed=False):
        if not executed:
            def no_effect(*args):
                self.p.calls.append(args)
                raise OSError('SECRET-SENTINEL')
            with patch.object(self.p,'call',side_effect=no_effect):self.run_guard()
        else:
            with patch.object(self.engine,'_execution_confirmed',return_value=False):self.run_guard()
        with self.engine.connect() as db:return control.admission_state(db)

    def resolve_uncertain(self, no_effect=False):
        method=getattr(self.engine,'resolve_admission',None)
        self.assertTrue(callable(method),'Debe existir recuperación operativa segura')
        with control.locked(self.engine.directory):return method(no_effect=no_effect)

    def test_recovery_requires_maintenance_and_explicit_no_late_effect(self):
        original=self.uncertain()
        with self.assertRaises(ValueError):self.resolve_uncertain(no_effect=True)
        (self.engine.directory/'maintenance').touch()
        with self.assertRaises(ValueError):self.resolve_uncertain()
        with self.engine.connect() as db:self.assertEqual(control.admission_state(db),original)
        self.ns+=20_000_000_000
        self.assertEqual(self.resolve_uncertain(no_effect=True),'not_sent')
        with self.engine.connect() as db:value=control.admission_state(db)
        self.assertEqual(value['last_attempt']['completed_ns'],self.ns)
        self.assertIsNone(value['last_confirmed'])
        (self.engine.directory/'maintenance').unlink()
        self.ns+=59_999_000_000;self.run_guard();self.assertEqual(len(self.starts()),1)
        self.ns+=1_000_000;self.run_guard();self.assertEqual(len(self.starts()),2)

    def test_recovery_confirms_only_observed_execution_and_preserves_history(self):
        self.uncertain(executed=True)
        (self.engine.directory/'maintenance').touch();self.ns+=20_000_000_000
        self.assertEqual(self.resolve_uncertain(),'confirmed')
        with self.engine.connect() as db:value=control.admission_state(db)
        self.assertEqual(value['last_attempt']['completed_ns'],self.ns)
        self.assertEqual(value['last_confirmed'],dict(at=self.iso(),grupo='jale',id=1))
        with self.assertRaises(ValueError):self.resolve_uncertain(no_effect=True)

    def test_recovery_other_boot_requires_certification_without_evidence(self):
        original=self.uncertain();(self.engine.directory/'maintenance').touch()
        boot='87654321-1234-1234-1234-123456789abc'
        self.engine.boot_clock=lambda:(boot,self.ns)
        with self.assertRaises(ValueError):self.resolve_uncertain()
        with self.engine.connect() as db:self.assertEqual(control.admission_state(db),original)
        self.assertEqual(self.resolve_uncertain(no_effect=True),'not_sent')
        with self.engine.connect() as db:value=control.admission_state(db)
        self.assertEqual(value['last_attempt']['boot_id'],boot)
        self.assertEqual(value['last_attempt']['reserved_ns'],self.ns)

    def test_recovery_clock_failure_or_rollback_does_not_release(self):
        original=self.uncertain();(self.engine.directory/'maintenance').touch()
        for clock in (OSError('SECRET-SENTINEL'),(BOOT,self.ns-1)):
            with self.subTest(clock=type(clock).__name__):
                with patch.object(self.engine,'boot_clock',side_effect=clock if isinstance(clock,Exception) else lambda:clock):
                    with self.assertRaises((OSError,ValueError)):self.resolve_uncertain(no_effect=True)
                with self.engine.connect() as db:self.assertEqual(control.admission_state(db),original)
        self.ns+=20_000_000_000
        with patch.object(self.engine,'boot_clock',side_effect=[(BOOT,self.ns),(BOOT,self.ns-1)]):
            with self.assertRaises(ValueError):self.resolve_uncertain(no_effect=True)
        with self.engine.connect() as db:self.assertIn(control.admission_state(db)['last_attempt']['outcome'],('reserved','unknown'))

    def test_recovery_cli_runs_under_lock(self):
        import sys
        original=self.uncertain();(self.engine.directory/'maintenance').touch()
        argv=['control.py','resolver-admision','--sin-efecto']
        import io
        from contextlib import redirect_stdout
        output=io.StringIO()
        with patch.object(control,'STATE_DIR',self.engine.directory),patch.object(control,'MiniControl',return_value=self.engine),patch.object(control,'Pueue',return_value=self.p),patch.object(sys,'argv',argv),redirect_stdout(output):
            try:control.main()
            except SystemExit as error:self.fail('CLI resolver-admision debe aceptarse: '+str(error))
        self.assertEqual(json.loads(output.getvalue())['resultado'],'not_sent')

    def test_clock_unreadable_at_completion_keeps_uncertainty(self):
        self.after=lambda task:setattr(self,'ns',self.ns+20_000_000_000)
        def clock():
            if self.starts():raise OSError('clock unavailable')
            return BOOT,self.ns
        self.engine.boot_clock=clock;self.run_guard()
        with self.engine.connect() as db:value=control.admission_state(db)
        self.assertIn(value['last_attempt']['outcome'],('reserved','unknown'))
        self.assertIsNone(value['last_attempt']['completed_ns'])
        self.after=None;self.engine.boot_clock=lambda:(BOOT,self.ns)
        self.ns+=40_000_000_000;self.run_guard()
        self.assertEqual(len(self.starts()),1)
        with self.engine.connect() as db:value=control.admission_state(db)
        self.assertEqual(value['last_attempt']['completed_ns'],self.ns)

    def test_clock_backwards_at_completion_keeps_uncertainty(self):
        self.after=lambda task:setattr(self,'ns',self.ns-1)
        self.run_guard()
        with self.engine.connect() as db:value=control.admission_state(db)
        self.assertEqual(value['last_attempt']['outcome'],'unknown')
        self.assertIsNone(value['last_attempt']['completed_ns'])
        self.ns+=40_000_000_001;self.after=None;self.run_guard()
        self.assertEqual(len(self.starts()),1)

    def test_future_execution_is_not_evidence(self):
        self.uncertain(executed=True)
        with self.engine.connect() as db:value=control.admission_state(db)
        self.tasks['1']['status']['Running']['start']=self.iso(86400)
        self.assertFalse(self.engine._execution_confirmed(value['last_attempt'],self.state()))
        (self.engine.directory/'maintenance').touch()
        with self.assertRaises(ValueError):self.resolve_uncertain()

    def test_execution_before_reservation_is_not_evidence(self):
        self.uncertain(executed=True)
        with self.engine.connect() as db:value=control.admission_state(db)
        self.tasks['1']['status']['Running']['start']=self.iso(-1)
        self.assertFalse(self.engine._execution_confirmed(value['last_attempt'],self.state()))
        (self.engine.directory/'maintenance').touch()
        with self.assertRaises(ValueError):self.resolve_uncertain()

if __name__=='__main__':unittest.main()

