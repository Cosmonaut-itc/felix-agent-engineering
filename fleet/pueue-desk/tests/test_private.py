"""Pueue real 4.0.4, exclusivamente en directorios privados de la corrida."""
import importlib.util
import json
import os
from pathlib import Path
import random
import signal
import subprocess
import time
import tempfile
import unittest

from test_startup import control, ROOT, queue, task

spec = importlib.util.spec_from_file_location('private_fixture', ROOT / 'evidence/diag-u17a.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


@unittest.skipUnless(os.environ.get('PAO_PUEUE_PRIVATE') == '1', 'requiere corrida privada en Mini')
class PrivateTests(unittest.TestCase):
    sequence = 0

    def instance(self):
        type(self).sequence += 1
        instance = fixture.Instance('test-' + str(self.sequence))
        self.addCleanup(instance.close)
        return instance

    def prepare(self, instance):
        directory = instance.root / 'control'
        with control.locked(directory):
            control.prepare_start(instance, directory, instance.data / 'state.json', 'mini', '/opt/homebrew/bin/pueued')
        return directory

    def open(self, instance, directory):
        with control.locked(directory):
            control.guard(instance, Path('/no-proc'), directory, host='mini')

    def wait_file(self, path):
        for _ in range(200):
            if path.exists():
                return
            time.sleep(.01)
        self.fail('Falta marcador ' + str(path))

    def test_term_and_kill_leave_interrupted_failed_and_queued_runs_once(self):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(signal=sig):
                inst = self.instance()
                inst.start()
                inst.call('group', 'add', 'suites', '--parallel', '1')
                inst.call('group', 'add', 'manual')
                inst.call('pause', '--wait', '-g', 'manual')
                pid_file = inst.root / 'pid'
                interrupted = inst.root / 'interrupted'
                pending = inst.root / 'pending'
                running = inst.add('echo inicio >> ' + str(interrupted) + '; echo $$ > ' + str(pid_file) + '; exec sleep 600', 'suites')
                inst.await_status(running, 'Running')
                self.wait_file(pid_file)
                inst.children.append(os.getpgid(int(pid_file.read_text())))
                queued = inst.add('echo pendiente >> ' + str(pending), 'suites')
                inst.stop(sig)
                directory = self.prepare(inst)
                inst.start()
                self.assertFalse(pending.exists())
                self.assertEqual(inst.status()['tasks'][running]['status']['Done']['result'], 'Killed')
                self.open(inst, directory)
                self.assertEqual(inst.await_status(queued, 'Done')['status']['Done']['result'], 'Success')
                self.assertEqual(interrupted.read_text().splitlines(), ['inicio'])
                self.assertEqual(pending.read_text().splitlines(), ['pendiente'])
                self.assertEqual(inst.status()['groups']['manual']['status'], 'Paused')
                inst.stop()
                print('PRIVADA interrupción', signal.Signals(sig).name, 'Killed; pendiente conserva id y corre una vez')

    def test_old_log_fails_visibly_and_group_stays_closed_until_resume(self):
        inst = self.instance()
        inst.start()
        inst.call('group', 'add', 'suites')
        inst.call('pause', '--wait', '-g', 'suites')
        sentinel = inst.root / 'must-not-run'
        logged = inst.add('echo repetida >> ' + str(sentinel), 'suites')
        next_id = inst.add('echo siguiente >> ' + str(sentinel), 'suites')
        inst.stop()
        log = inst.data / 'task_logs' / (logged + '.log')
        log.write_text('log anterior\n')
        os.utime(log, (1, 1))
        directory = self.prepare(inst)
        first = (inst.data / 'state.json').read_bytes()
        self.prepare(inst)
        self.assertEqual((inst.data / 'state.json').read_bytes(), first)
        inst.start()
        self.open(inst, directory)
        time.sleep(.35)
        self.assertFalse(sentinel.exists())
        self.assertEqual(inst.status()['tasks'][logged]['status']['Done']['result'], 'Killed')
        with control.locked(directory):
            control.request_resume(inst, 'suites', directory, Path('/no-proc'), host='mini')
        inst.await_status(next_id, 'Done')
        self.assertEqual(sentinel.read_text().splitlines(), ['siguiente'])

    def test_empty_group_add_waits_for_admission(self):
        inst = self.instance()
        inst.start()
        inst.stop()
        directory = self.prepare(inst)
        state = inst.start()
        self.assertEqual(state['groups']['suites']['status'], 'Paused')
        sentinel = inst.root / 'admitted'
        queued = inst.add('echo admitida > ' + str(sentinel), 'suites')
        time.sleep(.35)
        self.assertFalse(sentinel.exists())
        self.open(inst, directory)
        inst.await_status(queued, 'Done')
        self.assertEqual(sentinel.read_text().strip(), 'admitida')

    def test_corrupt_state_remains_identical_and_daemon_not_started(self):
        inst = self.instance()
        inst.start()
        inst.stop()
        path = inst.data / 'state.json'
        path.write_bytes(b'{truncado')
        with self.assertRaises(ValueError):
            self.prepare(inst)
        self.assertEqual(path.read_bytes(), b'{truncado')
        with self.assertRaises(subprocess.CalledProcessError):
            inst.status()

    def test_reset_refuses_start_and_stock_pueue_would_reopen_group(self):
        inst = self.instance()
        inst.start()
        inst.stop()
        path = inst.data / 'state.json'
        state = json.loads(path.read_text())
        state['groups']['default']['status'] = 'Reset'
        path.write_text(json.dumps(state))
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'Reset interrumpido'):
            self.prepare(inst)
        self.assertEqual(path.read_bytes(), before)
        # Comparador estándar privado: el loop de Pueue abre Reset sin pasar
        # por el guardia, incluso si el grupo estaba vacío.
        inst.start()
        for _ in range(100):
            if inst.status()['groups']['default']['status'] == 'Running':
                break
            time.sleep(.02)
        self.assertEqual(inst.status()['groups']['default']['status'], 'Running')
        print('PRIVADA Reset: preparador rechaza sin escribir; Pueue estándar lo abre solo')

    def test_500_crashes_never_repeat_a_sentinel(self):
        inst = self.instance()
        inst.start()
        inst.call('group', 'add', 'suites', '--parallel', '1')
        generator = random.Random(17)
        observed_log_window = 0
        executed = 0
        for index in range(500):
            sentinel = inst.root / ('iteration-' + str(index))
            task_id = inst.add('echo inicio >> ' + str(sentinel), 'suites')
            time.sleep(generator.uniform(0, .05))
            inst.stop(signal.SIGKILL)
            disk = json.loads((inst.data / 'state.json').read_text())
            if 'Queued' in disk['tasks'][task_id]['status'] and (inst.data / 'task_logs' / (task_id + '.log')).exists():
                observed_log_window += 1
            directory = self.prepare(inst)
            inst.start()
            self.open(inst, directory)
            result = inst.await_status(task_id, 'Done')
            count = len(sentinel.read_text().splitlines()) if sentinel.exists() else 0
            self.assertLessEqual(count, 1, (index, task_id, count))
            executed += count
            if inst.status()['groups']['suites']['status'] == 'Paused':
                # El caso Queued+log exige intervención por contrato. Resolver
                # sólo el grupo antes del próximo experimento, que usa otro id.
                self.assertEqual(result['status']['Done']['result'], 'Killed')
                with control.locked(directory):
                    control.request_resume(inst, 'suites', directory, Path('/no-proc'), host='mini')
            # Los ids se guardan hasta acabar los 500 ciclos: jamás restart.
            if index % 100 == 99:
                print('PRIVADA ciclos', index + 1, 'sin duplicados', flush=True)
        # Releer al final recoge también un último efecto de un hijo que aún
        # estaba terminando tras el SIGKILL del daemon.
        executed = 0
        time.sleep(.05)
        for sentinel in inst.root.glob('iteration-*'):
            count = len(sentinel.read_text().splitlines())
            self.assertLessEqual(count, 1, sentinel)
            executed += count
        print('PRIVADA 500 SIGKILL: efectos=', executed, 'ventanas Queued+log observadas=', observed_log_window)


@unittest.skipUnless(os.environ.get('PAO_PUEUE_PRIVATE') == '1', 'requiere corrida privada en Mini')
class PrivateCLITests(unittest.TestCase):
    def test_preparar_exec_handles_stale_pid_rejection_and_delayed_start(self):
        for scenario in ('pid ajeno vivo', 'pueued vivo sin socket', 'arranque demorado'):
            with self.subTest(scenario=scenario):
                with tempfile.TemporaryDirectory(dir=fixture.ROOT) as name:
                    home = Path(name)
                    data = home / 'Library/Application Support/pueue'
                    data.mkdir(parents=True)
                    # HOME relativo mantiene el socket Unix bajo su límite de
                    # 104 bytes en la copia larga de remote-run. Cwd y todas las
                    # rutas estándar resuelven dentro de esta corrida; no hay -c
                    # ni socket/ruta personalizados que B pudiera rechazar.
                    env = {'HOME': '.', 'PATH': '/opt/homebrew/bin:/usr/bin:/bin'}
                    (data / 'pueue.yml').write_text(json.dumps({
                        'shared': {}, 'daemon': {'compress_state_file': False}}))
                    path = data / 'state.json'
                    state = queue()
                    del state['groups']['reset']
                    sentinel = home / 'sentinel'
                    item = task(0)
                    item['command'] = item['original_command'] = 'echo una >> ' + str(sentinel)
                    item['path'] = str(home)
                    state['tasks'] = {'0': item}
                    path.write_text(json.dumps(state))
                    directory = home / '.local/state/pao/pueue-desk'
                    pid = data / 'pueue.pid'
                    command = ['/usr/bin/python3', '-I', str(ROOT / 'control.py'),
                               'preparar', '--host', 'mini', '--exec', '/opt/homebrew/bin/pueued']
                    processes = []
                    def client(*args, check=True):
                        return subprocess.run(['/opt/homebrew/bin/pueue', *args], cwd=home,
                            env=env, text=True, capture_output=True, timeout=10, check=check)
                    def launch(argv):
                        process = subprocess.Popen(argv, cwd=home, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        processes.append(process)
                        return process
                    def ready(process):
                        deadline = time.monotonic() + 8
                        while time.monotonic() < deadline:
                            response = client('status', '--json', check=False)
                            if response.returncode == 0:
                                self.assertIsNone(process.poll())
                                return json.loads(response.stdout)
                            if process.poll() is not None:
                                break
                            time.sleep(.05)
                        log = directory / 'guardia.log'
                        self.fail('No arrancó preparar --exec: ' + (log.read_text() if log.exists() else 'sin log'))
                    try:
                        if scenario == 'pueued vivo sin socket':
                            daemon = launch(['/opt/homebrew/bin/pueued'])
                            ready(daemon)
                            daemon.send_signal(signal.SIGSTOP)
                            # La cola probada sigue abierta en disco; sin el
                            # rechazo por PID B la cierra antes de fallar el exec.
                            path.write_text(json.dumps(state))
                            for socket in data.glob('*.socket'):
                                socket.unlink()
                            self.assertNotEqual(client('status', '--json', check=False).returncode, 0)
                            before = path.read_bytes()
                            pid_before = pid.read_bytes()
                            rejected = launch(command)
                            self.assertEqual(rejected.wait(timeout=10), 1)
                            self.assertEqual(path.read_bytes(), before)
                            self.assertEqual(pid.read_bytes(), pid_before)
                            self.assertFalse((directory / 'guardia-state.json').exists())
                            self.assertIsNone(daemon.poll())
                            self.assertIn('sigue vivo', (directory / 'guardia.log').read_text())
                        else:
                            pid.write_text(str(os.getpid()))
                            before = path.read_bytes()
                            if scenario == 'arranque demorado':
                                with control.locked(directory):
                                    process = launch(command)
                                    time.sleep(.3)
                                    self.assertIsNone(process.poll())
                                    self.assertEqual(path.read_bytes(), before)
                                    self.assertFalse(list(data.glob('*.socket')))
                                    self.assertNotEqual(client('status', '--json', check=False).returncode, 0)
                            else:
                                process = launch(command)
                            restored = ready(process)
                            # exec debe conservar el PID que observa el gestor.
                            self.assertEqual(int(pid.read_text()), process.pid)
                            self.assertEqual(restored['groups']['suites']['status'], 'Paused')
                            self.assertEqual(restored['groups']['manual']['status'], 'Paused')
                            self.assertIn('Queued', restored['tasks']['0']['status'])
                            self.assertFalse(sentinel.exists())
                            admitted = subprocess.run(['/usr/bin/python3', '-I', str(ROOT / 'control.py'),
                                'guardia', '--host', 'mini'], cwd=home, env=env, timeout=10)
                            self.assertEqual(admitted.returncode, 0)
                            deadline = time.monotonic() + 5
                            while time.monotonic() < deadline:
                                current = json.loads(client('status', '--json').stdout)
                                if 'Done' in current['tasks']['0']['status']:
                                    break
                                time.sleep(.05)
                            self.assertEqual(current['tasks']['0']['status']['Done']['result'], 'Success')
                            self.assertEqual(sentinel.read_text().splitlines(), ['una'])
                            self.assertEqual(current['groups']['manual']['status'], 'Paused')
                            self.assertEqual(control.load_owned(directory), set())
                        print('PRIVADA CLI:', scenario, 'OK', flush=True)
                    finally:
                        for process in reversed(processes):
                            if process.poll() is None:
                                # SIGKILL retira también el daemon privado detenido.
                                process.kill()
                            process.wait(timeout=10)


if __name__ == '__main__':
    unittest.main()
