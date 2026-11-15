"""U3.4 acceptance. Only --run-private inside an authorized desk remote-run.

Never imports HOME config or starts production units. Every Pueue argv pins
--config. Transient services use only pao-u34-injerto-* and are stopped in finally.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
from unittest.mock import patch

BASE=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('u34',BASE/'control.py')
control=importlib.util.module_from_spec(spec);spec.loader.exec_module(control)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--run-private',action='store_true',required=True)
    parser.add_argument('--unit-worker',choices=('prepare','guard','stop'))
    parser.add_argument('--root',type=Path)
    args=parser.parse_args()
    if args.unit_worker:
        return worker(args.root,args.unit_worker)
    root=Path.cwd()/'private-u34-injerto';root.mkdir()
    pueue=Path.home()/'.local/bin/pueue';daemon=Path.home()/'.local/bin/pueued'
    data=root/'data';data.mkdir();pid=data/'pid';config=root/'config.json'
    config.write_text(json.dumps(dict(shared=dict(pueue_directory=str(data),runtime_directory=str(data),
        pid_path=str(pid),unix_socket_path=str(data/'socket'),use_unix_socket=True,
        shared_secret_path=str(data/'secret'),alias_file=str(data/'aliases.yml')),daemon=dict(compress_state_file=False))))
    env=dict(os.environ,TMPDIR=str(root));env.pop('PUEUE_CONFIG_PATH',None)
    events=(root/'events.jsonl').open('w',buffering=1)
    def emit(event,**fields):
        line=json.dumps(dict(event=event,**fields),sort_keys=True);print(line,flush=True);events.write(line+'\n')
    class Private(control.Pueue):
        def call(self,*argv):
            return subprocess.run([str(pueue),'--config',str(config),*map(str,argv)],check=True,
                                  capture_output=True,text=True,timeout=8,env=env).stdout
    p=Private();engine=control.MiniControl(p,root/'control',host='desk')
    process=None;unit='pao-u34-injerto-'+str(os.getpid())+'.service';guard_unit=unit.replace('.service','-guard.service')
    cgroup_directory=None
    output=(root/'daemon.log').open('a')
    def wait_for(predicate, seconds=12):
        until=time.monotonic()+seconds
        while time.monotonic()<until:
            try:
                result=predicate()
                if result:return result
            except (OSError,subprocess.SubprocessError,ValueError):pass
            time.sleep(.1)
        raise AssertionError('private wait expired')
    def show(*keys):
        raw=subprocess.run(['systemctl','--user','show',unit,*[a for k in keys for a in ('-p',k)]],text=True,capture_output=True).stdout
        return dict(line.split('=',1) for line in raw.splitlines() if '=' in line)
    def verified_main_pid():
        main=int(show('MainPID')['MainPID'])
        assert main>1 and Path('/proc',str(main),'comm').read_text().strip()=='pueued'
        assert unit in Path('/proc',str(main),'cgroup').read_text()
        assert int(pid.read_text())==main
        return main
    def cgroup_pids():
        nonlocal cgroup_directory
        group=show('ControlGroup').get('ControlGroup','')
        if group:cgroup_directory=Path('/sys/fs/cgroup')/group.lstrip('/')
        if cgroup_directory is None:return []  # failure before our transient service existed
        base=cgroup_directory  # inspect the original cgroup even after systemd collected the unit
        return sorted({int(p) for file in base.rglob('cgroup.procs') for p in file.read_text().split()}) if base.exists() else []
    def start():
        nonlocal process
        process=subprocess.Popen([str(daemon),'--config',str(config)],stdout=output,stderr=output,env=env)
        wait_for(lambda:p.status())
    def stop_process():
        if process is not None and process.poll() is None:
            # Verified own Popen, never a name/pattern. Wait before touching pid file.
            process.kill();process.wait(timeout=5)
    def prepare():
        configuration=control.validate_configuration(config_path=config,data=data,pid_file=pid,pueue=pueue,daemon=daemon)
        with control.locked(engine.directory):control.prepare_start(engine,data/'state.json',configuration)
    def add(command,group='jale',stashed=False):
        argv=['add','-g',group,'--label','synthetic-u34-sol']
        if stashed:argv+=['--stashed']
        text=p.call(*argv,command)
        import re
        return re.search(r'(\d+)',text)[1]
    units_created=[]
    try:
        emit('runtime',python=sys.version.split()[0],version=subprocess.check_output([str(daemon),'--version'],text=True).strip())
        start()
        for g,n in control.desk_profile(time.time()).items():p.call('group','add',g,'--parallel',n)
        p.call('pause','--wait','-g','default')
        with control.locked(engine.directory):
            engine.adopt(pid)
            with engine.connect(write=True) as db:
                state=engine.state(db);state['startup_version']=1;engine.store(db,state)
        # Running plus waiting tasks, manual pause, stashed, terminal success/failure.
        gate=root/'gate.py'
        gate.write_text('import pathlib,time\np=pathlib.Path('+repr(str(root)) + ')\n(p/"active-started").touch()\nend=time.monotonic()+15\nwhile not (p/"release").exists() and time.monotonic()<end:time.sleep(.1)\n(p/"active-ended").touch()\n')
        p.parallel('jale',1)
        active=add('python3 '+shlex.quote(str(gate)))
        wait_for(lambda:(root/'active-started').exists())
        queued_id=add('printf queued-u34-sol')
        stashed=add('printf stashed-u34-sol',stashed=True)
        success=add('true',group='rapidas');failed=add('exit 9',group='rapidas')
        wait_for(lambda: all('Done' in p.status()['tasks'][i]['status'] for i in (success,failed)))
        with control.locked(engine.directory):engine.manual('otros')
        before=p.status();p.call('pause','--wait','-g','hasito')  # force state save; manual unknown
        stop_process();(root/'release').touch();wait_for(lambda:(root/'active-ended').exists())
        prepare();first=json.loads((data/'state.json').read_text());prepare()
        assert json.loads((data/'state.json').read_text())==first
        assert first['tasks'][active]['status']['Done']['result']=='Killed'
        assert first['tasks'][queued_id]==before['tasks'][queued_id]
        assert first['tasks'][stashed]==before['tasks'][stashed]
        assert all(g['status']=='Paused' for g in first['groups'].values())
        start();restored=p.status()
        assert restored['tasks'][queued_id]['status']==before['tasks'][queued_id]['status']
        assert all(g['status']=='Paused' for g in restored['groups'].values())
        with control.locked(engine.directory),patch.object(control,'pressure',return_value=dict(io=0,cpu=0,memory=64*1024**3)):
            # Exercise Docker failure and success without depending on desk readiness.
            # The double sits on the Docker probe itself: every admission path reaches it.
            real_run=subprocess.run
            def docker(ok):
                def run(argv,*args,**kwargs):
                    if list(argv)[:1]==['/usr/bin/docker']:
                        if not ok:raise subprocess.CalledProcessError(1,argv)
                        return subprocess.CompletedProcess(argv,0,b'',b'')
                    return real_run(argv,*args,**kwargs)
                return patch.object(control.subprocess,'run',run)
            with docker(False):engine.guard(dict(io=0,cpu=0,memory=64*1024**3))
            assert 'Queued' in p.status()['tasks'][queued_id]['status']
            with docker(True):engine.guard(dict(io=0,cpu=0,memory=64*1024**3))
        wait_for(lambda:'Done' in p.status()['tasks'][queued_id]['status'])
        assert p.status()['groups']['otros']['status']=='Paused'
        assert p.status()['groups']['hasito']['status']=='Paused'
        assert p.status()['tasks'][active]['status']['Done']['result']=='Killed'
        emit('stock_restore_PASS',running_failed=True,queued_same_id=queued_id,stashed_same_id=stashed,
             all_closed_before_guard=True,manual_and_unknown_stay_closed=True,repeat_identical=True)
        # Transition to equivalent real systemd ordering around the SAME private queue.
        p.call('shutdown');process.wait(timeout=5)
        script=Path(__file__).resolve()
        worker_base=[sys.executable,str(script),'--run-private','--root',str(root)]
        pre=shlex.join(worker_base+['--unit-worker','prepare'])
        stop=shlex.join(worker_base+['--unit-worker','stop'])
        start_command=[str(daemon),'--config',str(config)]
        # Read delivered properties rather than using test-only restart delays.
        delivered=(BASE/'units/pueued.service').read_text().replace('@HOME@',str(root))
        properties={}
        for line in delivered.splitlines():
            if '=' in line and not line.startswith('#'):
                key,value=line.split('=',1);properties[key]=value
        assert properties['Restart']=='always' and properties['RestartSec']=='5'
        assert properties['KillMode']=='mixed' and properties['StartLimitIntervalSec']=='0'
        assert properties['OOMPolicy']=='continue'
        assert 'control.py preparar --host desk' in properties['ExecStartPre']
        assert 'control.py parada --host desk' in properties['ExecStop']
        emit('delivered_unit',properties={k:properties[k] for k in ('Restart','RestartSec','KillMode','OOMPolicy','StartLimitIntervalSec','ExecStartPre','ExecStop')})
        props={k:properties[k] for k in ('Restart','RestartSec','KillMode','OOMPolicy','StartLimitIntervalSec','WorkingDirectory','StandardOutput','StandardError')}
        props.update(ExecStartPre=pre,ExecStop=stop,ConditionPathExists='!'+str(engine.directory/'maintenance'))
        (root/'unit.txt').write_text(delivered)
        units_created.append(unit)
        subprocess.run(['systemd-run','--user','--unit',unit,
                        *['--property='+k+'='+v for k,v in props.items()],*start_command],check=True,capture_output=True,text=True)
        wait_for(lambda:p.status())
        first_pid=verified_main_pid()
        # Guard is ordered After/Requires this private service, never a real unit.
        units_created.append(guard_unit)
        subprocess.run(['systemd-run','--user','--unit',guard_unit,'--property=Type=oneshot',
                        '--property=After='+unit,'--property=Requisite='+unit,
                        *worker_base,'--unit-worker','guard'],check=True,capture_output=True,text=True)
        wait_for(lambda:(root/'guard-observed').exists())
        assert (root/'prepare-observed').exists()
        # Graceful stock shutdown exits successfully; Restart=always must relaunch.
        p.call('shutdown')
        wait_for(lambda:pid.exists() and int(pid.read_text())!=first_pid)
        second_pid=int(pid.read_text());wait_for(lambda:p.status())
        assert second_pid!=first_pid
        properties=show('MainPID','NRestarts','Restart','RestartUSec','KillMode','ExecStartPre','NeedDaemonReload')
        assert properties['RestartUSec']=='5s' and properties['Restart']=='always' and properties['KillMode']=='mixed'
        assert properties['NeedDaemonReload']=='no' and 'prepare' in properties['ExecStartPre']
        emit('systemd_PASS',pre_daemon_guard_order=True,successful_exit_restarted=True,properties=properties)
        # OOMPolicy=continue: an OOM inside the unit kills only the process the kernel picks.
        # A runtime-only memory cap (no swap) makes the OOM local to this private cgroup.
        assert show('OOMPolicy')['OOMPolicy']=='continue'
        subprocess.run(['systemctl','--user','set-property','--runtime',unit,'MemoryMax=384M','MemorySwapMax=0'],
                       check=True,capture_output=True,text=True)
        def memory(name):
            group=subprocess.run(['systemctl','--user','show',name,'-p','ControlGroup','--value'],
                                 capture_output=True,text=True,check=True).stdout.strip()
            return Path('/sys/fs/cgroup')/group.lstrip('/')
        def capped(name,limit):
            # The hog runs on the shared desk: never launch it unless this cgroup enforces the cap.
            folder=memory(name)
            assert (folder/'memory.max').read_text().strip()==str(limit),(folder/'memory.max').read_text()
            assert (folder/'memory.swap.max').read_text().strip()=='0',(folder/'memory.swap.max').read_text()
            return folder
        def oom_kills(folder):
            return int(dict(line.split() for line in (folder/'memory.events').read_text().splitlines())['oom_kill'])
        group_folder=capped(unit,384<<20);kills_before=oom_kills(group_folder)
        sibling_script=root/'oom-sibling.py'
        sibling_script.write_text('import os,pathlib,time\np=pathlib.Path('+repr(str(root))+')\n(p/"oom-sibling-pid").write_text(str(os.getpid()))\nend=time.monotonic()+120\nwhile not (p/"oom-sibling-release").exists() and time.monotonic()<end:time.sleep(.1)\n')
        hog_script=root/'oom-hog.py'
        # Bounded at 1 GiB and exits 0: without an OOM the assertions below fail instead of the desk.
        hog_script.write_text('b=[]\nfor _ in range(64):b.append(b"x"*(16<<20))\n')
        restarts=show('NRestarts')['NRestarts'];main_before=verified_main_pid()
        p.call('start','-g','jale')
        sibling=add('python3 '+shlex.quote(str(sibling_script)))
        wait_for(lambda:(root/'oom-sibling-pid').exists())
        hog=add('python3 '+shlex.quote(str(hog_script)))
        wait_for(lambda:'Done' in p.status()['tasks'][hog]['status'],seconds=60)
        hog_result=p.status()['tasks'][hog]['status']['Done']['result']
        time.sleep(2)
        assert hog_result=={'Failed':137},hog_result
        assert oom_kills(group_folder)>kills_before,'the kill must be an OOM inside this private cgroup'
        assert 'Running' in p.status()['tasks'][sibling]['status'],'sibling must survive the OOM'
        assert verified_main_pid()==main_before and show('NRestarts')['NRestarts']==restarts
        assert show('ActiveState')['ActiveState']=='active'
        (root/'oom-sibling-release').touch()
        wait_for(lambda:'Done' in p.status()['tasks'][sibling]['status'],seconds=20)
        sibling_result=p.status()['tasks'][sibling]['status']['Done']['result']
        assert sibling_result=='Success',sibling_result
        subprocess.run(['systemctl','--user','set-property','--runtime',unit,'MemoryMax=infinity','MemorySwapMax=infinity'],
                       check=True,capture_output=True,text=True)
        p.call('pause','--wait','-g','jale')
        emit('OOMPolicy_continue_PASS',hog_result=hog_result,sibling_result=sibling_result,
             main_pid_kept=main_before,restarts=restarts)
        # Contrast: the same OOM under OOMPolicy=stop takes the sibling down with it.
        (root/'oom-sibling-pid').unlink();(root/'oom-sibling-release').unlink()
        contrast=unit.replace('.service','-oomstop.service');units_created.append(contrast)
        go=root/'oom-contrast-go'
        subprocess.run(['systemd-run','--user','--unit',contrast,'--property=OOMPolicy=stop',
                        '--property=MemoryMax=256M','--property=MemorySwapMax=0','/bin/sh','-c',
                        'while [ ! -e '+shlex.quote(str(go))+' ]; do sleep .1; done; python3 '+shlex.quote(str(sibling_script))+' & while [ ! -s '+shlex.quote(str(root/'oom-sibling-pid'))+' ]; do sleep .1; done; python3 '+shlex.quote(str(hog_script))+'; wait'],
                       check=True,capture_output=True,text=True)
        capped(contrast,256<<20);go.touch()
        wait_for(lambda:subprocess.run(['systemctl','--user','show',contrast,'-p','Result','--value'],
                                       capture_output=True,text=True).stdout.strip()=='oom-kill',seconds=60)
        stopped_sibling=int((root/'oom-sibling-pid').read_text())
        wait_for(lambda:not Path('/proc',str(stopped_sibling)).exists(),seconds=20)
        emit('OOMPolicy_stop_contrast_PASS',result='oom-kill',sibling_killed=stopped_sibling)
        # A child in the service cgroup must be retired after MainPID SIGKILL.
        child=root/'child.py'
        child.write_text('import os,pathlib,time\np=pathlib.Path('+repr(str(root)) + ')\n(p/"child-pid").write_text(str(os.getpid()))\nend=time.monotonic()+90\nwhile not (p/"child-release").exists() and time.monotonic()<end:time.sleep(.1)\n')
        p.call('start','-g','jale')
        interrupted=add('python3 '+shlex.quote(str(child)))
        wait_for(lambda:(root/'child-pid').exists())
        child_pid=int((root/'child-pid').read_text())
        assert child_pid in cgroup_pids(), 'child must belong to private unit'
        p.call('pause','--wait','-g','hasito')  # stock persists the Running status
        old=verified_main_pid();os.kill(old,signal.SIGKILL)
        wait_for(lambda:pid.exists() and int(pid.read_text())!=old,seconds=35)
        wait_for(lambda:p.status())
        new=verified_main_pid()
        assert new!=old and child_pid not in cgroup_pids()
        assert p.status()['tasks'][interrupted]['status']['Done']['result']=='Killed'
        assert all(g['status']=='Paused' for g in p.status()['groups'].values())
        emit('SIGKILL_cgroup_PASS',killed_main=old,new_main=new,retired_child=child_pid,cgroup_pids=cgroup_pids())
        # A completed transient oneshot is already garbage-collected by systemd.
        subprocess.run(['systemctl','--user','stop',guard_unit],check=False,capture_output=True,text=True)
        subprocess.run(['systemctl','--user','stop',unit],check=True,capture_output=True,text=True)
        wait_for(lambda:not pid.exists() or subprocess.run(['/bin/ps','-p',pid.read_text().strip(),'-o','comm='],capture_output=True).returncode==1)
        assert cgroup_pids()==[],cgroup_pids()
        emit('stopped_cgroup_empty',pids=cgroup_pids())
        # Re-use only our exact unit name after collection, with a corrupt queue.
        subprocess.run(['systemctl','--user','reset-failed',unit],check=False,capture_output=True,text=True)
        saved=(data/'state.json').read_bytes();(data/'state.json').write_bytes(b'{"tasks":')
        wait_for(lambda:subprocess.run(['systemctl','--user','show',unit,'-p','LoadState','--value'],capture_output=True,text=True).stdout.strip()=='not-found')
        corrupt_start=subprocess.run(['systemd-run','--user','--unit',unit,
                        *['--property='+k+'='+v for k,v in props.items()],*start_command],
                        check=False,capture_output=True,text=True)
        # systemd-run waits for the initial start job, which preparar must fail.
        # Restart=always then retries that same private unit independently.
        assert corrupt_start.returncode in (0,1),corrupt_start.stderr
        emit('corrupt_initial_start',returncode=corrupt_start.returncode,stderr=corrupt_start.stderr.strip())
        wait_for(lambda:int(show('NRestarts')['NRestarts'])>=2,seconds=40)
        assert (data/'state.json').read_bytes()==b'{"tasks":'
        assert int(show('MainPID')['MainPID'])==0
        try:p.status()
        except subprocess.CalledProcessError:pass
        else:raise AssertionError('corrupt queue must never reach the private daemon')
        emit('corrupt_queue_retries_PASS',properties=show('NRestarts','MainPID','RestartUSec'),cgroup_pids=cgroup_pids())
        subprocess.run(['systemctl','--user','stop',unit],check=True,capture_output=True,text=True)
        assert cgroup_pids()==[],cgroup_pids()
        (data/'state.json').write_bytes(saved)
        emit('PASS',windows_boot_not_claimed=True)
    finally:
        (root/'release').touch();(root/'child-release').touch()
        for name in reversed(units_created):
            subprocess.run(['systemctl','--user','stop',name],check=False,capture_output=True,text=True)
            subprocess.run(['systemctl','--user','reset-failed',name],check=False,capture_output=True,text=True)
        stop_process()
        assert cgroup_pids()==[],cgroup_pids()
        listing=subprocess.check_output(['systemctl','--user','list-units','pao-u34-*','--no-legend','--no-pager'],text=True)
        own=[line for line in listing.splitlines() if any(name in line for name in [unit,guard_unit,*units_created])]
        emit('cleanup',popen_reaped=process is None or process.poll() is not None,own_units=own)
        assert not own,own
        output.close();events.close()


def worker(root,operation):
    pueue=Path.home()/'.local/bin/pueue';daemon=Path.home()/'.local/bin/pueued'
    data=root/'data';config=root/'config.json';pid=data/'pid'
    class Private(control.Pueue):
        def call(self,*args):
            return subprocess.run([str(pueue),'--config',str(config),*map(str,args)],check=True,text=True,capture_output=True,timeout=8).stdout
    engine=control.MiniControl(Private(),root/'control',host='desk')
    with control.locked(engine.directory):
        if operation=='prepare':
            configuration=control.validate_configuration(config_path=config,data=data,pid_file=pid,pueue=pueue,daemon=daemon)
            control.prepare_start(engine,data/'state.json',configuration)
            with (root/'prepare-observed').open('a') as stream:stream.write(str(time.time())+'\n')
        elif operation=='stop':engine.record_stop()
        else:
            assert (root/'prepare-observed').exists()
            state=engine.pueue.status()
            assert all(g['status']=='Paused' for g in state['groups'].values()),'guard before offline closure'
            (root/'guard-observed').touch()


if __name__=='__main__':main()
