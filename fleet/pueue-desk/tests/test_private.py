"""U3.4 acceptance. Only --run-private inside an authorized desk remote-run.

Never imports HOME config or starts production units. Every Pueue argv pins
--config. Transient services use only pao-u34-sol-* and are stopped in finally.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shlex
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
    root=Path.cwd()/'private-u34-sol';root.mkdir()
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
    process=None;unit='pao-u34-sol-'+str(os.getpid())+'.service';guard_unit=unit.replace('.service','-guard.service')
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
            with patch.object(engine,'admission_ready',return_value=False):engine.guard(dict(io=0,cpu=0,memory=64*1024**3))
            assert 'Queued' in p.status()['tasks'][queued_id]['status']
            with patch.object(engine,'admission_ready',return_value=True):engine.guard(dict(io=0,cpu=0,memory=64*1024**3))
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
        unitfile=root/'unit.txt'
        unitfile.write_text('[Unit]\nStartLimitIntervalSec=0\n[Service]\nExecStartPre='+pre+'\nExecStart='+shlex.join(start_command)+'\nExecStop=-'+stop+'\nRestart=always\nRestartSec=1\nKillMode=mixed\n')
        # systemd-run accepts ExecStartPre as a service property. All units transient.
        units_created.append(unit)
        subprocess.run(['systemd-run','--user','--unit',unit,'--property=Restart=always','--property=RestartSec=1',
                        '--property=StartLimitIntervalSec=0','--property=KillMode=mixed','--property=ExecStartPre='+pre,
                        '--property=ExecStop='+stop,*start_command],check=True,capture_output=True,text=True)
        wait_for(lambda:p.status())
        first_pid=int(pid.read_text());assert first_pid>1
        # Guard is ordered After/Requires this private service, never a real unit.
        units_created.append(guard_unit)
        subprocess.run(['systemd-run','--user','--unit',guard_unit,'--property=Type=oneshot',
                        '--property=After='+unit,'--property=Requires='+unit,
                        *worker_base,'--unit-worker','guard'],check=True,capture_output=True,text=True)
        wait_for(lambda:(root/'guard-observed').exists())
        assert (root/'prepare-observed').exists()
        # Graceful stock shutdown exits successfully; Restart=always must relaunch.
        p.call('shutdown')
        wait_for(lambda:pid.exists() and int(pid.read_text())!=first_pid)
        second_pid=int(pid.read_text());wait_for(lambda:p.status())
        assert second_pid!=first_pid
        properties=subprocess.check_output(['systemctl','--user','show',unit,'-p','MainPID','-p','NRestarts','-p','Restart','-p','KillMode'],text=True)
        emit('systemd_PASS',pre_daemon_guard_order=True,successful_exit_restarted=True,properties=properties.strip())
        # A completed transient oneshot is already garbage-collected by systemd.
        subprocess.run(['systemctl','--user','stop',guard_unit],check=False,capture_output=True,text=True)
        subprocess.run(['systemctl','--user','stop',unit],check=True,capture_output=True,text=True)
        wait_for(lambda:not pid.exists() or subprocess.run(['/bin/ps','-p',pid.read_text().strip(),'-o','comm='],capture_output=True).returncode==1)
        emit('PASS',windows_boot_not_claimed=True)
    finally:
        (root/'release').touch()
        for name in reversed(units_created):
            subprocess.run(['systemctl','--user','stop',name],check=False,capture_output=True,text=True)
            subprocess.run(['systemctl','--user','reset-failed',name],check=False,capture_output=True,text=True)
        stop_process()
        listing=subprocess.check_output(['systemctl','--user','list-units','pao-u34-*','--no-legend','--no-pager'],text=True)
        own=[line for line in listing.splitlines() if unit in line or guard_unit in line]
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
