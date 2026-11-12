"""Round-seven mode, demand, sync and writer diagnostic regressions."""
import contextlib
import io
import json
import os
from pathlib import Path
from unittest import mock

from deskpolicy import cli
from deskpolicy.engine import DeskPolicy
from deskpolicy.store import atomic_json, held, read_json
from deskpolicy.tests import test_policy as fixtures
from deskpolicy.tests import test_r4 as r4


class RoundSevenTest(r4.RoundFourTest):
    advance = fixtures.PolicyTest.advance
    renew = fixtures.PolicyTest.renew
    close_actor = fixtures.PolicyTest.close_actor
    run_cli = fixtures.PolicyTest.run_cli

    def lane_pending(self, name, lane):
        return self.engine.register(name, 'hasito', 'script', 'test', 1, lane=lane)

    def dawn(self, writer_dies):
        if not writer_dies:
            self.h.now = self.h.now.replace(hour=13, minute=57, second=0)
        workers = []
        for n in (1, 2):
            self.lane_pending('H' + str(n), n)
        self.publish()
        for n in (1, 2):
            receipt, worker, pipe = self.launch('H' + str(n), n)
            self.assertTrue(receipt['execute'], receipt)
            workers.append((worker, pipe))
            if n == 1:
                self.advance(121)
        self.advance(30)
        self.lane_pending('H4', 4)
        before, _, _ = self.launch('H4', 4)
        self.assertEqual(before['reason'], 'quota-full')
        if writer_dies:
            self.h.advance(61)
            self.renew()
        else:
            self.advance(60)
        for worker, pipe in workers:
            self.close_actor(worker, pipe)
        view = self.engine.status()
        self.assertEqual((view['mode'], view['max_lane']), ('day', 3))
        reasons = []
        for _ in range(8):
            self.h.advance(30)
            self.renew()
            receipt, _, _ = self.launch('H4', 4)
            reasons.append(receipt['reason'])
        print('R7 DAWN', 'writer-dead' if writer_dies else 'dawn', reasons, flush=True)
        self.assertEqual(reasons, ['lane-closed'] * 8)
        self.assertIsNone(read_json(self.engine.record_path('H4'))['claim'])
        return receipt

    def test_dawn_high_lane_is_closed(self):
        self.dawn(False)

    def test_dead_writer_high_lane_is_closed(self):
        self.dawn(True)

    def test_closed_lane_precedes_busy_claim_and_fifo(self):
        self.lane_pending('live', 4)
        self.publish()
        admitted, _, _ = self.launch('live', 4)
        self.assertTrue(admitted['execute'])
        self.lane_pending('head', 4)
        self.lane_pending('successor', 4)
        self.h.advance(61)
        self.renew()
        for name in ('head', 'successor'):
            receipt, _, _ = self.launch(name, 4)
            self.assertEqual(receipt['reason'], 'lane-closed', receipt)
        self.assertEqual(self.engine.status()['live']['hasito'], 1)

    def test_closed_lanes_do_not_inflate_night_demand_or_take_jale_quota(self):
        for lane in (1, 2):
            self.lane_pending('H' + str(lane), lane)
        for n in range(3):
            self.pending('J' + str(n), 'jale', 'chain')
        before = self.publish()
        for lane in range(5, 9):
            self.lane_pending('H' + str(lane), lane)
        after = self.publish()
        print('R7 DEMAND', before['pending'], before['limits'], '->', after['pending'], after['limits'], flush=True)
        self.assertEqual(after['pending'], {'jale': 3, 'hasito': 2})
        self.assertEqual(after['limits'], before['limits'])
        self.assertEqual(after['limits']['jale'], 2)

    def test_day_and_expired_policy_views_ignore_closed_pending_lanes(self):
        self.lane_pending('high', 4)
        self.publish()
        self.h.advance(61)
        self.renew()
        self.assertEqual(self.engine.status()['pending']['hasito'], 0)
        self.h.now = self.h.now.replace(hour=16)
        self.assertEqual(self.publish()['pending']['hasito'], 0)
        self.assertEqual(self.engine.status()['head']['hasito'], None)
        # Ignoring demand does not discard the request or a future renewal.
        self.assertEqual(read_json(self.engine.record_path('high'))['state'], 'pending')

    def test_invalid_sync_byte_closes_without_execution_or_capacity(self):
        self.h.now = self.h.now.replace(hour=16)
        self.pending('bad')
        directory = self.receipt_directory('bad')
        marker = self.h.directory / 'must-not-execute'
        reader, writer = os.pipe()
        os.write(writer, b'x')
        try:
            args = self.execute_args('bad', ['/usr/bin/touch', str(marker)])
            at = args.index('--')
            args[at:at] = ['--sync-ready-fd', str(reader), '--sync-timeout-s', '8']
            result = self.run_cli(args)
        finally:
            os.close(reader)
            os.close(writer)
        claim = read_json(self.engine.record_path('bad'))['claim']
        receipt = read_json(directory / 'pao-admission.json')
        print('R7 BADBYTE', result, claim['state'], claim['close_cause'], flush=True)
        self.assertEqual(result['rc'], 75)
        self.assertEqual((claim['state'], claim['close_cause']), ('closed', 'sync-timeout'))
        self.assertEqual((receipt['claim_state'], receipt['close_cause']), ('closed', 'sync-timeout'))
        self.assertIsNone(receipt['script_exit_code'])
        self.assertFalse(marker.exists())
        self.assertEqual(self.engine.status()['weight'], 0)
        self.assertFalse(held(self.engine.life_path('hasito', 1)))

    def test_closed_claim_diagnostic_once_per_process_claim_and_error(self):
        self.pending('closed')
        directory = self.receipt_directory('closed')
        args = self.execute_args('closed', ['/bin/true'])
        self.assertEqual(self.run_cli(args)['rc'], 0)
        path = directory / 'pao-admission.json'
        original = read_json(path)
        claim = read_json(self.engine.record_path('closed'))['claim']
        self.assertEqual(claim['state'], 'closed')
        atomic_json(path, {**original, 'request_id': 'intruder'})
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            for _ in range(3):
                self.publish()
            # A second instance in the same process must not emit duplicates.
            DeskPolicy(self.engine.runtime).publish()
            path.write_bytes(b'{')
            for _ in range(2):
                self.publish()
        diagnostics = [json.loads(line) for line in errors.getvalue().splitlines()]
        print('R7 DIAGNOSTICS', diagnostics, flush=True)
        self.assertEqual(len(diagnostics), 2)
        self.assertEqual(diagnostics[0]['detail'], 'receipt-conflict')
        self.assertNotEqual(diagnostics[1]['detail'], diagnostics[0]['detail'])
        # Deduplication must never disable retries or repair.
        atomic_json(path, {**original, 'script_exit_code': None})
        self.publish()
        self.assertEqual(read_json(path)['script_exit_code'], 0)

    def test_contract_defines_auto_and_explicit_lane_closed_protocol(self):
        contract = (Path(__file__).resolve().parents[2] / 'contracts/desk-politica-v1.md').read_text()
        for text in ('`lane-closed`', 'carril N cerrado en el modo vigente',
                     'retira su turno', 'sucesores', 'sin `--lock` ni `MINI_LOCK`'):
            self.assertTrue(text in contract, text)
