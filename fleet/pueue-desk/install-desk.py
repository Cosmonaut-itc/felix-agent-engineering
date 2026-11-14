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
        fields_missing = 'mode_override' not in original
        if state['policy_hash'] == engine.policy_hash and not fields_missing:
            return None
        actions = [json.loads(row[0]) for row in db.execute('SELECT body FROM host_actions')]
        if any(a['response']['status'] in ('applying', 'partial', 'unknown') for a in actions):
            raise ValueError('Hay recibos abiertos; recuperar con el controlador anterior antes de migrar')
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


def preflight(home, pid_file, control):
    """Read-only gates shared by --seco and installation; no file creation."""
    directory = home / '.local/state/pao/pueue-desk'
    library = home / '.local/lib/pao-pueue-desk'
    target = library / 'control.py'
    if not target.is_file():
        raise ValueError('Primero se necesita el servicio desk existente; ver README')
    for name in TIMERS:
        run('systemctl', '--user', 'is-active', name)
    engine = control.MiniControl(control.Pueue(home / '.local/bin/pueue'), directory, host='desk')
    if control.has_authority(directory):
        with authority_lock(directory):
            migration = policy_plan(engine)
            if migration is None:
                engine.view()  # unreadable authority never grants re-adoption
            engine.install_migration = migration
    else:
        observed, _ = engine.observed()
        if observed != control.desk_profile(engine.clock()):
            raise ValueError('El desk no coincide con la tabla vigente; no se instala')
    control.mini_generation(pid_file)
    return engine


def install(home, pid_file, control, dry_run=False):
    engine = preflight(home, pid_file, control)
    if dry_run:
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
              'el switch creado aquí. No reiniciar pueued.')
        return
    directory = engine.directory
    library = home / '.local/lib/pao-pueue-desk'
    target = library / 'control.py'
    directory.mkdir(parents=True, exist_ok=True)
    switch = directory / 'slots.disabled'
    enable_after = False
    with control.locked(directory):
        if control.has_authority(directory):
            policy_plan(engine)  # refuse pending/corrupt authority before even the switch
        if not os.path.lexists(switch):
            with switch.open('x') as stream:
                stream.write('instalación en curso\n'); stream.flush(); os.fsync(stream.fileno())
            enable_after = True
    # Stop both triggers and wait for older workers before publishing/adoption.
    # pueued.service remains running, including its shared queue.
    try:
        run('systemctl', '--user', 'stop', *TIMERS, *SERVICES)
        with control.locked(directory):
            migration = policy_plan(engine) if control.has_authority(directory) else None
            if not control.has_authority(directory):
                backup = library / 'control.pre-slots.py'
                if not backup.exists():
                    backup.write_bytes(target.read_bytes())
            if control.has_authority(directory):
                backup = library / 'control.pre-mode.py'
                if not backup.exists():
                    with backup.open('xb') as stream:
                        stream.write(target.read_bytes()); stream.flush(); os.fsync(stream.fileno())
            temporary = target.with_suffix('.nuevo')
            with temporary.open('wb') as stream:
                stream.write((SOURCE / 'control.py').read_bytes()); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, target)
            folder = os.open(str(library), os.O_RDONLY)
            try:
                os.fsync(folder)
            finally:
                os.close(folder)
            if control.has_authority(directory):
                if migration is not None:
                    with engine.connect(write=True) as db:
                        engine.store(db, migration)  # one transaction: hash, desire, overrides, revision
                engine.view()
            else:
                engine.adopt(pid_file)
            if not engine.reconcile():
                raise ValueError('Deseo pendiente; conservar el switch y reinstalar')
    finally:
        run('systemctl', '--user', 'start', *TIMERS)
    if enable_after:
        with control.locked(directory):
            switch.unlink()
    print('Control desk migrado; timers activos; pueued y su configuración conservados')


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
