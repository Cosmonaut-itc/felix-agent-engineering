"""SEN-6417 on the desk, ONLY via remote-run: a private pueued started here with --config.

Proves the Pueue 4.0.4 fact the trickle relies on: `pueue start <id>` starts that task while
its group stays Paused, and a second one exceeds the group's parallel limit. Then, optionally,
the real guard against the same private daemon with simulated pressure admits exactly one.

Never reads the real pueued, its pid file, guardia.log or `pueue status`. Every client call has
a minimal environment, so task `envs` hold nothing secret; status JSON is never printed.
The private daemon is stopped by the PID of our own Popen, never by name.
Exit: 0 all checks, 1 the pueue start fact failed, 2 only the optional guard check failed.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

GIB = 1024 ** 3
HIGH = dict(io=0, cpu=40, memory=20 * GIB)
MID = dict(io=0, cpu=22, memory=20 * GIB)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-private', action='store_true', required=True)
    args = parser.parse_args()
    home = Path.home()
    pueue_bin, daemon_bin = str(home / '.local/bin/pueue'), str(home / '.local/bin/pueued')
    base = Path.cwd().resolve()
    root = base / 'private-sen-6417-opus'
    root.mkdir()  # refuse reuse
    config = root / 'config.json'
    config.write_text(json.dumps(dict(shared=dict(
        pueue_directory=str(root), runtime_directory=str(root), pid_path=str(root / 'pid'),
        unix_socket_path=str(base / 's6417.sock'), use_unix_socket=True,
        alias_file=str(root / 'aliases.yml'), shared_secret_path=str(root / 'secret')),
        daemon=dict(compress_state_file=False))))
    env = dict(PATH='/usr/bin:/bin', HOME=str(root), TMPDIR=str(root), PUEUE_CONFIG_PATH=str(config), LANG='C.UTF-8')
    spec = importlib.util.spec_from_file_location('candidate', str(Path(__file__).resolve().parents[1] / 'control.py'))
    control = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(control)
    events = (root / 'events.jsonl').open('w', buffering=1)

    def emit(event, **fields):
        line = json.dumps(dict(event=event, **fields), sort_keys=True, ensure_ascii=False)
        print(line, flush=True)
        events.write(line + '\n')

    class Private(control.Pueue):
        def call(self, *argv):
            return subprocess.run([pueue_bin, '--config', str(config), *map(str, argv)], check=True,
                                  capture_output=True, text=True, timeout=8, env=env, cwd=str(root)).stdout

    pueue = Private()

    def shape():
        """Only group states and task state names; never commands, paths or envs."""
        data = pueue.status()
        return ({g: v['status'] for g, v in data['groups'].items()},
                {int(k): (t['group'], next(iter(t['status'])) if isinstance(t['status'], dict) else t['status'])
                 for k, t in data['tasks'].items()})

    def wait_for(predicate, seconds=10):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            try:
                value = predicate()
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                value = None  # the daemon socket may not be ready yet
            if value:
                return value
            time.sleep(.1)
        return None

    output = (root / 'daemon.log').open('a')
    daemon = subprocess.Popen([daemon_bin, '--config', str(config)], cwd=str(root), env=env, stdout=output, stderr=output)
    code = 0
    try:
        emit('version', pueue=pueue.call('--version').strip())
        assert wait_for(lambda: daemon.poll() is None and (root / 'pid').exists() and shape()), 'private daemon unavailable'
        assert (root / 'pid').read_text().strip() == str(daemon.pid), 'pid file is not our Popen'
        assert Path('/proc', str(daemon.pid), 'comm').read_text().strip() == 'pueued'
        emit('daemon', pid=daemon.pid, verified='Popen + pid file + /proc comm')
        try:
            fact(pueue, shape, wait_for, emit)
        except AssertionError as error:
            emit('fact_failed', detail=str(error))
            return 1
        try:
            guard_scenario(control, pueue, root, shape, emit)
        except Exception as error:  # optional: report, never mask the fact above
            emit('guard_optional_failed', error=type(error).__name__, detail=str(error)[:300])
            code = 2
        return code
    finally:
        try:
            pueue.call('kill', '--all')  # only tasks of THIS private daemon (explicit --config)
        except (OSError, subprocess.SubprocessError):
            pass
        if daemon.poll() is None:
            daemon.terminate()
            try:
                daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait(timeout=5)
        emit('cleanup', daemon_reaped=daemon.poll() is not None, returncode=daemon.returncode)
        output.close()
        events.close()


def fact(pueue, shape, wait_for, emit):
    pueue.call('group', 'add', 'solo', '--parallel', 1)
    pueue.call('pause', '-g', 'solo')
    first = int(pueue.call('add', '--print-task-id', '-g', 'solo', '--', 'sleep', '20').strip())
    second = int(pueue.call('add', '--print-task-id', '-g', 'solo', '--', 'sleep', '20').strip())
    groups, tasks = shape()
    emit('queued', group=groups['solo'], tasks={first: tasks[first][1], second: tasks[second][1]})
    assert groups['solo'] == 'Paused' and tasks[first][1] == tasks[second][1] == 'Queued', (groups, tasks)
    pueue.call('start', first)
    assert wait_for(lambda: shape()[1][first][1] == 'Running'), 'start <id> did not start the task'
    time.sleep(3)  # give the scheduler a chance to (wrongly) start the other one or reopen the group
    groups, tasks = shape()
    emit('start_first', group=groups['solo'], tasks={first: tasks[first][1], second: tasks[second][1]})
    assert groups['solo'] == 'Paused', 'start <id> reopened the group'
    assert tasks[second][1] == 'Queued', 'the paused group started another task by itself'
    pueue.call('start', second)
    assert wait_for(lambda: shape()[1][second][1] == 'Running'), 'second start <id> did not start'
    groups, tasks = shape()
    running = sum(1 for g, s in tasks.values() if g == 'solo' and s == 'Running')
    emit('start_second', group=groups['solo'], running=running, parallel=1, exceeds_quota=running > 1)
    assert groups['solo'] == 'Paused' and running == 2, (groups, running)
    # Leave only the desk profile for the optional guard: kill, clean and remove this group.
    pueue.call('kill', '-g', 'solo')
    assert wait_for(lambda: all(s == 'Done' for g, s in shape()[1].values() if g == 'solo')), 'kill did not finish'
    pueue.call('clean', '-g', 'solo')
    pueue.call('group', 'remove', 'solo')


def guard_scenario(control, pueue, root, shape, emit):
    """The real guard on the private daemon: high pressure pauses, mid band admits one, then the cap holds."""
    now = time.time()
    for group, slots in control.desk_profile(now).items():
        pueue.call('group', 'add', group, '--parallel', slots)
    pueue.call('pause', '-g', 'default')
    directory = root / 'control'
    directory.mkdir()
    engine = control.MiniControl(pueue, directory, host='desk')
    with control.locked(directory):
        engine.adopt(root / 'pid')
    log = directory / 'guardia.log'

    def tick(readings):
        with patch.object(control, 'pressure', return_value=readings), control.locked(directory):
            engine.guard(readings)
        rows = [json.loads(line) for line in log.read_text().splitlines()]
        return next(r for r in reversed(rows) if r['evento'] in control.SUMMARY_EVENTS)

    tick(HIGH)
    groups, _ = shape()
    assert groups['otros'] == 'Paused', groups
    ident = int(pueue.call('add', '--print-task-id', '-g', 'otros', '--', 'sleep', '20').strip())
    other = int(pueue.call('add', '--print-task-id', '-g', 'otros', '--', 'sleep', '20').strip())
    summary = tick(MID)
    groups, tasks = shape()
    emit('guard_mid', summary=dict(evento=summary['evento'], grupo=summary.get('grupo'), id=summary.get('id'),
                                   causa=summary.get('causa')),
         otros=groups['otros'], tasks={ident: tasks[ident][1], other: tasks[other][1]})
    assert (summary['evento'], summary.get('id')) == ('admision_una', ident), summary
    assert groups['otros'] == 'Paused' and tasks[ident][1] == 'Running' and tasks[other][1] == 'Queued', (groups, tasks)
    summary = tick(MID)
    _, tasks = shape()
    emit('guard_mid_again', evento=summary['evento'], causa=summary.get('causa'), restante_ms=summary.get('restante_ms'),
         other=tasks[other][1])
    assert (summary['evento'], summary.get('causa'), tasks[other][1]) == ('tope_alcanzado', 'cooldown', 'Queued'), summary
    permit = engine.admission()
    emit('permit', outcome=permit['last_attempt']['outcome'], confirmed=permit['last_confirmed']['id'] == ident)


if __name__ == '__main__':
    sys.exit(main())
