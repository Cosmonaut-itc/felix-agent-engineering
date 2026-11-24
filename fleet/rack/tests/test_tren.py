import io
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from test_rack import BASE, module, sample, serve, task

tren = module('tren')
receiver = module('recibir_tren')
FIXTURE = Path(__file__).with_name('tren-host.json')
NOW = 1795480000.0
REPOS = ('Example/Train', 'Example/Shadow')


def fixture():
    return json.loads(FIXTURE.read_text())


def at(data, path):
    for part in path:
        data = data[part]
    return data


ROW = ('repos', 0)
PAUSE = ROW + ('pausa',)
BATCH = ROW + ('lote',)
SUITE = BATCH + ('suite', 0)
MINI_SUITE = BATCH + ('suite', 1)
TRIAL = BATCH + ('ensayo',)
WAITING = ROW + ('espera', 0)
RECENT = ROW + ('recientes', 0)
FINISHED = RECENT + ('miembros', 0)
RETURN = FINISHED + ('devolucion',)


class ValidatorTests(unittest.TestCase):
    def test_accepts_and_reconstructs_fixture(self):
        data = fixture()
        clean = tren.valid_tren(data, REPOS, NOW)
        self.assertEqual(clean, data)
        at(data, SUITE + ('lugar',))['pueue_id'] = -1
        self.assertEqual(clean, fixture())

    def test_rejects_extra_keys_at_every_level(self):
        paths = ((), ROW, PAUSE, BATCH, BATCH + ('miembros', 0), SUITE, SUITE + ('lugar',),
                 TRIAL, TRIAL + ('lugar',), WAITING, RECENT, FINISHED, RETURN, ('errors', 0))
        for path in paths:
            with self.subTest(path=path):
                data = fixture()
                at(data, path)['privado'] = 'fuera del contrato'
                with self.assertRaises(ValueError):
                    tren.valid_tren(data, REPOS, NOW)

    def test_rejects_pause_details(self):
        for key in ('detalle', 'archivos'):
            with self.subTest(key=key):
                data = fixture()
                at(data, PAUSE)[key] = '/privado/prueba'
                with self.assertRaises(ValueError):
                    tren.valid_tren(data, REPOS, NOW)

    def test_rejects_comment_url_for_other_repo(self):
        data = fixture()
        at(data, RETURN)['comentario_url'] = 'https://github.com/Example/Other/pull/905#issuecomment-123'
        with self.assertRaises(ValueError):
            tren.valid_tren(data, REPOS, NOW)

    def test_rejects_comment_url_for_other_pr_or_with_suffix(self):
        for value in ('https://github.com/Example/Train/pull/906#issuecomment-123',
                      'https://github.com/Example/Train/pull/905#issuecomment-123?ref=1'):
            with self.subTest(value=value):
                data = fixture()
                at(data, RETURN)['comentario_url'] = value
                with self.assertRaises(ValueError):
                    tren.valid_tren(data, REPOS, NOW)

    def test_rejects_unlisted_and_duplicate_repo(self):
        with self.assertRaises(ValueError):
            tren.valid_tren(fixture(), REPOS[:1], NOW)
        data = fixture()
        data['repos'][1]['repo'] = REPOS[0]
        with self.assertRaises(ValueError):
            tren.valid_tren(data, REPOS, NOW)

    def test_rejects_invalid_dates_phases_types_and_ranges(self):
        cases = [
            ((), 'taken', NOW - 86401), ((), 'taken', NOW + 301), ((), 'taken', True),
            ((), 'taken', float('nan')), (SUITE, 'inicio', float('inf')),
            (PAUSE, 'desde', NOW - 30 * 86400 - 1), (BATCH, 'cierre', NOW + 301),
            (RECENT, 'fin', NOW - 12 * 3600 - 1), (BATCH, 'fase', 'nueva'),
            (BATCH, 'miembros', []), (TRIAL, 'prs', [True]), (TRIAL, 'hechos', -1),
            (SUITE, 'intento', 100), (SUITE, 'fallas', 10**5 + 1),
            (SUITE + ('lugar',), 'vm', 'vm-prueba'), (WAITING, 'pr', 10**7 + 1),
            (WAITING, 'head', 'a' * 40), (WAITING, 'motivo', 'inventado'),
            (TRIAL, 'unidad', '/privado/prueba'), (TRIAL, 'unidad', 'a/../prueba'),
            (RETURN, 'unidad', 'prueba /privado/prueba'), (RETURN, 'unidad', 'C:\\privado\\prueba'),
            (RETURN, 'unidad', 'prueba\nprivada'),
        ]
        for path, field, value in cases:
            with self.subTest(path=path, field=field, value=value):
                data = fixture()
                at(data, path)[field] = value
                with self.assertRaises(ValueError):
                    tren.valid_tren(data, REPOS, NOW)


class TrenStateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = self.root / 'rack.json'
        self.config.write_text(json.dumps({'tren': {'repos': REPOS}}))
        self.state = self.root / 'state'
        self.path = self.state / 'tren.json'
        self.hosts = [dict(id=host, name=host.title(), transport='local', argv=['false'])
                      for host in ('mini', 'desk')]
        self.rack = serve.Rack(self.hosts, self.state, self.root / 'no-linear', tren_repos=REPOS)
        self.data = fixture()

    def receive(self, raw=None):
        with patch.object(receiver.time, 'time', return_value=NOW):
            return receiver.receive(io.BytesIO(raw if raw is not None else json.dumps(self.data).encode()),
                                    self.config, self.state)

    def store(self):
        self.assertEqual(self.receive(), 0)

    def cycle(self, now=NOW, **tasks):
        def fetch(host):
            return dict(sample(), host=host['id'], tasks=tasks.get(host['id'], []))
        with patch.object(serve.subprocess, 'run',
                          return_value=subprocess.CompletedProcess([], 0, stdout='{"ref":"sin-ticket","title":"Prueba"}')):
            self.rack.cycle(fetch=fetch, now=now)
        return self.rack.read()['tren']

    def test_receiver_cli_in_isolated_mode(self):
        payload = dict(schema='tren-host@1', taken=time.time(), repos=[self.data['repos'][1]], errors=[])
        args = ['/usr/bin/python3', '-I', '-B', str(BASE / 'recibir_tren.py'),
                '--config', str(self.config), '--state-dir', str(self.state)]
        good = subprocess.run(args, input=json.dumps(payload).encode(), capture_output=True, timeout=5)
        self.assertEqual(good.returncode, 0, good.stderr)
        saved = json.loads(self.path.read_text())
        received = saved.pop('received_at')
        self.assertGreaterEqual(received, payload['taken'])
        self.assertEqual(saved, payload)
        bad = subprocess.run(args, input=b'{}', capture_output=True, timeout=5)
        self.assertEqual(bad.returncode, 65, bad.stderr)

    def test_receiver_exact_size_and_oversize_preserve_last_good(self):
        raw = FIXTURE.read_bytes()
        limit = raw.ljust(tren.MAX_DOCUMENT, b' ')
        self.assertEqual(self.receive(limit), 0)
        saved = self.path.read_bytes()
        self.assertEqual(self.receive(limit + b' '), 65)
        self.assertEqual(self.path.read_bytes(), saved)

    def test_receiver_times_out_without_eof_and_preserves_last_good(self):
        self.store()
        saved = self.path.read_bytes()
        payload = dict(schema='tren-host@1', taken=time.time(), repos=[self.data['repos'][1]], errors=[])
        args = ['/usr/bin/python3', '-I', '-B', str(BASE / 'recibir_tren.py'),
                '--config', str(self.config), '--state-dir', str(self.state)]
        started = time.monotonic()
        with subprocess.Popen(args, stdin=subprocess.PIPE) as process:
            try:
                process.stdin.write(json.dumps(payload).encode())
                process.stdin.flush()
                self.assertEqual(process.wait(timeout=40), 65)
            finally:
                if process.poll() is None:
                    process.kill()
        self.assertGreaterEqual(time.monotonic() - started, 29)
        self.assertEqual(self.path.read_bytes(), saved)

    def test_receiver_rejection_and_failed_replace_preserve_last_good(self):
        self.store()
        saved = self.path.read_bytes()
        self.assertEqual(self.receive(b'{"schema": "duplicada", ' + FIXTURE.read_bytes().lstrip()[1:]), 65)
        self.assertEqual(self.receive(b'[' * 2000), 65)
        self.assertEqual(self.receive(b'\xff'), 65)
        self.assertEqual(self.receive(json.dumps(dict(self.data, received_at=NOW)).encode()), 65)
        self.assertEqual(self.path.read_bytes(), saved)
        with patch.object(Path, 'replace', side_effect=OSError('sin espacio')):
            self.assertEqual(self.receive(), 75)
        self.assertEqual(list(self.state.iterdir()), [self.path])
        self.assertEqual(self.path.read_bytes(), saved)

    def test_configuration_is_optional_and_validated(self):
        self.assertEqual(tren.load_repos(self.config), REPOS)
        self.assertEqual(tren.load_repos(self.root / 'missing.json'), ())
        for value in ('{}', '{"tren":{}}', '{"tren":{"repos":[]}}'):
            self.config.write_text(value)
            self.assertEqual(tren.load_repos(self.config), ())
            self.assertEqual(self.receive(), 65)
        self.config.write_text('{"tren":{"repos":["../.."]}}')
        with self.assertRaises(ValueError):
            tren.load_repos(self.config)
        self.assertEqual(self.receive(), 65)

    def test_freshness_uses_receipt_time_and_includes_660_seconds(self):
        self.data['taken'] -= 120
        self.store()
        at_limit = self.cycle(now=NOW + 660)
        self.assertTrue(at_limit['fresh'])
        self.assertEqual(at_limit['age'], 660)
        older = self.cycle(now=NOW + 661)
        self.assertFalse(older['fresh'])
        self.assertEqual(older['age'], 661)
        self.assertIsNone(older['error'])

    def test_accepted_document_stays_readable_after_24_hours_and_restart(self):
        self.store()
        for offset, restart in ((86400, False), (86401, False), (86401, True)):
            with self.subTest(offset=offset, restart=restart):
                if restart:
                    self.rack = serve.Rack(self.hosts, self.state, self.root / 'no-linear', tren_repos=REPOS)
                view = self.cycle(now=NOW + offset)
                self.assertFalse(view['fresh'])
                self.assertEqual(view['age'], offset)
                self.assertIsNone(view['error'])
                self.assertEqual(view['repos'], self.data['repos'])

    def test_server_revalidates_every_cycle_and_marks_last_good_old_on_error(self):
        self.store()
        good = self.cycle()
        broken = json.loads(self.path.read_text())
        broken['repos'][0]['pausa']['detalle'] = '/privado'
        self.path.write_text(json.dumps(broken))
        stale = self.cycle(now=NOW + 1)
        self.assertFalse(stale['fresh'])
        self.assertEqual(stale['age'], 1)
        self.assertEqual(stale['repos'], good['repos'])
        self.assertEqual(stale['error'], 'No se pudo leer el tren')
        self.data['repos'][0]['pausa'] = None
        self.store()
        fixed = self.cycle(now=NOW + 2)
        self.assertTrue(fixed['fresh'])
        self.assertIsNone(fixed['repos'][0]['pausa'])
        self.assertIsNone(fixed['error'])

    def test_joins_desk_by_ref_and_id_and_reuses_history_estimate(self):
        self.store()
        running = task(id=7812, start=NOW - 100, activity=dict(last=None, kind='chain', ref='job-abcDEF12'))
        wrong_ref = dict(running, activity=dict(running['activity'], ref='job-other'))
        wrong_id = dict(running, id=7813)
        for mismatch in (wrong_ref, wrong_id):
            with self.subTest(mismatch=mismatch):
                self.assertNotIn('rack', at(self.cycle(desk=[mismatch]), SUITE))
        self.rack.history.rows = [
            dict(host='desk', id=i, group='suites', name='x', result='Success',
                 start=NOW - 500 - duration, end=NOW - 500)
            for i, duration in enumerate([30, 40, 50, 60, 70])
        ]
        joined = at(self.cycle(desk=[running]), SUITE)
        self.assertEqual(joined['rack'], dict(host='desk', id=7812))
        self.assertEqual(joined['lugar']['rack'], joined['rack'])
        self.assertEqual(joined['est'], self.rack.read()['hosts'][1]['estimates']['7812'])
        self.assertEqual(joined['est']['typical'], 50)

    def test_joins_mini_without_ref_only_if_start_confirms_id(self):
        self.store()
        started = at(self.data, MINI_SUITE)['inicio']
        for start in (None, started - 1):
            with self.subTest(start=start):
                self.assertNotIn('rack', at(self.cycle(mini=[task(id=431, start=start)]), MINI_SUITE))
        self.assertEqual(at(self.cycle(mini=[task(id=431, start=started)]), MINI_SUITE)['rack'],
                         dict(host='mini', id=431))

    def test_mini_reused_id_without_ref_only_joins_unfinished_runs(self):
        suite = at(self.data, MINI_SUITE)
        at(self.data, TRIAL)['lugar'] = dict(suite['lugar'])
        self.store()
        reused = task(id=431, start=NOW - 35, name='otra corrida')
        view = self.cycle(mini=[reused])
        for path in (MINI_SUITE, TRIAL):
            self.assertEqual(at(view, path)['rack'], dict(host='mini', id=431))

        suite.update(estado='terminada', fin=suite['inicio'] + 600, resultado='verde', fallas=0)
        self.store()
        for start in (reused['start'], suite['inicio'] + 3600):
            with self.subTest(start=start):
                finished = at(self.cycle(mini=[dict(reused, start=start)]), MINI_SUITE)
                self.assertEqual(finished['lugar'], suite['lugar'])
                self.assertNotIn('rack', finished)
                self.assertNotIn('est', finished)

    def test_mini_with_ref_demands_match_even_with_compatible_start(self):
        ref = '20261123-120000-fab123'
        at(self.data, MINI_SUITE + ('lugar',))['ref'] = ref
        self.store()
        running = task(id=431, start=NOW - 100)
        self.assertNotIn('rack', at(self.cycle(mini=[running]), MINI_SUITE))
        running['activity'] = dict(last=None, kind='verify', ref=ref)
        self.assertEqual(at(self.cycle(mini=[running]), MINI_SUITE)['rack'], dict(host='mini', id=431))

    def test_trial_joins_desk_and_boat_never_joins(self):
        self.store()
        running = task(id=7812, start=NOW - 100, activity=dict(last=None, kind='chain', ref='job-abcDEF12'))
        self.assertNotIn('rack', at(self.cycle(desk=[running]), TRIAL))
        at(self.data, TRIAL)['lugar'] = at(self.data, SUITE)['lugar']
        self.store()
        self.assertEqual(at(self.cycle(desk=[running]), TRIAL)['rack'], dict(host='desk', id=7812))

    def test_enriches_prs_referenced_only_by_a_pause_or_trial(self):
        at(self.data, PAUSE)['pr'] = 910
        at(self.data, TRIAL)['prs'] = [911, 912]
        self.store()
        prs = self.cycle()['prs']
        self.assertEqual(set(prs), {f'Example/Train#{pr}' for pr in (905, 910, 911, 912, 919)})

    def test_pull_limit_retries_and_persistence(self):
        waiting = self.data['repos'][0]['espera']
        waiting[:] = [dict(waiting[0], pr=pr) for pr in range(920, 931)]
        self.store()

        def gh(argv, **kwargs):
            pr = int(argv[2].rsplit('/', 1)[1])
            if pr == 920:
                raise subprocess.TimeoutExpired(argv, 5)
            ref = 'feat/sEn-7285-otro-sen-123' if pr != 921 else 'sin-ticket'
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(dict(ref=ref, title=f'PR {pr}')))

        def fetch(host):
            return dict(sample(), host=host['id'])

        with patch.object(serve.subprocess, 'run', side_effect=gh) as run:
            self.rack.cycle(fetch, NOW)
            self.assertEqual(run.call_count, 10)
            self.assertEqual(run.call_args_list[0].args[0][2], 'repos/Example/Train/pulls/912')
            self.rack.cycle(fetch, NOW + 1799)
            self.assertEqual(run.call_count, 13)
            self.rack.cycle(fetch, NOW + 1800)
            self.assertEqual(run.call_count, 15)
        self.assertEqual(len(json.loads((self.state / 'pulls.json').read_text())), 13)
        prs = self.rack.read()['tren']['prs']
        self.assertEqual(prs['Example/Train#912'], dict(ticket='SEN-7285', title='PR 912'))
        self.assertEqual(prs['Example/Train#921'], dict(ticket=None, title='PR 921'))
        self.assertEqual(prs['Example/Train#920'], dict(ticket=None, title=None))
        reloaded = serve.Rack(self.hosts, self.state, self.root / 'no-linear', tren_repos=REPOS)
        self.assertEqual(reloaded.pulls, self.rack.pulls)

    def test_pull_lookups_share_the_commit_deadline(self):
        self.store()
        elapsed = 0
        requests = []

        def fetch(host):
            return dict(sample(), host=host['id'], tasks=[pending] if host['id'] == 'desk' else [])

        def gh(argv, *, timeout, **kwargs):
            nonlocal elapsed
            endpoint = argv[2]
            requests.append(endpoint)
            if '/commits/' in endpoint:
                elapsed += 4
                return subprocess.CompletedProcess(argv, 0, stdout=(
                    'release' if endpoint.endswith('/branches-where-head') else 'feat/sen-14'))
            elapsed += timeout
            raise subprocess.TimeoutExpired(argv, timeout)

        pending = task(repo='Example/Train', sha='a' * 40, ticket=None, branch=None, subject=None)
        with patch.object(serve.time, 'monotonic', side_effect=lambda: elapsed), \
             patch.object(serve.subprocess, 'run', side_effect=gh):
            self.rack.cycle(fetch=fetch, now=NOW)
        commit = 'repos/Example/Train/commits/' + 'a' * 40
        self.assertEqual(requests, [commit + '/branches-where-head', commit + '/pulls'])
        self.assertLessEqual(elapsed, serve.LOOKUP_BUDGET)
        self.assertIn('tren', self.rack.read())

    def test_public_text_preserves_digit_runs_and_shortens_hex_with_letters(self):
        texts = ('Migración 20261123 del rack', 'apps/web/e2e/20261123-alta.spec.ts',
                 'Corte 20261123235959')
        for value in texts:
            with self.subTest(value=value):
                self.assertEqual(tren.public_text(value), value)
                self.assertEqual(tren.public_title(value), value)
        self.assertEqual(tren.public_title('Validar 12345678ABCDEF'), 'Validar 1234567')
        digits = '1234567890' * 4
        self.assertEqual(tren.public_title(f'Validar {digits}'), 'Validar 1234567')
        self.assertEqual(tren.public_text(f'apps/test/{digits}.ts'), 'apps/test/1234567.ts')

    def test_publication_keeps_paths_private_keys_and_full_shas_out(self):
        full_sha = 'c0ffee1234' * 4
        at(self.data, TRIAL)['unidad'] = f'apps/test/{full_sha}.ts'
        at(self.data, RETURN)['unidad'] = f'apps/test/{full_sha}.ts'
        self.store()
        # Simula también una entrada de caché escrita antes de sanear los títulos.
        self.rack.pulls = {
            'Example/Train#912': dict(ticket='SEN-12', title=f'Validar {full_sha}', checked=NOW),
            'Example/Train#919': dict(ticket='SEN-13', title='Ruta /privado/prueba o C:\\privado\\prueba', checked=NOW),
        }
        running = task(id=7812, start=NOW - 100, activity=dict(last=None, kind='chain', ref='job-abcDEF12'),
                       branch='/privado/task', sha=full_sha, detalle='oculto', archivos=['/privado/task'])
        section = self.cycle(desk=[running])
        encoded = json.dumps(section)
        for forbidden in ('/privado', 'privado\\\\prueba', '"detalle"', '"archivos"', full_sha):
            self.assertNotIn(forbidden, encoded)
        self.assertIn(full_sha[:7], encoded)
        self.assertEqual(section['prs']['Example/Train#912']['title'], f'Validar {full_sha[:7]}')
        self.assertIsNone(section['prs']['Example/Train#919']['title'])
        hashes = re.findall(r'\b(?=[0-9a-f]*[a-f])[0-9a-f]{8,}\b', encoded)
        self.assertEqual(set(hashes), {'7c1ea9d0e2b4', '3f0b12aa90c1'})  # ids del lote, no heads

    def test_absent_file_keeps_the_legacy_payload_byte_for_byte(self):
        hosts = self.hosts[:1]
        rack = serve.Rack(hosts, self.state, self.root / 'no-linear', tren_repos=REPOS)
        legacy = (b'{"schema": "rack@1", "now": 1795480000.0, "hosts": [{"id": "mini", '
                  b'"name": "Mini", "actions": {"reason": "Este host no tiene canal de escritura."}, '
                  b'"fresh": true, "age": 0, "error": null, '
                  b'"snapshot": {"schema": "rack-host@1", "host": "mini", "os": "macos", '
                  b'"taken": 1795480000.0, "cpu": null, "mem": null, "disk": null, "guard": null, '
                  b'"groups": [{"name": "suites", "status": "Running", "slots": 3}], "tasks": [], '
                  b'"errors": []}, "estimates": {}}], "tickets": {}}')

        def fetch(host):
            return dict(sample(), taken=NOW)

        rack.cycle(fetch, NOW)
        self.assertEqual(json.dumps(rack.read(), allow_nan=False).encode(), legacy)
        self.assertFalse((self.state / 'pulls.json').exists())
        self.store()
        disabled = serve.Rack(hosts, self.state, self.root / 'no-linear')
        disabled.cycle(fetch, NOW)
        self.assertEqual(json.dumps(disabled.read(), allow_nan=False).encode(), legacy)
