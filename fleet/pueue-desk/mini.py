#!/usr/bin/python3 -I
"""Relevo recuperable de LaunchAgents; se ejecuta en la propia Mini."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('pao_control', ROOT / 'control.py')
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)
LABEL = 'io.pao.pueue'
GUARD_LABEL = 'io.pao.pueue-guardia'
BREW_LABEL = 'sh.brew.pueue'


def launchctl(*args, check=True):
    return subprocess.run(['/bin/launchctl', *args], check=check, text=True,
                          capture_output=True, timeout=5)


class Manager:
    def __init__(self, home=None, pueue=None, launch=launchctl, checkpoint=lambda _name: None):
        self.home = home or Path.home()
        self.directory = self.home / '.local/state/pao/pueue-desk'
        self.library = self.home / '.local/lib/pao-pueue-desk'
        self.agents = self.home / 'Library/LaunchAgents'
        self.transaction = self.directory / 'mini-handoff.json'
        self.domain = f'gui/{os.getuid()}'
        self.pueue = pueue or control.Pueue('/opt/homebrew/bin/pueue')
        self.launch = launch
        self.checkpoint = checkpoint

    def put(self, target, content, name):
        control.atomic_text(target, content)
        self.checkpoint(name)

    def remove(self, path, name):
        path.unlink(missing_ok=True)
        control.sync_directory(path.parent)
        self.checkpoint(name)

    def loaded(self, label):
        return self.launch('print', self.domain + '/' + label, check=False).returncode == 0

    def command(self, *args):
        result = self.launch(*args)
        self.checkpoint('launch:' + args[0] + ':' + Path(args[-1]).stem)
        return result

    def bootstrap(self, label):
        self.command('enable', self.domain + '/' + label)
        if not self.loaded(label):
            self.command('bootstrap', self.domain, str(self.agents / (label + '.plist')))

    def preflight(self, action):
        for binary in ('pueue', 'pueued'):
            output = subprocess.check_output(['/opt/homebrew/bin/' + binary, '--version'], text=True, timeout=5).strip()
            if output != binary + ' 4.0.4':
                raise RuntimeError('Se requiere Pueue estándar 4.0.4')
        pinned = subprocess.check_output(['/opt/homebrew/bin/brew', 'list', '--pinned'], text=True, timeout=10)
        if 'pueue' not in pinned.splitlines():
            raise RuntimeError('Pueue no está fijado con brew pin; no se instala nada')
        if not any(self.loaded(label) for label in (LABEL, BREW_LABEL)):
            raise RuntimeError('Ningún LaunchAgent conocido posee el daemon; no se adopta un daemon manual')
        control.validate_configuration()
        path = control.default_data() / 'state.json'
        disk = control.read_queue(path)
        memory = self.pueue.status()
        if disk['tasks'].keys() != memory['tasks'].keys():
            raise RuntimeError('Los ids en disco no coinciden con el daemon; revisar rutas estándar')
        if not (self.agents / (BREW_LABEL + '.plist')).exists():
            raise RuntimeError('Falta el LaunchAgent de brew para completar el relevo')
        # No admitir una segunda operación que reescriba el destino de otra.
        if self.transaction.exists():
            raise RuntimeError('Hay un relevo pendiente; el job lo completará antes de reintentar')

    def install_files(self):
        # Todo el código y ambos plists existen antes de poder parar un daemon.
        for name in ('control.py', 'mini.py'):
            target = self.library / name
            text = (ROOT / name).read_text()
            if not target.exists() or target.read_text() != text:
                self.put(target, text, 'copiar:' + name)
        for label in (LABEL, GUARD_LABEL):
            target = self.agents / (label + '.plist')
            template = plistlib.loads((ROOT / 'launchagents' / (label + '.plist')).read_bytes())
            template['ProgramArguments'] = [value.replace('@HOME@', str(self.home)) for value in template['ProgramArguments']]
            template['WorkingDirectory'] = str(self.home)
            text = plistlib.dumps(template).decode()
            if not target.exists() or target.read_text() != text:
                self.put(target, text, 'copiar:' + label)
        # host.log ya no recibe append del daemon. Acota un archivo heredado.
        for suffix in ('', '.1'):
            path = self.directory / ('host.log' + suffix)
            if path.exists() and path.stat().st_size > control.LOG_MAX_BYTES:
                with path.open('r+b') as stream:
                    stream.truncate(control.LOG_MAX_BYTES)
                    stream.flush()
                    os.fsync(stream.fileno())
                self.checkpoint('acotar:host.log' + suffix)
        self.bootstrap(GUARD_LABEL)

    def change(self, action, dry, wait_minutes=0, preflight=True):
        if preflight:
            self.preflight(action)
        if dry:
            print('SECO: validar 4.0.4, brew pin, configuración estándar e ids de cola')
            print('SECO: instalar job de relevo antes de pausar; intención durable; drenar; cambiar servicio; admisión')
            return
        with control.locked(self.directory):
            if self.transaction.exists():
                raise RuntimeError('Relevo pendiente: esperar al job antes de reintentar')
            self.install_files()
            source = LABEL if self.loaded(LABEL) else BREW_LABEL
            value = {'target': action, 'phase': 'drain', 'source': source,
                     'deadline': time.time() + wait_minutes * 60}
            self.put(self.transaction, json.dumps(value) + '\n', 'intencion')
            self._reconcile()
        print('Relevo solicitado; el job comprueba y completa las fronteras pendientes')

    def pause_owned(self):
        groups = self.pueue.status()['groups']
        owned = control.load_owned(self.directory)
        owned.update(name for name, group in groups.items() if group['status'] == 'Running')
        control.save_owned(self.directory, owned)
        self.checkpoint('propiedad')
        for name in sorted(groups):
            if groups[name]['status'] == 'Running':
                self.pueue.call('pause', '--wait', '-g', name)
                self.checkpoint('pausar:' + name)

    def ready(self):
        try:
            return self.pueue.status()
        except subprocess.CalledProcessError:
            return None

    def admit(self):
        # El mismo guardia aplica cuotas antes de abrir sólo las pausas propias.
        control.guard(self.pueue, Path('/no-proc'), self.directory, host='mini')
        self.checkpoint('admision')

    def reconcile(self):
        with control.locked(self.directory):
            self._reconcile()

    def _reconcile(self):
        if not self.transaction.exists():
            if self.ready() is not None:
                self.admit()
            return
        value = json.loads(self.transaction.read_text())
        if (value.get('target') not in ('install', 'withdraw') or
                value.get('phase') not in ('drain', 'switch') or
                value.get('source') not in (LABEL, BREW_LABEL)):
            raise ValueError('Intención de relevo ilegible; no se toca ningún servicio')
        if value['phase'] == 'drain':
            if self.ready() is None:
                # Antes del switch todavía no se retiró el origen. Si se cayó,
                # el job sabe levantarlo en lugar de esperar sin dueño.
                self.bootstrap(value['source'])
                self.launch('kickstart', self.domain + '/' + value['source'], check=False)
                self.checkpoint('reactivar:origen')
                return
            self.pause_owned()
            state = self.pueue.status()
            busy = any(any(name in task['status'] for name in ('Running', 'Paused'))
                       for task in state['tasks'].values())
            if busy:
                if time.time() < value['deadline']:
                    return
                self.admit()
                self.remove(self.transaction, 'abortar')
                raise RuntimeError('Relevo abortado: quedan tareas Running/Paused; el daemon original sigue vivo')
            value['phase'] = 'switch'
            self.put(self.transaction, json.dumps(value) + '\n', 'cambio_durable')
        desired = LABEL if value['target'] == 'install' else BREW_LABEL
        other = BREW_LABEL if desired == LABEL else LABEL
        # El job, no el solicitante, sabe completar también el intervalo sin daemon.
        # disable precede bootout: nunca relanzar el origen durante el relevo.
        self.command('disable', self.domain + '/' + other)
        if self.loaded(other):
            self.command('bootout', self.domain + '/' + other)
        if not self.loaded(desired) and self.ready() is not None:
            return  # bootout todavía está retirando el origen
        self.bootstrap(desired)
        # No sostener control.lock mientras preparar lo necesita para arrancar.
        # El siguiente StartInterval confirmará el socket y abrirá las pausas.
        if self.ready() is None:
            self.launch('kickstart', self.domain + '/' + desired, check=False)
            self.checkpoint('reactivar:destino')
            return
        self.checkpoint('daemon_responde')
        self.admit()
        if value['target'] == 'withdraw':
            # Todo lo que queda tras este punto es limpieza: brew responde y
            # ninguna pausa del relevo carece de dueño.
            self.remove(self.agents / (LABEL + '.plist'), 'retirar:daemon')
            self.remove(self.agents / (GUARD_LABEL + '.plist'), 'retirar:job')
            self.remove(self.transaction, 'completado')
            # Último efecto: puede matar a este propio job. Todo ya es seguro.
            if self.loaded(GUARD_LABEL):
                self.command('bootout', self.domain + '/' + GUARD_LABEL)
        else:
            self.remove(self.transaction, 'completado')
        control.log(self.directory, 'relevo_completado', destino=desired)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'withdraw', 'reconcile'])
    parser.add_argument('--seco', action='store_true')
    parser.add_argument('--esperar', type=float, default=0, metavar='MIN')
    args = parser.parse_args()
    if not 0 <= args.esperar <= 120:
        parser.error('--esperar debe estar entre 0 y 120 minutos')
    manager = Manager()
    try:
        if args.action == 'reconcile':
            manager.reconcile()
        else:
            manager.change(args.action, args.seco, args.esperar)
        return 0
    except Exception as error:
        if args.seco:
            print(str(error), file=sys.stderr)
        else:
            with control.locked(manager.directory):
                control.log(manager.directory, 'error_relevo', detalle=str(error))
        # launchd no escribe stderr ilimitado. El motivo vive en guardia.log.
        return 1


if __name__ == '__main__':
    sys.exit(main())
