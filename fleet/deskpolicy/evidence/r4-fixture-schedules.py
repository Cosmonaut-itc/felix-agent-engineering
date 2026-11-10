"""Force the slow-start schedules behind N-c without depending on loadavg."""
import contextlib
import ctypes
import multiprocessing as mp
import os
from pathlib import Path
import signal
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path.cwd() / 'fleet'))
from deskpolicy import cli
from deskpolicy.engine import DeskPolicy
from deskpolicy.model import Invalid
from deskpolicy.store import held, read_json
from deskpolicy.tests import test_r3 as r3


class FixtureScheduleTest(r3.RoundThreeTest):
    __unittest_skip__ = False

    def test_w11_preparation_may_stall_until_the_receipt_writer_releases(self):
        prepared = mp.get_context('fork').Value('i', 0)
        timed_out = mp.get_context('fork').Event()
        real_prepare, real_atomic = DeskPolicy.prepare, cli.atomic_json

        def prepare(engine, *args, **kwargs):
            try:
                result = real_prepare(engine, *args, **kwargs)
            except Invalid as exc:
                if str(exc) == 'mutex-timeout':
                    timed_out.set()
                raise
            with prepared.get_lock():
                prepared.value += 1
            return result

        def write(path, value, **kwargs):
            if path.name == 'pao-admission.json' and value.get('claim_state') == 'active':
                if prepared.value < 2:
                    assert timed_out.wait(60), 'replay never reached its prepare timeout'
            real_atomic(path, value, **kwargs)

        with mock.patch.object(DeskPolicy, 'prepare', prepare), mock.patch.object(cli, 'atomic_json', write):
            r3.RoundThreeTest.test_w11_replay_never_overwrites_terminal_receipt(self)

    def test_w12_script_may_start_only_after_the_fixture_takes_mutex(self):
        gate, pidfile = self.h.directory / 'start-gate', self.h.directory / 'delayed-pid'
        real_popen, real_mutex = subprocess.Popen, r3.mutex
        parent_pid = os.getpid()
        program = '''import os,pathlib,sys,time
pathlib.Path(sys.argv[2]).write_text(str(os.getpid()))
while not pathlib.Path(sys.argv[1]).exists(): time.sleep(.01)
os.execvp(sys.argv[3],sys.argv[3:])
'''

        def delayed(command, *args, **kwargs):
            return real_popen([sys.executable, '-I', '-c', program, str(gate), str(pidfile),
                               *command], *args, **kwargs)

        @contextlib.contextmanager
        def acquired(*args, **kwargs):
            with real_mutex(*args, **kwargs) as lock:
                if os.getpid() == parent_pid:
                    gate.touch()
                yield lock

        try:
            with mock.patch.object(subprocess, 'Popen', delayed), mock.patch.object(r3, 'mutex', acquired):
                r3.RoundThreeTest.test_w12_real_close_mutex_timeout_preserves_exit(self)
        finally:
            if pidfile.exists():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(pidfile.read_text()), signal.SIGTERM)


if __name__ == '__main__':
    ctypes.CDLL(None).prctl(36, 1, 0, 0, 0)
    suite = unittest.TestSuite(FixtureScheduleTest(name) for name in (
        'test_w11_preparation_may_stall_until_the_receipt_writer_releases',
        'test_w12_script_may_start_only_after_the_fixture_takes_mutex'))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
