"""U3.5: isolated filesystem/SQLite, fake launchd with real login reconstruction."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch
from test_slots import control, FakePueue
from test_startup import task, queued, running, done

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('mini_handoff_tests', ROOT / 'mini.py')
mini = importlib.util.module_from_spec(spec); spec.loader.exec_module(mini)
mini.control = control
LOW = dict(load=.1, memory=1)


class Crash(BaseException): pass  # SIGKILL doesn't run an Exception handler


class Queue(FakePueue):
    def __init__(self):
        super().__init__(); self.tasks = {}; self.online = True

    def status(self):
        if not self.online: raise subprocess.CalledProcessError(1, ['fake-pueue'])
        value = super().status(); value['tasks'] = copy.deepcopy(self.tasks)
        return value


class Harness:
    def __init__(self, test):
        self.tmp = tempfile.TemporaryDirectory(prefix='u35-handoff-', dir='/tmp')
        test.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.directory = self.home/'.local/state/pao/pueue-mini'
        self.data = self.home/'data'; self.data.mkdir()
        self.pid = self.data/'pueue.pid'; self.pid.write_text('999999\n')
        self.config = dict(data=self.data, pid_file=self.pid)
        self.p = Queue(); self.gen = 'mini-generation-1'
        self.p.groups['suites']['status'] = 'Paused'  # deliberately manual
        self.p.groups['rapidas']['status'] = 'Paused'  # guard ownership
        self.p.groups['ajeno']['status'] = 'Running'
        self.engine = control.MiniControl(self.p, self.directory, generation=lambda:self.gen)
        with control.locked(self.directory): self.engine.adopt(self.pid)
        self.state = self.engine.state
        with self.engine.connect(write=True) as db:
            state = self.engine.state(db);state['reasons']['rapidas']=['pressure'];self.engine.store(db,state)
        self.agents = self.home/'Library/LaunchAgents';self.agents.mkdir(parents=True)
        self.put_plist(mini.BREW, [mini.DAEMON]);self.put_plist(mini.GUARD, ['fake-guard'])
        self.disabled = {};self.loaded = {mini.BREW,mini.GUARD};self.owner=mini.BREW
        self.calls=[];self.trace=[];self.serial=1
        self.m = mini.Manager(home=self.home,pueue=self.p,launch=self.launch,
                              checkpoint=self.trace.append,configuration=self.config)
        self.m.engine = self.engine
        self.save_queue()

    def put_plist(self,label,argv):
        (self.agents/(label+'.plist')).write_bytes(plistlib.dumps(dict(Label=label,ProgramArguments=argv,RunAtLoad=True)))

    def save_queue(self):
        (self.data/'state.json').write_text(json.dumps(dict(groups=self.p.groups,tasks=self.p.tasks)))

    def launch(self,*args,check=True):
        self.calls.append(args);verb=args[0];out='';code=0
        if verb=='print-disabled':
            out=self.m.domain+' => {\n'+''.join('"'+k+'" => '+str(v).lower()+'\n' for k,v in self.disabled.items())+'}'
        else:
            label=Path(args[-1]).stem if verb=='bootstrap' else args[-1].split('/')[-1]
            if verb=='print':
                code=0 if label in self.loaded else 113
                out='service {\n pid = 999999\n}' if self.p.online and self.owner==label else 'service {}'
                if label==mini.LABEL and label in self.loaded and not self.p.online:
                    out='service {\n pid = '+str(os.getpid())+'\n}'
            elif verb in ('disable','enable'):self.disabled[label]=verb=='disable'
            elif verb=='bootout':
                assert label in self.loaded
                self.loaded.remove(label)
                if self.owner==label:
                    self.save_queue();self.p.online=False;self.owner=None
            elif verb=='bootstrap':
                assert label not in self.loaded
                assert not self.disabled.get(label,False)
                self.loaded.add(label)
            else:raise AssertionError('No real launchd fallback: '+str(args))
        if check and code:raise subprocess.CalledProcessError(code,['fake-launchctl'])
        return subprocess.CompletedProcess(args,code,out,'')

    def login(self):
        self.p.online=False;self.owner=None;self.loaded=set()
        for path in self.agents.glob('*.plist'):
            value=plistlib.loads(path.read_bytes());label=value['Label']
            if not self.disabled.get(label,False):self.loaded.add(label)
        candidates=self.loaded & {mini.BREW,mini.LABEL}
        assert len(candidates)<=1, 'Login rebuilt TWO daemon candidates from enabled plists'
        self.boot()

    def boot(self):
        candidates=self.loaded & {mini.BREW,mini.LABEL}
        assert len(candidates)<=1, 'Two managers own the same data'
        if self.p.online or not candidates:return
        target=next(iter(candidates))
        def execute(binary,argv):
            assert binary==mini.DAEMON and argv==[mini.DAEMON]
            image=control.read_queue(self.data/'state.json')
            self.p.groups=image['groups'];self.p.tasks=image['tasks'];self.p.online=True;self.owner=target
            self.serial+=1;self.gen='mini-generation-'+str(self.serial);self.pid.write_text('999999\n')
        if target==mini.LABEL:
            # During withdrawal the fixed wrapper deliberately refuses to revive its source.
            if self.policy()['mini_handoff']['target']=='withdraw':return
            self.m.start(execute=execute)
        else:
            image=control.read_queue(self.data/'state.json')
            # Stock restore: only Running/Paused become Killed; Queued closes its group.
            for t in image['tasks'].values():
                if set(t['status']) & {'Running','Paused'}:t['status']=done('Killed')
                if 'Queued' in t['status']:image['groups'][t['group']]['status']='Paused'
            (self.data/'state.json').write_text(json.dumps(image))
            execute(mini.DAEMON,[mini.DAEMON])

    def recover(self):
        self.boot()
        with control.locked(self.directory):
            ready=self.m._recover(self.config)
            if ready:
                with patch.object(control,'pressure_mini',return_value=LOW):self.engine.guard(LOW)
        return ready

    def finish(self):
        for _ in range(8):
            try:self.recover()
            except ValueError:
                # Wrapper waiting for the lock has not exec'd yet; next tick observes it.
                raise
            if self.policy().get('mini_handoff',{}).get('phase') in mini.TERMINAL:return
        raise AssertionError('Handoff did not settle')

    def policy(self):
        with self.engine.connect() as db:return self.engine.state(db)

    def fingerprint(self):
        return {str(p.relative_to(self.home)):p.read_bytes() for p in self.home.rglob('*') if p.is_file()}


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.ps=patch.object(control.subprocess,'run',side_effect=self.no_real_process)
        self.ps.start();self.addCleanup(self.ps.stop)
        self.h=Harness(self)

    @staticmethod
    def no_real_process(argv,**kwargs):
        if argv[:1]==['/bin/ps']:return subprocess.CompletedProcess(argv,1,'','')
        raise AssertionError('Forbidden real subprocess: '+str(argv))

    def test_install_login_disable_and_withdraw_idempotent_preserve_manual_guard_and_receipts(self):
        h=self.h;before=h.policy()
        h.m.change('install');h.finish()
        self.assertEqual(h.policy()['mini_handoff']['phase'],'done')
        self.assertTrue(h.disabled[mini.BREW]);self.assertEqual(h.owner,mini.LABEL)
        self.assertEqual(h.p.groups['suites']['status'],'Paused')
        self.assertEqual(h.p.groups['default']['status'],'Running')
        self.assertEqual(h.p.groups['rapidas']['status'],'Running')
        self.assertEqual(h.policy()['desired'],before['desired'])
        self.assertEqual(h.policy()['revision'],before['revision'])
        h.login();h.finish();self.assertEqual(h.owner,mini.LABEL)
        generation=h.gen;h.m.change('install');self.assertEqual(h.gen,generation)
        h.m.change('withdraw');h.finish();h.login();h.finish()
        self.assertEqual(h.owner,mini.BREW);self.assertTrue(h.disabled[mini.LABEL])
        self.assertFalse(h.disabled[mini.BREW]);self.assertIn(mini.GUARD,h.loaded)
        self.assertEqual(h.p.groups['suites']['status'],'Paused')
        self.assertTrue(h.engine.path.exists())
        generation=h.gen;h.m.change('withdraw');self.assertEqual(h.gen,generation)

    def test_w6_every_durable_and_effect_boundary_including_job_kill_and_login(self):
        counts={}
        for target in ('install','withdraw'):
            baseline=Harness(self)
            if target=='withdraw':baseline.m.change('install');baseline.finish()
            baseline.trace.clear();baseline.m.change(target);baseline.finish()
            phases=list(dict.fromkeys(baseline.trace));counts[target]=len(phases)
            self.assertTrue(any(p=='effect:bootout:'+ (mini.BREW if target=='install' else mini.LABEL) for p in phases))
            for phase in phases:
                with self.subTest(target=target,phase=phase):
                    h=Harness(self)
                    if target=='withdraw':h.m.change('install');h.finish()
                    fired=[False]
                    def crash(name):
                        if name==phase and not fired[0]:fired[0]=True;raise Crash(name)
                    h.m.checkpoint=crash
                    try:
                        h.m.change(target);h.finish()
                    except Crash:pass
                    self.assertTrue(fired[0],phase)
                    h.m.checkpoint=lambda _:None
                    marker=h.directory/'maintenance';marker.touch()
                    calls=len(h.calls)
                    self.assertFalse(h.m._recover(h.config))
                    self.assertEqual(len(h.calls),calls,'Maintenance must not touch launchd at any W6 boundary')
                    marker.unlink()
                    # The installer is gone. A login reloads ALL enabled files, then the SAME job repairs.
                    h.login();h.finish()
                    # Kill the repairing job once more, independently of the original requester.
                    self.assertIn(h.policy()['mini_handoff']['phase'],mini.TERMINAL)
                    self.assertEqual(h.owner,mini.LABEL if target=='install' else mini.BREW)
                    self.assertEqual(h.p.groups['suites']['status'],'Paused')
                    self.assertEqual(h.policy()['desired'],control.MINI_PROFILE)
                    self.assertFalse(any('handoff' in r for r in h.policy()['reasons'].values()))
        print('W6 distinct checkpoints:',counts)

    def test_seco_and_pending_receipts_reject_before_any_effect_or_write(self):
        h=self.h;before=h.fingerprint();calls=list(h.calls)
        with redirect_stdout(io.StringIO()):h.m.change('install',dry=True)
        self.assertTrue(h.fingerprint()==before,'Unexpected filesystem write')
        self.assertFalse(any(c[0] not in ('print','print-disabled') for c in h.calls[len(calls):]))
        for status in ('applying','partial','unknown'):
            with h.engine.connect(write=True) as db:
                db.execute('INSERT OR REPLACE INTO host_actions VALUES(?,?,?)',('pending-00001','digest',json.dumps(dict(request=dict(action_id='pending-00001'),response=dict(status=status)))))
            before=h.fingerprint();h.calls.clear()
            for dry in (True,False):
                with self.assertRaisesRegex(ValueError,'recibos'):h.m.change('install',dry)
                self.assertTrue(h.fingerprint()==before,'Unexpected filesystem write')
                self.assertEqual(h.calls,[])

    def test_maintenance_stops_installer_recoverer_wrapper_and_guard_without_reopening(self):
        h=self.h;h.m.change('install')
        h.boot()  # pending verification, but a live owner makes a missing gate observable
        marker=h.directory/'maintenance';marker.symlink_to(h.directory/'absent')
        before=h.fingerprint();h.calls.clear()
        self.assertFalse(h.m._recover(h.config));self.assertFalse(h.engine.reconcile())
        h.p.online=False  # model the owned wrapper waiting to exec, not the old daemon
        with patch.object(control,'prepare_start',side_effect=AssertionError('prepare under maintenance')):
            with self.assertRaises(ValueError):h.m.start(execute=lambda *_:self.fail('exec under maintenance'))
        with self.assertRaises(ValueError):h.m.change('withdraw')
        self.assertTrue(h.fingerprint()==before,'Unexpected filesystem write')
        self.assertFalse(any(c[0] in ('enable','bootstrap','bootout','disable') for c in h.calls))
        marker.unlink();h.finish()

    def test_running_drains_without_killing_then_recovery_owns_pause_before_rpc(self):
        h=self.h;h.p.tasks={'0':task(0,running(),'default')};h.save_queue()
        h.m.change('install')
        self.assertEqual(h.policy()['mini_handoff']['phase'],'drain')
        self.assertEqual(h.owner,mini.BREW)
        self.assertIn('handoff',h.policy()['reasons']['default'])
        self.assertFalse(any(c[0]=='bootout' for c in h.calls))
        h.engine.manual('default')  # a manual pause added while the handoff drains is separate
        h.p.tasks['0']['status']=done();h.save_queue();h.finish()
        self.assertEqual(h.p.tasks['0']['status'],done())
        self.assertEqual(h.p.groups['default']['status'],'Paused')

    def test_maintenance_during_bootout_gap_blocks_every_manager_effect(self):
        h=self.h
        def crash(phase):
            if phase=='effect:bootout:'+mini.BREW:raise Crash(phase)
        h.m.checkpoint=crash
        with self.assertRaises(Crash):h.m.change('install')
        h.m.checkpoint=lambda _:None
        marker=h.directory/'maintenance';marker.touch();h.calls.clear()
        self.assertFalse(h.m._recover(h.config))
        self.assertEqual(h.calls,[])
        self.assertNotIn(mini.LABEL,h.loaded)
        marker.unlink();h.finish()

    def test_maintenance_command_stops_guard_first_and_resumes_only_own_marker(self):
        h=self.h;h.m.change('install');h.finish();h.calls.clear()
        h.m.maintenance()
        bootouts=[call[-1].split('/')[-1] for call in h.calls if call[0]=='bootout']
        self.assertEqual(bootouts,[mini.GUARD,mini.LABEL])
        self.assertTrue((h.directory/'maintenance').is_file())
        self.assertFalse(h.m._recover(h.config))
        self.assertTrue(h.engine.disabled())
        h.m.maintenance(resume=True);h.finish()
        self.assertEqual(h.owner,mini.LABEL)
        self.assertEqual(h.p.groups['suites']['status'],'Paused')
        self.assertFalse((h.directory/'maintenance').exists())
        marker=h.directory/'maintenance';marker.write_text('someone else\n')
        h.m.maintenance();h.calls.clear()
        with self.assertRaisesRegex(ValueError,'ajeno'):h.m.maintenance(resume=True)
        self.assertEqual(marker.read_text(),'someone else\n');self.assertEqual(h.calls,[])

    def test_maintenance_marker_changed_after_publication_is_never_removed(self):
        h=self.h;h.m.change('install');h.finish();h.m.maintenance()
        marker=h.directory/'maintenance';marker.write_text('another maintenance\n');h.calls.clear()
        with self.assertRaisesRegex(ValueError,'ajeno'):h.m.maintenance(resume=True)
        self.assertEqual(marker.read_text(),'another maintenance\n');self.assertEqual(h.calls,[])

    def test_maintenance_stop_and_continue_replay_after_each_boundary(self):
        counts={}
        for resume in (False,True):
            baseline=Harness(self);baseline.m.change('install');baseline.finish()
            if resume:baseline.m.maintenance()
            baseline.trace.clear();baseline.m.maintenance(resume=resume)
            if resume:baseline.finish()
            phases=list(dict.fromkeys(baseline.trace));counts[str(resume)]=len(phases)
            for phase in phases:
                with self.subTest(resume=resume,phase=phase):
                    h=Harness(self);h.m.change('install');h.finish()
                    if resume:h.m.maintenance()
                    fired=[False]
                    def crash(name):
                        if name==phase and not fired[0]:fired[0]=True;raise Crash(name)
                    h.m.checkpoint=crash
                    try:
                        h.m.maintenance(resume=resume)
                        if resume:h.finish()
                    except Crash:pass
                    self.assertTrue(fired[0]);h.m.checkpoint=lambda _:None
                    if not resume:h.m.maintenance()
                    if not os.path.lexists(h.directory/'maintenance'):h.boot()
                    try:h.m.maintenance(resume=True);h.finish()
                    except Exception as error:self.fail('Maintenance replay '+phase+': '+type(error).__name__)
                    self.assertFalse(os.path.lexists(h.directory/'maintenance'))
                    self.assertEqual(h.owner,mini.LABEL)
                    self.assertEqual(h.p.groups['suites']['status'],'Paused')
        print('Maintenance distinct checkpoints:',counts)

    def test_corrupt_handoff_blocks_queries_and_recovery_without_effects(self):
        h=self.h;h.m.change('install');h.finish();original=h.policy()
        for field,value in (('version',True),('phase','unrecognized'),('maintenance_owned',1),('original_disabled',{}),('effect',42)):
            changed=copy.deepcopy(original);changed['mini_handoff'][field]=value
            with h.engine.connect(write=True) as db:h.engine.store(db,changed)
            before=h.fingerprint();h.calls.clear()
            with self.assertRaises(ValueError):h.engine.view()
            with self.assertRaises(ValueError):h.m._recover(h.config)
            self.assertEqual(h.calls,[]);self.assertTrue(h.fingerprint()==before)

    def test_existing_recoverer_is_required_before_any_publication(self):
        h=self.h;h.loaded.remove(mini.GUARD);before=h.fingerprint()
        for dry in (True,False):
            with self.assertRaisesRegex(ValueError,'guardia existente'):h.m.change('install',dry)
            self.assertTrue(h.fingerprint()==before)
            self.assertFalse(any(call[0] not in ('print','print-disabled') for call in h.calls))

    def test_recovery_error_still_closes_pressure_but_never_opens(self):
        h=self.h
        for sample in (dict(load=4,memory=1),LOW):
            h.p.calls.clear()
            with patch.object(control,'recover_mini',side_effect=ValueError('recovery failed')), \
                 patch.object(control,'MiniControl',return_value=h.engine),patch.object(control,'pressure_mini',return_value=sample):
                with self.assertRaisesRegex(ValueError,'recovery failed'):
                    control.guard(h.p,Path('/no-proc'),h.directory,host='mini')
            self.assertFalse(any(c[0]=='start' for c in h.p.calls))
            self.assertEqual(h.p.groups['default']['status'],'Paused')
            self.assertEqual(h.p.groups['rapidas']['status'],'Paused')

    def test_mini_clients_and_fixed_exec_share_home_cwd_and_clean_config_environment(self):
        h=self.h
        with patch.dict(os.environ,{key:'/another-queue' for key in control.MANAGER_KEYS}), \
             patch.object(control.Path,'home',return_value=h.home),patch.object(control,'Pueue',return_value=h.p), \
             patch.object(control,'guard'),patch.object(control.sys,'argv',['control.py','guardia','--host','mini']):
            control.main()
            env=mini.daemon_environment(h.home)
        self.assertEqual(getattr(h.p,'context',{}).get('cwd'),h.home)
        self.assertEqual(getattr(h.p,'context',{}).get('env'),env)
        self.assertEqual(env['HOME'],str(h.home))
        for key in control.MANAGER_KEYS:self.assertNotIn(key,env)

    def test_two_daemons_and_alive_pid_are_rejected_without_overwriting_queue(self):
        h=self.h;h.m.change('install');h.finish()
        before=(h.data/'state.json').read_bytes()
        h.loaded.add(mini.BREW)
        h.p.online=False  # origin can be loaded while its socket/PID is not ready yet
        with self.assertRaises(ValueError):h.m.start(execute=lambda *_:self.fail('second daemon'))
        h.loaded.remove(mini.BREW)
        h.p.online=True
        with patch.object(control.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'pueued','')):
            with self.assertRaises(ValueError):h.m.start(execute=lambda *_:self.fail('live pid'))
        self.assertEqual((h.data/'state.json').read_bytes(),before)

    def test_unowned_wrapper_cannot_prepare_or_exec(self):
        h=self.h;h.m.change('install')
        with patch.object(h.m,'loaded',side_effect=lambda label:'service {\n pid = 2\n}' if label==mini.LABEL else None), \
             patch.object(control,'prepare_start',side_effect=AssertionError('unowned prepare')):
            with self.assertRaisesRegex(ValueError,'MainPID'):h.m.start(execute=lambda *_:self.fail('unowned exec'))

    def test_async_bootout_waits_for_dead_pid_before_bootstrap(self):
        h=self.h;launch=h.m.launch
        def retiring(*args,**kwargs):
            result=launch(*args,**kwargs)
            if args==('bootout',h.m.domain+'/'+mini.BREW):h.p.online=True
            return result
        h.m.launch=retiring
        h.m.change('install')
        self.assertEqual(h.policy()['mini_handoff']['phase'],'switch')
        self.assertNotIn(mini.LABEL,h.loaded)
        self.assertFalse(any(c[0]=='bootstrap' and Path(c[-1]).stem==mini.LABEL for c in h.calls))
        h.p.online=False;h.m.launch=launch;h.finish()
        self.assertEqual(h.owner,mini.LABEL)

    def test_mini_task_matrix_offline_crashes_and_repeated_prepare(self):
        h=self.h
        h.p.tasks={str(i):task(i,status,group) for i,status,group in (
            (0,running(),'default'),(1,{'Paused':running()['Running']},'suites'),
            (2,queued(),'rapidas'),(3,queued(),'default'),(4,{'Stashed':dict(enqueue_at=None)},'suites'),
            (5,{'Locked':dict(previous_status=queued())},'suites'),(6,done(),'rapidas'),
            (7,done({'Failed':7}),'rapidas'),(8,done('Killed'),'default'))}
        h.p.tasks['2'].update(priority=19,dependencies=[6]);h.save_queue()
        (h.data/'task_logs').mkdir();(h.data/'task_logs/3.log').symlink_to(h.data/'missing')
        source=h.fingerprint()
        for phase in ('intent','queue','complete'):
            with self.subTest(phase=phase):
                for name,data in source.items():(h.home/name).write_bytes(data)
                h.p.online=False
                def crash(name):
                    if name==phase:raise Crash(name)
                with self.assertRaises(Crash):control.prepare_start(h.engine,configuration=h.config,boundary=crash)
                image=control.prepare_start(h.engine,configuration=h.config)
                self.assertEqual(control.prepare_start(h.engine,configuration=h.config),image)
                self.assertEqual({g['status'] for g in image['groups'].values()},{'Paused'})
                for i in ('0','1','3'):self.assertEqual(image['tasks'][i]['status']['Done']['result'],'Killed')
                for i in ('2','4','5','6','7','8'):self.assertEqual(image['tasks'][i],h.p.tasks[i])
                s=h.policy();self.assertEqual(set(s['startup']['interruptions']),{'0','1','3'})
                self.assertIn('Locked',s['startup']['holds']['suites'])
                self.assertIn('log previo',s['startup']['holds']['default'])
                self.assertEqual(s['desired'],control.MINI_PROFILE)
                with h.engine.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM host_actions').fetchone()[0],0)

    def test_missing_corrupt_state_authority_and_configuration_preserve_all_files(self):
        for failure in ('absent','corrupt','authority','profile','config'):
            with self.subTest(failure=failure):
                h=Harness(self)
                if failure=='absent':(h.data/'state.json').unlink()
                if failure=='corrupt':(h.data/'state.json').write_text('{')
                if failure=='authority':h.engine.path.write_bytes(b'not sqlite')
                if failure=='profile':
                    with h.engine.connect(write=True) as db:
                        s=h.engine.state(db);s['policy_hash']='0'*64;h.engine.store(db,s)
                if failure=='config':
                    h.m.configuration=None
                before=h.fingerprint();h.calls.clear()
                for dry in (True,False):
                    with patch.object(control,'validate_configuration',side_effect=ValueError('complex config')) if failure=='config' else patch.object(h.m,'configuration_for_host',return_value=h.config):
                        with self.assertRaises(Exception):h.m.change('install',dry)
                    self.assertTrue(h.fingerprint()==before,'Unexpected filesystem write')
                    self.assertFalse(any(c[0] not in ('print','print-disabled') for c in h.calls))

    def test_guard_hook_completes_actions_but_handoff_and_maintenance_never_open(self):
        h=self.h
        for mocked in (patch.object(control,'MiniControl',return_value=h.engine),
                       patch.object(control,'recover_mini',side_effect=lambda *a:h.m._recover(h.config))):
            mocked.start();self.addCleanup(mocked.stop)
        # Actual guard entry imports mini.py and calls recovery under the existing lock.
        with patch.object(control,'pressure_mini',return_value=LOW):
            control.guard(h.p,Path('/no-proc'),h.directory,host='mini')
        view=h.engine.view()
        request=dict(schema=control.HOST_SCHEMA,op='slots',action_id='recover-action-001',dry_run=False,
                     expected={k:view[k] for k in ('revision','generation','policy_hash','mode','slots')},
                     change=dict(group='default',delta=1))
        # Free a slot before testing a lost response, using the real host protocol.
        request['change']=dict(group='rapidas',delta=-1)
        h.p.fail_at=h.p.rpc+5
        h.engine.handle(request);h.p.fail_at=None
        with patch.object(control,'pressure_mini',return_value=LOW):
            control.guard(h.p,Path('/no-proc'),h.directory,host='mini')
        response=h.engine.handle(dict(schema=control.HOST_SCHEMA,op='status',action_id=request['action_id']))
        self.assertEqual(response['status'],'applied')
        self.assertEqual(h.engine.view()['desired']['rapidas'],2)
        h.m.change('install')
        self.assertTrue(h.engine.disabled())
        h.p.calls.clear()
        with patch.object(control,'pressure_mini',return_value=LOW),patch.object(control,'recover_mini',return_value=False):
            control.guard(h.p,Path('/no-proc'),h.directory,host='mini')
        self.assertFalse(any(c[0]=='start' for c in h.p.calls))

    def test_o_w3_login_reconstructs_all_enabled_plists_at_bootstrap_boundary(self):
        h=self.h
        def crash(phase):
            if phase=='effect:bootstrap:'+mini.LABEL:raise Crash(phase)
        h.m.checkpoint=crash
        with self.assertRaises(Crash):h.m.change('install')
        h.m.checkpoint=lambda _:None
        h.login();h.finish()
        self.assertEqual(h.loaded & {mini.BREW,mini.LABEL},{mini.LABEL})

    def test_each_manager_effect_has_prior_durable_intent_and_pause_ownership(self):
        h=self.h;launch=h.m.launch
        def checked(*args,**kwargs):
            if args[0] in ('enable','disable','bootout','bootstrap'):
                label=Path(args[-1]).stem if args[0]=='bootstrap' else args[-1].split('/')[-1]
                self.assertEqual(h.policy()['mini_handoff']['effect'],args[0]+':'+label)
            return launch(*args,**kwargs)
        h.m.launch=checked
        call=h.p.call
        def pause(*args):
            if args[0]=='pause':self.assertIn('handoff',h.policy()['reasons'].get(args[-1],[]))
            return call(*args)
        h.p.call=pause
        h.m.change('install');h.finish()

    def test_shared_validator_in_seco_and_real_precedes_all_effects(self):
        for dry in (True,False):
            h=Harness(self);h.m.configuration=None;before=h.fingerprint();h.calls.clear()
            with patch.object(control,'validate_configuration',side_effect=ValueError('unsupported')) as validator:
                with self.assertRaisesRegex(ValueError,'unsupported'):h.m.change('install',dry)
                validator.assert_called_once_with(home=h.home,env={},cwd=h.home,pueue=mini.PUEUE,daemon=mini.DAEMON)
            self.assertEqual(h.calls,[]);self.assertEqual(h.fingerprint(),before)

    def test_mini_admission_reconciles_before_final_sensor_and_preserves_pressure_ladder(self):
        h=self.h;h.m.change('install');h.finish()
        h.p.groups['default']['status']='Paused'
        with h.engine.connect(write=True) as db:
            s=h.engine.state(db);s['reasons']['default']=['pressure'];h.engine.store(db,s)
        h.p.groups['default']['parallel_tasks']=8
        def sample():
            self.assertEqual(h.p.groups['default']['parallel_tasks'],1)
            return dict(load=2.5,memory=1)
        h.p.calls.clear()
        with patch.object(control,'pressure_mini',side_effect=sample):h.engine.guard(LOW)
        self.assertNotIn(('start','-g','default'),h.p.calls)
        self.assertEqual(h.p.groups['default']['status'],'Paused')
        with patch.object(control,'pressure_mini',return_value=LOW):h.engine.guard(LOW)
        self.assertEqual(h.p.groups['default']['status'],'Running')
        self.assertEqual(h.p.groups['suites']['status'],'Paused')
        h.p.calls.clear()
        with patch.object(control,'pressure_mini',return_value=dict(load=2.5,memory=1)):
            h.engine.guard(dict(load=2.5,memory=1))
        self.assertIn(('pause','--wait','-g','default'),h.p.calls)
        self.assertNotIn(('pause','--wait','-g','rapidas'),h.p.calls)

    def test_fullfsync_is_required_for_own_files_on_macos(self):
        stream=Mock();stream.fileno.return_value=123
        with patch.object(control.sys,'platform','darwin'),patch.object(control.os,'fsync') as fsync,patch.object(control.fcntl,'fcntl') as full:
            control.sync_file(stream)
            stream.flush.assert_called_once();fsync.assert_called_once_with(123);full.assert_called_once_with(123,51)
            full.side_effect=OSError('disk')
            with self.assertRaises(OSError):control.sync_file(stream)

    def test_launchd_disabled_words_and_booleans_and_unknown_fail_closed(self):
        h=self.h
        for word,expected in (('true',True),('false',False),('disabled',True),('enabled',False)):
            output='disabled services = {\n "'+mini.BREW+'" => '+word+'\n}'
            with patch.object(h.m,'launch',return_value=subprocess.CompletedProcess([],0,output,'')):
                self.assertEqual(h.m.disabled(mini.BREW),expected)
                self.assertFalse(h.m.disabled(mini.GUARD))
        for output in ('disabled services = {\n "'+mini.BREW+'" => unknown\n}',
                       'disabled services = {\n "'+mini.BREW+'" => true\n "'+mini.BREW+'" => false\n}'):
            with patch.object(h.m,'launch',return_value=subprocess.CompletedProcess([],0,output,'')):
                with self.assertRaises(ValueError):h.m.disabled(mini.BREW)

    def test_delivered_plists_fixed_preparer_keepalive_throttle_and_existing_job(self):
        daemon=plistlib.loads((ROOT/'launchagents'/(mini.LABEL+'.plist')).read_bytes())
        guard=plistlib.loads((ROOT/'launchagents'/(mini.GUARD+'.plist')).read_bytes())
        self.assertEqual(daemon['ProgramArguments'],['/usr/bin/python3','-I','@HOME@/.local/lib/pao-pueue-mini/mini.py','arrancar'])
        self.assertTrue(daemon['KeepAlive']);self.assertTrue(daemon['RunAtLoad'])
        self.assertEqual(daemon['ThrottleInterval'],10)
        self.assertEqual(guard['StartInterval'],60);self.assertTrue(guard['RunAtLoad'])
        self.assertNotIn('KeepAlive',guard)
        h=self.h;h.m.change('install');h.boot()
        self.assertIn('start:prepared',h.trace)
        self.assertEqual(h.policy().get('startup',{}).get('phase'),'prepared')


if __name__=='__main__':unittest.main()
