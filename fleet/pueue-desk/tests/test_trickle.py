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

if __name__=='__main__':unittest.main()
