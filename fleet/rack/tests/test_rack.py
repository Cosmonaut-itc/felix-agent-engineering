import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
import urllib.error
import urllib.request

BASE = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, BASE / (name + '.py'))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


snapshot = module('snapshot')
serve = module('serve')


def sample():
    return dict(schema='rack-host@1', host='mini', os='macos', taken=1000, cpu=None, mem=None, disk=None,
                guard=None, groups=[dict(name='suites', status='Running', slots=3)], tasks=[], errors=[])


def task(**fields):
    row = dict(id=1, group='suites', state='Running', name='x', scope=None, project=None, ticket=None,
               repo=None, sha=None, branch=None, subject=None, priority=0, result=None, enqueued=None, start=None, end=None)
    row.update(fields)
    return row


def executable(path, text):
    path.write_text('#!/usr/bin/python3\n' + text)
    path.chmod(0o755)
    return str(path)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def fake_pueue(self, tasks):
        data = dict(groups={'suites': dict(status='Running', parallel_tasks=3)}, tasks=tasks)
        return executable(self.root / 'pueue', 'print(' + repr(json.dumps(data)) + ')\n')

    def test_mini_metadata_and_no_private_keys(self):
        fixture = json.loads((BASE / 'tests/mini-pueue.json').read_text())
        run = self.root / 'run'
        run.mkdir()
        (run / 'meta').write_text('src=/Users/felix/.t3/worktrees/SEN-CheckIn/t3-sen-4822\nmirror=remote-run/work/suite-sen-4822-sol-aabbccdd\n')
        for task in fixture['tasks'].values():
            task['path'] = str(run)
        pueue = self.fake_pueue(fixture['tasks'])
        data = snapshot.collect('macos', 'mini', pueue=pueue, now=1794507599)
        row = next(t for t in data['tasks'] if t['state'] == 'Running')
        self.assertEqual((row['name'], row['scope'], row['ticket'], row['project']), ('suite-sen-4822-sol', 'suite', 'SEN-4822', 'SEN-CheckIn'))
        def keys(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    self.assertNotIn(k, ('envs', 'command', 'original_command', 'path'))
                    keys(v)
            elif isinstance(obj, list):
                for v in obj:
                    keys(v)
        keys(data)
        (run / 'meta').write_text('src=/Users/felix/REPOS/comprador./x\nmirror=remote-run/work/diag-x-1234abcd\n')
        self.assertEqual(snapshot.mini_metadata({'path': str(run)})['project'], 'comprador')
        (run / 'meta').write_text('src=/Users/felix/VSCODE/REPOS/apotheca.-desk-pueue\nmirror=remote-run/work/suite-u15-1234abcd\n')
        self.assertEqual(snapshot.mini_metadata({'path': str(run)})['project'], 'apotheca')

    def test_desk_reads_only_allowed_variables_and_reports_commit(self):
        repo = self.root / 'SEN-CheckIn'
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-qm', 'fixture'], check=True)
        sha = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
        subprocess.run(['git', '-C', str(repo), 'remote', 'add', 'origin', 'https://github.com/Owner/SEN-CheckIn.git'], check=True)
        job = self.root / 'job-0001'
        job.mkdir()
        (job / 'body.sh').write_text('declare -x GH_T="gho_falso"\ndeclare -- MODE="contract"\ndeclare -- BASE_REPO="' + str(repo) + '"\ndeclare -- BRANCH=\"codex/SEN-123-rama\"\ndeclare -- REQUESTED_SHA="' + sha + '"\n')
        task = dict(id=1, group='suites', status={'Running': {'start': 100}}, command='exec bash ' + str(job / 'task.sh'))
        data = snapshot.collect('linux', 'desk', pueue=self.fake_pueue({'1': task}), home=self.root)
        self.assertNotIn('gho_falso', json.dumps(data))
        row = data['tasks'][0]
        self.assertEqual(row['branch'], 'codex/SEN-123-rama')
        body = job / 'body.sh'
        body.write_text(body.read_text().replace('codex/SEN-123-rama', sha.upper()))
        self.assertIsNone(snapshot.desk_metadata(task, self.root, {})['branch'])
        self.assertEqual((row['name'], row['project'], row['repo'], row['sha'], row['ticket']), ('contract', 'SEN-CheckIn', 'Owner/SEN-CheckIn', sha, None))

    def test_remote_run_metadata_shared_by_hosts_and_never_emits_cmd(self):
        mirror = self.root / 'mirror'
        subprocess.run(['git', 'init', '-q', '-b', 'codex/SEN-234-prueba', str(mirror)], check=True)
        subprocess.run(['git', '-C', str(mirror), '-c', 'user.name=Test', '-c', 'user.email=t@example.invalid',
                        'commit', '--allow-empty', '-qm', 'Descripción <arbitraria>'], check=True)
        run = self.root / 'run'
        run.mkdir()
        (run / 'meta').write_text('src=/Users/test/REPOS/proyecto/work\nmirror=mirror\ncmd=GH_T=secreto-no-exportar\nmirror=secreto-en-cmd\n')
        # Un grupo cualquiera: la lectura depende del tipo de corrida, no del grupo.
        raw = dict(id=8, group='otros', priority=9, status='Queued', path=str(run))
        for mode, host in (('macos', 'mini'), ('linux', 'desk')):
            data = snapshot.collect(mode, host, pueue=self.fake_pueue({'8': raw}), home=self.root)
            row = data['tasks'][0]
            self.assertEqual((row['branch'], row['subject'], row['project'], row['priority']),
                             ('codex/SEN-234-prueba', 'Descripción <arbitraria>', 'proyecto', 9))
            self.assertNotIn('cmd', json.dumps(data))
            self.assertNotIn('secreto', json.dumps(data))
        (run / 'meta').write_text('src=/Users/test/REPOS/proyecto\n')
        # Sin nombre propio del espejo, la rama proporciona el nombre.
        with patch.object(snapshot, 'command', return_value='codex/rama\n'):
            (run / 'meta').write_text('mirror=.\n')
            self.assertEqual(snapshot.mini_metadata(raw, self.root)['name'], 'rama')
        (run / 'meta').write_text('mirror=inexistente\n')
        row = snapshot.mini_metadata(raw, self.root)
        self.assertIsNone(row['branch'])
        self.assertIsNone(row['subject'])

    def test_capacity_failure_is_null_with_error(self):
        with patch.object(snapshot, 'mac_capacity', side_effect=OSError):
            data = snapshot.collect('macos', 'mini', pueue=self.fake_pueue({}))
        self.assertIsNone(data['cpu'])
        self.assertIsNone(data['mem'])
        self.assertTrue(any(e['stage'] == 'capacity' for e in data['errors']))
        process = subprocess.run(['/usr/bin/python3', '-I', str(BASE / 'snapshot.py'), '--os', 'linux'], stdout=subprocess.PIPE, text=True)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(json.loads(process.stdout)['schema'], 'rack-host@1')

    def test_linux_capacity_and_guard(self):
        (self.root / 'loadavg').write_text('2.5 2.0 1.0 1/10 1')
        (self.root / 'meminfo').write_text('MemTotal: 1000 kB\nMemAvailable: 400 kB\nSwapTotal: 100 kB\nSwapFree: 40 kB\n')
        cpu, mem = snapshot.linux_capacity(self.root)
        self.assertEqual(cpu['load'], [2.5, 2, 1])
        self.assertEqual(mem['swap_used'], 60 * 1024)
        control = self.root / '.local/lib/pao-pueue-desk/control.py'
        control.parent.mkdir(parents=True)
        control.write_text('PRESSURE_THRESHOLDS = {"pause": {"cpu": 35, "memory": 4 * 2**30}, "resume": {"cpu": 12}}\n')
        log = self.root / '.local/state/pao/pueue-desk/guardia.log'
        log.parent.mkdir(parents=True)
        log.write_text('{"evento":"decision","accion":"pausar","utc":"2026-11-12T00:00:00Z"}\nbad\n')
        self.assertEqual((snapshot.desk_guard(self.root)['cpu_pause'], snapshot.desk_guard(self.root)['cpu_resume']), (35, 12))

    def test_old_done_excluded(self):
        task = dict(id=1, group='suites', status={'Done': dict(start=1, end=2, result='Success')})
        data = snapshot.collect('linux', 'desk', pueue=self.fake_pueue({'1': task}), home=self.root, now=100000)
        self.assertEqual(data['tasks'], [])


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.hosts = [dict(id='mini', name='Mini', transport='local', argv=['false'])]
        warnings = patch.object(serve.logging, 'warning')
        warnings.start()
        self.addCleanup(warnings.stop)
        self.rack = serve.Rack(self.hosts, self.root, self.root / 'absent-key')

    def test_estimates_success_median_normalization_and_minimum(self):
        task = dict(id=99, group='suites', name='suite-x-sol-r2', state='Running')
        rows = [dict(id=i, group='suites', name='suite-x-opus', state='Done', start=100, end=100+d, result='Success') for i, d in enumerate([30, 10, 20, 40, 50])]
        self.rack.history.add('mini', rows[:4], 150)
        self.assertIsNone(self.rack.history.estimate('mini', task))
        self.rack.history.add('mini', rows + [dict(rows[0], id=20, end=2000, result='exit 1')], 150)
        self.assertEqual(self.rack.history.estimate('mini', task), dict(typical=30, low=14, high=46, n=5))
        self.assertEqual(len(self.rack.history.rows), 6)
        reloaded = serve.History(self.root)
        self.assertEqual(len(reloaded.rows), 6)
        reloaded.add('mini', [], 40 * 86400)
        self.assertEqual(reloaded.rows, [])

    def test_last_twenty_durations(self):
        rows = [dict(id=i, group='g', name='x', state='Done', start=i, end=i+10+i, result='Success') for i in range(25)]
        self.rack.history.add('mini', rows, 100)
        self.assertEqual(self.rack.history.estimate('mini', dict(group='g', name='x')), dict(typical=24.5, low=16.9, high=32.1, n=20))

    def test_branch_ticket_case_insensitive_and_existing_ticket_kept(self):
        rows = [task(branch='codex/sEn-456-prueba'), task(ticket='SEN-9', branch='sen-1')]
        self.rack.resolve_commits(rows, 1000)
        self.assertEqual([r['ticket'] for r in rows], ['SEN-456', 'SEN-9'])

    def test_new_snapshot_fields_are_validated_and_private_fields_removed(self):
        row = task(branch='<rama>', subject='"commit"', priority=-2)
        data = dict(sample(), tasks=[dict(row, envs={'GH_T': 'secret'}, command='secret', original_command='secret', path='secret')])
        clean = serve.validate_snapshot(data, 'mini')['tasks'][0]
        self.assertEqual(clean, row)
        for field, value in (('branch', []), ('subject', 5), ('priority', True), ('priority', 1.5), ('priority', None)):
            with self.assertRaises(ValueError):
                serve.validate_snapshot(dict(sample(), tasks=[dict(row, **{field: value})]), 'mini')
        for field in ('branch', 'subject', 'priority'):
            missing = dict(row)
            del missing[field]
            with self.assertRaises(ValueError):
                serve.validate_snapshot(dict(sample(), tasks=[missing]), 'mini')

    def test_new_commit_cache_fields_are_validated(self):
        path = self.root / 'commits.json'
        path.write_text(json.dumps({'ok': dict(branch='b', subject='s', ticket=None, checked=100),
                                    'bad_branch': dict(branch=[]), 'bad_subject': dict(subject=True)}))
        self.assertEqual(set(serve.Rack(self.hosts, self.root, self.root / 'absent-key').commits), {'ok'})

    def test_commit_negative_retry_and_deadline(self):
        row = task(repo='Owner/Repo', sha='a' * 40)
        self.rack.gh = '/bin/false'
        self.rack.resolve_commits([row], 1000, deadline=0)
        self.assertEqual(self.rack.commits, {})
        with patch.object(serve.subprocess, 'run') as run:
            run.return_value.stdout = ''
            self.rack.resolve_commits([row], 1000)
            self.assertEqual(run.call_count, 3)
            self.rack.resolve_commits([row], 2799)
            self.assertEqual(run.call_count, 3)
            self.rack.resolve_commits([row], 2800)
            self.assertEqual(run.call_count, 6)
            key = 'Owner/Repo@' + row['sha']
            self.rack.commits[key] = dict(ticket='SEN-1', branch='sen-1', subject=None, checked=2800)
            self.rack.resolve_commits([row], 4599)
            self.assertEqual(run.call_count, 6)
            run.return_value.stdout = 'sen-1'
            self.rack.resolve_commits([row], 4600)
            self.assertEqual(run.call_count, 8)
            self.assertEqual(self.rack.commits[key]['subject'], 'sen-1')

    def test_commit_retry_failure_keeps_known_fields_and_pull_branch_used(self):
        key = 'Owner/Repo@' + 'a' * 40
        self.rack.commits[key] = dict(ticket='SEN-42', branch='codex/sen-42', subject=None, checked=1000)
        row = task(repo='Owner/Repo', sha='a' * 40)
        with patch.object(serve.subprocess, 'run', side_effect=subprocess.TimeoutExpired('gh', 5)):
            self.rack.resolve_commits([row], 2800)
        self.assertEqual((row['ticket'], row['branch']), ('SEN-42', 'codex/sen-42'))
        self.assertEqual(self.rack.commits[key]['branch'], 'codex/sen-42')
        other = task(repo='Owner/Repo', sha='b' * 40)
        def run(argv, **kwargs):
            path = argv[2]
            out = '' if path.endswith('/branches-where-head') else 'codex/sen-7-prueba\n' if path.endswith('/pulls') else 'Asunto\n'
            return subprocess.CompletedProcess(argv, 0, stdout=out)
        with patch.object(serve.subprocess, 'run', side_effect=run):
            self.rack.resolve_commits([other], 1000)
        self.assertEqual((other['ticket'], other['branch'], other['subject']), ('SEN-7', 'codex/sen-7-prueba', 'Asunto'))

    def test_failed_host_keeps_previous_snapshot(self):
        self.rack.cycle(lambda host: sample(), now=1001)
        self.rack.cycle(lambda host: (_ for _ in ()).throw(subprocess.TimeoutExpired('private', 25)), now=1030)
        host = self.rack.read()['hosts'][0]
        self.assertFalse(host['fresh'])
        self.assertEqual(host['snapshot'], sample())
        self.assertEqual(host['age'], 30)
        self.assertNotIn('private', host['error'])

    def test_done_hidden_and_recorded(self):
        data = sample()
        data['tasks'] = [task(state='Done', start=900, end=950, result='Success')]
        self.rack.cycle(lambda host: data, now=1001)
        self.assertEqual(self.rack.read()['hosts'][0]['snapshot']['tasks'], [])
        self.assertEqual(len(self.rack.history.rows), 1)

    def test_invalid_hosts_exit_two(self):
        path = self.root / 'hosts.json'
        for invalid in ({}, [], [{'id': 'mini'}], self.hosts * 2):
            path.write_text(json.dumps(invalid))
            result = subprocess.run(['/usr/bin/python3', '-I', str(BASE / 'serve.py'), '--bind', '127.0.0.1', '--port', '0', '--hosts', str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('hosts.json inválido', result.stderr)

    def test_bind_rejects_public_and_requires_owner(self):
        for bind in ('0.0.0.0', '::', '192.168.1.5', '100.111.159.14'):
            result = subprocess.run(['/usr/bin/python3', '-I', str(BASE / 'serve.py'), '--bind', bind, '--port', '0'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
        self.assertEqual(serve.load_hosts(BASE / 'hosts.json')[0]['id'], 'mini')

    def test_http_routes_methods_and_funnel(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), serve.Handler)
        server.rack = self.rack
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join()
        self.addCleanup(cleanup)
        url = 'http://127.0.0.1:' + str(server.server_port)
        for path in ('/', '/api/rack.json'):
            with urllib.request.urlopen(url + path) as response:
                self.assertEqual(response.status, 200)
        for request, code in ((urllib.request.Request(url + '/../hosts.json'), 404),
                              (urllib.request.Request(url, data=b'', method='POST'), 405),
                              (urllib.request.Request(url, method='OPTIONS'), 405),
                              (urllib.request.Request(url, headers={'Tailscale-Funnel-Request': '?1'}), 403)):
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, code)

    def collector(self):
        # Como main(): cada despertar corre un ciclo; los hosts fallan y `now` numera el ciclo.
        stop = threading.Event()
        def run():
            while not stop.is_set():
                if self.rack.wake.wait(0.05):
                    self.rack.wake.clear()
                    self.rack.cycle(fetch=lambda host: (_ for _ in ()).throw(OSError()), now=1000 + self.rack.started)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(lambda: (stop.set(), thread.join()))

    def test_refresh_waits_for_a_read_started_after_the_request(self):
        def publish(number):
            with self.rack.published:
                self.rack.read_hosts = number
                self.rack.payload['now'] = number
                self.rack.published.notify_all()
        with self.rack.lock:
            self.rack.started = 1  # una lectura empezada antes de la petición
        result = []
        waiter = threading.Thread(target=lambda: result.append(self.rack.refresh(5)))
        waiter.start()
        for _ in range(100):
            if self.rack.wake.is_set(): break
            time.sleep(0.01)
        self.assertTrue(self.rack.wake.is_set())
        publish(1)
        waiter.join(0.2)
        self.assertTrue(waiter.is_alive())
        publish(2)
        waiter.join(5)
        self.assertEqual(result, [dict(self.rack.payload, now=2)])

    def test_refresh_endpoint_needs_its_header_and_reports_timeout(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), serve.Handler)
        server.rack = self.rack
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join()
        self.addCleanup(cleanup)
        url = 'http://127.0.0.1:' + str(server.server_port) + '/api/refresh'
        for headers, code in (({}, 403), ({'X-Rack-Refresh': '0'}, 403)):
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(urllib.request.Request(url, data=b'', method='POST', headers=headers))
            self.assertEqual(error.exception.code, code)
        self.assertFalse(self.rack.wake.is_set())
        with patch.object(serve, 'REFRESH_TIMEOUT', 0.1), self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(urllib.request.Request(url, data=b'', method='POST', headers={'X-Rack-Refresh': '1'}))
        self.assertEqual(error.exception.code, 504)
        self.collector()
        request = urllib.request.Request(url, data=b'', method='POST', headers={'X-Rack-Refresh': '1'})
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())['schema'], 'rack@1')
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(urllib.request.Request(url))
        self.assertEqual(error.exception.code, 404)

    def request_as(self, ip, owner_result=None, funnel=False):
        handler = object.__new__(serve.Handler)
        handler.client_address = (ip, 1234)
        handler.server = type('Server', (), dict(server_address=('100.100.0.1', 8787), rack=self.rack,
                                               owner_check=owner_result))()
        handler.headers = {'Tailscale-Funnel-Request': '?1'} if funnel else {}
        handler.command = 'GET'
        handler.path = '/api/rack.json'
        result = []
        handler.reply = lambda code, body, *args, **kw: result.append((code, body))
        handler.dispatch()
        return result[0]

    def fake_tailscale(self, login='owner@example.com', fail=False):
        path = self.root / 'Tailscale'
        body = 'exit 1' if fail else "printf '%s\\n' '" + json.dumps(dict(UserProfile=dict(LoginName=login))) + "'"
        path.write_text('#!/bin/sh\n[ "$1 $2" = "whois --json" ] || exit 2\n' + body + '\n')
        path.chmod(0o755)
        return serve.OwnerCheck('owner@example.com', str(path))

    def test_lan_client_forbidden(self):
        self.assertEqual(self.request_as('192.168.1.5')[0], 403)

    def test_tailnet_client_allowed(self):
        self.assertEqual(self.request_as('100.111.159.14', self.fake_tailscale())[0], 200)
        self.assertTrue(serve.allowed_client('fd7a:115c:a1e0::1', '100.100.0.1'))

    def test_funnel_forbidden(self):
        self.assertEqual(self.request_as('100.111.159.14', funnel=True), (403, b''))

    def test_owner_matches(self):
        check = self.fake_tailscale()
        self.assertEqual(self.request_as('100.111.159.14', check)[0], 200)
        with patch.object(serve.subprocess, 'run', side_effect=AssertionError('cache should avoid command')):
            self.assertTrue(check.allows('100.111.159.14'))

    def test_owner_cache_expires_and_timeout_denies(self):
        check = self.fake_tailscale()
        with patch.object(serve.time, 'monotonic', return_value=100):
            self.assertTrue(check.allows('100.111.159.14'))
        pidfile = self.root / 'whois.pid'
        hung = self.root / 'Tailscale'
        hung.write_text('#!/bin/sh\necho $$ > ' + str(pidfile) + '\nexec sleep 30\n')
        started = time.monotonic()
        with patch.object(serve.time, 'monotonic', return_value=401):
            self.assertFalse(check.allows('100.111.159.14'))
        self.assertLess(time.monotonic() - started, 6)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)

    def test_other_owner_forbidden(self):
        self.assertEqual(self.request_as('100.111.159.14', self.fake_tailscale('guest@example.com')), (403, b''))

    def test_whois_failure_forbidden(self):
        self.assertEqual(self.request_as('100.111.159.14', self.fake_tailscale(fail=True)), (403, b''))

    def test_malformed_whois_forbidden(self):
        check = serve.OwnerCheck('owner', executable(self.root / 'Tailscale', 'print("{}")\n'))
        self.assertEqual(self.request_as('100.111.159.14', check), (403, b''))

    def test_commit_ticket_from_github_cached(self):
        sha = 'a' * 40
        calls = self.root / 'calls'
        gh = executable(self.root / 'gh', 'import sys\nopen(' + repr(str(calls)) + ', "a").write(" ".join(sys.argv[1:]) + "\\n")\n'
                        'print("codex/sen-5702-pantalla" if sys.argv[2].endswith("/branches-where-head") else "Primera línea\\nSegunda línea")\n')
        rack = serve.Rack(self.hosts, self.root, self.root / 'absent-key', gh=gh)
        task = dict(id=1, group='jale', state='Running', name='gates', repo='Owner/SEN-CheckIn', sha=sha, ticket=None)
        rack.resolve_commits([task], 1000)
        self.assertEqual(task['ticket'], 'SEN-5702')
        self.assertIn('repos/Owner/SEN-CheckIn/commits/' + sha + '/branches-where-head', calls.read_text())
        again = dict(task, ticket=None)
        serve.Rack(self.hosts, self.root, self.root / 'absent-key', gh='/bin/false').resolve_commits([again], 5000)
        self.assertEqual(again['ticket'], 'SEN-5702')
        for bad in (dict(task, ticket=None, branch=None, repo='x;rm'), dict(task, ticket=None, branch=None, sha='HEAD')):
            rack.resolve_commits([bad], 1000)
            self.assertIsNone(bad['ticket'])
        self.assertEqual(len(calls.read_text().splitlines()), 2)
        cached = json.loads((self.root / 'commits.json').read_text())['Owner/SEN-CheckIn@' + sha]
        self.assertEqual(cached['branch'], 'codex/sen-5702-pantalla')
        self.assertEqual(cached['subject'], 'Primera línea')
        self.assertEqual(again['branch'], cached['branch'])
        self.assertEqual(again['subject'], cached['subject'])

    def test_malformed_caches_are_ignored(self):
        sha = 'b' * 40
        (self.root / 'history.jsonl').write_text('{"host":"mini","id":1,"group":"g","name":"x","result":"Success","start":"a","end":null}\n')
        for tickets, commits in (('[]', '[]'),
                                 ('{"SEN-1": {"title": NaN}}', '{"Owner/R@' + sha + '": {"ticket": null, "checked": []}}'),
                                 ('{"SEN-1": {"updated": []}}', '{"Owner/R@' + sha + '": "x"}'),
                                 ('{"SEN-1": {"failed": 1' + '0' * 320 + '}}', '{"Owner/R@' + sha + '": {"checked": 1' + '0' * 320 + '}}')):
            (self.root / 'tickets.json').write_text(tickets)
            (self.root / 'commits.json').write_text(commits)
            rack = serve.Rack(self.hosts, self.root, self.root / 'absent-key', gh='/bin/false')
            data = sample()
            data['tasks'] = [task(ticket='SEN-1'), task(id=2, repo='Owner/R', sha=sha)]
            rack.cycle(lambda host: data, now=1001)
            payload = json.loads(json.dumps(rack.read(), allow_nan=False))
            self.assertEqual(payload['now'], 1001)
            self.assertTrue(payload['hosts'][0]['fresh'])

    def test_bad_host_snapshot_isolated(self):
        hosts = [dict(id='mini', name='Mini', transport='local', argv=['x']), dict(id='desk', name='Desk', transport='local', argv=['x'])]
        rack = serve.Rack(hosts, self.root, self.root / 'absent-key')
        def good(host):
            return dict(sample(), host=host['id'])
        rack.cycle(good, now=1000)
        rack.history.add('desk', [task(id=i, state='Done', start=0, end=10, result='Success') for i in range(3)], 1000)
        missing = task()
        del missing['name']
        bad_cases = [dict(tasks=[task(ticket=['SEN-1'])]),
                     dict(tasks=[task(start=float('nan'))]),
                     dict(tasks=[missing]),
                     dict(cpu=dict(load=[float('nan')])),
                     dict(errors=[dict(stage='pueue', message='Lectura fallida: OSError')])]
        for index, bad in enumerate(bad_cases):
            now = 1001 + index
            rack.cycle(lambda host: dict(good(host), **bad) if host['id'] == 'desk' else good(host), now=now)
            payload = json.loads(json.dumps(rack.read(), allow_nan=False))
            mini, desk = payload['hosts']
            self.assertEqual(payload['now'], now)
            self.assertTrue(mini['fresh'])
            self.assertFalse(desk['fresh'])
            self.assertEqual(desk['snapshot']['taken'], 1000)

    def test_hosts_published_before_slow_lookups(self):
        seen = []
        def slow(*args, **kwargs):
            seen.append(self.rack.read()['now'])
        with patch.object(self.rack, 'resolve_commits', side_effect=slow):
            self.rack.cycle(lambda host: sample(), now=1001)
        self.assertEqual(seen, [1001])

    def test_linear_key_permissions_and_failure(self):
        key = self.root / 'key'
        key.write_text('fake-key')
        key.chmod(0o644)
        self.assertIsNone(serve.Rack(self.hosts, self.root, key).key)
        key.chmod(0o600)
        rack = serve.Rack(self.hosts, self.root, key)
        self.assertEqual(rack.key, 'fake-key')
        with patch.object(serve.urllib.request, 'urlopen', side_effect=OSError) as urlopen:
            rack.refresh_tickets({'SEN-123'}, 100000)
            rack.refresh_tickets({'SEN-123'}, 100000 + 1799)
            self.assertEqual(urlopen.call_count, 1)
            rack.refresh_tickets({'SEN-123'}, 100000 + 1800)
            self.assertEqual(urlopen.call_count, 2)
        self.assertIsNone(rack.tickets['SEN-123'].get('title'))
        with patch.object(serve.urllib.request, 'urlopen', side_effect=OSError) as urlopen:
            rack.refresh_tickets({'SEN-124'}, 100000, deadline=0)
            self.assertEqual(urlopen.call_count, 0)


if __name__ == '__main__':
    unittest.main()
