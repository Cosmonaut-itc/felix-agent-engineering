#!/usr/bin/python3
"""Diagnóstico previo U17a. Todos los archivos y sockets viven en esta corrida."""
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

ROOT = Path(tempfile.mkdtemp(prefix='.u17a-', dir=Path.cwd()))
ENV = {'PATH': '/opt/homebrew/bin:/usr/bin:/bin'}


def emit(label, **fields):
    print(label, json.dumps(fields, sort_keys=True), flush=True)


class Instance:
    def __init__(self, name):
        self.root = ROOT / name
        self.root.mkdir()
        self.data = self.root / 'data'
        self.config = self.root / 'pueue.yml'
        self.config.write_text(json.dumps({'shared': {
            'pueue_directory': str(self.data), 'runtime_directory': str(self.root),
            'unix_socket_path': 's', 'alias_file': str(self.root / 'aliases.yml')},
            'daemon': {'compress_state_file': False}}))
        self.daemon = None
        self.children = []

    def call(self, *args):
        return subprocess.check_output(['/opt/homebrew/bin/pueue', '-c', str(self.config), *args],
                                       cwd=self.root, env=ENV, text=True, stderr=subprocess.STDOUT, timeout=10).strip()

    def parallel(self, group, number):
        return self.call('parallel', str(number), '-g', group)

    def status(self):
        return json.loads(self.call('status', '--json'))

    def start(self):
        self.output = (self.root / 'daemon.log').open('a')
        self.daemon = subprocess.Popen(['/opt/homebrew/bin/pueued', '-c', str(self.config)],
                                       cwd=self.root, env=ENV, stdout=self.output, stderr=subprocess.STDOUT)
        for _ in range(100):
            try:
                return self.status()
            except subprocess.CalledProcessError:
                time.sleep(.05)
        raise RuntimeError((self.root / 'daemon.log').read_text())

    def stop(self, sig=signal.SIGTERM):
        self.daemon.send_signal(sig)
        self.daemon.wait(timeout=10)
        self.output.close()
        self.daemon = None

    def add(self, command, group='work'):
        return self.call('add', '--print-task-id', '-g', group, '--', command)

    def await_status(self, task_id, variant):
        for _ in range(200):
            task = self.status()['tasks'][task_id]
            if variant in task['status']:
                return task
            time.sleep(.05)
        raise RuntimeError('No llegó a ' + variant + ': ' + str(task))

    def close(self):
        if self.daemon is not None:
            self.stop()
        for pid in self.children:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


if __name__ == '__main__':
    emit('VERSION', daemon=subprocess.check_output(['/opt/homebrew/bin/pueued', '--version'], text=True).strip())
    inst = Instance('restore')
    try:
        inst.start()
        inst.call('group', 'add', 'work')
        inst.call('group', 'add', 'manual')
        inst.call('pause', '--wait', '-g', 'manual')
        marker = inst.root / 'pid'
        running = inst.add('echo $$ > ' + str(marker) + '; exec sleep 600')
        inst.await_status(running, 'Running')
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(.05)
        inst.children.append(os.getpgid(int(marker.read_text())))
        queued = inst.add('echo pendiente')
        inst.stop(signal.SIGKILL)
        disk = json.loads((inst.data / 'state.json').read_text())
        emit('DISCO', tareas={i: t['status'] for i, t in disk['tasks'].items()}, grupos=disk['groups'])
        restored = inst.start()
        emit('RESTAURADO', tareas={i: t['status'] for i, t in restored['tasks'].items()}, grupos=restored['groups'])
        assert 'Done' in restored['tasks'][running]['status']
        assert 'Queued' in restored['tasks'][queued]['status']
        assert restored['groups']['manual']['status'] == 'Paused'
        assert restored['groups']['work']['status'] == 'Paused'
        # Log deliberadamente anterior: no se usa el reloj para decidir.
        sentinel = inst.root / 'sentinel'
        logged = inst.add('echo comienzo >> ' + str(sentinel))
        inst.stop()
        log = inst.data / 'task_logs' / (logged + '.log')
        log.write_text('log previo\n')
        os.utime(log, (1, 1))
        inst.start()
        inst.call('start', '-g', 'work')
        inst.await_status(logged, 'Done')
        emit('QUEUED_CON_LOG', id=logged, ejecuciones=len(sentinel.read_text().splitlines()),
             status=inst.status()['tasks'][logged]['status'], log=log.read_text())
        assert len(sentinel.read_text().splitlines()) == 1
    finally:
        inst.close()
    emit('FIN', resultado='OK; daemon privado e hijos detenidos')
