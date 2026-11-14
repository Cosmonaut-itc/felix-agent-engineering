import safety  # fail closed before any subprocess
import importlib.util
import datetime
import json
import os
import shutil
from http.client import HTTPMessage
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
        usage = patch.object(snapshot, 'collect_usage', return_value=None)
        usage.start()
        self.addCleanup(usage.stop)
        # En la Mini el HOME real tiene el control instalado: estas pruebas nunca lo consultan.
        quotas = patch.object(snapshot, 'host_control', return_value=None)
        quotas.start()
        self.addCleanup(quotas.stop)

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
        (run / 'meta').write_text('src=/Users/felix/VSCODE/REPOS/.apotheca-exhib/sen-5957-sol\nmirror=remote-run/work/suite-sen-5957-1234abcd\n')
        self.assertIsNone(snapshot.mini_metadata({'path': str(run)})['project'])
        self.assertEqual(snapshot.mini_metadata({'path': str(run), 'label': 'apotheca-remote-run'})['project'], 'apotheca')

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
        # Never let the CLI's default paths reach an installed Pueue or real home.
        with patch.object(snapshot, 'command', side_effect=OSError('fake unavailable')):
            data = snapshot.collect('linux', 'desk', home=self.root, proc=self.root)
        self.assertEqual(data['schema'], 'rack-host@1')

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

    def test_desk_guard_reads_the_deployed_controller(self):
        control = self.root / '.local/lib/pao-pueue-desk/control.py'
        control.parent.mkdir(parents=True)
        shutil.copy(BASE.parent / 'pueue-desk/control.py', control)
        log = self.root / '.local/state/pao/pueue-desk/guardia.log'
        log.parent.mkdir(parents=True)
        log.write_text('{"evento":"decision","accion":"pausar","accion_rapidas":"mantener","utc":"2026-11-12T00:00:00Z"}\n')
        guard = snapshot.desk_guard(self.root)
        self.assertEqual((guard['action'], guard['cpu_pause'], guard['cpu_resume']), ('pausar', 30, 15))

    def test_old_done_excluded(self):
        task = dict(id=1, group='suites', status={'Done': dict(start=1, end=2, result='Success')})
        data = snapshot.collect('linux', 'desk', pueue=self.fake_pueue({'1': task}), home=self.root, now=100000)
        self.assertEqual(data['tasks'], [])


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        docker = patch.object(snapshot, 'docker_binary', return_value=None)
        docker.start()
        self.addCleanup(docker.stop)

    def proc_row(self, pid, ppid, ticks, name='bash', start=1, pages=2, argv='', cwd=None):
        path = self.root / str(pid)
        path.mkdir(exist_ok=True)
        fields = ['S', str(ppid)] + ['0'] * 9 + [str(ticks), '0'] + ['0'] * 6 + [str(start), '0', str(pages)]
        (path / 'stat').write_text(str(pid) + ' (' + name + ') ' + ' '.join(fields))
        (path / 'cmdline').write_bytes(argv.encode().replace(b' ', b'\0'))
        if cwd:
            (path / 'cwd').symlink_to(cwd)

    def output(self):
        return dict(sample(), cpu=dict(cores=12), mem=dict(total=100000000, available=60000000),
                    tasks=[task(id=1), task(id=2), task(id=3, state='Paused')])

    def test_linux_real_counters_tree_matching_and_cwd_container(self):
        (self.root / 'stat').write_text('cpu 100 0 100 700 100 0 0 0 999 999\nbtime 1000\n')
        self.proc_row(10, 1, 0, name='pueued')
        self.proc_row(20, 10, 10000, argv='bash /private/job-12345678/task.sh')
        self.proc_row(21, 20, 0, name='worker ) name', cwd='/private/worktrees/sen-test-4/packages/api')
        self.proc_row(30, 10, 500, argv='bash /home/u/remote-run/runs/run-abc/task.sh')
        self.proc_row(31, 30, 500, start=1)
        self.proc_row(40, 10, 0, argv='bash /private/job-123456789/task.sh')
        def advance(seconds):
            self.assertEqual(seconds, 1)
            (self.root / 'stat').write_text('cpu 300 0 200 1400 100 0 0 0 9999 9999\nbtime 1000\n')
            self.proc_row(20, 10, 10100, argv='bash /private/job-12345678/task.sh')
            self.proc_row(21, 20, 100, name='worker ) name')
            self.proc_row(30, 10, 550, argv='bash /home/u/remote-run/runs/run-abc/task.sh')
            self.proc_row(31, 30, 9999, start=2)  # PID reutilizado: no sumar su contador anterior.
        def docker(argv, timeout):
            self.assertEqual(argv[0], '/usr/bin/docker')
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 4)
            if argv[1] == 'ps':
                return 'sen-test-db-4\t1970-01-01 00:30:00 +0000 UTC\nunrelated\t1970-01-01 00:30:00 +0000 UTC\nsuite-30\t1970-01-01 00:30:00 +0000 UTC\n'
            self.assertEqual(argv[1:3], ['stats', '--no-stream'])
            return 'sen-test-db-4\t100.00%\t1MiB / 4GiB\nunrelated\t70.00%\t9GiB / 9GiB\nsuite-30\t50.00%\t2MB / 4GB\n'
        out = self.output()
        with patch.object(snapshot.time, 'sleep', side_effect=advance), patch.object(snapshot, 'command', side_effect=docker), patch.object(snapshot, 'docker_binary', return_value='/usr/bin/docker'):
            usage = snapshot.collect_usage(out, {1: 'job-12345678', 2: 'remote-run/runs/run-abc'}, 'linux', self.root)
        self.assertAlmostEqual(usage['busy'], 3.6)
        self.assertGreaterEqual(usage['interval'], 0)
        self.assertAlmostEqual(out['tasks'][0]['usage']['cpu'], 3.4)
        self.assertAlmostEqual(out['tasks'][1]['usage']['cpu'], 1.1)
        self.assertEqual(out['tasks'][0]['usage']['mem'], 4 * os.sysconf('SC_PAGE_SIZE') + 1024**2)
        self.assertEqual(out['tasks'][1]['usage']['containers'], 1)
        self.assertEqual(usage['outside']['cpu'], 0)  # Nunca negativo por diferencias entre fuentes.
        self.assertEqual(usage['used'], 40000000)
        self.assertEqual(usage['outside']['mem'], 40000000 - sum(t['usage']['mem'] for t in out['tasks'][:2]))
        self.assertNotIn('usage', out['tasks'][2])
        self.assertNotIn('private', json.dumps([usage, out]))
        # Un proceso que desaparece, una entrada rota y un directorio no PID no tumban /proc.
        (self.root / '99').mkdir()
        (self.root / '98').mkdir()
        (self.root / '98/stat').write_text('roto')
        (self.root / 'not-pid').mkdir()
        self.assertEqual(len(snapshot.linux_processes(self.root)[1]), 6)

    def test_macos_collect_single_ps_and_pid_container_without_cwd(self):
        raw = dict(groups={'suites': dict(status='Running', parallel_tasks=3)}, tasks={
            '1': dict(id=1, group='suites', status='Running', path='/private/remote-run/runs/run-1'),
            '2': dict(id=2, group='suites', status='Running', path='/private/remote-run/runs/run-2'),
            '3': dict(id=3, group='suites', status='Queued', path='/private/remote-run/runs/run-3')})
        calls = []
        def command(argv, timeout=5):
            calls.append(argv)
            if argv[0] == '/fake/pueue':
                return json.dumps(raw)
            if argv[0] == '/bin/ps':
                self.assertIn('pid=,ppid=,%cpu=,rss=,etime=,args=', argv)
                return ('10 1 0 10 2-03:04:05 /opt/homebrew/bin/pueued daemon\n'
                        '20 10 200 100 10:00 bash /private/remote-run/runs/run-1/body.sh SECRET\n'
                        '21 20 45 200 05:00 worker SECRET\n'
                        '30 10 0.7 64 01:00:00 bash /private/remote-run/runs/run-2/body.sh\n'
                        '40 10 100 50 00:01 bash /private/remote-run/runs/run-10/body.sh\n'
                        '50 1 10 500 10:00 com.docker.vm\n')
            if argv[1] == 'ps':
                # Creado hace un minuto: el PID 21 ya existía (nació hace cinco).
                stamp = datetime.datetime.fromtimestamp(time.time() - 60, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S +0000 UTC')
                return 'sen4951-suite-21\t' + stamp + '\nbuzz-espejo-x\t' + stamp + '\n'
            self.assertEqual(argv[1], 'stats')
            return 'sen4951-suite-21\t50%\t2MiB / 1GiB\nbuzz-espejo-x\t10%\t5MiB / 1GiB\n'
        with patch.object(snapshot, 'command', side_effect=command), patch.object(snapshot, 'mac_capacity', return_value=(dict(cores=10), dict(total=16000000, available=4000000))), patch.object(snapshot, 'mini_metadata', return_value=dict(name='suite-test')), patch.object(snapshot, 'disk', return_value=None), patch.object(snapshot, 'docker_binary', return_value='/opt/homebrew/bin/docker'), patch.object(snapshot.os, 'readlink', side_effect=AssertionError('macOS no lee cwd')):
            out = snapshot.collect('macos', 'mini', pueue='/fake/pueue', home=self.root)
        self.assertEqual(sum(argv[0] == '/bin/ps' for argv in calls), 1)
        self.assertEqual(out['usage']['interval'], 0)
        self.assertAlmostEqual(out['usage']['busy'], 3.557)
        self.assertEqual(out['usage']['docker'], 'read')
        self.assertAlmostEqual(out['tasks'][0]['usage']['cpu'], 2.95)
        self.assertEqual(out['tasks'][0]['usage']['mem'], 300 * 1024 + 2 * 1024**2)
        self.assertEqual(out['tasks'][0]['usage']['containers'], 1)
        self.assertAlmostEqual(out['tasks'][1]['usage']['cpu'], .007)
        self.assertNotIn('usage', out['tasks'][2])
        self.assertAlmostEqual(out['usage']['outside']['cpu'], .6)
        self.assertEqual(out['errors'], [])
        for secret in ('SECRET', '/private', 'sen4951', 'buzz-espejo', 'argv', 'cwd'):
            self.assertNotIn(secret, json.dumps(out))

    def test_container_rules_ambiguity_failures_and_absent_binary(self):
        contexts = {1: dict(born={20: 100.}, slots={'2'}), 2: dict(born={30: 100.}, slots={'2'})}
        created = '1970-01-01 00:10:00 +0000 UTC'  # 600: después de que nacieran 20 y 30
        def container(name, cpu, mem, when=600.):
            return dict(name=name, created=when, cpu=cpu, mem=mem)
        with patch.object(snapshot, 'command', side_effect=AssertionError('Docker ausente')):
            self.assertEqual(snapshot.docker_stats('linux', time.monotonic()+2), ('absent', []))
        with patch.object(snapshot, 'docker_binary', return_value='/usr/bin/docker'):
            # Un contenedor de dos dueños posibles o sin dueño queda fuera de las corridas.
            with patch.object(snapshot, 'command', side_effect=['sen-test-db-2\t' + created + '\nunknown\t' + created + '\nsuite-20\t' + created,
                                                                'sen-test-db-2\t20%\t1MiB / 1GiB\nunknown\t5%\t1MiB / 1GiB\nsuite-20\t30%\t1MiB / 1GiB\n']) as run:
                stats = snapshot.docker_stats('linux', time.monotonic()+2)
                self.assertEqual(run.call_count, 2)
            self.assertEqual(snapshot.container_usage(stats, contexts), ('read', {1: dict(cpu=.3, mem=1024**2, containers=1)}))
            for failure in (OSError(), subprocess.TimeoutExpired('docker', 1)):
                with patch.object(snapshot, 'command', side_effect=failure):
                    self.assertEqual(snapshot.docker_stats('linux', time.monotonic()+2), ('error', []))
            # Líneas ilegibles (contenedor arrancando, nan, negativo) no tumban las demás.
            with patch.object(snapshot, 'command', side_effect=['suite-30\t' + created, 'suite-20\t--\t-- / --\nsuite-20\tnan%\t1MiB / 1GiB\nsuite-20\t-1%\t1MiB / 1GiB\nroto\nsuite-30\t10%\t1KiB / 1GiB']):
                self.assertEqual(snapshot.docker_stats('linux', time.monotonic()+2), ('read', [container('suite-30', .1, 1024)]))
            with patch.object(snapshot, 'command', side_effect=AssertionError('presupuesto agotado')):
                self.assertEqual(snapshot.docker_stats('linux', time.monotonic()-1), ('error', []))
        # Un PID nacido después del contenedor es un PID reutilizado: el contenedor no es suyo.
        self.assertEqual(snapshot.container_usage(('read', [container('suite-20', .3, 1, when=50.)]), contexts), ('read', {}))
        # Sin fecha de creación o sin nacimiento del proceso no hay prueba de dueño.
        self.assertEqual(snapshot.container_usage(('read', [container('suite-20', .3, 1, when=None)]), contexts), ('read', {}))
        self.assertEqual(snapshot.container_usage(('read', [container('suite-20', .3, 1)]), {1: dict(born={20: None}, slots=set())}), ('read', {}))
        with patch.object(snapshot.Path, 'is_file', side_effect=lambda: False):
            # Probar la selección real sin ejecutar el binario.
            self.assertIsNone(UsageTests.real_docker_binary('linux'))

    real_docker_binary = staticmethod(snapshot.docker_binary)

    def test_review_w1_process_born_during_sample_keeps_its_cpu(self):
        (self.root / 'stat').write_text('cpu 100000 0 0 100000 0 0 0 0\nbtime 1000\n')
        (self.root / 'uptime').write_text('1000.00 0.00\n')
        hertz = os.sysconf('SC_CLK_TCK')
        self.proc_row(10, 1, 0, name='pueued')
        self.proc_row(20, 10, 0, argv='bash /private/job-12345678/task.sh')
        self.proc_row(22, 20, 50, start=10)  # ya vivía antes de la muestra: sólo cuenta su delta
        def advance(_):
            (self.root / 'stat').write_text('cpu 100100 0 0 100100 0 0 0 0\nbtime 1000\n')
            self.proc_row(21, 20, 100, start=1000 * hertz + 1)
            self.proc_row(22, 20, 50, start=10)
            self.proc_row(23, 20, 70, start=5)  # nació antes de la primera lectura: no se atribuye entero
        out = self.output()
        out['cpu']['cores'] = 2
        with patch.object(snapshot.time, 'sleep', side_effect=advance), patch.object(snapshot, 'docker_stats', return_value=('absent', [])):
            usage = snapshot.collect_usage(out, {1: 'job-12345678'}, 'linux', self.root)
        self.assertEqual((usage['busy'], out['tasks'][0]['usage']['cpu'], usage['outside']['cpu']), (1.0, 1.0, 0.0))

    def test_review_w2_reused_pid_never_claims_a_container(self):
        (self.root / 'stat').write_text('cpu 100000 0 0 100000 0 0 0 0\nbtime 1000\n')
        (self.root / 'uptime').write_text('1000.00 0.00\n')
        hertz = os.sysconf('SC_CLK_TCK')
        self.proc_row(10, 1, 0, name='pueued')
        self.proc_row(20, 10, 0, argv='bash /private/job-11111111/task.sh')
        self.proc_row(30, 10, 0, argv='bash /private/job-22222222/task.sh')
        self.proc_row(21, 20, 0, start=1)
        def advance(_):
            (self.root / 'stat').write_text('cpu 100100 0 0 100100 0 0 0 0\nbtime 1000\n')
            self.proc_row(21, 30, 0, start=1000 * hertz + 1)  # el PID 21 ahora es de la corrida 2
        # suite-21 lo creó el PID 21 original, antes de que naciera el nuevo.
        stats = ('read', [dict(name='suite-21', created=1500., cpu=1.0, mem=1048576)])
        out = self.output()
        out['cpu']['cores'] = 2
        with patch.object(snapshot.time, 'sleep', side_effect=advance), patch.object(snapshot, 'docker_stats', return_value=stats):
            usage = snapshot.collect_usage(out, {1: 'job-11111111', 2: 'job-22222222'}, 'linux', self.root)
        self.assertEqual([t.get('usage', {}).get('containers') for t in out['tasks'][:2]], [0, 0])
        self.assertEqual(out['tasks'][1]['usage']['cpu'], 0.0)
        self.assertEqual(usage['outside']['cpu'], 1.0)

    def test_docker_error_retains_process_usage_without_stage_error(self):
        out = self.output()
        rows = {10: dict(ppid=1, daemon=True, cpu=0, mem=0),
                20: dict(ppid=10, argv='/home/u/remote-run/runs/run-x/task.sh', cpu=2., mem=1024)}
        with patch.object(snapshot, 'process_sample', return_value=(0, 3., rows)), patch.object(snapshot, 'docker_binary', return_value='/usr/bin/docker'), patch.object(snapshot, 'command', side_effect=subprocess.TimeoutExpired('docker', 1)):
            out['usage'] = snapshot.collect_usage(out, {1: 'remote-run/runs/run-x', 2: 'unmatched'}, 'macos', self.root)
        self.assertEqual(out['usage']['docker'], 'error')
        self.assertEqual(out['tasks'][0]['usage'], dict(cpu=2., mem=1024, containers=0))
        self.assertNotIn('usage', out['tasks'][1])  # Sin árbol no hay lectura: el panel muestra «—», no 0 %.
        self.assertEqual(out['errors'], [])

    def test_usage_validation_and_old_snapshots(self):
        task_usage = dict(cpu=2.5, mem=12345, containers=1)
        host_usage = dict(interval=1.01, busy=4.5, used=123456, outside=dict(cpu=2., mem=111111), docker='read')
        for usage in (task_usage, None):
            out = serve.validate_snapshot(dict(sample(), usage=host_usage, tasks=[task(usage=usage)]), 'mini')
            self.assertEqual(out['usage'], host_usage)
            self.assertEqual(out['tasks'][0]['usage'], usage)
        self.assertNotIn('usage', serve.validate_snapshot(sample(), 'mini'))
        self.assertIsNone(serve.validate_snapshot(dict(sample(), tasks=[task(state='Paused', usage=task_usage)]), 'mini')['tasks'][0]['usage'])
        for key in task_usage:
            for bad in (True, -1, float('nan'), float('inf'), '1', [], None):
                out = serve.validate_snapshot(dict(sample(), tasks=[task(usage=dict(task_usage, **{key: bad}))]), 'mini')
                self.assertIsNone(out['tasks'][0]['usage'], (key, bad))
        for key in ('mem', 'containers'):
            self.assertIsNone(serve.valid_usage(dict(task_usage, **{key: 1.5})))
        for key in ('interval', 'busy', 'used'):
            for bad in (True, -1, float('nan'), float('inf'), '1', [], None):
                out = serve.validate_snapshot(dict(sample(), usage=dict(host_usage, **{key: bad})), 'mini')
                self.assertIsNone(out['usage'], (key, bad))
        for bad in (dict(host_usage, used=1.5), dict(host_usage, docker='unknown'), dict(host_usage, outside=dict(cpu=-1, mem=1)),
                    dict(host_usage, outside=dict(cpu=1, mem=1.5)), dict(host_usage, outside=dict(cpu=float('nan'), mem=1)),
                    dict(host_usage, outside=dict(cpu=1, mem=1, cwd='SECRET')), dict(host_usage, argv='SECRET'), [], True):
            self.assertIsNone(serve.validate_snapshot(dict(sample(), usage=bad), 'mini')['usage'])
        self.assertIsNone(serve.valid_usage(dict(task_usage, name='SECRET')))


class UsageRenderTests(unittest.TestCase):
    def test_render_ranking_percentages_legacy_and_escaping(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node no está disponible para ejecutar el render real')
        fixture = json.loads((BASE / 'tests/rack-fixture.json').read_text())
        html = (BASE / 'index.html').read_text()
        js = html.split('<script>', 1)[1].split('</script>', 1)[0]
        # Ejecuta el script de producción con DOM mínimo; no hace HTTP ni usa dependencias.
        harness = r'''
const vm=require('vm'), assert=require('assert');
const input=JSON.parse(require('fs').readFileSync(0,'utf8'));
const elements={};
const context={console, Intl, Date, Number, Object, String, Math,
  document:{getElementById:id=>elements[id] ||= {addEventListener(){},innerHTML:'',textContent:''},
    addEventListener(){},querySelector:()=>null},
  window:{addEventListener(){}},setInterval(){},AbortSignal:{timeout(){}},
  fetch:async()=>({ok:false})};
vm.createContext(context); vm.runInContext(input.js,context);
context.fixture=input.fixture;
vm.runInContext(`render(fixture)`,context);
let html=elements.app.innerHTML;
assert.equal((html.match(/<table class="usage">/g)||[]).length,2);
assert.equal((html.match(/Fuera de las corridas/g)||[]).length,2);
assert(html.includes('0,4 %')); assert(html.includes('25 %'));
assert(html.includes('aria-hidden="true"'));
assert(html.includes('<th scope="col">CPU</th>'));
assert(html.includes('<th scope="col">RAM</th>'));
assert(html.includes('Incluye contenedores atribuibles.'));
vm.runInContext(`
const h=S.hosts[0];
h.tasks=[{id:1,state:'Running',name:'menos',usage:{cpu:1,mem:1}},
 {id:2,state:'Running',name:'<más>',usage:{cpu:3,mem:2}},
 {id:3,state:'Running',name:'empate',usage:{cpu:3,mem:9}},
 {id:4,state:'Paused',name:'pausada',usage:{cpu:9,mem:9}},
 {id:5,state:'Queued',name:'enfila'}];
document.getElementById('app').innerHTML=resourceTable(h);
`,context);
html=elements.app.innerHTML;
assert(html.indexOf('empate')<html.indexOf('&lt;más&gt;'));
assert(html.indexOf('&lt;más&gt;')<html.indexOf('menos'));
assert(html.indexOf('menos')<html.indexOf('Fuera de las corridas'));
assert(!html.includes('pausada')); assert(!html.includes('enfila')); assert(!html.includes('<más>'));
for(const status of ['absent','error']){
 vm.runInContext(`S.hosts[0].usage.docker='${status}'; document.getElementById('app').innerHTML=resourceTable(S.hosts[0])`,context);
 assert(elements.app.innerHTML.includes(status==='absent'?'Docker no está instalado':'Docker no respondió'));
}
vm.runInContext(`S.hosts[0].tasks=[]; document.getElementById('app').innerHTML=resourceTable(S.hosts[0])`,context);
assert.equal(elements.app.innerHTML,'');
for(const host of context.fixture.hosts) delete host.snapshot.usage;
vm.runInContext(`render(fixture)`,context);
assert(!elements.app.innerHTML.includes('<table class="usage">'));
assert(elements.app.innerHTML.includes('slot busy'));
console.log('render: ranking, porcentajes es, escape, Docker y compatibilidad OK');
'''
        result = subprocess.run([node, '-e', harness], input=json.dumps(dict(js=js, fixture=fixture)),
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('compatibilidad OK', result.stdout)


class ActivityTests(unittest.TestCase):
    setUp = SnapshotTests.setUp
    fake_pueue = SnapshotTests.fake_pueue

    def test_activity_uses_only_stat_and_latest_log(self):
        secret = 'ghp_SECRET_NEVER_READ'
        for mode in ('macos', 'linux'):
            logdir = self.root / ('Library/Application Support/pueue' if mode == 'macos' else '.local/share/pueue') / 'task_logs'
            logdir.mkdir(parents=True)
            run = self.root / 'remote-run/runs' / ('run-' + mode)
            run.mkdir(parents=True)
            raw = dict(id=42, path=str(run))
            for path, mtime in ((logdir / '42.log', 200), (run / 'log', 400)):
                path.write_text(secret)
                os.utime(path, (mtime, mtime))
            with patch.object(Path, 'open', side_effect=AssertionError('No se abre la salida')):
                result = snapshot.task_activity(raw, mode, self.root, {})
            self.assertEqual(result, dict(last=400, kind=None, ref=run.name))
            self.assertNotIn(secret, json.dumps(result))
            os.utime(logdir / '42.log', (500, 500))
            self.assertEqual(snapshot.task_activity(raw, mode, self.root, {})['last'], 500)
            (run / 'log').unlink()
            (logdir / '42.log').unlink()
            self.assertIsNone(snapshot.task_activity(raw, mode, self.root, {})['last'])

    def test_verify_and_chain_keep_credentials_private(self):
        job = self.root / 'job-verify'
        job.mkdir()
        body = job / 'body.sh'
        raw = dict(id=9, group='jale', command='exec bash ' + str(job / 'task.sh'), status={'Paused': {'start': 100}})
        (job / 'log').write_text('github_pat_LOG_SECRET')
        os.utime(job / 'log', (300, 300))
        for mode, slot, kind in [('launch', 'verify', 'verify'), ('launch', 'other', 'chain'), ('gates', 'verify', 'chain')]:
            body.write_text('declare -- MODE="' + mode + '"\ndeclare -- SLOT_KIND="' + slot + '"\ndeclare -- GH_T="github_pat_BODY_SECRET"\n')
            data = snapshot.collect('linux', 'desk', pueue=self.fake_pueue({'9': raw}), home=self.root)
            self.assertEqual(data['tasks'][0]['activity'], dict(last=300, kind=kind, ref='job-verify'))
            self.assertNotIn('SECRET', json.dumps(data))
            raw['status'] = 'Queued'
            queued = snapshot.collect('linux', 'desk', pueue=self.fake_pueue({'9': raw}), home=self.root)
            self.assertEqual(queued['tasks'][0]['activity'], dict(last=None, kind=kind, ref='job-verify'))
            raw['status'] = {'Paused': {'start': 100}}

    def test_invalid_activity_is_discarded_without_losing_snapshot(self):
        good = dict(last=12.5, kind='chain', ref='job-x')
        invalid = [True, [], {}, dict(good, last=float('nan')), dict(good, last=float('inf')),
                   dict(good, last=True), dict(good, kind='launch'), dict(good, ref='x' * 201),
                   dict(good, ref=1), dict(good, ref='bad\nref'), dict(good, command='private')]
        for activity in invalid + [good, dict(last=None, kind=None, ref=None)]:
            data = dict(sample(), tasks=[task(activity=activity, command='private', envs={'GH_T': 'private'}, path='/private')])
            safe = serve.validate_snapshot(data, 'mini')
            self.assertEqual(safe['tasks'][0]['activity'], activity if activity in [good, dict(last=None, kind=None, ref=None)] else None)
            self.assertNotIn('private', json.dumps(safe, allow_nan=False))


class InspectorTests(unittest.TestCase):
    def setUp(self):
        self.row = task(id=77, group='jale', name='launch', ticket='SEN-5731', start=100,
                        activity=dict(last=200, kind='verify', ref='job-abc'))
        payload = dict(hosts=[dict(id='desk', snapshot=dict(tasks=[self.row]), estimates={'77': dict(n=5, typical=60, low=30, high=90)})],
                       tickets={'SEN-5731': dict(title='Título de prueba', project='Jale')})
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())

    def run_inspector(self, ref, *args, url=None, env=None):
        argv = ['/usr/bin/python3', '-B', str(BASE / 'rack-inspeccionar'), ref, *args]
        if url is not None:
            argv += ['--url', url]
        return subprocess.run(argv, capture_output=True, text=True, env=env or dict(os.environ, RACK_URL=self.url), timeout=5)

    def test_short_and_full_reference_and_read_commands(self):
        for ref in ['desk:77', 'rack desk:77 · jale · SEN-5731 · Jale · Arranque']:
            result = self.run_inspector(ref, url=self.url)
            self.assertEqual(result.returncode, 0, result.stderr)
            for value in ['host: desk', 'id: 77', 'título: Título de prueba', 'producto: Jale',
                          'ssh desk \'/home/felix/.local/bin/pueue log 77 --lines 50\'',
                          '/home/felix/.local/state/jale/pueue/job-abc', 'Para seguir:']:
                self.assertIn(value, result.stdout)
        self.assertEqual(self.run_inspector('desk:77', url=self.url, env=dict(os.environ, RACK_URL='http://127.0.0.1:1')).returncode, 0)

    def test_json_returns_exact_task_and_environment_url(self):
        result = self.run_inspector('rack desk:77 · ignored', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), self.row)

    def test_error_codes_and_single_line_messages(self):
        for ref in ['wrong', 'desk:77; rm', 'rack mini:x · texto', 'other:77']:
            result = self.run_inspector(ref)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(len(result.stderr.splitlines()), 1)
        for ref, url, code in [('desk:78', self.url, 3), ('mini:77', self.url, 3),
                               ('desk:77', 'http://127.0.0.1:1', 4)]:
            result = self.run_inspector(ref, url=url)
            self.assertEqual(result.returncode, code)
            if code:
                self.assertEqual(len(result.stderr.splitlines()), 1)

    def test_remote_run_commands(self):
        self.row['activity'] = dict(last=200, kind=None, ref='20261112-174413-f28e66')
        result = self.run_inspector('desk:77')
        self.assertEqual(result.returncode, 0)
        self.assertIn("REMOTE_RUN_HOST=desk remote-run status 20261112-174413-f28e66", result.stdout)
        self.assertIn("REMOTE_RUN_HOST=desk remote-run logs 20261112-174413-f28e66", result.stdout)


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

    def request_as(self, ip, owner_result=None, funnel=False, host='100.100.0.1:8787', names=()):
        handler = object.__new__(serve.Handler)
        handler.client_address = (ip, 1234)
        handler.server = type('Server', (), dict(server_address=('100.100.0.1', 8787), rack=self.rack,
                                               owner_check=owner_result, names=names))()
        handler.headers = HTTPMessage()
        for value in (host if isinstance(host, tuple) else (host,) if host else ()):
            handler.headers['Host'] = value
        if funnel:
            handler.headers['Tailscale-Funnel-Request'] = '?1'
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

    def test_host_must_name_this_server(self):
        check = self.fake_tailscale()
        for host in ('mini.example.ts.net:8787', 'MINI:8787', '100.100.0.1:8787'):
            self.assertEqual(self.request_as('100.111.159.14', check, host=host, names=('mini.example.ts.net', 'mini'))[0], 200)
        for host in ('evil.example:8787', 'mini.example.ts.net:80', 'mini.example.ts.net', '100.100.0.1', None,
                     ('100.100.0.1:8787', 'evil.example:8787'), ('100.100.0.1:8787', '100.100.0.1:8787')):
            self.assertEqual(self.request_as('100.111.159.14', check, host=host, names=('mini.example.ts.net', 'mini')), (403, b''))

    def test_rebound_host_cannot_read_or_refresh(self):
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
        host = 'evil.example:' + str(server.server_port)
        for request in (urllib.request.Request(url + '/api/rack.json', headers={'Host': host}),
                        urllib.request.Request(url + '/api/refresh', data=b'', method='POST', headers={'Host': host, 'X-Rack-Refresh': '1'})):
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 403)
        self.assertFalse(self.rack.wake.is_set())

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
        # whois es nieto del sh: muerto, queda zombi hasta que lo recoge quien lo adopta, y un zombi ya no corre.
        pid = int(pidfile.read_text())
        for _ in range(100):
            state = subprocess.run(['/bin/ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
            if not state or state.startswith('Z'):
                break
            time.sleep(0.02)
        else:
            os.kill(pid, 9)
            self.fail('whois sigue vivo tras el plazo')

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
