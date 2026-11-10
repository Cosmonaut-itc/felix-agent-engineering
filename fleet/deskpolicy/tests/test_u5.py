"""Observable publication, process and lock guarantees of the Linux writer."""
import contextlib
import ctypes
import fcntl
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import resource
import signal
import stat
import subprocess
import sys
import time
import unittest
from unittest import mock

from deskpolicy import cli, engine as engine_module, store
from deskpolicy.engine import DeskPolicy
from deskpolicy.tests import test_r9


class WriterProcessTest(unittest.TestCase):
    setUp = test_r9.RoundNineTest.setUp
    assert_mutex_free = test_r9.RoundNineTest.assert_mutex_free

    def tick(self, timeout=2, diagnostics=None):
        with store.mutex(self.h.root, name='writer.lock') as writer:
            return cli.writer_tick(self.engine, writer, _timeout=timeout, _diagnostics=diagnostics)

    def wait_file(self, path, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists() and path.stat().st_size:
                return path.read_text()
            time.sleep(.01)
        self.fail('no observation: ' + str(path))

    def dead(self, pid):
        try:
            return Path('/proc/%s/stat' % pid).read_text().split(') ', 1)[1].split()[0] == 'Z'
        except FileNotFoundError:
            return True

    def wait_dead(self, pid, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.dead(pid):
                return
            time.sleep(.01)
        self.fail('process still alive: %s' % pid)

    def lock_free(self, path):
        result = subprocess.run(['/usr/bin/python3', '-I', '-c',
            'import fcntl,os,sys; fd=os.open(sys.argv[1],os.O_RDWR); '
            'fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)', str(path)],
            capture_output=True, timeout=5)
        return result.returncode == 0

    def supervisor(self, *, timeout=.8, stdout=None, stderr=None, configure=None):
        real_tick = cli.writer_tick
        pid = os.fork()
        if pid == 0:
            try:
                for number, destination in ((1, stdout), (2, stderr)):
                    fd = os.open(destination or self.h.directory / ('log-%s' % number),
                                 os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                    os.dup2(fd, number)
                    os.close(fd)
                cli.writer_tick = lambda engine, writer, **kw: real_tick(
                    engine, writer, _timeout=timeout, **kw)
                if configure:
                    configure()
                cli._writer_loop(self.engine)
            except SystemExit as exc:
                os._exit(exc.code)
            except BaseException:
                os._exit(99)
            os._exit(0)
        self.addCleanup(self.cleanup_supervisor, pid)
        return pid

    def cleanup_supervisor(self, pid):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, 0)

    def blocking_publish(self, reached, *, stop=False):
        def publish(*args, **kwargs):
            with store.mutex(self.h.root):
                reached.write_text(str(os.getpid()))
                if stop:
                    os.kill(os.getpid(), signal.SIGSTOP)
                time.sleep(60)
        return publish

    def test_commit_stages_and_next_revision(self):
        for phase in ('before-commit', 'before-rename', 'after-rename', 'directory-fsync', 'before-result'):
            with self.subTest(phase=phase):
                before = store.read_json(self.h.root / 'pao-policy-v1.json')['revision']
                reached = self.h.directory / ('commit-' + phase)
                real_atomic = engine_module.atomic_json
                real_replace = os.replace
                real_fsync = os.fsync
                real_send = cli._writer_send

                def block():
                    reached.write_text(str(os.getpid()))
                    time.sleep(60)

                def atomic(path, value, **kw):
                    if path.name == 'pao-control.json' and phase == 'before-commit':
                        block()
                    return real_atomic(path, value, **kw)

                def replace(source, target):
                    if Path(target).name == 'pao-policy-v1.json':
                        if phase == 'before-rename':
                            block()
                        value = real_replace(source, target)
                        if phase == 'after-rename':
                            block()
                        return value
                    return real_replace(source, target)

                def fsync(fd):
                    if phase == 'directory-fsync' and stat.S_ISDIR(os.fstat(fd).st_mode):
                        current = store.read_json(self.h.root / 'pao-policy-v1.json')['revision']
                        if current > before:
                            block()
                    return real_fsync(fd)

                def send(fd, kind, **kw):
                    if kind == 'result' and phase == 'before-result':
                        block()
                    return real_send(fd, kind, **kw)

                with mock.patch.object(engine_module, 'atomic_json', atomic), \
                        mock.patch.object(os, 'replace', replace), \
                        mock.patch.object(os, 'fsync', fsync), \
                        mock.patch.object(cli, '_writer_send', send):
                    value = self.tick()
                self.assertTrue(reached.exists())
                suffix = '' if phase == 'before-commit' else '-publication-unknown'
                self.assertEqual(value['detail'], 'writer-cycle-timeout' + suffix)
                current = store.read_json(self.h.root / 'pao-policy-v1.json')['revision']
                self.assertEqual(current > before, phase in ('after-rename', 'directory-fsync', 'before-result'))
                self.assert_mutex_free()
                reserved = store.read_json(self.h.root / 'pao-control.json')['revision']
                next_policy = self.tick()
                self.assertEqual(next_policy['revision'], reserved + 1)
                self.assertEqual(next_policy['mode'], 'night')

    def test_confirmation_survives_timer_and_exit_boundaries(self):
        for phase in ('before-send', 'after-send', 'before-disarm', 'after-disarm'):
            with self.subTest(phase=phase):
                real_send, real_timer = cli._writer_send, signal.setitimer
                reached = self.h.directory / phase

                def pause():
                    reached.write_text(str(os.getpid()))
                    time.sleep(60)

                def send(fd, kind, **kw):
                    if kind == 'result' and phase == 'before-send':
                        pause()
                    result = real_send(fd, kind, **kw)
                    if kind == 'result' and phase == 'after-send':
                        pause()
                    return result

                def timer(which, seconds, *args):
                    if seconds == 0 and phase == 'before-disarm':
                        pause()
                    result = real_timer(which, seconds, *args)
                    if seconds == 0 and phase == 'after-disarm':
                        pause()
                    return result

                with mock.patch.object(cli, '_writer_send', send), \
                        mock.patch.object(signal, 'setitimer', timer):
                    value = self.tick()
                self.assertTrue(reached.exists())
                self.assertEqual(value.get('detail') if phase == 'before-send' else value['schema'],
                    'writer-cycle-timeout-publication-unknown' if phase == 'before-send' else 'pao/desk-policy@1')
                self.assert_mutex_free()
                self.assertEqual(self.tick()['mode'], 'night')

    def test_fsync_error_after_replace_is_unknown(self):
        before = store.read_json(self.h.root / 'pao-policy-v1.json')['revision']
        real = os.fsync

        def fail(fd):
            if (stat.S_ISDIR(os.fstat(fd).st_mode)
                    and store.read_json(self.h.root / 'pao-policy-v1.json')['revision'] > before):
                raise OSError('directory-durability-fixture')
            return real(fd)

        with mock.patch.object(os, 'fsync', fail):
            value = self.tick()
        self.assertIn('writer-cycle-error-publication-unknown', value['detail'])
        self.assertIn('directory-durability-fixture', value['detail'])
        self.assertGreater(store.read_json(self.h.root / 'pao-policy-v1.json')['revision'], before)
        self.assert_mutex_free()

    def test_stopped_child_killed_without_continue(self):
        reached = self.h.directory / 'stopped-child'
        with mock.patch.object(self.engine, 'publish', self.blocking_publish(reached, stop=True)):
            started = time.monotonic()
            value = self.tick(10)
        self.assertEqual(value['detail'], 'writer-cycle-timeout')
        self.assertGreaterEqual(time.monotonic() - started, 9.8)
        self.assertLess(time.monotonic() - started, 13)
        self.assertTrue(self.dead(int(reached.read_text())))
        self.assert_mutex_free()

    def test_c_call_and_pipe_with_writer_open(self):
        for operation in ('c-call', 'pipe-read'):
            reached = self.h.directory / operation

            def publish(**kw):
                with store.mutex(self.h.root):
                    reached.write_text(str(os.getpid()))
                    if operation == 'c-call':
                        hashlib.pbkdf2_hmac('sha256', b'fixture', b'salt', 500_000_000)
                    else:
                        reader, writer = os.pipe()
                        os.read(reader, 1)
                raise AssertionError('blocking operation returned')

            with mock.patch.object(self.engine, 'publish', publish):
                value = self.tick(.4)
            self.assertEqual(value['detail'], 'writer-cycle-timeout')
            self.assertTrue(reached.exists())
            self.assertTrue(self.dead(int(reached.read_text())))
            self.assert_mutex_free()

    def test_parent_death_stopped_child_frees_both_locks(self):
        reached = self.h.directory / 'parent-death'
        with mock.patch.object(self.engine, 'publish', self.blocking_publish(reached, stop=True)):
            parent = self.supervisor(timeout=5)
        child = int(self.wait_file(reached))
        self.assertFalse(self.lock_free(self.h.root / 'writer.lock'))
        self.assertFalse(self.lock_free(self.h.root / 'admission.lock'))
        os.kill(parent, signal.SIGKILL)
        os.waitpid(parent, 0)
        self.wait_dead(child)
        self.assertTrue(self.lock_free(self.h.root / 'writer.lock'))
        self.assert_mutex_free()

    def test_before_prctl_parent_death_never_enters_admission(self):
        reached = self.h.directory / 'before-prctl'
        worked = self.h.directory / 'unprotected-work'
        real_prctl = cli._parent_death_signal()

        def prctl(*args):
            reached.write_text(str(os.getpid()))
            os.kill(os.getpid(), signal.SIGSTOP)
            return real_prctl(*args)

        def prepare(*args, **kwargs):
            worked.write_text('unsafe')

        with mock.patch.object(cli, '_parent_death_signal', return_value=prctl), \
                mock.patch.object(self.engine, 'prepare', prepare):
            parent = self.supervisor(timeout=5)
        child = int(self.wait_file(reached))
        self.addCleanup(lambda: None if self.dead(child) else os.kill(child, signal.SIGKILL))
        os.kill(parent, signal.SIGKILL)
        os.waitpid(parent, 0)
        self.assert_mutex_free()
        self.assertFalse(self.lock_free(self.h.root / 'writer.lock'))
        self.assertFalse(worked.exists())
        os.kill(child, signal.SIGCONT)
        self.wait_dead(child)
        self.assertFalse(worked.exists())
        self.assertTrue(self.lock_free(self.h.root / 'writer.lock'))

    def test_prctl_failure_aborts_before_work(self):
        worked = self.h.directory / 'failed-prctl-work'
        with mock.patch.object(cli, '_parent_death_signal', return_value=lambda *a: -1), \
                mock.patch.object(self.engine, 'prepare', side_effect=lambda: worked.write_text('bad')):
            value = self.tick()
        self.assertEqual(value['schema'], 'pao/desk-error@1')
        self.assertFalse(worked.exists())
        self.assert_mutex_free()

    def test_stop_around_fork_masks_and_waitpid(self):
        for phase in ('before-fork', 'after-fork', 'before-mask-restore', 'after-waitpid', 'during-cleanup'):
            with self.subTest(phase=phase):
                events = self.h.directory / ('stop-race-' + phase)

                def configure():
                    supervisor_pid = os.getpid()
                    real_fork, real_mask, real_wait, real_kill = os.fork, signal.pthread_sigmask, os.waitpid, os.kill
                    fired = False

                    def fire():
                        nonlocal fired
                        if os.getpid() == supervisor_pid and not fired:
                            fired = True
                            real_kill(supervisor_pid, signal.SIGTERM)
                            real_kill(supervisor_pid, signal.SIGTERM)

                    def fork():
                        if phase == 'before-fork':
                            fire()
                        pid = real_fork()
                        if pid > 0:
                            with events.open('a') as log:
                                log.write('child %s\n' % pid)
                            if phase in ('after-fork', 'during-cleanup'):
                                fire()
                        return pid

                    def mask(how, value):
                        if how == signal.SIG_SETMASK and phase == 'before-mask-restore':
                            fire()
                        return real_mask(how, value)

                    def wait(pid, options):
                        if options == 0 and phase == 'during-cleanup':
                            real_kill(supervisor_pid, signal.SIGTERM)
                            real_kill(supervisor_pid, signal.SIGTERM)
                        result = real_wait(pid, options)
                        if result[0]:
                            with events.open('a') as log:
                                log.write('reaped %s\n' % result[0])
                            if phase == 'after-waitpid':
                                fire()
                        return result

                    def kill(pid, signum):
                        if pid != supervisor_pid:
                            with events.open('a') as log:
                                log.write('signal %s\n' % pid)
                        return real_kill(pid, signum)

                    os.fork, signal.pthread_sigmask, os.waitpid, os.kill = fork, mask, wait, kill

                parent = self.supervisor(configure=configure)
                _, status = os.waitpid(parent, 0)
                self.assertEqual(os.waitstatus_to_exitcode(status), 143)
                text = events.read_text()
                lines = text.splitlines()
                child = int(next(line.split()[1] for line in lines if line.startswith('child ')))
                self.wait_dead(child)
                reaped = lines.index('reaped %s' % child)
                self.assertNotIn('signal %s' % child, lines[reaped + 1:])
                self.assert_mutex_free()
                self.assertTrue(self.lock_free(self.h.root / 'writer.lock'))

    def test_cut_with_temporary_stream_directory_and_life_probe_open(self):
        original_record = store.read_json(self.engine.record_path('stale'))
        for function, marker in ((store.atomic_json, 'stream.flush()'),
                                 (store.atomic_json, 'os.fsync(directory)'),
                                 (store.held, 'fcntl.flock(fd,')):
            with self.subTest(marker=marker):
                self.engine.write_record(original_record)
                lines, first = inspect.getsourcelines(function)
                target = next(first + n for n, line in enumerate(lines) if marker in line)
                observed = self.h.directory / ('resource-' + str(first) + '-' + str(target))
                parent = os.getpid()

                def trace(frame, event, arg):
                    if (os.getpid() != parent and event == 'line' and frame.f_code is function.__code__
                            and frame.f_lineno == target):
                        descriptors = []
                        for item in os.listdir('/proc/self/fd'):
                            with contextlib.suppress(FileNotFoundError):
                                descriptors.append(os.readlink('/proc/self/fd/' + item))
                        observed.write_text(json.dumps([os.getpid(), descriptors]))
                        time.sleep(60)
                    return trace

                prior = sys.gettrace()
                try:
                    sys.settrace(trace)
                    value = self.tick(1)
                finally:
                    sys.settrace(prior)
                self.assertIn('writer-cycle-timeout', value['detail'])
                pid, descriptors = json.loads(observed.read_text())
                self.assertTrue(self.dead(pid))
                if marker == 'stream.flush()':
                    self.assertTrue(any('/.pao-' in name for name in descriptors))
                elif marker == 'os.fsync(directory)':
                    self.assertIn(str(self.h.root / 'pao-wait'), descriptors)
                else:
                    self.assertIn(str(self.engine.life_path('hasito', 1)), descriptors)
                self.assert_mutex_free()
        self.assertEqual(self.tick()['mode'], 'night')

    def test_stopped_supervisor_child_timer_still_frees_admission(self):
        reached = self.h.directory / 'stopped-supervisor'
        with mock.patch.object(self.engine, 'publish', self.blocking_publish(reached)):
            parent = self.supervisor(timeout=.6)
        child = int(self.wait_file(reached))
        os.kill(parent, signal.SIGSTOP)
        self.wait_dead(child)
        self.assert_mutex_free()
        self.assertFalse(self.lock_free(self.h.root / 'writer.lock'))
        os.kill(parent, signal.SIGCONT)
        self.wait_file(self.h.directory / 'log-1')
        os.kill(parent, signal.SIGTERM)
        _, status = os.waitpid(parent, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 143)

    def test_stop_supervisor_during_tick_and_wait(self):
        for during_tick in (True, False):
            with self.subTest(during_tick=during_tick):
                reached = self.h.directory / ('term-' + str(during_tick))
                if during_tick:
                    with mock.patch.object(self.engine, 'publish', self.blocking_publish(reached)):
                        parent = self.supervisor(timeout=5)
                    child = int(self.wait_file(reached))
                else:
                    parent = self.supervisor()
                    self.wait_file(self.h.directory / 'log-1')
                    child = None
                os.kill(parent, signal.SIGTERM)
                _, status = os.waitpid(parent, 0)
                self.assertEqual(os.waitstatus_to_exitcode(status), 143)
                if child:
                    self.wait_dead(child)
                self.assert_mutex_free()
                self.assertTrue(self.lock_free(self.h.root / 'writer.lock'))
                if (self.h.directory / 'log-1').exists():
                    (self.h.directory / 'log-1').unlink()

    def test_signal_child_only_then_next_tick(self):
        reached = self.h.directory / 'term-child'

        def publish(**kw):
            with store.mutex(self.h.root):
                reached.write_text(str(os.getpid()))
                os.kill(os.getpid(), signal.SIGTERM)

        with mock.patch.object(self.engine, 'publish', publish):
            value = self.tick()
        self.assertIn('writer-cycle-worker-exit', value['detail'])
        self.assertEqual(self.tick()['mode'], 'night')
        self.assert_mutex_free()

    def test_flock_negative_control_and_live_workload(self):
        life = self.engine.life_path('hasito', 1)
        fd = store.open_file(life)
        fcntl.flock(fd, fcntl.LOCK_EX)
        reader, writer = os.pipe()
        holder = os.fork()
        if holder == 0:
            os.close(writer)
            os.read(reader, 1)
            os._exit(0)
        os.close(reader)
        os.close(fd)
        try:
            self.assertFalse(self.lock_free(life), 'probe must detect the inherited flock')
            self.assertEqual(self.tick()['mode'], 'night')
            self.assertEqual(store.read_json(self.engine.record_path('stale'))['claim']['state'], 'uncertain')
            reached = self.h.directory / 'live-workload'
            with mock.patch.object(self.engine, 'publish', self.blocking_publish(reached)):
                self.assertEqual(self.tick(.2)['detail'], 'writer-cycle-timeout')
            self.assertFalse(self.lock_free(life), 'writer death released a live workload')
            self.assertEqual(self.tick()['mode'], 'night')
            self.assertEqual(store.read_json(self.engine.record_path('stale'))['claim']['state'], 'uncertain')
            self.assert_mutex_free()
        finally:
            os.close(writer)
            os.waitpid(holder, 0)
        self.assertTrue(self.lock_free(life))

    def test_parent_fds_do_not_grow_after_1030_open_cuts(self):
        parent = os.getpid()
        real_open = os.open
        reached = self.h.directory / 'open-cut'
        old_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (1024, old_limit[1]))
        try:
            with store.mutex(self.h.root, name='writer.lock') as writer:
                baseline = len(os.listdir('/proc/self/fd'))
                inodes = {name: (self.h.root / name).stat().st_ino
                          for name in ('writer.lock', 'admission.lock')}

                def opened(path, *args, **kwargs):
                    fd = real_open(path, *args, **kwargs)
                    if os.getpid() != parent and Path(path).name == 'admission.lock':
                        reached.write_text(str(os.getpid()))
                        time.sleep(60)
                    return fd

                with mock.patch.object(os, 'open', opened):
                    for iteration in range(1030):
                        value = cli.writer_tick(self.engine, writer, _timeout=.04)
                        self.assertEqual(value['detail'], 'writer-cycle-timeout')
                        self.assertTrue(reached.exists())
                        reached.unlink()
                self.assertEqual(len(os.listdir('/proc/self/fd')), baseline)
                self.assertFalse(self.lock_free(self.h.root / 'writer.lock'))
                self.assertEqual(inodes, {name: (self.h.root / name).stat().st_ino for name in inodes})
                self.assertEqual(cli.writer_tick(self.engine, writer)['mode'], 'night')
            self.assert_mutex_free()
        finally:
            resource.setrlimit(resource.RLIMIT_NOFILE, old_limit)

    def test_inherited_fds_closed_and_parent_never_has_admission(self):
        sentinel = self.h.directory / 'foreign-fd'
        fd = os.open(sentinel, os.O_RDWR | os.O_CREAT, 0o600)
        observed = self.h.directory / 'fd-inventory'
        parent = os.getpid()

        def inventory():
            result = []
            for name in os.listdir('/proc/self/fd'):
                with contextlib.suppress(FileNotFoundError):
                    result.append(os.readlink('/proc/self/fd/' + name))
            return result

        def publish(**kw):
            with store.mutex(self.h.root):
                own = inventory()
                theirs = []
                for name in os.listdir('/proc/%s/fd' % parent):
                    with contextlib.suppress(FileNotFoundError):
                        theirs.append(os.readlink('/proc/%s/fd/%s' % (parent, name)))
                observed.write_text(json.dumps([own, theirs]))
                time.sleep(60)

        try:
            with mock.patch.object(self.engine, 'publish', publish):
                self.tick(.3)
            own, theirs = json.loads(observed.read_text())
            self.assertNotIn(str(sentinel), own)
            self.assertIn(str(sentinel), theirs)
            self.assertIn(str(self.h.root / 'admission.lock'), own)
            self.assertNotIn(str(self.h.root / 'admission.lock'), theirs)
            self.assertIn(str(self.h.root / 'writer.lock'), own)
            self.assertIn(str(self.h.root / 'writer.lock'), theirs)
            self.assert_mutex_free()
        finally:
            os.close(fd)

    def test_pipe_and_fork_failures_preserve_parent_fds(self):
        for name in ('pipe', 'fork'):
            with self.subTest(name=name):
                baseline = len(os.listdir('/proc/self/fd'))
                with mock.patch.object(os, name, side_effect=OSError('creation-fixture')):
                    with self.assertRaises(OSError):
                        self.tick()
                self.assertEqual(len(os.listdir('/proc/self/fd')), baseline)
                self.assert_mutex_free()

    def test_protocol_integrity_and_pipe_pressure(self):
        real_send = cli._writer_send
        for mode in ('large-split', 'partial', 'duplicate', 'out-of-order', 'garbage', 'error-after-result',
                     'non-dict', 'bad-policy', 'duplicate-key', 'oversize', 'non-finite'):
            with self.subTest(mode=mode):
                def send(fd, kind, **payload):
                    if kind == 'result':
                        data = json.dumps(dict(kind=kind, **payload)).encode()
                        if mode == 'partial':
                            os.write(fd, data[:20])
                            os._exit(75)
                        if mode == 'out-of-order':
                            real_send(fd, 'commit-start')
                        if mode == 'large-split':
                            policy = json.dumps(payload['value']).encode()
                            policy = policy[:-1] + b' ' * (65536 - len(policy)) + b'}'
                            data = b'{"kind":"result","value":' + policy + b'}\n'
                            for offset in range(0, len(data), 997):
                                os.write(fd, data[offset:offset + 997])
                            return
                        if mode in ('non-dict', 'bad-policy', 'duplicate-key', 'oversize', 'non-finite'):
                            malformed = {'non-dict': b'[]\n',
                                'bad-policy': b'{"kind":"result","value":{"schema":"pao/desk-policy@1","revision":2}}\n',
                                'duplicate-key': b'{"kind":"result","kind":"error"}\n',
                                'oversize': b'x' * (128 * 1024 + 1),
                                'non-finite': b'{"kind":"error","detail":NaN}\n'}[mode]
                            while malformed:
                                size = os.write(fd, malformed)
                                malformed = malformed[size:]
                            return
                        real_send(fd, kind, **payload)
                        if mode == 'duplicate':
                            real_send(fd, kind, **payload)
                        elif mode == 'garbage':
                            os.write(fd, b'bad\n')
                        elif mode == 'error-after-result':
                            real_send(fd, 'error', detail='late-exit')
                        return
                    return real_send(fd, kind, **payload)

                with mock.patch.object(cli, '_writer_send', send):
                    value = self.tick()
                if mode in ('large-split', 'error-after-result'):
                    self.assertEqual(value['schema'], 'pao/desk-policy@1')
                else:
                    self.assertIn('publication-unknown', value['detail'])
                self.assert_mutex_free()
        # Lost integrity before commit must also report uncertainty.
        def corrupt(fd, kind, **payload):
            if kind == 'commit-start':
                os.write(fd, b'{"kind":')
                os._exit(75)
            return real_send(fd, kind, **payload)
        before = (self.h.root / 'pao-policy-v1.json').read_bytes()
        with mock.patch.object(cli, '_writer_send', corrupt):
            value = self.tick()
        self.assertIn('publication-unknown', value['detail'])
        self.assertEqual((self.h.root / 'pao-policy-v1.json').read_bytes(), before)

    def test_diagnostics_deduplicated_across_children_and_transport_failure(self):
        attempts = self.h.directory / 'repair-attempts'
        seen = set()

        def broken(record):
            with attempts.open('a') as stream:
                stream.write('attempt\n')
            raise OSError('receipt-fixture')

        with mock.patch.object(self.engine, 'reconcile_receipt', broken), \
                contextlib.redirect_stderr(io.StringIO()) as log:
            for _ in range(3):
                self.assertEqual(self.tick(diagnostics=seen)['mode'], 'night')
        self.assertEqual(attempts.read_text().count('attempt'), 3)
        self.assertEqual(len(log.getvalue().splitlines()), 1)
        record = store.read_json(self.engine.record_path('stale'))
        record['boot_id'] = record['claim']['boot_id'] = '11223344-5566-7788-99aa-bbccddeeff00'
        self.engine.write_record(record)
        with mock.patch.object(self.engine, 'reconcile_receipt', side_effect=ValueError('receipt-fixture')), \
                contextlib.redirect_stderr(io.StringIO()) as log:
            self.tick(diagnostics=seen)
        self.assertEqual(len(log.getvalue().splitlines()), 1)
        # Same text, different exception class and boot remain distinct identities.
        with mock.patch.object(self.engine, 'reconcile_receipt', side_effect=ValueError('receipt-fixture')), \
                contextlib.redirect_stderr(io.StringIO()) as log:
            self.tick(diagnostics=seen)
        self.assertEqual(len(log.getvalue().splitlines()), 1)
        real_send = cli._writer_send

        def fail(fd, kind, **payload):
            if kind == 'receipt-diagnostic':
                os.close(fd)
            return real_send(fd, kind, **payload)

        before = (self.h.root / 'pao-policy-v1.json').read_bytes()
        with mock.patch.object(self.engine, 'reconcile_receipt', broken), \
                mock.patch.object(cli, '_writer_send', fail):
            value = self.tick()
        self.assertEqual(value['schema'], 'pao/desk-error@1')
        self.assertEqual((self.h.root / 'pao-policy-v1.json').read_bytes(), before)
        self.assert_mutex_free()
        with mock.patch.object(self.engine, 'reconcile_receipt', broken), \
                contextlib.redirect_stderr(io.StringIO()) as log:
            self.engine.reconcile()
            self.engine.reconcile()
        self.assertEqual(len(log.getvalue().splitlines()), 1)

    def test_jale_archive_tombstone_receipt_and_revision_converge(self):
        record = store.read_json(self.engine.record_path('stale'))
        record.update(project='jale', kind='verify')
        record.pop('lane')
        self.engine.write_record(record)
        os.close(store.open_file(self.engine.life_path('jale', 1), create=True))
        metadata = self.h.root / 'slot-0'
        metadata.mkdir(mode=0o700)
        (metadata / 'evidence').write_text('keep')
        real_rename = os.rename
        reached = self.h.directory / 'jale-rename'

        def rename(source, target):
            result = real_rename(source, target)
            if Path(source) == metadata:
                reached.write_text(str(os.getpid()))
                time.sleep(60)
            return result

        with mock.patch.object(os, 'rename', rename):
            self.assertEqual(self.tick()['detail'], 'writer-cycle-timeout')
        self.assertTrue(reached.exists())
        self.assertFalse(metadata.exists())
        self.assertEqual(store.read_json(self.engine.record_path('stale'))['claim']['state'], 'uncertain')
        self.assertTrue((self.h.root / '.pao-reconciled-stale/evidence').exists())
        # A second cut leaves the tombstone but not its receipt.
        with mock.patch.object(self.engine, 'recover_receipt', side_effect=lambda record: time.sleep(60)):
            self.assertEqual(self.tick()['detail'], 'writer-cycle-timeout')
        self.assertEqual(store.read_json(self.engine.record_path('stale'))['claim']['state'], 'closed')
        receipt = self.h.directory / 'runs/run-stale/pao-admission.json'
        self.assertNotEqual(store.read_json(receipt)['claim_state'], 'closed')
        store.atomic_json(self.h.root / 'pao-control.json', {'schema': 'pao/desk-control@1', 'revision': 17})
        (self.h.root / '.pao-incomplete').write_text('{')
        self.assertEqual(self.tick()['revision'], 18)
        self.assertEqual(store.read_json(receipt)['claim_state'], 'closed')
        self.assertEqual((self.h.root / '.pao-incomplete').read_text(), '{')
        self.assert_mutex_free()

    def test_administrative_publish_between_commit_and_report(self):
        real_send = cli._writer_send
        administrative = self.h.directory / 'administrative'

        def send(fd, kind, **payload):
            if kind == 'result':
                other = self.engine.publish(disable_night=True)
                administrative.write_text(json.dumps(other))
            return real_send(fd, kind, **payload)

        with mock.patch.object(cli, '_writer_send', send):
            result = self.tick()
        self.assertEqual(result['mode'], 'night')
        current = store.read_json(self.h.root / 'pao-policy-v1.json')
        self.assertEqual(current['mode'], 'day')
        self.assertEqual(current['revision'], result['revision'] + 1)
        with mock.patch.object(cli, 'DeskPolicy', return_value=self.engine), \
                mock.patch.object(os, 'fork', side_effect=AssertionError('direct fork')), \
                mock.patch.object(signal, 'setitimer', side_effect=AssertionError('direct timer')), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['publicar', '--desactivar-noche']), 0)
            self.assertEqual(cli.main(['publicar', '--una-vez']), 0)

    def test_blocked_logs_do_not_retain_admission(self):
        for stream in ('stdout', 'stderr'):
            with self.subTest(stream=stream):
                reader, writer = os.pipe2(os.O_NONBLOCK)
                try:
                    while True:
                        os.write(writer, b'x' * 4096)
                except BlockingIOError:
                    pass
                os.set_blocking(writer, True)
                pipe_path = Path('/proc/self/fd/%s' % writer)
                try:
                    if stream == 'stderr':
                        with mock.patch.object(self.engine, 'reconcile_receipt', side_effect=OSError('blocked-log')):
                            parent = self.supervisor(stderr=pipe_path)
                    else:
                        parent = self.supervisor(stdout=pipe_path)
                    time.sleep(1.5)
                    self.assertFalse(self.dead(parent))
                    self.assert_mutex_free()
                    self.assertEqual(Path('/proc/%s/task/%s/children' % (parent, parent)).read_text().strip(), '')
                    os.kill(parent, signal.SIGTERM)
                    _, status = os.waitpid(parent, 0)
                    self.assertEqual(os.waitstatus_to_exitcode(status), 143)
                finally:
                    os.close(reader)
                    os.close(writer)

    def test_ignored_alarm_is_reset_in_child(self):
        previous = signal.signal(signal.SIGALRM, signal.SIG_IGN)
        reached = self.h.directory / 'ignored-alarm'
        try:
            with mock.patch.object(self.engine, 'publish', self.blocking_publish(reached)):
                parent = self.supervisor(timeout=.5)
            child = int(self.wait_file(reached))
            os.kill(parent, signal.SIGSTOP)
            self.wait_dead(child)
            self.assert_mutex_free()
            os.kill(parent, signal.SIGCONT)
        finally:
            signal.signal(signal.SIGALRM, previous)

    def test_equivalent_state_cost(self):
        self.engine.publish()  # Remove reconciliation from both measured paths.
        rows = {'direct': [], 'supervised': []}
        initial_fds = len(os.listdir('/proc/self/fd'))

        def cpu():
            own = resource.getrusage(resource.RUSAGE_SELF)
            children = resource.getrusage(resource.RUSAGE_CHILDREN)
            return own.ru_utime + own.ru_stime, children.ru_utime + children.ru_stime

        for iteration in range(21):
            for name, operation in (('direct', self.engine.publish), ('supervised', lambda: self.tick(10))):
                start_cpu, start_wall = cpu(), time.monotonic()
                outcome = operation()
                self.assertEqual(outcome.get('mode'), 'night', outcome)
                elapsed = time.monotonic() - start_wall
                end_cpu = cpu()
                own, children = ((end_cpu[n] - start_cpu[n]) * 1000 for n in (0, 1))
                rows[name].append((elapsed * 1000, own + children, own, children))
        for name, values in rows.items():
            print('U5 COST COLD', name, 'wall_ms', round(values[0][0], 3), 'cpu_ms', round(values[0][1], 3), flush=True)
            for column, label in ((0, 'wall_ms'), (1, 'cpu_ms'), (2, 'cpu_self_ms'), (3, 'cpu_children_ms')):
                ordered = sorted(row[column] for row in values[1:])
                print('U5 COST', name, label, 'n', len(ordered), 'p50', round(ordered[9], 3),
                      'p95', round(ordered[18], 3), flush=True)
        self.assertEqual(len(os.listdir('/proc/self/fd')), initial_fds)
        for column, label in ((0, 'wall_ms'), (1, 'cpu_ms')):
            overhead = sorted(rows['supervised'][n][column] - rows['direct'][n][column] for n in range(1, 21))
            print('U5 COST overhead', label, 'n', len(overhead), 'p50', round(overhead[9], 3),
                  'p95', round(overhead[18], 3), flush=True)
        print('U5 COST fds', initial_fds, 'maxrss_child_lifetime_kib', resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
              'affinity', sorted(os.sched_getaffinity(0)), flush=True)
        program = r'''
import datetime, json, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path('fleet').resolve()))
from deskpolicy import cli, store
from deskpolicy.engine import DeskPolicy
from deskpolicy.testing import Harness
h = Harness(sys.argv[1])
h.cpus = tuple(range(12))
h.now = datetime.datetime.fromisoformat(sys.argv[2])
h.bt = int(sys.argv[3])
engine = DeskPolicy(store.Runtime(h), prepare=sys.argv[4] == 'direct')
if sys.argv[4] == 'direct':
    result = engine.publish()
else:
    with store.mutex(engine.root, name='writer.lock') as writer:
        result = cli.writer_tick(engine, writer)
assert result['mode'] == 'night', result
'''
        for mode in rows:
            start_cpu, started = cpu(), time.monotonic()
            child = subprocess.run(['/usr/bin/python3', '-I', '-c', program, str(self.h.directory),
                self.h.now.isoformat(), str(self.h.bt), mode], capture_output=True, text=True, timeout=15)
            wall = (time.monotonic() - started) * 1000
            self.assertEqual(child.returncode, 0, child.stderr)
            own, children = cpu()
            print('U5 COST PROCESS_START', mode, 'wall_ms', round(wall, 3),
                  'cpu_ms', round((own - start_cpu[0] + children - start_cpu[1]) * 1000, 3), flush=True)

    def test_parent_channel_failure_is_unknown_and_collects_worker(self):
        reached = self.h.directory / 'channel-failure'
        parent = os.getpid()
        real_read = os.read

        def read(fd, count):
            if os.getpid() == parent and reached.exists():
                raise OSError('reader-fixture')
            return real_read(fd, count)

        def publish(**kw):
            with store.mutex(self.h.root):
                reached.write_text(str(os.getpid()))
                # Make the parent's select readable without a valid confirmation.
                self.engine.receipt_diagnostic(('root', 'boot', 'request', 'run', 'module', 'type', 'detail'),
                    {'schema': 'pao/desk-reconcile-diagnostic@1', 'request_id': 'fixture',
                     'run_id': 'fixture', 'reason': 'receipt-error', 'detail': 'fixture'})
                time.sleep(60)

        with mock.patch.object(self.engine, 'publish', publish), mock.patch.object(os, 'read', read):
            value = self.tick()
        self.assertIn('publication-unknown', value['detail'])
        self.assertIn('reader-fixture', value['detail'])
        self.assertTrue(self.dead(int(reached.read_text())))
        self.assert_mutex_free()

    def test_failed_stderr_does_not_replace_confirmed_result(self):
        class FailedLog:
            def write(self, value):
                raise OSError('stderr-fixture')

            def flush(self):
                pass

        with mock.patch.object(self.engine, 'reconcile_receipt', side_effect=OSError('receipt-fixture')), \
                contextlib.redirect_stderr(FailedLog()):
            value = self.tick()
        self.assertEqual(value['schema'], 'pao/desk-policy@1')
        self.assert_mutex_free()

    def test_inconclusive_session_proof_keeps_reservation(self):
        with mock.patch.object(engine_module, 'session_members', return_value=None):
            self.assertEqual(self.tick()['mode'], 'night')
        self.assertEqual(store.read_json(self.engine.record_path('stale'))['claim']['state'], 'uncertain')
        self.assertEqual(self.tick()['mode'], 'night')
        self.assertEqual(store.read_json(self.engine.record_path('stale'))['claim']['state'], 'closed')
