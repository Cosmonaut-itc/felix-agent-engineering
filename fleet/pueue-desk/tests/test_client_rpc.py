"""CC3-P1: the real emitter against a synthetic Pueue 4.0.4 daemon.

Real processes, real native births and real sockets; the daemon double only
speaks frames (fake_pueued.py). Every death boundary of §2.6 ends retained,
never held and never released, with D alive and with D finished.
"""
import copy
import errno
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import test_client_actions as cc1
import test_client_contract_rpc as corpus
import test_client_reconcile as cc2

control, a, c = cc1.control, cc1.a, cc1.c
rpc = a.rpc_module()
HERE = Path(__file__).resolve().parent
FRAMES = json.loads((HERE.parent / 'corpus/pueue-404-frames.json').read_text())
RUNNING = copy.deepcopy(cc1.CASES['remote-run-started']['response']['identity'])


def until(predicate, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.005)
    return False


def gone(pid):
    try:
        os.waitpid(pid, os.WNOHANG)  # Our own child: reap it so its birth reads as ended.
    except ChildProcessError:
        pass
    try:
        return cc1.native_birth(pid) is None
    except OSError:
        return False  # A zombie not yet reaped: look again.


class Wire(cc2.Evidence):
    """Q follows the synthetic daemon: the task moves only once frame 3 arrived."""
    def __init__(self, identity, directory, engine):
        super().__init__(identity, directory)
        self.engine, self.fake, self.moved, self.expire, self.timeouts = engine, None, None, False, []

    def sync(self):
        if self.fake is not None and (self.fake / 'got-request').exists():
            if self.expire:
                raise subprocess.TimeoutExpired('synthetic-status', a.READ_SECONDS)
            if self.moved is not None:
                self.identity = copy.deepcopy(self.moved)

    def observe(self, r, state, reservation):
        self.timeouts.append(getattr(self.engine.pueue, 'timeout', None))
        self.sync()
        return super().observe(r, state, reservation)

    def recover(self, *args):
        self.sync()
        return super().recover(*args)


class WireTests(cc2.ReconcileTests):
    pass


for _name in dir(cc2.ReconcileTests):
    if _name.startswith('test_'):
        setattr(WireTests, _name, None)


def setup(self):
    cc2.setup(self)
    self.life_patch.stop()  # D is the synthetic daemon's process and native birth.
    self.wire = Path(tempfile.mkdtemp(prefix='cc3p1-', dir='/tmp'))  # Short AF_UNIX path.
    self.addCleanup(shutil.rmtree, str(self.wire), True)
    self.evidence = Wire(self.r['expected'], self.engine.directory, self.engine)
    self.engine.client_adapter = self.evidence
    self.engine.pueue.context = {}
    self.secret = self.root / 'shared_secret'
    self.secret.write_bytes(b'synthetic-secret-' + b'0' * 47)
    self.engine.rpc_endpoint = lambda state: (self.wire / 's', self.secret)
    self.actions = a.ClientActions(self.engine, self.evidence)
    self.daemons = []
    self.addCleanup(self.release)


def start_daemon(self, mode):
    folder = self.wire / ('d' + str(len(self.daemons)))
    folder.mkdir()
    process = subprocess.Popen([sys.executable, '-I', '-S', str(HERE / 'fake_pueued.py'), str(self.wire / 's'),
                                str(self.secret), str(folder), mode], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    self.daemons.append((process, folder))
    self.assertTrue(until(lambda: (folder / 'ready').exists()), 'synthetic daemon listening')
    Path(self.state()['pid_file']).write_text(str(process.pid))
    self.fake = self.evidence.fake = folder
    return folder


def stop_daemon(self):
    process = self.daemons[-1][0]
    process.kill()  # Our own child: life D ends.
    process.wait()


def release(self):
    for process, folder in self.daemons:
        (folder / 'go').write_text('1')
        if process.poll() is None:
            process.kill()
            process.wait()


def process(self):
    return self.actions.record(self.r)['process']


def originals(self):
    return self.engine.directory / 'client-rpc' / self.process()['dispatch']


def seal(self):
    return json.loads((self.originals() / 'seal.json').read_text())


def requests(self):
    path = self.fake / 'requests'
    return path.read_text().split() if path.exists() else []


def kill_emitter(self):
    pid = self.process()['pid']
    os.kill(pid, signal.SIGKILL)  # The test kills its own child; CC1 itself never does.
    self.assertTrue(until(lambda: gone(pid)), 'emitter ended')


def reconcile(self):
    return a.Reconciler(self.engine, self.evidence, birth=cc1.native_birth, exclusion='filtered').run()


def check_retained(self, reason, uncertain):
    record, reserve = self.actions.record(self.r), self.actions.reservation(self.r)
    self.assertEqual((record['response']['status'], record['response']['reason']), ('unknown', reason))
    self.assertNotIn('attribution', record)
    self.assertEqual((reserve['open'], reserve['uncertain'], reserve['hold_identity']), (True, uncertain, None))
    self.assertTrue(self.engine.client_blocked('suites'))


def two_lives(self, sent, alive, dead):
    """CC2 with D alive, then with D finished: settles by S3/S4 but never releases."""
    self.assertEqual(len(self.requests()), sent)
    self.assertEqual(self.reconcile(), 1)
    self.check_retained(*alive)
    self.stop_daemon()
    for _ in range(2):
        self.assertEqual(self.reconcile(), 1)
        self.check_retained(*dead)


def act_with(self, seconds):
    with patch.object(a, 'SEAL_SECONDS', seconds):
        return self.act()


for _name, _value in list(globals().items()):
    if _name in ('start_daemon', 'stop_daemon', 'release', 'process', 'originals', 'seal', 'requests',
                 'kill_emitter', 'reconcile', 'check_retained', 'two_lives', 'act_with'):
        setattr(WireTests, _name, _value)
WireTests.setUp = setup


def test_success_seal_attributes_hold_with_one_frame(self):
    self.start_daemon('success')
    self.evidence.moved = self.after
    result = self.act_with(30)
    self.assertEqual((result['status'], result['identity']), ('held', self.after))
    seal = self.seal()
    self.assertEqual((seal['outcome'], seal['reply'], seal['peer']['pid']),
                     ('replied', 'success', self.daemons[-1][0].pid))
    record, reserve = self.actions.record(self.r), self.actions.reservation(self.r)
    self.assertEqual(record.get('attribution'), dict(dispatch=self.process()['dispatch'],
                                                 seal_receipt=seal['receipt'], exclusion='filtered'))
    self.assertEqual((reserve['hold_identity'], reserve['uncertain'], reserve['open']), (self.after, False, True))
    self.assertEqual(self.requests(), [rpc.request_payload('stash', 77).hex()])
    self.assertEqual(set(self.evidence.timeouts), {a.READ_SECONDS}, 'every CC1 daemon read is bounded')


def test_emitter_dies_before_claim(self):
    self.start_daemon('success')
    self.evidence.moved = self.after
    self.actions.boundary = lambda phase: self.kill_emitter() if phase == 'gated' else None
    first = self.act()
    self.assertEqual((first['status'], first['reason']), ('unknown', 'rpc_unresolved'))
    self.assertFalse((self.originals() / 'claim.json').exists())
    self.assertFalse((self.fake / 'secret-read').exists())
    self.two_lives(0, ('evidence_missing', True), ('evidence_missing', False))


def test_emitter_dies_between_claim_and_frame_3(self):
    self.start_daemon('stall-greeting')
    self.evidence.moved = self.after
    # Give this child a test-only deadline beyond the harness's wait. The
    # protocol still stalls at the same boundary and the test uses real SIGKILL.
    popen = subprocess.Popen

    def spawn(argv, **kwargs):
        if argv[3:4] != [str(a.EMITTER)]:
            return popen(argv, **kwargs)
        script = ('import runpy, sys; path = sys.argv.pop(1); '
                  'sys.argv[0] = path; main = runpy.run_path(path)["main"]; '
                  'main.__globals__["HANDSHAKE_SECONDS"] = 3600; sys.exit(main(sys.argv))')
        return popen([*argv[:3], '-c', script, *argv[3:]], **kwargs)

    with patch.object(a.subprocess, 'Popen', side_effect=spawn):
        first = self.act_with(0)
    self.assertEqual((first['status'], first['reason']), ('unknown', 'rpc_unresolved'))
    self.assertTrue(until(lambda: (self.fake / 'secret-read').exists()))
    self.kill_emitter()
    self.assertTrue((self.originals() / 'claim.json').exists())
    self.assertFalse((self.originals() / 'seal.json').exists())
    self.two_lives(0, ('rpc_unresolved', True), ('startup', False))


def test_emitter_dies_after_frame_3(self):
    self.start_daemon('stall-reply')
    self.evidence.moved = self.after  # The daemon applied it: still never attributed.
    first = self.act_with(0)
    self.assertEqual((first['status'], first['reason']), ('unknown', 'rpc_unresolved'))
    self.assertTrue(until(lambda: (self.fake / 'got-request').exists()))
    self.kill_emitter()
    self.assertFalse((self.originals() / 'seal.json').exists())
    self.two_lives(1, ('rpc_unresolved', True), ('startup', False))


def test_term_during_reply_wait_still_seals(self):
    self.start_daemon('stall-reply')
    self.evidence.moved = self.after
    self.assertEqual(self.act_with(0)['status'], 'unknown')
    self.assertTrue(until(lambda: (self.fake / 'got-request').exists()))
    pid = self.process()['pid']
    os.kill(pid, signal.SIGTERM)
    (self.fake / 'go').write_text('1')
    seal_path = self.originals() / 'seal.json'
    self.assertTrue(until(lambda: seal_path.exists() or gone(pid)))
    self.assertTrue(seal_path.exists(), 'TERM must leave the emitter able to seal the reply')
    self.assertEqual((self.seal()['outcome'], self.seal()['reply']), ('replied', 'success'))


def test_oversized_reply_header_without_body_never_seals(self):
    self.start_daemon('oversized-header')
    self.act_with(0)
    self.assertTrue(until(lambda: (self.fake / 'got-request').exists()))
    self.assertTrue(until(lambda: gone(self.process()['pid'])), 'emitter ended')
    self.assertFalse((self.originals() / 'seal.json').exists(), 'the reply body never arrived')
    self.assertEqual(self.reconcile(), 1)
    self.check_retained('rpc_unresolved', True)


def check_local_abort(self, where):
    self.start_daemon('stall-reply')
    real_socket = rpc.socket.socket

    class BrokenSocket(real_socket):
        written = 0

        def send(sock, data):
            if where == 'write' and sock.written:
                raise OSError(errno.EIO, 'synthetic local write error')
            count = super().send(data[:1] if where == 'write' else data)
            sock.written += count
            return count

        def recv(sock, *args):
            if where == 'read' and sock.written:
                raise OSError(errno.EIO, 'synthetic local read error')
            return super().recv(*args)

    def inject(dispatch, own, args):
        def talk(plan):
            with self.assertRaises(rpc.Abort):
                rpc.socket_talk(plan, self.wire / 's', self.secret, cc1.native_birth)
            raise rpc.Abort()

        with patch.object(rpc.socket, 'socket', BrokenSocket):
            self.assertIsNone(rpc.emit(self.engine.directory, dispatch, own, talk))

    self.actions.rpc = self.fail  # Select the in-process path; inject takes over the conversation.
    with patch.object(self.actions, 'injected', side_effect=inject):
        first = self.act()
    self.assertTrue((self.originals() / 'claim.json').exists())
    self.assertFalse((self.originals() / 'seal.json').exists())
    self.assertEqual((first['status'], first['reason']), ('unknown', 'rpc_unresolved'))
    self.assertEqual(self.reconcile(), 1)
    self.check_retained('rpc_unresolved', True)


def test_partial_request_write_aborts_without_seal(self):
    check_local_abort(self, 'write')


def test_local_error_reading_reply_aborts_without_seal(self):
    check_local_abort(self, 'read')


def test_lost_reply_is_s3_and_never_attributed(self):
    self.start_daemon('close')
    self.evidence.moved = self.after
    first = self.act_with(30)
    self.assertEqual((first['status'], first['reason']), ('unknown', 'evidence_missing'))
    self.assertEqual((self.seal()['outcome'], self.seal()['reply']), ('closed', None))
    self.check_retained('evidence_missing', False)
    self.two_lives(1, ('evidence_missing', False), ('startup', False))


def test_success_without_q_stays_retriable_until_cc2_reads_q(self):
    self.start_daemon('success')
    self.evidence.moved, self.evidence.expire = self.after, True
    first = self.act_with(30)
    self.assertEqual((first['status'], first['reason']), ('unknown', 'evidence_missing'))
    self.assertEqual(self.seal()['reply'], 'success')
    self.check_retained('evidence_missing', False)  # settled, yet not final
    self.evidence.expire = False
    self.assertEqual(self.reconcile(), 1)
    record, reserve = self.actions.record(self.r), self.actions.reservation(self.r)
    self.assertEqual(record['response']['status'], 'held')
    self.assertEqual(record.get('attribution', {}).get('seal_receipt'), self.seal()['receipt'])
    self.assertEqual((reserve['hold_identity'], reserve['uncertain']), (self.after, False))


def test_cc2_relay_retains_on_deadline_and_retries_incompatible_q(self):
    self.start_daemon('success')  # Q keeps showing Queued until the test moves it.
    first = self.act_with(0)
    self.assertEqual((first['status'], first['reason']), ('unknown', 'rpc_unresolved'))
    self.assertTrue(until(lambda: (self.originals() / 'seal.json').exists()))
    self.evidence.slow = 1.0  # Beyond the .4 s the batch shares.
    self.assertEqual(self.reconcile(), 1)
    self.check_retained('evidence_missing', True)
    self.evidence.slow = 0  # N+1: an incompatible Q is not attributed and stays retriable.
    self.assertEqual(self.reconcile(), 1)
    self.check_retained('evidence_missing', False)
    self.evidence.identity = copy.deepcopy(self.after)  # N+2 reaches the action again.
    self.assertEqual(self.reconcile(), 1)
    record = self.actions.record(self.r)
    self.assertEqual((record['response']['status'], record.get('attribution', {}).get('seal_receipt')),
                     ('held', self.seal()['receipt']))
    self.assertEqual(self.actions.reservation(self.r)['hold_identity'], self.after)


def test_cc2_stock_relay_under_contention(self):
    self.start_daemon('success')
    self.evidence.moved = self.after
    self.assertEqual(self.act_with(0)['status'], 'unknown')
    self.assertTrue(until(lambda: (self.originals() / 'seal.json').exists()))

    def turn():
        with control.locked(self.engine.directory):
            self.engine.reconcile_clients()
        return self.actions.record(self.r)['response']['status']

    with patch.object(a.Reconciler, 'lock', side_effect=BlockingIOError) as collision:
        self.assertEqual(turn(), 'unknown', 'N collects outside the lock')
        self.assertEqual(turn(), 'held', 'N+1 recovers the stock proposal under the original lock')
    self.assertGreaterEqual(collision.call_count, 2)
    self.assertEqual(self.actions.record(self.r)['attribution']['seal_receipt'], self.seal()['receipt'])


def test_retired_receipt_survives_reused_id_and_finish(self):
    self.start_daemon('success')
    r = self.r = corpus.request_for('retire-interrupted')
    self.evidence.identity = copy.deepcopy(r['expected'])
    self.evidence.moved = copy.deepcopy(corpus.RPC['observations']['reused'])
    absent = c.rpc_effect_identity(r['op'], r['expected'], self.evidence.moved)
    finish = dict(r, op='finish', phase='finish', expected=absent,
                  proof=dict(kind='interrupted', attempt=absent['attempt'],
                             receipt='c'*64, acceptance_receipt=None, exit=1))
    self.assertEqual(self.act_with(0)['status'], 'unknown')
    self.assertTrue(until(lambda: (self.originals() / 'seal.json').exists()))
    self.evidence.finish, self.evidence.proof = finish, finish['proof']
    self.assertEqual(self.reconcile(), 1)
    retired = self.actions.record(r)['response']
    self.assertEqual((retired['status'], retired['identity']), ('retired', absent))
    self.assertEqual(self.reconcile(), 1)
    terminal = self.actions.record(finish)
    self.assertIsNotNone(terminal)
    self.assertEqual((terminal['response']['status'], terminal['response']['identity']), ('finished', absent))
    self.assertEqual(self.actions.record(r)['response'], retired)
    self.assertFalse(self.actions.reservation(r)['open'])


def test_lost_claim_retains_a_success_seal(self):
    self.start_daemon('success')
    self.evidence.moved = self.after
    self.act_with(0)
    folder = self.originals()
    self.assertTrue(until(lambda: (folder / 'seal.json').exists()))
    (folder / 'claim.json').rename(folder / 'claim.restored-without')
    self.assertEqual(self.reconcile(), 1)
    self.check_retained('evidence_missing', True)


def test_claim_loser_never_connects_nor_seals(self):
    calls = []
    Path(self.state()['pid_file']).write_text(str(os.getpid()))  # D: a live process; no socket is used.
    self.actions = a.ClientActions(self.engine, self.evidence, rpc=lambda *args: calls.append(args))

    def gate(phase):
        if phase == 'gated':
            p = self.process()
            rpc.publish(self.originals(), 'claim.json', dict(schema=c.RPC_CLAIM, dispatch=p['dispatch'],
                                                             plan_digest=p['plan_digest'], pid=1, birth=p['birth']))
    self.actions.boundary = gate
    result = self.act()
    self.assertEqual((result['status'], result['reason']), ('unknown', 'evidence_missing'))
    self.assertEqual(calls, [])
    self.assertFalse((self.originals() / 'seal.json').exists())
    self.check_retained('evidence_missing', True)


def test_other_protocol_version_fails_closed_before_frame_3(self):
    self.start_daemon('version')
    result = self.act_with(30)
    self.assertEqual((result['status'], result['reason']), ('rejected', 'unsupported'))
    seal = self.seal()
    self.assertEqual((seal['outcome'], seal['reason']), ('not-sent', 'unsupported'))
    self.assertTrue((self.fake / 'secret-read').exists())
    self.assertEqual(self.requests(), [])
    self.assertFalse(self.actions.reservation(self.r)['open'])


def test_other_daemon_life_never_receives_the_secret(self):
    self.start_daemon('success')
    Path(self.state()['pid_file']).write_text(str(os.getpid()))  # P names another live D.
    result = self.act_with(30)
    self.assertEqual((result['status'], result['reason']), ('rejected', 'startup'))
    self.assertEqual((self.seal()['outcome'], self.seal()['peer']), ('not-sent', None))
    self.assertFalse((self.fake / 'secret-read').exists())


def test_coordinated_profile_rejects_remote_run_mutators_without_emitter(self):
    self.engine.client_exclusion = 'coordinated'
    self.actions = a.ClientActions(self.engine, self.evidence)
    with patch.object(a.subprocess, 'Popen', side_effect=OSError('no emitter')) as popen:
        result = self.act()
    self.assertEqual((result['status'], result['reason']), ('rejected', 'unsupported'))
    self.assertEqual(popen.call_count, 0)
    self.assertFalse((self.engine.directory / 'client-rpc').exists())


def test_coordinated_success_is_unsupported_not_held(self):
    # Before P1 the CLI's rc 0 and the edge confirmed `held`; now only R+ ∧ P ∧ Q ∧ Ex do.
    self.engine.client_exclusion = 'coordinated'
    self.actions = a.ClientActions(self.engine, self.evidence)
    self.r = cc1.request('jale-rack-queued-held')
    self.start_daemon('success')
    self.evidence.moved = self.after
    result = self.act_with(30)
    self.assertEqual((result['status'], result['reason']), ('unknown', 'unsupported'))
    self.assertEqual(len(self.requests()), 1)
    self.check_retained('unsupported', False)


def test_success_with_a_running_q_is_not_a_hold(self):
    self.start_daemon('success')
    self.evidence.moved = RUNNING  # It started between P and the stash.
    result = self.act_with(30)
    self.assertEqual((result['status'], result['reason']), ('unknown', 'evidence_missing'))
    self.check_retained('evidence_missing', False)


for _name, _value in list(globals().items()):
    if _name.startswith('test_') and callable(_value):
        setattr(WireTests, _name, _value)


class FrameTests(unittest.TestCase):
    def test_golden_requests_and_reply_classes(self):
        for form in FRAMES['forms']:
            with self.subTest(verb=form['verb'], task=form['task_id']):
                self.assertEqual(rpc.frame(rpc.request_payload(form['verb'], form['task_id'])).hex(), form['request'])
                self.assertEqual(rpc.reply_class(bytes.fromhex(form['response'])[8:]), form['reply'])

    def test_closed_forms_only(self):
        for verb in ('kill', 'Stash', 'remove ', 'kill:SigInt', 'enqueue', 'restart'):
            self.assertRaises(ValueError, rpc.request_payload, verb, 7)
        for task in (-1, 2**31, True, '7', None):
            self.assertRaises(ValueError, rpc.request_payload, 'stash', task)
        for raw in (b'', b'\xa0', b'\xa1\x67Success\x01', b'\xa1\x67Success\x60x', b'\xa1\x67Unknown\x60',
                    b'\xa2\x67Success\x60\x67Failure\x60', b'\x67Success'):
            self.assertEqual(rpc.reply_class(raw), 'unrecognized')
        self.assertEqual(rpc.reply_class(b'\xa1\x67Failure\x60'), 'failure')

    def test_publish_is_exclusive_and_private(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            rpc.publish(folder, 'claim.json', dict(n=1))
            self.assertRaises(FileExistsError, rpc.publish, folder, 'claim.json', dict(n=2))
            self.assertEqual(json.loads((Path(folder) / 'claim.json').read_text()), dict(n=1))
            self.assertEqual((Path(folder) / 'claim.json').stat().st_mode & 0o777, 0o600)
            self.assertEqual(sorted(os.listdir(folder)), ['claim.json'])


class EndpointTests(unittest.TestCase):
    def test_separate_runtime_and_data_paths(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            root = Path(folder)
            env = dict(HOME=folder, XDG_RUNTIME_DIR=str(root / 'run'), XDG_DATA_HOME=str(root / 'xdg'))
            pueue = control.Pueue(env=env, cwd=root)
            engine = control.MiniControl(pueue, root, host='desk')
            authority = dict(pid_file=str(root / 'run/pueue.pid'))
            user = __import__('pwd').getpwuid(os.getuid()).pw_name
            socket_path = root / 'run' / ('pueue_' + user + '.socket')
            with patch.object(control.sys, 'platform', 'linux'):
                secret_path = root / 'xdg/pueue/shared_secret'
                self.assertEqual(engine.rpc_endpoint(authority), (socket_path, secret_path))
                # The pre-start configuration can differ from the caller's environment.
                authority['pueue_data'] = str(root / 'validated')
                secret_path = root / 'validated/shared_secret'
                self.assertEqual(engine.rpc_endpoint(authority), (socket_path, secret_path))
                config = engine.reorder_config(dict(request=dict(action_id='synthetic')), authority)
            shared = json.loads(config.read_text())['shared']
            self.assertEqual(shared['unix_socket_path'], str(socket_path))
            self.assertEqual(shared['shared_secret_path'], str(secret_path))
            self.assertEqual(shared['runtime_directory'], str(socket_path.parent))
            self.assertEqual(shared['pueue_directory'], str(secret_path.parent))

    def test_darwin_keeps_socket_and_secret_together(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            root = Path(folder)
            data = root / 'Library/Application Support/pueue'
            env = dict(HOME=folder, XDG_RUNTIME_DIR=str(root / 'elsewhere'), XDG_DATA_HOME=str(root / 'xdg'))
            engine = control.MiniControl(control.Pueue(env=env, cwd=root), root, host='mini')
            authority = dict(pid_file=str(data / 'pueue.pid'))
            user = __import__('pwd').getpwuid(os.getuid()).pw_name
            with patch.object(control.sys, 'platform', 'darwin'):
                self.assertEqual(engine.rpc_endpoint(authority),
                                 (data / ('pueue_' + user + '.socket'), data / 'shared_secret'))

    @unittest.skipUnless(sys.platform == 'linux' and os.environ.get('CC3_PRIVATE_STOCK') == '1',
                         'requires private Pueue 4.0.4 with XDG runtime on Linux')
    def test_private_stock_split_paths_reply_failure(self):
        binary_path = os.pathsep.join([str(Path.home() / '.local/bin'), '/opt/homebrew/bin', os.defpath])
        pueue = shutil.which('pueue', path=binary_path)
        pueued = shutil.which('pueued', path=binary_path)
        self.assertTrue(pueue and pueued, 'private test requires both Pueue binaries')
        with tempfile.TemporaryDirectory(prefix='cc3-stock-', dir='/tmp') as folder:
            root = Path(folder)
            home, config, data, runtime = (root / name for name in ('home', 'conf', 'data', 'run'))
            for directory in (home, config, data, runtime):
                directory.mkdir(mode=0o700)
            env = dict(HOME=str(home), XDG_CONFIG_HOME=str(config), XDG_DATA_HOME=str(data),
                       XDG_RUNTIME_DIR=str(runtime), LANG='C', PATH=os.defpath)
            configuration = control.validate_configuration(home=home, pueue=pueue, daemon=pueued,
                                                           env=env, cwd=root)
            with (root / 'daemon.log').open('wb') as log:
                daemon = subprocess.Popen([pueued], env=env, cwd=root, stdin=subprocess.DEVNULL,
                                          stdout=log, stderr=log)
                try:
                    pid_path = configuration['pid_file']
                    self.assertTrue(until(lambda: daemon.poll() is not None or pid_path.exists()))
                    self.assertIsNone(daemon.poll(), (root / 'daemon.log').read_text())
                    cli = control.Pueue(pueue, env=env, cwd=root)
                    cli.timeout = 3
                    self.assertEqual(cli.status()['tasks'], {})
                    self.assertEqual(pid_path.parent, runtime)
                    self.assertTrue((configuration['data'] / 'shared_secret').is_file())
                    self.assertNotEqual(pid_path.parent, configuration['data'])
                    authority = dict(pid_file=str(pid_path), pueue_data=str(configuration['data']))
                    engine = control.MiniControl(cli, root, host='desk')
                    plan = dict(daemon=dict(pid=daemon.pid, birth=cc1.native_birth(daemon.pid)),
                                verb='stash', task_id=77)
                    reply = rpc.socket_talk(plan, *engine.rpc_endpoint(authority), cc1.native_birth)
                    self.assertEqual((reply['outcome'], reply['reply'], reply['reason']), ('replied', 'failure', None))
                    self.assertEqual(cli.status()['tasks'], {})
                finally:
                    if daemon.poll() is None:
                        daemon.terminate()
                        try:
                            daemon.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            daemon.kill()
                            daemon.wait()


if __name__ == '__main__':
    unittest.main()
