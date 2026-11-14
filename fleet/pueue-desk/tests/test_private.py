"""U3.4: arranque real y privado en el desk. Sólo vía remote-run, nunca en la MacBook.

Un pueued 4.0.4 de stock dentro de unidades transitorias `pao-u34-opus-*` (systemd-run --user), con HOME, XDG y
socket propios bajo /tmp/u34o.*. Las propiedades salen de units/pueued.service (ExecStartPre preparar, ExecStop
parada, Restart=always, RestartSec, KillMode=mixed, StartLimitIntervalSec=0). La guardia corre en otra unidad
transitoria con Requisite/After sobre la del daemon. Nunca toca el pueued real, sus unidades ni ~/.local.

/usr/bin/python3 -B -I tests/test_private.py --run-private --host desk   (cwd: directorio vacío de /tmp)
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
import uuid

SOURCE = Path(__file__).resolve().parents[1]
LOW = dict(io=0, cpu=0, memory=64 * 1024**3)


def guard_shim():
    """Runs inside a transient guard unit: the private controller with synthetic pressure and Docker."""
    home = Path.home()
    if not str(home).startswith('/tmp/u34o.'):
        sys.exit('HOME no privado; la guardia transitoria no corre')
    spec = importlib.util.spec_from_file_location('private_control', home / '.local/lib/pao-pueue-desk/control.py')
    control = importlib.util.module_from_spec(spec); spec.loader.exec_module(control)
    assert str(control.STATE_DIR).startswith(str(home)), control.STATE_DIR
    control.pressure = lambda *a, **k: LOW
    control.docker_ready = lambda *a, **k: os.environ.get('PAO_U34_DOCKER') == '1'
    sys.argv = ['control.py', 'guardia', '--host', 'desk']
    control.main()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-private', action='store_true')
    parser.add_argument('--guard-shim', action='store_true')
    parser.add_argument('--host', choices=('desk',), default='desk')
    args = parser.parse_args()
    if args.guard_shim:
        return guard_shim()
    if not args.run_private or sys.platform != 'linux':
        parser.error('sólo --run-private en el desk')
    base = Path.cwd().resolve()
    if not base.name.startswith('u34o.') or base.parent != Path('/tmp'):
        parser.error('cwd debe ser un /tmp/u34o.* nuevo')
    real_home = Path.home()
    root = base
    home, run_dir, tmp = root / 'h', root / 'r', root / 't'
    for folder in (home / '.local/bin', home / '.local/lib/pao-pueue-desk', home / '.config/pueue', run_dir, tmp):
        folder.mkdir(parents=True, mode=0o700)
    for name in ('pueue', 'pueued'):
        (home / '.local/bin' / name).symlink_to(real_home / '.local/bin' / name)
    controller = home / '.local/lib/pao-pueue-desk/control.py'
    controller.write_bytes((SOURCE / 'control.py').read_bytes())
    config = home / '.config/pueue/pueue.yml'
    config.write_text('shared:\n  use_unix_socket: true\ndaemon:\n  compress_state_file: false\n')
    real_runtime = os.environ.get('XDG_RUNTIME_DIR') or '/run/user/%d' % os.getuid()
    system_env = {k: os.environ[k] for k in ('PATH', 'LANG', 'USER', 'LOGNAME') if k in os.environ}
    system_env.update(XDG_RUNTIME_DIR=real_runtime, HOME=str(real_home))
    if 'DBUS_SESSION_BUS_ADDRESS' in os.environ:
        system_env['DBUS_SESSION_BUS_ADDRESS'] = os.environ['DBUS_SESSION_BUS_ADDRESS']
    private = dict(HOME=str(home), XDG_CONFIG_HOME=str(home / '.config'), XDG_DATA_HOME=str(home / '.local/share'),
                   XDG_RUNTIME_DIR=str(run_dir), TMPDIR=str(tmp), PYTHONDONTWRITEBYTECODE='1')
    # The whole process now sees only private paths; secrets of the remote-run task never reach task envs.
    for key in list(os.environ):
        if key not in ('PATH', 'LANG', 'USER', 'LOGNAME'):
            del os.environ[key]
    os.environ.update(private)
    spec = importlib.util.spec_from_file_location('private_control', controller)
    control = importlib.util.module_from_spec(spec); spec.loader.exec_module(control)
    assert str(control.STATE_DIR).startswith(str(home)), control.STATE_DIR
    paths = control.config_paths(os.environ, home, home)
    assert paths['pid'] == run_dir / 'pueue.pid' and str(paths['state']).startswith(str(home)), paths
    pueue = control.Pueue()
    assert pueue.binary.startswith(str(home)), pueue.binary
    pueue.timeout = 5
    tag = uuid.uuid4().hex[:6]
    unit = 'pao-u34-opus-%s-pueued.service' % tag
    log = (base / 'private-startup.jsonl').open('w', buffering=1)

    def emit(event, **values):
        line = json.dumps(dict(event=event, **values), sort_keys=True)
        print(line, flush=True); log.write(line + '\n')

    def systemctl(*argv, check=True):
        return subprocess.run(['systemctl', '--user', *argv], env=system_env, capture_output=True, text=True,
                              timeout=60, check=check)

    def show(name, *props):
        out = systemctl('show', name, *('-p' + p for p in props), check=False).stdout
        return dict(line.split('=', 1) for line in out.splitlines() if '=' in line)

    def unit_properties():
        text = (SOURCE / 'units/pueued.service').read_text().replace('@HOME@', str(home)).replace('%h', str(home))
        section, props, start = None, [], None
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('['):
                section = line; continue
            key, value = line.split('=', 1)
            if section == '[Service]' and key == 'ExecStart':
                start = shlex.split(value)
            elif section == '[Service]' or (section == '[Unit]' and key == 'StartLimitIntervalSec'):
                props.append(key + '=' + value)
            elif section == '[Unit]' and key not in ('Description', 'Wants'):
                raise AssertionError('Propiedad de unidad no trasladada: ' + key)
        assert start == [str(home / '.local/bin/pueued')], start
        assert any(p.startswith('ExecStartPre=') and str(controller) in p and 'preparar' in p for p in props), props
        return props, start + ['--config', str(config)]  # same file the stock lookup finds

    props, command = unit_properties()

    def start_daemon(expect_ok=True):
        argv = ['systemd-run', '--user', '--unit', unit, '--description', 'pao-u34-opus pueued privado']
        argv += ['--setenv=%s=%s' % kv for kv in private.items()]
        argv += ['-p' + p for p in props] + ['--', *command]
        result = subprocess.run(argv, env=system_env, capture_output=True, text=True, timeout=60)
        emit('systemd_run', unit=unit, rc=result.returncode, stderr=result.stderr.strip()[-300:])
        if expect_ok:
            assert result.returncode == 0, result.stderr
            wait_for(lambda: answers(), 'daemon privado no responde')
        return result

    def answers():
        try:
            pueue.status(); return True
        except (subprocess.SubprocessError, ValueError, OSError):
            return False

    def wait_for(predicate, message, seconds=30):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            if predicate():
                return
            time.sleep(0.2)
        raise AssertionError(message)

    def verified_main_pid():
        pid = int(show(unit, 'MainPID').get('MainPID') or 0)
        if pid <= 0:
            return None
        comm = Path('/proc/%d/comm' % pid).read_text().strip()
        cgroup = Path('/proc/%d/cgroup' % pid).read_text()
        assert comm == 'pueued' and unit in cgroup, (comm, cgroup)
        return pid

    def stop_daemon():
        systemctl('stop', unit, check=False)
        wait_for(lambda: show(unit, 'ActiveState').get('ActiveState', 'inactive') in ('inactive', 'failed', ''),
                 'la unidad privada no se detuvo')
        systemctl('reset-failed', unit, check=False)
        # A transient unit is collected when inactive; the next systemd-run needs the name free.
        wait_for(lambda: show(unit, 'LoadState').get('LoadState') == 'not-found', 'la unidad privada sigue cargada', 15)

    def guard(docker, name):
        g = 'pao-u34-opus-%s-%s.service' % (tag, name)
        argv = ['systemd-run', '--user', '--unit', g, '--wait', '--collect', '-p', 'Requisite=' + unit, '-p', 'After=' + unit]
        argv += ['--setenv=%s=%s' % kv for kv in dict(private, PAO_U34_DOCKER='1' if docker else '0').items()]
        argv += ['--', '/usr/bin/python3', '-I', str(Path(__file__).resolve()), '--guard-shim']
        result = subprocess.run(argv, env=system_env, capture_output=True, text=True, timeout=90)
        emit('guard_unit', unit=g, docker=docker, rc=result.returncode, stderr=result.stderr.strip()[-300:])
        return result.returncode

    def queue():
        return pueue.status()  # never printed: task envs stay inside the private daemon

    def variant(task):
        status = task['status']
        return status if isinstance(status, str) else next(iter(status))

    def result_of(task):
        status = task['status']
        return status.get('Done', {}).get('result') if isinstance(status, dict) else None

    def runs(label):
        path = root / ('runs-' + label)
        return path.read_text().split() if path.exists() else []

    def add(group, label, extra=()):
        program = ('import os,pathlib,time; p=pathlib.Path(%r); p.open("a").write(str(os.getpid())+"\\n"); '
                   'g=pathlib.Path(%r); e=time.monotonic()+90\nwhile not g.exists() and time.monotonic()<e: time.sleep(0.1)'
                   % (str(root / ('runs-' + label)), str(root / 'release')))
        cmd = shlex.join(['/usr/bin/python3', '-c', program])
        out = subprocess.run([pueue.binary, 'add', '--print-task-id', '--group', group, *extra, '--', cmd],
                             env=dict(os.environ), capture_output=True, text=True, timeout=10, check=True).stdout
        return int(out.strip())

    def control_main(*argv):
        saved = sys.argv
        sys.argv = ['control.py', *argv, '--host', 'desk']
        try:
            control.main()
        finally:
            sys.argv = saved

    def authority():
        engine = control.MiniControl(pueue, control.STATE_DIR, host='desk')
        with engine.connect() as db:
            return engine.state(db)

    task_pids = []
    try:
        # Never the real daemon: before ours exists, the private client must find nobody.
        assert not answers(), 'el cliente privado alcanzó un daemon; se aborta'
        emit('isolation', home=str(home), runtime=str(run_dir), unit=unit, client_reaches_nobody=True)
        emit('version', pueued=control.daemon_version(home / '.local/bin/pueued'), properties=props)
        # 1. First start: no authority yet, preparar only validates. Then adopt the desk table.
        start_daemon()
        now = time.time()
        for group, slots in control.desk_profile(now).items():
            pueue.call('group', 'add', group, '--parallel', slots)
        control_main('adoptar', '--pid-file', str(paths['pid']))
        control_main('pausar', 'pesadas')  # a manual pause must survive the restart
        a = add('hasito', 'A'); b = add('hasito', 'B'); c = add('jale', 'C')
        d = int(subprocess.run([pueue.binary, 'add', '--print-task-id', '--stashed', '--group', 'otros', '--', 'true'],
                               env=dict(os.environ), capture_output=True, text=True, timeout=10, check=True).stdout.strip())
        e = int(subprocess.run([pueue.binary, 'add', '--print-task-id', '--group', 'otros', '--', 'true'],
                               env=dict(os.environ), capture_output=True, text=True, timeout=10, check=True).stdout.strip())
        wait_for(lambda: len(runs('A')) == 1 and len(runs('C')) == 1 and result_of(queue()['tasks'][str(e)]) == 'Success',
                 'tareas privadas no arrancaron')
        q = queue()['tasks']
        assert variant(q[str(a)]) == 'Running' and variant(q[str(c)]) == 'Running' and variant(q[str(b)]) == 'Queued', \
            {k: variant(v) for k, v in q.items()}
        task_pids += runs('A') + runs('C')
        emit('before_stop', running=[a, c], queued=[b], stashed=[d], success=[e])
        # 2. Orderly stop: ExecStop=parada records the cut work before KillMode=mixed signals pueued.
        stop_daemon()
        assert not answers()
        interrupted = [i['id'] for i in authority()['startup']['interrupted']]
        assert interrupted == [a, c], interrupted
        saved = json.loads(paths['state'].read_text())
        emit('stopped', recorded_by_parada=interrupted,
             saved_by_stock={str(k): (variant(saved['tasks'][str(k)]), result_of(saved['tasks'][str(k)])) for k in (a, b, c)})
        # 3. Guard before the daemon: Requisite refuses and never starts pueued.
        assert guard(True, 'guardia0') != 0
        assert show(unit, 'ActiveState').get('ActiveState') in ('inactive', None, ''), show(unit, 'ActiveState')
        assert not answers()
        emit('requisite_refuses_guard_without_daemon')
        # 4. Restart: ExecStartPre=preparar closes every group and fails the cut work before stock starts.
        start_daemon()
        time.sleep(2)
        snap = queue()
        groups = {g: v['status'] for g, v in snap['groups'].items()}
        assert set(groups.values()) == {'Paused'}, groups
        t = snap['tasks']
        assert sorted(map(int, t)) == sorted([a, b, c, d, e]), sorted(t)
        assert (result_of(t[str(a)]), result_of(t[str(c)])) == ('Killed', 'Killed'), (t[str(a)]['status'], t[str(c)]['status'])
        assert variant(t[str(b)]) == 'Queued' and variant(t[str(d)]) == 'Stashed' and result_of(t[str(e)]) == 'Success'
        assert runs('A') == task_pids[:1] and runs('C') == task_pids[1:2], 'reejecutada'
        s = authority()
        assert s['startup']['phase'] == 'prepared', s['startup']['phase']
        assert s['reasons']['pesadas'] == ['manual'], s['reasons']
        assert s['startup']['held'].get('default') == 'fuera_del_reparto', s['startup']['held']
        assert all('startup' in s['reasons'][g] for g in ('hasito', 'jale', 'otros', 'rapidas', 'jale-verify')), s['reasons']
        emit('prepared_restart', groups=groups, interrupted=[i['id'] for i in s['startup']['interrupted']],
             queued_same_id=b, held=s['startup']['held'], reasons=s['reasons'])
        # 5. Guard after the daemon: waits for Docker, then opens only its own startup closures.
        assert guard(False, 'guardia1') == 0
        groups = {g: v['status'] for g, v in queue()['groups'].items()}
        assert set(groups.values()) == {'Paused'} and authority()['startup']['docker'] is False, groups
        emit('guard_waits_for_docker', groups=groups)
        assert guard(True, 'guardia2') == 0
        groups = {g: v['status'] for g, v in queue()['groups'].items()}
        assert groups['pesadas'] == 'Paused' and groups['default'] == 'Paused', groups
        assert all(groups[g] == 'Running' for g in ('hasito', 'jale', 'otros', 'rapidas', 'jale-verify')), groups
        assert authority()['startup']['phase'] == 'started'
        wait_for(lambda: len(runs('B')) == 1, 'B no arrancó tras abrir hasito')
        task_pids += runs('B')
        assert runs('A') == task_pids[:1] and runs('C') == task_pids[1:2]
        control_main('reanudar', 'default')
        assert queue()['groups']['default']['status'] == 'Running'
        assert 'default' not in authority()['startup']['held']
        emit('guard_opened_own', groups=groups, queued_task_started=b, default_resolved_by_reanudar=True)
        # 6. Crash: SIGKILL to the verified MainPID. Restart=always reruns preparar; no task is re-executed.
        before = verified_main_pid()
        os.kill(before, signal.SIGKILL)
        wait_for(lambda: int(show(unit, 'NRestarts').get('NRestarts') or 0) >= 1 and answers(), 'no reinició', 40)
        after = verified_main_pid()
        assert after and after != before, (before, after)
        time.sleep(2)
        snap = queue(); t = snap['tasks']
        assert result_of(t[str(b)]) == 'Killed' and runs('B') == task_pids[2:3], t[str(b)]['status']
        assert set(v['status'] for v in snap['groups'].values()) == {'Paused'}
        s = authority()
        assert b in [i['id'] for i in s['startup']['interrupted']] and s['startup']['phase'] == 'prepared'
        b_alive = [p for p in runs('B') if Path('/proc/' + p).exists()]
        emit('crash_restart', killed_pid=before, new_pid=after, nrestarts=show(unit, 'NRestarts').get('NRestarts'),
             task_b_process_alive_after_restart=b_alive,
             interrupted=[i['id'] for i in s['startup']['interrupted']], groups_closed=True)
        # 7. Unreadable queue: preparar fails, Restart=always keeps retrying without a start limit, file intact.
        stop_daemon()
        good = paths['state'].read_bytes()
        paths['state'].write_bytes(b'{"tasks":')
        start_daemon(expect_ok=False)
        wait_for(lambda: int(show(unit, 'NRestarts').get('NRestarts') or 0) >= 2, 'no reintentó', 40)
        state = show(unit, 'ActiveState', 'SubState', 'MainPID', 'NRestarts')
        assert not answers() and paths['state'].read_bytes() == b'{"tasks":', state
        emit('preparer_blocks_corrupt_queue', unit_state=state)
        stop_daemon()
        paths['state'].write_bytes(good)
        start_daemon()
        assert sorted(map(int, queue()['tasks'])) == sorted([a, b, c, d, e])
        emit('restored_queue_starts', tasks=len(queue()['tasks']))
        emit('PASS', assertions='ExecStartPre preparar antes de pueued; parada registra; Running fallan sin reejecución; '
             'Queued conserva id; grupos cerrados; manual y default retenidos; guardia espera Docker y Requisite; '
             'SIGKILL reinicia y vuelve a preparar; cola corrupta bloquea sin límite de arranques')
    finally:
        (root / 'release').touch()
        for name in list_units(systemctl):
            systemctl('stop', name, check=False); systemctl('reset-failed', name, check=False)
        if answers():
            pid = verified_main_pid_safe(show, unit)
            if pid:
                os.kill(pid, signal.SIGKILL)
        alive = [p for p in task_pids if Path('/proc/' + p).exists() and str(root) in Path('/proc/' + p + '/cmdline').read_text(errors='replace')]
        emit('cleanup', units_left=list_units(systemctl), daemon_answers=answers(), task_processes_alive=alive)
        log.close()


def list_units(systemctl):
    out = systemctl('list-units', '--all', '--plain', '--no-legend', 'pao-u34-opus-*', check=False).stdout
    return [line.split()[0] for line in out.splitlines() if line.strip()]


def verified_main_pid_safe(show, unit):
    pid = int(show(unit, 'MainPID').get('MainPID') or 0)
    if pid > 0 and Path('/proc/%d/comm' % pid).read_text().strip() == 'pueued' and unit in Path('/proc/%d/cgroup' % pid).read_text():
        return pid
    return None


if __name__ == '__main__':
    main()
