import json
from unittest.mock import patch
import unittest

import test_pool as base
from test_pool import FAKE_ID, NAME, NOW, fake_id

pool = base.pool


class Verificar(unittest.TestCase):
    # Reutiliza el mundo de Harness sin heredar sus casos de prueba.
    setUp = base.Harness.setUp
    add_vm = base.Harness.add_vm
    save = base.Harness.save
    run_cli = base.Harness.run_cli
    args = base.Harness.args
    get_lease = base.Harness.get_lease
    get_grant = base.Harness.get_grant
    journal = base.Harness.journal

    def other(self, name='vm-otra', places=2):
        identifier = fake_id('efgh2345')
        self.add_vm(name, identifier, places=places)
        return name, identifier

    def resumes(self):
        return sum(method == 'POST' and route.endswith('/resume')
                   for method, route, _ in self.api.calls)

    def test_dos_fragmentos_caen_en_vms_distintas(self):
        self.config['vms'][0]['lugares'] = 2
        other, _ = self.other()
        first = self.get_grant('fragmento-a', 'e2e')['vm']
        second = self.get_grant('fragmento-b', 'e2e')['vm']
        self.assertEqual({first, second}, {NAME, other})
        self.assertEqual(self.resumes(), 0)

    def test_menor_carga_concede_en_la_vacia(self):
        self.config['vms'][0]['lugares'] = 2
        self.get_lease('ocupante')
        other, _ = self.other()
        self.assertEqual(self.get_grant('nuevo')['vm'], other)
        self.assertEqual(self.resumes(), 0)

    def test_encendida_con_cupo_antes_que_apagada(self):
        self.config['vms'][0]['lugares'] = 2
        self.get_lease('ocupante')
        other, _ = self.other()
        self.assertEqual(self.run_cli('apagar', other)[0], 0)
        self.api.calls.clear()
        self.assertEqual(self.get_grant('nuevo')['vm'], NAME)
        self.assertEqual(self.resumes(), 0)

    def test_apagado_pendiente_va_despues_de_una_lista_con_cupo(self):
        self.config['vms'][0]['lugares'] = 2
        self.get_lease('ocupante')
        other, other_id = self.other()
        pool.atomic_json(self.state / 'vms' / other / 'operacion.json', {
            'id': 'pendiente', 'sandbox_id': other_id, 'accion': 'stop', 'desde': pool.stamp(NOW),
        })
        self.api.calls.clear()
        self.assertEqual(self.get_grant('nuevo')['vm'], NAME)
        self.assertEqual(self.resumes(), 0)

    def test_reanudada_vuelve_a_competir_por_carga(self):
        self.config['vms'][0]['lugares'] = 2
        self.get_lease('ocupante')
        other, _ = self.other()
        self.assertEqual(self.run_cli('apagar', other)[0], 0)
        self.assertEqual(self.run_cli('encender', other)[0], 0)
        self.api.calls.clear()
        self.assertEqual(self.get_grant('nuevo')['vm'], other)
        self.assertEqual(self.resumes(), 0)

    def test_diario_ilegible_va_al_final_y_la_otra_concede(self):
        self.config['vms'][0]['lugares'] = 2
        self.get_lease('ocupante')
        other, _ = self.other()
        (self.state / 'vms' / other / 'eventos.jsonl').write_text('no es JSON\n', encoding='utf-8')
        code, output, stderr = self.run_cli(*self.args('fragmento', 'e2e'))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(output)['vm'], NAME)

    def test_empate_concede_en_la_primera_de_pool_json(self):
        self.config['vms'][0]['lugares'] = 2
        self.other()
        self.assertEqual(self.get_grant('empate')['vm'], NAME)

    def test_empate_respeta_el_orden_de_pool_json(self):
        self.config['vms'][0]['lugares'] = 2
        other, _ = self.other()
        self.config['vms'].reverse()
        self.assertEqual(self.get_grant('empate')['vm'], other)

    def test_sin_cupo_con_espera_cero_sale_75_sin_salida(self):
        self.get_lease('uno')
        self.other(places=1)
        self.get_lease('dos')
        code, output, stderr = self.run_cli(*self.args('tres'))
        self.assertEqual(code, 75, stderr)
        self.assertFalse(output)
        self.assertTrue(stderr.startswith('boat-pool: '), stderr)

    def test_orden_no_consulta_boat(self):
        self.config['vms'][0]['lugares'] = 2
        self.other()
        ask_order = pool.Pool.ask_order
        windows = []

        def spy(instance, candidates, kind):
            before = len(self.api.calls)
            ordered = ask_order(instance, candidates, kind)
            windows.append(self.api.calls[before:])
            return ordered

        with patch.object(pool.Pool, 'ask_order', spy):
            code, output, stderr = self.run_cli(*self.args('solicitud', 'e2e'))
        self.assertEqual(code, 0, stderr)
        self.assertTrue(windows)
        self.assertEqual([call for window in windows for call in window], [])
