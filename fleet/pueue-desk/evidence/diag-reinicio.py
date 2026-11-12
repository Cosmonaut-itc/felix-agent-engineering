#!/usr/bin/python3
"""Medición aislada: nunca usa la configuración ni el socket del usuario."""
import json
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
        self.daemon = subprocess.Popen(['/opt/homebrew/bin/pueued', '-v', '-c', str(self.config)],
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


emit('VERSION', cliente=subprocess.check_output(['/opt/homebrew/bin/pueue', '--version'], text=True).strip(),
     daemon=subprocess.check_output(['/opt/homebrew/bin/pueued', '--version'], text=True).strip(), raiz=str(ROOT))
for name, sig in [('a-TERM', signal.SIGTERM), ('b-KILL', signal.SIGKILL)]:
    inst = Instance(name)
    try:
        inst.start()
        inst.call('group', 'add', 'work', '--parallel', '1')
        inst.call('group', 'add', 'manual', '--parallel', '3')
        inst.call('pause', '--wait', '-g', 'manual')
        marker = inst.root / 'child.pid'
        command = 'echo $$ > ' + shlex.quote(str(marker)) + '; exec sleep 600'
        running = inst.add(command)
        inst.running(running)
        queued = inst.add('echo cola')
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(.05)
        pid = int(marker.read_text())
        pgid = os.getpgid(pid)
        inst.groups.append(pgid)
        emit(name + '-ANTES', running=running, queued=queued, pid=pid, pgid=pgid,
             tasks={i: t['status'] for i, t in inst.status()['tasks'].items()}, grupos=inst.status()['groups'])
        code = inst.stop(sig)
        time.sleep(.2)
        alive = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'pid=,stat=,command='], text=True, capture_output=True)
        emit(name + '-HIJO', daemon_exit=code, hijo_vivo=alive.returncode == 0, ps=alive.stdout.strip())
        disk = json.loads((inst.data / 'state.json').read_text())
        emit(name + '-DISCO', tasks={i: t['status'] for i, t in disk['tasks'].items()}, grupos=disk['groups'])
        after = inst.start()
        emit(name + '-RESTAURADO', tasks={i: t['status'] for i, t in after['tasks'].items()}, grupos=after['groups'])
        if name == 'b-KILL':
            before = after['tasks'][running]
            out = inst.call('restart', '--in-place', running)
            after_task = inst.status()['tasks'][running]
            fields = ['id', 'command', 'original_command', 'path', 'group', 'envs']
            emit('e-RESTART', respuesta=out, conserva={f: before.get(f) == after_task.get(f) for f in fields},
                 campos=list(before), status=after_task['status'], entorno_marcador=after_task.get('envs', {}).get('U17_TEST'))
            disk_before = json.loads((inst.data / 'state.json').read_text())['tasks'][running]['status']
            time.sleep(.8)
            disk_after = json.loads((inst.data / 'state.json').read_text())['tasks'][running]['status']
            emit('e-RESTART-DISCO', inmediato=disk_before, despues_800ms=disk_after)
            inst.call('pause', '--wait', '-g', 'work')
            emit('e-FLUSH-PAUSE', status=json.loads((inst.data / 'state.json').read_text())['tasks'][running]['status'])
            inst.stop(signal.SIGKILL)
            restored = inst.start()
            emit('e-RESTART-PERSISTIDO', status=restored['tasks'][running]['status'])
    finally:
        inst.close()

inst = Instance('c-persistencia')
try:
    inst.start()
    inst.call('group', 'add', 'work', '--parallel', '2')
    inst.call('pause', '--wait', '-g', 'work')
    stamps = []
    for index in range(8):
        start = time.monotonic()
        task_id = inst.add('echo reciente-' + str(index))
        disk = json.loads((inst.data / 'state.json').read_text())
        stamps.append({'id': task_id, 'en_disco_al_responder': task_id in disk['tasks'],
                       'add_ms': round((time.monotonic() - start) * 1000, 2)})
    before = (inst.data / 'state.json').stat().st_mtime_ns
    time.sleep(1.2)
    emit('c-ESCRITURA', adds=stamps, cambio_en_reposo_1200ms=before != (inst.data / 'state.json').stat().st_mtime_ns)
    inst.stop(signal.SIGKILL)
    after = inst.start()
    emit('c-ADD-SIGKILL', ids=sorted(after['tasks']), grupos=after['groups'])
finally:
    inst.close()
inst = Instance('d-parallel')
try:
    inst.start()
    inst.call('group', 'add', 'work', '--parallel', '1')
    inst.call('parallel', '3', '-g', 'work')
    emit('d-PARALLEL-SIN-FLUSH', memoria=inst.status()['groups'], disco=json.loads((inst.data / 'state.json').read_text())['groups'])
    inst.call('pause', '--wait', '-g', 'work')
    inst.call('group', 'add', 'vacio', '--parallel', '2')
    inst.stop(signal.SIGKILL)
    emit('d-PARALLEL-RESTAURADO', grupos=inst.start()['groups'])
finally:
    inst.close()
emit('FIN', resultado='OK; instancias privadas detenidas y grupos de prueba limpiados')
