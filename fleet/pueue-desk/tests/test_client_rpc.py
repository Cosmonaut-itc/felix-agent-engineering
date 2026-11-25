"""CC3-P1: the real emitter against a synthetic Pueue 4.0.4 daemon.

Real processes, real native births and real sockets; the daemon double only
speaks frames (fake_pueued.py). Every death boundary of §2.6 ends retained,
never held and never released, with D alive and with D finished.
"""
import copy
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


if __name__ == '__main__':
    unittest.main()
