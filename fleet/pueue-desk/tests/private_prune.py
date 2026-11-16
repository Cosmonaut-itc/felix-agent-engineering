"""SEN-6495 M1–M5. Ejecutar sólo con remote-run y --run-private.

Todos los clientes llevan --config explícito; no se consulta la instancia real.
Conserva el directorio temporal y termina sólo el Popen verificado.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-private', required=True, action='store_true')
    parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='sen6495-private-'))
    config = root/'config.json'
    config.write_text(json.dumps(dict(shared=dict(
        pueue_directory=str(root/'data'), runtime_directory=str(root/'run'),
        pid_path=str(root/'pid'), unix_socket_path=str(root/'socket'),
        use_unix_socket=True, alias_file=str(root/'aliases.yml'),
        shared_secret_path=str(root/'secret')), daemon=dict(compress_state_file=False))))
    (root/'data').mkdir()
    (root/'run').mkdir()
    env = dict(PATH='/usr/bin:/bin', HOME=str(root), TMPDIR=str(root), LANG='C.UTF-8')
    binary = str(Path.home()/'.local/bin/pueue')
    daemon_binary = str(Path.home()/'.local/bin/pueued')
    spec = importlib.util.spec_from_file_location('control', Path(__file__).resolve().parents[1]/'control.py')
    control = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(control)
    def raw(*args):
        return subprocess.run([binary, '--config', str(config), *map(str,args)],
                              text=True, capture_output=True, timeout=30, env=env, cwd=root)
    class Private(control.Pueue):
        def call(self, *args):
            result=raw(*args)
            result.check_returncode()
            return result.stdout
    p=Private()
    def emit(label, **fields):
        print(label+' '+json.dumps(fields, ensure_ascii=False, sort_keys=True), flush=True)
    def literal(label, *args):
        result=raw(*args)
        emit(label, stdout=result.stdout, stderr=result.stderr, code=result.returncode)
        return result
    def wait(predicate, seconds=120):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            try:
                if predicate():return
            except (subprocess.SubprocessError, OSError, ValueError):pass
            time.sleep(.1)
        raise AssertionError('private condition timeout')
    def add(command='true', *args):
        return p.call('add', '--print-task-id', *args, '--', command).strip()
    def terminal(ident):
        return 'Done' in p.status()['tasks'][ident]['status']
    output=(root/'daemon.log').open('a')
    daemon=subprocess.Popen([daemon_binary,'--config',str(config)], env=env, cwd=root, stdout=output, stderr=output)
    try:
        wait(lambda: (root/'pid').exists() and p.status())
        assert (root/'pid').read_text().strip()==str(daemon.pid)
        assert Path('/proc',str(daemon.pid),'comm').read_text().strip()=='pueued'
        assert p.call('--version').strip()=='pueue 4.0.4'
        emit('private', directory=str(root), version=p.call('--version').strip())
        ident=add('printf sen6495')
        wait(lambda: terminal(ident))
        logfile=root/'data/task_logs'/f'{ident}.log'
        emit('M1_before', log_exists=logfile.exists())
        result=literal('M1_remove','remove',ident)
        assert result.returncode==0
        emit('M1_after', log_exists=logfile.exists())
        literal('M2_wait_missing','wait',ident)
        parent=add()
        wait(lambda: terminal(parent))
        p.call('pause','-g','default')
        child=add('true','--after',parent)
        emit('M3_before', parent=parent, child=child, state=p.status()['tasks'][child]['status'])
        literal('M3_remove_dependency','remove',parent)
        task=p.status()['tasks'][child]
        emit('M3_after', parent_present=parent in p.status()['tasks'], dependencies=task['dependencies'], state=task['status'])
        p.call('start','-g','default')
        wait(lambda: terminal(child))
        emit('M3_after_resume', state=p.status()['tasks'][child]['status'])
        first=add();second=add()
        wait(lambda: terminal(first) and terminal(second))
        literal('M4_missing_mix','remove',first,999999999,second)
        snapshot=p.status()['tasks']
        emit('M4_missing_after', first_present=first in snapshot, second_present=second in snapshot)
        running=add('sleep 120')
        wait(lambda: 'Running' in p.status()['tasks'][running]['status'])
        first=add();second=add()
        # Raise parallelism so Done can coexist with Running.
        p.call('parallel',4)
        wait(lambda: terminal(first) and terminal(second))
        literal('M4_running_mix','remove',first,running,second)
        snapshot=p.status()['tasks']
        emit('M4_running_after', first_present=first in snapshot, second_present=second in snapshot,
             running_state=snapshot[running]['status'])
        p.call('kill',running)
        wait(lambda: terminal(running))
        p.call('clean')
        assert not p.status()['tasks']
        p.call('parallel',32)
        started=time.monotonic()
        with ThreadPoolExecutor(max_workers=8) as pool:
            ids=set(pool.map(lambda _:add(), range(2000)))
        wait(lambda: len(p.status()['tasks'])==2000 and all('Done' in t['status'] for t in p.status()['tasks'].values()), seconds=240)
        emit('M5_created', count=len(ids), elapsed_seconds=round(time.monotonic()-started,3))
        def measure(label):
            sizes=[];ms=[]
            for _ in range(10):
                start=time.perf_counter_ns()
                data=p.call('status','--json')
                ms.append(round((time.perf_counter_ns()-start)/1e6,3))
                sizes.append(len(data.encode()))
            emit(label, bytes=sizes, milliseconds=ms, mean_ms=round(sum(ms)/10,3))
        measure('M5_before')
        directory=root/'control'
        directory.mkdir()
        now=time.time()+8*86400
        removed=set()
        for index in range(4):
            before=p.status()['tasks']
            selected,counts=control.select_prune(before, now)
            assert len(selected)==500
            control.prune_history(p,directory,now=now+index)
            after=p.status()['tasks']
            actual=set(before)-set(after)
            assert actual==set(selected), 'actual removals differ from selection'
            removed.update(actual)
            emit('M5_pass', number=index+1, selected=len(selected), removed=len(actual), exact=True,
                 log=json.loads((directory/'guardia.log').read_text().splitlines()[-1]))
        assert removed==ids and not p.status()['tasks']
        measure('M5_after')
        emit('M5_exact', removed=len(removed), selected_equals_removed=True)
    finally:
        if daemon.poll() is None:
            cmdline=Path('/proc',str(daemon.pid),'cmdline').read_bytes().split(b'\0')
            assert str(config).encode() in cmdline
            assert Path('/proc',str(daemon.pid),'comm').read_text().strip()=='pueued'
            daemon.terminate()
            try:daemon.wait(timeout=10)
            except subprocess.TimeoutExpired:
                daemon.kill();daemon.wait(timeout=10)
        output.close()
        emit('cleanup', daemon_reaped=daemon.poll() is not None)


if __name__=='__main__':main()
