"""Round-six regressions: deadlines, monotone recovery, lock order and handoff."""
import contextlib
import inspect
import json
import multiprocessing as mp
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from unittest import mock

from deskpolicy import cli
from deskpolicy.engine import DeskPolicy
from deskpolicy.model import Invalid
from deskpolicy.store import Runtime, held, mutex, open_file, process, read_json
from deskpolicy.tests import test_policy as fixtures
from deskpolicy.tests import test_r4 as r4


def bad_keeper_actor(harness, variant, connection):
    os.setsid()
    engine = DeskPolicy(Runtime(harness))
    life = open_file(engine.life_path('jale', 1), create=True)
    with mutex(engine.root) as lock:
        receipt = engine.admit('bad', 'run-bad', 1, os.getpid(), life, lock)
    reader, writer = os.pipe()
    keeper = os.fork()
    if keeper == 0:
        os.close(reader)
        os.setsid()
        r4.keep_fds(life, writer)
        # Both original C2 and C-r3 launch close_fds load before registration.
        command = ['/bin/sleep', '90'] if variant == 'C2' else [
            sys.executable, '-I', '-c', 'import time; time.sleep(90)']
        server = subprocess.Popen(command, close_fds=True)
        try:
            with mutex(engine.root) as lock:
                engine.handoff('bad', 'run-bad', os.getpid(), life, lock,
                               claim_token=receipt['claim_token'])
            outcome = 'accepted'
        except Invalid as exc:
            outcome = str(exc)
        os.write(writer, json.dumps([outcome, process(os.getpid()), process(server.pid)]).encode())
        os.close(writer)
        signal.pause()
        os._exit(0)
    os.close(writer)
    info = json.loads(os.read(reader, 4096))
    os.close(reader)
    if info[0] == 'accepted':
        os.close(life)
        life = None
    os.kill(keeper, signal.SIGKILL)
    os.waitpid(keeper, 0)
    connection.send(info)
    connection.recv()
    if life is not None:
        os.close(life)


class RoundSixTest(r4.RoundFourTest):
    run_cli = fixtures.PolicyTest.run_cli
    close_actor = fixtures.PolicyTest.close_actor
    renew = fixtures.PolicyTest.renew
    advance = fixtures.PolicyTest.advance

    def lane_pending(self, name, lane):
        # Run the same behavioral probes on the r5 artifact, before the API exists.
        kwargs = {'lane': lane} if 'lane' in inspect.signature(self.engine.register).parameters else {}
        return self.engine.register(name, 'hasito', 'script', 'test', 1, **kwargs)

    def claim(self, name):
        return read_json(self.engine.record_path(name))['claim']

    def sync_case(self, late):
        self.h.now = self.h.now.replace(hour=16)
        name = 'late' if late else 'timely'
        self.pending(name)
        directory = self.receipt_directory(name)
        marker = self.h.directory / (name + '.executed')
        reader, writer = os.pipe()
        worker = None
        worker_pid = None
        try:
            args = self.execute_args(name, [sys.executable, '-I', '-c',
                'import pathlib,sys; p=pathlib.Path(sys.argv[1]); p.open("a").write("once\\n")', str(marker)])
            at = args.index('--')
            args[at:at] = ['--sync-ready-fd', str(reader), '--sync-timeout-s', '3']
            worker, pipe = self.spawn(fixtures.cli_actor, self.h, args)
            worker_pid = worker.pid
            def waiting():
                if not (directory / 'pao-admission.json').exists():
                    return False
                wait = Path('/proc/{}/wchan'.format(worker.pid)).read_text()
                return 'poll_schedule_timeout' in wait or wait.strip() in ('do_select', '__pollwait')
            r4.until(waiting, 'control never reached production select')
            if late:
                os.kill(worker.pid, signal.SIGSTOP)
                _, status = os.waitpid(worker.pid, os.WUNTRACED)
                self.assertTrue(os.WIFSTOPPED(status))
                time.sleep(3.3)
            os.write(writer, b'1')
            if late:
                os.kill(worker.pid, signal.SIGCONT)
            result = self.result((worker, pipe))
            receipt = read_json(directory / 'pao-admission.json')
            print('R6 SYNC', late, result['rc'], receipt, 'ran', marker.exists(), flush=True)
            self.assertEqual(result['rc'], 75 if late else 0)
            self.assertEqual((receipt['claim_state'], receipt['close_cause'], receipt['script_exit_code']),
                             ('closed', 'sync-timeout', None) if late else ('closed', 'completed', 0))
            self.assertEqual(marker.read_text() if marker.exists() else '', '' if late else 'once\n')
            self.assertEqual(self.engine.status()['weight'], 0)
            self.assertFalse(held(self.engine.life_path('hasito', 1)))
            self.assertEqual(self.run_cli(args)['rc'], 0)
            self.assertEqual(marker.read_text() if marker.exists() else '', '' if late else 'once\n')
        finally:
            if worker is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(worker_pid, signal.SIGCONT)
            os.close(reader)
            os.close(writer)

    def test_late_ack_after_sigstop_never_executes(self):
        self.sync_case(True)

    def test_timely_ack_executes_exactly_once(self):
        self.sync_case(False)

    def test_eof_closes_immediately_with_long_deadline(self):
        self.pending('eof')
        self.receipt_directory('eof')
        reader, writer = os.pipe()
        os.close(writer)
        try:
            args = self.execute_args('eof', ['/bin/true'])
            at = args.index('--')
            args[at:at] = ['--sync-ready-fd', str(reader), '--sync-timeout-s', '8']
            worker, pipe = self.spawn(fixtures.cli_actor, self.h, args)
            self.assertTrue(pipe.poll(4), 'EOF retained capacity until the sync deadline')
            result = self.result((worker, pipe))
            self.assertEqual(result['rc'], 75)
            self.assertEqual(self.claim('eof')['close_cause'], 'sync-timeout')
            self.assertIsNone(self.claim('eof')['script_exit_code'])
            self.assertEqual(self.engine.status()['weight'], 0)
            self.assertFalse(held(self.engine.life_path('hasito', 1)))
        finally:
            os.close(reader)

    def test_cli_rejects_sync_timeout_over_900_before_claim(self):
        self.pending('long')
        self.receipt_directory('long')
        args = self.execute_args('long', ['/bin/true'])
        at = args.index('--')
        args[at:at] = ['--sync-timeout-s', '900.1']
        result = self.run_cli(args)
        self.assertEqual(result['rc'], 75, result)
        self.assertIsNone(self.claim('long'))

    def test_original_receipt_failure_reconcile_and_replay_recover_exit(self):
        self.pending('recovery')
        directory = self.receipt_directory('recovery')
        marker = self.h.directory / 'executions'
        pidfile = self.h.directory / 'child.pid'
        program = ('import subprocess,pathlib,sys; pathlib.Path(sys.argv[1]).open("a").write("once\\n"); '
                   'p=subprocess.Popen(["/bin/sleep","1.5"],close_fds=True); '
                   'pathlib.Path(sys.argv[2]).write_text(str(p.pid)); raise SystemExit(3)')
        args = self.execute_args('recovery', [sys.executable, '-I', '-c', program, str(marker), str(pidfile)])
        atomic = cli.atomic_json
        writes = mp.get_context('fork').Value('i', 0)
        def transient(path, value, **kwargs):
            if path.name == 'pao-admission.json':
                with writes.get_lock():
                    writes.value += 1
                if writes.value > 1:
                    raise OSError('receipt-volume-unavailable')
            return atomic(path, value, **kwargs)
        with mock.patch.object(cli, 'atomic_json', side_effect=transient):
            result = self.run_cli(args)
        self.assertEqual(result['rc'], 3, result)
        self.assertEqual(writes.value, 2)
        self.assertEqual((self.claim('recovery')['state'], self.claim('recovery')['script_exit_code']),
                         ('uncertain', 3))
        initial = read_json(directory / 'pao-admission.json')
        self.assertEqual((initial['claim_state'], initial['script_exit_code']), ('active', None))
        os.waitpid(int(pidfile.read_text()), 0)
        r4.until(lambda: (self.engine.reconcile(), self.claim('recovery')['state'])[1] == 'closed',
                 'known exit never reconciled')
        reconciled = read_json(directory / 'pao-admission.json')
        replay = self.run_cli(args)
        final = read_json(directory / 'pao-admission.json')
        print('R6 RECOVERY', reconciled, final, flush=True)
        self.assertEqual((reconciled['claim_state'], reconciled['close_cause'], reconciled['script_exit_code']),
                         ('closed', 'reconciled', 3))
        self.assertEqual(final['script_exit_code'], 3)
        self.assertEqual(replay['rc'], 0)
        self.assertFalse(json.loads(replay['output'])['execute'])
        self.assertEqual(marker.read_text(), 'once\n')
        for key in initial.keys() - {'claim_state', 'script_exit_code', 'close_cause', 'close_error'}:
            self.assertEqual(initial[key], final[key], key)

    def test_both_final_publications_fail_then_reconcile_closed_claim(self):
        self.pending('two-writes')
        directory = self.receipt_directory('two-writes')
        args = self.execute_args('two-writes', ['/bin/sh', '-c', 'exit 3'])
        atomic = cli.atomic_json
        writes = mp.get_context('fork').Value('i', 0)
        def transient(path, value, **kwargs):
            if path.name == 'pao-admission.json':
                with writes.get_lock():
                    writes.value += 1
                if writes.value > 1:
                    raise OSError('both-terminal-publications-unavailable')
            return atomic(path, value, **kwargs)
        with mock.patch.object(cli, 'atomic_json', side_effect=transient):
            result = self.run_cli(args)
        self.assertEqual(result['rc'], 3)
        self.assertEqual(writes.value, 3)
        self.assertEqual(self.claim('two-writes')['script_exit_code'], 3)
        path = directory / 'pao-admission.json'
        self.assertEqual((read_json(path)['claim_state'], read_json(path)['script_exit_code']), ('active', None))
        self.engine.reconcile()
        self.assertEqual((read_json(path)['claim_state'], read_json(path)['script_exit_code']), ('closed', 3))
        self.assertEqual(self.run_cli(args)['rc'], 0)
        self.assertEqual(read_json(path)['script_exit_code'], 3)

    def test_closed_receipt_unknown_fields_fill_without_replacing_known_values(self):
        self.pending('closed')
        directory = self.receipt_directory('closed')
        args = self.execute_args('closed', ['/bin/sh', '-c', 'exit 3'])
        self.assertEqual(self.run_cli(args)['rc'], 3)
        path = directory / 'pao-admission.json'
        original = read_json(path)
        damaged = {**original, 'script_exit_code': None}
        with mutex(self.engine.root):
            cli.atomic_json(path, damaged)
        self.assertEqual(self.run_cli(args)['rc'], 0)
        self.assertEqual(read_json(path), original)
        # A known result and identity cannot be replaced, even by durable data.
        known = {**original, 'script_exit_code': 7, 'close_cause': 'stopped'}
        with mutex(self.engine.root):
            cli.atomic_json(path, known)
            self.engine.reconcile_receipt(read_json(self.engine.record_path('closed')))
        self.assertEqual(read_json(path), known)
        self.assertEqual(self.run_cli(args)['rc'], 0)
        self.assertEqual(read_json(path), known)

    def local_first_case(self, suspension):
        import fcntl
        local = self.h.directory / 'desk.lock'
        owner = open_file(local, create=True)
        waiter = open_file(local)
        try:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.lane_pending('B1', 1)
            if suspension:
                self.h.advance(121)
                self.engine.publish()
            else:
                self.receipt_directory('B1')
                args = self.execute_args('B1', ['/bin/true'])
                reader, writer = os.pipe()
                at = args.index('--')
                args[at:at] = ['--sync-ready-fd', str(reader), '--sync-timeout-s', '.1']
                try:
                    self.assertEqual(self.run_cli(args)['rc'], 75)
                finally:
                    os.close(reader)
                    os.close(writer)
            # A queues locally and cannot register at PAO while B owns the lock.
            with self.assertRaises(BlockingIOError):
                fcntl.flock(waiter, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertFalse(self.engine.record_path('A').exists())
            self.lane_pending('B2', 1)
            self.h.now = self.h.now.replace(hour=16)
            receipt, worker, pipe = self.launch('B2')
            self.assertTrue(receipt['execute'], receipt)
            self.close_actor(worker, pipe)
            print('R6 LOCAL-FIRST', 'suspension' if suspension else 'sync-timeout', receipt['state'], flush=True)
            contract = (Path(__file__).resolve().parents[2] / 'contracts/desk-politica-v1.md').read_text()
            self.assertIn('Sólo el poseedor del lock local registra', contract)
        finally:
            os.close(owner)
            os.close(waiter)

    def test_local_first_sync_timeout_owner_advances(self):
        self.local_first_case(False)

    def test_local_first_suspension_owner_advances(self):
        self.local_first_case(True)

    def test_busy_lane_head_does_not_block_free_hasito_lane(self):
        self.h.now = self.h.now.replace(hour=16)
        self.lane_pending('X', 1)
        receipt, worker, pipe = self.launch('X')
        self.assertTrue(receipt['execute'])
        # Unknown live child retains remote lane 1 after X releases its local lock.
        pipe.send(('child', [0, 1]))
        self.assertTrue(pipe.poll(60))
        pid = pipe.recv()
        self.extra_pids.append((pid, process(pid)['start']))
        worker.kill()
        worker.join(60)
        self.lane_pending('A', 1)
        denied, _, _ = self.launch('A')
        self.assertEqual(denied['reason'], 'resource-busy')
        self.lane_pending('B', 2)
        admitted, other, connection = self.launch('B', 2)
        print('R6 HOL', denied['reason'], admitted['state'], admitted['reason'], flush=True)
        self.assertTrue(admitted['execute'], admitted)
        self.close_actor(other, connection)

    def test_pending_lane_is_immutable_and_admission_matches(self):
        self.lane_pending('bound', 2)
        with self.assertRaisesRegex(Invalid, 'request-conflict'):
            self.lane_pending('bound', 1)
        with self.assertRaisesRegex(Invalid, 'request-conflict'):
            # Wrong lane is rejected before taking any life flock.
            with mutex(self.engine.root) as lock:
                self.engine.admit('bound', 'run-bound', 1, os.getpid(), lambda _: None, lock)
        self.assertIsNone(self.claim('bound'))

    def bad_keeper_case(self, variant):
        self.h.now = self.h.now.replace(hour=16)
        self.pending('bad', 'jale', 'verify')
        worker, pipe = self.spawn(bad_keeper_actor, self.h, variant)
        self.assertTrue(pipe.poll(60))
        outcome, keeper, server = pipe.recv()
        self.extra_pids.append((server['pid'], server['start']))
        self.assertEqual(process(server['pid'])['start'], server['start'])
        reconciled = self.engine.reconcile()
        self.pending('replacement', 'jale', 'verify')
        receipt, _, _ = self.launch('replacement')
        print('R6 KEEPER', variant, outcome, 'closed', reconciled['closed'], 'replacement', receipt, flush=True)
        self.assertEqual(outcome, 'handoff-load-before-registration')
        self.assertTrue(held(self.engine.life_path('jale', 1)))
        self.assertEqual(reconciled['closed'], [])
        self.assertEqual(receipt['reason'], 'resource-busy')
        self.assertEqual(self.claim('bad')['keepers'], [])
        pipe.send('finish')
        worker.join(60)

    def test_original_c2_load_before_registration_rejected(self):
        self.bad_keeper_case('C2')

    def test_original_cr3_load_before_registration_rejected(self):
        self.bad_keeper_case('C-r3')

    def test_contract_names_turn_files_and_no_active_turn_renewal(self):
        contract = (Path(__file__).resolve().parents[2] / 'contracts/desk-politica-v1.md').read_text()
        for text in ('<lock>.turnos.json', '<lock>.turnos.lock', 'bitácora',
                     'un turno activo no se renueva', 'salvo que `admitir` la reconcilie'):
            self.assertIn(text, contract)
        self.assertNotIn('renovando tanto el registro local', contract)
