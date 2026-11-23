"""Contraste de resource-state@1 con la cola y el render del rack, sin hosts reales."""
from contextlib import ExitStack
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_rack import BASE, serve, snapshot
from test_reorder import browser_fixture

NOW = 1794507599
GENERATION = 'pueued-' + 'a' * 64
RUN = '20261112-115145-abcdef'
RESOURCE = 'postgres:5434'
PATH = '.local/state/comprador/mini/admission-v1/resource-state.json'
RESOURCE_WAIT = 'Esperando recurso · postgres:5434 (legado #242)'
SLOT_WAIT = 'Esperando lugar en suites'
LEGACY = 'Corrida del legado · ocupa un lugar'
UNPERMITTED = 'dato-sintetico-privado'


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enter(tempfile.TemporaryDirectory()))
        self.pueue = json.loads((BASE / 'tests/mini-pueue.json').read_text())
        self.pueue['tasks'] = {k: self.pueue['tasks'][k] for k in ('242', '244', '245')}
        self.pueue['groups'] = {'suites': dict(status='Running', parallel_tasks=3)}
        self.legacy = self.pueue['tasks']['242']
        self.legacy['label'] = None
        self.legacy['created_at'] = '2026-11-12T12:00:04.922402123-06:00'
        self.legacy['status'] = {'Running': dict(start=NOW - 1800)}
        self.managed = self.pueue['tasks']['244']
        self.managed.update(label='run=dirigida-espera repo=se-omite sha=abcd123', status='Stashed')
        self.reserved = self.pueue['tasks']['245']
        self.reserved.update(label='run=dirigida-reserva', status={'Queued': dict(enqueued_at=NOW - 60)})
        for i, task in self.pueue['tasks'].items():
            run = self.root / 'remote-run/runs' / (RUN if i == '244' else i)
            run.mkdir(parents=True)
            (run / 'meta').write_text('mirror=' + ('legado' if i == '242' else UNPERMITTED) + '\n'
                                      'cmd=' + UNPERMITTED + '\n', encoding='utf-8')
            (run / 'body.sh').write_text(UNPERMITTED, encoding='utf-8')
            (run / 'log').touch()
            os.utime(run / 'log', (NOW - 1800, NOW - 1800))
            task.update(path=str(run), command=UNPERMITTED, envs={'private': UNPERMITTED})
        blocker = dict(resource=RESOURCE, kind='legacy', task_id=242, ref='unlabelled-242', seq=None)
        self.waiting = self.request(self.managed, 'dirigida-espera', 1, 'waiting', [blocker])
        self.waiting['positions'][RESOURCE] = 2
        self.holding = self.request(self.reserved, 'dirigida-reserva', 2, 'reserved', [])
        self.projection = dict(schema='resource-state@1', status='ok', revision=10, updated_at=NOW - 5,
                               migration=dict(phase='legacy-drain',
                                              barriers=[dict(resource=RESOURCE, task_ids=[242])]),
                               requests=[self.waiting, self.holding])
        self.path = self.root / PATH
        self.path.parent.mkdir(parents=True)
        self.generation = self.enter(patch.object(snapshot, 'admission_generation',
                                                  return_value=GENERATION, create=True))
        for name in ('mac_capacity', 'disk', 'mini_guard', 'collect_usage', 'desk_guard',
                     'linux_capacity', 'desk_orphans'):
            self.enter(patch.object(snapshot, name, return_value=None))

    def enter(self, manager):
        if not hasattr(self, 'stack'):
            self.stack = ExitStack()
            self.addCleanup(self.stack.close)
        return self.stack.enter_context(manager)

    def request(self, task, ref, seq, phase, blockers):
        return dict(task_id=task['id'], task_created_at=task['created_at'], ref=ref, seq=seq,
                    daemon_generation=GENERATION, attempt='12345678-1234-1234-1234-123456789ab' + str(seq),
                    phase=phase, arrived_at=NOW - 120, deadline_at=NOW + 100, payload_started_at=None,
                    updated_at=NOW - 5, resources=[RESOURCE], positions={RESOURCE: 1}, blockers=blockers, cut=None)

    def write_projection(self):
        self.path.write_text(json.dumps(self.projection), encoding='utf-8')

    def collect(self, host='mini'):
        mode = 'macos' if host == 'mini' else 'linux'

        def command(argv):
            if argv == ['queue-fixture', 'status', '--json']:
                return json.dumps(self.pueue)
            if argv[0] == 'git':
                raise OSError('espejo sintético ausente')
            self.fail('comando inesperado en la prueba')

        with patch.object(snapshot, 'command', side_effect=command):
            return snapshot.collect(mode, host, pueue='queue-fixture', home=self.root, proc=self.root, now=NOW)

    def row(self, snap, task_id):
        return next(t for t in snap['tasks'] if t['id'] == task_id)

    def payload(self, snap, reorder=False):
        fixture = json.loads((BASE / 'tests/rack-fixture.json').read_text(encoding='utf-8'))
        host = next(h for h in fixture['hosts'] if h['id'] == snap['host'])
        host['snapshot'] = serve.validate_snapshot(snap, snap['host'])
        capacity = dict(cores=10, load=[1, 1, 1])
        host['snapshot'].update(cpu=capacity, mem=dict(total=16 * 2**30, available=10 * 2**30))
        host['estimates'] = {str(t['id']): dict(low=1800, typical=2100, high=2400, n=5) for t in snap['tasks']}
        if reorder:
            host['snapshot']['control'] = browser_fixture()['hosts'][0]['snapshot']['control']
        fixture.update(now=NOW, hosts=[host], tickets={})
        return fixture

    def render(self, fixture):
        source = (BASE / 'index.html').read_text(encoding='utf-8').split('<script>', 1)[1].split('</script>', 1)[0]
        result = subprocess.run(['node', str(BASE / 'tests/admission-ui-check.js')],
                                input=json.dumps(dict(js=source, fixture=fixture)), encoding='utf-8',
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_resource_wait_stashed_in_group_and_legacy_over_idle(self):
        self.write_projection()
        snap = self.collect()
        html = self.render(self.payload(snap, reorder=True))
        self.assertIn(RESOURCE_WAIT, html, 'la Stashed gestionada aparece en su grupo con su bloqueante')
        li = re.search(r'<li[^>]*data-id="244".*?</li>', html, re.S)[0]
        slot = re.search(r'<div class="slot busy[^>]*data-id="242".*?(?=<div class="slot empty)', html, re.S)[0]
        self.assertIn('data-group="suites"', html)
        self.assertIn(RESOURCE_WAIT, li)
        self.assertIn('data-reorder="false"', li)
        self.assertNotIn('class="bar', li)
        self.assertIn(LEGACY, slot)
        self.assertNotIn('Sin actividad', slot)
        self.assertIn('class="bar', slot)
        self.assertRegex(html, r'>1 <span[^>]*>ocupados')
        self.assertRegex(html, r'<li[^>]*data-id="245"[^>]*data-reorder="true"')
        self.assertEqual(self.row(snap, 244)['admission']['positions'], {RESOURCE: 2})

    def test_wait_for_slot_and_no_progress_before_payload(self):
        self.reserved['status'] = {'Running': dict(start=NOW - 1800)}
        self.write_projection()
        snap = self.collect()
        html = self.render(self.payload(snap))
        self.assertIn(SLOT_WAIT, html, 'el preámbulo del worker no es ejecución de la carga')
        slot = html.split('data-id="245"', 1)[1].split('<div class="slot empty', 1)[0]
        self.assertIn(SLOT_WAIT, slot)
        self.assertNotIn('class="bar', slot)
        self.assertNotIn('Sin actividad', slot)
        self.holding.update(phase='running', payload_started_at=NOW - 120)
        self.write_projection()
        html = self.render(self.payload(self.collect()))
        slot = html.split('data-id="245"', 1)[1].split('<div class="slot empty', 1)[0]
        self.assertIn('class="bar', slot)
        self.assertIn('2 min de', slot)

    def test_identity_mismatch_discards_only_that_request(self):
        expected = [{'task_created_at': self.legacy['created_at']}, {'ref': 'otro-intento'},
                    {'daemon_generation': 'pueued-' + 'b' * 64}, {'task_id': 12345}]
        for identity in expected:
            with self.subTest(identity=identity):
                self.projection['requests'][0] = dict(self.waiting, **identity)
                self.write_projection()
                snap = self.collect()
                admitted = [t['id'] for t in snap['tasks'] if 'admission' in t]
                self.assertEqual(admitted, [245], 'se descarta la identidad ajena y se conserva la válida')
                self.assertNotIn(LEGACY, self.render(self.payload(snap)))
        self.projection['requests'][0] = self.waiting
        self.write_projection()
        self.generation.side_effect = [GENERATION, 'pueued-' + 'b' * 64]
        self.assertFalse(any('admission' in t for t in self.collect()['tasks']))

    def test_managed_metadata_and_projection_privacy(self):
        # A managed run's meta may be missing or may contain metadata that must not be attributed.
        self.write_projection()
        with patch.object(snapshot, 'mini_metadata', wraps=snapshot.mini_metadata) as meta:
            snap = self.collect()
        self.assertEqual([args[0]['id'] for args, _ in meta.call_args_list], [242],
                         'sólo la corrida del legado puede abrir meta')
        for raw in (snap, self.payload(snap), self.render(self.payload(snap))):
            self.assertNotIn(UNPERMITTED, json.dumps(raw))
            self.assertNotIn('se-omite', json.dumps(raw))
        self.assertEqual(self.row(snap, 244)['name'], 'dirigida-espera')
        for container in (self.projection, self.waiting, self.waiting['blockers'][0]):
            container.update(cmd='fixture-projection-command', env='fixture-projection-environment')
        self.write_projection()
        snap = self.collect()
        for raw in (snap, self.payload(snap), self.render(self.payload(snap))):
            self.assertNotIn('fixture-projection-', json.dumps(raw))

    def test_missing_or_invalid_projection_preserves_snapshot_and_view(self):
        baseline = self.collect()
        html = self.render(self.payload(baseline))
        for mode, raw in (
                ('ilegible', b'\xff'), ('otro esquema', json.dumps(dict(self.projection, schema='another@1')).encode()),
                ('grande', b' ' * (1024 * 1024) + b'{}'),
                ('vieja', json.dumps(dict(self.projection, updated_at=NOW - 31)).encode()),
                ('futura', json.dumps(dict(self.projection, updated_at=NOW + 1)).encode()),
                ('estado desconocido', json.dumps(dict(self.projection, status='unknown')).encode()),
                ('campos repetidos', b'{"status":"ok",' + json.dumps(self.projection).encode()[1:]),
                ('demasiadas filas', json.dumps(dict(self.projection, requests=[{}] * 4097)).encode())):
            with self.subTest(mode=mode):
                self.path.write_bytes(raw)
                actual = self.collect()
                self.assertEqual(actual, baseline)
                self.assertEqual(self.render(self.payload(actual)), html)
        self.assertNotIn('data-id="244"', html)
        self.assertIn('Sin actividad', html)
        self.assertEqual(self.generation.call_count, 0)

    def test_desk_ignores_mini_projection(self):
        baseline = self.collect('desk')
        self.write_projection()
        self.assertEqual(self.collect('desk'), baseline)
        self.assertEqual(self.generation.call_count, 0)

    def test_symlink_fifo_and_denied_projection_preserve_native_snapshot(self):
        baseline = self.collect()
        source = self.path.with_name('elsewhere')
        source.write_text(json.dumps(self.projection), encoding='utf-8')
        self.path.symlink_to(source)
        self.assertEqual(self.collect(), baseline)
        self.path.unlink()
        os.mkfifo(self.path)
        self.assertEqual(self.collect(), baseline)
        with patch.object(snapshot, 'read_owned_file', side_effect=PermissionError):
            self.assertEqual(self.collect(), baseline)
        self.generation.assert_not_called()

    def test_server_discards_undeclared_projection_fields(self):
        self.write_projection()
        snap = self.collect()
        self.row(snap, 244)['admission']['blockers'][0]['cmd'] = UNPERMITTED
        clean = serve.validate_snapshot(snap, 'mini')
        self.assertNotIn('admission', self.row(clean, 244))
        self.assertIn('admission', self.row(clean, 245))
        self.assertNotIn(UNPERMITTED, json.dumps(clean))

    def test_newest_attempt_for_same_pueue_identity(self):
        next_attempt = dict(self.waiting, seq=3, attempt='12345678-1234-1234-1234-123456789ab3',
                            phase='reserved', blockers=[])
        self.projection['requests'].insert(0, next_attempt)
        self.write_projection()
        self.assertEqual(self.row(self.collect(), 244)['admission']['seq'], 3)


class AdmissionGenerationTests(unittest.TestCase):
    def test_darwin_generation_matches_hasito_boot_pid_and_birth(self):
        # The observed process and pid file are synthetic. No installed daemon is queried.
        boot = '12345678-1234-1234-1234-123456789abc'
        observed = [boot, 4321, [1794506401, 654321]]
        expected = 'pueued-' + hashlib.sha256(json.dumps(observed).encode()).hexdigest()

        def pidinfo(pid, flavor, argument, output, size):
            self.assertEqual((pid, flavor, argument, size), (4321, 3, 0, 136))
            info = ctypes.cast(output, ctypes.POINTER(snapshot.AdmissionBSDInfo)).contents
            info.pid, info.status, info.comm = pid, 2, b'pueued'
            info.sec, info.usec = observed[2]
            return size

        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            pidfile = home / 'Library/Application Support/pueue/pueue.pid'
            pidfile.parent.mkdir(parents=True)
            pidfile.write_text('4321\n')
            with patch.object(snapshot.ctypes, 'CDLL') as library, patch.object(snapshot, 'command', return_value=boot):
                library.return_value.proc_pidinfo.side_effect = pidinfo
                self.assertEqual(snapshot.admission_generation(home), expected)


if __name__ == '__main__':
    unittest.main()
