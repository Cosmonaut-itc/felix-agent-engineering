import copy
import json
import unittest
from hostgate.salud.lector_mini import evaluar
from hostgate.salud.lector_desk import _ts
from hostgate.tests.support.world import FIXTURES


class MiniReader(unittest.TestCase):
    def setUp(self):
        self.sample = json.loads((FIXTURES / 'health/mini.json').read_text())
        self.now = _ts(self.sample['ts']) + 30
        self.boot = self.sample['boot']['bootId']

    def reasons(self, sample=None):
        return evaluar(self.sample if sample is None else sample, self.now, self.boot)

    def test_healthy_and_additive(self):
        self.assertEqual(self.reasons(), [])
        self.sample['observations'] = {'irrelevant': None}
        self.assertEqual(self.reasons(), [])

    def test_no_snapshot_boot_age_status_persistence(self):
        for bad in (None, [], 'x', 1): self.assertTrue(evaluar(bad, self.now, self.boot))
        self.assertIn('boot', evaluar(self.sample, self.now, None))
        self.assertIn('boot', evaluar(self.sample, self.now, 'other'))
        for offset in (-61, 181): self.assertIn('vigencia', evaluar(self.sample, self.now - 30 + offset, self.boot))
        for field, value in [('status', 'warn'), ('schema', 'x'), ('host', 'other'),
                             ('persist', {}), ('errors', [1]), ('ts', '2026-02-30T00:00:00Z')]:
            sample = copy.deepcopy(self.sample); sample[field] = value
            self.assertTrue(self.reasons(sample), field)

    def test_ram_pressure_disk_boundaries(self):
        for metric, value, unhealthy in [('memLevelPct', 31, False), ('memLevelPct', 30, True),
                                        ('memLevelPct', 15, True), ('memPressure', 1, False),
                                        ('memPressure', 2, True), ('memPressure', 4, True),
                                        ('memPressure', 3, True)]:
            sample = copy.deepcopy(self.sample); sample['metrics'][metric]['value'] = value
            self.assertEqual(bool(self.reasons(sample)), unhealthy)
        for gib, unhealthy in [(50, False), (49, True), (25, True), (24, True)]:
            sample = copy.deepcopy(self.sample)
            sample['volumes']['/System/Volumes/Data']['freeBytes'] = gib * 1024**3
            self.assertEqual(bool(self.reasons(sample)), unhealthy)

    def test_malformed_fields_never_succeed_or_raise(self):
        for name in self.sample['metrics']:
            for value in (None, True, -1, float('nan'), float('inf'), '', {}, []):
                sample = copy.deepcopy(self.sample); sample['metrics'][name]['value'] = value
                self.assertTrue(self.reasons(sample), (name, value))
        for field in ('metrics', 'volumes', 'boot', 'persist'):
            for value in (None, 1, [], ''):
                sample = copy.deepcopy(self.sample); sample[field] = value
                self.assertTrue(self.reasons(sample))
        sample = copy.deepcopy(self.sample); sample['metrics']['ncpu']['value'] = 1.5
        self.assertTrue(self.reasons(sample))
