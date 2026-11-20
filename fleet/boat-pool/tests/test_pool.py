import contextlib
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pool
from boat_api import Boat, BoatError


NAME = 'vm-pruebas'
REPO = 'ejemplo/proyecto'


def fake_id(suffix):
    return 'bx_' + suffix


FAKE_ID = fake_id('abcd2345')
NOW = datetime(2026, 11, 23, 12, tzinfo=timezone.utc).timestamp()
MONTH = '2026-11-01T00:00:00Z'
MOCK_KEY = 'boat_demo_local'
LIMITS = {
    'starts': {'day': {'remaining': 100}},
    'planIncluded': {'vmHoursRemaining': 48},
    'wallet': {'balanceDollars': 10},
}


def monthly_cost(seconds, dollars):
    return {'since': MONTH, 'seconds': seconds, 'dollars': dollars}


class Double:
    def __init__(self, state):
        self.path = state / 'api-falsa.json'
        self.now = NOW
        self.calls = []
        self.keys = [{'keyPrefix': 'boat_demo', 'keyLastFour': MOCK_KEY[-4:], 'expiresAt': None}]
        self.limits = json.loads(json.dumps(LIMITS))
        self.stop_fails = False
        self.extend_fails = False
        self.sleeps = 0
        self.resume_fields = {'holdsCreatorLogins': False}
        self.page_size = 200
        self.deleted = monthly_cost(0, 0)
        self.usage = {}

    def factory(self, org, service):
        return Boat(org, service, transport=self.transport, key_reader=lambda _: MOCK_KEY)

    def sandbox(self, identifier, **fields):
        vms = pool.read_json(self.path) if self.path.exists() else {}
        vms.setdefault(identifier, {'id': identifier, 'name': NAME, 'state': 'ready',
                                    'cost': monthly_cost(0, 0)})
        vms[identifier].update(fields)
        pool.atomic_json(self.path, vms)

    def transport(self, method, path, headers, body, timeout):
        if headers['Authorization'] != 'Bearer ' + MOCK_KEY or headers['X-Boat-Org'] != 'org-prueba':
            raise AssertionError('credenciales del doble')
        self.calls.append((method, path, body))
        if path == '/limits':
            return self.limits
        if path == '/api-keys':
            return {'apiKeys': self.keys}
        vms = pool.read_json(self.path) if self.path.exists() else {}
        url = urlsplit(path)
        query = parse_qs(url.query)
        if url.path == '/sandboxes':
            if 'name' in query:
                return {'sandboxes': [vm for vm in vms.values() if vm['name'] in query['name']]}
            start = int(query.get('cursor', ['0'])[0])
            end = start + self.page_size
            has_more = end < len(vms)
            return {'sandboxes': list(vms.values())[start:end],
                    'pageInfo': {'hasMore': has_more, 'nextCursor': str(end) if has_more else None},
                    'deletedCost': self.deleted}
        _, group, identifier, *operation = url.path.split('/')
        if group != 'sandboxes':
            raise AssertionError('ruta inesperada')
        if method == 'GET' and operation == ['usage']:
            if identifier not in vms:
                raise HTTPError(path, 404, 'sandbox no encontrado', None, None)
            since, = query['since']
            until, = query['until']
            return {
                'ok': True, 'type': 'sandbox.usage', 'sandboxId': identifier,
                'sandboxType': 'large', 'billingMultiplier': 2,
                'since': since, 'until': until, 'seconds': 0, 'dollars': 0,
                'secondsPerDollar': 100000, 'running': True,
                **self.usage.get((identifier, since), {}),
            }
        vm = vms[identifier]
        if method == 'GET' and not operation:
            return {'sandbox': vm}
        if method == 'PATCH' and not operation:
            if self.extend_fails:
                raise BoatError(MOCK_KEY)
            vm.update(archiveAfter=pool.stamp(self.now + body['ttlSeconds']))
        elif method == 'POST' and operation == ['resume']:
            vm.update(state='provisioning', stoppedAt=None, **self.resume_fields)
        elif method == 'POST' and operation == ['stop']:
            if self.stop_fails:
                raise BoatError(MOCK_KEY)
            vm.update(state='archived', stoppedAt=pool.stamp(self.now))
        else:
            raise AssertionError('mutación inesperada')
        pool.atomic_json(self.path, vms)
        return {'sandbox': vm}

    def sleep(self, seconds):
        self.sleeps += 1
        vms = pool.read_json(self.path)
        for vm in vms.values():
            if vm['state'] == 'provisioning':
                vm['state'] = 'ready'
        pool.atomic_json(self.path, vms)


def worker():
    state = Path(os.environ['XDG_STATE_HOME']) / 'boat-pool'
    double = Double(state)
    return pool.main(sys.argv[2:], boat_factory=double.factory, now=lambda: NOW, notify=lambda _: None)


class Harness(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.home = self.base / 'home'
        self.state = self.base / 'state' / 'boat-pool'
        self.config_file = self.base / 'config' / 'boat-pool' / 'pool.json'
        self.state.mkdir(parents=True)
        self.config_file.parent.mkdir(parents=True)
        self.home.mkdir()
        self.env = {
            'HOME': str(self.home), 'XDG_STATE_HOME': str(self.state.parent),
            'XDG_CONFIG_HOME': str(self.config_file.parent.parent),
        }
        environment = patch.dict(os.environ, self.env)
        environment.start()
        self.addCleanup(environment.stop)
        self.config = {
            'version': 1, 'org': 'org-prueba', 'llavero': 'servicio-prueba',
            'prohibidos': [],
            'inactividad_min': 30, 'reserva_arranques': 100,
            'piso': {'horas_plan': 48, 'monedero_usd': 10}, 'vms': [],
        }
        self.api = Double(self.state)
        self.notices = []
        self.add_vm()

    def add_vm(self, name=NAME, identifier=FAKE_ID, *, apt=True, places=1,
               overrides=None, created=NOW, remote_state='ready'):
        declaration = {'nombre': name, 'repo': REPO, 'tipo': 'large', 'lugares': places,
                       'limite_clase': {'e2e': 1}, **(overrides or {})}
        self.config['vms'].append(declaration)
        folder = pool.private_dir(self.state / 'vms' / name)
        pool.atomic_json(folder / 'registro.json', {
            'nombre': name, 'id': identifier, 'repo': declaration['repo'],
            'tipo': declaration['tipo'], 'preparada_en': pool.stamp(created),
            'revision_runner': 'rev-vieja', 'cpu_robada_pct': 1, 'apta': apt,
        })
        self.api.sandbox(identifier, name=name, state=remote_state)

    def save(self):
        pool.atomic_json(self.config_file, self.config)

    def run_cli(self, *args, factory=None):
        self.save()
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = pool.main(args, boat_factory=factory or self.api.factory,
                                 now=lambda: self.api.now, sleep=self.api.sleep, notify=self.notices.append)
            except SystemExit as error:
                code = error.code
        return code, stdout.getvalue(), stderr.getvalue()

    def args(self, run='corrida', kind='contract', *extra):
        return 'pedir', '--repo', REPO, '--corrida', run, '--clase', kind, *extra

    def get_lease(self, *args):
        code, output, stderr = self.run_cli(*self.args(*args))
        self.assertEqual(code, 0, stderr)
        return json.loads(output)['arriendo']

    def assert_unavailable(self, cause, *args, **kwargs):
        code, output, stderr = self.run_cli(*args, **kwargs)
        self.assertEqual(code, 75, stderr)
        self.assertFalse(output)
        self.assertTrue(stderr.startswith(f'boat-pool: {cause}: '), stderr)

    def start_worker(self, *args):
        self.save()
        process = subprocess.Popen(
            [sys.executable, '-IB', __file__, 'worker', *args],
            env={**os.environ, **self.env}, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

        self.addCleanup(cleanup)
        return process

    def queue(self, run):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            for path in (self.state / 'espera').glob('*.json'):
                try:
                    if pool.read_json(path)['corrida'] == run:
                        return
                except FileNotFoundError:
                    pass
            time.sleep(0.02)
        self.fail('no llegó a la fila: ' + run)

    def result(self, process, expected):
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, expected, stderr)
        return stdout

    def journal(self, name=NAME):
        return pool.events(self.state / 'vms' / name / 'eventos.jsonl')

    def writes(self):
        return [call for call in self.api.calls if call[0] in ('POST', 'PATCH')]

    def usage_calls(self):
        return [route for method, route, _ in self.api.calls if method == 'GET' and '/usage?' in route]

    def test_solo_un_lugar_con_dos_procesos(self):
        requests = [self.start_worker(*self.args(run, 'contract', '--esperar', '1'))
                    for run in ('primera', 'segunda')]
        outputs = [process.communicate(timeout=8) for process in requests]
        self.assertCountEqual([process.returncode for process in requests], [0, 75], outputs)
        self.assertEqual(sum(event['evento'] == 'concedido' for event in self.journal()), 1)

    def test_prioridad_tren_y_fifo(self):
        original = self.get_lease()
        jobs = []
        for name, priority in (('suite', 'suite'), ('tren-viejo', 'tren'), ('tren-nuevo', 'tren')):
            process = self.start_worker(*self.args(name, 'contract', '--esperar', '8', '--prioridad', priority))
            jobs.append(process)
            self.queue(name)
        self.assertEqual(self.run_cli('soltar', original)[0], 0)
        for process in (jobs[1], jobs[2], jobs[0]):
            output = self.result(process, 0)
            self.assertEqual(self.run_cli('soltar', json.loads(output)['arriendo'])[0], 0)
        delivered = [item['corrida'] for item in self.journal() if item['evento'] == 'concedido']
        self.assertEqual(delivered, ['corrida', 'tren-viejo', 'tren-nuevo', 'suite'])

    def test_turno_muerto_no_bloquea(self):
        lease = self.get_lease()
        process = self.start_worker(*self.args('muerto', 'contract', '--esperar', '5', '--prioridad', 'tren'))
        self.queue('muerto')
        process.kill()
        process.communicate(timeout=5)
        self.assertEqual(self.run_cli('soltar', lease)[0], 0)
        self.get_lease('vivo')

    def test_limite_por_clase_y_latido(self):
        self.config['vms'][0]['lugares'] = 2
        lease = self.get_lease('primera', 'e2e')
        self.assertEqual(self.run_cli(*self.args('segunda', 'e2e'))[0], 75)
        self.get_lease('tercera', 'contract')
        self.api.now += 10
        self.assertEqual(self.run_cli('latido', lease, '--ttl', '3600')[0], 0)
        data = pool.read_json(self.state / 'vms' / NAME / 'arriendos' / (lease + '.json'))
        self.assertEqual(pool.epoch(data['vence']), self.api.now + 3600)

    def test_arriendo_vencido_y_soltar_idempotente(self):
        lease = self.get_lease('caduca', 'contract', '--ttl', '300')
        self.api.now += 301
        second = self.get_lease('segunda')
        self.assertEqual(self.run_cli('latido', lease)[0], 69)
        self.assertEqual(self.run_cli('soltar', second)[0], 0)
        self.assertEqual(self.run_cli('soltar', second)[0], 0)
        self.assertEqual(sum(item['evento'] == 'vencido' for item in self.journal()), 1)
        self.assertEqual(sum(item['evento'] == 'soltado' for item in self.journal()), 1)

    def test_apagar_protege_arriendo_vivo_sin_http(self):
        self.get_lease()
        self.api.calls.clear()
        self.assertEqual(self.run_cli('apagar', NAME)[0], 75)
        self.assertFalse(self.api.calls)

    def test_enciende_y_espera_ready_con_puertas_iguales_al_piso(self):
        self.api.sandbox(FAKE_ID, state='archived')
        self.get_lease()
        self.assertEqual(self.writes(), [
            ('POST', '/sandboxes/' + FAKE_ID + '/resume', {'noEnv': True, 'type': 'large'}),
        ])
        self.assertEqual(self.api.sleeps, 1)
        self.assertEqual([item['evento'] for item in self.journal()], ['registro', 'arranque', 'concedido'])

    def test_cada_puerta_cerrada_se_registra_y_no_reanuda(self):
        self.api.sandbox(FAKE_ID, state='archived')
        for gate, section, field, value in (
            ('reserva_arranques', 'starts', 'day', {'remaining': 99}),
            ('horas_plan', 'planIncluded', 'vmHoursRemaining', 47),
            ('monedero_usd', 'wallet', 'balanceDollars', 9),
        ):
            with self.subTest(gate=gate):
                self.api.limits = json.loads(json.dumps(LIMITS))
                self.api.limits[section][field] = value
                self.assert_unavailable('puerta', *self.args())
                self.assertIn(gate, {item.get('puerta') for item in self.journal()})
                self.assertEqual(self.writes(), [])

    def test_no_llama_http_con_nombre_o_id_prohibido(self):
        for name in ('ci-pruebas', 'hasito-sen', FAKE_ID, 'ajena'):
            with self.subTest(name=name):
                self.assert_unavailable('sin_vm', 'encender', name)
                self.assertFalse(self.api.calls)
        self.config['prohibidos'] = [FAKE_ID]
        self.assert_unavailable('sin_vm', *self.args())
        self.assertFalse(self.api.calls)
        self.config['prohibidos'] = [{'id': FAKE_ID, 'hasta': pool.stamp(NOW + 1)}]
        self.assert_unavailable('sin_vm', 'apagar', NAME)
        self.assertFalse(self.api.calls)
        self.api.now += 1
        self.assertEqual(self.run_cli('apagar', NAME)[0], 0)

    def test_un_registro_prohibido_no_bloquea_soltar_ni_vigilar(self):
        other = fake_id('efgh2345')
        self.add_vm('vm-reservada', other)
        token = self.get_lease()
        self.config['prohibidos'] = [other]
        self.assertEqual(self.run_cli('soltar', token)[0], 0)
        self.assertFalse((self.state / 'vms' / NAME / 'arriendos' / (token + '.json')).exists())
        self.api.now += 30 * 60
        self.assertEqual(self.run_cli('vigilar')[0], 0)
        self.assertEqual(self.writes(), [('POST', '/sandboxes/' + FAKE_ID + '/stop', None)])
        self.assertFalse(any(other in route or 'vm-reservada' in route for _, route, _ in self.api.calls))
        self.assertIn('vm-reservada', {e['nombre'] for e in pool.events(self.state / 'eventos.jsonl')})

    def test_causas_de_salida_y_lista_obligatoria(self):
        self.assert_unavailable('sin_vm', 'encender', 'ajena')
        self.get_lease()
        self.assert_unavailable('sin_lugar', *self.args('otra'))
        self.assert_unavailable('sin_lugar', 'apagar', NAME)
        self.add_vm('vm-caida', fake_id('kmnp2345'), remote_state='error')
        self.assert_unavailable('api', *self.args('otra'))
        with patch.object(self, 'save'):
            self.config_file.unlink()
            self.assert_unavailable('apagado', *self.args())
        del self.config['prohibidos']
        self.api.calls.clear()
        self.assertEqual(self.run_cli(*self.args())[0], 64)
        self.assertFalse(self.api.calls)

    def test_error_de_boat_no_reanuda_y_avisa_una_vez(self):
        self.api.sandbox(FAKE_ID, state='error')
        for _ in range(2):
            self.assert_unavailable('api', *self.args())
        self.assertEqual(self.run_cli('vigilar')[0], 0)
        self.assertFalse(self.writes())
        self.assertEqual(self.notices, ['Boat informa un error en ' + NAME])

    def test_reanudacion_sin_acreditar_credenciales_apaga_y_no_concede(self):
        for fields in ({'holdsCreatorLogins': True}, {}):
            with self.subTest(fields=fields):
                self.api.sandbox(FAKE_ID, state='archived', environment=None)
                vms = pool.read_json(self.api.path)
                vms[FAKE_ID].pop('holdsCreatorLogins', None)
                pool.atomic_json(self.api.path, vms)
                self.api.resume_fields = fields
                self.api.calls.clear()
                self.assert_unavailable('api', *self.args())
                self.assertEqual(self.writes(), [
                    ('POST', '/sandboxes/' + FAKE_ID + '/resume', {'noEnv': True, 'type': 'large'}),
                    ('POST', '/sandboxes/' + FAKE_ID + '/stop', None),
                ])
        self.assertEqual(len(self.notices), 1)
        self.assertNotIn('concedido', {event['evento'] for event in self.journal()})

    def test_reintento_tras_apagado_inseguro_fallido_no_concede(self):
        self.api.sandbox(FAKE_ID, state='archived')
        self.api.resume_fields = {'holdsCreatorLogins': True}
        self.api.stop_fails = True
        self.assert_unavailable('api', *self.args())
        self.assertEqual(self.run_cli('estado')[0], 0)
        self.api.stop_fails = False
        self.assert_unavailable('api', *self.args())
        self.assertEqual(pool.read_json(self.api.path)[FAKE_ID]['state'], 'archived')
        self.assertNotIn('concedido', {event['evento'] for event in self.journal()})

    def test_vigilar_reintenta_apagado_inseguro_sin_renovar(self):
        self.config['vms'][0]['inactividad_min'] = None
        deadline = pool.stamp(NOW + 3600)
        self.api.sandbox(FAKE_ID, state='archived', archiveAfter=deadline)
        self.api.resume_fields = {}
        self.api.stop_fails = True
        self.assert_unavailable('api', *self.args())
        for fails in (True, False):
            self.api.stop_fails = fails
            self.api.calls.clear()
            self.api.now += 300
            self.assertEqual(self.run_cli('vigilar')[0], 0)
            self.assertEqual(self.writes(), [('POST', '/sandboxes/' + FAKE_ID + '/stop', None)])
            vm = pool.read_json(self.api.path)[FAKE_ID]
            self.assertEqual(vm['archiveAfter'], deadline)
            self.assertEqual(vm['state'], 'ready' if fails else 'archived')
        self.assertEqual(self.notices, ['Boat no acreditó la ausencia de credenciales en ' + NAME])

    def test_apagar_reintenta_post_con_pendiente_por_credenciales(self):
        self.api.sandbox(FAKE_ID, state='archived')
        self.api.resume_fields = {'holdsCreatorLogins': True}
        self.api.stop_fails = True
        self.assert_unavailable('api', *self.args())
        pending = self.state / 'vms' / NAME / 'operacion.json'
        self.assertTrue(pool.read_json(pending)['credenciales'])

        self.api.stop_fails = False
        self.api.now += pool.POWER_TIMEOUT + 5
        self.api.calls.clear()
        with patch.object(pool.time, 'monotonic', side_effect=[0, 2 * pool.POWER_TIMEOUT]):
            code, _, stderr = self.run_cli('apagar', NAME)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.writes(), [('POST', '/sandboxes/' + FAKE_ID + '/stop', None)])
        self.assertEqual(pool.read_json(self.api.path)[FAKE_ID]['state'], 'archived')
        self.assertFalse(pending.exists())

    def test_reporte_respeta_medidor_pausado_tras_stop_rechazado(self):
        self.assertEqual(self.run_cli('estado')[0], 0)
        self.api.now += 60
        self.api.sandbox(FAKE_ID, state='archiving', stop={'status': 'pending'})
        self.assertEqual(self.run_cli('estado')[0], 0)
        self.api.sandbox(FAKE_ID, state='ready', stop={'status': 'failing'})
        self.assertEqual(self.run_cli('vigilar')[0], 0)
        self.api.now += 600
        self.assertEqual(self.run_cli('vigilar')[0], 0)
        self.api.sandbox(FAKE_ID, state='archived', stoppedAt=pool.stamp(self.api.now))
        self.assertEqual(self.run_cli('estado')[0], 0)

        self.api.usage[(FAKE_ID, '2026-11-23T00:00:00Z')] = {
            'seconds': 120, 'dollars': 0.0012, 'running': False,
        }
        code, output, stderr = self.run_cli('reporte', '--dia', '2026-11-23')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(output.split('\t')[2:5], ['0.016667', '0.000000', '0.001200'])
        self.assertEqual(len(self.usage_calls()), 1)

    def test_registro_ajeno_o_invalido_no_recibe_mutaciones(self):
        self.api.sandbox(FAKE_ID, name='ci-ajena')
        self.assertEqual(self.run_cli('apagar', NAME)[0], 75)
        self.assertEqual(self.writes(), [])
        path = self.state / 'vms' / NAME / 'registro.json'
        record = pool.read_json(path)
        record['repo'] = 'otro/repo'
        pool.atomic_json(path, record)
        self.api.calls.clear()
        self.assertEqual(self.run_cli('encender', NAME)[0], 75)
        self.assertFalse(self.api.calls)

    def test_preparar_exclusivo_con_apta_false_y_revision_vieja(self):
        path = self.state / 'vms' / NAME / 'registro.json'
        record = pool.read_json(path)
        record['apta'] = False
        pool.atomic_json(path, record)
        self.assertEqual(self.run_cli(*self.args())[0], 75)
        lease = self.get_lease('prep', 'preparar', '--vm', NAME)
        record.update(apta=True, preparada_en=pool.stamp(NOW + 1), revision_runner='rev-nueva')
        pool.atomic_json(path, record)
        self.assertEqual(self.run_cli(*self.args())[0], 75)
        self.assertEqual(self.run_cli('soltar', lease)[0], 0)
        self.get_lease()

    def test_preparar_ajeno_o_sin_vm_es_uso_invalido(self):
        for args in (
            ('pedir', '--repo', 'otro/repo', '--corrida', 'prep', '--clase', 'preparar', '--vm', NAME),
            self.args('prep', 'preparar'),
            self.args('normal', 'contract', '--vm', NAME),
            self.args('normal', 'contract', '--prioridad', 'preparar'),
        ):
            self.assertEqual(self.run_cli(*args)[0], 64)
            self.assertFalse(self.api.calls)

    def test_turno_preparar_vivo_bloquea_hasta_que_drene_la_vm(self):
        self.config['vms'][0]['lugares'] = 2
        lease = self.get_lease()
        prepare = self.start_worker(*self.args('prep', 'preparar', '--vm', NAME, '--esperar', '7'))
        self.queue('prep')
        self.assertEqual(self.run_cli(*self.args('tren', 'contract', '--prioridad', 'tren'))[0], 75)
        self.assertEqual(self.run_cli('soltar', lease)[0], 0)
        token = json.loads(self.result(prepare, 0))['arriendo']
        self.assertEqual(self.run_cli(*self.args('suite'))[0], 75)
        self.assertEqual(self.run_cli('soltar', token)[0], 0)
        self.get_lease('siguiente')

    def test_turno_en_fila_sobrevive_a_registro_reescrito(self):
        token = self.get_lease('prep', 'preparar', '--vm', NAME)
        worker = self.start_worker(*self.args('en-fila', 'contract', '--esperar', '5'))
        self.queue('en-fila')
        path = self.state / 'vms' / NAME / 'registro.json'
        record = pool.read_json(path)
        record.update(preparada_en=pool.stamp(NOW + 1), revision_runner='rev-nueva')
        pool.atomic_json(path, record)
        self.assertEqual(self.run_cli('soltar', token)[0], 0)
        self.assertEqual(json.loads(self.result(worker, 0))['vm'], NAME)

    def test_turno_en_fila_sale_sin_vm_si_prepare_la_deja_no_apta(self):
        token = self.get_lease('prep', 'preparar', '--vm', NAME)
        worker = self.start_worker(*self.args('en-fila', 'contract', '--esperar', '3'))
        self.queue('en-fila')
        path = self.state / 'vms' / NAME / 'registro.json'
        record = pool.read_json(path)
        record.update(apta=False, cpu_robada_pct=9)
        pool.atomic_json(path, record)
        self.assertEqual(self.run_cli('soltar', token)[0], 0)
        stdout, stderr = worker.communicate(timeout=8)
        self.assertEqual(worker.returncode, 75, stderr)
        self.assertFalse(stdout)
        self.assertTrue(stderr.startswith('boat-pool: sin_vm: '), stderr)

    def test_turno_en_fila_encuentra_otra_vm_que_prepare_deja_apta(self):
        other = 'vm-recién-preparada'
        self.add_vm(other, fake_id('kmnp2345'), apt=False)
        first = self.get_lease('prep-primera', 'preparar', '--vm', NAME)
        second = self.get_lease('prep-segunda', 'preparar', '--vm', other)
        suite = self.start_worker(*self.args('suite-en-fila', 'contract', '--esperar', '5'))
        self.queue('suite-en-fila')
        for name, apt, token in ((other, True, second), (NAME, False, first)):
            path = self.state / 'vms' / name / 'registro.json'
            record = pool.read_json(path)
            record.update(apta=apt, cpu_robada_pct=1 if apt else 9)
            pool.atomic_json(path, record)
            self.assertEqual(self.run_cli('soltar', token)[0], 0)
        self.assertEqual(json.loads(self.result(suite, 0))['vm'], other)

    def test_pedir_sin_espera_tolera_lock_breve(self):
        with contextlib.ExitStack() as locks:
            locks.enter_context(pool.lock(self.state / 'vms' / NAME / 'lock'))
            with patch.object(self.api, 'sleep', lambda _: locks.close()):
                self.get_lease()

    def test_estado_texto_muestra_arriendos_vivos(self):
        token = self.get_lease('suite-visible')
        code, output, stderr = self.run_cli('estado')
        self.assertEqual(code, 0, stderr)
        self.assertIn(f'{token}\tsuite-visible\tcontract', output)

    def test_rangos_de_ttl_y_espera(self):
        for flag, invalid in (('--ttl', '299'), ('--ttl', '3601'), ('--ttl', 'nan'),
                              ('--esperar', '-1'), ('--esperar', '7201'), ('--esperar', 'inf')):
            with self.subTest(flag=flag, invalid=invalid):
                self.assertEqual(self.run_cli(*self.args('corrida', 'contract', flag, invalid))[0], 64)
        self.assertEqual(self.run_cli('latido', 'inventado', '--ttl', '3599')[0], 69)
        self.assertEqual(self.run_cli('latido', 'inventado', '--ttl', '3601')[0], 64)
        self.assertFalse(self.api.calls)
        self.get_lease('maxima', 'contract', '--esperar', '7200', '--ttl', '3600')

    def test_vigilar_apaga_solo_registradas_tras_vencimiento_e_inactividad(self):
        other = fake_id('zyxw2345')
        self.api.sandbox(other, name='ajena')
        self.get_lease('caduca', 'contract', '--ttl', '300')
        self.api.now += 300 + 30 * 60 - 1
        self.assertEqual(self.run_cli('vigilar')[0], 0)
        self.assertEqual(self.writes(), [])
        self.api.now += 1
        self.assertEqual(self.run_cli('vigilar')[0], 0)
        self.assertEqual(self.run_cli('vigilar')[0], 0)
        self.assertEqual(self.writes(), [('POST', '/sandboxes/' + FAKE_ID + '/stop', None)])
        self.assertEqual(sum(item['evento'] == 'apagada' for item in self.journal()), 1)
        self.assertTrue(all(other not in route and 'ajena' not in route for _, route, _ in self.api.calls))

    def test_vigilar_doble_apagado_concurrente(self):
        self.config['vms'][0]['inactividad_min'] = 0
        workers = [self.start_worker('vigilar') for _ in range(2)]
        for worker_process in workers:
            self.result(worker_process, 0)
        self.assertEqual(sum(item['evento'] == 'apagada' for item in self.journal()), 1)
        self.assertEqual(pool.read_json(self.api.path)[FAKE_ID]['state'], 'archived')

    def test_inactividad_nula_y_plazo_propio(self):
        self.config['vms'][0]['inactividad_min'] = None
        shortened = fake_id('kmnp2345')
        self.add_vm('vm-plazo-propio', shortened, overrides={'inactividad_min': 10})
        self.api.now += 599
        self.assertEqual(self.run_cli('vigilar')[0], 0)
        self.assertEqual(self.writes(), [])
        self.api.now += 1
        self.assertEqual(self.run_cli('vigilar')[0], 0)
        self.api.now += 31 * 60
        self.assertEqual(self.run_cli('vigilar')[0], 0)
        self.assertEqual(self.writes(), [('POST', '/sandboxes/' + shortened + '/stop', None)])
        self.assertEqual(pool.read_json(self.api.path)[FAKE_ID]['state'], 'ready')

    def test_renueva_ttl_de_vm_activa_y_detenida_si_quedan_menos_de_48_horas(self):
        archived = fake_id('efgh2345')
        limit = fake_id('nprt2345')
        self.add_vm('vm-detenida', archived, remote_state='archived')
        self.add_vm('vm-en-limite', limit)
        for identifier, remaining in ((FAKE_ID, 3600), (archived, 7200), (limit, 48 * 3600)):
            self.api.sandbox(identifier, archiveAfter=pool.stamp(self.api.now + remaining))
        self.get_lease()
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.run_cli('vigilar')[0], 0)
        self.assertEqual(self.run_cli('vigilar')[0], 0)
        self.assertEqual(self.writes(), [
            ('PATCH', '/sandboxes/' + identifier, {'ttlSeconds': pool.RENEW_FOR})
            for identifier in (FAKE_ID, archived)
        ])
        self.assertEqual(pool.read_json(self.api.path)[archived]['state'], 'archived')

    def test_avisa_si_falla_renovacion_o_no_hay_endpoint_de_retencion(self):
        self.api.sandbox(FAKE_ID, archiveAfter=pool.stamp(NOW + 3600))
        self.api.extend_fails = True
        archived = fake_id('kmnp2345')
        self.add_vm('vm-detenida', archived, remote_state='archived')
        self.api.sandbox(archived, stoppedAt=pool.stamp(NOW - 13 * 86400))
        self.assertEqual(self.run_cli('vigilar')[0], 0)
        log = (self.state / 'avisos.log').read_text()
        self.assertIn('No se pudo renovar el plazo de Boat de ' + NAME, log)
        self.assertIn('Retención de disco cerca del límite para vm-detenida', log)
        self.assertNotIn(MOCK_KEY, log)

    def test_aviso_retencion_se_mide_desde_stopped_at(self):
        self.api.sandbox(FAKE_ID, state='archived', archiveAfter=pool.stamp(NOW + 7 * 86400),
                         stoppedAt=pool.stamp(NOW - 12 * 86400 + 1))
        self.assertEqual(self.run_cli('vigilar')[0], 0)
        self.assertFalse(self.notices)
        self.api.now += 1
        self.assertEqual(self.run_cli('vigilar')[0], 0)
        self.assertEqual(self.run_cli('vigilar')[0], 0)
        self.assertEqual(self.notices, ['Retención de disco cerca del límite para ' + NAME])
        self.assertFalse(self.writes())

    def test_aviso_de_llave_correcta_y_piso_sin_duplicar(self):
        expire = pool.stamp(NOW + 3 * 86400)
        self.api.keys = [
            {'keyPrefix': 'otro', 'keyLastFour': MOCK_KEY[-4:], 'expiresAt': pool.stamp(NOW + 10000)},
            {'keyPrefix': 'boat_demo', 'keyLastFour': MOCK_KEY[-4:], 'expiresAt': expire},
        ]
        self.api.limits['wallet']['balanceDollars'] = 9
        self.assertEqual(self.run_cli('vigilar')[0], 0)
        self.assertEqual(self.run_cli('vigilar')[0], 0)
        log = (self.state / 'avisos.log').read_text()
        self.assertEqual(len(log.splitlines()), 2)
        self.assertEqual(len(self.notices), 2)
        self.assertIn(expire, log)
        self.assertIn('monedero_usd', log)
        self.assertNotIn(MOCK_KEY, log)
        self.api.keys = []
        self.assertEqual(self.run_cli('vigilar')[0], 0)
        self.assertIn('No se pudo comprobar el vencimiento', (self.state / 'avisos.log').read_text())

    def test_reporte_consulta_ventanas_utc_y_todos_los_ids_del_dia(self):
        day_before = '2026-11-22T00:00:00Z'
        today = '2026-11-23T00:00:00Z'
        start = pool.epoch(today)
        next_id = fake_id('efgh2345')
        xlarge = fake_id('nprt2345')
        self.add_vm('vm-grande', xlarge, overrides={'tipo': 'xlarge'}, created=start + 10)
        record_path = self.state / 'vms' / NAME / 'registro.json'
        record = pool.read_json(record_path)
        record['preparada_en'] = pool.stamp(start - 3600)
        pool.atomic_json(record_path, record)
        self.api.now = start
        self.api.sandbox(FAKE_ID, state='archived')
        lease = self.get_lease('misma')
        self.assertEqual(self.run_cli('soltar', lease)[0], 0)
        self.get_lease('misma')

        self.api.now = NOW
        record.update(id=next_id, preparada_en=pool.stamp(start + 3600))
        pool.atomic_json(record_path, record)
        self.api.sandbox(next_id)
        self.api.usage = {
            (FAKE_ID, day_before): {'seconds': 7200, 'dollars': 0.072},
            (FAKE_ID, today): {'seconds': 7200, 'dollars': 0.072},
            (next_id, today): {'seconds': 14400, 'dollars': 0.144},
            (xlarge, today): {'sandboxType': 'xlarge', 'billingMultiplier': 50 / 9,
                              'seconds': 20000, 'dollars': 0.20},
        }
        self.api.calls.clear()
        code, output, stderr = self.run_cli('reporte')
        self.assertEqual(code, 0, stderr)
        self.assertFalse(stderr)
        self.assertEqual([row.split('\t') for row in output.splitlines()], [
            ['2026-11-22', '0.000000', '1.000000', '0.000000', '0.072000', '0', '0', '0.000000', '0.000000'],
            ['2026-11-23', '0.000000', '3.000000', '1.000000', '0.416000', '1', '1', '0.000000', '0.000000'],
        ])
        calls = [(urlsplit(route).path.split('/')[2], parse_qs(urlsplit(route).query))
                 for route in self.usage_calls()]
        self.assertCountEqual(calls, [
            (FAKE_ID, {'since': [day_before], 'until': [today]}),
            *((identifier, {'since': [today], 'until': [pool.stamp(NOW)]})
              for identifier in (FAKE_ID, next_id, xlarge)),
        ])
        self.assertEqual([event['id'] for event in self.journal() if event['evento'] == 'registro'],
                         [FAKE_ID, next_id])

    def test_reporte_marca_pool_na_si_un_id_historico_ya_no_existe(self):
        self.get_lease()
        self.api.now += 300
        next_id = fake_id('efgh2345')
        path = self.state / 'vms' / NAME / 'registro.json'
        record = pool.read_json(path)
        record.update(id=next_id, preparada_en=pool.stamp(self.api.now))
        pool.atomic_json(path, record)
        self.api.sandbox(next_id)
        sandboxes = pool.read_json(self.api.path)
        del sandboxes[FAKE_ID]
        pool.atomic_json(self.api.path, sandboxes)
        code, output, stderr = self.run_cli('reporte', '--dia', '2026-11-23')
        self.assertEqual(code, 0)
        self.assertEqual(output.split()[1:], ['NA', 'NA', 'NA', 'NA', '0', '1', '0.000000', '0.000000'])
        self.assertIn(f'{NAME}: Boat devolvió HTTP 404', stderr)
        self.assertCountEqual([urlsplit(route).path for route in self.usage_calls()],
                              [f'/sandboxes/{identifier}/usage' for identifier in (FAKE_ID, next_id)])
        self.api.now += 86400
        code, output, stderr = self.run_cli('reporte', '--dia', '2026-11-24')
        self.assertEqual(code, 0, stderr)
        self.assertFalse(stderr)
        self.assertEqual(output.split()[1:5], ['0.000000'] * 4)

    def test_reporte_marca_pool_na_si_uso_invalido(self):
        self.api.now += 300
        for fields in ({'billingMultiplier': 0}, {'seconds': -1}, {'sandboxId': None},
                       {'dollars': '0'}, {'until': '2026-11-23T00:00:00Z'}):
            with self.subTest(fields=fields):
                self.api.usage[(FAKE_ID, '2026-11-23T00:00:00Z')] = fields
                code, output, stderr = self.run_cli('reporte', '--dia', '2026-11-23')
                self.assertEqual(code, 0)
                self.assertEqual(output.split()[1:5], ['NA'] * 4)
                self.assertIn('respuesta de uso inválida', stderr)

    def test_reporte_no_consulta_id_reservado_ni_id_de_diario_invalido(self):
        self.get_lease()
        self.api.now += 300
        self.config['prohibidos'] = [FAKE_ID]
        code, output, stderr = self.run_cli('reporte', '--dia', '2026-11-23')
        self.assertEqual(code, 0)
        self.assertEqual(output.split()[1:5], ['NA'] * 4)
        self.assertIn('ID de VM reservado', stderr)
        self.assertFalse(self.usage_calls())

        self.config['prohibidos'] = []
        journal = self.state / 'vms' / NAME / 'eventos.jsonl'
        pool.append_event(journal, {'evento': 'registro', 'en': pool.stamp(NOW - 60), 'id': '../invalido'})
        code, output, stderr = self.run_cli('reporte', '--dia', '2026-11-23')
        self.assertEqual(code, 0)
        self.assertEqual(output.split()[1:5], ['NA'] * 4)
        self.assertIn('ID inválido', stderr)
        self.assertEqual([urlsplit(route).path for route in self.usage_calls()],
                         [f'/sandboxes/{FAKE_ID}/usage'])

    def test_reporte_conserva_corridas_del_diario_anterior_sin_inventar_costo(self):
        year_month_day = '2026-11-22'
        journal = self.state / 'vms' / NAME / 'eventos.jsonl'
        pool.append_event(journal, {'evento': 'concedido', 'en': year_month_day + 'T12:00:00Z',
                                    'repo': REPO, 'corrida': 'antigua'})
        code, output, stderr = self.run_cli('reporte', '--dia', year_month_day)
        self.assertEqual(code, 0)
        self.assertEqual(output.split()[1:7], ['NA', 'NA', 'NA', 'NA', '0', '1'])
        self.assertIn('diario histórico sin ID de Boat', stderr)
        self.assertFalse(self.usage_calls())

    def test_reporte_suma_la_org_paginada_con_reservadas_y_borradas(self):
        other = fake_id('kmnp2345')
        self.api.sandbox(FAKE_ID, cost=monthly_cost(3600, 0.036))
        self.api.sandbox(other, name='ci-ejemplo', cost=monthly_cost(7200, 0.072))
        self.api.deleted = monthly_cost(3600, 0.036)
        self.api.page_size = 1
        self.get_lease()
        self.api.now += 86400
        self.api.calls.clear()
        code, output, stderr = self.run_cli('reporte')
        self.assertEqual(code, 0, stderr)
        self.assertFalse(stderr)
        self.assertEqual([row.split('\t')[7:] for row in output.splitlines()],
                         [['4.000000', '0.144000']] * 2)
        self.assertEqual([call for call in self.api.calls if '/usage?' not in call[1]], [
            ('GET', '/sandboxes?limit=200&include=cost%2CdeletedCost', None),
            ('GET', '/sandboxes?limit=200&include=cost%2CdeletedCost&cursor=1', None),
        ])

    def test_reporte_otro_mes_da_na_sin_consultar_a_boat(self):
        code, output, stderr = self.run_cli('reporte', '--dia', '2026-10-31')
        self.assertEqual(code, 0, stderr)
        self.assertTrue(output.endswith('\tNA\tNA\n'))
        self.assertIn('reporte 2026-10-31: Boat sólo informa el mes UTC en curso', stderr)
        self.assertFalse(self.api.calls)

    def test_reporte_da_na_si_boat_no_acredita_el_total_del_mes(self):
        for cost in (monthly_cost(None, 0.036), {**monthly_cost(3600, 0.036), 'since': '2026-10-01T00:00:00Z'}):
            with self.subTest(cost=cost):
                self.api.sandbox(FAKE_ID, cost=cost)
                code, output, stderr = self.run_cli('reporte', '--dia', '2026-11-23')
                self.assertEqual(code, 0, stderr)
                self.assertTrue(output.endswith('\tNA\tNA\n'))
                self.assertIn('reporte 2026-11-23: Boat no confirmó el consumo del mes UTC en curso', stderr)

    def test_reporte_ignora_archivos_en_vms(self):
        (self.state / 'vms' / '.DS_Store').touch()
        self.assertEqual(self.run_cli('reporte')[0], 0)

    def test_recupera_concesion_interrumpida_sin_evento_duplicado(self):
        path = self.state / 'vms' / NAME
        token = '0' * 32
        lease = {'arriendo': token, 'clase': 'contract', 'vence': pool.stamp(NOW + 900)}
        event = {'evento': 'concedido', 'cambio': 'incompleto', 'arriendo': token}
        pool.atomic_json(path / 'arriendo-pendiente.json', {'evento': event, 'arriendo': lease})
        pool.append_event(path / 'eventos.jsonl', event)
        code, output, stderr = self.run_cli('estado', '--json')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(output)['vms'][0]['lugares_libres'], 0)
        self.assertEqual(sum(item['evento'] == 'concedido' for item in self.journal()), 1)

    def test_grep_operaciones_prohibidas(self):
        forbidden = re.compile(
            r'extra[-_ ]starts|wallet[ /_-]+pay|auto[-_ ]top[-_ ]up|billing[ /_-]+plan',
            re.IGNORECASE,
        )
        sources = [ROOT / 'boat-pool', ROOT / 'boat_api.py', ROOT / 'pool.py']
        for source in sources:
            self.assertIsNone(forbidden.search(source.read_text()), source.name)
        client = (ROOT / 'boat_api.py').read_text()
        self.assertNotRegex(client, r"""['"]DELETE['"]|_request\(['"]POST['"],\s*['"]/sandboxes['"]""")


class Install(unittest.TestCase):
    def test_instalador_y_launch_agent(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home, stubs = base / 'home', base / 'stubs'
            config = base / 'xdg-config'
            state = base / 'xdg-state'
            log = base / 'commands.txt'
            stubs.mkdir()
            (config / 'boat-pool').mkdir(parents=True)
            (config / 'boat-pool' / 'pool.json').write_text('{}')
            for binary in ('plutil', 'launchctl'):
                command = stubs / binary
                command.write_text(
                    '#!/bin/sh\n'
                    'printf "%s %s\\n" "${0##*/}" "$*" >> "$STUB_LOG"\n'
                    'if [ "${0##*/}:${1:-}" = launchctl:print ]; then exit 1; fi\n'
                )
                command.chmod(0o700)
            result = subprocess.run(
                [str(ROOT / 'install.sh')],
                env={**os.environ, 'HOME': str(home), 'STUB_LOG': str(log),
                     'XDG_CONFIG_HOME': str(config), 'XDG_STATE_HOME': str(state),
                     'PATH': str(stubs) + ':/usr/bin:/bin'},
                text=True, capture_output=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            link = home / '.local/bin/boat-pool'
            self.assertTrue(link.is_symlink())
            self.assertTrue(os.access(link, os.X_OK))
            self.assertTrue((link.resolve().parent / 'pool.py').is_file())
            self.assertTrue((state / 'boat-pool').is_dir())
            agent = home / 'Library/LaunchAgents/local.pao.boat-pool.plist'
            parsed = plistlib.loads(agent.read_bytes())
            self.assertEqual(parsed['StartInterval'], 300)
            self.assertIn('boat-pool" vigilar', parsed['ProgramArguments'][-1])
            commands = log.read_text().splitlines()
            self.assertEqual(commands[0], 'plutil -lint ' + str(agent))
            self.assertEqual(commands[-1], f'launchctl bootstrap gui/{os.getuid()} {agent}')


if __name__ == '__main__':
    if sys.argv[1:2] == ['worker']:
        sys.exit(worker())
    unittest.main()
