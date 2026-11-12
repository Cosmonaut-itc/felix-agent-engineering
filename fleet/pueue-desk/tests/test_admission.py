from copy import deepcopy
from pathlib import Path
import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_startup import control, queue


class FakePueue:
    def __init__(self):
        self.state = queue()
        self.calls = []
        self.before_pause = lambda group: None

    def status(self):
        return deepcopy(self.state)

    def parallel(self, group, value):
        self.state['groups'][group]['parallel_tasks'] = value

    def call(self, *args):
        self.calls.append(args)
        if args[0] == 'pause':
            self.before_pause(args[-1])
            self.state['groups'][args[-1]]['status'] = 'Paused'
        elif args[0] == 'start':
            self.state['groups'][args[-1]]['status'] = 'Running'
        else:
            raise AssertionError(args)
        return ''


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.pueue = FakePueue()
        self.pueue.state['groups'].update({g: {'status': 'Running', 'parallel_tasks': 1}
                                          for g in control.GROUP_LIMITS})
        self.low = patch.object(control, 'pressure', return_value={'io': 0, 'cpu': 0, 'memory': 8 * 1024**3})
        self.low.start()
        self.addCleanup(self.low.stop)

    def guard(self, ready=True, host='desk'):
        with patch.object(control, 'docker_ready', return_value=ready):
            control.guard(self.pueue, Path('/nonexistent'), self.directory, host=host)

    def test_docker_down_closes_and_returns_only_owned_on_low_pressure(self):
        self.pueue.state['groups']['jale']['status'] = 'Paused'
        self.guard(ready=False)
        self.assertFalse(any(c[0] == 'start' for c in self.pueue.calls))
        self.assertNotIn('jale', control.load_owned(self.directory))
        control.save_owned(self.directory, control.load_owned(self.directory) | {'default'})
        self.pueue.state['groups']['default']['status'] = 'Paused'
        self.guard()
        self.assertEqual(self.pueue.state['groups']['default']['status'], 'Running')
        self.assertEqual(self.pueue.state['groups']['jale']['status'], 'Paused')
        self.assertEqual(control.load_owned(self.directory), set())

    def test_ownership_is_written_before_pause_even_if_killed(self):
        def abort(group):
            self.assertIn(group, control.load_owned(self.directory))
            raise RuntimeError('muerte entre intención y pausa')
        self.pueue.before_pause = abort
        with self.assertRaises(RuntimeError):
            self.guard(ready=False)
        self.pueue.before_pause = lambda group: None
        self.guard(ready=False)
        self.assertTrue(control.load_owned(self.directory))
        self.guard()
        self.assertEqual(control.load_owned(self.directory), set())

    def test_reanudar_requests_admission_and_keeps_paused_on_docker_failure(self):
        self.pueue.state['groups']['hasito']['status'] = 'Paused'
        control.save_owned(self.directory, set())
        with patch.object(control, 'docker_ready', return_value=False):
            control.request_resume(self.pueue, 'hasito', self.directory, Path('/nonexistent'), host='desk')
        self.assertEqual(self.pueue.state['groups']['hasito']['status'], 'Paused')
        self.assertIn('hasito', control.load_owned(self.directory))
        self.assertFalse(any(c[0] == 'start' for c in self.pueue.calls))
        self.guard()
        self.assertEqual(self.pueue.state['groups']['hasito']['status'], 'Running')

    def test_reanudar_respects_pressure(self):
        self.pueue.state['groups']['hasito']['status'] = 'Paused'
        with patch.object(control, 'docker_ready', return_value=True), patch.object(
                control, 'pressure', return_value={'io': 90, 'cpu': 0, 'memory': 8 * 1024**3}):
            control.request_resume(self.pueue, 'hasito', self.directory, Path('/nonexistent'), host='desk')
        self.assertEqual(self.pueue.state['groups']['hasito']['status'], 'Paused')
        self.assertFalse(any(c[0] == 'start' for c in self.pueue.calls))

    def test_mini_does_not_require_docker_or_proc_and_preserves_manual_pause(self):
        control.save_owned(self.directory, {'suites'})
        self.pueue.state['groups']['suites']['status'] = 'Paused'
        with patch.object(control, 'docker_ready', side_effect=AssertionError('Docker en Mini')), patch.object(
                control, 'pressure', side_effect=AssertionError('PSI en Mini')):
            control.guard(self.pueue, Path('/nonexistent'), self.directory, host='mini')
        self.assertEqual(self.pueue.state['groups']['suites']['status'], 'Running')
        self.assertEqual(self.pueue.state['groups']['manual']['status'], 'Paused')

    def test_logs_are_bounded_even_with_large_errors(self):
        for _ in range(4):
            control.log(self.directory, 'error', detalle='x' * (control.LOG_MAX_BYTES * 2))
        for path in self.directory.glob('*.log*'):
            self.assertLessEqual(path.stat().st_size, control.LOG_MAX_BYTES)
        for line in (self.directory / 'guardia.log').read_text().splitlines():
            json.loads(line)


if __name__ == '__main__':
    unittest.main()


class DockerTimeoutTests(unittest.TestCase):
    def test_docker_timeout_and_missing_binary_are_closed(self):
        for error in (FileNotFoundError(), subprocess.TimeoutExpired('docker', 3)):
            with patch.object(control.subprocess, 'run', side_effect=error) as call:
                self.assertFalse(control.docker_ready('/fake/docker'))
                self.assertEqual(call.call_args.kwargs['timeout'], 3)
