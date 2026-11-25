import copy
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import patch

from test_rack import sample, serve, task


def snapshots():
    ref = '20261116-120000-abcdef'
    at = '2026-11-16T12:00:00Z'
    running = task(
        name='dirigida-ejemplo', scope='dirigida', project='Ejemplo', ticket='SEN-7288',
        repo='Org/Repo', sha='a' * 40, branch='fix/sen-7288', subject='Ejemplo', priority=2,
        enqueued=900, start=920,
        control=dict(launcher='remote-run', ref=ref, expected=dict(task_id=1, state='Running'), reason=None),
        activity=dict(last=950, kind='verify', ref=ref),
        admission=dict(task_id=1, task_created_at=at, ref=ref, daemon_generation='example',
                       attempt='12345678-1234-1234-1234-123456789abc', seq=1, phase='running',
                       arrived_at=900, deadline_at=980, updated_at=950, payload_started_at=925,
                       resources=['path'], positions={'path': None},
                       blockers=[dict(resource='path', kind='holder', task_id=2, ref=None, seq=2)]),
        usage=dict(cpu=0.5, mem=64, containers=1))
    interrupted = task(
        id=2, group='jale', state='Done', enqueued=900, start=910, end=980, result='Killed',
        interruption_reason='Interrumpida al reiniciar Pueue; relanzar manualmente')
    mini = dict(sample(), tasks=[running], cpu=dict(cores=4, load=[1.0, 2.0, 3.0], psi_avg60=None),
                mem=dict(total=1000, available=500, swap_total=40, swap_used=10),
                disk=dict(total=2000, free=1000),
                usage=dict(interval=1, busy=0.75, used=500, outside=dict(cpu=0.25, mem=436), docker='read'),
                errors=[dict(stage='metadata', message='Lectura fallida: OSError')],
                control=serve.accion.host_channel.unavailable())
    guard = dict(action='mantener', at=at, cpu_pause=30, cpu_resume=15, status='fresh',
                 pressure=dict(at=at, cpu=20, io=10, memory=600),
                 thresholds=dict(normal=dict(pause=dict(cpu=30, io=40, memory=400),
                                             resume=dict(cpu=15, io=20, memory=600)),
                                 rapidas=dict(pause=dict(memory=400), resume=dict(memory=600))),
                 last_result=dict(evento='retenido', tick_id='1' * 24, at=at,
                                  bloqueos=[dict(grupo='jale', operacion='one', causa='docker')]),
                 last_admission=dict(at=at, grupo='jale', id=3))
    desk = dict(sample(), host='desk', os='linux', cpu=dict(cores=8, load=[0, 1, 2], psi_avg60=20),
                guard=guard, groups=[dict(name='jale', slots=2, status='Paused')], tasks=[interrupted],
                orphans=[dict(pid=42, comm='example', origin_task='2', group='jale', origin_state='Killed',
                              rss_bytes=64, cpu_percent=0.5, started_at=at)], orphans_total=1)
    return dict(mini=mini, desk=desk)


class SnapshotPublicationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        hosts = [dict(id=host, name=host, transport='local', argv=['false']) for host in ('mini', 'desk')]
        self.rack = serve.Rack(hosts, root, root / 'absent-key')
        for method in ('resolve_commits', 'refresh_tickets'):
            stub = patch.object(self.rack, method)
            stub.start()
            self.addCleanup(stub.stop)
        server = ThreadingHTTPServer(('127.0.0.1', 0), serve.Handler)
        server.rack = self.rack
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = f'http://127.0.0.1:{server.server_port}/api/rack.json'

    def read_hosts(self, data):
        self.rack.cycle(lambda host: data[host['id']], now=1000)
        with urllib.request.urlopen(self.url, timeout=5) as response:
            self.assertEqual(response.status, 200)
            payload = json.load(response)
        return {host['id']: host for host in payload['hosts']}

    def published(self, data):
        hosts = self.read_hosts(data)
        for host in hosts.values():
            self.assertTrue(host['fresh'], host)
        return {ident: host['snapshot'] for ident, host in hosts.items()}

    def operational_snapshots(self):
        from test_mode import ModeChannelTests
        from test_reorder import ReorderChannelTests

        data = snapshots()
        for host, case, held, waiting in (
                ('mini', ReorderChannelTests(), 'default', 'rapidas'),
                ('desk', ModeChannelTests(), 'jale', 'otros')):
            self.addCleanup(case.doCleanups)
            case.setUp()
            engine = case.engine
            case.p.groups[held]['status'] = case.p.groups[waiting]['status'] = 'Paused'
            with engine.connect() as db:
                state = engine.resolve(engine.state(db))
            state['reasons'].update({held: ['manual', 'startup'], waiting: ['startup']})
            with patch.object(engine, 'resolve', return_value=state):
                data[host]['control'] = engine.view()
            data[host]['groups'] = [dict(name=name, status=group['status'], slots=group['parallel_tasks'])
                                    for name, group in case.p.groups.items()]

        expected = copy.deepcopy(data)
        for snapshot in expected.values():
            control = snapshot['control']
            del control['pending_actions']
            del control['capabilities']['client']
            if control['capabilities']['reason_reorder'] is None:
                del control['capabilities']['reason_reorder']
        return data, expected

    def test_preserves_known_fields(self):
        data = snapshots()
        self.assertEqual(self.published(data), snapshots())

    def test_extensions_do_not_reach_the_api(self):
        expected = snapshots()
        data = copy.deepcopy(expected)
        mini = data['mini']
        row = mini['tasks'][0]
        guard = data['desk']['guard']
        orphan = data['desk']['orphans'][0]
        levels = [
            mini, mini['cpu'], mini['mem'], mini['disk'], mini['groups'][0], mini['errors'][0],
            mini['control'], mini['control']['capabilities'], mini['control']['error'], row,
            guard, guard['pressure'], guard['thresholds'], guard['last_result'],
            guard['last_result']['bloqueos'][0], guard['last_admission'], orphan,
        ]
        for limits in guard['thresholds'].values():
            levels.extend((limits, limits['pause'], limits['resume']))
        for level in levels:
            level['new_field'] = dict(nested='synthetic')
        mini['new_field'] = float('nan')
        orphan['command'] = 'synthetic'
        for private in ('envs', 'command', 'original_command', 'path'):
            row[private] = 'synthetic'
            guard[private] = 'synthetic'
        self.assertEqual(self.published(data), expected)

        for path in (('control',), ('control', 'expected'), ('activity',), ('usage',),
                     ('admission',), ('admission', 'positions'), ('admission', 'blockers', 0)):
            with self.subTest(path=path):
                expected = snapshots()
                data = copy.deepcopy(expected)
                level = data['mini']['tasks'][0]
                for key in path:
                    level = level[key]
                level['new_field'] = 'synthetic'
                task_out = expected['mini']['tasks'][0]
                if path[0] == 'admission':
                    del task_out['admission']
                else:
                    task_out[path[0]] = None
                self.assertEqual(self.published(data), expected)

        for path in (('usage',), ('usage', 'outside')):
            with self.subTest(host_path=path):
                expected = snapshots()
                data = copy.deepcopy(expected)
                level = data['mini']
                for key in path:
                    level = level[key]
                level['new_field'] = 'synthetic'
                expected['mini']['usage'] = None
                self.assertEqual(self.published(data), expected)

    def test_operational_control_projects_extensions(self):
        baseline, expected = self.operational_snapshots()
        self.assertEqual(self.published(baseline), expected)
        for host in baseline:
            for path, extra in (
                    ((), 'synthetic'), (('capabilities',), True),
                    (('pause_reasons',), ['startup']),
                    (('startup_holds',), 'pausa manual'),
                    (('startup_waiting',), 'cuotas_sensores')):
                with self.subTest(host=host, path=path):
                    data = copy.deepcopy(baseline)
                    level = data[host]['control']
                    for key in path:
                        level = level[key]
                    level['new_field'] = extra
                    self.assertEqual(self.published(data), expected)

    def test_operational_control_rejects_extensions_in_exact_contracts(self):
        baseline, expected = self.operational_snapshots()
        self.assertEqual(self.published(baseline), expected)
        for host, paths in (
                ('mini', (('slots',), ('desired',), ('reorder_groups',),
                          ('reorder_groups', 'suites'), ('reorder_groups', 'suites', 'expected'),
                          ('reorder_groups', 'suites', 'expected', 'queue', 0))),
                ('desk', (('slots',), ('desired',), ('reorder_groups',), ('tables',),
                          ('tables', 'dia'), ('tables', 'noche'), ('mode_pending',)))):
            for path in paths:
                with self.subTest(host=host, path=path):
                    data = copy.deepcopy(baseline)
                    level = data[host]['control']
                    for key in path:
                        level = level[key]
                    level['new_field'] = 'synthetic'
                    hosts = self.read_hosts(data)
                    self.assertIs(hosts[host]['fresh'], False)
                    self.assertEqual(hosts[host]['error'], 'No se pudo leer el host: ValueError')
                    self.assertEqual({ident: row['snapshot'] for ident, row in hosts.items()}, expected)
                    other = 'desk' if host == 'mini' else 'mini'
                    self.assertIs(hosts[other]['fresh'], True)
