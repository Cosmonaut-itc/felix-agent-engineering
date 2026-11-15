#!/usr/bin/python3 -I
"""Migra sólo el controlador desk instalado; no reinicia ni configura pueued."""
import argparse
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


def generation_plan(engine, control, state, planned=None):
    """Caller holds control.lock. Felix, 2026-11-15: the legacy lstart hash cannot prove
    «same daemon», so it is converted only while no desk group is Paused. reconcile marks
    only Paused groups ambiguous; with every group Running conversion hides nothing.

    With `planned`, revalidates an earlier plan: any change in the daemon or the decision stops.
    """
    current = engine.generation(state)
    if planned is not None and current != planned['current']:
        raise ValueError('Instalación detenida: la generación del daemon cambió durante la instalación')
    converting = control.generation_kind(state['generation']) == 'legacy' or (
        planned is not None and planned['kind'] == 'convert')
    if not converting:
        # A stored stable generation: equal is the same daemon, different is a restart for reconcile.
        plan = dict(kind='unchanged' if state['generation'] == current else 'restart', current=current, reason=None)
    else:
        blockers = []
        phase = (state.get('startup') or {}).get('phase')
        if phase not in (None, 'ready'):
            blockers.append('arranque durable en fase ' + str(phase))
        paused = sorted(g for g, row in engine.pueue.status()['groups'].items() if row['status'] == 'Paused')
        if paused:
            blockers.append('grupos Paused: ' + ', '.join(paused))
        plan = dict(kind='blocked' if blockers else 'convert', current=current, reason='; '.join(blockers) or None)
    if plan['kind'] == 'blocked' and planned is not None:
        raise ValueError('Instalación detenida: la generación anterior no se convierte con ' + plan['reason'])
    if planned is not None and plan['kind'] != planned['kind']:
        raise ValueError('Instalación detenida: la autoridad cambió durante la instalación')
    return plan


def refuse_blocked(plan):
    if plan is not None and plan['kind'] == 'blocked':
        raise ValueError('Instalación detenida sin cambios: la generación anterior no se convierte con '
                         + plan['reason'] + '. Reanudar o esperar esos grupos y repetir --seco.')


def current_state(engine, migration):
    if migration is not None:
        return migration
    with engine.connect() as db:
        return engine.state(db)


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
    engine.install_generation = None
    if control.has_authority(directory):
        with authority_lock(directory):
            migration = policy_plan(engine)
            if migration is None:
                engine.view()  # unreadable authority never grants re-adoption
            with engine.connect() as db:
                stored = engine.state(db, policy_migration=True)
            if Path(stored['pid_file']).resolve() != pid_file.resolve():
                raise ValueError('pid_path difiere de la autoridad')
            engine.install_migration = migration
            # Independent of policy_plan, which is None when the table is current.
            engine.install_generation = generation_plan(engine, control, migration or stored)
    else:
        observed, _ = engine.observed()
        if observed != control.desk_profile(engine.clock()):
            raise ValueError('El desk no coincide con la tabla vigente; no se instala')
    control.desk_generation(pid_file)
    engine.install_unit_before = unit_properties()
    if (not engine.install_unit_before['MainPID'].isdigit() or int(engine.install_unit_before['MainPID']) < 1
            or engine.install_unit_before['ActiveState'] != 'active'):
        raise ValueError('Se requiere pueued activo con MainPID verificable')
    if pid_file.read_text().strip() != engine.install_unit_before['MainPID']:
        raise ValueError('El pid file no es el MainPID de pueued.service')
    return engine


def describe_generation(plan):
    if plan is None:
        return 'SECO, generación: sin autoridad; la adopción guarda la generación estable.'
    if plan['kind'] == 'convert':
        return ('SECO, generación: convertiría la anterior a ' + plan['current']
                + '; ningún grupo Paused; conserva motivos, revisión, deseo y recibos.')
    if plan['kind'] == 'blocked':
        return 'SECO, generación: se detendría antes de efectos: ' + plan['reason'] + '.'
    if plan['kind'] == 'restart':
        return 'SECO, generación: otro daemon; la reconciliación marcará ambiguous los grupos Paused.'
    return 'SECO, generación: vigente, sin conversión.'


def install(home, pid_file, control, dry_run=False, sources=None):
    engine = preflight(home, pid_file, control)
    planned = getattr(engine, 'install_generation', None)
    if dry_run:
        print(describe_generation(planned))
        refuse_blocked(planned)
        if getattr(engine, 'install_migration', None) is not None:
            plan = engine.install_migration
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
    refuse_blocked(planned)
    with control.locked(directory):
        if control.has_authority(directory):
            # Refuse pending/corrupt authority or a newly Paused group before even the switch.
            refuse_blocked(generation_plan(engine, control, current_state(engine, policy_plan(engine)), planned))
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
            migration = policy_plan(engine) if control.has_authority(directory) else None
            if control.has_authority(directory):
                # Timers and guard are stopped: decide again, never from the preflight plan.
                generation_plan(engine, control, current_state(engine, migration), planned)
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
                state = current_state(engine, migration)
                plan = generation_plan(engine, control, state, planned)  # immediately before the commit
                if plan['kind'] == 'convert':
                    state = dict(state, generation=plan['current'])  # same revision: desire is unchanged
                if migration is not None or plan['kind'] == 'convert':
                    with engine.connect(write=True) as db:
                        engine.store(db, state)  # one transaction: hash, desire, overrides, revision, generation
                engine.view()
            else:
                engine.adopt(pid_file)
                with engine.connect(write=True) as db:
                    state = engine.state(db)
                    state['startup_version'] = 1
                    engine.store(db, state)
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
            if planned is not None:
                generation_plan(engine, control, current_state(engine, None), planned)
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
