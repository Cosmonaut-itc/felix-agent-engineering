import hashlib
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.environ.get("REPRO_TESTS") or str(Path(__file__).resolve().parent))
import test_pool
from test_pool import FAKE_ID, NAME, NOW, pool


class Repro(test_pool.Harness):
    def test_b1_espera_viva_sobrevive_a_repreparar(self):
        prep = self.get_lease('prep', 'preparar', '--vm', NAME)
        suite = self.start_worker(*self.args('suite', 'contract', '--esperar', '8', '--prioridad', 'suite'))
        self.queue('suite')
        path = self.state / 'vms' / NAME / 'registro.json'
        record = pool.read_json(path)
        record.update(preparada_en=pool.stamp(NOW + 1), revision_runner='rev-nueva')
        pool.atomic_json(path, record)
        released = self.run_cli('soltar', prep)[0]
        out, err = suite.communicate(timeout=15)
        print(f'\nB1 soltar(prep)={released} suite rc={suite.returncode} stdout={out!r} stderr={err!r}')
        self.assertEqual(suite.returncode, 0, err)

    def test_b4_un_id_prohibido_apaga_vigilar_y_soltar_de_todo_el_pool(self):
        other = 'bx_qrst2345'
        self.add_vm('vm-otra', other)
        lease = self.get_lease()
        hashed = hashlib.sha256(other.encode()).hexdigest()
        with patch.object(pool, 'FORBIDDEN_IDS', {hashed}):
            release = self.run_cli('soltar', lease)
            remains = (self.state / 'vms' / NAME / 'arriendos' / (lease + '.json')).exists()
            self.api.now += 900 + 30 * 60 + 60
            watch = self.run_cli('vigilar')
        stop = ('POST', '/sandboxes/' + FAKE_ID + '/stop', None)
        print(f'\nB4 soltar rc={release[0]} stderr={release[2]!r} arriendo_sigue={remains}'
              f' | vigilar rc={watch[0]} stderr={watch[2]!r} escrituras={self.writes()}')
        self.assertEqual((release[0], remains, watch[0], stop in self.writes()), (0, False, 0, True))


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0], '-v',
                        'Repro.test_b1_espera_viva_sobrevive_a_repreparar',
                        'Repro.test_b4_un_id_prohibido_apaga_vigilar_y_soltar_de_todo_el_pool'])
