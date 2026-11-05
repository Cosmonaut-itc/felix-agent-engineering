"""Round-three lifecycle regressions, using real sessions and flocks."""
import contextlib
import io
import json
import multiprocessing as mp
import os
import sys
import time
import unittest
from unittest import mock

from deskpolicy import cli
from deskpolicy.engine import DeskPolicy
from deskpolicy.model import Invalid
from deskpolicy.store import Runtime, mutex, read_json
from deskpolicy.tests import test_policy as fixtures
from deskpolicy.tests.test_r2 import RegressionTest


def receipt_actor(harness, role, paused, replay_ready, finished, connection):
    real_atomic = cli.atomic_json
    writes = 0

    def controlled(path, value, **kwargs):
        nonlocal writes
        if path.name == 'pao-admission.json':
            writes += 1
            if role == 'first' and writes == 1:
                paused.set()
                # Old code lets the replay pass here. Correct code holds the
                # mutex, so this bounded pause simply delays both callers.
                replay_ready.wait(.5)
            if role == 'second':
                replay_ready.set()
                finished.wait(2)
        real_atomic(path, value, **kwargs)
        if role == 'first' and writes == 2:
            finished.set()
    with mock.patch.object(cli, 'atomic_json', side_effect=controlled):
        fixtures.cli_actor(harness, ['ejecutar', '--request-id', 'race', '--run-id',
            'run-race', '--lane', '1', '--recibo', '--', '/bin/true'], connection)


def slow_final_actor(harness, arguments, closed, connection):
    real_atomic = cli.atomic_json
    def controlled(path, value, **kwargs):
        if path.name == 'pao-admission.json' and value.get('claim_state') == 'closed':
            closed.set()
            time.sleep(.5)
        real_atomic(path, value, **kwargs)
    with mock.patch.object(cli, 'atomic_json', side_effect=controlled):
        fixtures.cli_actor(harness, arguments, connection)


def failing_close_actor(harness, arguments, failure, connection):
    engine = DeskPolicy(Runtime(harness))
    stream, errors = io.StringIO(), io.StringIO()
    os.setsid()
    with mock.patch.object(cli, 'DeskPolicy', return_value=engine), \
         mock.patch.object(engine, 'close', side_effect=failure), \
         contextlib.redirect_stdout(stream), contextlib.redirect_stderr(errors):
        code = cli.main(arguments)
    connection.send({'rc': code, 'output': stream.getvalue(), 'stderr': errors.getvalue()})


class RoundThreeTest(unittest.TestCase):
    setUp = fixtures.PolicyTest.setUp
    tearDown = fixtures.PolicyTest.tearDown
    pending = fixtures.PolicyTest.pending
    publish = fixtures.PolicyTest.publish
    renew = fixtures.PolicyTest.renew
    advance = fixtures.PolicyTest.advance
    launch = fixtures.PolicyTest.launch
    run_cli = fixtures.PolicyTest.run_cli
    receipt_directory = RegressionTest.receipt_directory
    execute_args = RegressionTest.execute_args

    def spawn(self, target, *args):
        parent, child = mp.get_context('fork').Pipe()
        worker = mp.get_context('fork').Process(target=target, args=(*args, child))
        worker.start()
        child.close()
        self.children.append((worker, parent))
        return worker, parent

    def result(self, pair):
        worker, parent = pair
        self.assertTrue(parent.poll(30), 'wrapper timeout')
        value = parent.recv()
        worker.join(5)
        self.assertEqual(worker.exitcode, 0)
        return value

    def test_w11_replay_never_overwrites_terminal_receipt(self):
        self.pending('race')
        self.publish()
        directory = self.receipt_directory('race')
        paused, replay_ready, finished = [mp.get_context('fork').Event() for _ in range(3)]
        first = self.spawn(receipt_actor, self.h, 'first', paused, replay_ready, finished)
        self.assertTrue(paused.wait(10))
        second = self.spawn(receipt_actor, self.h, 'second', paused, replay_ready, finished)
        self.assertEqual(self.result(first)['rc'], 0)
        self.assertEqual(self.result(second)['rc'], 0)
        receipt = read_json(directory / 'pao-admission.json')
        self.assertEqual((receipt['claim_state'], receipt['script_exit_code'], receipt['close_cause']),
                         ('closed', 0, 'completed'))

    def test_wc_census_during_final_receipt_has_no_untracked_lock(self):
        self.pending('concurrent')
        self.publish()
        self.receipt_directory('concurrent')
        closed = mp.get_context('fork').Event()
        worker = self.spawn(slow_final_actor, self.h,
                            self.execute_args('concurrent', ['/bin/true']), closed)
        self.assertTrue(closed.wait(10))
        self.engine.status()
        self.engine.publish()
        self.assertEqual(self.result(worker)['rc'], 0)

    def test_w12_real_close_mutex_timeout_preserves_exit(self):
        self.pending('timeout')
        self.publish()
        directory = self.receipt_directory('timeout')
        ready, proceed = self.h.directory / 'ready', self.h.directory / 'proceed'
        program = ('import pathlib,time; pathlib.Path({!r}).touch();\n'
                   'while not pathlib.Path({!r}).exists(): time.sleep(.01)\n'
                   'raise SystemExit(3)').format(str(ready), str(proceed))
        worker = self.spawn(fixtures.cli_actor, self.h,
            self.execute_args('timeout', [sys.executable, '-I', '-c', program]))
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(ready.exists())
        with mutex(self.h.root):
            proceed.touch()
            time.sleep(6)
        result = self.result(worker)
        receipt = read_json(directory / 'pao-admission.json')
        print('R3 timeout', result['rc'], receipt, flush=True)
        self.assertEqual(result['rc'], 3)
        self.assertEqual(receipt['script_exit_code'], 3)
        self.assertEqual(receipt['claim_state'], 'uncertain')
        self.assertEqual(self.engine.status()['weight'], 1)

    def test_n2_other_close_failures_preserve_exit_and_diagnostic(self):
        for index, failure in enumerate((Invalid('process-owner'), OSError('disk-error'),
                                         RuntimeError('unexpected-close'))):
            name = 'failure' + str(index)
            with self.subTest(failure=str(failure)):
                self.pending(name)
                self.publish()
                directory = self.receipt_directory(name)
                args = self.execute_args(name, ['/bin/sh', '-c', 'exit 3'])
                args[args.index('--lane') + 1] = str(index + 1)
                result = self.result(self.spawn(failing_close_actor, self.h, args, failure))
                self.assertEqual(result['rc'], 3)
                receipt = read_json(directory / 'pao-admission.json')
                self.assertEqual((receipt['script_exit_code'], receipt['claim_state']), (3, 'uncertain'))
                diagnostic = json.loads(result['stderr'])
                self.assertEqual((diagnostic['schema'], diagnostic['detail']),
                                 ('pao/desk-error@1', str(failure)))
                self.h.advance(120)
                self.renew()

    def test_wa_reconcile_dead_day_claim_restores_night_cpus(self):
        self.h.now = self.h.now.replace(hour=16)
        self.pending('day')
        receipt, worker, _ = self.launch('day')
        self.assertEqual(len(receipt['cpus']), 12)
        worker.kill()
        worker.join(5)
        self.h.advance(14 * 3600)
        self.pending('night')
        # Writer reconciles before publication, and does not change arm data.
        self.engine.publish()
        self.assertEqual(read_json(self.engine.record_path('day'))['claim']['close_cause'], 'reconciled')
        admitted, _, _ = self.launch('night', 2)
        self.assertTrue(admitted['execute'], admitted)

    def test_wa_reconcile_keeps_child_and_life_lock_then_closes(self):
        self.pending('child')
        self.publish()
        _, worker, connection = self.launch('child')
        connection.send(('child',))
        self.assertTrue(connection.poll(10))
        pid = connection.recv()
        from deskpolicy.store import process
        self.extra_pids.append((pid, process(pid)['start']))
        worker.kill()
        worker.join(5)
        self.engine.reconcile()
        self.assertEqual(read_json(self.engine.record_path('child'))['claim']['state'], 'uncertain')
        os.kill(pid, 15)
        deadline = time.monotonic() + 10
        from deskpolicy.store import session_members
        while session_members(worker.pid) and time.monotonic() < deadline:
            time.sleep(.02)
        self.engine.reconcile()
        self.assertEqual(read_json(self.engine.record_path('child'))['claim']['state'], 'closed')

    def test_wb_any_chooses_second_lane_after_uncertain_first(self):
        self.pending('old')
        self.pending('next')
        self.publish()
        _, worker, _ = self.launch('old')
        worker.kill()
        worker.join(5)
        self.h.advance(120)
        self.renew()
        self.receipt_directory('next')
        args = self.execute_args('next', ['/bin/true'])
        args[args.index('--lane') + 1] = 'any'
        result = self.run_cli(args)
        self.assertEqual(result['rc'], 0)
        receipt = read_json(self.h.directory / 'runs/run-next/pao-admission.json')
        self.assertEqual(receipt['lane'], 2)

    def test_n1_contract_documents_status_bootstrap_and_u2_notes(self):
        from pathlib import Path
        contract = (Path(__file__).resolve().parents[2] / 'contracts/desk-politica-v1.md').read_text()
        for required in ('idempotente', 'no crea claims', '/proc/*/cwd',
                         '.claude/skills/verify', 'cola del mismo hilo', 'fd 8 antes de soltar fd 9'):
            self.assertIn(required, contract)
