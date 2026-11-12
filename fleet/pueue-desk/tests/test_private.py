"""Pueue real 4.0.4, exclusivamente en directorios privados de la corrida."""
import importlib.util
import json
import os
from pathlib import Path
import random
import signal
import subprocess
import time
import unittest

from test_startup import control, ROOT

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
            inst.await_status(task_id, 'Done')
            count = len(sentinel.read_text().splitlines()) if sentinel.exists() else 0
            self.assertLessEqual(count, 1, (index, task_id, count))
            executed += count
            # Evita crecimiento cuadrático y conserva la prueba de ausencia de
            # reutilización: los ids se guardan hasta acabar las 500 iteraciones.
            if index % 100 == 99:
                print('PRIVADA ciclos', index + 1, 'sin duplicados', flush=True)
        print('PRIVADA 500 SIGKILL: efectos=', executed, 'ventanas Queued+log observadas=', observed_log_window)


if __name__ == '__main__':
    unittest.main()
