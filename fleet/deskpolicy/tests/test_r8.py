"""Final-round recovery and consumer-contract regressions."""
from pathlib import Path

from deskpolicy.store import atomic_json, read_json
from deskpolicy.tests import test_policy as fixtures
from deskpolicy.tests import test_r4 as r4


class RoundEightTest(r4.RoundFourTest):
    advance = fixtures.PolicyTest.advance
    renew = fixtures.PolicyTest.renew
    close_actor = fixtures.PolicyTest.close_actor

    def claim(self, name):
        return read_json(self.engine.record_path(name))['claim']

    def lane_pending(self, name, lane, project='hasito'):
        kwargs = {'lane': lane} if project == 'hasito' else {}
        return self.engine.register(name, project, 'script' if project == 'hasito' else 'chain',
                                    'test', 1, **kwargs)

    def expire_without_writer(self):
        for _ in range(3):
            self.h.advance(30)
            self.renew()
        self.assertEqual(self.engine.status()['mode'], 'day')

    def test_closed_high_lane_reconciles_then_h2_enters_first_attempt(self):
        self.lane_pending('H1', 1)
        self.lane_pending('H4', 4)
        self.publish()
        first, w1, c1 = self.launch('H1', 1)
        self.assertTrue(first['execute'], first)
        self.advance(121)
        high, w3, _ = self.launch('H4', 4)
        self.assertTrue(high['execute'], high)
        w3.kill()
        w3.join(60)
        self.expire_without_writer()
        self.lane_pending('H4b', 4)
        denied, _, _ = self.launch('H4b', 4)
        self.assertEqual(denied['reason'], 'lane-closed', denied)
        self.engine.register('H4b', 'hasito', 'script', 'test', 1, withdraw=True, lane=4)
        self.lane_pending('H2', 2)
        second, w2, c2 = self.launch('H2', 2)
        print('R8 STALE H4', self.claim('H4')['state'], 'H4b', denied['reason'],
              'H2 first attempt', second['state'], second['reason'], flush=True)
        self.assertEqual(self.claim('H4')['state'], 'closed')
        self.assertEqual(self.claim('H4')['close_cause'], 'reconciled')
        self.assertTrue(second['execute'], second)
        self.close_actor(w2, c2)
        self.close_actor(w1, c1)

    def test_closed_lane_reconciles_only_requested_project_and_lane(self):
        self.configuration['night_budget'] = 5
        atomic_json(self.h.config, self.configuration)
        self.engine.preflight(self.evidence, arm=True)
        self.lane_pending('H4', 4)
        self.lane_pending('H5', 5)
        self.lane_pending('J3', 3, 'jale')
        self.publish()
        workers = []
        for name, lane in (('H4', 4), ('H5', 5), ('J3', 3)):
            receipt, worker, _ = self.launch(name, lane)
            self.assertTrue(receipt['execute'], receipt)
            workers.append(worker)
            if name != 'J3':
                self.advance(121)
        for worker in workers:
            worker.kill()
            worker.join(60)
        self.expire_without_writer()
        self.lane_pending('H4b', 4)
        denied, _, _ = self.launch('H4b', 4)
        self.assertEqual(denied['reason'], 'lane-closed', denied)
        states = {name: self.claim(name)['state'] for name in ('H4', 'H5', 'J3')}
        print('R8 SCOPE', states, flush=True)
        self.assertEqual(states, {'H4': 'closed', 'H5': 'uncertain', 'J3': 'uncertain'})

    def test_contract_defines_jale_lane_closed_reselection(self):
        contract = (Path(__file__).resolve().parents[2] / 'contracts/desk-politica-v1.md').read_text()
        jale = contract.split('## Integración Jale', 1)[1].split('## Integración Hasito', 1)[0]
        for text in ('Ante `lane-closed`', 'suelta el slot', 'vuelve a leer `estado`',
                     'espera y reintenta como ante `resource-busy`'):
            self.assertIn(text, jale)
