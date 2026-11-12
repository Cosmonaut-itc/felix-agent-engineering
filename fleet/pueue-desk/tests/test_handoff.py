"""SIGKILL real al solicitante, launchd simulado y reconciliación independiente.

El doble cuenta daemons efectivos, no sólo si un socket responde. No modifica
LaunchAgents ni servicios del usuario. Cada escenario vive bajo el cwd remoto.
"""
from copy import deepcopy
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from test_startup import ROOT, NOW, task

spec = importlib.util.spec_from_file_location('mini_handoff', ROOT / 'mini.py')
mini = importlib.util.module_from_spec(spec)
# La implementación se carga al descubrir pruebas, después de la medición previa.
if spec.loader is not None and (ROOT / 'mini.py').exists():
    spec.loader.exec_module(mini)


class FakeMachine:
    """Estado durable al que acceden el solicitante y el job en procesos separados."""
    def __init__(self, home, checkpoint=lambda _name: None):
        self.checkpoint = checkpoint
        self.home = home
        self.file = home / 'machine.json'

    def read(self):
        return json.loads(self.file.read_text())

    def write(self, value):
        mini.control.atomic_json(self.file, value)

    def status(self):
        value = self.read()
        if not value['daemons']:
            raise subprocess.CalledProcessError(1, ['pueue', 'status'])
        assert len(value['daemons']) == 1, value
        return deepcopy(value['queue'])

    def parallel(self, group, number):
        value = self.read()
        value['queue']['groups'][group]['parallel_tasks'] = number
        self.write(value)
        self.checkpoint('pueue:parallel:' + group)

    def call(self, *args):
        value = self.read()
        assert len(value['daemons']) == 1
        if args[0] == 'pause':
            value['queue']['groups'][args[-1]]['status'] = 'Paused'
        elif args[0] == 'start':
            value['queue']['groups'][args[-1]]['status'] = 'Running'
        else:
            raise AssertionError(args)
        self.write(value)
        self.checkpoint('pueue:' + args[0] + ':' + args[-1])
        return ''

    def launch(self, *args, check=True):
        value = self.read()
        op = args[0]
        label = Path(args[-1]).stem if op == 'bootstrap' else args[-1].split('/')[-1]
        code = 0
        if op == 'print':
            code = 0 if label in value['loaded'] else 113
        elif op in ('disable', 'enable'):
            value['disabled'][label] = op == 'disable'
        elif op == 'bootout':
            if label not in value['loaded']:
                code = 113
            else:
                value['loaded'].remove(label)
                if label in value['daemons']:
                    value['daemons'].remove(label)
        elif op == 'kickstart':
            code = 0 if label in value['loaded'] else 113
        elif op == 'bootstrap':
            if label in value['loaded']:
                code = 5
            else:
                value['loaded'].append(label)
                if label in (mini.LABEL, mini.BREW_LABEL):
                    assert not value['daemons'], 'dos daemons: ' + repr(value)
                    value['daemons'].append(label)
        else:
            raise AssertionError(args)
        if len(value['daemons']) > value['maximum']:
            value['maximum'] = len(value['daemons'])
        self.write(value)
        if check and code:
            raise subprocess.CalledProcessError(code, ['launchctl', *args])
        return subprocess.CompletedProcess(args, code, '', '')


def manager(home, checkpoint=lambda _label: None):
    machine = FakeMachine(home, checkpoint)
    return mini.Manager(home=home, pueue=machine, launch=machine.launch, checkpoint=checkpoint)


def request(home, action=None, kill_at=None, record=None):
    count = 0
    def checkpoint(label):
        nonlocal count
        count += 1
        if record is not None:
            record.append(label)
        if count == kill_at:
            os.kill(os.getpid(), signal.SIGKILL)
    original = mini.control.save_owned
    def save_owned(directory, owned):
        original(directory, owned)
        checkpoint('owned_durable')
    with patch.object(mini.control, 'save_owned', side_effect=save_owned):
        instance = manager(home, checkpoint)
        if action is None:
            instance.reconcile()
        else:
            instance.change(action, False, wait_minutes=0, preflight=False)


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.context = multiprocessing.get_context('fork')

    def fresh(self, name, initial='brew'):
        home = self.root / name
        home.mkdir()
        agents = home / 'Library/LaunchAgents'
        agents.mkdir(parents=True)
        (agents / (mini.BREW_LABEL + '.plist')).write_text('brew fixture')
        source = mini.BREW_LABEL if initial == 'brew' else mini.LABEL
        loaded = [source]
        if initial != 'brew':
            loaded.append(mini.GUARD_LABEL)
            library = home / '.local/lib/pao-pueue-desk'
            library.mkdir(parents=True)
            for name in ('control.py', 'mini.py'):
                (library / name).write_text((ROOT / name).read_text())
            for label in (mini.LABEL, mini.GUARD_LABEL):
                (agents / (label + '.plist')).write_text('own fixture')
        FakeMachine(home).write({'loaded': loaded, 'disabled': {}, 'daemons': [source], 'maximum': 1,
            'queue': {'tasks': {}, 'groups': {
                'suites': {'status': 'Running', 'parallel_tasks': 3},
                'default': {'status': 'Running', 'parallel_tasks': 1},
                'manual': {'status': 'Paused', 'parallel_tasks': 1}}}})
        return home

    def assert_healthy(self, home):
        machine = FakeMachine(home)
        value = machine.read()
        self.assertEqual(len(value['daemons']), 1, value)
        self.assertEqual(value['maximum'], 1)
        self.assertEqual(value['queue']['groups']['suites']['status'], 'Running')
        self.assertEqual(value['queue']['groups']['manual']['status'], 'Paused')
        self.assertEqual(mini.control.load_owned(manager(home).directory), set())

    def test_sigkill_at_every_install_and_withdraw_boundary(self):
        for action, initial in (('install', 'brew'), ('withdraw', 'own')):
            reference = self.fresh(action + '-reference', initial)
            steps = []
            request(reference, action, record=steps)
            self.assert_healthy(reference)
            self.assertGreater(len(steps), 8)
            for index, label in enumerate(steps, 1):
                with self.subTest(action=action, boundary=index, label=label):
                    home = self.fresh(action + '-' + str(index), initial)
                    child = self.context.Process(target=request, args=(home, action, index))
                    child.start()
                    child.join(10)
                    if child.is_alive():
                        child.kill()
                        child.join(5)
                        self.fail('solicitante bloqueado')
                    self.assertEqual(child.exitcode, -signal.SIGKILL)
                    # Es otro proceso lógico, como StartInterval: no queda el
                    # instalador ni se ejecuta finally, atexit o un handler.
                    machine = FakeMachine(home)
                    if mini.GUARD_LABEL in machine.read()['loaded']:
                        started = time.monotonic()
                        job = self.context.Process(target=lambda: manager(home).reconcile())
                        job.start()
                        job.join(10)
                        self.assertFalse(job.is_alive())
                        self.assertEqual(job.exitcode, 0)
                        # Peor espera hasta StartInterval + tiempo real de reparación.
                        self.assertLess(15 + time.monotonic() - started, 60)
                    self.assert_healthy(home)
            print(f'W6 {action}: SIGKILL en {len(steps)} fronteras; un daemon, suites abierta, pausa manual intacta')

    def test_reconciler_can_be_killed_and_retried_at_every_boundary(self):
        for action, initial in (('install', 'brew'), ('withdraw', 'own')):
            reference = self.fresh('job-ref-' + action, initial)
            request_steps = []
            request(reference, action, record=request_steps)
            intent_index = request_steps.index('intencion') + 1
            def pending(home):
                child = self.context.Process(target=request, args=(home, action, intent_index))
                child.start()
                child.join(10)
                self.assertFalse(child.is_alive())
                self.assertEqual(child.exitcode, -signal.SIGKILL)
            trace_home = self.fresh('job-trace-' + action, initial)
            pending(trace_home)
            job_steps = []
            request(trace_home, record=job_steps)
            self.assert_healthy(trace_home)
            for index, label in enumerate(job_steps, 1):
                with self.subTest(action=action, job_boundary=index, label=label):
                    home = self.fresh('job-' + action + '-' + str(index), initial)
                    pending(home)
                    job = self.context.Process(target=request, args=(home, None, index))
                    job.start()
                    job.join(10)
                    self.assertFalse(job.is_alive())
                    self.assertEqual(job.exitcode, -signal.SIGKILL)
                    if mini.GUARD_LABEL in FakeMachine(home).read()['loaded']:
                        manager(home).reconcile()
                    self.assert_healthy(home)
            print(f'W6 job {action}: SIGKILL en {len(job_steps)} fronteras y reintento sano')

    def test_async_bootstrap_and_sigkill_before_socket_response(self):
        for action, initial in (('install', 'brew'), ('withdraw', 'own')):
            with self.subTest(action=action):
                home = self.fresh('async-' + action, initial)
                desired = mini.LABEL if action == 'install' else mini.BREW_LABEL
                machine = FakeMachine(home)
                # Bootstrap retorna antes de que preparar tome control.lock.
                def launch(*args, check=True):
                    result = machine.launch(*args, check=check)
                    if args[0] == 'bootstrap' and Path(args[-1]).stem == desired:
                        state = machine.read()
                        state['daemons'] = []
                        state['pending'] = desired
                        machine.write(state)
                    return result
                def interrupted():
                    instance = manager(home)
                    instance.launch = launch
                    instance.change(action, False, preflight=False)
                    os.kill(os.getpid(), signal.SIGKILL)
                child = self.context.Process(target=interrupted)
                child.start()
                child.join(10)
                self.assertFalse(child.is_alive())
                self.assertEqual(child.exitcode, -signal.SIGKILL)
                self.assertEqual(machine.read()['daemons'], [])
                self.assertTrue(manager(home).transaction.exists())
                # Otro tick mientras el destino sigue arrancando no pierde
                # intención ni carga un segundo daemon.
                manager(home).reconcile()
                state = machine.read()
                self.assertEqual(state['daemons'], [])
                state['daemons'] = [state.pop('pending')]
                machine.write(state)
                manager(home).reconcile()
                self.assert_healthy(home)

    def test_running_task_aborts_without_stopping_daemon(self):
        home = self.fresh('busy')
        machine = FakeMachine(home)
        before = machine.read()
        before['queue']['tasks'] = {'0': task(0, 'Running')}
        machine.write(before)
        with self.assertRaisesRegex(RuntimeError, 'tareas'):
            request(home, 'install')
        self.assert_healthy(home)
        self.assertEqual(machine.read()['daemons'], [mini.BREW_LABEL])
        self.assertIn('Running', machine.read()['queue']['tasks']['0']['status'])

    def test_idempotent_install_then_withdraw_with_queued_task(self):
        home = self.fresh('twice')
        machine = FakeMachine(home)
        value = machine.read()
        value['queue']['tasks'] = {'0': task(0)}
        machine.write(value)
        for action in ('install', 'install', 'withdraw', 'withdraw'):
            request(home, action)
            self.assert_healthy(home)
            self.assertEqual(machine.read()['queue']['tasks'], value['queue']['tasks'])

    def test_interval_and_daemon_output_are_bounded(self):
        import plistlib
        guard = plistlib.loads((ROOT / 'launchagents' / (mini.GUARD_LABEL + '.plist')).read_bytes())
        daemon = plistlib.loads((ROOT / 'launchagents' / (mini.LABEL + '.plist')).read_bytes())
        self.assertLessEqual(guard['StartInterval'], 15)
        self.assertEqual(daemon['ThrottleInterval'], 5)
        self.assertTrue(daemon['KeepAlive'])
        for plist in (guard, daemon):
            self.assertEqual(plist['StandardOutPath'], '/dev/null')
            self.assertEqual(plist['StandardErrorPath'], '/dev/null')
        self.assertIn('preparar', daemon['ProgramArguments'])
        self.assertIn('--exec', daemon['ProgramArguments'])


if __name__ == '__main__':
    unittest.main()
