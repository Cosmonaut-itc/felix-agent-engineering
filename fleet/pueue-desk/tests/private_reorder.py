"""U3.6 private acceptance, only through the authorized Mini remote-run.

Independent scenarios retain partial metrics on failure. No production config,
queue, launchd job or inherited task environment is used.
"""
import argparse
from contextlib import contextmanager
import copy
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from unittest.mock import patch

BASE=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('private_u36',BASE/'control.py')
c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)


def mark_times(marks):
    phases=dict(marks)
    def span(a,b):
        return phases[b]-phases[a] if a in phases and b in phases else None
    return dict(acceptance_to_pause_seconds=span('prepared','pause_begin'),
                pause_start_envelope_seconds=span('pause_begin','start_effect'),
                verification_seconds=span('verification_begin','verification_end'),
                admission_seconds=span('admission_begin','admission_end'),
                receipt_seconds=span('receipt_begin','terminal'),
                acceptance_to_receipt_seconds=span('prepared','terminal'),
                retained_close_observed_seconds=time.monotonic()-phases['pause_begin'] if 'pause_begin' in phases and 'start_effect' not in phases else None,
                last_phase=marks[-1][0] if marks else None)


def emit(event,**fields):
    print(json.dumps(dict(event=event,**fields),sort_keys=True),flush=True)


def step(name, run):
    try:
        run()
        emit('step',case=name,status='passed')
        return True
    except Exception as error:
        emit('step',case=name,status='failed',kind=type(error).__name__)
        return False


class Crash(BaseException):pass


class Sandbox:
    def __init__(self,root):
        self.root=root;root.mkdir(mode=0o700)
        self.data=root/'data';self.data.mkdir(mode=0o700)
        self.cfg=root/'config.json';self.pid=self.data/'pueue.pid'
        self.socket=Path('/tmp')/('u36-injerto-'+str(os.getpid())+'-'+root.name+'.sock')
        self.settings=dict(shared=dict(pueue_directory=str(self.data),runtime_directory=str(self.data),
            pid_path=str(self.pid),unix_socket_path=str(self.socket),shared_secret_path=str(self.data/'shared_secret'),
            alias_file=str(root/'aliases.yml'),use_unix_socket=True),daemon=dict(compress_state_file=False,env_vars={}),client=dict(edit_mode='toml'))
        c.atomic_json(self.cfg,self.settings);(root/'aliases.yml').write_text('{}\n')
        self.env=dict(HOME=str(root),PATH='/usr/bin:/bin:/opt/homebrew/bin',TMPDIR=str(root))
        self.daemon=None;self.owned=[];self.marks=[];self.measurements=[];self.rpcs=[]
        outer=self
        class Private(c.Pueue):
            def call(self,*argv):
                begun=time.monotonic();limit=self.timeout or 5
                if self.deadline is not None:
                    remaining=self.deadline-begun
                    if remaining<=0:raise TimeoutError('phase expired')
                    limit=min(limit,remaining)
                try:
                    return subprocess.run([self.binary,'--config',str(outer.cfg),*map(str,argv)],text=True,
                        capture_output=True,check=True,timeout=limit,env=outer.env,cwd=str(root)).stdout
                finally:outer.rpcs.append(dict(op=str(argv[0]),seconds=time.monotonic()-begun,timeout=limit))
        self.p=Private('/opt/homebrew/bin/pueue')
        self.p.edit_settings=self.settings;self.p.daemon_config=self.cfg
        self.log=(root/'daemon.log').open('a')

    def start(self):
        if self.pid.exists():
            assert self.daemon is not None and self.daemon.poll() is not None
            self.pid.unlink()
        self.daemon=subprocess.Popen(['/opt/homebrew/bin/pueued','--config',str(self.cfg)],cwd=str(self.root),env=self.env,stdout=self.log,stderr=self.log)
        end=time.monotonic()+5
        while time.monotonic()<end:
            if self.daemon.poll() is not None:raise RuntimeError('private daemon exited')
            try:self.p.status();return
            except subprocess.SubprocessError:time.sleep(.03)
        raise RuntimeError('private startup timeout')

    def stop(self):
        if self.daemon is None or self.daemon.poll() is not None:return
        assert int(self.pid.read_text())==self.daemon.pid
        comm=subprocess.check_output(['/bin/ps','-p',str(self.daemon.pid),'-o','comm='],text=True).strip()
        command=subprocess.check_output(['/bin/ps','-p',str(self.daemon.pid),'-o','command='],text=True)
        assert Path(comm).name=='pueued' and str(self.cfg) in command
        # Record descendants BEFORE killing tasks, so reparenting loses no identities.
        rows=subprocess.check_output(['/bin/ps','-axo','pid=,ppid='],text=True).splitlines()
        parents={int(a):int(b) for a,b in (row.split() for row in rows)}
        found={self.daemon.pid}
        while True:
            prior=set(found);found.update(pid for pid,parent in parents.items() if parent in found)
            if prior==found:break
        self.owned.extend(dict(pid=p,birth=c.priority_helper().birth(p)) for p in found if p!=self.daemon.pid)
        for group,row in self.p.status()['groups'].items():
            if row['status']=='Running':self.p.call('pause','--wait','-g',group)
        for ident,t in self.p.status()['tasks'].items():
            if isinstance(t['status'],dict) and set(t['status']) & {'Running','Paused'}:self.p.call('kill',ident)
        self.daemon.terminate();self.daemon.wait(timeout=5)

    def add(self,command='true',priority=0,label=None,stash=False):
        argv=['add','-p','-g','suites','--priority='+str(priority)]
        if stash:argv+=['--stashed']
        if label is not None:argv+=['--label',label]
        return int(self.p.call(*argv,'--',command).strip())

    def seed(self,history=False,count=5,alias=False):
        self.start()
        for group,n in c.MINI_PROFILE.items():
            if group=='default':self.p.parallel(group,n)
            else:self.p.call('group','add',group,'--parallel',str(n))
        self.p.call('pause','--wait','-g','suites')
        self.blockers=[self.add('sleep 400',priority=10000) for _ in range(3)]
        for i in range(count):self.add("new_after_add" if alias and i==4 else "printf '  α\npriority = 777\n  '",priority=[0,-2,-2,-8,-12][i%5],label='  unicode α  ' if i%2 else None)
        if history:
            ident=self.add(stash=True)
            self.stop();queue=c.read_queue(self.data/'state.json');template=queue['tasks'].pop(str(ident));maximum=max(map(int,queue['tasks']))
            for i in range(maximum+1,maximum+1501):
                row=copy.deepcopy(template);row.update(id=i,command='true # '+('x'*2048),original_command='true # '+('x'*2048),group='default')
                queue['tasks'][str(i)]=row
            pad=max(0,(9400000-len(c.canonical(queue).encode())+1499)//1500)
            for i in range(maximum+1,maximum+1501):queue['tasks'][str(i)]['envs']['SYNTHETIC_PAD']='y'*pad
            c.atomic_json(self.data/'state.json',queue);self.start()
        directory=self.root/'control';directory.mkdir(mode=0o700)
        self.engine=c.MiniControl(self.p,directory,generation=lambda:'private-'+str(self.daemon.pid))
        self.engine.coordinated_reorder=True
        self.engine.reorder_host_ready=lambda state:True  # no production launchd owner in this sandbox
        self.engine.adopt(self.pid)
        # Every manual opening goes through authority and a fresh synthetic admission.
        for group in c.MINI_PROFILE:self.engine.manual(group,resume=True)
        self.engine.guard(dict(load=0,memory=1))
        end=time.monotonic()+5
        while time.monotonic()<end:
            snap=self.p.status()
            if all('Running' in snap['tasks'][str(i)]['status'] for i in self.blockers):break
            time.sleep(.03)
        else:raise AssertionError('private blockers not running')
        original=self.engine.connect
        @contextmanager
        def connect(*args,**kwargs):
            begun=time.monotonic()
            try:
                with original(*args,**kwargs) as db:yield db
            finally:
                if kwargs.get('write'):self.measurements.append(('journal_write',time.monotonic()-begun))
        self.engine.connect=connect
        self.engine.reorder_boundary=lambda phase:self.marks.append((phase,time.monotonic()))
        self.engine.reorder_measure=lambda event,seconds:self.measurements.append((event,seconds))

    def request(self,ident,undo=None):
        expected=self.engine.view()['reorder_groups']['suites']['expected']
        order=[r['id'] for r in expected['queue']];order.insert(1,order.pop())
        return dict(schema=c.HOST_SCHEMA,op='undo' if undo else 'reorder',action_id=ident,dry_run=False,expected=expected,
            **(dict(undo_of=undo) if undo else dict(change=dict(order=order))))

    def action(self,request,label):
        self.marks.clear();self.measurements.clear();self.rpcs.clear()
        begun=time.monotonic();answer=None
        try:
            answer=self.engine.handle(request)
            return answer
        finally:
            totals={key:sum(v for k,v in self.measurements if k==key) for key,_ in self.measurements}
            metrics=dict(label=label,status=answer['status'] if answer else None,handle_seconds=time.monotonic()-begun,
                **mark_times(self.marks),steps=totals,step_samples=self.measurements,rpcs=self.rpcs,
                load=os.getloadavg(),state_bytes=(self.data/'state.json').stat().st_size,
                pause_budget=self.engine.reorder_budget,rpc_budget=self.engine.reorder_rpc,private_synthetic_admission=True)
            metrics['normal_budget_met']=metrics['pause_start_envelope_seconds'] is not None and metrics['pause_start_envelope_seconds']<=3
            emit('pipeline',**metrics)
            (self.root/(label+'.json')).write_text(json.dumps(metrics,indent=2))

    def recover(self,request):
        begun=time.monotonic()
        self.engine.reorder_boundary=lambda phase:None
        end=begun+12
        while time.monotonic()<end:
            self.engine.reconcile()
            answer=self.engine.handle(request)
            if answer['status'] in ('applied','aborted'):
                emit('recovery_terminal',status=answer['status'],seconds=time.monotonic()-begun)
                return answer
            time.sleep(.05)
        raise AssertionError('recovery did not reach terminal receipt')

    def close(self):
        try:self.stop()
        finally:
            for identity in self.owned:
                if identity['birth'] and c.priority_helper().birth(identity['pid'])==identity['birth']:os.kill(identity['pid'],signal.SIGTERM)
            time.sleep(.1)
            remaining=[i['pid'] for i in self.owned if i['birth'] and c.priority_helper().birth(i['pid'])==i['birth']]
            emit('cleanup',case=self.root.name,daemon_exited=self.daemon is None or self.daemon.poll() is not None,launchagents_created=0,remaining_owned_pids=remaining)
            self.log.close()
            assert not remaining,'own process still alive'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-private',action='store_true',required=True)
    parser.add_argument('--only',default='normal,large,aliases,orphan,restart,64')
    args=parser.parse_args()
    root=Path.cwd()/('private-u36-injerto-'+str(os.getpid()));root.mkdir(mode=0o700)
    failures=[];sensor=c.pressure_mini
    def measured_sensor(*a,**kw):
        sensor(*a,**kw)
        return dict(load=0,memory=1)  # actual sensor cost, synthetic admission; never authorizes real opening
    def normal(box,history=False):
        box.seed(history)
        before=box.p.status()
        r=box.request('private-normal-001')
        result=box.action(r,'normal')
        if result['status']=='unknown':box.recover(r)
        assert result['status']=='applied',dict(status=result['status'],stage='normal')
        snapshot=box.p.status()
        for ident,task in before['tasks'].items():
            after=snapshot['tasks'][ident]
            assert {k:v for k,v in task.items() if k!='priority'}=={k:v for k,v in after.items() if k!='priority'}
        undo_request=box.request('private-undo-0001',undo=r['action_id'])
        undo=box.action(undo_request,'undo')
        if undo['status']=='unknown':
            terminal=box.recover(undo_request)
            emit('undo_recovered',status=terminal['status'])
        snapshot=box.p.status()
        assert [snapshot['tasks'][i]['priority'] for i in before['tasks']]==[before['tasks'][i]['priority'] for i in before['tasks']]
        assert undo['status']=='applied',undo['status']  # recovery is not a normal-budget PASS
    def aliases(box):
        box.seed(alias=True)
        ident=max(t['id'] for t in box.p.status()['tasks'].values() if t['original_command']=='new_after_add')
        (box.root/'aliases.yml').write_text('new_after_add: "true"\n')
        request=box.request('private-alias-001');box.rpcs.clear()
        answer=box.action(request,'alias')
        assert answer['error']['code']=='edit_unsafe',answer['status']
        assert not any(r['op'] in ('pause','edit','start') for r in box.rpcs)
        t=box.p.status()['tasks'][str(ident)]
        assert t['command']==t['original_command']=='new_after_add'
        emit('alias_after_add',status=answer['status'],no_effects=True)
    def orphan(box,restart=False):
        box.seed()
        library=box.root/'library';library.mkdir(mode=0o700)
        helper=(BASE/'editor_prioridad.py').read_text()
        needle="    if (root/'revoked').exists():\n        raise ValueError('edit_unsafe')\n    if birth(plan['daemon']['pid'])"
        assert helper.count(needle)==1
        helper=helper.replace(needle,"    time.sleep(5)  # private fault injection, after editor identity is durable\n"+needle)
        (library/'editor_prioridad.py').write_text(helper)
        request=box.request('private-orphan-001')
        def boundary(phase):
            box.marks.append((phase,time.monotonic()))
            if phase!='client_sent':return
            action=box.engine.reorder_actions()[-1];info=action['process'];path=Path(info['root'])/'editor.json'
            end=time.monotonic()+.8
            while not path.exists() and time.monotonic()<end:time.sleep(.005)
            assert path.exists(),'helper identity not recorded'
            editor=json.loads(path.read_text());box.owned.extend([editor,info['client']])
            assert c.priority_helper().birth(editor['pid'])==editor['birth']
            os.kill(info['client']['pid'],signal.SIGKILL)
            os.kill(editor['pid'],signal.SIGKILL)
            emit('killed_own_editor_and_client',editing_id=action['editing_id'],phase=action['phase'])
            raise Crash()
        box.engine.reorder_boundary=boundary
        with patch.object(c,'__file__',str(library/'control.py')):
            try:box.action(request,'orphan')
            except Crash:pass
        box.engine.reorder_boundary=lambda phase:None
        if restart:
            # Save a real Locked through pause, stop our verified daemon, then run
            # the real preparer BEFORE stock restore can turn it into Stashed.
            box.p.call('pause','--wait','-g','suites');box.stop()
            image=c.prepare_start(box.engine,configuration=dict(data=box.data,pid_file=box.pid))
            assert not any('Locked' in t['status'] for t in image['tasks'].values())
            box.start()
        result=box.recover(request)
        assert result['status']=='aborted'
        assert not any('Locked' in t['status'] for t in box.p.status()['tasks'].values())
    def sixtyfour(box):
        box.seed(count=64)
        answer=box.action(box.request('private-64-00001'),'64-reject')
        assert answer['error']['code']=='pause_budget',answer['status']
        assert not any(r['op'] in ('pause','edit','start') for r in box.rpcs)
        emit('64_rejected_before_pause',status=answer['status'])
    cases=dict(normal=lambda b:normal(b),large=lambda b:normal(b,True),aliases=aliases,orphan=orphan,restart=lambda b:orphan(b,True),**{'64':sixtyfour})
    signal.signal(signal.SIGTERM,lambda a,b:(_ for _ in ()).throw(SystemExit(143)))
    emit('version',value=subprocess.check_output(['/opt/homebrew/bin/pueue','--version'],text=True).strip())
    with patch.object(c,'pressure_mini',side_effect=measured_sensor):
        for name in args.only.split(','):
            box=Sandbox(root/name)
            try:
                if not step(name,lambda:cases[name](box)):
                    failures.append(name)
                    if hasattr(box,'engine'):
                        emit('pending',actions=[dict(phase=a.get('phase'),failure=a.get('failure'),editing_id=a.get('editing_id'),status=a['response']['status']) for a in box.engine.reorder_actions()])
            finally:box.close()
    emit('FAIL' if failures else 'PASS',failed=failures,no_launchagents_created=True)
    return 1 if failures else 0


if __name__=='__main__':raise SystemExit(main())
