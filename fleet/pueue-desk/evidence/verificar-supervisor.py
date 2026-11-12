#!/usr/bin/python3
"""Integración con Pueue real, exclusivamente dentro de la copia de remote-run."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import tempfile
import time

CODE = Path(__file__).resolve().parents[1]
ROOT = Path(tempfile.mkdtemp(prefix='.u17-host-', dir=Path.cwd()))
ENV = {'PATH': '/opt/homebrew/bin:/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'}
spec = importlib.util.spec_from_file_location('control', CODE / 'control.py')
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)


def emit(label, **fields):
    print(label, json.dumps(fields, sort_keys=True), flush=True)


class Host:
    def __init__(self, name):
        self.root = ROOT / name
        self.root.mkdir()
        self.config = self.root / 'pueue.yml'
        self.paths = self.root / 'paths.json'
        self.data = self.root / 'data'
        self.state = self.root / 'control'
        settings = {'shared': {
            'pueue_directory': str(self.data), 'runtime_directory': str(self.root),
            'unix_socket_path': 's', 'pid_path': str(self.root / 'pueue.pid'),
            'alias_file': str(self.root / 'aliases.yml')}, 'daemon': {'compress_state_file': False}}
        self.paths.write_text(json.dumps(settings))
        self.config.write_text('shared: ' + json.dumps(settings['shared']) + '\ndaemon:\n  compress_state_file: false\n  env_vars: {}\n')
        self.launchers = []
        self.groups = []

    def call(self, *args):
        return subprocess.check_output(['/opt/homebrew/bin/pueue', '-c', str(self.config), *args],
                                       cwd=self.root, env=ENV, text=True, stderr=subprocess.STDOUT).strip()

    def status(self):
        return json.loads(self.call('status', '--json'))

    def launch(self):
        output = (self.root / 'host.log').open('a')
        process = subprocess.Popen(['/usr/bin/python3', '-I', str(CODE / 'host.py'), '--host', 'mini',
                                    '--config', str(self.config), '--paths', str(self.paths), '--state-dir', str(self.state),
                                    '--pueue-bin', '/opt/homebrew/bin/pueue', '--pueued-bin', '/opt/homebrew/bin/pueued'],
                                   cwd=self.root, env=ENV, stdout=output, stderr=subprocess.STDOUT)
        self.launchers.append((process, output))
        return process

    def start(self):
        process = self.launch()
        for _ in range(200):
            if process.poll() is not None:
                raise RuntimeError((self.root / 'host.log').read_text())
            try:
                if self.status()['groups']['suites']['status'] == 'Running':
                    return process
            except (subprocess.CalledProcessError, KeyError):
                pass
            time.sleep(.05)
        raise RuntimeError('No arrancó: ' + (self.root / 'host.log').read_text())

    def stop(self, process):
        process.terminate()
        assert process.wait(timeout=25) == 0

    def add_running_and_queued(self):
        self.call('parallel', '1', '-g', 'suites')
        self.call('pause', '--wait', '-g', 'default')
        marker = self.root / 'ejecuciones'
        pid = self.root / 'child.pid'
        command = ('echo intento >> ' + shlex.quote(str(marker)) + '; echo $$ > ' + shlex.quote(str(pid)) + '; exec sleep 600')
        running = self.call('add', '--print-task-id', '-g', 'suites', '--', command)
        self.wait_status(running, 'Running')
        for _ in range(100):
            if pid.exists():
                break
            time.sleep(.05)
        child = int(pid.read_text())
        self.groups.append(os.getpgid(child))
        queued = self.call('add', '--print-task-id', '-g', 'suites', '--',
                           'echo cola >> ' + shlex.quote(str(self.root / 'cola')))
        assert 'Queued' in self.status()['tasks'][queued]['status']
        return running, queued, child

    def wait_status(self, task_id, kind):
        for _ in range(200):
            item = self.status()['tasks'][task_id]
            if kind in item['status']:
                return item
            time.sleep(.05)
        raise RuntimeError('Estado inesperado: ' + str(item['status']))

    def close(self):
        for process, output in self.launchers:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            output.close()
        pidfile = self.root / 'pueue.pid'
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), signal.SIGTERM)
            except ProcessLookupError:
                pass
        for group in self.groups:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass


emit('INTEGRACION', raiz=str(ROOT), usuario_sin_cambios=True)
host = Host('ordenada')
try:
    first = host.start()
    running, queued, child = host.add_running_and_queued()
    before = host.status()['tasks'][running]
    host.stop(first)
    dead = subprocess.run(['/bin/ps', '-p', str(child)], stdout=subprocess.DEVNULL).returncode != 0
    assert dead
    queue = json.loads((host.data / 'state.json').read_text())
    assert queue['tasks'][running]['status']['Done']['result'] == 'Killed'
    assert 'Queued' in queue['tasks'][queued]['status']
    emit('PARADA-ORDENADA', hijo_muerto=dead, en_cola=queued, interrumpida=running)
    second = host.start()
    after = host.wait_status(running, 'Running')
    host.groups.append(os.getpgid(int((host.root / 'child.pid').read_text())))
    host.wait_status(queued, 'Done')
    for key in set(before) - {'status'}:
        assert before[key] == after[key], key
    assert (host.root / 'ejecuciones').read_text().splitlines() == ['intento', 'intento']
    assert (host.root / 'cola').read_text().splitlines() == ['cola']
    state_before = (host.state / 'guardia-state.json').read_bytes()
    subprocess.run(['/usr/bin/python3', '-I', str(CODE / 'control.py'), 'recuperar', '--host', 'mini',
                    '--state-dir', str(host.state), '--data-dir', str(host.data), '--config', str(host.config),
                    '--pueue-bin', '/opt/homebrew/bin/pueue'], cwd=host.root, env=ENV, check=True)
    assert state_before == (host.state / 'guardia-state.json').read_bytes()
    emit('RECUPERADA', id=running, campos_iguales=True, intentos=2, cola_ejecutada_una_vez=True, recuperar_idempotente=True)
    host.stop(second)
    third = host.start()
    status = host.status()['tasks'][running]['status']
    assert status['Done']['result'] == 'Killed'
    record = control.load_control(host.state)['recovery'][control.task_key(before)]
    assert record['phase'] == 'fallida' and 'Segundo reinicio' in record['motivo']
    assert len((host.root / 'ejecuciones').read_text().splitlines()) == 2
    emit('SEGUNDO-REINICIO', id=running, status=status, motivo=record['motivo'], no_tercer_intento=True)
    host.stop(third)
finally:
    host.close()

host = Host('daemon-kill')
try:
    first = host.start()
    running, queued, child = host.add_running_and_queued()
    daemon_pid = int((host.root / 'pueue.pid').read_text())
    os.kill(daemon_pid, signal.SIGKILL)
    assert first.wait(timeout=10) == 1
    assert subprocess.run(['/bin/ps', '-p', str(child)], stdout=subprocess.DEVNULL).returncode == 0
    second = host.start()
    status = host.status()['tasks'][running]['status']
    assert status['Done']['result'] == 'Killed'
    assert len((host.root / 'ejecuciones').read_text().splitlines()) == 1
    record = next(iter(control.load_control(host.state)['recovery'].values()))
    assert 'hijos vivos' in record['motivo']
    emit('KILL-SIN-DUPLICADO', id=running, hijo_original_vivo=True, intentos=1, motivo=record['motivo'])
    host.stop(second)
finally:
    host.close()

host = Host('launcher-kill')
try:
    first = host.start()
    daemon_pid = int((host.root / 'pueue.pid').read_text())
    first.kill()
    first.wait(timeout=5)
    before = (host.data / 'state.json').read_bytes()
    second = host.launch()
    assert second.wait(timeout=10) == 1
    assert before == (host.data / 'state.json').read_bytes()
    assert int((host.root / 'pueue.pid').read_text()) == daemon_pid
    emit('LAUNCHER-KILL', daemon_original_conservado=True, segundo_daemon_rechazado=True, cola_intacta=True)
finally:
    host.close()
emit('FIN', resultado='OK; supervisor real, sin instalar ni tocar el Pueue del usuario')
