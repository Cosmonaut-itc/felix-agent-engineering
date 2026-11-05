"""Round-four handoff and negative-proof regressions on real Linux processes."""
import contextlib
import io
import json
import multiprocessing as mp
import os
from pathlib import Path
import signal
import subprocess
import time
import unittest
from unittest import mock

from deskpolicy import cli
from deskpolicy.engine import DeskPolicy
from deskpolicy.model import Invalid
from deskpolicy.store import Runtime, held, mutex, open_file, process, read_json, session_members
from deskpolicy.tests import test_policy as fixtures
from deskpolicy.tests import test_r3 as r3

CTX = mp.get_context('fork')


def keep_fds(*descriptors):
    for entry in Path('/proc/self/fd').iterdir():
        fd = int(entry.name)
        if fd > 2 and fd not in descriptors:
            with contextlib.suppress(OSError):
                os.close(fd)


def until(check, label):
    deadline = time.monotonic() + 60
    while not check():
        if time.monotonic() >= deadline:
            raise AssertionError(label)
        time.sleep(.01)


def register_keeper(engine, receipt, life):
    # The old runtime has no handoff operation. Continue its original protocol
    # so baseline failures show the unsafe closure, rather than AttributeError.
    if hasattr(engine, 'handoff'):
        reader, writer = os.pipe()
        os.write(writer, receipt['claim_token'].encode('ascii'))
        os.close(writer)
        stream = io.StringIO()
        try:
            with mutex(engine.root) as lock, mock.patch.object(cli, 'DeskPolicy', return_value=engine), \
                 contextlib.redirect_stdout(stream):
                code = cli.main(['traspasar', '--request-id', receipt['request_id'],
                    '--run-id', receipt['run_id'], '--pid', str(os.getpid()),
                    '--mutex-fd', str(lock), '--life-fd', str(life), '--claim-token-fd', str(reader)])
            assert code == 0, stream.getvalue()
            ack = json.loads(stream.getvalue())
            assert ack['schema'] == 'pao/desk-handoff@1' and 'claim_token' not in ack
        finally:
            os.close(reader)
        with mutex(engine.root) as lock:
            engine.handoff(receipt['request_id'], receipt['run_id'], os.getpid(), life,
                           lock, claim_token=receipt['claim_token'])


def handoff_actor(harness, count, register, connection):
    os.setsid()
    engine = DeskPolicy(Runtime(harness))
    life = open_file(engine.life_path('jale', 1), create=True)
    with mutex(engine.root) as lock:
        receipt = engine.admit('handoff', 'run-handoff', 1, os.getpid(), life, lock)
    connection.send(('launcher', process(os.getpid())))
    descendants = []
    try:
        for index in range(count):
            ready_r, ready_w = os.pipe()
            keeper = os.fork()
            if keeper:
                os.close(ready_w)
                assert os.read(ready_r, 1) == b'1'
                os.close(ready_r)
                os.close(life)
                os._exit(0)
            os.close(ready_r)
            os.setsid()
            keep_fds(life, connection.fileno(), ready_w)
            if register:
                register_keeper(engine, receipt, life)
            # Registration ACK precedes both launcher release and any load.
            os.write(ready_w, b'1')
            os.close(ready_w)
            server = subprocess.Popen(['/bin/sleep', '90'], close_fds=True)
            descendants = [server]
            connection.send(('keeper', process(os.getpid()), process(server.pid)))
        connection.recv()
        for server in descendants:
            server.terminate()
            server.wait()
        with mutex(engine.root) as lock:
            try:
                closed = engine.close('handoff', 'run-handoff', os.getpid(), life, lock,
                                      claim_token=receipt['claim_token'], script_exit_code=3)
                connection.send(('closed', closed['claim']['state']))
            except Invalid as exc:
                connection.send(('closed', str(exc)))
            finally:
                os.close(life)
    finally:
        os._exit(0)


def fork_load(connection):
    connection.send(process(os.getpid()))
    connection.recv()
    child = subprocess.Popen(['/bin/sleep', '90'], close_fds=True)
    connection.send(process(child.pid))
    os._exit(0)


def fork_actor(harness, stage, connection):
    os.setsid()
    engine = DeskPolicy(Runtime(harness))
    life = open_file(engine.life_path('hasito', 1), create=True)
    with mutex(engine.root) as lock:
        engine.admit('fork', 'run-fork', 1, os.getpid(), life, lock)
    child = os.fork()
    if child == 0:
        os.close(life)
        keep_fds(stage.fileno())
        fork_load(stage)
    stage.close()
    os.close(life)


def close_race_actor(harness, connection):
    os.setsid()
    engine = DeskPolicy(Runtime(harness))
    life = open_file(engine.life_path('hasito', 1), create=True)
    with mutex(engine.root) as lock:
        receipt = engine.admit('fork', 'run-fork', 1, os.getpid(), life, lock)
    parent, child = CTX.Pipe()
    pid = os.fork()
    if pid == 0:
        os.close(life)
        keep_fds(child.fileno())
        fork_load(child)
    child.close()
    assert parent.poll(60)
    stage = parent.recv()
    real_iterdir = Path.iterdir
    fired, children = False, []

    def pause(path):
        nonlocal fired
        if path == Path('/proc') and not fired:
            fired = True
            entries = list(real_iterdir(path))
            parent.send('fork')
            assert parent.poll(60)
            children.append(parent.recv())
            os.waitpid(stage['pid'], 0)
            return iter(entries)
        return real_iterdir(path)

    try:
        with mutex(engine.root) as lock, mock.patch.object(Path, 'iterdir', pause):
            try:
                engine.close('fork', 'run-fork', os.getpid(), life, lock,
                             claim_token=receipt['claim_token'], script_exit_code=3)
            except Invalid as exc:
                assert str(exc) == 'close-unproven'
        claim = read_json(engine.record_path('fork'))['claim']
        connection.send({'state': claim['state'], 'code': claim['script_exit_code'], 'children': children})
    finally:
        os.close(life)
        parent.close()


class RoundFourTest(unittest.TestCase):
    def setUp(self):
        import ctypes
        # Adopt orphaned test servers; PID 1 reaping must not drive the proof.
        self.assertEqual(ctypes.CDLL(None).prctl(36, 1, 0, 0, 0), 0)
        fixtures.PolicyTest.setUp(self)

    def tearDown(self):
        fixtures.PolicyTest.tearDown(self)
        for pid, _ in self.extra_pids:
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, 0)

    pending = fixtures.PolicyTest.pending
    publish = fixtures.PolicyTest.publish
    launch = fixtures.PolicyTest.launch
    receipt_directory = r3.RoundThreeTest.receipt_directory
    execute_args = r3.RoundThreeTest.execute_args
    spawn = r3.RoundThreeTest.spawn
    result = r3.RoundThreeTest.result


class LifecycleTest(RoundFourTest):
    def claim(self, name):
        return read_json(self.engine.record_path(name))['claim']

    def keeper_case(self, count, registered=True, clean_close=False, previous_live=False):
        self.pending('handoff', 'jale', 'verify')
        self.publish()
        worker, pipe = self.spawn(handoff_actor, self.h, count, registered)
        self.assertTrue(pipe.poll(60))
        role, launcher = pipe.recv()
        self.assertEqual(role, 'launcher')
        keepers, servers = [], []
        for _ in range(count):
            self.assertTrue(pipe.poll(60))
            role, keeper, server = pipe.recv()
            self.assertEqual(role, 'keeper')
            keepers.append(keeper)
            servers.append(server)
            self.extra_pids.extend((p['pid'], p['start']) for p in (keeper, server))
        self.assertTrue(held(self.engine.life_path('jale', 1)))
        worker.join(60)
        for identity in keepers[:-1]:
            os.waitpid(identity['pid'], 0)
        if clean_close:
            if not previous_live:
                for identity in servers[:-1]:
                    os.kill(identity['pid'], signal.SIGTERM)
                    os.waitpid(identity['pid'], 0)
            pipe.send('drain')
            self.assertTrue(pipe.poll(60))
            self.assertEqual(pipe.recv(), ('closed', 'close-unproven' if previous_live else 'closed'))
            self.assertEqual(self.claim('handoff')['state'], 'uncertain' if previous_live else 'closed')
            worker.join(60)
            self.assertEqual(self.claim('handoff')['script_exit_code'], 3)
            return
        # The last keeper dies before its first close. Prior sessions still
        # contain their own close_fds servers in a second handoff.
        os.kill(keepers[-1]['pid'], signal.SIGKILL)
        os.waitpid(keepers[-1]['pid'], 0)
        if count > 1:
            os.kill(servers[-1]['pid'], signal.SIGTERM)
            os.waitpid(servers[-1]['pid'], 0)
        until(lambda: not held(self.engine.life_path('jale', 1)), 'life lock release')
        result = self.engine.reconcile()
        print('R4 keeper', count, registered, result, self.claim('handoff')['state'], flush=True)
        self.assertEqual(result['closed'], [])
        self.assertEqual(self.claim('handoff')['state'], 'uncertain')
        self.h.advance(120)
        self.pending('replacement', 'jale', 'verify')
        self.publish()
        denied, _, _ = self.launch('replacement')
        self.assertEqual(denied['reason'], 'resource-busy')
        if registered:
            durable = self.claim('handoff')['keepers']
            self.assertEqual([(p['pid'], p['start'], p['sid']) for p in durable],
                             [(p['pid'], p['start'], p['sid']) for p in keepers])
            for server in (servers[:-1] if count > 1 else servers):
                os.kill(server['pid'], signal.SIGTERM)
            # A zombie remains a member until reaped. Our fixture is a subreaper.
            for identity in keepers + servers:
                with contextlib.suppress(ChildProcessError):
                    os.waitpid(identity['pid'], 0)
            until(lambda: self.engine.reconcile()['closed'] == ['handoff'], 'all sessions drained')
            self.assertEqual(self.claim('handoff')['close_cause'], 'reconciled')

    def test_c2_keeper_death_before_first_close(self):
        self.keeper_case(1)

    def test_c2_second_handoff_tracks_every_session(self):
        self.keeper_case(2)

    def test_c2_missing_keeper_registration_never_reconciles(self):
        self.keeper_case(1, registered=False)

    def test_c2_close_checks_previous_keeper_session(self):
        self.keeper_case(2, clean_close=True, previous_live=True)

    def test_c2_close_after_all_handoff_sessions_drained(self):
        self.keeper_case(2, clean_close=True)

    def test_c3_fork_during_reconcile_snapshot(self):
        self.pending('fork')
        self.publish()
        stage_parent, stage_child = CTX.Pipe()
        worker, _ = self.spawn(fork_actor, self.h, stage_child)
        stage_child.close()
        self.assertTrue(stage_parent.poll(60))
        stage = stage_parent.recv()
        self.extra_pids.append((stage['pid'], stage['start']))
        worker.join(60)
        self.assertEqual(worker.exitcode, 0)
        real_iterdir = Path.iterdir
        fired = False

        def pause(path):
            nonlocal fired
            if path == Path('/proc') and not fired:
                fired = True
                entries = list(real_iterdir(path))
                stage_parent.send('fork')
                self.assertTrue(stage_parent.poll(60))
                descendant = stage_parent.recv()
                self.extra_pids.append((descendant['pid'], descendant['start']))
                os.waitpid(stage['pid'], 0)
                self.assertIn(Path('/proc') / str(stage['pid']), entries)
                self.assertNotIn(Path('/proc') / str(descendant['pid']), entries)
                return iter(entries)
            return real_iterdir(path)

        try:
            with mock.patch.object(Path, 'iterdir', pause):
                result = self.engine.reconcile()
            print('R4 fork reconcile', result, self.claim('fork')['state'], flush=True)
            self.assertTrue(fired)
            self.assertEqual(result['closed'], [])
            self.assertEqual(self.claim('fork')['state'], 'uncertain')
        finally:
            stage_parent.close()

    def test_c3_fork_during_close_snapshot(self):
        self.pending('fork')
        self.publish()
        result = self.result(self.spawn(close_race_actor, self.h))
        for identity in result['children']:
            self.extra_pids.append((identity['pid'], identity['start']))
        print('R4 fork close', result, flush=True)
        self.assertEqual((result['state'], result['code']), ('uncertain', 3))
        self.assertEqual(self.claim('fork')['script_exit_code'], 3)

    def test_na_reconcile_updates_fixed_receipt_without_replay(self):
        self.pending('receipt')
        self.publish()
        directory = self.receipt_directory('receipt')
        # Stop the wrapper at the close boundary to persist an uncertain result.
        from deskpolicy.tests.test_r3 import failing_close_actor
        result = self.result(self.spawn(failing_close_actor, self.h,
            self.execute_args('receipt', ['/bin/sh', '-c', 'exit 3']), Invalid('close-unproven')))
        self.assertEqual(result['rc'], 3)
        self.assertEqual(self.engine.reconcile()['closed'], ['receipt'])
        receipt = read_json(directory / 'pao-admission.json')
        print('R4 reconciled receipt', receipt['claim_state'], receipt['script_exit_code'], receipt['close_cause'], flush=True)
        self.assertEqual((receipt['claim_state'], receipt['script_exit_code'], receipt['close_cause']),
                         ('closed', 3, 'reconciled'))
        self.assertTrue(receipt['execute'])
        self.assertFalse(receipt['replay'])

    def test_nb_close_diagnostic_is_separate_and_stdout_single_line(self):
        self.pending('diagnostic')
        self.publish()
        self.receipt_directory('diagnostic')
        from deskpolicy.tests.test_r3 import failing_close_actor
        result = self.result(self.spawn(failing_close_actor, self.h,
            self.execute_args('diagnostic', ['/bin/sh', '-c', 'exit 3']), Invalid('close-unproven')))
        self.assertEqual(result['rc'], 3)
        diagnostic = json.loads(result['stderr'].splitlines()[0])
        print('R4 diagnostic', diagnostic, 'stdout_lines', len(result['output'].splitlines()), flush=True)
        self.assertEqual(diagnostic['schema'], 'pao/desk-close-diagnostic@1')
        self.assertNotIn('exit_code', diagnostic)
        self.assertEqual(diagnostic['script_exit_code'], 3)
        self.assertEqual(len(result['output'].splitlines()), 1)

    def test_wr3_hasito_contract_requires_local_explicit_lane_before_sync(self):
        contract = (Path(__file__).resolve().parents[2] / 'contracts/desk-politica-v1.md').read_text()
        self.assertFalse('La admisión Hasito usa normativamente `--lane any`' in contract)
        for text in ('--desk-lane N', '--lane N', 'sin sincronizar', 'menor lock local libre'):
            self.assertTrue(text in contract, 'Missing contract rule: ' + text)


class SessionProofTest(unittest.TestCase):
    def probe(self, identities, sid=987654321):
        from deskpolicy import store
        entry = Path('/proc') / str(os.getpid())
        with mock.patch.object(Path, 'iterdir', return_value=iter([entry])), \
             mock.patch.object(store, 'process', side_effect=identities):
            return session_members(sid)

    def test_c3_disappearing_pid_is_inconclusive(self):
        self.assertIsNone(self.probe([FileNotFoundError('vanished')]))

    def test_c3_stat_read_failure_is_inconclusive(self):
        self.assertIsNone(self.probe([PermissionError('unreadable')]))

    def test_c3_identity_change_is_inconclusive(self):
        identity = process(os.getpid())
        self.assertIsNone(self.probe([identity, {**identity, 'start': identity['start'] + 1}]))

    def test_c3_zombie_counts_until_reaped(self):
        identity = {**process(os.getpid()), 'zombie': True}
        self.assertEqual(self.probe([identity, identity], identity['sid']), [identity['pid']])

    def test_c3_empty_first_sweep_does_not_prove_empty(self):
        entry = Path('/proc') / str(os.getpid())
        with mock.patch.object(Path, 'iterdir', side_effect=[iter([]), iter([entry])]):
            self.assertEqual(session_members(os.getsid(0)), [os.getpid()])

    def test_c3_two_complete_empty_sweeps_are_positive_control(self):
        with mock.patch.object(Path, 'iterdir', side_effect=[iter([]), iter([])]) as listing:
            self.assertEqual(session_members(987654321), [])
            self.assertEqual(listing.call_count, 2)
