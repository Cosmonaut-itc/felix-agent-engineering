"""Round-five capacity recovery and bounded pre-sync admission."""
import contextlib
import errno
import gc
import io
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time
import unittest
from unittest import mock

from deskpolicy import cli, store
from deskpolicy.model import Invalid
from deskpolicy.store import Runtime, held, mutex, open_file, process, read_json, session_members
from deskpolicy.engine import DeskPolicy
from deskpolicy.tests import test_policy as fixtures
from deskpolicy.tests import test_r3 as r3
from deskpolicy.tests import test_r4 as r4


def preparation_actor(harness, connection):
    os.setsid()
    engine = DeskPolicy(Runtime(harness))
    life = open_file(engine.life_path('jale', 1), create=True)
    child = None
    try:
        with mutex(engine.root) as lock:
            receipt = engine.admit('prepare', 'run-prepare', 1, os.getpid(), life, lock)
        child = subprocess.Popen(['/bin/sleep', '90'], close_fds=True)
        with mutex(engine.root) as lock:
            try:
                engine.close('prepare', 'run-prepare', os.getpid(), life, lock,
                             claim_token=receipt['claim_token'], script_exit_code=1)
            except Invalid as exc:
                assert str(exc) == 'close-unproven'
        connection.send(process(child.pid))
        connection.recv()
    finally:
        if child is not None:
            child.terminate()
            child.wait()
        os.close(life)


def unregistered_keeper(harness, connection):
    os.setsid()
    engine = DeskPolicy(Runtime(harness))
    life = open_file(engine.life_path('jale', 1), create=True)
    with mutex(engine.root) as lock:
        receipt = engine.admit('keeper', 'run-keeper', 1, os.getpid(), life, lock)
        # A failed preparation can already have marked the launcher uncertain.
        record = read_json(engine.record_path('keeper'))
        record['claim']['state'] = 'uncertain'
        engine.write_record(record)
    keeper = os.fork()
    if keeper:
        os.close(life)
        os._exit(0)
    os.setsid()
    r4.keep_fds(life, connection.fileno())
    connection.send(process(os.getpid()))
    connection.recv()
    os.close(life)
    os._exit(0)


class SessionProofTest(unittest.TestCase):
    def test_three_empty_sweeps_spaced_at_least_fifty_ms(self):
        stamps = []
        def listing(path):
            stamps.append(time.monotonic())
            return iter([])
        with mock.patch.object(Path, 'iterdir', listing):
            self.assertEqual(session_members(987654321), [])
        self.assertEqual(len(stamps), 3)
        self.assertTrue(all(b - a >= .05 for a, b in zip(stamps, stamps[1:])), stamps)

    def test_disappearing_unrelated_pids_are_ignored_in_every_sweep(self):
        entry = Path('/proc') / str(os.getpid())
        for error in (FileNotFoundError(errno.ENOENT, 'gone'), ProcessLookupError(errno.ESRCH, 'gone')):
            with self.subTest(error=error), \
                 mock.patch.object(Path, 'iterdir', side_effect=lambda: iter([entry])), \
                 mock.patch.object(store, 'process', side_effect=error):
                self.assertEqual(session_members(987654321), [])

    def test_member_in_third_sweep_prevents_close(self):
        entry = Path('/proc') / str(os.getpid())
        with mock.patch.object(Path, 'iterdir', side_effect=[iter([]), iter([]), iter([entry])]):
            self.assertEqual(session_members(os.getsid(0)), [os.getpid()])

    def test_read_and_parse_errors_remain_inconclusive(self):
        entry = Path('/proc') / str(os.getpid())
        for error in (PermissionError('denied'), OSError(errno.EIO, 'disk'), ValueError('bad stat')):
            with self.subTest(error=error), \
                 mock.patch.object(Path, 'iterdir', side_effect=lambda: iter([entry])), \
                 mock.patch.object(store, 'process', side_effect=error):
                self.assertIsNone(session_members(987654321))


class RoundFiveTest(r4.RoundFourTest):
    claim = r4.LifecycleTest.claim
    run_cli = fixtures.PolicyTest.run_cli

    def reconcile_until_closed(self, name):
        r4.until(lambda: (self.engine.reconcile(), self.claim(name)['state'])[1] == 'closed',
                 'empty sessions never reconciled: ' + name)

    def test_admit_reconciles_only_uncertain_claim_of_requested_resource(self):
        self.h.now = self.h.now.replace(hour=16)
        for name, lane in (('old', 1), ('other', 2)):
            self.pending(name)
            receipt, worker, _ = self.launch(name, lane)
            self.assertTrue(receipt['execute'], receipt)
            worker.kill()
            worker.join(60)
        self.pending('next')
        receipt, _, _ = self.launch('next', 1)
        print('R5 no-writer recovery', receipt['state'], self.claim('old')['state'],
              self.claim('other')['state'], flush=True)
        self.assertTrue(receipt['execute'], receipt)
        self.assertEqual(self.claim('old')['close_cause'], 'reconciled')
        self.assertEqual(self.claim('other')['state'], 'uncertain')

    def test_failed_verify_preparation_reconciles_after_child_finishes(self):
        self.h.now = self.h.now.replace(hour=16)
        self.pending('prepare', 'jale', 'verify')
        worker, pipe = self.spawn(preparation_actor, self.h)
        self.assertTrue(pipe.poll(60))
        child = pipe.recv()
        self.extra_pids.append((child['pid'], child['start']))
        self.assertEqual(self.engine.reconcile()['closed'], [])
        self.assertEqual(self.claim('prepare')['keepers'], [])
        pipe.send('finish')
        worker.join(60)
        self.assertEqual(worker.exitcode, 0)
        self.reconcile_until_closed('prepare')
        self.assertEqual(self.claim('prepare')['script_exit_code'], 1)
        self.pending('next', 'jale', 'verify')
        receipt, _, _ = self.launch('next')
        self.assertTrue(receipt['execute'], receipt)

    def test_alive_unregistered_keeper_holds_life_until_registration(self):
        self.h.now = self.h.now.replace(hour=16)
        self.pending('keeper', 'jale', 'verify')
        worker, pipe = self.spawn(unregistered_keeper, self.h)
        self.assertTrue(pipe.poll(60))
        keeper = pipe.recv()
        self.extra_pids.append((keeper['pid'], keeper['start']))
        worker.join(60)
        self.assertTrue(held(self.engine.life_path('jale', 1)))
        self.assertEqual(self.engine.reconcile()['closed'], [])
        self.assertEqual(self.claim('keeper')['state'], 'uncertain')
        pipe.send('finish')
        os.waitpid(keeper['pid'], 0)
        self.reconcile_until_closed('keeper')

    def test_receipt_failure_does_not_abort_writer_or_other_claims(self):
        self.h.now = self.h.now.replace(hour=16)
        for name, lane in (('broken', 1), ('healthy', 2)):
            self.pending(name)
            self.receipt_directory(name)
            _, worker, _ = self.launch(name, lane)
            worker.kill()
            worker.join(60)
        broken = self.h.directory / 'runs/run-broken/pao-admission.json'
        broken.write_text('{')
        broken.chmod(0o600)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            policy = self.engine.publish()
        self.assertEqual(policy['revision'], 1)
        self.assertEqual([self.claim(n)['state'] for n in ('broken', 'healthy')], ['closed', 'closed'])
        error = json.loads(errors.getvalue())
        self.assertEqual((error['schema'], error['request_id']), ('pao/desk-reconcile-diagnostic@1', 'broken'))
        self.assertTrue(error['detail'])
        self.assertNotIn('exit_code', error)

    def test_status_reports_effective_day_and_night_lanes(self):
        self.h.now = self.h.now.replace(hour=16)
        for published in (False, True):
            if published:
                self.publish()
            view = self.engine.status()
            self.assertEqual((view['mode'], view['budget'], view.get('max_lane')), ('day', None, 2))
            self.assertEqual(view['limits'], {'jale': 2, 'hasito': 2})
        self.h.now = self.h.now.replace(hour=6)
        self.publish()
        view = self.engine.status()
        self.assertEqual((view['mode'], view['budget'], view.get('max_lane')), ('night', 4, 4))

    def test_sync_deadline_closes_without_script_and_releases_capacity(self):
        self.pending('sync', weight=2)
        self.publish()
        directory = self.receipt_directory('sync')
        marker = self.h.directory / 'must-not-run'
        reader, writer = os.pipe()
        try:
            args = self.execute_args('sync', ['/usr/bin/touch', str(marker)])
            args[args.index('--'):args.index('--')] = [
                '--sync-ready-fd', str(reader), '--sync-timeout-s', '.15']
            result = self.run_cli(args)
            replay = self.run_cli(args)
        finally:
            os.close(reader)
            os.close(writer)
        receipt = read_json(directory / 'pao-admission.json')
        self.assertEqual(result['rc'], 75, result)
        self.assertEqual((receipt['state'], receipt['claim_state'], receipt['close_cause'],
                          receipt['script_exit_code']), ('admitted', 'closed', 'sync-timeout', None))
        self.assertFalse(marker.exists())
        self.assertEqual((self.engine.status()['weight'], self.engine.status()['live']['hasito']), (0, 0))
        self.assertFalse(held(self.engine.life_path('hasito', 1)))
        self.assertEqual(replay['rc'], 0)
        self.assertFalse(marker.exists())

    def test_sync_ack_executes_once_and_default_is_fifteen_minutes(self):
        self.assertEqual(cli.parser().parse_args(['ejecutar', '--request-id', 'sync',
            '--run-id', 'run-sync', '--lane', '1', '--', '/bin/true']).sync_timeout_s, 900)
        self.h.now = self.h.now.replace(hour=16)
        self.pending('sync')
        directory = self.receipt_directory('sync')
        marker = self.h.directory / 'ran'
        reader, writer = os.pipe()
        os.write(writer, b'1')
        try:
            args = self.execute_args('sync', ['/usr/bin/touch', str(marker)])
            args[args.index('--'):args.index('--')] = ['--sync-ready-fd', str(reader)]
            result = self.run_cli(args)
        finally:
            os.close(reader)
            os.close(writer)
        self.assertEqual(result['rc'], 0, result)
        self.assertTrue(marker.exists())
        self.assertEqual(read_json(directory / 'pao-admission.json')['close_cause'], 'completed')

    def test_fixture_reaps_process_handles_under_fd_limit(self):
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        start = len(list(Path('/proc/self/fd').iterdir()))
        resource.setrlimit(resource.RLIMIT_NOFILE, (start + 40, hard))
        try:
            for _ in range(30):
                self.assertEqual(self.run_cli(['estado'])['rc'], 0)
            self.assertLessEqual(len(list(Path('/proc/self/fd').iterdir())), start + 4)
        finally:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))

    def test_module_io_does_not_leak_fds(self):
        gc.collect()
        start = len(list(Path('/proc/self/fd').iterdir()))
        for _ in range(50):
            self.pending('renew')
            self.engine.status()
            self.engine.reconcile()
        self.assertLessEqual(len(list(Path('/proc/self/fd').iterdir())), start)

    def test_contract_fixes_hasito_fifo_mapping_and_sync_deadline(self):
        contract = (Path(__file__).resolve().parents[2] / 'contracts/desk-politica-v1.md').read_text()
        for rule in ('`MINI_LOCK`', 'FIFO por lock', 'no salta de carril',
                     '`desk.lock` = carril 1', '15 min', 'sync-timeout', '--sync-ready-fd',
                     'tres barridos', '50 ms', 'cadena de fork y salida'):
            self.assertTrue(rule in contract, 'Missing contract rule: ' + rule)
