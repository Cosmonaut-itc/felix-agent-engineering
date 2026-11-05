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
from deskpolicy.tests import test_r2 as r2


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
            time.sleep(1)
        real_atomic(path, value, **kwargs)
    with mock.patch.object(cli, 'atomic_json', side_effect=controlled):
        fixtures.cli_actor(harness, arguments, connection)


def census_actor(harness, action, connection):
    engine = DeskPolicy(Runtime(harness), prepare=False)
    try:
        connection.send({'rc': 0, 'view': getattr(engine, action)()})
    except (OSError, ValueError) as exc:
        connection.send({'rc': 75, 'detail': str(exc)})


def failing_close_actor(harness, arguments, failure, connection, *, journal_failure=False):
    engine = DeskPolicy(Runtime(harness))
    stream, errors = io.StringIO(), io.StringIO()
    os.setsid()
    def fail_close(*args, **kwargs):
        if journal_failure:
            engine.write_record = mock.Mock(side_effect=OSError('journal-disk'))
        raise failure

    with mock.patch.object(cli, 'DeskPolicy', return_value=engine), \
         mock.patch.object(engine, 'close', side_effect=fail_close), \
         contextlib.redirect_stdout(stream), contextlib.redirect_stderr(errors):
        code = cli.main(arguments)
    connection.send({'rc': code, 'output': stream.getvalue(), 'stderr': errors.getvalue()})


def failed_journal_actor(harness, arguments, failure, connection):
    failing_close_actor(harness, arguments, failure, connection, journal_failure=True)


def any_admit_actor(harness, connection):
    from deskpolicy.store import open_file
    os.setsid()
    engine = DeskPolicy(Runtime(harness))
    fds = {lane: open_file(engine.life_path('hasito', lane), create=True) for lane in (1, 2, 3, 4)}
    try:
        stream = io.StringIO()
        with mutex(engine.root) as lock, mock.patch.object(cli, 'DeskPolicy', return_value=engine), \
             contextlib.redirect_stdout(stream):
            code = cli.main(['admitir', '--request-id', 'next', '--run-id', 'run-next',
                '--lane', 'any', '--pid', str(os.getpid()), '--mutex-fd', str(lock),
                '--life-fds', *['{}:{}'.format(n, fd) for n, fd in fds.items()]])
        connection.send({'rc': code, 'receipt': json.loads(stream.getvalue())})
        connection.recv()
    finally:
        for fd in fds.values():
            os.close(fd)


def keeper_child_actor(harness, connection):
    import subprocess
    from deskpolicy.store import open_file
    os.setsid()
    engine = DeskPolicy(Runtime(harness))
    life = open_file(engine.life_path('jale', 1), create=True)
    with mutex(engine.root) as lock:
        receipt = engine.admit('keeper-child', 'run-keeper-child', 1, os.getpid(), life, lock)
    keeper = os.fork()
    if keeper:
        os.close(life)
        os.waitpid(keeper, 0)
        return
    try:
        os.setsid()
        child = subprocess.Popen(['/usr/bin/taskset', '-c', '0', '/bin/sleep', '60'], close_fds=True)
        with mutex(engine.root) as lock:
            try:
                engine.close('keeper-child', 'run-keeper-child', os.getpid(), life, lock,
                             claim_token=receipt['claim_token'], script_exit_code=3)
            except Invalid as exc:
                connection.send((child.pid, str(exc)))
    finally:
        os.close(life)
        os._exit(0)


class RoundThreeTest(unittest.TestCase):
    setUp = fixtures.PolicyTest.setUp
    tearDown = fixtures.PolicyTest.tearDown
    pending = fixtures.PolicyTest.pending
    publish = fixtures.PolicyTest.publish
    renew = fixtures.PolicyTest.renew
    advance = fixtures.PolicyTest.advance
    launch = fixtures.PolicyTest.launch
    run_cli = fixtures.PolicyTest.run_cli
    receipt_directory = r2.RegressionTest.receipt_directory
    execute_args = r2.RegressionTest.execute_args

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
        readers = [self.spawn(census_actor, self.h, action) for action in ('status', 'publish')]
        for reader in readers:
            result = self.result(reader)
            self.assertEqual(result['rc'], 0, result)
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
        print('R3 timeout', {k: receipt.get(k) for k in
              ('script_exit_code', 'claim_state', 'close_error')}, 'wrapper_rc', result['rc'], flush=True)
        self.assertEqual(result['rc'], 3)
        self.assertEqual(receipt['script_exit_code'], 3)
        self.assertEqual(receipt['claim_state'], 'uncertain')
        self.assertEqual(self.engine.status()['weight'], 1)

    def check_close_failure(self, failure, target=failing_close_actor):
        name = 'failure'
        self.pending(name)
        self.publish()
        directory = self.receipt_directory(name)
        args = self.execute_args(name, ['/bin/sh', '-c', 'exit 3'])
        result = self.result(self.spawn(target, self.h, args, failure))
        self.assertEqual(result['rc'], 3)
        receipt = read_json(directory / 'pao-admission.json')
        self.assertEqual((receipt['script_exit_code'], receipt['claim_state']), (3, 'uncertain'))
        diagnostic = json.loads(result['stderr'].splitlines()[0])
        self.assertEqual((diagnostic['schema'], diagnostic['detail']),
                         ('pao/desk-error@1', str(failure)))

    def test_n2_process_owner_preserves_exit(self):
        self.check_close_failure(Invalid('process-owner'))

    def test_n2_oserror_preserves_exit(self):
        self.check_close_failure(OSError('disk-error'))

    def test_n2_unexpected_close_preserves_exit(self):
        self.check_close_failure(RuntimeError('unexpected-close'))

    def test_n2_receipt_survives_failure_to_persist_uncertainty(self):
        self.check_close_failure(OSError('close-disk'), failed_journal_actor)
        self.assertEqual(self.engine.status()['weight'], 1)

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
        self.pending('child', 'jale', 'verify')
        self.publish()
        _, worker, connection = self.launch('child')
        metadata = self.h.root / 'slot-0'
        metadata.mkdir(mode=0o700)
        (metadata / 'kind').write_text('verify')
        connection.send(('child',))
        self.assertTrue(connection.poll(10))
        pid = connection.recv()
        from deskpolicy.store import process
        self.extra_pids.append((pid, process(pid)['start']))
        worker.kill()
        worker.join(5)
        self.engine.reconcile()
        self.assertEqual(read_json(self.engine.record_path('child'))['claim']['state'], 'uncertain')
        self.assertTrue(metadata.exists())
        os.kill(pid, 15)
        deadline = time.monotonic() + 10
        from deskpolicy.store import session_members
        while session_members(worker.pid) and time.monotonic() < deadline:
            time.sleep(.02)
        self.engine.reconcile()
        self.assertEqual(read_json(self.engine.record_path('child'))['claim']['state'], 'closed')
        self.assertFalse(metadata.exists())
        self.assertEqual((self.h.root / '.pao-reconciled-child/kind').read_text(), 'verify')
        self.engine.publish()

    def test_wa_reconcile_checks_the_recorded_keeper_session(self):
        self.pending('keeper-child', 'jale', 'verify')
        self.publish()
        worker, pipe = self.spawn(keeper_child_actor, self.h)
        self.assertTrue(pipe.poll(10))
        pid, reason = pipe.recv()
        self.assertEqual(reason, 'close-unproven')
        from deskpolicy.store import process, session_members
        self.extra_pids.append((pid, process(pid)['start']))
        worker.join(5)
        self.engine.reconcile()
        claim = read_json(self.engine.record_path('keeper-child'))['claim']
        self.assertEqual((claim['state'], claim['script_exit_code']), ('uncertain', 3))
        os.kill(pid, 15)
        deadline = time.monotonic() + 10
        while session_members(claim['keeper_sid']) and time.monotonic() < deadline:
            time.sleep(.02)
        result = self.run_cli(['reconciliar'])
        self.assertEqual(result['rc'], 0)
        self.assertEqual(json.loads(result['output'])['closed'], ['keeper-child'])
        self.assertEqual(read_json(self.engine.record_path('keeper-child'))['claim']['script_exit_code'], 3)

    def test_wb_any_chooses_second_lane_after_uncertain_first(self):
        self.pending('old')
        self.pending('next')
        self.publish()
        _, worker, _ = self.launch('old')
        worker.kill()
        worker.join(5)
        for _ in range(4):
            self.h.advance(30)
            self.renew()
        self.receipt_directory('next')
        args = self.execute_args('next', ['/bin/true'])
        args[args.index('--lane') + 1] = 'any'
        result = self.run_cli(args)
        self.assertEqual(result['rc'], 0, result)
        receipt = read_json(self.h.directory / 'runs/run-next/pao-admission.json')
        self.assertEqual(receipt['lane'], 2)

    def test_wb_admitir_any_inherited_candidates_retain_selected_flock(self):
        self.pending('old')
        self.pending('next')
        self.publish()
        _, worker, _ = self.launch('old')
        worker.kill()
        worker.join(5)
        for _ in range(4):
            self.h.advance(30)
            self.renew()
        # Refresh the night lease without reconciling the deliberately empty
        # reservation: this test targets admission, not the writer tick.
        with mock.patch.object(self.engine, 'reconcile', create=True):
            self.publish()
        candidate, pipe = self.spawn(any_admit_actor, self.h)
        self.assertTrue(pipe.poll(10))
        result = pipe.recv()
        self.assertEqual((result['rc'], result['receipt']['lane']), (0, 2), result)
        from deskpolicy.store import held
        self.assertTrue(held(self.engine.life_path('hasito', 2)))
        self.assertFalse(held(self.engine.life_path('hasito', 1)))
        pipe.send('done')
        candidate.join(5)

    def test_disable_night_persists_before_a_failed_reconciliation(self):
        self.pending('broken')
        self.engine.record_path('broken').write_bytes(b'{')
        with self.assertRaises(ValueError):
            self.engine.publish(disable_night=True)
        self.assertFalse(read_json(self.h.config)['night_enabled'])

    def test_n1_contract_documents_status_bootstrap_and_u2_notes(self):
        from pathlib import Path
        contract = (Path(__file__).resolve().parents[2] / 'contracts/desk-politica-v1.md').read_text()
        for required in ('idempotente', 'no crea claims', '/proc/*/cwd',
                         '.claude/skills/verify', 'cola del mismo hilo', 'fd 8 antes de soltar fd 9'):
            self.assertTrue(required in contract, 'Missing contract rule: ' + required)
