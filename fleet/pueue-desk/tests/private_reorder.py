"""U3.6 acceptance: --run-private ONLY through the authorized Mini remote-run.

No default client config, production daemon, launchd or inherited task envs.
"""
import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from unittest.mock import patch

BASE=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('private_u36',BASE/'control.py')
c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-private',action='store_true',required=True)
    args=parser.parse_args()
    root=Path.cwd()/'private-u36-sol';root.mkdir(mode=0o700)
    data=root/'data';data.mkdir(mode=0o700)
    socket=Path('/tmp')/('u36-sol-'+str(os.getpid())+'.sock')
    cfg=root/'config.json';pid=data/'pueue.pid'
    settings=dict(shared=dict(pueue_directory=str(data),runtime_directory=str(data),pid_path=str(pid),
                  unix_socket_path=str(socket),shared_secret_path=str(data/'shared_secret'),
                  alias_file=str(root/'aliases.yml'),use_unix_socket=True),
                  daemon=dict(compress_state_file=False,env_vars={}),client=dict(edit_mode='toml'))
    c.atomic_json(cfg,settings)
    (root/'aliases.yml').write_text('synthetic_alias: "true"\n')
    env=dict(HOME=str(root),PATH='/usr/bin:/bin:/opt/homebrew/bin',TMPDIR=str(root))
    class Private(c.Pueue):
        def __init__(self):
            super().__init__('/opt/homebrew/bin/pueue',env=env,cwd=str(root))
            self.edit_settings=settings
            self.timings=[]
        def call(self,*argv):
            begun=time.monotonic()
            try:return subprocess.run([self.binary,'--config',str(cfg),*map(str,argv)],text=True,
                capture_output=True,check=True,timeout=self.timeout or 5,env=env,cwd=str(root)).stdout
            finally:self.timings.append((str(argv[0]),time.monotonic()-begun))
    p=Private();daemon=None;owned_processes=[];sensor_times=[];edit_times=[]
    sensor=c.pressure_mini
    def measured_sensor(*args,**kwargs):
        begun=time.monotonic();sensor(timeout=1);sensor_times.append(time.monotonic()-begun)
        return dict(load=0,memory=1)  # private admission is synthetic; real sampling cost is included
    out=(root/'daemon.log').open('a')
    def emit(event,**fields):print(json.dumps(dict(event=event,**fields),sort_keys=True),flush=True)
    def start():
        nonlocal daemon
        if pid.exists():pid.unlink()  # only our previous verified child has been waited
        daemon=subprocess.Popen(['/opt/homebrew/bin/pueued','--config',str(cfg)],cwd=str(root),env=env,stdout=out,stderr=out)
        end=time.monotonic()+5
        while time.monotonic()<end:
            if daemon.poll() is not None:raise RuntimeError('private daemon exited')
            try:p.status();return
            except subprocess.SubprocessError:time.sleep(.03)
        raise RuntimeError('private startup timeout')
    def descendants():
        rows=subprocess.check_output(['/bin/ps','-axo','pid=,ppid='],text=True).splitlines()
        parents={int(a):int(b) for a,b in (row.split() for row in rows)}
        found={daemon.pid};changed=True
        while changed:
            prior=set(found);found.update(pid for pid,parent in parents.items() if parent in found);changed=found!=prior
        return [dict(pid=pid,birth=c.priority_helper().birth(pid)) for pid in found if pid!=daemon.pid]
    def stop(hard=False):
        if daemon is None or daemon.poll() is not None:return
        assert int(pid.read_text())==daemon.pid
        comm=subprocess.check_output(['/bin/ps','-p',str(daemon.pid),'-o','comm='],text=True).strip()
        cmd=subprocess.check_output(['/bin/ps','-p',str(daemon.pid),'-o','command='],text=True)
        assert Path(comm).name=='pueued' and str(cfg) in cmd
        for group,row in p.status()['groups'].items():
            if row['status']=='Running':p.call('pause','--wait','-g',group)
        owned_processes.extend(descendants())
        for ident,t in p.status()['tasks'].items():
            if isinstance(t['status'],dict) and set(t['status']) & {'Running','Paused'}:p.call('kill',ident)
        daemon.kill() if hard else daemon.terminate();daemon.wait(timeout=5)
    def terminated(signum,frame):raise SystemExit(128+signum)
    signal.signal(signal.SIGTERM,terminated)
    engine=None
    def request(ident,undo=None):
        expected=engine.view()['reorder_groups']['suites']['expected']
        order=[r['id'] for r in expected['queue']];order.insert(1,order.pop())
        return dict(schema=c.HOST_SCHEMA,op='undo' if undo else 'reorder',action_id=ident,
             dry_run=False,expected=expected,**(dict(undo_of=undo) if undo else dict(change=dict(order=order))))
    def wait_running(ids):
        end=time.monotonic()+5
        while time.monotonic()<end:
            if all('Running' in p.status()['tasks'][str(i)]['status'] for i in ids):return
            time.sleep(.03)
        raise AssertionError('private blockers did not run')
    def add(command='true',priority=0,label=None,stash=False):
        argv=['add','-g','suites','--priority='+str(priority)]
        if stash:argv+=['--stashed']
        if label is not None:argv+=['--label',label]
        p.call(*argv,'--',command)
        return max(p.status()['tasks'].values(),key=lambda t:t['id'])['id']
    class Crash(BaseException):pass
    try:
        emit('version',value=subprocess.check_output([p.binary,'--version'],text=True).strip())
        start()
        for group,n in c.MINI_PROFILE.items():
            if group=='default':p.parallel(group,n)
            else:p.call('group','add',group,'--parallel',str(n))
        directory=root/'control';directory.mkdir(mode=0o700)
        engine=c.MiniControl(p,directory,generation=lambda:'private-'+str(daemon.pid))
        engine.coordinated_reorder=True
        engine.reorder_host_ready=lambda state:True  # private daemon has no launchd owner
        # Benchmark acceptance explicitly measures beyond the provisional bound.
        engine.reorder_budget=180;engine.reorder_rpc=5;engine.reorder_cost=.001
        with c.locked(directory),patch.object(c,'pressure_mini',side_effect=measured_sensor):
            engine.adopt(pid)
            original_edit=engine.edit_priority
            def timed_edit(*args):
                begun=time.monotonic()
                try:return original_edit(*args)
                finally:edit_times.append(time.monotonic()-begun)
            engine.edit_priority=timed_edit
            blockers=[add('sleep 400') for _ in range(3)]
            wait_running(blockers)
            for i in range(5):add("printf '  α\npriority = 777\n  '",[-2,-2,-8,-12,0][i], '  unicode α  ' if i%2 else None)
            before=p.status()
            marks=[]
            engine.reorder_boundary=lambda phase:marks.append((phase,time.monotonic()))
            r=request('private-order-0001');rpc_begin=len(p.timings);sensor_begin=len(sensor_times);edit_begin=len(edit_times);started=time.monotonic();answer=engine.handle(r);elapsed=time.monotonic()-started
            assert answer['status']=='applied',answer
            phase_times=dict(marks)
            emit('pipeline',pending=5,history=0,seconds=elapsed,paused_seconds=phase_times.get('start_effect',phase_times['terminal'])-phase_times['prepared'],accepted_to_receipt_seconds=phase_times['terminal']-phase_times['prepared'],status=answer['status'],max_rpc_seconds=max([v for _,v in p.timings[rpc_begin:]] or [0]),max_edit_seconds=max(edit_times[edit_begin:] or [0]),sensor_seconds=sum(sensor_times[sensor_begin:]),pause_budget=180,rpc_budget=5,private_synthetic_admission=True)
            for ident,task in before['tasks'].items():
                after=p.status()['tasks'][ident]
                assert {k:v for k,v in task.items() if k!='priority'}=={k:v for k,v in after.items() if k!='priority'}
            undo=engine.handle(request('private-undo-00001',r['action_id']))
            assert undo['status']=='applied',undo
            assert {i:t['priority'] for i,t in before['tasks'].items()}=={i:t['priority'] for i,t in p.status()['tasks'].items()}
            emit('exact_undo',negative_ties=True,fields=True,null_labels=True,whitespace=True,unicode=True)
            alias=add('synthetic_alias')
            unsafe=engine.handle(request('private-alias-0001'))
            assert unsafe['error']['code']=='edit_unsafe',unsafe
            p.call('stash',str(alias));p.call('remove',str(alias));emit('alias_rejected_before_pause',status=unsafe['status'])
            p.call('add','--stashed','-g','default','--','true')
            # True orphan CLI: stop a client while Pueue has its selected id Locked.
            r=request('private-orphan-001')
            engine.reorder_boundary=lambda phase:(_ for _ in ()).throw(Crash()) if phase=='prepared' else None
            try:engine.handle(r)
            except Crash:pass
            engine.reorder_boundary=lambda phase:None
            action=engine.reorder_actions()[-1];ident=action['before'][0]['id']
            p.call('pause','--wait','-g','suites')
            own=root/'orphan';own.mkdir(mode=0o700)
            sleeper=own/'sleeper.py'
            sleeper.write_text("import json,os,sys,time\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(str(os.getpid()))\ntime.sleep(8)\n")
            orphan_env=dict(env,EDITOR='/usr/bin/python3 -I '+str(sleeper)+' '+str(own/'pid'))
            cli=subprocess.Popen([p.binary,'--config',str(cfg),'edit',str(ident)],env=orphan_env,cwd=str(root),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            birth=c.priority_helper().birth(cli.pid)
            end=time.monotonic()+4
            while time.monotonic()<end:
                if 'Locked' in p.status()['tasks'][str(ident)]['status'] and (own/'pid').exists():break
                time.sleep(.03)
            else:raise AssertionError('no private Locked')
            editor_pid=int((own/'pid').read_text());editor_birth=c.priority_helper().birth(editor_pid)
            owned_processes.append(dict(pid=editor_pid,birth=editor_birth))
            editor_parent=int(subprocess.check_output(['/bin/ps','-p',str(editor_pid),'-o','ppid='],text=True))
            if editor_parent not in (1,cli.pid):owned_processes.append(dict(pid=editor_parent,birth=c.priority_helper().birth(editor_parent)))
            c.atomic_json(own/'editor.json',dict(pid=editor_pid,birth=editor_birth))
            # root must be a direct child of the private authority for recovery.
            inside=directory/'orphan';own.rename(inside)
            action.update(phase='edit_intent',editing_id=ident,process=dict(root=str(inside),client=dict(pid=cli.pid,birth=birth)))
            engine.reorder_save(action)
            cli.kill();cli.wait(timeout=3)
            for _ in range(5):
                if engine.reconcile():break
                time.sleep(.05)
            assert engine.handle(r)['status']=='aborted'
            assert 'Queued' in p.status()['tasks'][str(ident)]['status']
            emit('orphan_recovery',enqueue_by_id=ident,status=engine.handle(r)['status'])
            # Restart while a single known task is Locked, before stock restore.
            r=request('private-reboot-001')
            engine.reorder_boundary=lambda phase:(_ for _ in ()).throw(Crash()) if phase=='prepared' else None
            try:engine.handle(r)
            except Crash:pass
            engine.reorder_boundary=lambda phase:None
            action=engine.reorder_actions()[-1]
            action['phase']='edit_intent';action['editing_id']=action['before'][0]['id'];engine.reorder_save(action)
            p.call('pause','--wait','-g','suites')
            # Forge only our private on-disk Locked image with validated fields;
            # the preceding case already measured actual EditRequest/Locked.
            stop()
            queue=c.read_queue(data/'state.json');t=queue['tasks'][str(action['editing_id'])]
            t['status']={'Locked':dict(previous_status=t['status'])}
            c.atomic_json(data/'state.json',queue)
            image=c.prepare_start(engine,configuration=dict(data=data,pid_file=pid))
            assert 'Queued' in image['tasks'][str(action['editing_id'])]['status']
            start();assert engine.reconcile()
            emit('offline_before_stock',own_locked='Queued',foreign_stashed='preserved')
            # Large history: same fixture dimensions as MO2 (1500 x 2 KiB).
            # Build offline, without spending 1500 adds or running any history.
            stop();queue=c.read_queue(data/'state.json')
            template=next(t for t in queue['tasks'].values() if 'Stashed' in t['status'])
            maximum=max(int(i) for i in queue['tasks'])
            for i in range(maximum+1,maximum+1501):
                row=copy.deepcopy(template);row.update(id=i,command='true # '+('x'*2048),original_command='true # '+('x'*2048),group='default')
                queue['tasks'][str(i)]=row
            size=len(c.canonical(queue).encode())
            pad=max(0,(9400000-size+1499)//1500)
            for i in range(maximum+1,maximum+1501):queue['tasks'][str(i)]['envs']['SYNTHETIC_PAD']='y'*pad
            c.atomic_json(data/'state.json',queue)
            start();engine.reconcile()
            # Startup intentionally retained Paused groups; open only our groups
            # in the synthetic authority with fresh readings and no real queue.
            for group in c.MINI_PROFILE:engine.manual(group,resume=True)
            engine.guard(dict(load=0,memory=1))  # pressure ownership is released by the guard, never manual()
            blockers=[add('sleep 400') for _ in range(3)]
            wait_running(blockers)
            for count in (5,64):
                queued=[t for t in p.status()['tasks'].values() if t['group']=='suites' and 'Queued' in t['status']]
                while len(queued)<count:
                    add();queued=[t for t in p.status()['tasks'].values() if t['group']=='suites' and 'Queued' in t['status']]
                marks=[]
                engine.reorder_boundary=lambda phase:marks.append((phase,time.monotonic()))
                r=request('private-large-'+str(count).zfill(3))
                rpc_begin=len(p.timings);sensor_begin=len(sensor_times);edit_begin=len(edit_times);started=time.monotonic();answer=engine.handle(r);elapsed=time.monotonic()-started
                assert answer['status']=='applied',answer
                phase_times=dict(marks)
                emit('pipeline',pending=count,history=1500,state_bytes=(data/'state.json').stat().st_size,seconds=elapsed,paused_seconds=phase_times.get('start_effect',phase_times['terminal'])-phase_times['prepared'],accepted_to_receipt_seconds=phase_times['terminal']-phase_times['prepared'],status=answer['status'],max_rpc_seconds=max([v for _,v in p.timings[rpc_begin:]] or [0]),max_edit_seconds=max(edit_times[edit_begin:] or [0]),sensor_seconds=sum(sensor_times[sensor_begin:]),pause_budget=180,rpc_budget=5,private_synthetic_admission=True)
            emit('PASS',private_daemon=True,no_launchagents_created=True)
    finally:
        if daemon and daemon.poll() is None:
            # Retire only tasks of this config; daemon PID identity checked in stop().
            try:
                for ident,t in p.status()['tasks'].items():
                    if isinstance(t['status'],dict) and set(t['status']) & {'Running','Paused'}:p.call('kill',ident)
            finally:stop()
        time.sleep(.1)
        for identity in owned_processes:
            if identity['birth'] and c.priority_helper().birth(identity['pid'])==identity['birth']:
                os.kill(identity['pid'],signal.SIGTERM)
        time.sleep(.1)
        remaining=[p['pid'] for p in owned_processes if p['birth'] and c.priority_helper().birth(p['pid'])==p['birth']]
        emit('cleanup',daemon_exited=daemon is None or daemon.poll() is not None,launchagents_created=0,remaining_owned_pids=remaining)
        assert not remaining,'private owned child still alive'
        out.close()


if __name__=='__main__':main()
