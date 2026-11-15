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
import time
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

    def call(self,*args):
        try:return super().call(*args)
        finally:
            if hasattr(self,'persist'):self.persist()  # stock pause/start save; parallel still doesn't


class Harness:
    def __init__(self, test):
        self.tmp = tempfile.TemporaryDirectory(prefix='u35-handoff-', dir='/tmp')
        test.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.directory = self.home/'.local/state/pao/pueue-mini'
        self.data = self.home/'data'; self.data.mkdir()
        self.pid = self.data/'pueue.pid'; self.pid.write_text('999999\n')
        self.config = dict(data=self.data, pid_file=self.pid)
        self.p = Queue(); self.gen = 'mini-generation-1'; self.now = 1794740000; self.runtime = control
        self.p.groups['suites']['status'] = 'Paused'  # deliberately manual
        self.p.groups['rapidas']['status'] = 'Paused'  # guard ownership
        self.p.groups['ajeno']['status'] = 'Running'
        self.engine = control.MiniControl(self.p, self.directory, generation=lambda:self.gen, clock=lambda:self.now)
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
        self.p.persist = self.save_queue

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
                code=0 if label in self.loaded else 113
                self.loaded.discard(label)
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
            # launchd retries a rejected wrapper, including while withdrawal is
            # in flight. Never skip the wrapper based on the requested target.
            try:self.m.start(execute=execute)
            except ValueError:return
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
                with patch.object(self.runtime,'pressure_mini',return_value=LOW):self.engine.guard(LOW)
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

    def opposite_aborts(self, first, maintenance=False):
        h=self.h
        if first=='withdraw':h.m.change('install');h.finish()
        expected=h.owner;owner='prepared' if expected==mini.LABEL else 'homebrew'
        h.p.tasks={'0':task(0,running(),'default')};h.save_queue()
        h.m.change(first,wait=10);h.now+=11;h.m._recover(h.config)
        self.assertEqual(h.policy()['mini_handoff']['phase'],'aborted')
        self.assertEqual(h.policy()['mini_handoff']['retained_owner'],owner)
        second='install' if first=='withdraw' else 'withdraw'
        h.calls.clear();h.p.calls.clear();h.trace.clear();generation=h.gen
        h.m.change(second,wait=10);h.now+=11;h.m._recover(h.config)
        value=h.policy()['mini_handoff']
        self.assertEqual(value['phase'],'done' if expected==mini.LABEL else 'withdrawn')
        self.assertEqual(value.get('retained_owner'),owner)
        self.assertEqual(h.gen,generation)
        self.assertIn('Running',h.p.tasks['0']['status'],'same task survives both deadlines')
        self.assertFalse(any(c[0] not in ('print','print-disabled') for c in h.calls))
        self.assertEqual(h.p.calls,[],'convergence must not pause, drain or start')
        self.assertEqual(h.trace,['handoff:converged'])
        self.assertFalse(h.disabled.get(expected,False));self.assertTrue(h.disabled[mini.BREW if expected==mini.LABEL else mini.LABEL])
        if maintenance:
            h.m.maintenance();h.m.maintenance(resume=True);h.finish()
            self.assertEqual(h.owner,expected)
            self.assertEqual(h.policy()['mini_handoff']['phase'],'done' if expected==mini.LABEL else 'withdrawn')
        h.login();h.recover()
        self.assertEqual(h.owner,expected);self.assertTrue(h.p.online)
        self.assertEqual(h.p.tasks['0']['status']['Done']['result'],'Killed')
        self.assertEqual(h.loaded & {mini.LABEL,mini.BREW},{expected})
        print('R2 opposite:',first,'->',second,'owner='+expected,'online=True maintenance='+str(maintenance))

    def test_opposite_withdraw_install_running_converges_and_login(self):
        self.opposite_aborts('withdraw')

    def test_opposite_install_withdraw_running_converges_and_login(self):
        self.opposite_aborts('install')

    def test_opposite_aborts_maintenance_retains_each_owner(self):
        for first in ('withdraw','install'):
            self.h=Harness(self);self.opposite_aborts(first,maintenance=True)

    def test_live_target_converges_without_stage_drain_or_publication(self):
        for target in ('install','withdraw'):
            h=Harness(self);h.m.change('install');h.finish()
            if target=='withdraw':h.m.change('withdraw');h.finish()
            h.p.tasks={'0':task(0,running(),'default')};h.save_queue()
            before=h.fingerprint();h.calls.clear();h.p.calls.clear();h.trace.clear()
            for wait in (0,10):
                for dry in (True,False):
                    try:
                        with redirect_stdout(io.StringIO()):h.m.change(target,dry=dry,wait=wait)
                    except ValueError as error:self.fail('live target rejected: '+str(error))
            self.assertEqual(h.fingerprint(),before)
            self.assertEqual(h.p.calls,[]);self.assertEqual(h.trace,[])
            self.assertFalse(any(c[0] not in ('print','print-disabled') for c in h.calls))

    def test_abort_retains_verified_source_independently_of_target_and_validates_identity(self):
        h=self.h;h.p.tasks={'0':task(0,running(),'default')};h.save_queue()
        h.m.change('install',wait=10)
        source=h.policy()['mini_handoff'].get('source')
        self.assertEqual(source,dict(owner='homebrew',pid=999999,generation=h.gen))
        # A durable intention can describe the live target; abort must consume
        # the evidence recorded at acceptance, regardless of the requested verb.
        state=h.policy();state['mini_handoff']['target']='withdraw'
        h.m.save(state,'test:source-equals-target')
        try:
            h.m.abort()
            retained=h.policy()['mini_handoff']['retained_owner']
        except ValueError as error:self.fail('abort inferred a contradictory owner: '+str(error))
        self.assertEqual(retained,'homebrew')
        valid=h.policy()['mini_handoff']
        for invalid in ({},dict(owner='homebrew',pid=True,generation=h.gen),
                        dict(owner='homebrew',pid=0,generation=h.gen),
                        dict(owner='homebrew',pid=999999,generation=''),
                        dict(owner='prepared',pid=999999,generation=h.gen)):
            value=copy.deepcopy(valid);value['source']=invalid
            with self.assertRaises(ValueError):control.validate_mini_handoff(value)
        value=copy.deepcopy(valid);value.pop('source')
        with self.assertRaises(ValueError):control.validate_mini_handoff(value)

    def test_legacy_abort_requires_live_origin_and_never_infers_target(self):
        h=self.h;h.p.tasks={'0':task(0,running(),'default')};h.save_queue()
        h.m.change('install',wait=10);h.now+=11;h.m._recover(h.config)
        state=h.policy();value=state['mini_handoff'];value['version']=1;value.pop('source',None)
        h.m.save(state,'test:legacy');before=h.fingerprint();h.p.online=False
        with self.assertRaises(ValueError):h.m._recover(h.config)
        self.assertEqual(h.fingerprint(),before,'no live identity must leave legacy files unchanged')
        h.p.online=True;h.m._recover(h.config)
        self.assertEqual(h.policy()['mini_handoff']['source'],dict(owner='homebrew',pid=999999,generation=h.gen))

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
            phases=[p for p in dict.fromkeys(baseline.trace) if not p.startswith('publish:')];counts[target]=len(phases)
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
                    for group in ('default','rapidas'):
                        if target=='install':self.assertEqual(h.p.groups[group]['status'],'Running')
                        elif h.p.groups[group]['status']=='Paused':
                            # Withdrawal intentionally restores Brew without a future pre-start.
                            # A subsequent login may hold it, but never silently lose ownership.
                            self.assertIn('ambiguous',h.policy()['reasons'][group])
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
        after={k:v for k,v in h.fingerprint().items() if not k.endswith('/arranque.log')}
        self.assertEqual(after,before,'Only a rejected-start diagnostic may be written')
        self.assertFalse(any(c[0] in ('enable','bootstrap','bootout','disable') for c in h.calls))
        marker.unlink();h.finish()

    def test_running_drains_without_killing_then_recovery_owns_pause_before_rpc(self):
        h=self.h;h.p.tasks={'0':task(0,running(),'default')};h.save_queue()
        h.m.change('install',wait=600)
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
        self.assertEqual(bootouts,[mini.GUARD,mini.BREW,mini.LABEL])
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

    def continue_after_stale_bootout(self, target):
        h=self.h;h.m.change('install');h.finish()
        if target==mini.BREW:h.m.change('withdraw');h.finish()
        h.p.tasks={'0':task(0,running(),'default'),'1':task(1,queued(),'default')};h.save_queue()
        waiting=copy.deepcopy(h.p.tasks['1'])
        h.m.maintenance()
        # launchd still lists the retiring service when continuar checks it.
        h.loaded.add(target)
        launch=h.m.launch;stale=[False];observed=[]
        def asynchronous(*args,**kwargs):
            if args==('print',h.m.domain+'/'+target) and stale[0]:
                stale[0]=False;observed.append('stale-loaded')
                return subprocess.CompletedProcess(args,0,'service {}','')
            result=launch(*args,**kwargs)
            if args==('bootout',h.m.domain+'/'+target):
                h.pid.unlink();observed.append('bootout')
            return result
        h.m.launch=asynchronous;h.calls.clear();h.trace.clear()
        # Preserve verify's fallback independently of the synchronous bootout:
        # model a stale read at switch after the unload was acknowledged.
        def stale_at_switch(phase):
            h.trace.append(phase)
            if phase=='maintenance:restart-intent':stale[0]=True
        h.m.checkpoint=stale_at_switch
        h.m.maintenance(resume=True)
        self.assertEqual(observed,['bootout','stale-loaded'])
        self.assertEqual(h.policy()['mini_handoff']['phase'],'verify')
        self.assertNotIn(target,h.loaded);self.assertFalse(h.pid.exists())
        # A dead recoverer must leave the retry phase durable for its next tick.
        def crash(phase):
            h.trace.append(phase)
            if phase=='handoff:retry-switch':raise Crash(phase)
        h.m.checkpoint=crash
        try:
            with self.assertRaises(Crash):h.recover()
        except Exception as error:self.fail('verify failed instead of saving switch: '+type(error).__name__)
        self.assertEqual(h.policy()['mini_handoff']['phase'],'switch')
        self.assertIn('handoff:retry-switch',h.trace)
        h.m.checkpoint=h.trace.append
        h.finish()
        self.assertTrue(h.p.online);self.assertEqual(h.owner,target)
        self.assertEqual(h.loaded & {mini.BREW,mini.LABEL},{target})
        self.assertEqual(h.policy()['mini_handoff']['phase'],'done' if target==mini.LABEL else 'withdrawn')
        self.assertFalse(h.disabled[target]);self.assertTrue(h.disabled[mini.BREW if target==mini.LABEL else mini.LABEL])
        self.assertEqual(h.p.tasks['0']['status']['Done']['result'],'Killed')
        self.assertEqual(h.p.tasks['1'],waiting,'queued identity and position survive the retry')
        self.assertEqual(sum(c[0]=='bootstrap' and Path(c[-1]).stem==target for c in h.calls),1)
        generation=h.gen;h.recover();self.assertEqual(h.gen,generation,'no second daemon or task retry')
        print('R2b stale bootout:',target,'online=True retry_durable=True bootstraps=1 no_retry=True')

    def test_continue_stale_bootout_retries_prepared_owner(self):
        self.continue_after_stale_bootout(mini.LABEL)

    def test_continue_stale_bootout_retries_homebrew_owner(self):
        self.continue_after_stale_bootout(mini.BREW)

    def continue_with_bootout_race(self, target, delayed=False):
        h=self.h;h.m.change('install');h.finish()
        if target==mini.BREW:h.m.change('withdraw');h.finish()
        h.p.tasks={'0':task(0,running(),'default'),'1':task(1,queued(),'default')};h.save_queue()
        waiting=copy.deepcopy(h.p.tasks['1'])
        h.m.maintenance();h.loaded.add(target)
        launch=h.m.launch;pending=[False];clock=[0.];observed=[]
        def race(*args,**kwargs):
            if args==('bootout',h.m.domain+'/'+target):
                self.assertEqual(h.policy()['mini_handoff']['effect'],'bootout:'+target)
                result=launch(*args,check=False)
                if delayed:
                    pending[0]=True;h.loaded.add(target)
                else:
                    result=subprocess.CompletedProcess(args,3,'','Boot-out failed: 3: No such process')
                    if kwargs.get('check',True):result.check_returncode()
                observed.append('bootout')
                return result
            return launch(*args,**kwargs)
        def sleep(seconds):
            self.assertTrue(pending[0]);self.assertEqual(h.policy()['mini_handoff']['phase'],'maintenance')
            self.assertTrue((h.directory/'maintenance').exists())
            self.assertEqual(h.policy()['mini_handoff']['effect'],'bootout:'+target)
            self.assertFalse(any(c[0] in ('enable','bootstrap') for c in h.calls))
            clock[0]+=seconds;observed.append('wait')
            if clock[0]>=.3:
                h.loaded.remove(target);pending[0]=False;observed.append('unloaded')
        h.calls.clear();h.m.launch=race
        with patch.object(time,'monotonic',side_effect=lambda:clock[0]),patch.object(time,'sleep',side_effect=sleep):
            try:h.m.maintenance(resume=True)
            except Exception as error:self.fail('continuar failed: '+type(error).__name__)
        if delayed:
            self.assertIn('wait',observed,'bootout must wait while print still reports loaded')
            self.assertIn('unloaded',observed)
        else:self.assertEqual(observed,['bootout'])
        h.finish()
        self.assertTrue(h.p.online);self.assertEqual(h.owner,target)
        self.assertEqual(h.loaded & {mini.BREW,mini.LABEL},{target})
        self.assertEqual(h.policy()['mini_handoff']['phase'],'done' if target==mini.LABEL else 'withdrawn')
        self.assertEqual(h.p.tasks['0']['status']['Done']['result'],'Killed');self.assertEqual(h.p.tasks['1'],waiting)
        generation=h.gen;h.recover();self.assertEqual(h.gen,generation)
        print('R2c continuar:',target,'delayed='+str(delayed),'online=True no_retry=True')

    def test_continue_bootout_absent_3_prepared(self):
        self.continue_with_bootout_race(mini.LABEL)

    def test_continue_bootout_absent_3_homebrew(self):
        self.continue_with_bootout_race(mini.BREW)

    def test_continue_bootout_waits_prepared(self):
        self.continue_with_bootout_race(mini.LABEL,delayed=True)

    def test_continue_bootout_waits_homebrew(self):
        self.continue_with_bootout_race(mini.BREW,delayed=True)

    def bootout_failure_at_each_site(self, timeout=False):
        for site in ('switch','resume','guard-stop','daemon-stop'):
            with self.subTest(site=site):
                h=Harness(self);h.m.change('install');h.finish()
                if site=='resume':h.m.maintenance()
                label=mini.GUARD if site=='guard-stop' else mini.LABEL
                launch=h.m.launch;clock=[0.];hit=[False]
                def fail(*args,**kwargs):
                    if args==('bootout',h.m.domain+'/'+label):
                        hit[0]=True
                        result=subprocess.CompletedProcess(args,0 if timeout else 5,'','Input/output error')
                        if kwargs.get('check',True):result.check_returncode()
                        return result
                    return launch(*args,**kwargs)
                def sleep(seconds):clock[0]+=seconds
                h.m.launch=fail;h.calls.clear();h.trace.clear()
                operation=(lambda:h.m.change('withdraw')) if site=='switch' else lambda:h.m.maintenance(resume=site=='resume')
                # A stale loaded service persists for the entire bounded wait.
                h.loaded.add(label)
                with patch.object(time,'monotonic',side_effect=lambda:clock[0]),patch.object(time,'sleep',side_effect=sleep):
                    with self.assertRaises(TimeoutError if timeout else subprocess.CalledProcessError):operation()
                self.assertTrue(hit[0]);value=h.policy()['mini_handoff']
                self.assertEqual(value['phase'],'switch' if site=='switch' else 'maintenance')
                self.assertEqual(value['effect'],'bootout:'+label)
                self.assertFalse(any(c[0] in ('enable','bootstrap') for c in h.calls))
                self.assertNotIn('complete:bootout:'+label,h.trace)
                if timeout:
                    self.assertGreaterEqual(clock[0],5);self.assertLess(clock[0],5.1)
                    h.loaded.discard(label)
                h.m.launch=launch;h.m.checkpoint=h.trace.append
                if site=='switch':h.finish()
                else:h.m.maintenance();h.m.maintenance(resume=True);h.finish()
                self.assertTrue(h.p.online)

    def test_bootout_other_code_keeps_phase_at_each_site(self):
        self.bootout_failure_at_each_site()

    def test_bootout_timeout_keeps_phase_and_retries_at_each_site(self):
        self.bootout_failure_at_each_site(timeout=True)

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


class InjertoTests(unittest.TestCase):
    setUp = HandoffTests.setUp
    no_real_process = staticmethod(HandoffTests.no_real_process)
    def installed(self, h):
        library=h.m.library
        self.assertTrue(all((library/name).is_file() for name in (
            'mini.py','control.py','launchagents/'+mini.LABEL+'.plist','launchagents/'+mini.GUARD+'.plist')),
            'Installed recovery package must contain its templates before any accepted intent')
        spec=importlib.util.spec_from_file_location('installed_u35',library/'mini.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.assertEqual(module.ROOT,library.resolve())
        self.assertEqual(Path(module.control.__file__).parent,library.resolve())
        h.runtime=module.control
        h.engine=module.control.MiniControl(h.p,h.directory,generation=lambda:h.gen,clock=lambda:h.now)
        h.m=module.Manager(home=h.home,pueue=h.p,launch=h.launch,configuration=h.config,checkpoint=h.trace.append)
        h.m.engine=h.engine
        # Every future import/render is rooted in the installed package, never ROOT.
        return module

    def test_installed_w6_publications_requester_and_job_death_without_checkout(self):
        counts={}
        for target in ('install','withdraw'):
            baseline=Harness(self);baseline.m.install_recoverer()
            if target=='withdraw':baseline.m.change('install');baseline.finish()
            baseline.trace.clear();baseline.m.change(target);baseline.finish()
            phases=list(dict.fromkeys(baseline.trace));counts[target]=len(phases)
            for actor in ('requester','job'):
                for phase in phases:
                    # Module/guard publications precede the accepted intention;
                    # they belong only to the installer, never to the periodic job.
                    if actor=='job' and (phase=='handoff:intent' or phase.startswith('publish:') and
                            not phase.endswith('Library/LaunchAgents/'+mini.LABEL+'.plist')):
                        continue
                    with self.subTest(target=target,actor=actor,phase=phase):
                        h=Harness(self);h.m.install_recoverer()
                        if target=='withdraw':h.m.change('install');h.finish()
                        if actor=='job':
                            def accept(name):
                                if name=='handoff:intent':raise Crash(name)
                            h.m.checkpoint=accept
                            with self.assertRaises(Crash):h.m.change(target)
                            self.installed(h)
                        fired=[False]
                        def die(name):
                            if name==phase and not fired[0]:fired[0]=True;raise Crash(name)
                        h.m.checkpoint=die
                        try:
                            if actor=='requester':h.m.change(target)
                            h.finish()
                        except Crash:pass
                        self.assertTrue(fired[0],phase)
                        # Discard the requester/job and import both production modules
                        # afresh from the installed library. No installer rerun.
                        self.installed(h)
                        before=h.policy().get('mini_handoff')
                        try:
                            if before and before['phase'] not in mini.TERMINAL:
                                h.login();h.finish()
                            else:h.recover()
                        except Exception as error:
                            self.fail('Installed recovery failed: '+repr(error))
                        expected=(mini.LABEL if target=='withdraw' else mini.BREW) if not before or (
                            before['target']!=target) else (mini.LABEL if target=='install' else mini.BREW)
                        self.assertEqual(h.owner,expected)
                        self.assertIn(mini.GUARD,h.loaded)
                        self.assertEqual(h.p.groups['suites']['status'],'Paused')
                        self.assertEqual(h.policy()['desired'],control.MINI_PROFILE)
                        self.assertFalse(any('handoff' in r for r in h.policy()['reasons'].values()))
        # Two complete handoffs and two preparations, all with the checkout gone.
        h=Harness(self);h.m.install_recoverer();self.installed(h)
        for _ in range(2):
            h.m.change('install');h.finish();h.login();h.finish()
            h.m.change('withdraw');h.finish()
        h.p.online=False
        for _ in range(2):
            with control.locked(h.directory):h.runtime.prepare_start(h.engine,configuration=h.config)
        print('Installed W6 checkpoints (requester + job):',counts,'two full install/withdraw cycles; two preparations')

    def test_shared_projection_rejects_pending_and_bad_images_before_effects(self):
        for case in ('intent','target-image','queue','pid'):
            h=Harness(self)
            if case=='intent':
                h.p.online=False
                def crash(phase):
                    if phase=='intent':raise Crash(phase)
                with self.assertRaises(Crash):control.prepare_start(h.engine,configuration=h.config,boundary=crash)
                h.p.online=True
            if case=='queue':
                h.p.tasks={'0':task(0,queued())};h.p.tasks['0']['created_at']='2026-11-12T08:00:00+00:99';h.save_queue()
            if case=='pid':h.m.configuration=dict(h.config,pid_file=h.data/'other.pid')
            before=h.fingerprint();h.calls.clear()
            for dry in (True,False):
                plan=control.startup_plan
                def invalid_image(*args):
                    state,image=plan(*args);image['groups']['default']['status']='Reset'
                    return state,image
                invalid=patch.object(control,'startup_plan',side_effect=invalid_image)
                with invalid if case=='target-image' else patch.object(h.m,'configuration_for_host',wraps=h.m.configuration_for_host):
                    with self.assertRaises(ValueError):h.m.change('install',dry)
                self.assertEqual(h.fingerprint(),before)
                self.assertFalse(any(c[0] not in ('print','print-disabled') for c in h.calls))

    def test_explicit_wait_deadline_survives_job_death_aborts_without_killing_or_opening(self):
        h=self.h;h.p.tasks={'0':task(0,{'Paused':running()['Running']},'default')};h.save_queue()
        before=h.fingerprint()
        for dry in (False,True):
            with self.assertRaisesRegex(ValueError,'tareas en curso'):h.m.change('install',dry)
            self.assertEqual(h.fingerprint(),before)
        h.m.change('install',wait=600)
        self.assertEqual(h.policy()['mini_handoff']['deadline'],h.now+600)
        h.engine.manual('default')
        self.installed(h)  # the original requester has disappeared
        h.now+=601;h.p.calls.clear();h.calls.clear()
        self.assertTrue(h.m._recover(h.config))
        self.assertEqual(h.policy()['mini_handoff']['phase'],'aborted')
        self.assertEqual(h.owner,mini.BREW)
        self.assertIn('Paused',h.p.tasks['0']['status'])
        self.assertFalse(any(c[0] in ('bootout','bootstrap') for c in h.calls))
        self.assertFalse(any(c[0]=='start' for c in h.p.calls))
        self.assertEqual(set(h.policy()['reasons']['default']),{'manual','pressure'})
        self.assertNotIn('stop_tasks',h.policy())
        h.p.calls.clear()
        with patch.object(h.runtime,'pressure_mini',return_value=dict(load=4,memory=1)):
            h.engine.guard(dict(load=4,memory=1))
        self.assertFalse(any(c[0]=='start' for c in h.p.calls))
        with patch.object(h.runtime,'pressure_mini',return_value=LOW):h.engine.guard(LOW)
        self.assertEqual(h.p.groups['default']['status'],'Paused')
        self.assertEqual(h.p.groups['rapidas']['status'],'Running')
        valid=h.policy()
        for deadline in (True,'tomorrow',float('nan'),float('inf')):
            state=copy.deepcopy(valid);state['mini_handoff']['deadline']=deadline
            with h.engine.connect(write=True) as db:db.execute('UPDATE policy SET body=? WHERE id=1',(json.dumps(state),))
            with self.assertRaises(ValueError):h.m._recover(h.config)

    def test_withdraw_after_aborted_install_retains_source_enable_and_login(self):
        h=self.h;h.p.tasks={'0':task(0,running(),'default')};h.save_queue()
        h.m.change('install',wait=10);h.now+=11
        self.assertTrue(h.m._recover(h.config))
        self.assertEqual(h.policy()['mini_handoff']['phase'],'aborted')
        h.p.tasks['0']['status']=done();h.save_queue()
        h.m.change('withdraw');h.finish()
        self.assertFalse(h.disabled.get(mini.BREW,False))
        self.assertTrue(h.disabled[mini.LABEL])
        h.login();h.finish()
        self.assertEqual(h.owner,mini.BREW)

    def test_wait_budget_invalid_rejects_without_writes(self):
        for wait in (True,-1,7201,float('nan'),float('inf'),'60'):
            before=self.h.fingerprint()
            with self.assertRaises(ValueError):self.h.m.change('install',wait=wait)
            self.assertEqual(self.h.fingerprint(),before)

    def test_external_sigterm_without_record_stop_never_retries_or_invents_reason(self):
        h=self.h
        # Stock graceful shutdown persists Running as Done/Killed before preparing.
        active=task(0,running(),'default');cancelled=task(1,done('Killed'),'default')
        h.p.tasks={'0':active,'1':cancelled};h.save_queue()
        self.assertNotIn('stop_tasks',h.policy())
        h.p.tasks['0']['status']=done('Killed');h.save_queue();h.p.online=False
        before=copy.deepcopy(h.p.tasks)
        image=control.prepare_start(h.engine,configuration=h.config)
        self.assertEqual(image['tasks'],before)
        self.assertEqual(h.policy()['startup']['interruptions'],{})
        self.assertEqual(control.prepare_start(h.engine,configuration=h.config)['tasks'],before)
        self.assertNotIn('stop_tasks',h.policy())
        print('C-W1 negative: external SIGTERM -> Done/Killed retained; no retry; no invented attribution')

    def test_package_complete_before_intent_and_only_guard_code_updated(self):
        h=self.h
        def accepted(phase):
            if phase=='handoff:intent':
                self.assertTrue(all((h.m.library/name).is_file() for name in (
                    'mini.py','control.py','launchagents/'+mini.LABEL+'.plist','launchagents/'+mini.GUARD+'.plist')),
                    'Incomplete package published before handoff intent')
                self.assertIn(mini.GUARD,h.loaded)
                self.assertFalse(any(c[0]=='bootout' and c[-1].endswith('/'+mini.GUARD) for c in h.calls))
                raise Crash(phase)
        h.m.checkpoint=accepted
        with self.assertRaises(Crash):h.m.change('install')
        self.installed(h)
        with patch.object(mini,'ROOT',h.home/'checkout-no-longer-exists'):
            h.finish()
        self.assertEqual(h.owner,mini.LABEL)

    def test_installer_relevo_opt_in_routes_explicitly_without_legacy_reload(self):
        import test_pueue_mini as legacy
        installer=legacy.installer
        spec=Mock();module=Mock();manager=module.Manager.return_value
        with patch.object(installer.importlib.util,'spec_from_file_location',return_value=spec), \
             patch.object(installer.importlib.util,'module_from_spec',return_value=module), \
             patch.object(installer.Path,'home',return_value=self.h.home):
            with patch.object(installer.sys,'argv',['install-mini.py','instalar','--seco']):
                try:
                    with redirect_stdout(io.StringIO()):installer.main()
                except SystemExit as error:self.fail('Plain guard update unexpectedly routed to handoff: '+str(error))
            manager.change.assert_not_called()
            with patch.object(installer.sys,'argv',['install-mini.py','instalar','--relevo','--seco','--esperar','2']):
                installer.main()
            manager.change.assert_called_once_with('install',True,wait=120)
            manager.change.reset_mock()
            with patch.object(installer.sys,'argv',['install-mini.py','desinstalar','--relevo']):installer.main()
            manager.change.assert_called_once_with('withdraw',False,wait=0)


if __name__=='__main__':unittest.main()
