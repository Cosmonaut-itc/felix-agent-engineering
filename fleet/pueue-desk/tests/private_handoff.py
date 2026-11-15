"""U3.5 private Mini acceptance. Run ONLY via remote-run, from an empty work directory.

Everything is private: HOME, pueue.yml, pueued, authority and the LaunchAgents
pao.u35.opus.{brew,daemon} loaded in gui/$UID from plists inside the work
directory. disable/enable are recorded in a private file instead of launchd's
persistent override database, so no residue survives the run (README gap).
The real sh.brew.pueue, local.pao.* and ~/.local are never touched.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import time

PREFIX = 'pao.u35.opus.'
BREW, OURS = PREFIX + 'brew', PREFIX + 'daemon'
PUEUE, PUEUED = '/opt/homebrew/bin/pueue', '/opt/homebrew/bin/pueued'
UID = os.getuid()
DOMAIN = 'gui/' + str(UID)


def load_control(path):
    spec = importlib.util.spec_from_file_location('control_u35_private', path)
    control = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(control)
    # Same code, private labels: validate_handoff and every phase read these globals.
    control.BREW_LABEL, control.DAEMON_LABEL = BREW, OURS
    return control


def paths(work):
    home = work / 'home'
    data = home / 'Library/Application Support/pueue'
    return dict(home=home, data=data, pid=data / 'pueue.pid', config=work / 'pueue.yml',
                state=home / '.local/state/pao/pueue-mini', agents=work / 'agents', flags=work / 'launchd-flags.json',
                events=work / 'events.jsonl')


def private_launchd(control, p):
    class PrivateLaunchd(control.Launchd):
        """Real print/bootstrap/bootout; enable/disable recorded privately (no persistent override)."""
        def flags(self):
            return json.loads(p['flags'].read_text()) if p['flags'].exists() else {}

        def set(self, label, value):
            flags = self.flags()
            flags[label] = value
            tmp = p['flags'].with_suffix('.tmp')
            tmp.write_text(json.dumps(flags))
            os.replace(tmp, p['flags'])

        def disabled(self, label):
            return self.flags().get(label) == 'disabled'

        def disable(self, label):
            self.set(label, 'disabled')

        def enable(self, label):
            self.set(label, 'enabled')

        def bootstrap(self, label, plist):
            if self.disabled(label):
                raise subprocess.CalledProcessError(5, ['launchctl', 'bootstrap'])  # like a disabled service
            super().bootstrap(label, plist)
    return PrivateLaunchd(DOMAIN, p['agents'])


def emit(p, event, **values):
    line = json.dumps(dict(t=round(time.time(), 3), event=event, **values), sort_keys=True, default=str)
    print(line, flush=True)
    with p['events'].open('a') as stream:
        stream.write(line + '\n')


def daemon_pid(p):
    """Verified identity: pid file, comm pueued and our private --config in its command."""
    try:
        pid = p['pid'].read_text().strip()
    except FileNotFoundError:
        return None
    comm = subprocess.run(['/bin/ps', '-p', pid, '-o', 'comm='], text=True, capture_output=True)
    command = subprocess.run(['/bin/ps', '-ww', '-p', pid, '-o', 'command='], text=True, capture_output=True)
    if (comm.returncode == 0 and Path(comm.stdout.strip()).name == 'pueued'
            and str(p['config']) in command.stdout):
        return int(pid)
    return None


def wait_status(pueue, seconds):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        try:
            return pueue.status()
        except subprocess.SubprocessError:
            time.sleep(.2)
    raise RuntimeError('daemon privado sin respuesta')


def setup(control, p, controller):
    for d in (p['data'], p['state'].parent, p['agents']):
        d.mkdir(parents=True, exist_ok=True)
    data = str(p['data'])
    # Exactly the private shape validate_configuration(config_path=...) admits.
    p['config'].write_text(json.dumps(dict(
        shared=dict(pueue_directory=data, runtime_directory=data, unix_socket_path=data + '/socket',
                    pid_path=str(p['pid']), use_unix_socket=True),
        daemon=dict(compress_state_file=False))))
    wrapper = p['agents'].parent / 'preparar.py'
    wrapper.write_text('import sys\nsys.path.insert(0, %r)\nimport private_handoff as h\nc = h.load_control(%r)\n'
                       'sys.argv = ["control.py", "preparar", "--host", "mini", "--exec", %r, "--config", %r]\nc.main()\n'
                       % (str(Path(__file__).resolve().parent), str(controller), PUEUED, str(p['config'])))
    env = dict(HOME=str(p['home']), PATH='/usr/bin:/bin', TMPDIR=str(p['agents'].parent))
    common = dict(RunAtLoad=True, KeepAlive=True, ThrottleInterval=10, EnvironmentVariables=env,
                  WorkingDirectory=str(p['home']))
    plists = {
        BREW: dict(common, Label=BREW, ProgramArguments=[PUEUED, '--config', str(p['config'])],
                   StandardOutPath=str(p['agents'].parent / 'brew.log'), StandardErrorPath=str(p['agents'].parent / 'brew.log')),
        OURS: dict(common, Label=OURS, ProgramArguments=['/usr/bin/python3', '-I', '-B', str(wrapper)],
                   StandardOutPath=str(p['agents'].parent / 'ours.log'), StandardErrorPath=str(p['agents'].parent / 'ours.log')),
    }
    for label in (BREW, OURS):
        (p['agents'] / (label + '.plist')).write_bytes(plistlib.dumps(plists[label]))


def drive(control, engine, launchd, p, target, wait, checkpoint):
    handoff = control.MiniHandoff(engine, launchd, p['pid'], checkpoint=checkpoint)
    with control.locked(p['state'], create=False):
        result = handoff.request(target, wait)
    for _ in range(int(wait) + 90):
        if result in ('done', 'aborted'):
            return result
        time.sleep(1)  # control.lock free: launchd's preparer may take it
        with control.locked(p['state'], create=False):
            result = handoff.tick()
    return result


def scenario(work, controller):
    p = paths(work)
    os.environ['HOME'] = str(p['home'])
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    control = load_control(controller)
    setup(control, p, controller)
    launchd = private_launchd(control, p)
    pueue = control.Pueue(PUEUE, config=p['config'])
    marks = {}
    real = control.pressure_mini()
    # Pressure is not under test here and the Mini is shared: a fixed low reading
    # keeps admission deterministic. The real reading is recorded.
    control.pressure_mini = lambda: dict(load=0.0, memory=1)
    emit(p, 'presion_real', **real)

    def checkpoint(name):
        marks.setdefault(name, time.monotonic())
        emit(p, 'frontera', nombre=name)
    emit(p, 'version', pueue=subprocess.check_output([PUEUE, '--version'], text=True).strip(),
         python=sys.version.split()[0])
    # 1. Homebrew-like manager, as on today's Mini.
    launchd.bootstrap(BREW, str(p['agents'] / (BREW + '.plist')))
    wait_status(pueue, 10)
    pueue.call('group', 'add', 'suites', '-p', '3')
    pueue.call('group', 'add', 'rapidas', '-p', '3')
    pueue.call('parallel', '1', '-g', 'default')
    pueue.call('pause', '-g', 'rapidas')
    pueue.call('add', '-g', 'rapidas', '--', 'true')  # queued in a manual pause: id and place must survive
    engine = control.MiniControl(pueue, p['state'])
    with control.locked(p['state']):
        engine.adopt(p['pid'])
    with engine.connect(write=True) as db:
        state = engine.state(db)
        state['startup_version'] = 1
        engine.store(db, state)
        control.handoff_state(db, migrate=True)
    launchd.disable(OURS)  # before our plist could ever be loaded
    emit(p, 'adoptado', motivos=engine.view()['pause_reasons'])
    # 2. Install handoff with a running task: refused without wait, drained with it.
    pueue.call('add', '-g', 'suites', '--', 'sleep 6')
    time.sleep(1)
    try:
        with control.locked(p['state'], create=False):
            control.MiniHandoff(engine, launchd, p['pid']).request('install', 0)
        raise AssertionError('relevo con tarea en curso y sin espera')
    except ValueError as error:
        emit(p, 'rechazo_sin_espera', detalle=str(error))
        assert 'tareas en curso' in str(error)
    start = time.monotonic()
    result = drive(control, engine, launchd, p, 'install', 60, checkpoint)
    snapshot = wait_status(pueue, 5)
    gap = marks.get('relevo:completado', time.monotonic()) - marks['bootout:' + BREW]
    emit(p, 'instalado', resultado=result, segundos_total=round(time.monotonic() - start, 2),
         segundos_sin_daemon_max=round(gap, 2), cargados=[l for l in (BREW, OURS) if launchd.loaded(l)],
         tareas={k: next(iter(t['status'])) for k, t in snapshot['tasks'].items()},
         grupos={g: r['status'] for g, r in snapshot['groups'].items()}, vista=engine.view()['startup_waiting'],
         retenidos=engine.view()['startup_holds'])
    assert result == 'done' and launchd.loaded(OURS) and not launchd.loaded(BREW)
    assert all(r['status'] == 'Paused' for r in snapshot['groups'].values())
    assert next(iter(snapshot['tasks']['0']['status'])) == 'Queued'
    guard_tick(control, pueue, p, launchd)
    groups = {g: r['status'] for g, r in pueue.status()['groups'].items()}
    view = engine.view()
    emit(p, 'guardia_tras_relevo', grupos=groups, motivos=view['pause_reasons'], retenidos=view['startup_holds'],
         pendiente=view['recovery_pending'])
    # Quotas and sensors ready: startup groups open; the manual pause stays retained.
    assert groups['suites'] == 'Running' and groups['rapidas'] == 'Paused'
    assert view['startup_holds'].get('rapidas') == 'pausa manual'
    # 3. SIGKILL pueued with a running task: KeepAlive reruns the preparer, the task fails visibly.
    # The task records its own pid: cleanup may stop only that verified orphan.
    pueue.call('add', '-g', 'suites', '--', 'echo $$ > ' + json.dumps(str(work / 'tarea.pid')) + '; exec sleep 45')
    time.sleep(2)
    victim = daemon_pid(p)
    assert victim, 'pid privado no verificado'
    killed_at = time.monotonic()
    os.kill(victim, signal.SIGKILL)
    snapshot = wait_status(pueue, 40)
    emit(p, 'reinicio_keepalive', segundos=round(time.monotonic() - killed_at, 2),
         tareas={k: t['status'] for k, t in snapshot['tasks'].items()},
         grupos={g: r['status'] for g, r in snapshot['groups'].items()},
         interrupciones=engine.view().get('startup_holds'),
         autoridad=_startup(engine).get('interruptions'))
    killed = [k for k, t in snapshot['tasks'].items() if t['status'].get('Done', {}).get('result') == 'Killed']
    assert killed and all(k in _startup(engine)['interruptions'] for k in killed)
    guard_tick(control, pueue, p, launchd)
    view = engine.view()
    emit(p, 'guardia_tras_reinicio', grupos={g: r['status'] for g, r in pueue.status()['groups'].items()},
         motivos=view['pause_reasons'], retenidos=view['startup_holds'], fase=_startup(engine).get('phase'))
    assert _startup(engine).get('phase') == 'ready'
    # 4. Withdraw with SIGKILL between bootout and bootstrap; the guard alone finishes.
    child = subprocess.run([sys.executable, '-I', str(Path(__file__).resolve()), '--child-withdraw', str(work), str(controller)],
                           text=True, capture_output=True, timeout=120, env=dict(os.environ))
    emit(p, 'hijo_retiro', rc=child.returncode, stderr=child.stderr[-400:],
         cargados=[l for l in (BREW, OURS) if launchd.loaded(l)], fase=_handoff(engine)['phase'])
    assert child.returncode == -signal.SIGKILL
    # The switch is durable before disable/bootout; the source is gone, nothing runs.
    assert not launchd.loaded(BREW) and not launchd.loaded(OURS) and _handoff(engine)['phase'] == 'switch'
    killed_at = [json.loads(line)['t'] for line in p['events'].read_text().splitlines()
                 if json.loads(line).get('nombre') == 'bootout:' + OURS][-1]
    ticks = 0
    for ticks in range(1, 21):
        guard_tick(control, pueue, p, launchd)
        if _handoff(engine)['phase'] == 'idle':
            break
        time.sleep(1)
    snapshot = wait_status(pueue, 5)
    emit(p, 'retirado_por_guardia', segundos_sin_daemon=round(time.time() - killed_at, 2), ticks=ticks,
         relevo=_handoff(engine)['last'], motivos=engine.view()['pause_reasons'],
         cargados=[l for l in (BREW, OURS) if launchd.loaded(l)],
         tareas={k: next(iter(t['status'])) for k, t in snapshot['tasks'].items()})
    assert launchd.loaded(BREW) and not launchd.loaded(OURS) and _handoff(engine)['last']['result'] == 'done'


def guard_tick(control, pueue, p, launchd):
    # The installed guard job, in-process: same function, private state and labels.
    with control.locked(p['state'], create=False):
        control.guard(pueue, None, p['state'], host='mini', launchd=launchd)


def _startup(engine):
    with engine.connect() as db:
        return engine.state(db).get('startup') or {}


def _handoff(engine):
    control = sys.modules[type(engine).__module__]
    with engine.connect() as db:
        return control.handoff_state(db)


def child_withdraw(work, controller):
    p = paths(work)
    os.environ['HOME'] = str(p['home'])
    control = load_control(controller)
    launchd = private_launchd(control, p)
    engine = control.MiniControl(control.Pueue(PUEUE, config=p['config']), p['state'])

    def checkpoint(name):
        emit(p, 'frontera_hijo', nombre=name)
        if name == 'bootout:' + OURS:
            os.kill(os.getpid(), signal.SIGKILL)  # between bootout and bootstrap
    drive(control, engine, launchd, p, 'withdraw', 30, checkpoint)


def cleanup(work):
    """bootout every private label, then kill a leftover daemon only by verified PID."""
    p = paths(work)
    for label in (OURS, BREW):
        subprocess.run(['/bin/launchctl', 'bootout', DOMAIN + '/' + label], capture_output=True)
    time.sleep(1)
    victim = daemon_pid(p)
    if victim:
        os.kill(victim, signal.SIGTERM)
        time.sleep(2)
    try:
        task = (work / 'tarea.pid').read_text().strip()
        comm = subprocess.run(['/bin/ps', '-p', task, '-o', 'comm='], text=True, capture_output=True).stdout.strip()
        command = subprocess.run(['/bin/ps', '-ww', '-p', task, '-o', 'command='], text=True, capture_output=True).stdout
        if task.isdigit() and Path(comm).name == 'sleep' and command.strip() == 'sleep 45':
            os.kill(int(task), signal.SIGTERM)  # orphan of the SIGKILLed private daemon only
            emit(p, 'huerfana_detenida', pid=int(task))
    except FileNotFoundError:
        pass
    left = subprocess.run(['/bin/launchctl', 'print', DOMAIN], text=True, capture_output=True).stdout
    residue = sorted({line.strip() for line in left.splitlines() if PREFIX in line})
    emit(p, 'limpieza', residuos_launchd=residue, daemon_vivo=daemon_pid(p))
    return not residue and daemon_pid(p) is None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-private', type=Path)
    parser.add_argument('--child-withdraw', nargs=2)
    parser.add_argument('--controller', type=Path, default=Path(__file__).resolve().parents[1] / 'control.py')
    args = parser.parse_args()
    if args.child_withdraw:
        return child_withdraw(Path(args.child_withdraw[0]), Path(args.child_withdraw[1]))
    work = args.run_private.resolve()
    work.mkdir()  # refuse reuse
    ok = False
    try:
        scenario(work, args.controller.resolve())
        ok = True
    finally:
        clean = cleanup(work)
    print('ESCENARIO ' + ('PASS' if ok and clean else 'FAIL'), flush=True)
    sys.exit(0 if ok and clean else 1)


if __name__ == '__main__':
    main()
