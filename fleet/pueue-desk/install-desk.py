#!/usr/bin/python3 -I
"""Migra sólo el controlador desk instalado; no reinicia ni configura pueued."""
import argparse
from datetime import datetime, timezone
from contextlib import contextmanager
import fcntl
import json
import importlib.util
import os
from pathlib import Path
import subprocess

SOURCE = Path(__file__).resolve().parent
TIMERS = ('pueue-desk-mode.timer', 'pueue-desk-guard.timer')
SERVICES = ('pueue-desk-mode.service', 'pueue-desk-guard.service')
SHOWN = ('MainPID', 'ActiveState', 'NeedDaemonReload', 'Restart', 'KillMode', 'ExecStartPre')


def unit_properties():
    result = run('systemctl', '--user', 'show', 'pueued.service',
                 *[arg for key in SHOWN for arg in ('-p', key)])
    values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    # systemd omite una lista Exec vacía, incluso con --all: la unidad previa a U3.4 no tiene ExecStartPre.
    values.setdefault('ExecStartPre', '')
    if set(values) != set(SHOWN):
        raise ValueError('systemctl show incompleto')
    return values


def run(*argv):
    return subprocess.run(list(argv), check=True, capture_output=True, text=True)


@contextmanager
def authority_lock(directory):
    # --seco may lock the existing inode, but must never create an authority/file.
    try:
        handle = (directory / 'control.lock').open('rb')
    except FileNotFoundError as error:
        raise ValueError('Falta el candado de la autoridad; no se readopta') from error
    with handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def policy_plan(engine):
    """Installer-only validation; runtime still requires the exact policy hash."""
    with engine.connect() as db:
        original = json.loads(db.execute('SELECT body FROM policy WHERE id=1').fetchone()[0])
        state = engine.state(db, policy_migration=True)
        # Read-only plan must validate the permit before engine.view or publication.
        engine.admission_plan(db)
        fields_missing = 'mode_override' not in original or original.get('startup_version') != 1
        state['startup_version'] = 1
        actions = [json.loads(row[0]) for row in db.execute('SELECT body FROM host_actions')]
        if any(a['response']['status'] in ('applying', 'partial', 'unknown') for a in actions):
            raise ValueError('Hay recibos abiertos; recuperar con el controlador anterior antes de migrar')
        if state['policy_hash'] == engine.policy_hash and not fields_missing:
            return None
    if state['policy_hash'] == engine.policy_hash:
        # Additive migration: retain revisions, original expirations and all receipts.
        engine.observed()
        engine.generation(state)
        return state
    # Validate the live host too, before effects. Time is sampled after blocking reads.
    engine.observed()
    engine.generation(state)
    event = engine.resolve(state)['schedule_event']
    desired = {g: row[engine.mode(dict(schedule_event=event, mode_override=None))] for g, row in engine.profile.items()}
    return dict(state, policy_hash=engine.policy_hash, desired=desired,
                overrides={}, mode_override=None, schedule_event=event, revision=state['revision'] + 1)



def generation_plan(engine, control, state, *, require_running=False):
    """Installer-only conversion. Legacy hashes cannot prove daemon continuity."""
    phase = state.get('startup', {}).get('phase')
    if phase not in (None, 'ready'):
        raise ValueError('Arranque durable en ' + str(phase) + '; recuperar antes de instalar')
    kind = control.generation_kind(state['generation'])
    current = engine.generation(state)
    if control.generation_kind(current) != 'linux-proc-v1':
        raise ValueError('La identidad actual del desk no es Linux v1')
    if kind == 'legacy' or require_running:
        _, groups = engine.observed()
        paused = sorted(g for g, row in groups.items() if row['status'] == 'Paused')
        if paused:
            raise ValueError('Conversión detenida; grupos Paused: ' + ', '.join(paused))
        if any(row['status'] != 'Running' for row in groups.values()):
            raise ValueError('Admisión de grupos desconocida; no convertir')
    if kind != 'legacy':
        return current, None, 'vigente' if current == state['generation'] else 'reinicio'
    converted = dict(state, generation=current)
    startup = state.get('startup')
    if startup is not None and startup.get('previous_generation') == state['generation']:
        converted['startup'] = dict(startup, previous_generation=current)
    return current, converted, 'convertir legado con todos los grupos Running'


def daemon_fence(engine, control, pid_file, expected=None):
    current = control.desk_generation(pid_file)
    loaded = unit_properties()
    pid = pid_file.read_text(encoding='ascii').strip()
    if (not pid.isascii() or not pid.isdecimal() or int(pid) < 1
            or loaded['MainPID'] != str(int(pid)) or loaded['ActiveState'] != 'active'
            or loaded['MainPID'] != engine.install_unit_before['MainPID']):
        raise ValueError('MainPID no coincide con el daemon del pid file; conservar mantenimiento')
    if expected is not None and current != expected:
        raise ValueError('La generación del daemon cambió; conservar mantenimiento')
    return current


def installation_plan(engine, control, pid_file, *, expected=None, require_running=False):
    """Always reload authority under the caller's lock, independently of table migration."""
    migration = None
    current = daemon_fence(engine, control, pid_file, expected)
    kind = 'adoptar identidad Linux v1'
    if control.has_authority(engine.directory):
        migration = policy_plan(engine)
        with engine.connect() as db:
            state = engine.state(db, policy_migration=True)
        observed, converted, kind = generation_plan(engine, control, state, require_running=require_running)
        if observed != current:
            raise ValueError('El daemon cambió durante el plan de instalación')
        if converted is not None:
            migration = dict(migration if migration is not None else state, generation=current)
            if 'startup' in converted:
                migration['startup'] = converted['startup']
    return migration, current, kind


def preflight(home, pid_file, control):
    """Read-only gates shared by --seco and installation; no file creation."""
    manager = control.manager_environment(run('systemctl', '--user', 'show-environment').stdout)
    config = control.validate_configuration(home=home, env=manager, cwd=home)
    control.read_queue(config['data'] / 'state.json')
    if pid_file.resolve() != config['pid_file'].resolve():
        raise ValueError('pid_path no coincide con la configuración')
    directory = home / '.local/state/pao/pueue-desk'
    library = home / '.local/lib/pao-pueue-desk'
    target = library / 'control.py'
    if not target.is_file():
        raise ValueError('Primero se necesita el servicio desk existente; ver README')
    for name in TIMERS:
        run('systemctl', '--user', 'is-active', name)
    client_env = dict(os.environ)
    for key in control.MANAGER_KEYS:
        client_env.pop(key, None)
    client_env.update(manager)
    engine = control.MiniControl(control.Pueue(home / '.local/bin/pueue', env=client_env, cwd=home), directory, host='desk')
    if control.has_authority(directory):
        with authority_lock(directory):
            migration = policy_plan(engine)
            if migration is None:
                engine.view()  # unreadable authority never grants re-adoption
            with engine.connect() as db:
                if Path(engine.state(db, policy_migration=True)['pid_file']).resolve() != pid_file.resolve():
                    raise ValueError('pid_path difiere de la autoridad')
            engine.install_policy_migration = migration
            engine.install_unit_before = unit_properties()
            engine.install_migration, engine.install_generation, engine.install_generation_action = installation_plan(
                engine, control, pid_file)
    else:
        observed, _ = engine.observed()
        if observed != control.desk_profile(engine.clock()):
            raise ValueError('El desk no coincide con la tabla vigente; no se instala')
        engine.install_unit_before = unit_properties()
        engine.install_migration, engine.install_generation, engine.install_generation_action = installation_plan(
            engine, control, pid_file)
    return engine


def install(home, pid_file, control, dry_run=False, sources=None):
    engine = preflight(home, pid_file, control)
    if dry_run:
        print("SECO, generación: " + engine.install_generation_action)
        print("SECO, guard_admission: crear sólo si falta; validar y conservar permiso existente, incluso unknown.")
        if getattr(engine, 'install_policy_migration', None) is not None:
            plan = engine.install_policy_migration
            print('SECO, autoridad: añadir mode_override=null si falta; conservar recibos y vencimientos.')
            with engine.connect() as db:
                old_hash = json.loads(db.execute('SELECT body FROM policy WHERE id=1').fetchone()[0])['policy_hash']
            kind = 'migración de tabla: retirar ajustes' if old_hash != plan['policy_hash'] else 'migración aditiva'
            print('SECO, ' + kind + '; revisión ' + str(plan['revision'])
                  + '; policy_hash ' + plan['policy_hash'] + '; desired ' + json.dumps(plan['desired'], sort_keys=True))
        print('Comprobado (lectura): controlador existente, timers activos, autoridad o tabla vigente y PID de pueued.')
        print('SECO, pendiente: apagar cuotas nuevas; detener timers/oneshots; bajo control.lock publicar, '
              'adoptar/importar o conservar SQLite/recibos; reconciliar; reactivar timers y retirar sólo '
              'el switch creado aquí; publicar unidades y daemon-reload. No reiniciar pueued.')
        return
    directory = engine.directory
    library = home / '.local/lib/pao-pueue-desk'
    target = library / 'control.py'
    directory.mkdir(parents=True, exist_ok=True)
    switch = directory / 'slots.disabled'
    maintenance = directory / 'maintenance'
    own_maintenance = False
    enable_after = False
    legacy_conversion = engine.install_generation_action.startswith('convertir')
    with control.locked(directory):
        installation_plan(engine, control, pid_file, expected=engine.install_generation,
                          require_running=legacy_conversion)  # before even the switch
        if not os.path.lexists(switch):
            with switch.open('x') as stream:
                stream.write('instalación en curso\n'); stream.flush(); os.fsync(stream.fileno())
            enable_after = True
        if not os.path.lexists(maintenance):
            with maintenance.open('x') as stream:
                stream.write('instalación U3.4\n'); stream.flush(); os.fsync(stream.fileno())
            control.sync_directory(directory)
            own_maintenance = True
    # Stop both triggers and wait for older workers before publishing/adoption.
    # pueued.service remains running, including its shared queue.
    try:
        run('systemctl', '--user', 'stop', *TIMERS)
        run('systemctl', '--user', 'stop', *SERVICES)
        with control.locked(directory):
            migration, _, action = installation_plan(engine, control, pid_file, expected=engine.install_generation,
                                                     require_running=legacy_conversion)
            legacy_conversion = legacy_conversion or action.startswith('convertir')
            if control.has_authority(directory):
                with engine.connect(write=True) as db:
                    control.admission_state(db, migrate=True)
            # Always back up the immediately preceding controller, not a historic release.
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
            dated = library / ('control.py.pre-sen6417-' + stamp)
            with dated.open('xb') as stream:
                stream.write(target.read_bytes()); stream.flush(); os.fsync(stream.fileno())
            control.sync_directory(library)

            if not control.has_authority(directory):
                backup = library / 'control.pre-slots.py'
                if not backup.exists():
                    backup.write_bytes(target.read_bytes())
            if control.has_authority(directory):
                backup = library / 'control.pre-mode.py'
                if not backup.exists():
                    with backup.open('xb') as stream:
                        stream.write(target.read_bytes()); stream.flush(); os.fsync(stream.fileno())
            backup_u34 = library / 'control.py.pre-u34'
            if not backup_u34.exists():
                backup_u34.write_bytes(target.read_bytes())
            temporary = target.with_suffix('.nuevo')
            with temporary.open('wb') as stream:
                stream.write(sources['control.py'].encode() if sources else (SOURCE / 'control.py').read_bytes()); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, target)
            folder = os.open(str(library), os.O_RDONLY)
            try:
                os.fsync(folder)
            finally:
                os.close(folder)
            if control.has_authority(directory):
                if migration is not None:
                    with engine.connect(write=True) as db:
                        engine.store(db, migration)  # one transaction: table and identity, preserving receipts
                        # Recheck after the write but before SQLite commits; failure rolls it back.
                        generation_plan(engine, control, engine.state(db), require_running=legacy_conversion)
                        daemon_fence(engine, control, pid_file, engine.install_generation)
                engine.view()
            else:
                engine.adopt(pid_file)
                with engine.connect(write=True) as db:
                    state = engine.state(db)
                    state['startup_version'] = 1
                    engine.store(db, state)
                    daemon_fence(engine, control, pid_file, engine.install_generation)
            units = home / '.config/systemd/user'
            units.mkdir(parents=True, exist_ok=True)
            unit_sources = ({k:v for k,v in sources.items() if k.endswith('.service') or k.endswith('.timer')} if sources else
                            {p.name:p.read_text() for p in (SOURCE / 'units').iterdir()})
            for name, content in unit_sources.items():
                unit = units / name
                backup = unit.with_name(unit.name + '.pre-u34')
                if unit.exists() and not backup.exists():
                    backup.write_bytes(unit.read_bytes())
                temporary_unit = unit.with_name(unit.name + '.u34-tmp')
                with temporary_unit.open('w') as stream:
                    stream.write(content.replace('@HOME@', str(home))); stream.flush(); os.fsync(stream.fileno())
                os.replace(temporary_unit, unit)
            control.sync_directory(units)
            run('systemctl', '--user', 'daemon-reload')  # never restart the shared daemon
            loaded = unit_properties()
            if (loaded['MainPID'] != engine.install_unit_before['MainPID']
                    or loaded['ActiveState'] != engine.install_unit_before['ActiveState']
                    or loaded['NeedDaemonReload'] != 'no' or loaded['Restart'] != 'always'
                    or loaded['KillMode'] != 'mixed'
                    or str(home / '.local/lib/pao-pueue-desk/control.py') + ' preparar --host desk' not in loaded['ExecStartPre']):
                raise ValueError('La unidad cargada no coincide o cambió MainPID; conservar mantenimiento')
            installation_plan(engine, control, pid_file, expected=engine.install_generation,
                              require_running=legacy_conversion)
            if own_maintenance:
                maintenance.unlink(); control.sync_directory(directory)
                if not engine.reconcile():
                    raise ValueError('Deseo pendiente; conservar mantenimiento y reinstalar')
    except BaseException:
        if own_maintenance and not os.path.lexists(maintenance):
            control.atomic_json(maintenance, dict(reason='instalación incompleta'))
        raise
    if own_maintenance:
        try:
            run('systemctl', '--user', 'start', *TIMERS)
        except BaseException:
            if not os.path.lexists(maintenance):
                control.atomic_json(maintenance, dict(reason='instalación incompleta'))
            try:
                run('systemctl', '--user', 'stop', *TIMERS)
            except Exception:
                pass  # keep the original error; maintenance already blocks guard and mode
            raise
    if enable_after and own_maintenance:
        with control.locked(directory):
            switch.unlink()
    print('Control y unidades desk migrados; daemon-reload sin reiniciar pueued; preparador en próximo arranque natural')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid-file', required=True, type=Path, help='pid_path real configurado, no lo modifica')
    parser.add_argument('--seco', action='store_true')
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('desk_control_install', SOURCE / 'control.py')
    control = importlib.util.module_from_spec(spec); spec.loader.exec_module(control)
    install(Path.home(), args.pid_file, control, dry_run=args.seco)


if __name__ == '__main__':
    main()
