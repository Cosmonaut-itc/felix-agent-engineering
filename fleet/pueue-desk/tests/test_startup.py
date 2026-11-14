"""U3.4: private filesystem, fake clock/Pueue/processes/config/systemd only."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from test_desk_slots import DeskFixture, utc
from test_slots import control

STAMP = '2026-11-14T12:00:00+00:00'


def task(ident, status, group='jale'):
    return dict(id=ident, created_at=STAMP, command='true', original_command='true', path='/synthetic',
                envs={}, group=group, dependencies=[], priority=0, label=None, status=status)


def queued(): return {'Queued': dict(enqueued_at=STAMP)}
def running(): return {'Running': dict(enqueued_at=STAMP, start=STAMP)}
def done(result='Success'): return {'Done': dict(enqueued_at=STAMP, start=STAMP, end=STAMP, result=result)}


class StartupTests(DeskFixture):
    def setUp(self):
        super().setUp()
        self.data = self.root/'data'; self.data.mkdir()
        self.path = self.data/'state.json'
        self.pid = self.root/'pid'; self.pid.write_text('999999\n')
        self.config = dict(data=self.data, pid_file=self.pid)
        self.queue = dict(groups=copy.deepcopy(self.p.groups), tasks={'0':task(0,queued())})
        self.write()
        state = self.state(); state['startup_version'] = 1
        with self.engine.connect(write=True) as db: self.engine.store(db,state)
        self.processes = []
        def fake_run(argv, **kwargs):
            self.processes.append(argv)
            if argv[:1] == ['/bin/ps']:
                return subprocess.CompletedProcess(argv,1,'','')
            if argv == ['/usr/bin/docker','info']:
                return subprocess.CompletedProcess(argv,0,'','')
            raise AssertionError('No real binaries: '+str(argv))
        self.mockrun = patch.object(control.subprocess,'run',side_effect=fake_run)
        self.mockrun.start(); self.addCleanup(self.mockrun.stop)

    def write(self): self.path.write_text(json.dumps(self.queue))

    def prepare(self, **kwargs):
        with control.locked(self.engine.directory), patch.object(self.p,'status',side_effect=subprocess.CalledProcessError(1,['fake'])):
            return control.prepare_start(self.engine,self.path,self.config,**kwargs)

    def boot(self):
        image = json.loads(self.path.read_text())
        self.p.groups = copy.deepcopy(image['groups'])
        self.gen = 'desk-daemon-2'
        return image

    def test_s_w1_offsets_calendar_and_nested_reject_without_writes_or_start(self):
        dates = ['2026-11-12T08:00:00'+o for o in ('+00:99','-00:99','+24:00')]
        dates += ['2026-02-30T12:00:00Z','2026-11-12T24:00:00Z','2026-11-12T08:60:00Z',
                  '2026-11-12T08:00:60Z','0000-11-12T08:00:00Z']
        for stamp in dates:
            for nested in (False,True):
                with self.subTest(stamp=stamp,nested=nested):
                    self.queue['tasks']['0'] = task(0, {'Locked':dict(previous_status={'Queued':dict(enqueued_at=stamp)})} if nested else queued())
                    if not nested:self.queue['tasks']['0']['created_at']=stamp
                    self.write(); before=self.fingerprint()
                    with self.assertRaises(ValueError): self.prepare()
                    self.assertEqual(self.fingerprint(),before)
                    self.assertEqual(self.p.calls,[])
                    self.assertFalse(any('docker' in str(c) for c in self.processes))
        for stamp in ('2024-02-29T00:00:00.123456789-23:59','2026-11-12T08:00:00Z'):
            control.validate_timestamp(stamp)

    def test_strict_queue_and_missing_initialized_state_preserve_every_file(self):
        for invalid in ('missing','json','duplicate','gzip','reset','identity','environment','enum'):
            with self.subTest(invalid=invalid):
                self.queue['tasks']['0'] = task(0,queued());self.queue['groups']['jale']['status']='Running';self.write()
                if invalid=='missing':self.path.unlink()
                if invalid=='json':self.path.write_text('{')
                if invalid=='duplicate':self.path.write_text('{"tasks":{},"tasks":{},"groups":{}}')
                if invalid=='gzip':self.path.with_suffix('.json.gz').write_bytes(b'opaque')
                if invalid=='reset':self.queue['groups']['jale']['status']='Reset';self.write()
                if invalid=='identity':self.queue['tasks']['0']['id']=2;self.write()
                if invalid=='environment':self.queue['tasks']['0']['envs']=[];self.write()
                if invalid=='enum':self.queue['tasks']['0']['status']={'Unknown':{}};self.write()
                before=self.fingerprint()
                with self.assertRaises((ValueError,OSError)):self.prepare()
                self.assertEqual(self.fingerprint(),before)
                if invalid=='gzip':self.path.with_suffix('.json.gz').unlink()

    def test_task_matrix_and_all_groups_closed_without_requeue_or_cancel_receipts(self):
        self.queue['groups']['suites']=dict(status='Running',parallel_tasks=3)
        self.queue['tasks']={
            '0':task(0,running()), '1':task(1,{'Paused':running()['Running']}),
            '2':task(2,queued()),'3':task(3,queued()),'4':task(4,{'Stashed':dict(enqueue_at=None)}),
            '5':task(5,{'Locked':dict(previous_status=queued())},'hasito'),
            '6':task(6,done()),'7':task(7,done({'Failed':9})),'8':task(8,done('Killed'))}
        self.queue['tasks']['2'].update(priority=17,dependencies=[6])
        (self.data/'task_logs').mkdir(); (self.data/'task_logs/3.log').symlink_to(self.root/'absent-log')
        self.write(); old=copy.deepcopy(self.queue)
        image=self.prepare()
        self.assertEqual({g['status'] for g in image['groups'].values()},{'Paused'})
        self.assertEqual(image['tasks']['2'],old['tasks']['2'])
        for ident in ('4','5','6','7','8'):self.assertEqual(image['tasks'][ident],old['tasks'][ident])
        for ident in ('0','1','3'):
            self.assertIn('Done',image['tasks'][ident]['status'])
            self.assertEqual(image['tasks'][ident]['status']['Done']['result'],'Killed')
        state=self.state()
        self.assertEqual(set(state['startup']['interruptions']),{'0','1','3'})
        self.assertIn('log previo',state['startup']['holds']['jale'])
        self.assertIn('Locked',state['startup']['holds']['hasito'])
        self.assertIn('suites',state['reasons']);self.assertNotIn('suites',state['desired'])
        with self.engine.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM host_actions').fetchone()[0],0)
        self.assertEqual(self.prepare(),image)
        self.assertEqual(self.state()['startup']['interruptions'],state['startup']['interruptions'])

    def test_crash_at_every_cross_file_boundary_and_repeat_recovery(self):
        self.queue['tasks']['0']=task(0,running());self.write()
        original=self.fingerprint()
        for crash in ('intent','queue','complete'):
            with self.subTest(crash=crash):
                for p in self.root.rglob('*'):
                    if p.is_file() and p.relative_to(self.root).as_posix() in original:p.write_bytes(original[p.relative_to(self.root).as_posix()])
                def boundary(phase):
                    if phase==crash:raise RuntimeError('simulated power loss')
                with self.assertRaises(RuntimeError):self.prepare(boundary=boundary)
                self.assertIn('startup',self.state())
                self.assertIn(self.state()['startup']['phase'],('intent','prepared'))
                # Crash a second time during intent recovery as well.
                with self.assertRaises(RuntimeError):self.prepare(boundary=lambda phase: (_ for _ in ()).throw(RuntimeError()) if phase=='intent' else None)
                first=self.prepare();second=self.prepare()
                self.assertEqual(first,second)
                self.assertEqual(json.loads(self.path.read_text()),first)
                self.assertEqual(first['tasks']['0']['status']['Done']['result'],'Killed')
                self.assertEqual(len(self.state()['startup']['interruptions']),1)
                self.assertEqual(self.state()['startup']['phase'],'prepared')

    def test_live_or_unknown_previous_pid_blocks_and_leaves_bytes_intact(self):
        for code in (0,2):
            with patch.object(control.subprocess,'run',return_value=subprocess.CompletedProcess([],code,'pueued','')):
                before=self.fingerprint()
                with self.assertRaises(ValueError):self.prepare()
                self.assertEqual(self.fingerprint(),before)
        before=self.fingerprint()
        with self.assertRaises(ValueError):control.prepare_start(self.engine,self.path,self.config)
        self.assertEqual(self.fingerprint(),before)

    def test_ensayo_branches_before_intent_expiry_pid_or_queue_write(self):
        before=self.fingerprint();image=self.prepare(dry_run=True)
        self.assertTrue(all(g['status']=='Paused' for g in image['groups'].values()))
        self.assertEqual(self.fingerprint(),before)
        self.assertEqual(self.p.calls,[])

    def test_event_crosses_prepare_and_expired_advance_adjustments_are_not_reinstated(self):
        state=self.state();state['overrides']['jale']=dict(slots=6,actor='Felix',action_id='old-slots-0001',requested_at=self.now,expires_at=utc('2026-11-14T14:00:00'))
        state['desired']['jale']=6
        with self.engine.connect(write=True) as db:self.engine.store(db,state)
        def boundary(phase):
            if phase=='queue':self.now=utc('2026-11-14T14:00:00')
        image=self.prepare(boundary=boundary)
        self.assertEqual({g:image['groups'][g]['parallel_tasks'] for g in control.GROUP_LIMITS},control.desk_profile(self.now))
        self.assertEqual(self.state()['overrides'],{})
        self.assertIsNone(self.state()['mode_override'])
        self.boot()
        with patch.object(control,'pressure',return_value=self.low):self.engine.guard(self.low)
        self.assertEqual(self.engine.view()['slots'],control.desk_profile(self.now))

    def test_guard_reconciles_before_ready_sensors_then_only_releases_owned_startup(self):
        self.engine.manual('otros')
        self.p.groups['otros']['status']='Paused'
        self.queue['groups']=copy.deepcopy(self.p.groups);self.write()
        self.prepare();self.boot()
        self.p.groups['jale']['parallel_tasks']=8
        with patch.object(control,'pressure',return_value=self.low),patch.object(self.engine,'admission_ready',return_value=False):
            self.engine.guard(self.low)
        self.assertEqual(self.p.groups['jale']['parallel_tasks'],7)
        self.assertFalse(any(c[0]=='start' for c in self.p.calls))
        with patch.object(control,'pressure',return_value=self.low):self.engine.guard(self.low)
        for g in control.GROUP_LIMITS:
            self.assertEqual(self.p.groups[g]['status'],'Paused' if g=='otros' else 'Running')
        for g in ('default','ajeno'):self.assertEqual(self.p.groups[g]['status'],'Paused')
        self.assertIn('manual',self.state()['reasons']['otros'])
        self.assertTrue(any(c==['/usr/bin/docker','info'] for c in self.processes))
        self.assertNotIn('jale',self.engine.view()['startup_holds'])

    def test_docker_failure_and_new_pressure_prevent_admission_and_explicit_resume(self):
        self.prepare();self.boot()
        with patch.object(control,'pressure',return_value=self.low),patch.object(control.subprocess,'run',side_effect=OSError('Docker absent')):
            self.engine.guard(self.low)
            with self.assertRaises(ValueError):self.engine.manual('jale',resume=True)
        self.assertFalse(any(c[0]=='start' for c in self.p.calls))
        with patch.object(control,'pressure',return_value=dict(self.low,io=90)):
            self.engine.guard(self.low)
        self.assertEqual(self.p.groups['jale']['status'],'Paused')

    def test_manual_ambiguous_resume_uses_same_admission_and_no_panel_button(self):
        self.queue['groups']['jale']['status']='Paused';self.write();self.prepare();self.boot()
        with patch.object(control,'pressure',return_value=self.low):
            self.engine.guard(self.low)
            self.assertEqual(self.p.groups['jale']['status'],'Paused')
            self.engine.manual('jale',resume=True)
        self.assertEqual(self.p.groups['jale']['status'],'Running')
        self.assertEqual(self.state()['reasons']['jale'],[])
        self.assertNotIn('jale',self.engine.view()['startup_holds'])

    def test_unreadable_authority_no_fallback_and_maintenance_no_open_or_prepare(self):
        (self.engine.directory/'maintenance').touch();before=self.fingerprint()
        with self.assertRaises(ValueError):self.prepare()
        self.assertEqual(self.fingerprint(),before)
        with patch.object(control,'pressure',return_value=self.low):
            with self.assertRaises(ValueError):self.engine.guard(self.low)
            with self.assertRaises(ValueError):self.engine.manual('jale',resume=True)
        self.assertFalse(any(c[0] in ('parallel','start') for c in self.p.calls))
        (self.engine.directory/'maintenance').unlink()
        self.engine.path.write_bytes(b'corrupt');before=self.fingerprint()
        with self.assertRaises(sqlite3.Error):self.prepare()
        self.assertEqual(self.fingerprint(),before)

    def test_o_w1_legacy_foreign_ownership_does_not_poison_three_ticks(self):
        self.queue['groups']['suites']=dict(status='Running',parallel_tasks=3);self.write()
        self.prepare();self.boot()
        for _ in range(3):
            with patch.object(control,'pressure',return_value=self.low):self.engine.guard(self.low)
        self.assertIn('suites',self.p.groups)
        self.assertIn('suites',self.state()['reasons'])
        self.assertEqual(self.p.groups['jale']['status'],'Running')
        # Authorized external retirement of an EMPTY group; absent ownership is cleaned.
        del self.p.groups['suites']
        for _ in range(3):
            with patch.object(control,'pressure',return_value=self.low):self.engine.guard(self.low)
        self.assertNotIn('suites',self.state()['reasons'])
        self.assertNotIn('suites',self.engine.view()['startup_holds'])

    def test_explicit_retirement_is_recoverable_and_refuses_tasks(self):
        self.p.groups['suites']=dict(status='Running',parallel_tasks=3)
        state=self.state();state['reasons']['suites']=['manual']
        with self.engine.connect(write=True) as db:self.engine.store(db,state)
        original=self.p.call
        def remove(*args):
            if args==('group','remove','suites'):
                self.p.calls.append(args);del self.p.groups['suites']
                raise OSError('lost answer after removal')
            return original(*args)
        with patch.object(self.p,'call',side_effect=remove):
            with self.assertRaises(OSError):self.engine.retire_empty('suites')
        self.assertEqual(self.state()['retire_intent'],'suites')
        self.assertTrue(self.engine.reconcile())
        self.assertNotIn('retire_intent',self.state());self.assertNotIn('suites',self.state()['reasons'])
        self.p.groups['suites']=dict(status='Running',parallel_tasks=3)
        original_status=self.p.status
        def busy():
            value=original_status();value['tasks']['7']=task(7,queued(),'suites');return value
        before=self.engine.path.read_bytes()
        with patch.object(self.p,'status',side_effect=busy):
            with self.assertRaises(ValueError):self.engine.retire_empty('suites')
            self.assertEqual(self.engine.path.read_bytes(),before)
            for _ in range(3):
                with patch.object(control,'pressure',return_value=self.low):self.engine.guard(self.low)
        self.assertIn('suites',self.p.groups)
        self.assertEqual(self.p.groups['jale']['status'],'Running')

    def test_graceful_stop_marks_only_active_identities_before_terminal_killed(self):
        snapshot=copy.deepcopy(self.queue)
        snapshot['tasks']={'0':task(0,running()),'1':task(1,done('Killed'))}
        with patch.object(self.p,'status',return_value=snapshot):self.engine.record_stop()
        snapshot['tasks']['0']['status']=done('Killed')
        self.queue=snapshot;self.write();self.prepare()
        self.assertEqual(set(self.state()['startup']['interruptions']),{'0'})
        self.assertNotIn('stop_tasks',self.state())

    def test_cli_invalid_queue_does_not_even_write_diagnostic_log(self):
        self.queue['tasks']['0']['created_at']='2026-11-12T08:00:00+00:99';self.write()
        before=self.fingerprint()
        with patch.object(control,'validate_configuration',return_value=self.config),patch.object(control,'STATE_DIR',self.engine.directory), \
             patch.object(control,'MiniControl',return_value=self.engine),patch.object(control,'Pueue',return_value=self.p), \
             patch.object(control.sys,'argv',['control.py','preparar','--host','desk']):
            with self.assertRaises(ValueError):control.main()
        self.assertEqual(self.fingerprint(),before)



class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp=tempfile.TemporaryDirectory(prefix='u34-config-sol-',dir='/tmp');self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name);self.data=self.home/'data';self.data.mkdir()
        self.config=self.home/'pueue.json';self.pid=self.data/'pid'
        self.commands=[]
        def version(argv,**kw):
            self.commands.append(argv);return Path(argv[0]).name+' 4.0.4\n'
        manager=patch.object(control.subprocess,'check_output',side_effect=version);manager.start();self.addCleanup(manager.stop)

    def validate(self):
        return control.validate_configuration(home=self.home,config_path=self.config,data=self.data,pid_file=self.pid,
                                               pueue=self.home/'pueue',daemon=self.home/'pueued')

    def test_o_w2_complex_configuration_rejects_in_same_validator(self):
        for text in ('daemon:\n  env_vars:\n    TZ: UTC\n','daemon:\n  shell_command:\n    - sh\n    - -c\n',
                     'shared:\n  pueue_directory: /custom/data\n',json.dumps(dict(daemon=dict(env_vars={'TZ':'UTC'}))),
                     json.dumps(dict(daemon=dict(shell_command=['sh','-c'])))):
            with self.subTest(text=text):
                self.config.write_text(text);before=self.config.read_bytes()
                with self.assertRaises(ValueError):self.validate()
                self.assertEqual(self.config.read_bytes(),before)
                self.assertEqual(list(self.data.iterdir()),[])
        self.config.write_text('daemon:\n  compress_state_file: false\nshared:\n  use_unix_socket: true\n')
        self.assertEqual(self.validate(),dict(data=self.data,pid_file=self.pid))

    def test_binary_versions_and_relative_xdg_are_fail_closed(self):
        with patch.object(control.subprocess,'check_output',return_value='pueued 5.0.0'):
            with self.assertRaises(ValueError):self.validate()
        with patch.dict(control.os.environ,{'XDG_RUNTIME_DIR':'relative'}):
            with self.assertRaises(ValueError):self.validate()



class InstallerStartupTests(unittest.TestCase):
    def setUp(self):
        ConfigurationTests.setUp(self)
        spec=importlib.util.spec_from_file_location('u34_install',Path(control.__file__).with_name('install-desk.py'))
        self.installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.installer)
        self.config.write_text('{}')
        self.directory=self.home/'.local/state/pao/pueue-desk'
        self.library=self.home/'.local/lib/pao-pueue-desk';self.library.mkdir(parents=True)
        (self.library/'control.py').write_text('# previous controller\n')
        from test_slots import FakePueue
        self.p=FakePueue();self.now=utc('2026-11-14T13:59:59')
        self.p.groups={g:dict(status='Running',parallel_tasks=n) for g,n in control.desk_profile(self.now).items()}
        self.engine=control.MiniControl(self.p,self.directory,host='desk',clock=lambda:self.now,generation=lambda:'live-original')
        with control.locked(self.directory):self.engine.adopt(self.pid)
        (self.data/'state.json').write_text(json.dumps(dict(tasks={},groups=self.p.groups)))
        self.syscalls=[]
        original_validator=control.validate_configuration
        def validate(home=None):
            return original_validator(home=self.home,config_path=self.config,data=self.data,pid_file=self.pid,
                                      pueue=self.home/'pueue',daemon=self.home/'pueued')
        for manager in (patch.object(control,'validate_configuration',side_effect=validate),
                        patch.object(control,'Pueue',return_value=self.p),
                        patch.object(control,'MiniControl',return_value=self.engine),
                        patch.object(control,'mini_generation',return_value='live-original'),
                        patch.object(self.installer,'run',side_effect=lambda *a:self.syscalls.append(a))):
            manager.start();self.addCleanup(manager.stop)

    def fingerprint(self):return {str(p.relative_to(self.home)):p.read_bytes() for p in self.home.rglob('*') if p.is_file()}

    def test_o_w2_install_real_and_seco_use_validator_before_every_effect(self):
        for text in ('daemon:\n  env_vars:\n    TZ: UTC\n','daemon:\n  shell_command:\n    - sh\n',
                     'shared:\n  pueue_directory: /custom\n'):
            for dry in (True,False):
                self.config.write_text(text);before=self.fingerprint();self.syscalls.clear()
                with self.assertRaises(ValueError):self.installer.install(self.home,self.pid,control,dry_run=dry)
                self.assertEqual(self.fingerprint(),before)
                self.assertEqual(self.syscalls,[])
                self.assertEqual(self.engine.generation(None),'live-original')
                self.assertEqual(self.p.calls,[])

    def test_all_pending_receipts_reject_even_when_schema_already_current(self):
        with self.engine.connect(write=True) as db:
            state=self.engine.state(db);state['startup_version']=1;self.engine.store(db,state)
        for status in ('applying','partial','unknown'):
            with self.engine.connect(write=True) as db:
                db.execute('INSERT OR REPLACE INTO host_actions VALUES (?,?,?)',('pending-0001','hash',json.dumps(dict(request=dict(action_id='pending-0001'),response=dict(status=status)))))
            before=self.fingerprint()
            for dry in (True,False):
                with self.assertRaises(ValueError):self.installer.install(self.home,self.pid,control,dry_run=dry)
                self.assertEqual(self.fingerprint(),before)
                self.assertTrue(all(c[2]=='is-active' for c in self.syscalls))
                self.assertEqual(self.p.calls,[])

    def test_additive_install_keeps_pid_desire_receipts_and_loads_unit_without_restart(self):
        with self.engine.connect() as db:before=self.engine.state(db)
        with redirect_stdout(io.StringIO()):self.installer.install(self.home,self.pid,control)
        with self.engine.connect() as db:after=self.engine.state(db)
        self.assertEqual(after,dict(before,startup_version=1))
        self.assertTrue((self.home/'.config/systemd/user/pueued.service').is_file())
        self.assertIn(('systemctl','--user','daemon-reload'),self.syscalls)
        self.assertFalse(any('pueued.service' in c for c in self.syscalls))
        stops=[c for c in self.syscalls if c[2]=='stop']
        self.assertEqual(stops,[('systemctl','--user','stop',*self.installer.TIMERS),('systemctl','--user','stop',*self.installer.SERVICES)])
        self.assertFalse((self.directory/'maintenance').exists())
        self.assertEqual(self.engine.generation(None),'live-original')
        self.assertEqual(self.p.calls,[])

    def test_preexisting_maintenance_and_broken_symlink_are_never_removed(self):
        marker=self.directory/'maintenance';marker.symlink_to(self.home/'absent')
        with redirect_stdout(io.StringIO()):self.installer.install(self.home,self.pid,control)
        self.assertTrue(marker.is_symlink())
        self.assertFalse(any(c[2]=='start' for c in self.syscalls))

    def test_failure_after_unit_publication_retains_maintenance_and_stops_timers(self):
        def run(*args):
            self.syscalls.append(args)
            if args[2]=='daemon-reload':raise OSError('injected systemd failure')
        with patch.object(self.installer,'run',side_effect=run):
            with self.assertRaises(OSError):self.installer.install(self.home,self.pid,control)
        self.assertTrue((self.directory/'maintenance').exists())
        self.assertFalse(any(c[2]=='start' for c in self.syscalls))


if __name__=='__main__':unittest.main()
