#!/usr/bin/python3 -I
"""Guardia local de la Mini y, con autoridad adoptada, relevo recuperable de pueued (U3.5)."""
import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import time

LABEL = "local.pao.pueue-mini.guard"
SOURCE = Path(__file__).resolve().parent
PUEUE = "/opt/homebrew/bin/pueue"
LAUNCHCTL = "/bin/launchctl"


def run(*args, check=True):
    return subprocess.run(list(args), check=check, text=True, capture_output=True)


def resume_owned(state):
    """Reanuda las pausas de la guardia: sin guardia cargada, nadie lo haría."""
    if os.path.lexists(state / 'control.adopted') or os.path.lexists(state / 'control.sqlite3'):
        raise ValueError('Autoridad durable: reconciliar y resolver pausas con control.py; no abrir directamente')
    owned_file = state / "guardia-state.json"
    if not owned_file.exists():
        return
    with (state / "control.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        owned = json.loads(owned_file.read_text())
        if not set(owned) <= {"suites", "default", "rapidas"}:
            raise ValueError("Grupos desconocidos en estado del guardia")
        for group in sorted(owned):
            run(PUEUE, "start", "-g", group)
            remaining = sorted(set(json.loads(owned_file.read_text())) - {group})
            temporary = owned_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(remaining) + "\n")
            temporary.replace(owned_file)


def keep_owned_guarded(state, domain, agent, error):
    """Tras un fallo sin guardia cargada: reanuda sus grupos o la recarga; si no, lo dice."""
    if os.path.lexists(state / 'control.adopted') or os.path.lexists(state / 'control.sqlite3'):
        if run(LAUNCHCTL, 'print', f'{domain}/{LABEL}', check=False).returncode == 0:
            return
        try:
            run(LAUNCHCTL, 'bootstrap', domain, str(agent))
        except Exception:
            raise RuntimeError('Guardia no cargada; ajustes apagados. Conservar control.sqlite3 y '
                               'slots.disabled; reinstalar la misma versión. No ejecutar start directo.') from error
        return
    try:
        resume_owned(state)
        return
    except Exception:
        pass
    try:
        # Recargada, la guardia conserva la propiedad y reanuda cuando baje la presión.
        run(LAUNCHCTL, "bootstrap", domain, str(agent))
        return
    except Exception:
        pass
    owned = json.loads((state / "guardia-state.json").read_text())
    commands = "; ".join(f"{PUEUE} start -g {group}" for group in owned)
    raise RuntimeError(f"sin guardia cargada y en pausa por ella: {', '.join(owned)}. "
                       f"Reanudar a mano: {commands}") from error


def load_control():
    spec = importlib.util.spec_from_file_location('mini_control_install', SOURCE / 'control.py')
    control = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(control)
    return control


def render(home, label):
    template = plistlib.loads((SOURCE / "launchagents" / (label + ".plist")).read_bytes())
    for key in ("ProgramArguments", "StandardOutPath", "StandardErrorPath", "WorkingDirectory"):
        if key in template:
            value = template[key]
            template[key] = ([part.replace("@HOME@", str(home)) for part in value]
                             if isinstance(value, list) else value.replace("@HOME@", str(home)))
    return plistlib.dumps(template)


def atomic_bytes(target, data):
    temporary = target.with_name(target.name + ".tmp")
    with temporary.open('wb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, target)
    folder = os.open(str(target.parent), os.O_RDONLY)
    try:
        os.fsync(folder)
    finally:
        os.close(folder)


def handoff_preflight(control, home, state, pid_file, command, launchd, pueue=None, config=None):
    """Read-only gates for --seco and the real run, before any effect.

    The queue/configuration/authority projection is exactly the preparer's
    (control.start_projection); a mismatch rejects without touching anything.
    Returns mode: 'handoff' (start a switch), 'resume' (one is pending) or
    'current' (the daemon already belongs to the requested manager).
    """
    pueue = pueue or control.Pueue(PUEUE)
    config = config or control.validate_configuration(home=home, pueue=Path(PUEUE),
                                                      daemon=Path(control.MINI_PUEUED), cwd=home)
    if pid_file.resolve() != config['pid_file'].resolve():
        raise ValueError('pid_path no coincide con la configuración')
    engine = control.MiniControl(pueue, state)
    control.start_projection(engine, config, config['data'] / 'state.json')
    with engine.connect() as db:
        policy = engine.state(db)  # fixed 3/1/3 profile hash; never a default table
        handoff = control.handoff_state(db)
        if any(json.loads(r[0])['response']['status'] in ('applying', 'partial', 'unknown')
               for r in db.execute('SELECT body FROM host_actions')):
            raise ValueError('Hay recibos applying/partial/unknown; recuperar antes de instalar')
    target = 'install' if command == 'instalar' else 'withdraw'
    if handoff is not None and handoff['phase'] != 'idle' and handoff['target'] != target:
        raise ValueError('Relevo pendiente en la otra dirección')
    if handoff is not None and handoff['phase'] != 'idle':
        return engine, handoff, 'resume'
    source, destination = ((control.BREW_LABEL, control.DAEMON_LABEL) if target == 'install'
                           else (control.DAEMON_LABEL, control.BREW_LABEL))
    if launchd.loaded(destination) and not launchd.loaded(source):
        if launchd.disabled(destination) or target == 'install' and (handoff is None or not launchd.disabled(source)):
            raise ValueError('Estado de launchd inesperado; revisar a mano')
        return engine, handoff, 'current'
    if not launchd.loaded(source) or launchd.loaded(destination):
        raise ValueError(source + ' no es el único gestor cargado; no se releva')
    if target == 'withdraw' and (handoff is None or handoff['brew_plist'] is None
                                  or not Path(handoff['brew_plist']).is_file()):
        raise ValueError('Falta el plist de Homebrew registrado; no se retira')
    pueue.status()  # the current daemon answers before anything changes
    if engine.generation(policy) != policy['generation']:
        raise ValueError('Daemon sin reconciliar; esperar un tick de la guardia')
    return engine, handoff, 'handoff'


def drive(control, engine, launchd, pid_file, target, wait, state, sleep=time.sleep):
    """Request, then tick with control.lock released between attempts (preparar needs it)."""
    handoff = control.MiniHandoff(engine, launchd, pid_file)
    with (state / 'control.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        result = handoff.request(target, wait)
    for _ in range(int(wait) + 60):
        if result in ('done', 'aborted'):
            break
        sleep(1)
        with (state / 'control.lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            result = handoff.tick()
    print('Relevo: ' + {'done': 'completado', 'aborted': 'abortado; el daemon original sigue'}.get(
        result, 'pendiente (' + result + '); la guardia lo completa en su próximo tick'))
    return result


def adopted(args, home, state, library, domain, control=None, launchd=None, pueue=None, config=None,
            sleep=time.sleep):
    control = control or load_control()
    agents = home / 'Library/LaunchAgents'
    launchd = launchd or control.Launchd(domain, agents)
    pueue = pueue or control.Pueue(PUEUE)
    pid_file = args.pid_file or home / 'Library/Application Support/pueue/pueue.pid'
    daemon_agent = agents / (control.DAEMON_LABEL + '.plist')
    if args.command in ('parar', 'arrancar'):
        return maintenance(args, control, launchd, state, agents, pueue)
    engine, handoff, mode = handoff_preflight(control, home, state, pid_file, args.command, launchd, pueue, config)
    pending = mode == 'resume'
    if mode == 'current' and args.command == 'desinstalar':
        print('El daemon ya es de ' + control.BREW_LABEL + '; nada que retirar')
        return 'current'
    if mode == 'current':
        if args.seco:
            print('SECO: validado con el preparador; el daemon ya es de ' + control.DAEMON_LABEL)
            print(f'SECO: copiar control.py -> {library / "control.py"}; plists -> {daemon_agent} y '
                  f'{agents / (LABEL + ".plist")}; recargar sólo {LABEL}')
            return 'current'
        with (state / 'control.lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            atomic_bytes(library / 'control.py', (SOURCE / 'control.py').read_bytes())
            atomic_bytes(daemon_agent, render(home, control.DAEMON_LABEL))  # used on the next start
            atomic_bytes(agents / (LABEL + '.plist'), render(home, LABEL))
        launchd.bootout(LABEL)
        launchd.bootstrap(LABEL, str(agents / (LABEL + '.plist')))
        print('Actualizado: controlador y guardia; el daemon sigue sin reiniciarse')
        return 'current'
    if args.seco:
        print('SECO: validado con el preparador: configuración, Pueue 4.0.4, state.json, autoridad 3/1/3, '
              'recibos cerrados, gestor actual y generación')
        if args.command == 'instalar' and not pending:
            print('SECO: bajo control.lock migrar startup_version=1 y la fila mini_handoff (aditivo)')
            print(f'SECO: {LAUNCHCTL} disable {domain}/{control.DAEMON_LABEL} antes de escribir su plist')
            print(f'SECO: copiar control.py -> {library / "control.py"}; plists -> {daemon_agent} y {agents / (LABEL + ".plist")}')
            print(f'SECO: recargar {LABEL} (el recuperador queda instalado antes del relevo)')
        print('SECO: relevo ' + ('Homebrew -> ' + control.DAEMON_LABEL if args.command == 'instalar' else
                                 control.DAEMON_LABEL + ' -> Homebrew') + (' (reanudar pendiente)' if pending else ''))
        print('SECO: intención durable; pausar grupos Running (motivo handoff); esperar sin tareas en curso '
              f'hasta {args.esperar} min o abortar')
        print('SECO: disable origen, bootout, confirmar daemon detenido por pid file e identidad; '
              'enable destino, bootstrap, confirmar respuesta' + (' y preparación' if args.command == 'instalar' else ''))
        print('SECO: devolver pausas a la guardia; ' + ('retirar el plist propio' if args.command == 'desinstalar'
                                                        else 'la guardia abre con cuotas y sensores'))
        return 'seco'
    if args.command == 'instalar':
        with (state / 'control.lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            with engine.connect(write=True) as db:
                # Additive: keep desired policy, overrides, revisions and every receipt.
                policy = engine.state(db)
                if any(json.loads(r[0])['response']['status'] in ('applying', 'partial', 'unknown')
                       for r in db.execute('SELECT body FROM host_actions')):
                    raise ValueError('Hay recibos abiertos; no se migra')
                if policy.get('startup_version') != 1:
                    policy['startup_version'] = 1
                    engine.store(db, policy)
                control.handoff_state(db, migrate=True)
            if not pending:
                # A login may only ever load one enabled daemon plist.
                launchd.disable(control.DAEMON_LABEL)
                library.mkdir(parents=True, exist_ok=True)
                atomic_bytes(library / 'control.py', (SOURCE / 'control.py').read_bytes())
                atomic_bytes(daemon_agent, render(home, control.DAEMON_LABEL))
                atomic_bytes(agents / (LABEL + '.plist'), render(home, LABEL))
        if not pending:
            launchd.bootout(LABEL)
            launchd.bootstrap(LABEL, str(agents / (LABEL + '.plist')))
    return drive(control, engine, launchd, pid_file, 'install' if args.command == 'instalar' else 'withdraw',
                 args.esperar * 60, state, sleep)


def maintenance(args, control, launchd, state, agents, pueue):
    """Marker first; then the job, then the daemon. Start reverses it."""
    marker = state / 'maintenance'
    engine = control.MiniControl(pueue, state)
    plist = agents / (control.DAEMON_LABEL + '.plist')
    if not plist.is_file():
        raise ValueError('El daemon no es de ' + control.DAEMON_LABEL + '; usar brew services')
    if args.seco:
        if args.command == 'parar':
            print(f'SECO: crear {marker} bajo control.lock (guardia, relevo y preparador no reabren ni arrancan)')
            print(f'SECO: bootout {LABEL}; registrar tareas en curso (parada); disable y bootout {control.DAEMON_LABEL}')
        else:
            print(f'SECO: retirar {marker}; enable y bootstrap {control.DAEMON_LABEL}; bootstrap {LABEL}')
        return 'seco'
    with (state / 'control.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        with engine.connect() as db:
            handoff = control.handoff_state(db)
        if handoff is not None and handoff['phase'] != 'idle':
            raise ValueError('Relevo pendiente; completarlo antes de un mantenimiento')
        if args.command == 'parar' and not os.path.lexists(marker):
            with marker.open('x') as stream:
                stream.write('mantenimiento\n'); stream.flush(); os.fsync(stream.fileno())
    if args.command == 'parar':
        launchd.bootout(LABEL)
        with (state / 'control.lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                engine.record_stop()  # interruptions become attributable on the next start
            except subprocess.CalledProcessError:
                pass  # daemon already down: nothing is running under it
        launchd.disable(control.DAEMON_LABEL)  # a login inside maintenance loads no daemon
        launchd.bootout(control.DAEMON_LABEL)
        print('Mini en mantenimiento: guardia y daemon detenidos; cola y autoridad intactas')
        return
    with (state / 'control.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        if os.path.lexists(marker):
            marker.unlink()
    if launchd.loaded(control.BREW_LABEL) or not launchd.disabled(control.BREW_LABEL):
        raise ValueError(control.BREW_LABEL + ' habilitado o cargado; no se arranca un segundo daemon')
    launchd.enable(control.DAEMON_LABEL)
    launchd.bootstrap(control.DAEMON_LABEL, str(plist))
    launchd.bootstrap(LABEL, str(agents / (LABEL + '.plist')))
    print('Mini arrancada: el preparador cierra la fila y la guardia la abre con cuotas y sensores')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["instalar", "desinstalar", "parar", "arrancar"])
    parser.add_argument("--seco", action="store_true")
    parser.add_argument('--cuotas', action='store_true', help='Adoptar el deseo durable Mini 3/1/3')
    parser.add_argument('--pid-file', type=Path, help='pid_path configurado de pueued; no lo modifica')
    parser.add_argument('--relevo', action='store_true',
                        help='Con autoridad adoptada: pasar pueued de Homebrew al agente propio (instalar) o devolverlo (desinstalar)')
    parser.add_argument('--esperar', type=float, default=0, metavar='MIN',
                        help='Minutos para que terminen las tareas en curso antes de abortar el relevo (0..120)')
    args = parser.parse_args()
    if not 0 <= args.esperar <= 120:
        parser.error('--esperar entre 0 y 120 minutos')
    home = Path.home()
    library = home / ".local/lib/pao-pueue-mini"
    state = home / ".local/state/pao/pueue-mini"
    agent = home / "Library/LaunchAgents" / (LABEL + ".plist")
    # Equivalente a id -u, sin ejecutar comandos tampoco en --seco.
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{LABEL}"
    has_authority = os.path.lexists(state / 'control.adopted') or os.path.lexists(state / 'control.sqlite3')
    ours = (home / 'Library/LaunchAgents/local.pao.pueue-mini.daemon.plist').exists()
    if has_authority and (args.relevo or ours or args.command in ('parar', 'arrancar')):
        # Adopted authority: guard and controller stay; only pueued changes hands.
        return adopted(args, home, state, library, domain)
    if args.command in ('parar', 'arrancar') or args.relevo:
        parser.error('El relevo y el mantenimiento del daemon requieren la autoridad adoptada')
    if args.command == 'instalar' and has_authority:
        args.cuotas = True  # an adopted installation cannot return to the legacy path
    if args.command == 'desinstalar' and has_authority:
        parser.error('Conservar controlador/guardia: hay deseo durable. Para devolver pueued a Homebrew: '
                     'desinstalar --relevo (README).')
    if args.seco:
        if args.command == "instalar":
            print(f"SECO: comprobar {LAUNCHCTL} print {domain}/sh.brew.pueue")
            print(f"SECO: comprobar {PUEUE} status --json (grupos suites y default)")
        print(f"SECO: si está cargado, {LAUNCHCTL} bootout {service}")
        if args.command == "instalar":
            print(f"SECO: copiar {SOURCE / 'control.py'} -> {library / 'control.py'}")
            print(f"SECO: crear {state} y renderizar plist -> {agent}")
            print("SECO: " + shlex.join([LAUNCHCTL, "bootstrap", domain, str(agent)]))
            if args.cuotas:
                print('SECO: bajo control.lock verificar perfil 3/1/3, adoptar deseo/propiedad y conservar recibos')
        else:
            print(f"SECO: {PUEUE} start -g <grupo> para cada grupo que pausó la guardia, y vaciar su propiedad")
            print(f"SECO: retirar {agent} y {library / 'control.py'}; conservar el log en {state}")
        return

    if args.command == "instalar":
        run(LAUNCHCTL, "print", f"{domain}/sh.brew.pueue")
        # Valida respuesta y grupos antes de escribir o descargar la guardia anterior.
        status = json.loads(run(PUEUE, "status", "--json").stdout)
        if not isinstance(status.get("tasks"), dict) or not {"suites", "default"} <= status["groups"].keys():
            raise ValueError("Pueue incompleto: se necesitan suites y default")
        if args.cuotas:
            spec = importlib.util.spec_from_file_location('mini_control_install', SOURCE / 'control.py')
            control = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(control)
            engine = control.MiniControl(control.Pueue(PUEUE), state)
            if control.has_authority(state):
                engine.view()  # no default table when missing/corrupt
            else:
                observed, _ = engine.observed()
                if observed != control.MINI_PROFILE:
                    raise ValueError('La Mini no coincide con el perfil 3/1/3; no se instala')
                control.mini_generation(args.pid_file or home / 'Library/Application Support/pueue/pueue.pid')
        template = plistlib.loads((SOURCE / "launchagents" / (LABEL + ".plist")).read_bytes())
        for key in ("ProgramArguments", "StandardOutPath", "StandardErrorPath"):
            value = template[key]
            template[key] = ([part.replace("@HOME@", str(home)) for part in value]
                             if isinstance(value, list) else value.replace("@HOME@", str(home)))
    enable_after = False
    if args.command == 'instalar' and args.cuotas:
        state.mkdir(parents=True, exist_ok=True)
        with (state / 'control.lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            switch = state / 'slots.disabled'
            # Preserve an operator's pre-existing switch, including a broken symlink.
            if not os.path.lexists(switch):
                with switch.open('x') as stream:
                    stream.write('instalación en curso\n'); stream.flush(); os.fsync(stream.fileno())
                enable_after = True
    if run(LAUNCHCTL, "print", service, check=False).returncode == 0:
        run(LAUNCHCTL, "bootout", service)
    if args.command == "instalar":
        try:
            library.mkdir(parents=True, exist_ok=True)
            state.mkdir(parents=True, exist_ok=True)
            agent.parent.mkdir(parents=True, exist_ok=True)
            # Copia y reemplazo atómico: un fallo a medias deja intacto el archivo anterior.
            with (state / 'control.lock').open('a') as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                if args.cuotas:
                    if control.has_authority(state):
                        engine.view()
                    else:
                        engine.adopt(args.pid_file)
                for target, write in ((library / "control.py", lambda path: shutil.copy2(SOURCE / "control.py", path)),
                                      (agent, lambda path: path.write_bytes(plistlib.dumps(template)))):
                    temporary = target.with_name(target.name + ".tmp")
                    write(temporary)
                    os.replace(temporary, target)
            run(LAUNCHCTL, "bootstrap", domain, str(agent))
            if enable_after:
                with (state / 'control.lock').open('a') as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX)
                    switch.unlink()
        except Exception as error:
            keep_owned_guarded(state, domain, agent, error)
            raise
    else:
        try:
            resume_owned(state)
        except Exception as error:
            keep_owned_guarded(state, domain, agent, error)
            raise
        for target in (agent, library / "control.py"):
            target.unlink(missing_ok=True)
            target.with_name(target.name + ".tmp").unlink(missing_ok=True)
        if library.exists():
            library.rmdir()


if __name__ == "__main__":
    main()
