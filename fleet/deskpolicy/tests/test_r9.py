"""Real SIGALRM at each protected step of a writer tick; Linux fixtures only."""
import contextlib
import inspect
import io
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from deskpolicy import cli, engine as engine_module, model, store
from deskpolicy.engine import DeskPolicy
from deskpolicy.model import CONFIG, utc_text
from deskpolicy.store import Runtime, atomic_json, open_file, read_json
from deskpolicy.testing import Harness


class RoundNineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='.deskpolicy-r9-', dir=Path.cwd())
        self.addCleanup(self.tmp.cleanup)
        self.h = Harness(self.tmp.name)
        self.h.cpus = tuple(range(12))
        self.engine = DeskPolicy(Runtime(self.h))
        self.h.config.parent.mkdir(mode=0o700)
        atomic_json(self.h.config, {**CONFIG, 'night_enabled': True})
        self.engine.preflight({
            'schema': 'pao/desk-preflight@1', 'boot_id': self.h.boot,
            'checked_at': utc_text(self.h.now),
            'consumers': {'jale': 'a' * 40, 'hasito': 'b' * 40},
            'resources_checked': True, 'legacy_drained': True}, arm=True)
        self.engine.publish()
        # Valid uncertain claim with an absent session and a free life flock.
        # No workload is launched and no production state is consulted.
        record = self.engine.register('stale', 'hasito', 'script', 'r9', 1, lane=1)
        record.update(state='admitted', admitted_run_id='run-stale', claim={
            'run_id': 'run-stale', 'lane': 1, 'weight': 1, 'cpus': [0],
            'boot_id': self.h.boot, 'process': {
                'pid': 2**30, 'start': 1, 'pgid': 2**30, 'sid': 2**30, 'zombie': False},
            'keepers': [], 'state': 'uncertain', 'admitted_boottime_s': self.h.bt,
            'claim_token': 'a' * 64, 'closed_boottime_s': None, 'close_evidence': None,
            'script_exit_code': None, 'close_cause': None, 'mode': 'night', 'revision': 1})
        self.engine.write_record(record)
        os.close(open_file(self.engine.life_path('hasito', 1), create=True))
        receipt = self.h.directory / 'runs' / 'run-stale'
        receipt.mkdir(mode=0o700, parents=True)
        atomic_json(receipt / 'pao-admission.json', self.engine.receipt(
            'stale', 'run-stale', record['claim'], 1, claim=record['claim']))
        self.h.advance(1)

    def assert_mutex_free(self):
        probe = subprocess.run([sys.executable, '-I', '-c',
            'import fcntl,os,sys; fd=os.open(sys.argv[1],os.O_RDWR); '
            'fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB); os.close(fd); print("mutex-free")',
            str(self.h.root / 'admission.lock')], capture_output=True, text=True, timeout=5)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(probe.stdout.strip(), 'mutex-free')

    def interrupt_publish(self, function, marker, *, ancestor=None):
        lines, first = inspect.getsourcelines(function)
        targets = [first + i for i, line in enumerate(lines) if marker in line]
        self.assertEqual(len(targets), 1, (function.__name__, marker, targets))
        fired = []
        before = (self.h.root / 'pao-policy-v1.json').read_bytes()
        previous_handler = signal.getsignal(signal.SIGALRM)
        previous_trace = sys.gettrace()
        caught = None
        returned = False

        def trace(frame, event, arg):
            if event == 'line' and frame.f_code is function.__code__ and frame.f_lineno == targets[0] and not fired:
                if ancestor:
                    parent = frame.f_back
                    while parent and parent.f_code.co_name != ancestor:
                        parent = parent.f_back
                    if parent is None:
                        return trace
                fired.append(signal.getitimer(signal.ITIMER_REAL)[0])
                os.kill(os.getpid(), signal.SIGALRM)
            return trace

        try:
            sys.settrace(trace)
            try:
                with cli.cycle_timeout():
                    self.engine.publish()
                returned = True
            except BaseException as exc:
                caught = exc
        finally:
            sys.settrace(previous_trace)
        changed = before != (self.h.root / 'pao-policy-v1.json').read_bytes()
        print('R9', function.__name__, marker, 'fired', len(fired),
              'returned', returned, 'exception', type(caught).__name__,
              'policy_changed', changed, flush=True)
        self.assertEqual(len(fired), 1, 'injection point was not reached')
        self.assertGreater(fired[0], 0)
        self.assertLessEqual(fired[0], 10)
        self.assertFalse(returned, 'engine.publish() returned normally after SIGALRM')
        self.assertIsNotNone(caught)
        self.assertEqual(str(caught), 'writer-cycle-timeout')
        self.assertNotIsInstance(caught, Exception, 'deadline must bypass Exception and OSError')
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous_handler)
        self.assertFalse(changed, 'failed tick renewed policy')
        self.assert_mutex_free()
        # Partial reconciliation (including a closed tombstone) must converge.
        self.assertEqual(self.engine.publish()['mode'], 'night')
        self.assertEqual(read_json(self.engine.record_path('stale'))['claim']['state'], 'closed')
        receipt = read_json(self.h.directory / 'runs/run-stale/pao-admission.json')
        self.assertEqual(receipt['claim_state'], 'closed')

    def test_reconcile_claim_oserror(self):
        self.interrupt_publish(DeskPolicy.reconcile_claim, 'free = not held')

    def test_session_members_outer_oserror(self):
        self.interrupt_publish(store.session_members, 'entries = list')

    def test_session_members_inner_missing_process(self):
        self.interrupt_publish(store.session_members, 'before = item.stat()')

    def test_recover_receipt_exception(self):
        self.interrupt_publish(DeskPolicy.recover_receipt, 'self.reconcile_receipt(record)')

    def test_reconcile_receipt_missing_file(self):
        self.interrupt_publish(DeskPolicy.reconcile_receipt, 'receipt = read_json')

    def test_arm_valid_oserror(self):
        self.interrupt_publish(DeskPolicy.arm_valid, 'arm = read_json')

    def test_publish_missing_control(self):
        self.interrupt_publish(DeskPolicy.publish, 'control = read_json')

    def test_open_file_baseexception_cleanup(self):
        self.interrupt_publish(store.open_file, 'secure_fd(fd, path', ancestor='read_json')

    def test_mutex_blocking_io(self):
        self.interrupt_publish(store.mutex.__wrapped__, 'fcntl.flock(fd,')

    def test_assert_held_probe_blocking_io(self):
        self.interrupt_publish(store.assert_held, 'fcntl.flock(probe,')

    def test_assert_held_owner_blocking_io(self):
        self.interrupt_publish(store.assert_held, 'fcntl.flock(fd,')

    def test_held_blocking_io(self):
        self.interrupt_publish(store.held, 'fcntl.flock(fd,')

    def test_strict_json_valueerror(self):
        self.interrupt_publish(model.strict_json, 'result = json.loads')

    def test_timestamp_valueerror(self):
        self.interrupt_publish(model.timestamp, 'return dt.datetime.strptime')

    def test_public_cli_loop_aborts_then_publishes_with_new_deadline(self):
        # Invoke the actual script/parser with publicar (no --una-vez), a real
        # 20 s wait and real timer. Only the one-shot slow point is injected.
        program = r'''
import os, runpy, signal, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from deskpolicy.engine import DeskPolicy
original = DeskPolicy.arm_valid
calls = 0
def slow_once(self, *args):
    global calls
    calls += 1
    remaining = signal.getitimer(signal.ITIMER_REAL)[0]
    assert 0 < remaining <= 10, remaining
    print('R9 CLI tick=%s deadline=%s' % (calls, remaining), file=sys.stderr, flush=True)
    if calls == 1:
        os.kill(os.getpid(), signal.SIGALRM)
    return original(self, *args)
DeskPolicy.arm_valid = slow_once
sys.argv = [str(Path(sys.argv[1]) / 'deskpolicy/cli.py'), 'publicar']
runpy.run_path(sys.argv[0], run_name='__main__')
'''
        env = dict(os.environ)
        env.update(RUN_SUITE_HARNESS='1', LOCK_ROOT=str(self.h.root),
                   PAO_DESK_HARNESS=str(self.h.directory))
        before = (self.h.root / 'pao-policy-v1.json').read_bytes()
        with tempfile.TemporaryFile(mode='w+') as diagnostics:
            child = subprocess.Popen([sys.executable, '-I', '-c', program,
                str(Path(cli.__file__).resolve().parent.parent)], env=env,
                stdout=subprocess.PIPE, stderr=diagnostics, text=True)
            try:
                self.assertTrue(select.select([child.stdout], [], [], 15)[0], 'first tick produced no output')
                first = json.loads(child.stdout.readline())
                print('R9 CLI first', first, flush=True)
                self.assertEqual(first, {'schema': 'pao/desk-error@1', 'reason': 'census-unknown',
                    'detail': 'writer-cycle-timeout', 'exit_code': 75})
                self.assertEqual((self.h.root / 'pao-policy-v1.json').read_bytes(), before)
                self.assert_mutex_free()
                self.assertTrue(select.select([child.stdout], [], [], 30)[0], 'second tick produced no output')
                second = json.loads(child.stdout.readline())
                print('R9 CLI second', second['schema'], second['revision'], second['mode'], flush=True)
                self.assertEqual(second['schema'], 'pao/desk-policy@1')
                self.assertEqual(second['revision'], 2)
                self.assertEqual(second['mode'], 'night')
                self.assert_mutex_free()
            finally:
                child.terminate()
                try:
                    child.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate(timeout=5)
                diagnostics.seek(0)
                text = diagnostics.read()
                print(text, end='', flush=True)
            self.assertIn('tick=1 deadline=', text)
            self.assertIn('tick=2 deadline=', text)
        # The other public commands must never arm a cycle alarm. Their
        # existing argument/descriptor behavior is exercised by test_policy/r2.
        commands = [(['publicar', '--una-vez'], 0), (['reconciliar'], 0), (['estado'], 0),
                    (['admitir', '--request-id', 'absent', '--run-id', 'absent', '--lane', '1',
                      '--pid', str(os.getpid())], 75),
                    (['cerrar', '--request-id', 'absent', '--run-id', 'absent',
                      '--pid', str(os.getpid()), '--claim-token-fd', '0'], 75)]
        for arguments, expected in commands:
            with mock.patch.object(cli, 'DeskPolicy', return_value=self.engine), \
                    mock.patch.object(cli.signal, 'setitimer', side_effect=AssertionError('unexpected alarm')), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(arguments), expected, arguments)
