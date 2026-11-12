"""Day quotas after the NVMe move; real claims and unchanged safety gates."""
import unittest

from deskpolicy.model import Invalid, validate_policy
from deskpolicy.store import atomic_json
from deskpolicy.tests import test_policy as fixtures


class DayBridgeTest(unittest.TestCase):
    setUp = fixtures.PolicyTest.setUp
    tearDown = fixtures.PolicyTest.tearDown
    pending = fixtures.PolicyTest.pending
    publish = fixtures.PolicyTest.publish
    launch = fixtures.PolicyTest.launch
    advance = fixtures.PolicyTest.advance
    renew = fixtures.PolicyTest.renew

    def day(self):
        self.h.now = self.h.now.replace(hour=16)

    def admit(self, name, project, kind, lane):
        self.pending(name, project, kind)
        receipt, _, _ = self.launch(name, lane)
        self.assertTrue(receipt['execute'], receipt)
        self.assertEqual(receipt['cpus'], list(range(12)))

    def test_three_hasito_and_two_chains_fill_five_day_places(self):
        self.day()
        for lane in (1, 2, 3):
            self.admit('H' + str(lane), 'hasito', 'script', lane)
        for lane in (1, 2):
            self.admit('J' + str(lane), 'jale', 'chain', lane)
        self.assertEqual(self.engine.status()['live'], {'hasito': 3, 'jale': 2})

    def test_two_day_chains(self):
        self.day()
        self.admit('first', 'jale', 'chain', 1)
        self.admit('second', 'jale', 'chain', 2)

    def test_chain_and_verify_can_coexist(self):
        self.day()
        self.admit('chain', 'jale', 'chain', 1)
        self.admit('verify', 'jale', 'verify', 2)

    def test_fourth_hasito_lane_stays_closed(self):
        self.day()
        self.pending('fourth')
        receipt, _, _ = self.launch('fourth', 4)
        self.assertEqual(receipt['reason'], 'lane-closed')

    def test_jale_lane_three_waits_as_quota_full(self):
        # run-suite.sh recorre los carriles hasta max_lane; lane-closed lo haría reintentar sin pausa.
        self.day()
        self.pending('third', 'jale', 'chain')
        receipt, _, _ = self.launch('third', 3)
        self.assertEqual(receipt['reason'], 'quota-full')

    def test_lane_three_pending_and_policy_view_in_day_and_fallback(self):
        self.day()
        self.engine.register('H3', 'hasito', 'script', 'test', 1, lane=3)
        for published in (False, True):
            if published:
                policy = self.publish()
                self.assertEqual(policy['limits'], {'jale': 2, 'hasito': 3})
                validate_policy(policy, self.h.sample())
                with self.assertRaisesRegex(Invalid, 'day-limits'):
                    validate_policy({**policy, 'limits': {'jale': 2, 'hasito': 2}}, self.h.sample())
            view = self.engine.status()
            self.assertEqual(view['max_lane'], 3)
            self.assertEqual(view['limits'], {'jale': 2, 'hasito': 3})
            self.assertEqual(view['pending']['hasito'], 1)
            self.assertEqual(view['head']['hasito'], 'H3')

    def seed_night(self, with_jale):
        # Retained high night lanes leave lane 1 free to reach count checks.
        self.configuration['night_budget'] = 6
        atomic_json(self.h.config, self.configuration)
        self.engine.preflight(self.evidence, arm=True)
        seeds = [('H' + str(n), 'hasito', 'script', n) for n in (2, 3, 4)]
        if with_jale:
            seeds += [('J1', 'jale', 'chain', 1), ('J2', 'jale', 'chain', 2)]
        for name, project, kind, lane in seeds:
            self.pending(name, project, kind)
        self.publish()
        for name, _, _, lane in seeds:
            receipt, _, _ = self.launch(name, lane)
            self.assertTrue(receipt['execute'], receipt)
            self.advance(120)
        self.day()

    def test_fourth_hasito_is_quota_full_with_retained_night_lanes(self):
        self.seed_night(False)
        self.pending('fourth')
        receipt, _, _ = self.launch('fourth', 1)
        self.assertEqual(receipt['reason'], 'quota-full')

    def test_sixth_total_is_budget_full_with_retained_night_lanes(self):
        self.seed_night(True)
        self.pending('sixth')
        receipt, _, _ = self.launch('sixth', 1)
        self.assertEqual(receipt['reason'], 'budget-full')

    def test_exclusive_blocks_chain_and_verify(self):
        self.day()
        self.admit('exclusive', 'jale', 'exclusive', 1)
        for kind in ('chain', 'verify'):
            self.pending(kind, 'jale', kind)
            receipt, _, _ = self.launch(kind, 2)
            self.assertEqual(receipt['reason'], 'resource-busy')
            self.engine.register(kind, 'jale', kind, 'test', 1, withdraw=True)

    def test_chain_blocks_exclusive(self):
        self.day()
        self.admit('chain', 'jale', 'chain', 1)
        self.pending('exclusive', 'jale', 'exclusive')
        receipt, _, _ = self.launch('exclusive', 2)
        self.assertEqual(receipt['reason'], 'resource-busy')

    def test_second_verify_stays_resource_busy(self):
        self.day()
        self.admit('verify1', 'jale', 'verify', 1)
        self.pending('verify2', 'jale', 'verify')
        receipt, _, _ = self.launch('verify2', 2)
        self.assertEqual(receipt['reason'], 'resource-busy')
