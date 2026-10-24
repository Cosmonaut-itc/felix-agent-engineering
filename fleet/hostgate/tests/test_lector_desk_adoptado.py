import json
from hostgate.salud import lector_desk
from hostgate.salud.lector_desk import _ts
from hostgate.tests.test_contract import Harness
from hostgate.tests.support.world import FIXTURES


class DesktopAdoption(Harness):
    def huge_sample(self, value):
        self.world.health_kind = 'desk'
        self.world.witness = self.world.default_shell = True
        self.world.sample = json.loads((FIXTURES / 'health/desk.json').read_text())
        self.world.sample_boot = self.world.sample['boot']['bootId']
        self.world.utc = _ts(self.world.sample['ts']) + 30
        self.assertEqual(self.gate._health(), [])
        self.world.sample['metrics']['commitBytes']['value'] = value
        self.world.sample = json.loads(json.dumps(self.world.sample))

    def check_huge_json(self, value):
        self.huge_sample(value)
        # Sólo el lector adoptado emite este motivo; el lector previo cae en las capturas del adaptador.
        self.assertEqual([(r['code'], r['detail']) for r in self.gate._health()],
                         [('health-invalid', 'commitBytes incompleta')])
        self.assertTrue(self.view()['degraded'])
        rejected = self.reserve()
        self.assertEqual(rejected['exitCode'], 1)
        self.assertFalse(rejected['wouldGrant'])

    def test_positive_huge_json_is_not_healthy(self):
        self.check_huge_json(10**400)

    def test_negative_huge_json_is_not_healthy(self):
        self.check_huge_json(-10**400)

    def test_adopted_reader_rejects_huge_json_without_overflow(self):
        for value in (10**400, -10**400):
            with self.subTest(value=value):
                self.huge_sample(value)
                self.assertEqual(lector_desk.evaluar(self.world.sample, self.world.utc, self.world.sample_boot),
                                 ['commitBytes incompleta'])
