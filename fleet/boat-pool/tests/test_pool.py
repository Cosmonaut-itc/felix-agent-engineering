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
                                    'createdAt': pool.stamp(self.now),
                                    'archiveAfter': pool.stamp(self.now + 9000),
                                    'ttlSeconds': 9000,
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
            result = {'sandboxes': list(vms.values())[start:end],
                      'pageInfo': {'hasMore': has_more, 'nextCursor': str(end) if has_more else None}}
            if self.deleted is not None:
                result['deletedCost'] = self.deleted
            return result
        _, group, identifier, *operation = url.path.split('/')
        if group != 'sandboxes':
            raise AssertionError('ruta inesperada')
        if identifier not in vms:
            raise HTTPError(path, 404, 'sandbox no encontrado', None, None)
        if method == 'GET' and operation == ['usage']:
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
            vm.update(ttlSeconds=body['ttlSeconds'],
                      archiveAfter=pool.stamp(self.now + body['ttlSeconds']))
        elif method == 'POST' and operation == ['resume']:
            vm.update(state='provisioning', stoppedAt=None, **self.resume_fields)
            if 'ttlSeconds' in body:
                vm.update(ttlSeconds=body['ttlSeconds'],
                          archiveAfter=pool.stamp(self.now + body['ttlSeconds']))
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
        self.api.sandbox(identifier, name=name, state=remote_state, createdAt=pool.stamp(created))

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
        return self.get_grant(*args)['arriendo']

    def get_grant(self, *args):
        code, output, stderr = self.run_cli(*self.args(*args))
        self.assertEqual(code, 0, stderr)
        return json.loads(output)

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

    def test_q1_un_turno_no_bloquea_una_vm_que_ya_descarto(self):
        other = 'vm-ocupada'
        self.add_vm(other, fake_id('efgh2345'))
        self.config['vms'].reverse()
        busy_lease = self.get_lease('ocupante')
        self.config['vms'].reverse()
        transport = self.api.transport
        timed_out = False
        outcomes = []
        watch_writes = []

        def intermittent_transport(method, route, headers, body, timeout):
            nonlocal timed_out
            if (not timed_out and method == 'GET'
                    and parse_qs(urlsplit(route).query).get('name') == [NAME]):
                timed_out = True
                raise TimeoutError('primer GET de identidad agotó el tiempo')
            return transport(method, route, headers, body, timeout)

        def allow_second_turn(_):
            self.assertFalse(outcomes, 'el primer turno debe obtener la VM liberada')
            queued, = (self.state / 'espera').glob('*.json')
            self.assertEqual(pool.read_json(queued)['corrida'], 'tren-en-espera')
            self.assertEqual(pool.read_json(self.api.path)[FAKE_ID]['state'], 'ready')
            self.assertFalse(list((self.state / 'vms' / NAME / 'arriendos').glob('*.json')))
            self.api.sandbox(FAKE_ID, archiveAfter=pool.stamp(NOW + 3600))
            self.api.calls.clear()
            self.assertEqual(self.run_cli('vigilar')[0], 0)
            watch_writes.extend(self.writes())
            code, output, stderr = self.run_cli(
                *self.args('suite-segunda', 'contract', '--prioridad', 'suite', '--esperar', '0'))
            outcomes.append((code, json.loads(output)['vm'] if output else None, stderr.strip()))
            self.assertEqual(self.run_cli('soltar', busy_lease)[0], 0)

        with patch.object(self.api, 'transport', side_effect=intermittent_transport), \
                patch.object(self.api, 'sleep', side_effect=allow_second_turn):
            code, output, stderr = self.run_cli(
                *self.args('tren-en-espera', 'contract', '--prioridad', 'tren', '--esperar', '1800'))
        self.assertTrue(timed_out)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(output)['vm'], other)
        with self.subTest(comprobacion='concesión'):
            self.assertEqual(outcomes, [(0, NAME, '')])
        with self.subTest(comprobacion='renovación'):
            self.assertEqual(watch_writes, [])

    def test_q2_pid_reutilizado_no_mantiene_viva_la_vm(self):
        queue = pool.private_dir(self.state / 'espera')
        since = NOW - 86400
        expired = {
            'turno': 'f' * 32, 'repo': REPO, 'corrida': 'proceso-terminado',
            'clase': 'contract', 'vm': None, 'prioridad': 'tren',
            'desde': pool.stamp(since), 'vence': pool.stamp(since + 1800 + pool.LOCK_WAIT),
            'orden': int(since) * 10**9, 'host': pool.socket.gethostname(), 'pid': os.getpid(),
        }
        legacy = dict(expired, turno='e' * 32, corrida='version-anterior')
        del legacy['vence']
        for turn in (expired, legacy):
            pool.atomic_json(queue / (turn['turno'] + '.json'), turn)
        initial_deadline = pool.read_json(self.api.path)[FAKE_ID]['archiveAfter']

        for cycle in range(1, 33):
            self.api.now = NOW + cycle * 300
            self.assertEqual(self.run_cli('vigilar')[0], 0)

        sandbox = pool.read_json(self.api.path)[FAKE_ID]
        with self.subTest(comprobacion='fila'):
            self.assertEqual(list(queue.glob('*.json')), [])
        with self.subTest(comprobacion='apagado'):
            self.assertEqual(sandbox['state'], 'archived')
            self.assertEqual(sandbox['archiveAfter'], initial_deadline)
        with self.subTest(comprobacion='escrituras'):
            self.assertEqual(self.writes(), [('POST', f'/sandboxes/{FAKE_ID}/stop', None)])

    def test_limite_por_clase_y_latido(self):
        self.config['vms'][0]['lugares'] = 2
        lease = self.get_lease('primera', 'e2e')
        self.assertEqual(self.run_cli(*self.args('segunda', 'e2e'))[0], 75)
        self.get_lease('tercera', 'contract')
        self.api.now += 10
        self.assertEqual(self.run_cli('latido', lease, '--ttl', '3600')[0], 0)
        data = pool.read_json(self.state / 'vms' / NAME / 'arriendos' / (lease + '.json'))
        self.assertEqual(pool.epoch(data['vence']), self.api.now + 3600)

    def test_pedir_reparte_dos_corridas_entre_vms(self):
        self.config['vms'][0]['lugares'] = 2
        other = 'vm-otra'
        self.add_vm(other, fake_id('efgh2345'), places=2)
        self.assertEqual(self.get_grant('primera')['vm'], NAME)
        self.assertEqual(self.get_grant('segunda')['vm'], other)

    def test_pedir_limita_candidatas_y_ordena_por_carga_dentro_de_la_lista(self):
        self.config['vms'][0]['lugares'] = 3
        other = 'vm-otra'
        self.get_lease('ocupante')
        self.add_vm(other, fake_id('efgh2345'), places=3)

        self.assertEqual(self.get_grant('limitada', 'contract', '--candidatas', NAME)['vm'], NAME)
        self.assertEqual(self.get_grant('ambas', 'contract', '--candidatas', f'{NAME},{other}')['vm'], other)

    def test_pedir_rechaza_candidatas_invalidas(self):
        other = 'vm-ajena'
        self.add_vm(other, fake_id('efgh2345'), overrides={'repo': 'otro/proyecto'})
        for names, options, message in (
            (f'{NAME},vm-desconocida', ('contract',), 'VM fuera del pool'),
            (f'{NAME},{other}', ('contract',), 'pertenece a otro repositorio'),
            (NAME, ('preparar', '--vm', NAME), 'no se admite con la clase preparar'),
            (f'{NAME},', ('contract',), 'argumentos del pool inválidos'),
            (f'{NAME},vm\ninválida', ('contract',), 'argumentos del pool inválidos'),
        ):
            with self.subTest(names=names, options=options):
                code, output, stderr = self.run_cli(*self.args('suite', *options, '--candidatas', names))
                self.assertEqual(code, 64, stderr)
                self.assertIn(message, stderr)
                self.assertFalse(output)
        self.assertFalse(self.api.calls)

    def test_pedir_espera_y_vence_si_la_unica_candidata_esta_llena(self):
        self.get_lease('ocupante')
        self.add_vm('vm-otra', fake_id('efgh2345'))
        self.api.calls.clear()

        def advance_clock(seconds):
            self.api.now += seconds

        with patch.object(pool.time, 'monotonic', side_effect=lambda: self.api.now - NOW), \
                patch.object(self.api, 'sleep', side_effect=advance_clock):
            self.assert_unavailable(
                'sin_lugar', *self.args('limitada', 'contract', '--candidatas', NAME, '--esperar', '0.25'))
        self.assertEqual(self.api.now, NOW + 0.25)
        self.assertFalse(self.api.calls)

    def test_turno_con_candidatas_no_bloquea_una_vm_fuera_de_su_lista(self):
        token = self.get_lease('ocupante')
        other = 'vm-otra'
        self.add_vm(other, fake_id('efgh2345'))
        restricted = self.start_worker(
            *self.args('tren', 'contract', '--candidatas', NAME, '--prioridad', 'tren', '--esperar', '8'))
        self.queue('tren')

        self.assertEqual(self.get_grant('suite', 'contract', '--prioridad', 'suite')['vm'], other)
        self.assertIsNone(restricted.poll())
        self.assertEqual(self.run_cli('soltar', token)[0], 0)
        self.assertEqual(json.loads(self.result(restricted, 0))['vm'], NAME)

    def test_pedir_no_prueba_vm_fuera_de_la_lista_si_rechaza_el_arranque(self):
        self.add_vm('vm-otra', fake_id('efgh2345'))
        self.api.sandbox(FAKE_ID, state='archived')
        self.api.limits['starts']['day']['remaining'] = 0

        self.assert_unavailable('puerta', *self.args('limitada', 'contract', '--candidatas', NAME))
        self.assertFalse(self.journal('vm-otra'))

    def test_contract_elige_otra_vm_si_una_e2e_ocupa_un_lugar(self):
        self.config['vms'][0].update(lugares=2, peso_clase={'contract': 2})
        other = 'vm-otra'
        self.add_vm(other, fake_id('efgh2345'), places=2,
                    overrides={'peso_clase': {'contract': 2}})
        self.assertEqual(self.get_grant('fragmento', 'e2e')['vm'], NAME)
        code, _, stderr = self.run_cli('apagar', other)
        self.assertEqual(code, 0, stderr)

        self.assertEqual(self.get_grant('contrato')['vm'], other)

    def test_ejemplo_declara_el_peso_en_las_dos_vms_de_jale(self):
        example_path = Path(pool.__file__).with_name('pool.example.json')
        first, second = json.loads(example_path.read_text())['vms'][:2]
        for declaration in (first, second):
            declaration.pop('nombre')
            declaration.pop('repo')
        self.config['vms'][0].update(first)
        other = 'vm-otra'
        self.add_vm(other, fake_id('efgh2345'), overrides=second)
        code, _, stderr = self.run_cli('apagar', NAME)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.get_grant('contrato')['vm'], other)

        self.assertEqual(self.get_grant('fragmento', 'e2e')['vm'], NAME)

    def test_e2e_agota_la_espera_con_una_contract_en_cada_vm(self):
        self.config['vms'][0].update(lugares=2, peso_clase={'contract': 2})
        other = 'vm-otra'
        self.add_vm(other, fake_id('efgh2345'), places=2,
                    overrides={'peso_clase': {'contract': 2}})
        self.assertEqual(self.get_grant('primera')['vm'], NAME)
        self.assertEqual(self.get_grant('segunda')['vm'], other)

        def advance_clock(seconds):
            self.api.now += seconds

        with patch.object(pool.time, 'monotonic', side_effect=lambda: self.api.now - NOW), \
                patch.object(self.api, 'sleep', side_effect=advance_clock):
            self.assert_unavailable('sin_lugar', *self.args('fragmento', 'e2e', '--esperar', '0.25'))
        self.assertEqual(self.api.now, NOW + 0.25)

    def test_estado_descuenta_el_peso_de_la_contract(self):
        self.config['vms'][0].update(lugares=2, peso_clase={'contract': 2})
        self.assertEqual(self.get_grant('contrato')['vm'], NAME)

        code, output, stderr = self.run_cli('estado', '--json')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(output)['vms'][0]['lugares_libres'], 0)

    def test_sin_peso_clase_caben_dos_contracts_en_la_misma_vm(self):
        declaration = self.config['vms'][0]
        declaration.update(lugares=2, peso_clase={'contract': 2})
        self.assertEqual(self.get_grant('primera')['vm'], NAME)
        self.assert_unavailable('sin_lugar', *self.args('segunda'))

        del declaration['peso_clase']
        self.assertEqual(self.get_grant('segunda')['vm'], NAME)

    def test_rechaza_peso_clase_invalido(self):
        declaration = self.config['vms'][0]
        declaration['lugares'] = 2
        for weights in ({'contract': 0}, {'contract': 3}, {'contract': '2'},
                        {'contract': 2.0}, {'contract': True}, None):
            with self.subTest(peso_clase=weights):
                declaration['peso_clase'] = weights
                code, _, stderr = self.run_cli(*self.args())
                self.assertEqual(code, 64, stderr)

    def test_pedir_ordena_por_el_peso_de_los_arriendos(self):
        self.config['vms'][0].update(lugares=3, peso_clase={'contract': 2})
        self.assertEqual(self.get_grant('contrato')['vm'], NAME)
        other = 'vm-otra'
        self.add_vm(other, fake_id('efgh2345'), places=3)
        self.assertEqual(self.get_grant('fragmento', 'e2e')['vm'], other)

        self.assertEqual(self.get_grant('integracion', 'integracion')['vm'], other)

    def test_pedir_elige_la_primera_si_la_segunda_esta_llena(self):
        first = self.config['vms'].pop()
        first['lugares'] = 2
        self.add_vm('vm-otra', fake_id('efgh2345'), places=2)
        self.get_lease('ocupante-uno')
        self.get_lease('ocupante-dos')
        self.config['vms'].insert(0, first)
        self.assertEqual(self.get_grant('siguiente')['vm'], NAME)

    def test_pedir_desempata_por_orden_del_pool(self):
        self.config['vms'][0]['lugares'] = 2
        self.add_vm('vm-antes-alfabeticamente', fake_id('efgh2345'), places=2)
        self.assertEqual(self.get_grant()['vm'], NAME)

    def test_pedir_respeta_limite_de_clase_en_la_vm_menos_cargada(self):
        self.config['vms'][0]['lugares'] = 2
        self.get_lease('contrato')
        other = 'vm-otra'
        self.add_vm(other, fake_id('efgh2345'), places=3)
        self.config['vms'].reverse()
        self.assertEqual(self.get_grant('e2e-activa', 'e2e')['vm'], other)
        self.assertEqual(self.get_grant('e2e-nueva', 'e2e')['vm'], NAME)

    def test_pedir_compara_la_proporcion_de_lugares_ocupados(self):
        self.config['vms'][0]['lugares'] = 2
        self.get_lease('ocupante')
        other = 'vm-grande'
        self.add_vm(other, fake_id('efgh2345'), places=5)
        for i in range(3):
            self.assertEqual(self.get_grant(f'corrida-{i}')['vm'], other)
        self.assertEqual(self.get_grant('siguiente')['vm'], NAME)

    def test_pedir_prefiere_la_vm_encendida_con_cupo(self):
        other = 'vm-encendida'
        self.add_vm(other, fake_id('efgh2345'), places=2)
        self.api.sandbox(FAKE_ID, state='archived')
        marker = self.state / 'vms' / NAME / 'operacion.json'
        pool.atomic_json(marker, {
            'id': 'apagado-local', 'sandbox_id': FAKE_ID, 'accion': 'stop', 'desde': pool.stamp(NOW),
        })
        self.assertEqual(self.get_grant('primera')['vm'], other)
        self.assertEqual(self.get_grant('segunda')['vm'], other)
        self.assertEqual(self.writes(), [])
        self.assertTrue(marker.exists())
        self.assertEqual(self.get_grant('tercera')['vm'], NAME)
        self.assertFalse(marker.exists())
        self.assertEqual(self.writes(), [
            ('POST', '/sandboxes/' + FAKE_ID + '/resume',
             {'noEnv': True, 'type': 'large', 'ttlSeconds': 9000}),
        ])

    def test_pedir_concede_sin_sondear_otra_vm_lenta(self):
        other = fake_id('efgh2345')
        self.add_vm('vm-otra', other)
        transport = self.api.transport

        def slow_transport(method, route, headers, body, timeout):
            if method == 'GET' and route == '/sandboxes/' + other:
                self.api.now += timeout
                raise TimeoutError('agotó su plazo la consulta de la otra VM')
            return transport(method, route, headers, body, timeout)

        ask_order = pool.Pool.ask_order
        queries = []

        def record_order(instance, candidates, kind):
            before = mocked_transport.call_count
            ordered = ask_order(instance, candidates, kind)
            queries.append(mocked_transport.call_count - before)
            return ordered

        with patch.object(self.api, 'transport', side_effect=slow_transport) as mocked_transport, \
                patch.object(pool.Pool, 'ask_order', record_order), \
                patch.object(pool.time, 'monotonic', side_effect=lambda: self.api.now - NOW):
            for wait in (0, 1800):
                with self.subTest(esperar=wait):
                    queries.clear()
                    before = self.api.now
                    code, output, stderr = self.run_cli(*self.args(
                        'solicitud', 'e2e', '--esperar', str(wait)))
                    vm = json.loads(output)['vm'] if output else None
                    self.assertEqual((code, vm, self.api.now - before), (0, NAME, 0), stderr)
                    self.assertEqual(queries, [0])
                    self.assertEqual(self.run_cli('soltar', json.loads(output)['arriendo'])[0], 0)

    def test_pedir_prefiere_una_vm_lista_a_un_apagado_local_pendiente(self):
        self.config['vms'][0]['lugares'] = 2
        self.get_lease('contrato', 'contract')
        other = fake_id('efgh2345')
        self.add_vm('vm-otra', other, created=NOW - 1800)
        transport = self.api.transport
        delayed_stop = False

        def accept_stop(method, route, headers, body, timeout):
            nonlocal delayed_stop
            if method == 'POST' and route == '/sandboxes/' + other + '/stop':
                delayed_stop = True
                return {'ok': True}
            return transport(method, route, headers, body, timeout)

        sleep = self.api.sleep

        def finish_stop_on_poll(seconds):
            nonlocal delayed_stop
            if delayed_stop:
                delayed_stop = False
                self.api.sandbox(other, state='archived')
            sleep(seconds)

        with patch.object(self.api, 'transport', side_effect=accept_stop), \
                patch.object(self.api, 'sleep', side_effect=finish_stop_on_poll):
            self.assertEqual(self.run_cli('vigilar')[0], 0)
            self.assertTrue(delayed_stop)
            local = self.state / 'vms' / 'vm-otra' / 'operacion.json'
            self.assertEqual(pool.read_json(local)['accion'], 'stop')
            remote = pool.read_json(self.api.path)[other]
            self.assertEqual(remote['state'], 'ready')
            self.assertNotIn('stop', remote)
            self.api.calls.clear()
            grant = self.get_grant('fragmento', 'e2e')

        resumes = sum(method == 'POST' and route.endswith('/resume')
                      for method, route, _ in self.api.calls)
        self.assertEqual((grant['vm'], resumes), (NAME, 0))

    def test_pedir_pospone_la_vm_con_un_arriendo_sin_clase(self):
        self.config['vms'][0]['lugares'] = 2
        self.get_lease('ocupante')
        other = 'vm-otra'
        self.add_vm(other, fake_id('efgh2345'), places=3)
        leases = pool.private_dir(self.state / 'vms' / other / 'arriendos')
        pool.atomic_json(leases / 'dañado.json', {'vence': pool.stamp(NOW + 900)})

        self.assertEqual(self.get_grant('siguiente')['vm'], NAME)

    def test_pedir_prefiere_la_vm_lista_despues_de_un_apagado_confirmado(self):
        self.config['vms'][0]['lugares'] = 2
        self.get_lease('contrato', 'contract')
        other = 'vm-otra'
        other_id = fake_id('efgh2345')
        self.add_vm(other, other_id, places=2, created=NOW - 1800)
        self.assertEqual(self.run_cli('vigilar')[0], 0)

        directory = self.state / 'vms' / other
        self.assertEqual(pool.read_json(self.api.path)[other_id]['state'], 'archived')
        self.assertFalse((directory / 'operacion.json').exists())
        self.assertTrue(any(event['evento'] == 'apagada' and event.get('id') == other_id
                            for event in pool.events(directory / 'eventos.jsonl')))

        self.api.calls.clear()
        grant = self.get_grant('fragmento', 'e2e', '--esperar', '1800')
        resumes = sum(method == 'POST' and route.endswith('/resume')
                      for method, route, _ in self.api.calls)
        self.assertEqual((grant['vm'], resumes), (NAME, 0))

    def test_pedir_vuelve_a_considerar_la_vm_reanudada_con_encender(self):
        self.config['vms'][0]['lugares'] = 2
        self.get_lease('contrato', 'contract')
        other = 'vm-otra'
        other_id = fake_id('efgh2345')
        self.add_vm(other, other_id, places=2)
        self.assertEqual(self.run_cli('apagar', other)[0], 0)
        self.assertEqual(self.run_cli('encender', other)[0], 0)
        power = [event['evento'] for event in self.journal(other) if event.get('id') == other_id]
        self.assertEqual(power, ['registro', 'apagada', 'arranque'])

        self.assertEqual(self.get_grant('fragmento', 'e2e')['vm'], other)

    def test_pedir_busca_el_ultimo_evento_completo_del_id_vigente(self):
        self.config['vms'][0]['lugares'] = 2
        self.get_lease('contrato', 'contract')
        other = 'vm-otra'
        other_id = fake_id('efgh2345')
        self.add_vm(other, other_id, places=2)
        self.assertEqual(self.run_cli('apagar', other)[0], 0)
        journal = self.state / 'vms' / other / 'eventos.jsonl'
        pool.append_event(journal, {'evento': 'encendida', 'id': fake_id('prev2345'), 'en': pool.stamp(NOW)})
        pool.append_event(journal, {'evento': 'rechazo', 'id': other_id, 'en': pool.stamp(NOW),
                                    'motivo': 'á' * 8192})
        with journal.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'evento': 'encendida', 'id': other_id, 'en': pool.stamp(NOW)}))

        self.assertEqual(self.get_grant('fragmento', 'e2e')['vm'], NAME)

    def test_pedir_pospone_la_vm_con_un_diario_ilegible(self):
        self.config['vms'][0]['lugares'] = 2
        self.get_lease('contrato', 'contract')
        other = 'vm-otra'
        self.add_vm(other, fake_id('efgh2345'), places=2)
        journal = self.state / 'vms' / other / 'eventos.jsonl'
        journal.write_text('no es JSON\n', encoding='utf-8')

        self.assertEqual(self.get_grant('fragmento', 'e2e')['vm'], NAME)

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
            ('POST', '/sandboxes/' + FAKE_ID + '/resume',
             {'noEnv': True, 'type': 'large', 'ttlSeconds': 9000}),
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
        self.api.calls.clear()
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
                    ('POST', '/sandboxes/' + FAKE_ID + '/resume',
                     {'noEnv': True, 'type': 'large', 'ttlSeconds': 9000}),
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

    def test_ronda5_operacion_de_otro_id_se_descarta(self):
        self.config['vms'][0]['inactividad_min'] = None
        self.api.sandbox(FAKE_ID, state='archived')
        self.api.resume_fields = {'holdsCreatorLogins': True}
        self.api.stop_fails = True
        self.assert_unavailable('api', *self.args())
        directory = self.state / 'vms' / NAME
        pending = directory / 'operacion.json'
        operation = pool.read_json(pending)
        replacement = fake_id('kmnp2345')
        self.api.now += 300
        self.add_vm(NAME, replacement, created=self.api.now)
        self.config['vms'].pop(0)
        sandboxes = pool.read_json(self.api.path)
        del sandboxes[FAKE_ID]
        pool.atomic_json(self.api.path, sandboxes)
        self.api.stop_fails = False

        for index, command in enumerate((('vigilar',), ('encender', NAME), self.args(),
                                         ('apagar', NAME)), start=1):
            with self.subTest(command=command):
                if command == ('apagar', NAME):
                    self.api.sandbox(replacement, state='archived')
                else:
                    self.api.sandbox(replacement, state='ready')
                pool.atomic_json(pending, operation)
                self.api.calls.clear()
                code, output, stderr = self.run_cli(*command)
                self.assertEqual(code, 0, stderr)
                self.assertFalse(self.writes())
                self.assertFalse(pending.exists())
                discards = [item for item in self.journal() if item['evento'] == 'operacion_descartada']
                self.assertEqual(len(discards), index)
                self.assertEqual(discards[-1]['operacion'], operation['id'])
                if command[0] == 'pedir':
                    self.assertEqual(self.run_cli('soltar', json.loads(output)['arriendo'])[0], 0)
        self.assertEqual(operation['sandbox_id'], FAKE_ID)

    def test_ronda6_pendiente_sin_identidad_o_ilegible_bloquea_y_avisa_una_vez(self):
        self.config['vms'][0].update(lugares=2, inactividad_min=None)
        token = self.get_lease('en-curso')
        directory = self.state / 'vms' / NAME
        lease_path = directory / 'arriendos' / (token + '.json')
        original_lease = pool.read_json(lease_path)
        pending = directory / 'operacion.json'
        self.api.now += 60
        self.api.sandbox(FAKE_ID, archiveAfter=pool.stamp(self.api.now + 3600))
        legacy = json.dumps({'id': 'f' * 32, 'accion': 'stop', 'credenciales': True,
                             'desde': pool.stamp(self.api.now)})
        for raw in (legacy, '{'):
            with self.subTest(pending=raw):
                pending.write_text(raw)
                self.api.sandbox(FAKE_ID, state='ready')
                self.assertEqual(self.run_cli('vigilar')[0], 0)
                self.assert_unavailable('api', *self.args('nueva'))
                self.assertEqual(self.run_cli('latido', token)[0], 69)
                self.assertEqual(pool.read_json(lease_path), original_lease)
                self.api.sandbox(FAKE_ID, state='archived')
                self.assert_unavailable('api', 'encender', NAME)
                self.assertEqual(pending.read_text(), raw)
                self.assertFalse(self.writes())
        self.assertNotIn('operacion_descartada', {event['evento'] for event in self.journal()})
        self.assertEqual(self.notices, ['Operación pendiente sin identificar en ' + NAME])
        self.assertEqual(self.run_cli('soltar', token)[0], 0)

    def test_ronda6_apagar_registra_una_vez_el_stop_pendiente(self):
        self.api.stop_fails = True
        self.assert_unavailable('api', 'apagar', NAME)
        pending = self.state / 'vms' / NAME / 'operacion.json'
        operation = pool.read_json(pending)
        self.api.sandbox(FAKE_ID, state='archived')
        for _ in range(2):
            code, _, stderr = self.run_cli('apagar', NAME)
            self.assertEqual(code, 0, stderr)
        self.assertFalse(pending.exists())
        self.assertEqual([event.get('operacion') for event in self.journal()
                          if event['evento'] == 'apagada'], [operation['id']])

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

    def test_turno_en_fila_conserva_candidatas_al_releer_registros(self):
        self.get_lease('ocupante')
        self.add_vm('vm-otra', fake_id('efgh2345'))
        path = self.state / 'vms' / NAME / 'registro.json'

        def mark_unfit(seconds):
            self.api.now += seconds
            pool.atomic_json(path, {**pool.read_json(path), 'apta': False})

        with patch.object(pool.time, 'monotonic', side_effect=lambda: self.api.now - NOW), \
                patch.object(self.api, 'sleep', side_effect=mark_unfit):
            self.assert_unavailable(
                'sin_vm', *self.args('limitada', 'contract', '--candidatas', NAME, '--esperar', '1'))
        self.assertFalse(self.journal('vm-otra'))

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

    def test_ronda5_encendida_legacy_no_reinicia_inactividad(self):
        self.api.now += 1800
        journal = self.state / 'vms' / NAME / 'eventos.jsonl'
        pool.append_event(journal, {'evento': 'encendida', 'en': pool.stamp(self.api.now)})
        self.assertEqual(self.run_cli('vigilar')[0], 0)
        self.assertEqual(self.writes(), [('POST', '/sandboxes/' + FAKE_ID + '/stop', None)])

    def test_inactividad_nula_y_plazo_propio(self):
        self.config['vms'][0]['inactividad_min'] = None
        self.api.sandbox(FAKE_ID, archiveAfter=None)
        shortened = fake_id('kmnp2345')
        self.add_vm('vm-plazo-propio', shortened, overrides={'inactividad_min': 10})
        self.api.sandbox(shortened, ttlSeconds=7800, archiveAfter=pool.stamp(NOW + 7800))
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
        for vm in self.config['vms']:
            vm['inactividad_min'] = None
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
        self.get_lease()
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

    def test_ronda10_auto_stop_acotado_al_conceder_y_reanudar(self):
        self.api.sandbox(FAKE_ID, archiveAfter=pool.stamp(NOW + 3600))
        lease = self.get_lease()
        self.assertEqual(self.run_cli('soltar', lease)[0], 0)
        self.api.now += 60
        self.assertEqual(self.run_cli('vigilar')[0], 0)
        path = '/sandboxes/' + FAKE_ID
        self.assertEqual(self.writes(), [('PATCH', path, {'ttlSeconds': 9000})])
        self.assertEqual(pool.read_json(self.api.path)[FAKE_ID]['archiveAfter'], pool.stamp(NOW + 9000))

        self.api.sandbox(FAKE_ID, state='archived')
        self.api.calls.clear()
        self.get_lease()
        self.assertEqual(self.writes(), [
            ('POST', path + '/resume', {'noEnv': True, 'type': 'large', 'ttlSeconds': 9000}),
        ])

    def test_ronda10_latido_reusa_auto_stop_y_tolera_patch_fallido(self):
        lease = self.get_lease('suite', 'contract', '--ttl', '3600')
        self.api.calls.clear()
        self.api.now += 1800
        self.assertEqual(self.run_cli('latido', lease, '--ttl', '3600')[0], 0)
        self.assertFalse(self.api.calls)

        path = '/sandboxes/' + FAKE_ID
        self.api.now += 1
        self.api.extend_fails = True
        self.assertEqual(self.run_cli('latido', lease)[0], 0)
        self.assertEqual(self.api.calls, [('PATCH', path, {'ttlSeconds': 9000})])
        self.api.extend_fails = False
        self.api.calls.clear()
        self.assertEqual(self.run_cli('latido', lease)[0], 0)
        self.assertEqual(self.run_cli('latido', lease)[0], 0)
        self.assertEqual(self.api.calls, [('PATCH', path, {'ttlSeconds': 9000})])

    def test_ronda10_vigilar_renueva_auto_stop_mientras_acepta_un_turno(self):
        self.api.sandbox(FAKE_ID, archiveAfter=pool.stamp(NOW + 604800))
        turn = {'repo': REPO, 'clase': 'contract'}
        with patch.object(pool.Pool, 'turns', return_value=[turn]):
            self.assertEqual(self.run_cli('vigilar')[0], 0)
        path = '/sandboxes/' + FAKE_ID
        self.assertEqual(self.writes(), [('PATCH', path, {'ttlSeconds': 9000})])

        self.api.sandbox(FAKE_ID, archiveAfter=pool.stamp(NOW + 3600))
        self.api.calls.clear()
        self.assertEqual(self.run_cli('vigilar')[0], 0)
        self.assertFalse(self.writes())

    def test_ronda11_vigilar_comprueba_auto_stop_de_vm_ociosa(self):
        stop = ('POST', '/sandboxes/' + FAKE_ID + '/stop', None)
        for ttl, remaining, should_stop in (
            (604800, 9061, True),
            (604800, 8500, False),
            (9000, 9061, True),
            (9000, 3000, False),
        ):
            with self.subTest(ttl=ttl, remaining=remaining):
                deadline = pool.stamp(NOW + remaining)
                self.api.sandbox(FAKE_ID, state='ready', ttlSeconds=ttl, archiveAfter=deadline)
                self.api.calls.clear()
                code, _, stderr = self.run_cli('vigilar')
                self.assertEqual(code, 0, stderr)
                self.assertEqual(self.writes(), [stop] if should_stop else [])
                vm = pool.read_json(self.api.path)[FAKE_ID]
                self.assertEqual(vm['state'], 'archived' if should_stop else 'ready')
                self.assertEqual(vm['ttlSeconds'], ttl)
                self.assertEqual(vm['archiveAfter'], deadline)

    def test_ronda13_vigilar_apaga_en_el_primer_ciclo_con_siete_dias_restantes(self):
        deadline = pool.stamp(NOW + 604800)
        self.api.sandbox(FAKE_ID, ttlSeconds=604800, archiveAfter=deadline)
        code, _, stderr = self.run_cli('vigilar')
        self.assertEqual(code, 0, stderr)
        vm = pool.read_json(self.api.path)[FAKE_ID]
        self.assertEqual(vm['state'], 'archived')
        self.assertEqual(vm['archiveAfter'], deadline)
        self.assertEqual(self.writes(), [('POST', '/sandboxes/' + FAKE_ID + '/stop', None)])
        self.assertEqual(self.notices, ['Plazo de Boat sin acotar para ' + NAME])

    def test_ronda13_vigilar_reintenta_apagado_sin_alargar_plazo_omitido(self):
        deadline = pool.stamp(NOW + 604800)
        self.api.sandbox(FAKE_ID, ttlSeconds=604800, archiveAfter=deadline)
        transport = self.api.transport
        path = '/sandboxes/' + FAKE_ID
        stop_path = path + '/stop'

        def omit_deadline_and_timeout_stop(method, route, headers, body, timeout):
            if route == stop_path and method == 'POST':
                self.api.calls.append((method, route, body))
                raise TimeoutError('POST no llegó a Boat')
            result = transport(method, route, headers, body, timeout)
            if route == path and method == 'GET':
                del result['sandbox']['archiveAfter']
            return result

        deadlines = []
        with patch.object(self.api, 'transport', side_effect=omit_deadline_and_timeout_stop):
            for cycle in range(1, 33):
                self.api.now = NOW + cycle * 300
                self.assertEqual(self.run_cli('vigilar')[0], 0)
                deadlines.append(pool.read_json(self.api.path)[FAKE_ID]['archiveAfter'])
            writes = self.writes()
            self.api.now += 1
            self.assertEqual(self.run_cli('vigilar')[0], 0)
        warning = 'Plazo de Boat sin acotar para ' + NAME
        with self.subTest(comprobacion='plazos'):
            self.assertEqual((min(deadlines), max(deadlines)), (deadline, deadline))
        with self.subTest(comprobacion='peticiones'):
            self.assertEqual(sum(method == 'PATCH' for method, _, _ in writes), 0)
            self.assertEqual(writes, [('POST', stop_path, None)] * 32)
            self.assertEqual(self.writes(), writes)
        self.assertEqual(self.notices, [warning])
        self.assertEqual(pool.read_json(self.state / 'avisos.json'), {'ttl:' + NAME: warning})

    def test_ronda13_vigilar_no_hace_patch_ocioso_en_30_ciclos(self):
        no_deadline = fake_id('nprt2345')
        patch_timeout = fake_id('efgh2345')
        self.add_vm('vm-sin-plazo', no_deadline)
        self.add_vm('vm-timeout-patch', patch_timeout)
        deadlines = {identifier: pool.stamp(NOW + remaining) for identifier, remaining in (
            (FAKE_ID, 8500), (no_deadline, 8500), (patch_timeout, 604800),
        )}
        for identifier, deadline in deadlines.items():
            self.api.sandbox(identifier, ttlSeconds=604800, archiveAfter=deadline)
        self.api.stop_fails = True
        transport = self.api.transport

        def partial_response(method, path, headers, body, timeout):
            if method == 'PATCH' and path == '/sandboxes/' + patch_timeout:
                self.api.calls.append((method, path, body))
                raise TimeoutError('PATCH no llegó a Boat')
            result = transport(method, path, headers, body, timeout)
            if method == 'GET' and path == '/sandboxes/' + no_deadline:
                del result['sandbox']['archiveAfter']
            if method in ('GET', 'PATCH') and path == '/sandboxes/' + FAKE_ID:
                del result['sandbox']['ttlSeconds']
            return result

        with patch.object(self.api, 'transport', side_effect=partial_response):
            for cycle in range(30):
                self.api.now = NOW + cycle * 300
                self.assertEqual(self.run_cli('vigilar')[0], 0)
        sandboxes = pool.read_json(self.api.path)
        for identifier, deadline in deadlines.items():
            with self.subTest(vm=identifier):
                path = '/sandboxes/' + identifier
                writes = [call for call in self.writes() if call[1] in (path, path + '/stop')]
                stop_count = 24 if identifier == FAKE_ID else 30
                self.assertEqual(sum(method == 'PATCH' for method, _, _ in writes), 0)
                self.assertEqual(writes, [('POST', path + '/stop', None)] * stop_count)
                self.assertEqual(sandboxes[identifier]['archiveAfter'], deadline)

    def test_ronda13_vigilar_comprueba_tolerancia_y_plazo_invalido(self):
        over_limit = fake_id('nprt2345')
        malformed = fake_id('efgh2345')
        self.add_vm('vm-limite-61', over_limit)
        self.add_vm('vm-plazo-invalido', malformed)
        self.api.sandbox(FAKE_ID, archiveAfter=pool.stamp(NOW + 9060))
        self.api.sandbox(over_limit, archiveAfter=pool.stamp(NOW + 9061))
        self.api.sandbox(malformed, archiveAfter='fecha inválida')
        self.assertEqual(self.run_cli('vigilar')[0], 0)
        self.assertCountEqual(self.writes(), [
            ('POST', '/sandboxes/' + identifier + '/stop', None)
            for identifier in (over_limit, malformed)
        ])
        self.assertCountEqual(self.notices, [
            'Plazo de Boat sin acotar para vm-limite-61',
            'Plazo de Boat sin acotar para vm-plazo-invalido',
        ])

    def test_ronda13_vigilar_mide_tolerancia_al_recibir_el_get(self):
        deadline = pool.stamp(NOW + 9061)
        self.api.sandbox(FAKE_ID, archiveAfter=deadline)
        transport = self.api.transport

        def delayed_get(method, path, headers, body, timeout):
            if method == 'GET' and path == '/sandboxes/' + FAKE_ID:
                self.api.now = NOW + 1
            return transport(method, path, headers, body, timeout)

        with patch.object(self.api, 'transport', side_effect=delayed_get):
            code, _, stderr = self.run_cli('vigilar')
        self.assertEqual(code, 0, stderr)
        self.assertFalse(self.writes())
        self.assertFalse(self.notices)
        self.assertEqual(pool.read_json(self.api.path)[FAKE_ID]['archiveAfter'], deadline)

    def test_r14_control_vm_recien_preparada_con_inactividad_de_20_minutos(self):
        self.config['vms'][0]['inactividad_min'] = 20
        target = f'/sandboxes/{FAKE_ID}'
        self.assertEqual(self.run_cli('vigilar')[0], 0)
        self.assertEqual(self.writes(), [('POST', target + '/stop', None)])
        self.assertEqual(self.notices, ['Plazo de Boat sin acotar para ' + NAME])
        self.api.calls.clear()
        token = self.get_lease()
        self.assertEqual(self.writes(), [('POST', target + '/resume', {
            'noEnv': True, 'type': 'large', 'ttlSeconds': 8400,
        })])
        self.api.calls.clear()
        self.api.now += 600
        self.assertEqual(self.run_cli('vigilar')[0], 0)
        self.assertFalse(self.writes())
        self.assertEqual(self.run_cli('latido', token)[0], 0)
        self.assertEqual(self.run_cli('soltar', token)[0], 0)
        self.assertEqual(self.run_cli('vigilar')[0], 0)
        self.assertFalse(self.writes())
        self.api.now += 1200
        self.assertEqual(self.run_cli('vigilar')[0], 0)
        self.assertEqual(self.writes(), [('POST', target + '/stop', None)])

    def assert_r14_apagado_tras_inactividad(self):
        self.api.calls.clear()
        stopped = [('POST', f'/sandboxes/{FAKE_ID}/stop', None)]
        for elapsed in range(300, 1801, 300):
            self.api.now += 300
            deadline = pool.read_json(self.api.path)[FAKE_ID]['archiveAfter']
            self.assertLessEqual(pool.epoch(deadline) - self.api.now, pool.SUITE_MAX + 1800 + 60)
            self.assertEqual(self.run_cli('vigilar')[0], 0)
            self.assertEqual(self.writes(), [] if elapsed < 1800 else stopped)
        self.assertEqual(self.notices, [])

    def test_r14_control_ocupada_a_ociosa(self):
        token = self.get_lease('suite', 'contract', '--ttl', '900')
        for _ in range(24):
            self.api.now += 300
            self.assertEqual(self.run_cli('latido', token, '--ttl', '900')[0], 0)
            self.assertEqual(self.run_cli('vigilar')[0], 0)
            deadline = pool.read_json(self.api.path)[FAKE_ID]['archiveAfter']
            self.assertGreaterEqual(pool.epoch(deadline) - self.api.now, pool.SUITE_MAX - 300)
        self.assertEqual(self.run_cli('soltar', token)[0], 0)
        self.assert_r14_apagado_tras_inactividad()

    def test_r14_control_encender_a_ociosa(self):
        self.api.sandbox(FAKE_ID, state='archived', archiveAfter=None)
        self.assertEqual(self.run_cli('encender', NAME)[0], 0)
        self.assert_r14_apagado_tras_inactividad()

    def test_ronda11_pedir_corrige_ttl_heredado(self):
        self.api.sandbox(FAKE_ID, ttlSeconds=604800, archiveAfter=pool.stamp(NOW + 8500))
        self.get_lease()
        self.assertEqual(self.writes(), [('PATCH', '/sandboxes/' + FAKE_ID, {'ttlSeconds': 9000})])

    def test_ronda11_latido_corrige_ttl_omitido(self):
        lease = self.get_lease()
        cache = self.state / 'vms' / NAME / 'auto-stop.json'
        pool.atomic_json(cache, {'id': FAKE_ID, 'archiveAfter': NOW + 8500})
        sandboxes = pool.read_json(self.api.path)
        del sandboxes[FAKE_ID]['ttlSeconds']
        pool.atomic_json(self.api.path, sandboxes)
        self.api.calls.clear()
        code, _, stderr = self.run_cli('latido', lease)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.writes(), [('PATCH', '/sandboxes/' + FAKE_ID, {'ttlSeconds': 9000})])
        self.assertEqual(pool.read_json(cache)['ttlSeconds'], 9000)

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

    def test_corte_diario_de_uso_y_contadores_tras_concesion_concurrente(self):
        self.api.now += 60
        cutoff = self.api.now
        self.api.sandbox(FAKE_ID, state='archived')
        transport = self.api.transport
        triggered = False

        def ask_while_reading_inventory(method, route, headers, body, timeout):
            nonlocal triggered
            if 'include=cost' in route and not triggered:
                triggered = True
                self.api.now += 2
                self.get_lease('concedida-despues-del-corte')
            return transport(method, route, headers, body, timeout)

        with patch.object(self.api, 'transport', side_effect=ask_while_reading_inventory):
            code, output, stderr = self.run_cli('reporte', '--dia', '2026-11-23')
        self.assertEqual(code, 0, stderr)
        route, = self.usage_calls()
        until, = parse_qs(urlsplit(route).query)['until']
        self.assertEqual(pool.epoch(until), cutoff)
        self.assertEqual(output.split()[5:7], ['0', '0'])

    def test_reporte_consulta_ventanas_utc_y_todos_los_ids_del_dia(self):
        day_before = '2026-11-22T00:00:00Z'
        today = '2026-11-23T00:00:00Z'
        start = pool.epoch(today)
        next_id = fake_id('efgh2345')
        xlarge = fake_id('nprt2345')
        record_path = self.state / 'vms' / NAME / 'registro.json'
        record = pool.read_json(record_path)
        record['preparada_en'] = pool.stamp(start - 3600)
        pool.atomic_json(record_path, record)
        self.api.now = start
        self.api.sandbox(FAKE_ID, state='archived', createdAt=record['preparada_en'])
        lease = self.get_lease('misma')
        self.assertEqual(self.run_cli('soltar', lease)[0], 0)
        self.get_lease('misma')

        self.api.now = NOW
        self.add_vm('vm-grande', xlarge, overrides={'tipo': 'xlarge'}, created=start + 10)
        record.update(id=next_id, preparada_en=pool.stamp(start + 3600))
        pool.atomic_json(record_path, record)
        self.api.sandbox(next_id, createdAt=record['preparada_en'])
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

    def test_ronda6_dia_posterior_sin_registro_de_retiro_da_na(self):
        self.get_lease()
        record = self.state / 'vms' / NAME / 'registro.json'
        record.unlink()
        self.api.now += 86400
        self.api.calls.clear()
        code, output, stderr = self.run_cli('reporte', '--dia', '2026-11-24')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(output.split()[1:5], ['NA'] * 4)
        self.assertIn('falta registro de retiro', stderr)
        self.assertFalse(self.usage_calls())

    def retire(self, *, delete_sandbox=True):
        self.assertEqual(self.run_cli('apagar', NAME)[0], 0)
        record = self.state / 'vms' / NAME / 'registro.json'
        record.rename(record.with_name(f'registro.retirado-{int(self.api.now * 10**9)}.json'))
        if delete_sandbox:
            sandboxes = pool.read_json(self.api.path)
            del sandboxes[FAKE_ID]
            pool.atomic_json(self.api.path, sandboxes)
        self.api.calls.clear()

    def test_ronda5_dia_posterior_al_retiro_sin_consultas(self):
        self.api.now += 2
        self.retire()
        self.api.now += 86400
        code, output, stderr = self.run_cli('reporte', '--dia', '2026-11-24')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(output.split()[1:7], ['0.000000'] * 4 + ['0', '0'])
        self.assertFalse(stderr)
        self.assertFalse(any(FAKE_ID in route for _, route, _ in self.api.calls))

    def test_ronda6_retiro_despues_de_medianoche_consulta_hasta_el_dia_del_retiro(self):
        day = '2026-11-24'
        midnight = int(pool.epoch(day + 'T00:00:00Z'))
        self.api.now = midnight - 300
        self.assertEqual(self.run_cli('soltar', self.get_lease())[0], 0)
        self.api.now = midnight + 300
        self.api.sandbox(FAKE_ID, state='archived', stoppedAt=pool.stamp(midnight + 120))
        directory = self.state / 'vms' / NAME
        (directory / 'registro.json').rename(
            directory / f'registro.retirado-{self.api.now * 10**9}.json')
        self.config['vms'].clear()
        self.api.usage[FAKE_ID, day + 'T00:00:00Z'] = {'seconds': 240, 'dollars': 0.0024}
        for deleted in (False, True):
            if deleted:
                pool.atomic_json(self.api.path, {})
            self.api.calls.clear()
            code, output, stderr = self.run_cli('reporte', '--dia', day)
            self.assertEqual(code, 0, stderr)
            self.assertEqual(output.split()[4], 'NA' if deleted else '0.002400')
            self.assertEqual(len(self.usage_calls()), 1)
            if deleted:
                self.assertIn('HTTP 404', stderr)
            else:
                self.assertFalse(stderr)
        self.api.now += 86400
        self.api.calls.clear()
        code, output, stderr = self.run_cli('reporte', '--dia', '2026-11-25')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(output.split()[1:5], ['0.000000'] * 4)
        self.assertFalse(stderr)
        self.assertFalse(self.usage_calls())

    def test_ronda7_cierre_invalido_marca_na_desde_preparada(self):
        directory = self.state / 'vms' / NAME
        record = directory / 'registro.json'
        previous_day = '2026-11-22'
        other_id = fake_id('efgh2345')
        self.add_vm('vm-anterior', other_id, created=NOW - 86400)
        self.api.usage[other_id, previous_day + 'T00:00:00Z'] = {'seconds': 240, 'dollars': 0.0024}
        self.api.now = NOW + 86400
        for reason, suffix in (
                ('segundos', int(NOW)),
                ('milisegundos', int(NOW) * 1000),
                ('no entero', 'fecha-invalida'),
                ('antes de preparada_en', (int(NOW) - 2) * 10**9),
                ('posterior a ahora', (int(self.api.now) + 2) * 10**9)):
            with self.subTest(reason=reason):
                marker = record.with_name(f'registro.retirado-{suffix}.json')
                record.rename(marker)
                try:
                    self.api.calls.clear()
                    code, output, stderr = self.run_cli('reporte')
                    self.assertEqual(code, 0, stderr)
                    rows = [row.split()[:5] for row in output.splitlines()]
                    self.assertEqual(rows, [
                        [previous_day, '0.000000', '0.033333', '0.000000', '0.002400'],
                        ['2026-11-23'] + ['NA'] * 4,
                        ['2026-11-24'] + ['NA'] * 4,
                    ])
                    for day in ('2026-11-23', '2026-11-24'):
                        self.assertIn(f'reporte {day}: {NAME}: cierre de retiro inválido', stderr)
                    self.assertFalse(any(FAKE_ID in route for route in self.usage_calls()))
                    code, output, stderr = self.run_cli('reporte', '--dia', '2026-11-24')
                    self.assertEqual(code, 0, stderr)
                    self.assertEqual(output.split()[:5], rows[-1])
                finally:
                    marker.rename(record)

    def test_cierre_con_medio_segundo_de_margen_da_na(self):
        record = self.state / 'vms' / NAME / 'registro.json'
        pool.atomic_json(record, {**pool.read_json(record), 'preparada_en': NOW})
        record.rename(record.with_name(f'registro.retirado-{int(NOW) * 10**9 + 500_000_000}.json'))
        self.api.now += 60
        code, output, stderr = self.run_cli('reporte', '--dia', '2026-11-23')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(output.split()[1:5], ['NA'] * 4)
        self.assertIn('cierre de retiro inválido', stderr)
        self.assertFalse(self.usage_calls())

    def test_cierre_con_dos_segundos_de_margen_es_valido(self):
        record = self.state / 'vms' / NAME / 'registro.json'
        pool.atomic_json(record, {**pool.read_json(record), 'preparada_en': NOW})
        record.rename(record.with_name(f'registro.retirado-{(int(NOW) + 2) * 10**9}.json'))
        self.api.now += 86400 + 60
        self.api.usage[FAKE_ID, '2026-11-23T00:00:00Z'] = {'seconds': 600, 'dollars': 0.006}
        code, output, stderr = self.run_cli('reporte')
        self.assertEqual(code, 0, stderr)
        self.assertEqual([row.split()[4] for row in output.splitlines()], ['0.006000', '0.000000'])
        self.assertFalse(stderr)
        self.assertEqual(len(self.usage_calls()), 1)

    def test_cierre_con_margen_insuficiente_para_created_at_da_na(self):
        directory = self.state / 'vms' / NAME
        record = directory / 'registro.json'
        pool.atomic_json(record, {**pool.read_json(record), 'preparada_en': NOW - 2})
        pool.append_event(directory / 'eventos.jsonl', {
            'evento': 'registro', 'en': pool.stamp(NOW), 'id': FAKE_ID,
            'creada_en': pool.stamp(NOW),
        })
        record.rename(record.with_name(f'registro.retirado-{int(NOW) * 10**9 + 500_000_000}.json'))
        self.api.now += 60
        code, output, stderr = self.run_cli('reporte', '--dia', '2026-11-23')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(output.split()[1:5], ['NA'] * 4)
        self.assertIn('cierre de retiro inválido', stderr)
        self.assertFalse(self.usage_calls())

    def test_cierre_cerca_de_medianoche_conserva_consulta_del_dia_siguiente(self):
        day = '2026-11-24'
        midnight = int(pool.epoch(day + 'T00:00:00Z'))
        record = self.state / 'vms' / NAME / 'registro.json'
        record.rename(record.with_name(f'registro.retirado-{midnight * 10**9 + 1}.json'))
        self.api.now = midnight + 60
        self.api.usage[FAKE_ID, day + 'T00:00:00Z'] = {'seconds': 1, 'dollars': 0.001}
        code, output, stderr = self.run_cli('reporte', '--dia', day)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(output.split()[4], '0.001000')
        self.assertEqual(len(self.usage_calls()), 1)
        self.assertFalse(stderr)

    def test_ronda8_diario_conserva_fraccion_de_created_at(self):
        created = '2026-11-23T11:59:59.999999831Z'
        self.api.sandbox(FAKE_ID, createdAt=created)
        self.api.now = NOW + 3600
        code, _, stderr = self.run_cli('reporte', '--dia', '2026-11-23')
        self.assertEqual(code, 0, stderr)
        self.assertFalse(stderr)
        event, = self.journal()
        self.assertEqual(event['creada_en'], created)

    def test_reporte_valida_retiro_con_el_corte_inicial(self):
        record = self.state / 'vms' / NAME / 'registro.json'
        retired_at = int(NOW) + 1
        marker = record.with_name(f'registro.retirado-{retired_at * 10**9}.json')
        record.rename(marker)
        read_json = pool.read_json

        def read_after_retirement(path):
            if path == marker:
                self.api.now = retired_at
            return read_json(path)

        with patch.object(pool, 'read_json', side_effect=read_after_retirement):
            code, output, stderr = self.run_cli('reporte', '--dia', '2026-11-23')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(output.split()[1:5], ['NA'] * 4)
        self.assertIn('cierre de retiro inválido', stderr)
        self.assertFalse(self.usage_calls())

        code, output, stderr = self.run_cli('reporte', '--dia', '2026-11-23')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(output.split()[1:5], ['0.000000'] * 4)
        self.assertFalse(stderr)
        self.assertEqual(len(self.usage_calls()), 1)

    def test_ronda7_actividad_sin_id_entre_periodos_marca_na(self):
        self.api.now = NOW + 60
        self.retire(delete_sandbox=False)
        next_id = fake_id('efgh2345')
        self.api.now = NOW + 2 * 86400 + 60
        self.config['vms'].clear()
        self.add_vm(identifier=next_id, created=NOW + 2 * 86400)
        journal = self.state / 'vms' / NAME / 'eventos.jsonl'
        original = journal.read_text()
        for kind, counts in (('concedido', ['0', '1']), ('arranque', ['1', '0'])):
            with self.subTest(kind=kind):
                journal.write_text(original)
                event = {'evento': kind, 'en': pool.stamp(NOW + 86400), 'repo': REPO, 'corrida': 'legada'}
                pool.append_event(journal, event)
                self.api.calls.clear()
                code, output, stderr = self.run_cli('reporte', '--dia', '2026-11-24')
                self.assertEqual(code, 0, stderr)
                self.assertEqual(output.split()[1:7], ['NA'] * 4 + counts)
                self.assertIn(f'{NAME}: actividad sin ID fuera de todo periodo', stderr)
                self.assertFalse(self.usage_calls())

                pool.append_event(journal, {**event, 'en': pool.stamp(NOW + 2 * 86400)})
                code, output, stderr = self.run_cli('reporte', '--dia', '2026-11-25')
                self.assertEqual(code, 0, stderr)
                self.assertEqual(output.split()[1:7], ['0.000000'] * 4 + counts)
                self.assertFalse(stderr)

    def test_ronda5_vm_quitada_del_pool_conserva_uso_y_corridas(self):
        self.api.sandbox(FAKE_ID, state='archived')
        self.assertEqual(self.run_cli('soltar', self.get_lease())[0], 0)
        self.api.now += 60
        self.retire(delete_sandbox=False)
        self.config['vms'].clear()
        # Un registro retirado también documenta el ID de un diario anterior.
        journal = self.state / 'vms' / NAME / 'eventos.jsonl'
        legacy = [item for item in self.journal() if item['evento'] != 'registro']
        journal.write_text(''.join(json.dumps({key: val for key, val in item.items() if key != 'id'})
                                   + '\n' for item in legacy))
        self.api.usage[FAKE_ID, '2026-11-23T00:00:00Z'] = {'seconds': 120, 'dollars': 0.0012}
        for expected in (['0.000000', '0.016667', '0.000000', '0.001200'], ['NA'] * 4):
            code, output, stderr = self.run_cli('reporte', '--dia', '2026-11-23')
            self.assertEqual(code, 0, stderr)
            self.assertEqual(output.split()[1:7], expected + ['1', '1'])
            if expected[0] == 'NA':
                self.assertIn('HTTP 404', stderr)
            else:
                self.assertFalse(stderr)
                pool.atomic_json(self.api.path, {})
            self.assertIn(FAKE_ID, self.usage_calls()[-1])

    def test_ronda5_creacion_antes_de_medianoche_amplia_reporte(self):
        created = '2026-11-22T23:50:00Z'
        self.api.sandbox(FAKE_ID, createdAt=created)
        self.api.usage[FAKE_ID, '2026-11-22T00:00:00Z'] = {'seconds': 1200, 'dollars': 0.012}
        self.assertEqual(self.run_cli('estado')[0], 0)
        self.api.calls.clear()
        code, output, stderr = self.run_cli('reporte')
        self.assertEqual(code, 0, stderr)
        self.assertFalse(stderr)
        self.assertEqual(output.splitlines()[0].split()[:5],
                         ['2026-11-22', '0.000000', '0.166667', '0.000000', '0.012000'])
        event, = [item for item in self.journal() if item['evento'] == 'registro']
        self.assertEqual(event['creada_en'], created)
        self.assertFalse(any(route == '/sandboxes/' + FAKE_ID for _, route, _ in self.api.calls))

    def test_creacion_cerca_de_medianoche_da_na_en_ambos_dias(self):
        for created in ('2026-11-22T23:59:59.6Z', '2026-11-23T00:00:00.4Z'):
            with self.subTest(created=created):
                journal = self.state / 'vms' / NAME / 'eventos.jsonl'
                journal.write_text('')
                self.api.sandbox(FAKE_ID, createdAt=created)
                code, output, stderr = self.run_cli('reporte')
                self.assertEqual(code, 0, stderr)
                days = ('2026-11-22', '2026-11-23')
                self.assertEqual([row.split()[:5] for row in output.splitlines()],
                                 [[day] + ['NA'] * 4 for day in days])
                for day in days:
                    self.assertIn(f'reporte {day}: {NAME}: creación cerca de medianoche UTC', stderr)
                self.assertFalse(self.usage_calls())

    def test_creacion_dos_segundos_antes_de_medianoche_se_factura(self):
        self.api.sandbox(FAKE_ID, createdAt='2026-11-22T23:59:58Z')
        self.api.usage[FAKE_ID, '2026-11-22T00:00:00Z'] = {'seconds': 2, 'dollars': 0.002}
        code, output, stderr = self.run_cli('reporte')
        self.assertEqual(code, 0, stderr)
        self.assertEqual([row.split()[4] for row in output.splitlines()], ['0.002000', '0.000000'])
        self.assertFalse(stderr)

    def test_ronda5_creacion_no_acreditada_se_reintenta_en_diario_viejo(self):
        journal = self.state / 'vms' / NAME / 'eventos.jsonl'
        pool.append_event(journal, {'evento': 'registro', 'en': pool.stamp(NOW), 'id': FAKE_ID})
        self.api.sandbox(FAKE_ID, createdAt=None)
        for day in ('2026-11-22', '2026-11-23'):
            code, output, stderr = self.run_cli('reporte', '--dia', day)
            self.assertEqual(code, 0, stderr)
            self.assertEqual(output.split()[1:5], ['NA'] * 4)
            self.assertIn('creación sin acreditar', stderr)
        self.assertFalse(self.usage_calls())
        self.api.sandbox(FAKE_ID, createdAt='2026-11-22T23:50:00Z')
        self.api.usage[FAKE_ID, '2026-11-22T00:00:00Z'] = {'seconds': 1200, 'dollars': 0.012}
        code, output, stderr = self.run_cli('reporte', '--dia', '2026-11-22')
        self.assertEqual(code, 0, stderr)
        self.assertFalse(stderr)
        self.assertEqual(output.split()[4], '0.012000')

    def test_ronda5_reporte_documenta_atribucion_al_tamano_actual(self):
        self.api.usage[FAKE_ID, '2026-11-23T00:00:00Z'] = {
            'sandboxType': 'xlarge', 'billingMultiplier': 50 / 9, 'seconds': 27200, 'dollars': 0.272,
        }
        code, output, stderr = self.run_cli('reporte', '--dia', '2026-11-23')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(output.split()[2:5], ['0.000000', '1.360000', '0.272000'])
        self.assertIn('se atribuyen al tamaño actual', (ROOT / 'README.md').read_text())

    def test_ronda6_reporte_exige_identidad_confirmada_despues_de_preparar(self):
        self.config['vms'].clear()
        self.api.sandbox(FAKE_ID, name='vm-ajena')
        self.api.now += 86400 + 300
        day = '2026-11-24'
        self.api.usage[FAKE_ID, day + 'T00:00:00Z'] = {'seconds': 7200, 'dollars': 0.072}
        for confirmed in (False, False, True):
            with self.subTest(confirmed=confirmed):
                if confirmed:
                    self.api.sandbox(FAKE_ID, name=NAME)
                self.api.calls.clear()
                code, output, stderr = self.run_cli('reporte', '--dia', day)
                self.assertEqual(code, 0, stderr)
                self.assertIn(('GET', '/sandboxes/' + FAKE_ID, None), self.api.calls)
                if confirmed:
                    self.assertEqual(output.split()[4], '0.072000')
                    self.assertFalse(stderr)
                    self.assertEqual(len(self.usage_calls()), 1)
                else:
                    self.assertEqual(output.split()[1:5], ['NA'] * 4)
                    self.assertIn('creación sin acreditar', stderr)
                    self.assertFalse(self.usage_calls())

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

    def test_reporte_otro_mes_da_na_para_la_org(self):
        code, output, stderr = self.run_cli('reporte', '--dia', '2026-10-31')
        self.assertEqual(code, 0, stderr)
        self.assertTrue(output.endswith('\tNA\tNA\n'))
        self.assertIn('reporte 2026-10-31: Boat sólo informa el mes UTC en curso', stderr)
        self.assertEqual(self.api.calls, [
            ('GET', '/sandboxes?limit=200&include=cost%2CdeletedCost', None),
            ('GET', '/sandboxes/' + FAKE_ID, None),
        ])

    def test_reporte_da_na_si_boat_no_acredita_el_total_del_mes(self):
        for cost in (monthly_cost(None, 0.036), {**monthly_cost(3600, 0.036), 'since': '2026-10-01T00:00:00Z'}):
            with self.subTest(cost=cost):
                self.api.sandbox(FAKE_ID, cost=cost)
                code, output, stderr = self.run_cli('reporte', '--dia', '2026-11-23')
                self.assertEqual(code, 0, stderr)
                self.assertTrue(output.endswith('\tNA\tNA\n'))
                self.assertIn('reporte 2026-11-23: Boat no confirmó el consumo del mes UTC en curso', stderr)

    def test_vm_declarada_sin_registro_da_na_desde_created_at(self):
        name = 'vm-preparandose'
        self.config['vms'].append({**self.config['vms'][0], 'nombre': name})
        self.api.sandbox(fake_id('efgh2345'), name=name, createdAt='2026-11-22T12:00:00Z',
                         cost=monthly_cost(7200, 0.072))
        code, output, stderr = self.run_cli('reporte')
        self.assertEqual(code, 0, stderr)
        days = ('2026-11-22', '2026-11-23')
        self.assertEqual([row.split()[:5] for row in output.splitlines()],
                         [[day] + ['NA'] * 4 for day in days])
        for day in days:
            self.assertIn(f'reporte {day}: {name}: VM de Boat sin registro', stderr)
        self.assertTrue(all(row.split()[8] == '0.072000' for row in output.splitlines()))
        self.assertEqual([route for _, route, _ in self.api.calls if route.startswith('/sandboxes?')],
                         ['/sandboxes?limit=200&include=cost%2CdeletedCost'])

        code, output, stderr = self.run_cli('reporte', '--dia', '2026-11-21')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(output.split()[1:5], ['0.000000'] * 4)
        self.assertFalse(stderr)

    def test_vm_declarada_ausente_en_boat_conserva_costo_del_pool(self):
        self.config['vms'].append({**self.config['vms'][0], 'nombre': 'vm-ausente'})
        self.api.now += 3600
        self.api.usage[FAKE_ID, '2026-11-23T00:00:00Z'] = {'seconds': 600, 'dollars': 0.006}
        code, output, stderr = self.run_cli('reporte')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(output.split()[4], '0.006000')
        self.assertFalse(stderr)

    def test_vm_declarada_sin_registro_y_sin_inventario_da_na(self):
        name = 'vm-preparandose'
        self.config['vms'].append({**self.config['vms'][0], 'nombre': name})
        for day in ('2026-11-23', '2026-10-31'):
            with self.subTest(day=day):
                with patch.object(Boat, 'org_inventory', side_effect=BoatError('inventario inaccesible')) as inventory:
                    code, output, stderr = self.run_cli('reporte', '--dia', day)
                inventory.assert_called_once_with()
                self.assertEqual(code, 0, stderr)
                self.assertEqual(output.split()[1:5], ['NA'] * 4)
                self.assertIn('inventario de Boat sin acreditar: inventario inaccesible', stderr)

    def test_ronda10_nombre_reutilizado_se_concilia_por_id(self):
        self.api.now += 60
        self.retire()
        replacement = fake_id('efgh2345')
        reserved = fake_id('kmnp2345')
        self.config['prohibidos'] = [reserved]
        self.api.now += 2 * 86400
        self.api.sandbox(replacement, createdAt='2026-11-25T08:00:00Z')
        self.api.sandbox(reserved, createdAt='2026-11-01T08:00:00Z')
        self.api.sandbox(fake_id('nprt2345'), name='vm-ajena', createdAt='2026-11-01T08:00:00Z')
        for created, expected in (('2026-11-25T08:00:00Z', ['0.000000', 'NA']),
                                  ('fecha inválida', ['NA', 'NA'])):
            with self.subTest(created=created):
                self.api.sandbox(replacement, createdAt=created)
                for day, cost in zip(('2026-11-24', '2026-11-25'), expected):
                    code, output, stderr = self.run_cli('reporte', '--dia', day)
                    self.assertEqual(code, 0, stderr)
                    self.assertEqual(output.split()[1:5], [cost] * 4)
                    if cost == 'NA':
                        self.assertIn(f'{NAME}: VM de Boat sin registro ({replacement})', stderr)
                    else:
                        self.assertFalse(stderr)

    def test_ronda10_inventario_sin_deleted_cost_da_na_en_todos_los_dias(self):
        self.config['vms'].append({**self.config['vms'][0], 'nombre': 'vm-fuera-del-alcance'})
        self.api.now += 86400
        self.api.deleted = None
        code, output, stderr = self.run_cli('reporte')
        self.assertEqual(code, 0, stderr)
        rows = [row.split() for row in output.splitlines()]
        self.assertEqual([row[0] for row in rows], ['2026-11-23', '2026-11-24'])
        for row in rows:
            self.assertEqual(row[1:5] + row[7:], ['NA'] * 6)
            self.assertIn(f'reporte {row[0]}: inventario de Boat sin acreditar', stderr)

    def test_ronda10_reintenta_si_hay_retiro_entre_paginas(self):
        other = fake_id('efgh2345')
        self.api.sandbox(FAKE_ID, cost=monthly_cost(3600, 0.036))
        vms = pool.read_json(self.api.path)
        for i in range(199):
            identifier = fake_id(f'{i:08d}')
            vms[identifier] = {'id': identifier, 'name': 'vm-ajena', 'cost': monthly_cost(0, 0)}
        vms[other] = {'id': other, 'name': 'vm-otra', 'cost': monthly_cost(7200, 0.072)}
        transport = self.api.transport
        self.api.now += 60

        for persistent in (False, True):
            with self.subTest(persistent=persistent):
                pool.atomic_json(self.api.path, vms)
                self.api.deleted = monthly_cost(0, 0)
                pages = []

                def retire_between_pages(method, path, headers, body, timeout):
                    if 'include=' in path:
                        later_page = 'cursor=' in path
                        pages.append(later_page)
                        if later_page:
                            if persistent:
                                self.api.deleted = monthly_cost(7200 * len(pages), 0.072 * len(pages))
                            else:
                                self.api.deleted = monthly_cost(7200, 0.072)
                                remaining = pool.read_json(self.api.path)
                                del remaining[other]
                                pool.atomic_json(self.api.path, remaining)
                    return transport(method, path, headers, body, timeout)

                with patch.object(self.api, 'transport', side_effect=retire_between_pages):
                    code, output, stderr = self.run_cli('reporte', '--dia', '2026-11-23')
                self.assertEqual(code, 0, stderr)
                row = output.split()
                if persistent:
                    self.assertEqual(pages, [False, True, False, True])
                    self.assertEqual(row[1:5] + row[7:], ['NA'] * 6)
                    self.assertIn('inventario de Boat sin acreditar: deletedCost cambió', stderr)
                else:
                    self.assertEqual(pages, [False, True, False])
                    self.assertEqual(row[7:], ['3.000000', '0.108000'])
                    self.assertFalse(stderr)

    def test_ronda10_fecha_iso_basica_y_fracciones_con_zona(self):
        for value, offset in (
            ('2026-11-23T12:00:00.4Z', 0.4),
            ('2026-11-23T12:00:00.123456789Z', 0.123456),
            ('20261123T120000Z', 0),
            ('20261123T143000.123456789+0230', 0.123456),
            ('2026-11-23T09:30:00-02:30', 0),
            ('2026-11-23T13:00:00+01', 0),
        ):
            with self.subTest(value=value):
                self.assertEqual(pool.epoch(value), NOW + offset)
        with self.assertRaises(ValueError):
            pool.epoch('2026-11-23T12:00:00.4')

    def test_ronda10_marcador_sin_registro_previo_acredita_el_id_del_retiro(self):
        folder = self.state / 'vms' / NAME
        (folder / 'registro.json').unlink()
        retired = int(NOW + 60)
        pool.atomic_json(folder / f'registro.retirado-{retired * 10**9}.json', {
            'id': FAKE_ID, 'nombre': NAME, 'tipo': 'large',
            'preparada_en': '2026-11-22T08:00:00Z', 'apta': False,
        })
        pool.atomic_json(self.api.path, {})
        self.api.now += 86400
        code, output, stderr = self.run_cli('reporte')
        self.assertEqual(code, 0, stderr)
        rows = [row.split()[:5] for row in output.splitlines()]
        self.assertEqual(rows, [
            ['2026-11-22'] + ['NA'] * 4,
            ['2026-11-23'] + ['NA'] * 4,
            ['2026-11-24'] + ['0.000000'] * 4,
        ])
        for day in ('2026-11-22', '2026-11-23'):
            self.assertIn(f'reporte {day}: {NAME}: creación sin acreditar: Boat devolvió HTTP 404', stderr)

        self.api.sandbox(FAKE_ID, createdAt='2026-11-22T08:00:00Z')
        code, output, stderr = self.run_cli('reporte', '--dia', '2026-11-24')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(output.split()[1:5], ['0.000000'] * 4)
        self.assertFalse(stderr)

        replacement = fake_id('efgh2345')
        self.api.sandbox(replacement, createdAt='2026-11-24T08:00:00Z')
        code, output, stderr = self.run_cli('reporte', '--dia', '2026-11-24')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(output.split()[1:5], ['NA'] * 4)
        self.assertIn(f'VM de Boat sin registro ({replacement})', stderr)

    def test_reporte_ignora_archivos_en_vms(self):
        (self.state / 'vms' / '.DS_Store').touch()
        self.assertEqual(self.run_cli('reporte')[0], 0)

    def test_ronda11_reporte_concilia_id_al_vencer_reserva(self):
        replacement = fake_id('efgh2345')
        self.api.sandbox(replacement, createdAt=pool.stamp(NOW - 3600), cost=monthly_cost(7200, 0.072))
        self.config['prohibidos'] = [{'id': replacement, 'hasta': pool.stamp(NOW + 60)}]
        code, output, stderr = self.run_cli('reporte', '--dia', '2026-11-23')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(output.split()[1:5], ['0.000000'] * 4)
        self.assertFalse(stderr)

        self.api.now = NOW + 60
        code, output, stderr = self.run_cli('reporte', '--dia', '2026-11-23')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(output.split()[1:5], ['NA'] * 4)
        self.assertIn(f'VM de Boat sin registro ({replacement})', stderr)

    def test_ronda11_reporte_lee_marcador_escrito_al_consultar_inventario(self):
        directory = self.state / 'vms' / NAME
        (directory / 'registro.json').unlink()
        self.api.sandbox(FAKE_ID, createdAt=pool.stamp(NOW - 3600), cost=monthly_cost(7200, 0.072))
        self.api.now = NOW + 60
        transport = self.api.transport

        def retire_on_inventory(method, path, headers, body, timeout):
            if 'include=cost' in path:
                pool.atomic_json(self.api.path, {})
                self.api.deleted = monthly_cost(7200, 0.072)
                stamp = int(self.api.now) * 10**9
                pool.atomic_json(directory / f'registro.retirado-{stamp}.json', {
                    'id': FAKE_ID, 'nombre': NAME, 'tipo': 'large', 'apta': False,
                    'preparada_en': NOW - 3600,
                })
            return transport(method, path, headers, body, timeout)

        with patch.object(self.api, 'transport', side_effect=retire_on_inventory):
            code, output, stderr = self.run_cli('reporte', '--dia', '2026-11-23')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(output.split()[1:5], ['NA'] * 4)
        self.assertIn('Boat devolvió HTTP 404', stderr)

    def test_ronda12_m1_vm_creada_despues_del_inventario(self):
        name = 'vm-otra-prueba'
        identifier = fake_id('efgh2345')
        self.config['vms'].append({'nombre': name, 'repo': REPO, 'tipo': 'large', 'lugares': 1})
        cutoff = NOW + 60
        self.api.now = cutoff
        transport = self.api.transport

        def create_during_local_read(method, path, headers, body, timeout):
            if path == '/sandboxes/' + FAKE_ID:
                self.api.sandbox(identifier, name=name, createdAt=pool.stamp(NOW + 61),
                                 cost=monthly_cost(120, 0.0012))
                self.api.now = NOW + 121
            return transport(method, path, headers, body, timeout)

        with patch.object(self.api, 'transport', side_effect=create_during_local_read):
            code, output, stderr = self.run_cli('reporte', '--dia', '2026-11-23')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(output.split()[1:5], ['0.000000'] * 4)
        self.assertEqual(sum('include=cost' in path for _, path, _ in self.api.calls), 1)
        self.assertEqual([parse_qs(urlsplit(route).query)['until'] for route in self.usage_calls()],
                         [[pool.stamp(cutoff)]])

        code, output, stderr = self.run_cli('reporte', '--dia', '2026-11-23')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(output.split()[1:5], ['NA'] * 4)

    def test_ronda12_m2_mes_cambia_durante_el_reporte(self):
        midnight = datetime(2026, 12, 1, tzinfo=timezone.utc).timestamp()
        self.api.now = midnight - 2
        self.api.sandbox(FAKE_ID, cost=monthly_cost(7200, 0.072))
        transport = self.api.transport

        def cross_month(method, path, headers, body, timeout):
            if path == '/sandboxes/' + FAKE_ID:
                self.api.now = midnight + 2
            return transport(method, path, headers, body, timeout)

        with patch.object(self.api, 'transport', side_effect=cross_month):
            code, output, stderr = self.run_cli('reporte', '--dia', '2026-11-30')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.api.now, midnight + 2)
        self.assertEqual(sum('include=cost' in path for _, path, _ in self.api.calls), 1)
        self.assertEqual(output.split()[-2:], ['2.000000', '0.072000'])
        self.assertFalse(stderr)
        self.assertEqual([parse_qs(urlsplit(route).query)['until'] for route in self.usage_calls()],
                         [[pool.stamp(midnight - 2)]])

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
    @contextlib.contextmanager
    def install(self, *, loaded=False):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home, stubs = base / 'home', base / 'stubs'
            config = base / 'xdg-config'
            state = base / 'xdg-state'
            log = base / 'commands.txt'
            stubs.mkdir()
            (config / 'boat-pool').mkdir(parents=True)
            (config / 'boat-pool' / 'pool.json').write_text('{}')
            for binary in ('plutil', 'launchctl', 'sleep'):
                command = stubs / binary
                command.write_text(
                    '#!/bin/sh\n'
                    'printf "%s %s\\n" "${0##*/}" "$*" >> "$STUB_LOG"\n'
                    'if [ "${0##*/}:${1:-}" = launchctl:print ]; then\n'
                    '  [ "$STUB_LOADED" = 1 ] && [ "$(grep -c "^launchctl print " "$STUB_LOG")" -le 3 ]\n'
                    'fi\n'
                )
                command.chmod(0o700)
            result = subprocess.run(
                [str(ROOT / 'install.sh')],
                env={**os.environ, 'HOME': str(home), 'STUB_LOG': str(log),
                     'STUB_LOADED': str(int(loaded)),
                     'XDG_CONFIG_HOME': str(config), 'XDG_STATE_HOME': str(state),
                     'PATH': str(stubs) + ':/usr/bin:/bin'},
                text=True, capture_output=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            yield home, state, log.read_text().splitlines()

    def test_instalador_y_launch_agent(self):
        with self.install() as (home, state, commands):
            link = home / '.local/bin/boat-pool'
            self.assertTrue(link.is_symlink())
            self.assertTrue(os.access(link, os.X_OK))
            self.assertTrue((link.resolve().parent / 'pool.py').is_file())
            self.assertTrue((state / 'boat-pool').is_dir())
            agent = home / 'Library/LaunchAgents/local.pao.boat-pool.plist'
            parsed = plistlib.loads(agent.read_bytes())
            self.assertEqual(parsed['StartInterval'], 300)
            self.assertIn('boat-pool" vigilar', parsed['ProgramArguments'][-1])
            self.assertEqual(commands[0], 'plutil -lint ' + str(agent))
            self.assertEqual(commands[-1], f'launchctl bootstrap gui/{os.getuid()} {agent}')

    def test_recarga_espera_bootout(self):
        with self.install(loaded=True) as (home, _, commands):
            domain = f'gui/{os.getuid()}'
            agent = home / 'Library/LaunchAgents/local.pao.boat-pool.plist'
            inspect = f'launchctl print {domain}/local.pao.boat-pool'
            self.assertEqual(commands[1:], [
                inspect, f'launchctl bootout {domain} {agent}',
                inspect, 'sleep 1', inspect, 'sleep 1', inspect,
                f'launchctl bootstrap {domain} {agent}',
            ])


if __name__ == '__main__':
    if sys.argv[1:2] == ['worker']:
        sys.exit(worker())
    unittest.main()
