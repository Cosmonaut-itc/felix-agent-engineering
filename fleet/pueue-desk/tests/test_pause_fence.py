"""SEN-6487: emergency pause provenance, private files and simulated Pueue only."""
import copy
from contextlib import ExitStack
import json
from pathlib import Path
import sqlite3
import subprocess
from unittest.mock import patch

from test_desk_slots import DeskFixture
from test_slots import control, FakePueue


class EmergencyRestartTests(DeskFixture):
    def setUp(self):
        super().setUp()
        self.data = self.root/'data'
        self.data.mkdir()
        self.queue_path = self.data/'state.json'
        self.config = dict(data=self.data, pid_file=self.root/'pid')
        state = self.state()
        self.restart_number = 1
        state['startup_version'] = 1
        with self.engine.connect(write=True) as db:
            self.engine.store(db, state)
        def fake_run(argv, **kwargs):
            if argv[:1] == ['/bin/ps']:
                return subprocess.CompletedProcess(argv, 1, '', '')
            if argv == ['/usr/bin/docker', 'info']:
                return subprocess.CompletedProcess(argv, 0, '', '')
            raise AssertionError('No real binaries: '+str(argv))
        mock = patch.object(control.subprocess, 'run', side_effect=fake_run)
        mock.start()
        self.addCleanup(mock.stop)

    def restart(self):
        self.queue_path.write_text(json.dumps(dict(groups=copy.deepcopy(self.p.groups), tasks={})))
        with control.locked(self.engine.directory), patch.object(
                self.p, 'status', side_effect=subprocess.CalledProcessError(1, ['fake'])):
            control.prepare_start(self.engine, self.queue_path, self.config)
        self.p.groups = json.loads(self.queue_path.read_text())['groups']
        self.restart_number += 1
        self.gen = 'linux-proc-v1:'+str(self.restart_number)*64

    def incident(self, prior_pressure):
        # Both queue snapshots are Paused, but only one has durable ownership.
        state = self.state()
        state['reasons']['hasito'] = ['pressure'] if prior_pressure else []
        with self.engine.connect(write=True) as db:
            self.engine.store(db, state)
        self.p.groups['hasito']['status'] = 'Paused' if prior_pressure else 'Running'
        with patch.object(self.engine, 'store', side_effect=sqlite3.OperationalError('disk')):
            with self.assertRaises(sqlite3.OperationalError):
                self.engine.guard(dict(self.low, io=90))
        self.assertEqual(self.p.groups['hasito']['status'], 'Paused')
        self.restart()
        with patch.object(control, 'pressure', return_value=self.low):
            for _ in range(3):
                self.engine.guard(self.low)
        self.assertEqual(self.p.groups['hasito']['status'], 'Running')
        self.assertNotIn('manual', self.state()['reasons']['hasito'])
        self.assertFalse(self.engine.pause_fence('hasito').exists())

    def test_incident_managed_restart_without_prior_pressure(self):
        self.incident(prior_pressure=False)

    def test_incident_managed_restart_with_prior_pressure(self):
        self.incident(prior_pressure=True)

    def test_managed_restart_manual_marker_remains_held(self):
        self.engine.pause_fence('hasito').write_bytes(b'manual\n')
        self.p.groups['hasito']['status'] = 'Paused'
        self.restart()
        with patch.object(control, 'pressure', return_value=self.low):
            for _ in range(3):
                self.engine.guard(self.low)
        self.assertEqual(self.p.groups['hasito']['status'], 'Paused')
        self.assertIn('manual', self.state()['reasons']['hasito'])
        self.assertEqual(self.engine.pause_fence('hasito').read_bytes(), b'manual\n')

    def test_unknown_pause_without_marker_stays_manual_in_preparer_and_guard(self):
        self.p.groups['hasito']['status'] = 'Paused'
        with patch.object(control, 'pressure', return_value=self.low):
            self.engine.guard(self.low)
        self.assertEqual(self.state()['reasons']['hasito'], ['manual'])
        # Drop the inferred record to exercise the preparer's own inference too.
        state = self.state()
        state['reasons']['hasito'] = []
        with self.engine.connect(write=True) as db:
            self.engine.store(db, state)
        self.restart()
        self.assertIn('manual', self.state()['reasons']['hasito'])
        with patch.object(control, 'pressure', return_value=self.low):
            self.engine.guard(self.low)
        self.assertEqual(self.p.groups['hasito']['status'], 'Paused')


class EmergencyFenceTests(DeskFixture):
    def setUp(self):
        super().setUp()
        for name, sample in (('pressure', self.low), ('pressure_mini', dict(load=0, memory=1))):
            mock = patch.object(control, name, return_value=sample)
            mock.start()
            self.addCleanup(mock.stop)

    def fixture(self, host):
        if host == 'desk':
            return self.engine, self.p, 'hasito', dict(self.low, io=90), self.low
        p = FakePueue()
        engine = control.MiniControl(p, self.root/'mini', clock=lambda:1234,
                                     generation=lambda:'daemon-1')
        with control.locked(engine.directory):
            engine.adopt()
        return engine, p, 'suites', dict(load=4, memory=1), dict(load=0, memory=1)

    def read(self, engine):
        with engine.connect() as db:
            return engine.state(db)

    def save(self, engine, state):
        with engine.connect(write=True) as db:
            engine.store(db, state)

    def test_protective_marker_requires_state_read_and_no_manual_or_ambiguous(self):
        for host in ('desk', 'mini'):
            engine, p, group, high, _ = self.fixture(host)
            for reasons in ([], ['manual'], ['ambiguous']):
                with self.subTest(host=host, reasons=reasons):
                    state = self.read(engine)
                    state['reasons'][group] = reasons
                    self.save(engine, state)
                    p.groups[group]['status'] = 'Running'
                    path = engine.pause_fence(group)
                    if path.exists(): path.unlink()
                    with patch.object(engine, 'store', side_effect=sqlite3.OperationalError('disk')):
                        with self.assertRaises(sqlite3.OperationalError): engine.guard(high)
                    self.assertEqual(path.read_bytes(), b'manual\n' if reasons else b'pressure\n')
                    self.assertEqual(p.groups[group]['status'], 'Paused')
            p.groups[group]['status'] = 'Running'
            path.unlink()
            with patch.object(engine, 'state', side_effect=sqlite3.OperationalError('unreadable')):
                with self.assertRaises(sqlite3.OperationalError): engine.guard(high)
            self.assertEqual(path.read_bytes(), b'manual\n')
            self.assertEqual(p.groups[group]['status'], 'Paused')

    def test_protective_marker_with_read_state_despite_reconciliation_failure(self):
        for host in ('desk', 'mini'):
            engine, p, group, high, _ = self.fixture(host)
            with patch.object(engine, 'reconcile', return_value=False):
                with self.assertRaises(ValueError): engine.guard(high)
            self.assertEqual(engine.pause_fence(group).read_bytes(), b'pressure\n')
            self.assertEqual(p.groups[group]['status'], 'Paused')

    def test_conservative_content_and_read_errors_and_no_manual_downgrade(self):
        path = self.engine.pause_fence('hasito')
        for content in (b'', b'pressure', b'pressure\r\n', b'pressure\n\n', b'unknown\n', b'\xff', b'manual\n'):
            with self.subTest(content=content):
                path.write_bytes(content)
                self.assertIn('manual', self.engine.view()['pause_reasons']['hasito'])
                self.engine.fence_pause('hasito', 'pressure')
                self.assertEqual(path.read_bytes(), b'manual\n')
        path.write_bytes(b'pressure\n')
        self.assertEqual(self.engine.view()['pause_reasons']['hasito'], ['pressure'])
        with patch.object(Path, 'read_bytes', side_effect=PermissionError('unreadable')):
            self.assertIn('manual', self.engine.view()['pause_reasons']['hasito'])
        path.unlink()
        path.symlink_to(self.root/'missing')
        self.assertIn('manual', self.engine.view()['pause_reasons']['hasito'])

    def test_manual_pause_promotes_marker_before_store_and_rpc_then_survives_state_loss(self):
        for host in ('desk', 'mini'):
            engine, p, group, _, low = self.fixture(host)
            path = engine.pause_fence(group)
            path.write_bytes(b'pressure\n')
            old = self.read(engine)
            observed = []
            synced = []
            store = engine.store
            def checked_store(db, state):
                self.assertEqual(path.read_bytes(), b'manual\n')
                self.assertGreaterEqual(len(synced), 2)  # file and directory fsync
                self.assertNotIn(('pause', '--wait', '-g', group), p.calls)
                observed.append('store')
                return store(db, state)
            # manual stores twice; only its first store precedes the pause RPC.
            def first_store(db, state):
                if not observed: return checked_store(db, state)
                return store(db, state)
            p.calls.clear()
            fsync = control.os.fsync
            def sync(fd):
                synced.append(fd)
                return fsync(fd)
            with patch.object(engine, 'store', side_effect=first_store), \
                 patch.object(control.os, 'fsync', side_effect=sync): engine.manual(group)
            self.assertTrue(observed)
            self.save(engine, old)  # restored authority lacks the manual record
            for _ in range(3): engine.guard(low)
            self.assertEqual(p.groups[group]['status'], 'Paused')
            self.assertEqual(path.read_bytes(), b'manual\n')
            # The same operator command without a marker must not create one.
            other = 'otros' if host == 'desk' else 'default'
            engine.manual(other)
            self.assertFalse(engine.pause_fence(other).exists())

    def test_manual_promotion_survives_failed_store_and_healthy_repair(self):
        for host in ('desk', 'mini'):
            engine, p, group, _, low = self.fixture(host)
            engine.pause_fence(group).write_bytes(b'pressure\n')
            p.groups[group]['status'] = 'Paused'
            with patch.object(engine, 'store', side_effect=sqlite3.OperationalError('disk')):
                with self.assertRaises(sqlite3.OperationalError): engine.manual(group)
            engine.guard(low)
            self.assertEqual(p.groups[group]['status'], 'Paused')
            self.assertEqual(engine.pause_fence(group).read_bytes(), b'manual\n')

    def test_unknown_pause_without_marker_stays_manual_in_both_guards(self):
        for host in ('desk', 'mini'):
            engine, p, group, _, low = self.fixture(host)
            p.groups[group]['status'] = 'Paused'
            engine.guard(low)
            self.assertEqual(self.read(engine)['reasons'][group], ['manual'])
            self.assertEqual(p.groups[group]['status'], 'Paused')

    def test_pressure_marker_and_manual_state_remain_held(self):
        for host in ('desk', 'mini'):
            engine, p, group, _, low = self.fixture(host)
            state = self.read(engine)
            state['reasons'][group] = ['manual']
            self.save(engine, state)
            engine.pause_fence(group).write_bytes(b'pressure\n')
            p.groups[group]['status'] = 'Paused'
            engine.guard(low)
            self.assertEqual(p.groups[group]['status'], 'Paused')
            self.assertIn('manual', self.read(engine)['reasons'][group])

    def test_pressure_marker_deleted_only_after_committed_reason_and_retries_failed_store(self):
        for host in ('desk', 'mini'):
            engine, p, group, high, low = self.fixture(host)
            path = engine.pause_fence(group)
            path.write_bytes(b'pressure\n')
            p.groups[group]['status'] = 'Paused'
            with patch.object(engine, 'store', side_effect=sqlite3.OperationalError('disk')):
                with self.assertRaises(sqlite3.OperationalError): engine.guard(low)
            self.assertTrue(path.exists())
            self.assertEqual(p.groups[group]['status'], 'Paused')
            unlink = Path.unlink
            removed = []
            def checked_unlink(candidate, *args, **kwargs):
                if candidate == path:
                    self.assertIn('pressure', self.read(engine)['reasons'][group])
                    removed.append('committed')
                return unlink(candidate, *args, **kwargs)
            with patch.object(Path, 'unlink', checked_unlink): engine.guard(high)
            self.assertEqual(removed, ['committed'])
            self.assertFalse(path.exists())
            engine.guard(low)
            self.assertEqual(p.groups[group]['status'], 'Running')

    def test_protective_pause_survives_failed_disk_and_keeps_intent_before_rpc(self):
        for host in ('desk', 'mini'):
            engine, p, group, high, _ = self.fixture(host)
            events = []
            def fail_store(*args):
                events.append('store')
                raise sqlite3.OperationalError('disk')
            def fail_fence(*args):
                events.append('fence')
                raise OSError('disk')
            call = p.call
            def pause(*args):
                if args == ('pause', '--wait', '-g', group):
                    self.assertIn('fence', events)
                    self.assertIn('store', events)
                return call(*args)
            with patch.object(engine, 'store', side_effect=fail_store), \
                 patch.object(engine, 'fence_pause', side_effect=fail_fence), \
                 patch.object(p, 'call', side_effect=pause):
                with self.assertRaises((sqlite3.OperationalError, OSError)): engine.guard(high)
            self.assertEqual(p.groups[group]['status'], 'Paused')

    def test_failed_external_release_keeps_manual_marker_and_foreign_pause(self):
        for host in ('desk', 'mini'):
            engine, p, group, high, low = self.fixture(host)
            engine.guard(high)
            p.call('start', '-g', group)
            with patch.object(engine, 'store', side_effect=sqlite3.OperationalError('disk')):
                with self.assertRaises(sqlite3.OperationalError): engine.guard(low)
            self.assertEqual(engine.pause_fence(group).read_bytes(), b'manual\n')
            p.call('pause', '--wait', '-g', group)
            engine.guard(low)
            self.assertEqual(p.groups[group]['status'], 'Paused')

    def test_unrecovered_pressure_marker_cannot_reclaim_foreign_pause_after_external_start(self):
        for host in ('desk', 'mini'):
            engine, p, group, _, low = self.fixture(host)
            for failure in ('authority', 'reconciliation', 'unlink', 'directory_sync', 'marker_read', 'marker_io'):
                with self.subTest(host=host, failure=failure):
                    path = engine.pause_fence(group)
                    path.write_bytes(b'pressure\n')
                    state = self.read(engine)
                    state['reasons'][group] = [] if failure.startswith('marker_') else ['pressure']
                    self.save(engine, state)
                    p.groups[group]['status'] = 'Running'  # another actor started it
                    if failure.startswith('marker_'):
                        read = engine.fence_reason
                        fault = ExitStack()
                        fault.enter_context(patch.object(engine, 'fence_reason', side_effect=lambda g: 'manual' if g == group else read(g)))
                        error = None
                        if failure == 'marker_io':
                            fault.enter_context(patch.object(engine, 'fence_pause', side_effect=PermissionError('disk')))
                            error = PermissionError
                    elif failure == 'authority':
                        fault = patch.object(engine, 'state', side_effect=sqlite3.OperationalError('disk'))
                        error = sqlite3.OperationalError
                    elif failure == 'reconciliation':
                        fault = patch.object(engine, 'reconcile', return_value=False)
                        error = ValueError
                    elif failure == 'unlink':
                        fault = patch.object(Path, 'unlink', side_effect=OSError('disk'))
                        error = OSError
                    else:
                        # The mark is gone in memory, but deletion was not made durable.
                        fault = patch.object(control, 'sync_directory', side_effect=OSError('disk'))
                        error = OSError
                    with fault:
                        if error is None:
                            engine.guard(low)
                        else:
                            with self.assertRaises(error): engine.guard(low)
                    self.assertTrue(path.exists())
                    if failure == 'marker_io':
                        self.assertEqual(path.read_bytes(), b'pressure\n')
                        self.assertIn('manual', self.read(engine)['reasons'][group])
                    else:
                        self.assertEqual(path.read_bytes(), b'manual\n')
                    p.call('pause', '--wait', '-g', group)
                    engine.guard(low)
                    self.assertEqual(p.groups[group]['status'], 'Paused')
