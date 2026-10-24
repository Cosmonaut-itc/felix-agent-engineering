import contextlib
from datetime import datetime, timezone
import fcntl
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('muestra', Path(__file__).resolve().parents[1] / 'muestra.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
GIB = 1024 ** 3


class MuestraTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / 'ultimo.json'
        self.raw = {
            'LocalHostName': 'mini-prueba',
            'kern.boottime': '{ sec = 1792738812, usec = 550262 } Fri Oct 23',
            'kern.memorystatus_level': '77',
            'kern.memorystatus_vm_pressure_level': '1',
            'vm.swapusage': 'total = 1024.00M  used = 450.00M  free = 574.00M  (encrypted)',
            'vm.loadavg': '{ 2.79 3.1 4.0 }',
            'hw.ncpu': '10',
        }
        self.free = 100 * GIB
        self.config = m.leer_umbrales(Path(m.__file__).with_name('umbrales.json'))

    def command(self, argv):
        value = self.raw[argv[-1]]
        if isinstance(value, Exception):
            raise value
        return value

    def stat(self, path):
        self.assertEqual(path, '/System/Volumes/Data')
        return SimpleNamespace(f_frsize=1, f_blocks=500 * GIB, f_bavail=self.free)

    def capture(self):
        return m.capturar(self.config, comando=self.command, reloj=lambda: datetime(2026, 10, 23, 8, tzinfo=timezone.utc), statvfs=self.stat)

    def run_main(self, config=None):
        with patch.object(m, 'ejecutar_comando', self.command), patch.object(m.os, 'statvfs', self.stat):
            return m.main(['--salida', str(self.out), '--umbrales', str(config or Path(m.__file__).with_name('umbrales.json'))])

    def test_normal_contract_and_boot(self):
        s = self.capture()
        self.assertEqual(set(s), {'schema', 'host', 'ts', 'boot', 'status', 'metrics', 'volumes', 'errors', 'persist'})
        self.assertEqual(s['schema'], 'mini-vigilante/muestra@1')
        self.assertEqual(s['status'], 'ok')
        self.assertEqual(s['errors'], [])
        self.assertEqual(s['boot'], {'bootId': 'mini-prueba/2026-10-23T07:00:12.550262Z', 'lastBootUtc': '2026-10-23T07:00:12.550262Z', 'error': None})
        self.assertEqual(s['ts'], '2026-10-23T08:00:00.000000Z')
        for name, value, unit, key in [('memLevelPct', 77, 'pct', 'kern.memorystatus_level'), ('memPressure', 1, 'level', 'kern.memorystatus_vm_pressure_level'), ('swapUsedBytes', 450 * 1024 ** 2, 'bytes', 'vm.swapusage'), ('load1', 2.79, 'load', 'vm.loadavg'), ('ncpu', 10, 'count', 'hw.ncpu')]:
            self.assertEqual(s['metrics'][name], dict(value=value, unit=unit, status='ok', error=None, source='sysctl ' + key))
        self.assertEqual(s['persist'], {'error': None, 'ultimo': 'written'})

    def test_memory_boundaries(self):
        for value, status in [(31, 'ok'), (30, 'warn'), (16, 'warn'), (15, 'crit'), (0, 'crit')]:
            with self.subTest(value=value):
                self.raw['kern.memorystatus_level'] = str(value)
                self.assertEqual(self.capture()['status'], status)

    def test_pressure_exact_levels(self):
        for value, status in [(1, 'ok'), (2, 'warn'), (4, 'crit'), (0, 'unknown'), (3, 'unknown'), (8, 'unknown')]:
            with self.subTest(value=value):
                self.raw['kern.memorystatus_vm_pressure_level'] = str(value)
                s = self.capture()
                self.assertEqual(s['status'], status)
                if status == 'unknown':
                    self.assertIsNone(s['metrics']['memPressure']['value'])
                    self.assertTrue(s['errors'])

    def test_disk_strict_boundaries(self):
        for free, status in [(50 * GIB, 'ok'), (50 * GIB - 1, 'warn'), (25 * GIB, 'warn'), (25 * GIB - 1, 'crit')]:
            with self.subTest(free=free):
                self.free = free
                self.assertEqual(self.capture()['status'], status)

    def test_precedence(self):
        self.raw['hw.ncpu'] = OSError('private/path')
        self.assertEqual(self.capture()['status'], 'unknown')
        self.raw['kern.memorystatus_level'] = '30'
        self.assertEqual(self.capture()['status'], 'warn')
        self.free = 24 * GIB
        self.assertEqual(self.capture()['status'], 'crit')

    def test_each_metric_failure_and_timeout(self):
        for name, key in [('memLevelPct', 'kern.memorystatus_level'), ('memPressure', 'kern.memorystatus_vm_pressure_level'), ('swapUsedBytes', 'vm.swapusage'), ('load1', 'vm.loadavg'), ('ncpu', 'hw.ncpu')]:
            original = self.raw[key]
            for failure in [OSError('/private/secret'), subprocess.TimeoutExpired(['sysctl'], 5), subprocess.CalledProcessError(1, ['sysctl']), 'garbage']:
                with self.subTest(name=name, failure=type(failure).__name__):
                    self.raw[key] = failure
                    s = self.capture()
                    self.assertIsNone(s['metrics'][name]['value'])
                    self.assertEqual(s['metrics'][name]['status'], 'unknown')
                    self.assertTrue(s['metrics'][name]['error'])
                    self.assertEqual(s['status'], 'unknown')
                    self.assertTrue(s['errors'])
                    self.assertNotIn('/private/secret', json.dumps(s))
            self.raw[key] = original

    def test_invalid_swap_tokens_fail_closed(self):
        for raw in ['total = 1024.00M  used = 0.00Q  free = 1024.00M',
                    'total = 1024.00M  used = 450.00Q  free = 574.00M',
                    'total = 1024.00M  used = 4..50M  free = 574.00M',
                    'total = 1024.00M  used = 0.00 Q  free = 1024.00M',
                    'total = 1024.00M  used = 450.00 Q  free = 574.00M',
                    'total = 1024.00M  used = 450.00 M\tfree = 574.00M',
                    'total = 1024.00M  used = 450.00M',
                    'total = 1024.00M  used = 450.00M  free = 574.00M trailing']:
            with self.subTest(raw=raw):
                self.raw['vm.swapusage'] = raw
                sample = self.capture()
                metric = sample['metrics']['swapUsedBytes']
                self.assertIsNone(metric['value'])
                self.assertEqual(metric['status'], 'unknown')
                self.assertTrue(metric['error'])
                self.assertTrue(any(error['stage'] == 'metrics.swapUsedBytes' for error in sample['errors']))
                self.assertNotEqual(sample['status'], 'ok')

    def test_valid_swap_lines_are_fully_parsed(self):
        for raw in [
            'total = 1024.00M  used = 450.00M  free = 574.00M  (encrypted)',
            'total = 1024.00M  used = 450.00M  free = 574.00M',
            'total\t=\t1024.00M\tused\t=\t450.00M\tfree\t=\t574.00M',
        ]:
            with self.subTest(raw=raw):
                self.raw['vm.swapusage'] = raw
                sample = self.capture()
                self.assertEqual(sample['metrics']['swapUsedBytes']['value'], 450 * 1024 ** 2)
                self.assertEqual(sample['metrics']['swapUsedBytes']['status'], 'ok')
                self.assertIsNone(sample['metrics']['swapUsedBytes']['error'])
                self.assertEqual(sample['status'], 'ok')
                self.assertEqual(sample['errors'], [])

    def test_boot_failure(self):
        for raw in [OSError('secret'), '{ sec = 1, usec = 1000000 }', 'garbage']:
            self.raw['kern.boottime'] = raw
            s = self.capture()
            self.assertIsNone(s['boot']['bootId'])
            self.assertIsNone(s['boot']['lastBootUtc'])
            self.assertTrue(s['boot']['error'])
            self.assertEqual(s['status'], 'unknown')
            self.assertTrue(s['errors'])

    def test_host_failure(self):
        self.raw['LocalHostName'] = OSError('secret')
        s = self.capture()
        self.assertIsNone(s['host'])
        self.assertIsNone(s['boot']['bootId'])
        self.assertNotEqual(s['status'], 'ok')

    def test_volume_failure(self):
        with patch.object(self, 'stat', side_effect=OSError('secret')):
            s = self.capture()
        self.assertIsNone(s['volumes']['/System/Volumes/Data']['freeBytes'])
        self.assertEqual(s['status'], 'unknown')
        self.assertTrue(s['errors'])

    def test_persist_success(self):
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(json.loads(self.out.read_text())['status'], 'ok')
        self.assertEqual(sorted(p.name for p in self.out.parent.iterdir()), ['ultimo.json', 'ultimo.lock'])

    def test_atomic_failure_keeps_previous(self):
        self.out.write_text('previous')
        for target in ['fsync', 'replace']:
            with self.subTest(target=target), patch.object(m.os, target, side_effect=OSError('/private/secret')), contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(self.run_main(), 3)
                self.assertEqual(self.out.read_text(), 'previous')
                self.assertEqual(len(err.getvalue().splitlines()), 1)
                self.assertNotIn('/private/secret', err.getvalue())
        self.assertFalse(list(self.out.parent.glob('*.tmp')))

    def test_second_process_busy(self):
        self.out.write_text('previous')
        with self.out.with_name('ultimo.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            p = subprocess.run(['/usr/bin/python3', m.__file__, '--salida', str(self.out)], capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stderr.strip(), 'busy')
        self.assertEqual(self.out.read_text(), 'previous')

    def test_bad_config_has_no_snapshot(self):
        path = self.out.parent / 'config.json'
        for content in [None, '{', '{}', 'null']:
            if content is not None:
                path.write_text(content)
            with self.subTest(content=content), contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(self.run_main(path), 2)
                self.assertIn('umbrales', err.getvalue())
                self.assertFalse(self.out.exists())

    def test_boolean_threshold_has_no_snapshot(self):
        path = self.out.parent / 'config.json'
        config = json.loads(Path(m.__file__).with_name('umbrales.json').read_text())
        config['memLevelPct']['crit'] = False
        config['memLevelPct']['warn'] = 30
        path.write_text(json.dumps(config))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.run_main(path), 2)
        self.assertIn('umbrales', err.getvalue())
        self.assertFalse(self.out.exists())

    def test_external_command_timeout(self):
        with patch.object(m.subprocess, 'run', return_value=SimpleNamespace(stdout='77')) as run:
            self.assertEqual(m.ejecutar_comando(['/usr/sbin/sysctl', '-n', 'kern.memorystatus_level']), '77')
        self.assertEqual(run.call_args.kwargs['timeout'], 5)
        self.assertFalse(run.call_args.kwargs.get('shell', False))

    def test_external_command_forces_c_locale(self):
        inherited = {'LC_ALL': 'es_ES.UTF-8', 'LC_NUMERIC': 'es_ES.UTF-8', 'LANG': 'es_ES.UTF-8', 'PATH': '/usr/bin'}
        with patch.dict(m.os.environ, inherited, clear=True), \
                patch.object(m.subprocess, 'run', return_value=SimpleNamespace(stdout='77')) as run:
            m.ejecutar_comando(['/usr/sbin/sysctl', '-n', 'vm.swapusage'])
            self.assertEqual(m.os.environ['LC_ALL'], 'es_ES.UTF-8')
        env = run.call_args.kwargs['env']
        self.assertEqual(env['LC_ALL'], 'C')
        self.assertEqual(env['PATH'], '/usr/bin')


if __name__ == '__main__':
    unittest.main()
