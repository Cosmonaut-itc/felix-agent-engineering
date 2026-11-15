"""Opt-in macOS acceptance in a remote-run directory. Never uses a shared daemon.

Every Pueue process pins --config. All launchd labels use private pao.u35 prefixes and
all plists live under this run, never the user's Library/LaunchAgents.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from unittest.mock import patch

BASE=Path(__file__).resolve().parents[1]
mini=control=None  # Workers import exclusively from the installed package once published.
LOW=dict(load=.1,memory=1)


def host_clock_ns():
    # Unlike Python 3.9's macOS monotonic epoch, this OS clock is shared by workers.
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC)


def emit(root,event,**fields):
    line=json.dumps(dict(event=event,**fields),sort_keys=True)
    with (root/'events.jsonl').open('a') as stream:stream.write(line+'\n')
    print(line,flush=True)


def context(root, role="driver"):
    global mini,control
    library=root/'.local/lib/pao-pueue-mini'
    package=library if all((library/n).is_file() for n in ('mini.py','control.py')) else root/'package'
    spec=importlib.util.spec_from_file_location('private_installed_mini',package/'mini.py')
    mini=importlib.util.module_from_spec(spec);spec.loader.exec_module(mini);control=mini.control
    info=json.loads((root/'info.json').read_text())
    prefix=info['prefix'];mini.LABEL=prefix+'.daemon';mini.GUARD=prefix+'.guard';mini.BREW=prefix+'.brew'
    assert prefix.startswith(('pao.u35.injerto.','pao.u35.r1.','pao.u35.r2.'))
    data=root/'data';config=root/'config.json';pid=data/'pid'
    env=dict(os.environ,TMPDIR=str(root));env.pop('PUEUE_CONFIG_PATH',None)
    class Private(control.Pueue):
        def call(self,*argv):
            return subprocess.run([mini.PUEUE,'--config',str(config),*map(str,argv)],check=True,
                capture_output=True,text=True,timeout=8,env=env,cwd=str(root)).stdout
    p=Private()
    configuration=control.validate_configuration(home=root,config_path=config,data=data,pid_file=pid,
        pueue=mini.PUEUE,daemon=mini.DAEMON,env={},cwd=root)
    m=mini.Manager(home=root,pueue=p,directory=root/'control',configuration=configuration)
    m.agents=root/'agents'
    def preflight(target):
        # Real private paths differ from the production preflight's fixed Homebrew plist.
        # Its conservative production validation is covered separately by the doubles.
        control.start_projection(m.engine,configuration,allow_pending=False)
        state=m.state();m.transaction(state)
        source=mini.LABEL if m.loaded(mini.LABEL) is not None else mini.BREW
        identity=m.verify_owner(source,state);m.engine.view()
        return state,configuration,{label:m.disabled(label) for label in (mini.BREW,mini.LABEL,mini.GUARD)},identity
    m.preflight=preflight
    def checkpoint(phase):
        if phase.startswith('effect:bootout:') and phase.endswith(('.brew','.daemon')):
            (root/'offline-since').write_text(str(host_clock_ns()))
        if phase=='handoff:complete' and (root/'offline-since').exists():
            elapsed=(host_clock_ns()-int((root/'offline-since').read_text()))/1e9
            assert elapsed>=0,'shared monotonic clock moved backwards'
            emit(root,'handoff_verified',seconds_since_bootout=round(elapsed,3))
        requested=root/'crash-phase'
        if requested.exists() and requested.read_text().strip()==phase:
            requested.rename(root/'crash-consumed')
            emit(root,'process_crash',phase=phase,role=role)
            os._exit(137)
        if phase=='start:prepared':
            queue=control.read_queue(data/'state.json')
            assert all(g['status']=='Paused' for g in queue['groups'].values())
            emit(root,'prepared_before_exec',closed=sorted(queue['groups']))
    m.checkpoint=checkpoint
    return m,p,env,configuration


def worker(root,operation):
    m,p,env,configuration=context(root,operation)
    if operation in ('install','withdraw'):
        m.change(operation)
        return
    if operation=='start':
        def execute(binary,argv):
            assert binary==mini.DAEMON and argv==[mini.DAEMON]
            os.execve(binary,[binary,'--config',str(root/'config.json')],env)
        m.start(execute=execute)
    else:
        with control.locked(m.directory,create=False):
            if m._recover(configuration):
                with patch.object(control,'pressure_mini',return_value=LOW):m.engine.guard(LOW)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-private',action='store_true',required=True)
    parser.add_argument('--worker',choices=('start','guard','install','withdraw'))
    parser.add_argument('--root',type=Path)
    parser.add_argument('--r1',action='store_true',help='Directed abort/restart and bounded diagnostic acceptance')
    parser.add_argument('--r2',action='store_true',help='Opposite abort convergence plus R1 acceptance')
    args=parser.parse_args()
    args.r1 = args.r1 or args.r2
    if args.worker:return worker(args.root,args.worker)
    assert sys.platform=='darwin','macOS required'
    ident=(Path.cwd()/'.remote-run.lock').read_text().strip()
    assert re.fullmatch(r'[0-9]{8}-[0-9]{6}-[0-9a-f]{6}',ident)
    root=Path.home()/'remote-run/runs'/ident/('u35-r2' if args.r2 else 'u35-r1' if args.r1 else 'u35-ij');root.mkdir()
    (root/'agents').mkdir();data=root/'data';data.mkdir()
    assert len(str(data/'socket').encode())<104
    prefix=('pao.u35.r2.' if args.r2 else 'pao.u35.r1.' if args.r1 else 'pao.u35.injerto.')+str(os.getpid())
    (root/'info.json').write_text(json.dumps(dict(prefix=prefix)))
    (root/'config.json').write_text(json.dumps(dict(shared=dict(pueue_directory=str(data),runtime_directory=str(data),
        pid_path=str(data/'pid'),unix_socket_path=str(data/'socket'),use_unix_socket=True,
        shared_secret_path=str(data/'secret'),alias_file=str(data/'aliases.yml')),daemon=dict(compress_state_file=False))))
    package=root/'package';(package/'launchagents').mkdir(parents=True)
    for name in ('mini.py','control.py'):shutil.copyfile(BASE/name,package/name)
    m,p,env,configuration=context(root)
    library=m.library;library.mkdir(parents=True)
    installed_driver=library/'private_handoff.py'
    shutil.copyfile(Path(__file__).resolve(),installed_driver)
    labels=[mini.BREW,mini.LABEL,mini.GUARD]
    script=installed_driver;python='/usr/bin/python3'
    for label,operation in ((mini.BREW,None),(mini.LABEL,'start'),(mini.GUARD,'guard')):
        template=plistlib.loads((BASE/'launchagents'/('local.pao.pueue-mini.guard.plist' if operation=='guard' else 'local.pao.pueue-mini.daemon.plist')).read_bytes())
        template.update(Label=label,WorkingDirectory=str(root),StandardOutPath=str(root/(label+'.out')),
                        StandardErrorPath=str(root/(label+'.err')))
        template['ProgramArguments']=([mini.DAEMON,'--config',str(root/'config.json')] if operation is None else
            [python,'-I',str(script),'--run-private','--root',str(root),'--worker',operation])
        if args.r1 and operation is None:
            template['ProgramArguments'].append('--verbose')
            template['WorkingDirectory']='/opt/homebrew/var'
        if args.r1 and operation=='start':
            # Exercise the delivered /dev/null sinks: only the safe, bounded
            # preparer diagnostic may contain the blocked-start reason.
            template.update(StandardOutPath='/dev/null',StandardErrorPath='/dev/null')
        if operation=='guard':template['StartInterval']=2  # accelerated private clock; delivered interval is asserted as 60
        rendered=plistlib.dumps(template)
        (m.agents/(label+'.plist')).write_bytes(rendered)
        if operation is not None:(package/'launchagents'/(label+'.plist')).write_bytes(rendered)
    process=None;output=(root/'direct-daemon.log').open('a');created=[]
    def wait_for(predicate,seconds=35):
        until=time.monotonic()+seconds
        while time.monotonic()<until:
            try:
                value=predicate()
                if value:return value
            except (OSError,ValueError,subprocess.SubprocessError):pass
            time.sleep(.15)
        raise AssertionError('private condition timed out')
    def verified_pid():
        pid=int((data/'pid').read_text().strip());assert pid>1
        comm=subprocess.check_output(['/bin/ps','-p',str(pid),'-o','comm='],text=True).strip()
        argv=subprocess.check_output(['/bin/ps','-p',str(pid),'-o','command='],text=True).strip()
        assert Path(comm).name=='pueued'
        assert '--config '+str(root/'config.json') in argv
        return pid
    def stop_direct():
        if process is not None and process.poll() is None:
            assert verified_pid()==process.pid
            process.kill();process.wait(timeout=5)
    def load(label):
        created.append(label)
        def attempt():
            result=mini.launchctl('bootstrap',m.domain,str(m.agents/(label+'.plist')),check=False)
            if result.returncode==0:return True
            if result.returncode==5 and m.loaded(label) is None:return False
            raise subprocess.CalledProcessError(result.returncode,['private-launchctl','bootstrap'])
        wait_for(attempt)
    def settled(phase):return m.state().get('mini_handoff',{}).get('phase')==phase
    def status(ident):return p.status()['tasks'][str(ident)]['status']
    def add(command,group='default',stashed=False):
        argv=['add','--print-task-id','--group',group]
        if stashed:argv+=['--stashed']
        return int(p.call(*argv,'--',command).strip())
    try:
        emit(root,'runtime',python=sys.version.split()[0],daemon=subprocess.check_output([mini.DAEMON,'--version'],text=True).strip())
        process=subprocess.Popen([mini.DAEMON,'--config',str(root/'config.json')],env=env,cwd=str(root),stdout=output,stderr=output)
        wait_for(lambda:p.status())
        for g,n in control.MINI_PROFILE.items():
            if g=='default':p.parallel(g,n)
            else:p.call('group','add',g,'--parallel',n)
        p.call('pause','--wait','-g','suites')
        with control.locked(m.directory):m.engine.adopt(data/'pid')
        queued=add('/usr/bin/true',group='suites')
        stashed=add('/usr/bin/true',group='rapidas',stashed=True)
        p.call('pause','--wait','-g','suites')  # save fixture
        stop_direct()
        if (data/'pid').exists():(data/'pid').unlink()  # only our verified, reaped Popen
        load(mini.BREW);wait_for(lambda:p.status())
        load(mini.GUARD)
        created.append(mini.LABEL)  # may be bootstrapped by recovery, always clean the exact label
        start=time.monotonic()
        (root/'crash-phase').write_text('handoff:intent')
        requester=subprocess.run([python,'-I',str(installed_driver),'--run-private','--root',str(root),'--worker','install'],
                                 env=env,cwd=str(root),timeout=30)
        assert requester.returncode==137,'requester did not die after durable intent'
        assert (root/'crash-consumed').read_text()=='handoff:intent'
        (root/'crash-consumed').unlink()
        # The requester and the source package are gone. Only installed code and
        # templates remain available to the real periodic launchd job.
        package.rename(root/'source-unavailable')
        m,p,env,configuration=context(root)
        assert mini.ROOT==library.resolve()
        wait_for(lambda:settled('done'));wait_for(lambda:verified_pid())
        assert status(queued).get('Queued') is not None
        assert 'Stashed' in status(stashed)
        assert p.status()['groups']['suites']['status']=='Paused'
        emit(root,'installed',elapsed_seconds=round(time.monotonic()-start,3),one_owner=m.loaded(mini.BREW) is None,
             queued_id=queued,stashed_id=stashed,manual_preserved=True,installed_package=str(library),source_unavailable=True)
        # Equivalent service reconstruction: every private plist participates,
        # including Homebrew, and real launchd retains its disable overrides.
        with control.locked(m.directory):
            for label in (mini.GUARD,mini.LABEL,mini.BREW):
                mini.launchctl('bootout',m.domain+'/'+label,check=False)
            wait_for(lambda:all(m.loaded(label) is None for label in labels))
            def offline():
                control.assert_offline(p,configuration['pid_file'])
                return True
            wait_for(offline)
            for label in labels:
                if not m.disabled(label):load(label)
            assert sum(m.loaded(label) is not None for label in (mini.BREW,mini.LABEL))==1
        wait_for(lambda:p.status())
        assert m.loaded(mini.BREW) is None and m.disabled(mini.BREW)
        emit(root,'login_reconstruction',one_enabled_daemon=True,homebrew_disabled=True)
        if args.r1:
            if args.r2:
                for first in ('withdraw','install'):
                    if first=='install':
                        m.change('withdraw');wait_for(lambda:settled('withdrawn'));wait_for(lambda:p.status())
                    expected=mini.LABEL if first=='withdraw' else mini.BREW
                    owner='prepared' if first=='withdraw' else 'homebrew'
                    launches=root/('launches-'+first)
                    program="import pathlib,time;pathlib.Path("+repr(str(launches))+").open('a').write('launch\\n');time.sleep(90)"
                    active=add(shlex.join([python,'-c',program]));wait_for(lambda:'Running' in status(active))
                    old=verified_pid()
                    m.change(first,wait=1);wait_for(lambda:settled('aborted'))
                    value=m.state()['mini_handoff']
                    assert value['source']['owner']==owner and value['source']['pid']==old
                    assert value['retained_owner']==owner and 'Running' in status(active)
                    second='install' if first=='withdraw' else 'withdraw'
                    phase='done' if first=='withdraw' else 'withdrawn'
                    m.change(second,wait=1);time.sleep(1.1)
                    assert settled(phase) and verified_pid()==old and 'Running' in status(active)
                    assert m.state()['mini_handoff']['retained_owner']==owner
                    assert not m.disabled(expected)
                    assert m.disabled(mini.BREW if expected==mini.LABEL else mini.LABEL)
                    assert launches.read_text().splitlines()==['launch']
                    emit(root,'r2_opposite_converged',first=first,phase=phase,owner=owner,
                         same_pid=True,same_running_task=True,launches=1,owner_enabled=True)
                    m.maintenance();m.maintenance(resume=True)
                    wait_for(lambda:settled(phase));wait_for(lambda:p.status())
                    assert m.loaded(expected) is not None
                    assert status(active).get('Done',{}).get('result')=='Killed'
                    # Reconstruct from enabled plists, as a new session does.
                    with control.locked(m.directory):
                        for label in (mini.GUARD,mini.LABEL,mini.BREW):
                            mini.launchctl('bootout',m.domain+'/'+label,check=False)
                        wait_for(offline)
                        for label in labels:
                            if not m.disabled(label):load(label)
                    wait_for(lambda:p.status())
                    assert m.loaded(expected) is not None
                    assert sum(m.loaded(label) is not None for label in (mini.BREW,mini.LABEL))==1
                    assert launches.read_text().splitlines()==['launch']
                    assert p.status()['groups']['suites']['status']=='Paused'
                    emit(root,'r2_opposite_maintenance_login',first=first,owner=owner,online=True,
                         phase=phase,no_retry=True,manual_preserved=True)
                m.change('install');wait_for(lambda:settled('done'));wait_for(lambda:p.status())
            def assert_prepared_owner():
                wait_for(lambda:p.status());wait_for(lambda:m.state().get('startup',{}).get('phase')=='ready')
                assert m.loaded(mini.LABEL) is not None and m.loaded(mini.BREW) is None
                assert m.disabled(mini.BREW) and not m.disabled(mini.LABEL)
                assert p.status()['groups']['suites']['status']=='Paused'
                assert {g:p.status()['groups'][g]['parallel_tasks'] for g in control.MINI_GROUPS}==control.MINI_PROFILE
            active=add('/bin/sleep 5');wait_for(lambda:'Running' in status(active))
            m.change('withdraw',wait=1);wait_for(lambda:settled('aborted'))
            assert m.state()['mini_handoff']['retained_owner']=='prepared'
            old=verified_pid();os.kill(old,signal.SIGKILL)
            wait_for(lambda:verified_pid()!=old);assert_prepared_owner()
            # The drain/launchd retries may outlast this bounded task. A task
            # already completed before the stop must retain Success, not acquire
            # Killed merely to satisfy the restart fixture.
            result=status(active).get('Done',{}).get('result')
            assert result in ('Killed','Success')
            emit(root,'r1_aborted_withdraw_keepalive',prepared_owner=True,brew_disabled=True,
                 manual_preserved=True,task_result=result)
            # Same enabled-plist reconstruction after an aborted withdrawal.
            with control.locked(m.directory):
                for label in (mini.GUARD,mini.LABEL,mini.BREW):
                    mini.launchctl('bootout',m.domain+'/'+label,check=False)
                wait_for(offline)
                for label in labels:
                    if not m.disabled(label):load(label)
            assert_prepared_owner()
            emit(root,'r1_aborted_withdraw_login',prepared_owner=True,online=True)
            m.maintenance();m.maintenance(resume=True);wait_for(lambda:settled('done'));assert_prepared_owner()
            emit(root,'r1_aborted_withdraw_maintenance',same_owner=True)
            # A malformed queue blocks the actual launchd MainPID. KeepAlive
            # retries with both stdout/stderr discarded, retaining a safe reason.
            with control.locked(m.directory):
                mini.launchctl('disable',m.domain+'/'+mini.GUARD)
                mini.launchctl('bootout',m.domain+'/'+mini.GUARD)
                mini.launchctl('bootout',m.domain+'/'+mini.LABEL)
                wait_for(offline)
                queue_path=data/'state.json';before=queue_path.read_bytes()
                queue_path.write_text('{"invalid":')
                load(mini.LABEL)
            log=m.directory/'arranque.log'
            wait_for(lambda:log.exists() and any(json.loads(line)['motivo']=='Cola JSON ilegible'
                                                for line in log.read_text().splitlines()))
            assert queue_path.read_text()=='{"invalid":','blocked start replaced corrupt queue'
            assert log.stat().st_size<=65536 and log.stat().st_mode & 0o777==0o600
            time.sleep(11)  # one real ThrottleInterval retry, never a rapid loop
            assert len(log.read_text().splitlines())>=2
            assert not any('cmd' in line or 'envs' in line for line in log.read_text().splitlines())
            emit(root,'r1_blocked_start_diagnostic',reason='Cola JSON ilegible',mode='0600',bounded=True,
                 retries=len(log.read_text().splitlines()),raw_daemon_output_discarded=True)
            shutil.copyfile(log,Path.cwd()/'private-u35-r1-arranque.log')
            queue_path.write_bytes(before)
            mini.launchctl('enable',m.domain+'/'+mini.GUARD);load(mini.GUARD)
            assert_prepared_owner()
            # The symmetric maintenance path must restore Homebrew after an
            # aborted installation, never finish that installation implicitly.
            m.change('withdraw');wait_for(lambda:settled('withdrawn'));wait_for(lambda:p.status())
            active=add('/bin/sleep 5');wait_for(lambda:'Running' in status(active))
            m.change('install',wait=1);wait_for(lambda:settled('aborted'))
            assert m.state()['mini_handoff']['retained_owner']=='homebrew'
            m.maintenance();m.maintenance(resume=True);wait_for(lambda:settled('withdrawn'));wait_for(lambda:p.status())
            assert m.loaded(mini.BREW) is not None and m.loaded(mini.LABEL) is None
            assert p.status()['groups']['suites']['status']=='Paused'
            emit(root,'r1_aborted_install_maintenance',same_owner=True,brew_verbose_cwd_private_config=True)
            emit(root,'PASS',assertions='R1 abort KeepAlive/login; maintenance both owners; safe diagnostic with real launchd retries')
            return
        # A killed standard daemon is restarted through the fixed offline preparation.
        program="import os,pathlib,time;pathlib.Path("+repr(str(root/'child.pid'))+").write_text(str(os.getpid()));time.sleep(20)"
        running=add(shlex.join(['/usr/bin/python3','-c',program]))
        wait_for(lambda:'Running' in status(running))
        waiting=add('/usr/bin/true')
        p.call('pause','--wait','-g','rapidas')  # persist Running + Queued without pausing default
        old=verified_pid();os.kill(old,signal.SIGKILL)
        wait_for(lambda:verified_pid()!=old)
        wait_for(lambda:status(running).get('Done',{}).get('result')=='Killed')
        wait_for(lambda:status(waiting).get('Done',{}).get('result')=='Success')
        reason=m.state()['startup']['interruptions'][str(running)]['message']
        assert reason==control.INTERRUPTED
        assert p.status()['groups']['suites']['status']=='Paused'
        emit(root,'keepalive_sigkill',interrupted_id=running,queued_id=waiting,reason=reason,manual_preserved=True)
        # External SIGTERM, without record_stop: terminal, never retried and no
        # invented interruption attribution (C-W1 is an accepted owner limit).
        external=add('/bin/sleep 20')
        wait_for(lambda:'Running' in status(external))
        p.call('pause','--wait','-g','rapidas')
        old=verified_pid();os.kill(old,signal.SIGTERM)
        wait_for(lambda:verified_pid()!=old);wait_for(lambda:p.status())
        wait_for(lambda:status(external).get('Done',{}).get('result')=='Killed')
        assert str(external) not in m.state()['startup']['interruptions']
        emit(root,'external_sigterm_negative',terminal_id=external,no_retry=True,no_invented_reason=True)
        # Successful stock shutdown also re-enters KeepAlive, not only failures.
        old=verified_pid()
        with control.locked(m.directory):m.engine.record_stop()
        p.call('shutdown');wait_for(lambda:verified_pid()!=old)
        wait_for(lambda:p.status())
        wait_for(lambda:m.state().get('startup',{}).get('phase')=='ready')
        emit(root,'keepalive_successful_exit',restarted=True)
        # Maintenance blocks both the fixed wrapper and the existing recovery/sensor job.
        m.maintenance()
        time.sleep(12)
        try:p.status()
        except subprocess.CalledProcessError:pass
        else:raise AssertionError('daemon reopened during maintenance')
        m.maintenance(resume=True);wait_for(lambda:p.status());wait_for(lambda:settled('done'))
        emit(root,'maintenance',no_reopen=True)
        # Requester dies at the critical bootout→bootstrap gap; only the periodic job finishes.
        (root/'crash-phase').write_text('effect:bootout:'+mini.LABEL)
        requester=subprocess.run([python,'-I',str(installed_driver),'--run-private','--root',str(root),'--worker','withdraw'],
                                 env=env,cwd=str(root),timeout=30)
        assert requester.returncode==137,'requester did not die at bootout'
        assert (root/'crash-consumed').read_text()=='effect:bootout:'+mini.LABEL
        (root/'crash-consumed').unlink()
        wait_for(lambda:settled('withdrawn'));wait_for(lambda:p.status())
        emit(root,'recover_after_bootout',source=m.loaded(mini.LABEL) is None,brew_restored=m.loaded(mini.BREW) is not None)
        # The job itself dies after bootout during installation; its NEXT tick finishes.
        (root/'crash-phase').write_text('effect:bootout:'+mini.BREW)
        class RequesterDied(BaseException):pass
        def leave_for_job(phase):
            if phase=='handoff:drain':raise RequesterDied()
        m.checkpoint=leave_for_job
        try:m.change('install')
        except RequesterDied:pass
        wait_for(lambda:(root/'crash-consumed').exists())
        wait_for(lambda:settled('done'));wait_for(lambda:p.status())
        assert p.status()['groups']['suites']['status']=='Paused'
        emit(root,'recover_after_job_death',one_owner=m.loaded(mini.BREW) is None,manual_preserved=True)
        emit(root,'PASS',assertions='stock restore; fixed prepare before exec; KeepAlive success/failure; maintenance; requester and job death at bootout gap')
    finally:
        # Mark first so an in-flight own guard cannot start another private instance.
        if m.directory.exists():(m.directory/'maintenance').touch(exist_ok=True)
        for label in (mini.GUARD,mini.LABEL,mini.BREW):
            if label not in created:continue
            assert label.startswith(prefix+'.')
            mini.launchctl('disable',m.domain+'/'+label,check=False)
            mini.launchctl('bootout',m.domain+'/'+label,check=False)
        stop_direct()
        # A daemon surviving bootout is addressed ONLY through its private, checked PID/config.
        if (data/'pid').exists():
            try:
                pid=verified_pid();os.kill(pid,signal.SIGTERM)
                wait_for(lambda:subprocess.run(['/bin/ps','-p',str(pid),'-o','comm='],capture_output=True).returncode==1,8)
            except subprocess.CalledProcessError:pass
        if (root/'child.pid').exists():
            child=int((root/'child.pid').read_text());assert child>1
            # Its program has a 20 s bound; wait for its exact PID, never kill by name.
            wait_for(lambda:subprocess.run(['/bin/ps','-p',str(child),'-o','comm='],capture_output=True).returncode==1,22)
        for label in labels:
            assert mini.launchctl('print',m.domain+'/'+label,check=False).returncode==113,'private service still loaded'
        listing=mini.launchctl('print',m.domain).stdout
        # macOS also lists persistent disable overrides here; these aren't loaded jobs.
        loaded_lines=[line for line in listing.splitlines() if prefix in line and '=>' not in line]
        assert not loaded_lines,'private LaunchAgent survived cleanup'
        # Bounded synthetic child is allowed to exit naturally; no process-name killing.
        time.sleep(1)
        emit(root,'cleanup',no_private_launchagents=True,no_private_daemon=True,prefix=prefix)
        shutil.copyfile(root/'events.jsonl',Path.cwd()/'private-u35-events.jsonl')
        for label in labels:
            error=root/(label+'.err')
            if error.exists() and error.stat().st_size:
                # Only our private worker tracebacks, never production output or task/env dumps.
                print('PRIVATE WORKER '+label+'\n'+error.read_text()[-5000:],flush=True)
        output.close()


if __name__=='__main__':main()
