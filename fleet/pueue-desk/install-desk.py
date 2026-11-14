#!/usr/bin/python3 -I
"""Migra sólo el controlador desk instalado; no reinicia ni configura pueued."""
import argparse
import importlib.util
import os
from pathlib import Path
import subprocess

SOURCE = Path(__file__).resolve().parent
TIMERS = ('pueue-desk-mode.timer', 'pueue-desk-guard.timer')
SERVICES = ('pueue-desk-mode.service', 'pueue-desk-guard.service')


def run(*argv):
    return subprocess.run(list(argv), check=True, capture_output=True, text=True)


def preflight(home, pid_file, control):
    """Read-only gates shared by --seco and installation; no lock or mkdir."""
    directory = home / '.local/state/pao/pueue-desk'
    library = home / '.local/lib/pao-pueue-desk'
    target = library / 'control.py'
    if not target.is_file():
        raise ValueError('Primero se necesita el servicio desk existente; ver README')
    for name in TIMERS:
        run('systemctl', '--user', 'is-active', name)
    engine = control.MiniControl(control.Pueue(home / '.local/bin/pueue'), directory, host='desk')
    if control.has_authority(directory):
        engine.view()  # unreadable authority never grants re-adoption
    else:
        observed, _ = engine.observed()
        if observed != control.desk_profile(engine.clock()):
            raise ValueError('El desk no coincide con la tabla vigente; no se instala')
    control.mini_generation(pid_file)
    return engine


def install(home, pid_file, control, dry_run=False):
    engine = preflight(home, pid_file, control)
    if dry_run:
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
        if not os.path.lexists(switch):
            with switch.open('x') as stream:
                stream.write('instalación en curso\n'); stream.flush(); os.fsync(stream.fileno())
            enable_after = True
    # Stop both triggers and wait for older workers before publishing/adoption.
    # pueued.service remains running, including its shared queue.
    try:
        run('systemctl', '--user', 'stop', *TIMERS, *SERVICES)
        with control.locked(directory):
            if not control.has_authority(directory):
                backup = library / 'control.pre-slots.py'
                if not backup.exists():
                    backup.write_bytes(target.read_bytes())
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
