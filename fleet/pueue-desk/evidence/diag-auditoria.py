#!/usr/bin/python3
"""Medición aislada: nunca usa la configuración ni el socket del usuario."""
import json
import re
import os
from pathlib import Path
import shlex
import signal
import subprocess
import time
import tempfile

ROOT = Path(tempfile.mkdtemp(prefix='.u17-diag-', dir=Path.cwd()))
ENV = {'PATH': '/opt/homebrew/bin:/usr/bin:/bin', 'U17_TEST': 'entorno-original'}


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
        self.groups = []

    def call(self, *args):
        return subprocess.check_output(['/opt/homebrew/bin/pueue', '-c', str(self.config), *args],
                                       cwd=self.root, env=ENV, text=True, stderr=subprocess.STDOUT).strip()

    def status(self):
        return json.loads(self.call('status', '--json'))

    def start(self):
        self.output = (self.root / 'daemon.log').open('a')
        self.daemon = subprocess.Popen(['/opt/homebrew/bin/pueued', '-vv', '-c', str(self.config)],
                                       cwd=self.root, env=ENV, stdout=self.output, stderr=subprocess.STDOUT)
        for _ in range(100):
            try:
                return self.status()
            except subprocess.CalledProcessError:
                time.sleep(.05)
        raise RuntimeError((self.root / 'daemon.log').read_text())

    def stop(self, sig):
        self.daemon.send_signal(sig)
        code = self.daemon.wait(timeout=10)
        self.output.close()
        self.daemon = None
        return code

    def add(self, command, group='work'):
        return self.call('add', '--print-task-id', '-g', group, '--', command)

    def running(self, task_id):
        for _ in range(100):
            task = self.status()['tasks'][task_id]
            if 'Running' in task['status']:
                return task
            time.sleep(.05)
        raise RuntimeError('No empezó: ' + str(task))

    def close(self):
        if self.daemon is not None:
            self.stop(signal.SIGTERM)
        # Sólo grupos de procesos creados por nuestros comandos con marcador privado.
        for pid in self.groups:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass



inst = Instance('auditoria-term')
try:
    inst.start()
    inst.call('group', 'add', 'work', '--parallel', '3')
    a = inst.add('sleep 600'); b = inst.add('sleep 600')
    inst.running(a); inst.running(b)
    inst.call('kill', a)
    time.sleep(.4)
    inst.stop(signal.SIGTERM)
    selected = []
    collecting = False
    for raw in (inst.root / 'daemon.log').read_text().splitlines():
        line = re.sub(r"\x1b\[[0-9;]*m", "", raw)
        if 'Received message: Kill(' in line:
            collecting = True
        if collecting or 'Killing all running tasks' in line or 'finished with result:' in line:
            selected.append(line)
        if collecting and line == ')':
            collecting = False
    emit('AUDIT-ORDEN', eventos=selected)
    disk = json.loads((inst.data / 'state.json').read_text())
    emit('AUDIT-DISCO', tasks={i: t['status'] for i, t in disk['tasks'].items()})
finally:
    inst.close()
