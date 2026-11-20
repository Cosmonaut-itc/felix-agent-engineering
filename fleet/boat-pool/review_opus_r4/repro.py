"""Reproducciones del revisor B (opus, ronda 4) de SEN-6820.

Cada prueba imprime la salida real del reporte y la que exige la intención.
Una prueba que falla confirma el hallazgo.
"""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
import test_pool
from test_pool import FAKE_ID, NAME, NOW, fake_id, pool

OTHER = fake_id('kmnp2345')
DAY = 86400


class Repro(test_pool.Harness):
    def retire_first_vm(self):
        """Retiro de Jale (SEN-6817): registro renombrado y sandbox borrado en Boat."""
        self.add_vm('vm-otra', OTHER)
        self.assertEqual(self.run_cli('soltar', self.get_lease())[0], 0)
        folder = self.state / 'vms' / NAME
        (folder / 'registro.json').rename(folder / 'registro.retirado-1.json')
        sandboxes = pool.read_json(self.api.path)
        del sandboxes[FAKE_ID]
        pool.atomic_json(self.api.path, sandboxes)

    def report(self, day):
        code, output, stderr = self.run_cli('reporte', '--dia', day)
        print(f'\n[{self.id().rsplit(".", 1)[-1]}] reporte --dia {day}\n  stdout: {output.strip()}\n  stderr: {stderr.strip()}')
        self.assertEqual(code, 0, stderr)
        return output.split('\t')[1:5]

    def test_e1a_vm_retirada_en_pool_json_deja_na_todos_los_dias_siguientes(self):
        self.retire_first_vm()
        self.api.now = NOW + DAY
        self.api.usage[(OTHER, '2026-11-24T00:00:00Z')] = {'seconds': 7200, 'dollars': 0.072}
        # El ID retirado no existió el 24; la única VM viva usó 1 h large.
        self.assertEqual(self.report('2026-11-24'), ['0.000000', '1.000000', '0.000000', '0.072000'])

    def test_e1b_vm_retirada_y_quitada_de_pool_json_baja_el_costo_sin_na(self):
        self.retire_first_vm()
        del self.config['vms'][0]
        self.api.now = NOW + 3600
        # Boat midió 1 h large para cada VM el 23; el ID retirado ya da 404.
        self.api.usage[(OTHER, '2026-11-23T00:00:00Z')] = {'seconds': 7200, 'dollars': 0.072}
        # Intención: NA (no se puede medir una VM del pool que corrió ese día), nunca 1 h y 0.072.
        self.assertEqual(self.report('2026-11-23'), ['NA'] * 4)

    def test_e2_vm_creada_antes_de_medianoche_y_preparada_despues(self):
        # Jale: POST /sandboxes a las 23:40 del 22; registro con preparada_en 00:10 del 23.
        path = self.state / 'vms' / NAME / 'registro.json'
        record = pool.read_json(path)
        record['preparada_en'] = '2026-11-23T00:10:00Z'
        pool.atomic_json(path, record)
        self.api.usage[(FAKE_ID, '2026-11-22T00:00:00Z')] = {'seconds': 2400, 'dollars': 0.024}
        # Boat midió 20 min de large el 22: 0.333333 h y 0.024 USD (o NA), nunca 0.
        self.assertIn(self.report('2026-11-22'),
                      (['0.000000', '0.333333', '0.000000', '0.024000'], ['NA'] * 4))


if __name__ == '__main__':
    unittest.main(verbosity=2)
