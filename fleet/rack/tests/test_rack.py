import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import threading
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
        (job / 'body.sh').write_text('declare -x GH_T="gho_falso"\ndeclare -- MODE="contract"\ndeclare -- BASE_REPO="' + str(repo) + '"\ndeclare -- REQUESTED_SHA="' + sha + '"\n')
        task = dict(id=1, group='suites', status={'Running': {'start': 100}}, command='exec bash ' + str(job / 'task.sh'))
        data = snapshot.collect('linux', 'desk', pueue=self.fake_pueue({'1': task}), home=self.root)
        self.assertNotIn('gho_falso', json.dumps(data))
        row = data['tasks'][0]
        self.assertEqual((row['name'], row['project'], row['repo'], row['sha'], row['ticket']), ('contract', 'SEN-CheckIn', 'Owner/SEN-CheckIn', sha, None))

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
        rows = [dict(id=i, group='suites', name='suite-x-opus', state='Done', start=100, end=100+d, result='Success') for i, d in enumerate([30, 10, 20])]
        self.rack.history.add('mini', rows[:2], 150)
        self.assertIsNone(self.rack.history.estimate('mini', task))
        self.rack.history.add('mini', rows + [dict(rows[0], id=20, end=2000, result='exit 1')], 150)
        self.assertEqual(self.rack.history.estimate('mini', task), dict(typical=20, n=3))
        self.assertEqual(len(self.rack.history.rows), 4)
        reloaded = serve.History(self.root)
        self.assertEqual(len(reloaded.rows), 4)
        reloaded.add('mini', [], 40 * 86400)
        self.assertEqual(reloaded.rows, [])

    def test_last_twenty_durations(self):
        rows = [dict(id=i, group='g', name='x', state='Done', start=i, end=i+10+i, result='Success') for i in range(25)]
        self.rack.history.add('mini', rows, 100)
        self.assertEqual(self.rack.history.estimate('mini', dict(group='g', name='x')), dict(typical=24.5, n=20))

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
        data['tasks'] = [dict(id=1, name='x', group='suites', state='Done', start=900, end=950, result='Success')]
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
        text = 'import sys\nassert sys.argv[1:3] == ["whois", "--json"]\n'
        text += 'sys.exit(1)\n' if fail else 'print(' + repr(json.dumps(dict(UserProfile=dict(LoginName=login)))) + ')\n'
        return serve.OwnerCheck('owner@example.com', executable(path, text))

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
        with patch.object(serve.time, 'monotonic', return_value=401), patch.object(serve.subprocess, 'run', side_effect=subprocess.TimeoutExpired('whois', 3)) as run:
            self.assertFalse(check.allows('100.111.159.14'))
            self.assertEqual(run.call_args.kwargs['timeout'], 3)

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
                        'print("codex/sen-5702-pantalla" if sys.argv[2].endswith("/branches-where-head") else "")\n')
        rack = serve.Rack(self.hosts, self.root, self.root / 'absent-key', gh=gh)
        task = dict(id=1, group='jale', state='Running', name='gates', repo='Owner/SEN-CheckIn', sha=sha, ticket=None)
        rack.resolve_commits([task], 1000)
        self.assertEqual(task['ticket'], 'SEN-5702')
        self.assertIn('repos/Owner/SEN-CheckIn/commits/' + sha + '/branches-where-head', calls.read_text())
        again = dict(task, ticket=None)
        serve.Rack(self.hosts, self.root, self.root / 'absent-key', gh='/bin/false').resolve_commits([again], 5000)
        self.assertEqual(again['ticket'], 'SEN-5702')
        for bad in (dict(task, ticket=None, repo='x;rm'), dict(task, ticket=None, sha='HEAD')):
            rack.resolve_commits([bad], 1000)
            self.assertIsNone(bad['ticket'])
        self.assertEqual(len(calls.read_text().splitlines()), 1)

    def test_linear_key_permissions_and_failure(self):
        key = self.root / 'key'
        key.write_text('fake-key')
        key.chmod(0o644)
        self.assertIsNone(serve.Rack(self.hosts, self.root, key).key)
        key.chmod(0o600)
        rack = serve.Rack(self.hosts, self.root, key)
        self.assertEqual(rack.key, 'fake-key')
        with patch.object(serve.urllib.request, 'urlopen', side_effect=OSError):
            rack.refresh_tickets({'SEN-123'}, 100000)
        self.assertEqual(rack.tickets, {})


if __name__ == '__main__':
    unittest.main()
