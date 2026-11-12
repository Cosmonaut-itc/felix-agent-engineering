"""Contrato U17a: transformación offline y admisión cerrada por defecto."""
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('startup_control', ROOT / 'control.py')
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)
NOW = '2026-11-12T08:00:00+00:00'


def task(i, variant='Queued', group='suites'):
    detail = {'enqueued_at': NOW}
    if variant in ('Running', 'Paused', 'Done'):
        detail['start'] = NOW
    if variant == 'Done':
        detail.update(end=NOW, result='Killed')
    return dict(id=i, created_at=NOW, original_command='echo tarea', command='echo tarea',
                path='/tmp', envs={}, group=group, dependencies=[], priority=0,
                label=None, status={variant: detail})


def queue():
    return {'tasks': {}, 'groups': {
        'default': {'status': 'Running', 'parallel_tasks': 1},
        'suites': {'status': 'Running', 'parallel_tasks': 3},
        'manual': {'status': 'Paused', 'parallel_tasks': 1},
        'reset': {'status': 'Reset', 'parallel_tasks': 1}}}


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'state.json'
        self.directory = self.root / 'control'
        self.directory.mkdir()

    def test_interrupted_killed_once_and_no_requeue(self):
        before = queue()
        before['tasks'] = {'0': task(0, 'Running'), '1': task(1, 'Paused'), '2': task(2)}
        after, owned, events = control.prepare_queue(before, set(), set(), NOW, 'mini')
        for i in ('0', '1'):
            self.assertEqual(after['tasks'][i]['status'], {'Done': {
                'enqueued_at': NOW, 'start': NOW, 'end': NOW, 'result': 'Killed'}})
        self.assertIn('Queued', after['tasks']['2']['status'])
        self.assertEqual(before['tasks']['0']['status'], task(0, 'Running')['status'])
        again, owned2, _ = control.prepare_queue(after, set(), owned, '2027-01-01T00:00:00Z', 'mini')
        self.assertEqual(after, again)
        self.assertEqual(owned, owned2)
        self.assertEqual([e['causa'] for e in events if e['evento'] == 'interrumpida'], ['sin_parada'] * 2)

    def test_queued_any_log_fails_and_blocks_group_until_operator(self):
        before = queue()
        before['tasks'] = {'0': task(0), '1': task(1)}
        after, owned, events = control.prepare_queue(before, {'0'}, {'suites'}, NOW, 'mini')
        self.assertEqual(after['tasks']['0']['status']['Done']['result'], 'Killed')
        self.assertNotIn('suites', owned)
        self.assertEqual(after['groups']['suites']['status'], 'Paused')
        self.assertIn('Queued', after['tasks']['1']['status'])
        self.assertEqual([e['causa'] for e in events if e['evento'] == 'interrumpida'], ['log_existente'])
        again, owned2, _ = control.prepare_queue(after, {'0'}, owned, NOW, 'mini')
        self.assertEqual((again, owned2), (after, owned))

    def test_missing_groups_created_closed_and_reset_preserved(self):
        before = queue()
        del before['groups']['suites']
        after, owned, _ = control.prepare_queue(before, set(), set(), NOW, 'mini')
        self.assertEqual(after['groups']['suites'], {'status': 'Paused', 'parallel_tasks': 3})
        self.assertEqual(owned, {'default', 'suites'})
        self.assertEqual(after['groups']['manual']['status'], 'Paused')
        self.assertEqual(after['groups']['reset']['status'], 'Reset')

    @patch.object(subprocess, 'check_output', return_value='pueued 4.0.4\n')
    def test_invalid_queue_never_written_or_executed(self, _version):
        for content in ('{', '{"tasks":{},"groups":{"x":{"status":"???"}}}'):
            self.path.write_text(content)
            with self.assertRaises((ValueError, KeyError)):
                control.prepare_start(self.dead(), self.directory, self.path, 'mini', '/fake/pueued')
            self.assertEqual(self.path.read_text(), content)
            self.assertFalse((self.directory / 'guardia-state.json').exists())

    def dead(self):
        class Dead:
            def status(self):
                raise subprocess.CalledProcessError(1, ['pueue'])
        return Dead()

    @patch.object(subprocess, 'check_output', return_value='pueued 4.0.4\n')
    def test_ownership_durable_before_queue_and_retry(self, _version):
        value = queue()
        del value["groups"]["reset"]
        self.path.write_text(json.dumps(value))
        original = control.atomic_json
        def fail_on_queue(path, value):
            if path == self.path:
                raise OSError('corte antes de escribir la cola')
            original(path, value)
        with patch.object(control, 'atomic_json', side_effect=fail_on_queue):
            with self.assertRaises(OSError):
                control.prepare_start(self.dead(), self.directory, self.path, 'mini', '/fake/pueued')
        self.assertIn('suites', control.load_owned(self.directory))
        self.assertEqual(json.loads(self.path.read_text())['groups']['suites']['status'], 'Running')
        control.prepare_start(self.dead(), self.directory, self.path, 'mini', '/fake/pueued')
        self.assertEqual(json.loads(self.path.read_text())['groups']['suites']['status'], 'Paused')
        first = self.path.read_bytes()
        control.prepare_start(self.dead(), self.directory, self.path, 'mini', '/fake/pueued')
        self.assertEqual(self.path.read_bytes(), first)

    @patch.object(subprocess, 'check_output', return_value='pueued 4.0.4\n')
    def test_reset_validates_but_refuses_start_without_overwriting(self, _version):
        value = queue()
        self.path.write_text(json.dumps(value))
        self.assertEqual(control.read_queue(self.path), value)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'Reset interrumpido'):
            control.prepare_start(self.dead(), self.directory, self.path, 'mini', '/fake/pueued')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.directory / 'guardia-state.json').exists())

    def test_responding_daemon_rejected_without_mutation(self):
        self.path.write_text(json.dumps(queue()))
        class Alive:
            def status(self):
                return queue()
        before = self.path.read_bytes()
        with self.assertRaises(RuntimeError):
            control.prepare_start(Alive(), self.directory, self.path, 'mini', '/fake/pueued')
        self.assertEqual(self.path.read_bytes(), before)

    def test_compressed_state_rejected(self):
        self.path.with_suffix('.json.gz').write_bytes(b'compressed')
        with self.assertRaises(ValueError):
            control.read_queue(self.path)
        self.assertFalse(self.path.exists())

    @patch.object(subprocess, 'check_output', return_value='pueued 4.1.0\n')
    def test_wrong_version_rejected(self, _version):
        self.path.write_text(json.dumps(queue()))
        before = self.path.read_bytes()
        with self.assertRaises(RuntimeError):
            control.prepare_start(self.dead(), self.directory, self.path, 'mini', '/fake/pueued')
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.data = self.home / 'Library/Application Support/pueue'
        self.data.mkdir(parents=True)
        self.config = self.data / 'pueue.yml'
        for item in (patch.object(control.Path, 'home', return_value=self.home),
                     patch.object(control.sys, 'platform', 'darwin'),
                     patch.dict(os.environ, {}, clear=True)):
            item.start()
            self.addCleanup(item.stop)

    def test_standard_yaml_and_json_without_managing_configuration(self):
        for text in ('shared:\n  pueue_directory: null\n  use_unix_socket: true\ndaemon:\n  compress_state_file: false\n  callback: null\n  env_vars: {}\n  shell_command: null\nclient:\n  dark_mode: false\nprofiles: {}\n',
                     json.dumps({'shared': {}, 'daemon': {'compress_state_file': False}})):
            # Pueue emite profiles: {}. La prueba exige aceptar su YAML real.
            self.config.write_text(text)
            control.validate_configuration()
            self.assertEqual(self.config.read_text(), text)

    def test_custom_paths_compression_and_ambiguous_yaml_rejected(self):
        for value in ({'shared': {'pueue_directory': '/elsewhere'}},
                      {'shared': {'unix_socket_path': '/elsewhere'}},
                      {'daemon': {'compress_state_file': True}}):
            self.config.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                control.validate_configuration()
        self.config.write_text('shared: {pueue_directory: /elsewhere}\n')
        with self.assertRaises(ValueError):
            control.validate_configuration()

    def test_relative_xdg_paths_rejected_before_reading_wrong_queue(self):
        self.config.write_text('{"shared":{}}')
        for key in ('XDG_DATA_HOME', 'XDG_CONFIG_HOME', 'XDG_RUNTIME_DIR'):
            for value in ('relative/path', ''):
                with patch.dict(os.environ, {key: value}):
                    with self.assertRaisesRegex(ValueError, key):
                        control.validate_configuration()

    def test_duplicate_json_and_invalid_rust_numbers_rejected(self):
        path = self.data / 'state.json'
        path.write_text('{"tasks":{},"tasks":{},"groups":{}}')
        with self.assertRaises(ValueError):
            control.read_queue(path)
        for change in ('id', 'result', 'dependency'):
            value = queue()
            item = task(0, 'Done')
            key = '0'
            if change == 'id':
                item['id'] = 2**64
                key = str(2**64)
            elif change == 'result':
                item['status']['Done']['result'] = {'Failed': 2**31}
            else:
                item['dependencies'] = [2**64]
            value['tasks'] = {key: item}
            path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                control.read_queue(path)
