import json
from hostgate.salud.lector_desk import _ts
from hostgate.tests.test_contract import Harness
from hostgate.tests.support.world import FIXTURES


class DesktopAdoption(Harness):
    def check_huge_json(self, value):
        self.world.health_kind = 'desk'
        self.world.witness = self.world.default_shell = True
        self.world.sample = json.loads((FIXTURES / 'health/desk.json').read_text())
        self.world.sample_boot = self.world.sample['boot']['bootId']
        self.world.utc = _ts(self.world.sample['ts']) + 30
        self.assertEqual(self.gate._health(), [])
        self.world.sample['metrics']['commitBytes']['value'] = value
        self.world.sample = json.loads(json.dumps(self.world.sample))
        reasons = self.gate._health()
        self.assertTrue(reasons)
        self.assertTrue({r['code'] for r in reasons} <= {'health-invalid', 'health-reader-pending'})
        self.assertTrue(self.view()['degraded'])
        rejected = self.reserve()
        self.assertEqual(rejected['exitCode'], 1)
        self.assertFalse(rejected['wouldGrant'])

    def test_positive_huge_json_is_not_healthy(self):
        self.check_huge_json(10**400)

    def test_negative_huge_json_is_not_healthy(self):
        self.check_huge_json(-10**400)
