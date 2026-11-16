"""Aceptación real: sólo --run-private mediante remote-run en el desk.

Pueue, configuración, tareas y unidad transitoria propios; nunca pueued.service.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import statistics
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1]/(name+'.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-private', required=True, action='store_true')
    parser.parse_args()
    assert sys.platform == 'linux'
    snapshot, serve = load('snapshot'), load('serve')
    root = Path(tempfile.mkdtemp(prefix='pao-orphans-private-', dir='/tmp'))
    unit = 'pao-orphans-private-'+str(os.getpid())+'.service'
    data = root/'data'
    data.mkdir()
    config = root/'config.json'
    config.write_text(json.dumps(dict(shared=dict(pueue_directory=str(data), runtime_directory=str(data),
        pid_path=str(data/'pid'), unix_socket_path=str(data/'socket'), use_unix_socket=True,
        shared_secret_path=str(data/'secret'), alias_file=str(data/'aliases.yml')),
        daemon=dict(compress_state_file=False))))
    env = dict(os.environ)
    env.pop('PUEUE_CONFIG_PATH', None)
    pueue, daemon = (Path.home()/'.local/bin'/name for name in ('pueue', 'pueued'))
    events = (root/'events.jsonl').open('a', buffering=1)
    def emit(event, **values):
        line = json.dumps(dict(event=event, **values), sort_keys=True)
        print(line, flush=True)
        events.write(line+'\n')
    def run(*argv, check=True):
        return subprocess.run(list(map(str, argv)), capture_output=True, text=True, check=check, timeout=8, env=env)
    def p(*argv):
        return run(pueue, '--config', config, *argv).stdout
    def status():
        return json.loads(p('status', '--json'))
    def show():
        return dict(line.split('=', 1) for line in run('systemctl', '--user', 'show', unit,
                    '-p', 'MainPID', '-p', 'ControlGroup').stdout.splitlines() if '=' in line)
    def wait(predicate):
        deadline = time.monotonic()+15
        while time.monotonic() < deadline:
            try:
                value = predicate()
                if value:
                    return value
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                pass
            time.sleep(.1)
        raise AssertionError('Espera de unidad privada agotada')
    def verify_main():
        pid = int(show()['MainPID'])
        assert pid > 1 and int((data/'pid').read_text()) == pid
        assert Path('/proc', str(pid), 'comm').read_text().strip() == 'pueued'
        assert unit in Path('/proc', str(pid), 'cgroup').read_text()
        return pid
    created, group = False, None
    children = []
    try:
        emit('private_start', root=str(root), unit=unit, version=run(daemon, '--version').stdout.strip())
        run('systemd-run', '--user', '--unit', unit, '--property=KillMode=control-group',
            '--property=OOMPolicy=continue', '--property=RuntimeMaxSec=90', daemon, '--config', config)
        created = True
        wait(status)
        main_pid = verify_main()
        group = Path('/sys/fs/cgroup')/show()['ControlGroup'].lstrip('/')
        p('parallel', '2')
        child = root/'child.py'
        child.write_text('import os,pathlib,sys,time\np=pathlib.Path(sys.argv[1]); tag=sys.argv[2]\n'
                         '(p/(tag+".pid")).write_text(str(os.getpid()))\nend=time.monotonic()+60\n'
                         'while not (p/"release").exists() and time.monotonic()<end:time.sleep(.05)\n')
        parent = root/'parent.py'
        parent.write_text('import pathlib,subprocess,sys,time\np=pathlib.Path(sys.argv[1]);tag=sys.argv[2]\n'
                          'child=subprocess.Popen([sys.executable,str(p/"child.py"),str(p),tag])\n'
                          'while not (p/(tag+".pid")).exists():time.sleep(.01)\n'
                          'if tag=="running":child.wait()\n')
        def add(tag):
            command = shlex.join(['exec', sys.executable, str(parent), str(root), tag])
            return re.search(r'(\d+)', p('add', '--', command))[1]
        done_id = add('orphan')
        wait(lambda: (root/'orphan.pid').exists())
        wait(lambda: 'Done' in status()['tasks'][done_id]['status'])
        assert status()['tasks'][done_id]['status']['Done']['result'] == 'Success'
        running_id = add('running')
        wait(lambda: (root/'running.pid').exists())
        assert 'Running' in status()['tasks'][running_id]['status']
        orphan_pid, running_pid = (int((root/(tag+'.pid')).read_text()) for tag in ('orphan', 'running'))
        children = [orphan_pid, running_pid]
        assert set(children) <= {int(x) for x in (group/'cgroup.procs').read_text().split()}
        fd_targets = [os.readlink(Path('/proc', str(orphan_pid), 'fd', fd)) for fd in ('1', '2')]
        assert any('/task_logs/'+done_id+'.log' in target for target in fd_targets), fd_targets
        emit('private_tasks', main_pid=main_pid, done_task=done_id, running_task=running_id,
             orphan_pid=orphan_pid, running_child=running_pid, fd_log_confirmed=True)
        wrapper = root/'pueue-private'
        wrapper.write_text('#!/bin/sh\nexec '+shlex.join([str(pueue), '--config', str(config)])+' "$@"\n')
        wrapper.chmod(0o700)
        # Se ejercita collect real; el muestreo anterior de uso y la guardia son ajenos a esta prueba.
        with patch.object(snapshot, 'collect_usage', return_value=None), patch.object(snapshot, 'desk_guard', return_value=None), \
                patch.object(snapshot, 'mini_metadata', return_value={}):
            result = snapshot.collect('linux', 'desk', pueue=str(wrapper), home=root, pueue_unit=unit)
        serve.validate_snapshot(result, 'desk')
        assert not result['errors'], result['errors']
        assert result['orphans_total'] == 1, result['orphans']
        row = result['orphans'][0]
        assert (row['pid'], row['origin_task'], row['group'], row['origin_state']) == (orphan_pid, done_id, 'default', 'Done'), row
        assert running_pid not in [r['pid'] for r in result['orphans']]
        (root/'snapshot.json').write_text(json.dumps(result, indent=2))
        costs = []
        for _ in range(7):
            started = time.monotonic()
            rows, total = snapshot.desk_orphans(status(), unit=unit)
            costs.append(time.monotonic()-started)
            assert total == 1 and rows[0]['pid'] == orphan_pid
        emit('detection_PASS', orphan=row, running_excluded=True, costs_seconds=costs,
             min_seconds=min(costs), median_seconds=statistics.median(costs), max_seconds=max(costs))
    finally:
        (root/'release').touch()
        if created:
            # Autoridad acotada a nuestro nombre y al MainPID comprobado, jamás por patrón.
            props = show()
            if int(props.get('MainPID', '0')) > 1:
                verify_main()
            assert unit.startswith('pao-orphans-private-') and unit != 'pueued.service'
            run('systemctl', '--user', 'stop', unit)
            run('systemctl', '--user', 'reset-failed', unit, check=False)
        remaining = (group/'cgroup.procs').read_text().split() if group and group.exists() else []
        listing = run('systemctl', '--user', 'list-units', unit, '--all', '--no-legend', '--no-pager').stdout.strip()
        wait(lambda: all(not Path('/proc', str(pid)).exists() for pid in children))
        emit('cleanup', pids=remaining, units=listing, children_gone=True)
        assert not remaining and not listing
        events.close()


if __name__ == '__main__':
    main()
