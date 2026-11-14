"""U3.4: arranque offline del desk con dobles (cola, reloj, Pueue, systemd y archivos en /tmp)."""
import copy
from contextlib import redirect_stdout
from datetime import datetime
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_slots import control

BASE = Path(__file__).resolve().parents[1]


def utc(value):
    return datetime.fromisoformat(value + '+00:00').timestamp()


STAMP = '2026-11-14T15:00:00.123456789-06:00'


def task(ident, group, status, created=STAMP):
    return dict(id=ident, created_at=created, original_command='echo', command='echo', path='/tmp',
                envs={}, group=group, dependencies=[], priority=0, label=None, status=status)


def running(start=STAMP):
    return {'Running': dict(enqueued_at=STAMP, start=start)}


def queued():
    return {'Queued': dict(enqueued_at=STAMP)}


def done(result):
    return {'Done': dict(enqueued_at=STAMP, start=STAMP, end=STAMP, result=result)}


class DeskDaemon:
    """In-memory pueued; `start` replays the stock 4.0.4 restore subset tests depend on."""
    def __init__(self, paths, queue):
        self.paths, self.queue, self.up, self.gen, self.calls = paths, queue, True, 1, []

    def status(self):
        if not self.up:
            raise subprocess.CalledProcessError(2, ['pueue', 'status'])
        return copy.deepcopy(self.queue)

    def parallel(self, group, number):
        self.calls.append(('parallel', group, number))
        self.queue['groups'][group]['parallel_tasks'] = number

    def call(self, *args):
        self.calls.append(args)
        if args[0] in ('pause', 'start'):
            self.queue['groups'][args[-1]]['status'] = 'Paused' if args[0] == 'pause' else 'Running'
        elif args[:2] == ('group', 'remove'):
            del self.queue['groups'][args[2]]  # the controller, not Pueue, must refuse a group with tasks
        else:
            raise AssertionError('No fallback to real Pueue: ' + repr(args))

    def stop(self):
        self.paths['state'].parent.mkdir(parents=True, exist_ok=True)
        self.paths['state'].write_text(json.dumps(self.queue))
        self.up = False

    def start(self):
        queue = json.loads(self.paths['state'].read_text())
        for t in queue['tasks'].values():
            variant, detail = next(iter(t['status'].items()))
            if variant in ('Running', 'Paused'):  # stock: Killed, end=start
                t['status'] = done('Killed')
            elif variant == 'Locked':
                t['status'] = {'Stashed': dict(enqueue_at=None)}
        for name, g in queue['groups'].items():
            if g['status'] == 'Running' and any(t['group'] == name and 'Queued' in t['status'] for t in queue['tasks'].values()):
                g['status'] = 'Paused'  # stock pauses groups with Queued work
        self.queue, self.up = queue, True
        self.gen += 1


class StartupFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='u34o-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home'
        self.run = self.root / 'run'
        self.run.mkdir(parents=True)
        self.env = {'XDG_RUNTIME_DIR': str(self.run)}
        self.paths = control.config_paths(self.env, self.home, self.home)
        self.paths['logs'].mkdir(parents=True)
        self.now = utc('2026-11-14T16:00:00')
        profile = control.desk_profile(self.now)
        groups = {g: dict(status='Running', parallel_tasks=n) for g, n in profile.items()}
        groups.update(default=dict(status='Running', parallel_tasks=1), ajeno=dict(status='Running', parallel_tasks=2))
        self.d = DeskDaemon(self.paths, dict(tasks={}, groups=groups))
        self.directory = self.home / '.local/state/pao/pueue-desk'
        self.engine = self.make_engine()
        with control.locked(self.directory):
            self.engine.adopt(self.paths['pid'])
        self.low = dict(io=0, cpu=0, memory=8 * 1024**3)
        for manager in (patch.object(control, 'docker_ready', return_value=True),
                        patch.object(control, 'pressure', side_effect=lambda proc: self.low)):
            self.mock = manager.start(); self.addCleanup(manager.stop)

    def make_engine(self):
        return control.MiniControl(self.d, self.directory, clock=lambda: self.now,
                                   generation=lambda: 'gen-%d' % self.d.gen, host='desk')

    def state(self):
        with self.engine.connect() as db:
            return self.engine.state(db)

    def raw_policy(self):
        with self.engine.connect() as db:
            return db.execute('SELECT body FROM policy WHERE id=1').fetchone()[0]

    def prepare(self, **kw):
        with control.locked(self.directory):
            return self.engine.prepare(self.paths, control.PUEUED_VERSION, name_of=lambda pid: None, **kw)

    def queue(self):
        return json.loads(self.paths['state'].read_text())

    def boot(self):
        """ExecStartPre → ExecStart → first guard tick."""
        self.prepare()
        self.d.start()
        self.guard()

    def guard(self, readings=None):
        with control.locked(self.directory):
            self.engine.guard(readings or self.low)

    def mixed_queue(self):
        tasks = {
            '0': task(0, 'hasito', running()),
            '1': task(1, 'jale', {'Paused': dict(enqueued_at=STAMP, start=STAMP)}),
            '2': task(2, 'otros', queued()),
            '3': task(3, 'pesadas', queued()),
            '4': task(4, 'rapidas', {'Stashed': dict(enqueue_at=None)}),
            '5': task(5, 'jale-verify', {'Locked': dict(previous_status=queued())}),
            '6': task(6, 'hasito', done('Success')),
            '7': task(7, 'hasito', done({'Failed': 2})),
            '8': task(8, 'jale', done('Killed')),  # terminal cancel before the restart
        }
        self.d.queue['tasks'] = tasks
        (self.paths['logs'] / '3.log').write_text('salida previa')
        self.d.stop()


class TimestampTests(unittest.TestCase):
    def test_chrono_subset(self):
        for good in ('2026-11-12T08:00:00Z', '2026-11-12T08:00:00.123456789+05:30', '2024-02-29T23:59:59-23:59'):
            self.assertEqual(control.pueue_timestamp(good), good)
        for bad in ('2026-11-12T08:00:00+00:99', '2026-11-12T08:00:00-00:99', '2026-11-12T08:00:00+24:00',
                    '2026-02-30T08:00:00Z', '2025-02-29T08:00:00Z', '2026-11-12T24:00:00Z', '2026-11-12T08:60:00Z',
                    '2026-11-12T08:00:60Z', '2026-11-12 08:00:00Z', '2026-11-12T08:00:00', 20261112):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                control.pueue_timestamp(bad)


class SW1Tests(StartupFixture):
    def test_invalid_timestamps_reject_with_zero_writes_and_daemon_stopped(self):
        cases = {
            'offset-minute': task(1, 'jale', running('2026-11-12T08:00:00+00:99')),
            'negative-offset': task(1, 'jale', running('2026-11-12T08:00:00-00:99')),
            'offset-hour': task(1, 'jale', {'Queued': dict(enqueued_at='2026-11-12T08:00:00+24:00')}),
            'impossible-date': task(1, 'jale', queued(), created='2026-02-30T08:00:00Z'),
            'nested-locked': task(1, 'jale', {'Locked': dict(previous_status={'Queued': dict(enqueued_at='2026-11-12T08:00:00+00:99')})}),
            'nested-depth': task(1, 'jale', {'Locked': dict(previous_status={'Locked': dict(previous_status=queued())})}),
        }
        for name, bad in cases.items():
            with self.subTest(name=name):
                self.d.queue['tasks'] = {'1': bad}
                self.d.stop()
                before = hashlib.sha256(self.paths['state'].read_bytes()).hexdigest()
                policy = self.raw_policy()
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertEqual(hashlib.sha256(self.paths['state'].read_bytes()).hexdigest(), before)
                self.assertEqual(self.raw_policy(), policy)
                self.assertEqual(sorted(p.name for p in self.paths['state'].parent.iterdir()), ['state.json', 'task_logs'])
                self.assertFalse(self.d.up)


class StartupTests(StartupFixture):
    def test_restart_closes_every_group_fails_interrupted_and_keeps_queue(self):
        self.d.queue['groups']['suites'] = dict(status='Paused', parallel_tasks=1)
        self.d.queue['groups']['jale']['parallel_tasks'] = 9  # stale quota left by the old daemon
        self.mixed_queue()
        result = self.prepare()
        q = self.queue()
        self.assertTrue(result['prepared'])
        self.assertEqual({g['status'] for g in q['groups'].values()}, {'Paused'})  # incl. default and empty ones
        self.assertEqual({g: q['groups'][g]['parallel_tasks'] for g in control.GROUP_LIMITS}, control.desk_profile(self.now))
        self.assertEqual(list(q['tasks']), [str(i) for i in range(9)])  # same ids, same order
        killed = {k for k, t in q['tasks'].items() if t['status'].get('Done', {}).get('result') == 'Killed'}
        self.assertEqual(killed, {'0', '1', '3', '8'})
        self.assertEqual(q['tasks']['0']['status']['Done']['start'], STAMP)
        self.assertEqual(q['tasks']['2']['status'], queued())  # Queued without log keeps id and place
        self.assertEqual(q['tasks']['4']['status'], {'Stashed': dict(enqueue_at=None)})
        self.assertIn('Locked', q['tasks']['5']['status'])
        for k in ('6', '7', '8'):
            self.assertEqual(q['tasks'][k], self.d.queue['tasks'][k])
        s = self.state()
        self.assertEqual([i['id'] for i in s['startup']['interrupted']], [0, 1, 3])  # never the old cancel
        self.assertEqual(s['startup']['held'], {'pesadas': 'log_existente', 'jale-verify': 'bloqueada',
                                                'default': 'fuera_del_reparto', 'ajeno': 'fuera_del_reparto'})
        self.assertEqual(s['reasons']['hasito'], ['startup'])
        self.assertEqual(s['reasons']['pesadas'], ['ambiguous', 'startup'])
        self.assertNotIn('suites', s['reasons'])  # a foreign group already paused is left alone
        self.assertEqual(s['startup']['phase'], 'prepared')
        # Pueue starts the prepared queue; the guard opens only its own startup closures.
        self.d.start()
        self.guard()
        starts = {c[-1] for c in self.d.calls if c[0] == 'start'}
        self.assertEqual(starts, {'hasito', 'jale', 'otros', 'rapidas'})
        self.assertEqual(self.state()['startup']['phase'], 'started')
        for g in ('pesadas', 'jale-verify', 'default', 'ajeno', 'suites'):
            self.assertEqual(self.d.queue['groups'][g]['status'], 'Paused', g)
        v = self.engine.view()
        self.assertEqual(v['startup_held']['pesadas'], 'log_existente')
        self.assertEqual([i['id'] for i in v['interrupted']], [0, 1, 3])
        self.assertIsNone(v['startup_docker'])  # no group waits for docker anymore... except held ones
        # Felix resolves retained groups explicitly, also those outside the table.
        for g in ('pesadas', 'default'):
            with control.locked(self.directory):
                self.engine.manual(g, resume=True)
            self.assertEqual(self.d.queue['groups'][g]['status'], 'Running')
            self.assertNotIn(g, self.state()['startup']['held'])

    def test_manual_and_guard_pauses_survive_restart(self):
        self.d.queue['groups']['otros']['status'] = 'Paused'
        self.d.queue['groups']['rapidas']['status'] = 'Paused'
        s = self.state(); s['reasons'].update(otros=['manual'], rapidas=['pressure'])
        with self.engine.connect(write=True) as db:
            self.engine.store(db, s)
        self.engine.fence_pause('hasito')
        self.d.stop()
        self.boot()
        s = self.state()
        self.assertEqual(s['reasons']['otros'], ['manual'])
        self.assertEqual(self.d.queue['groups']['otros']['status'], 'Paused')
        self.assertEqual(self.d.queue['groups']['hasito']['status'], 'Paused')  # emergency fence
        self.assertEqual(self.d.queue['groups']['rapidas']['status'], 'Running')  # own pressure reason
        self.assertNotIn('pausa_previa', s['startup']['held'].values())

    def test_paused_profile_group_without_reason_is_retained(self):
        self.d.queue['groups']['otros']['status'] = 'Paused'  # paused by a direct pueue call
        self.d.stop()
        self.boot()
        self.assertEqual(self.state()['startup']['held'].get('otros'), 'pausa_previa')  # visible motive
        self.assertEqual(self.d.queue['groups']['otros']['status'], 'Paused')

    def test_docker_gate_keeps_startup_closure_until_ready(self):
        self.d.stop()
        self.prepare(); self.d.start()
        with patch.object(control, 'docker_ready', return_value=False):
            self.guard()
        self.assertFalse(any(c[0] == 'start' for c in self.d.calls))
        self.assertIs(self.engine.view()['startup_docker'], False)
        self.guard()
        self.assertTrue(all(self.d.queue['groups'][g]['status'] == 'Running' for g in control.GROUP_LIMITS))
        self.assertIsNone(self.engine.view()['startup_docker'])

    def test_pressure_blocks_startup_release(self):
        self.d.stop()
        self.prepare(); self.d.start()
        self.guard(dict(self.low, io=60))
        self.assertEqual([c for c in self.d.calls if c[0] == 'start'], [('start', '-g', 'rapidas')])  # #118 ladder
        self.assertEqual(self.state()['reasons']['jale'], ['startup'])

    def test_external_start_ends_startup_ownership(self):
        self.d.stop(); self.prepare(); self.d.start()
        self.d.queue['groups']['jale']['status'] = 'Running'
        self.guard(dict(self.low, io=30))  # hysteresis band: keep
        self.assertNotIn('startup', self.state()['reasons'].get('jale', []))

    def test_prepare_twice_same_decisions_without_duplicates(self):
        self.mixed_queue()
        self.prepare()
        first, policy = self.paths['state'].read_bytes(), self.state()
        self.prepare()
        self.assertEqual(self.paths['state'].read_bytes(), first)
        again = self.state()
        for key in ('reasons', 'desired'):
            self.assertEqual(again[key], policy[key])
        for key in ('held', 'interrupted', 'phase'):
            self.assertEqual(again['startup'][key], policy['startup'][key])

    def reference(self):
        other = StartupFixture('run')
        other.setUp()
        self.addCleanup(other.doCleanups)
        other.mixed_queue(); other.prepare()
        return other.queue(), other.state()

    def test_crash_at_each_boundary_converges(self):
        ref_queue, ref_state = self.reference()
        for boundary in ('before-intent', 'after-intent', 'after-queue'):
            with self.subTest(boundary=boundary):
                self.setUp()
                self.mixed_queue()
                source = self.paths['state'].read_bytes()
                stores, original = [], self.engine.store

                def store(db, state):
                    stores.append(state['startup']['phase'])
                    if boundary == 'before-intent' or boundary == 'after-queue' and len(stores) == 2:
                        raise OSError('caída')
                    original(db, state)
                crash = (patch.object(self.engine, 'store', side_effect=store) if boundary != 'after-intent'
                         else patch.object(control, 'atomic_replace', side_effect=OSError('caída')))
                with crash, self.assertRaises(OSError):
                    self.prepare()
                phase = self.state()['startup']['phase']
                if boundary == 'before-intent':
                    self.assertEqual(phase, 'none')
                    self.assertEqual(self.paths['state'].read_bytes(), source)
                elif boundary == 'after-intent':
                    self.assertEqual(phase, 'preparing')
                    self.assertEqual(self.paths['state'].read_bytes(), source)
                else:
                    self.assertEqual(phase, 'preparing')
                    self.assertNotEqual(self.paths['state'].read_bytes(), source)
                self.prepare()  # systemd retries ExecStartPre
                self.assertEqual(self.queue(), ref_queue)
                s = self.state()
                self.assertEqual(s['reasons'], ref_state['reasons'])
                self.assertEqual(s['startup']['held'], ref_state['startup']['held'])
                self.assertEqual([i['id'] for i in s['startup']['interrupted']], [0, 1, 3])
                self.assertEqual(s['startup']['phase'], 'prepared')

    def test_unfinished_preparation_never_reopens_on_start(self):
        self.mixed_queue()  # otros and pesadas keep Queued work, so stock pauses them
        with patch.object(control, 'atomic_replace', side_effect=OSError('caída')), self.assertRaises(OSError):
            self.prepare()
        self.assertEqual(self.state()['startup']['phase'], 'preparing')
        self.d.start()  # someone started pueued outside the unit
        self.guard()
        self.assertFalse(any(c[0] == 'start' for c in self.d.calls))
        self.assertIn('ambiguous', self.state()['reasons']['otros'])

    def test_absent_or_corrupt_queue_blocks_and_preserves_files(self):
        policy = self.raw_policy()
        self.d.up = False
        with self.assertRaisesRegex(ValueError, 'Falta state.json'):
            self.prepare()
        self.assertFalse(self.paths['state'].exists())
        for name, raw in (('json', b'{"tasks":'), ('dup', b'{"tasks":{},"tasks":{},"groups":{}}'),
                          ('nan', b'{"tasks":{},"groups":{"a":{"status":"Paused","parallel_tasks":NaN}}}'),
                          ('reset', json.dumps(dict(tasks={}, groups=dict(a=dict(status='Reset', parallel_tasks=1)))).encode()),
                          ('orphan', json.dumps(dict(tasks={'1': task(1, 'nadie', queued())}, groups={})).encode()),
                          ('gz', b'{"tasks":{},"groups":{}}')):
            with self.subTest(name=name):
                self.paths['state'].parent.mkdir(parents=True, exist_ok=True)
                self.paths['state'].write_bytes(raw)
                gz = self.paths['state'].with_name('state.json.gz')
                if name == 'gz':
                    gz.write_bytes(b'x')
                with self.assertRaises((ValueError, UnicodeDecodeError)):
                    self.prepare()
                self.assertEqual(self.paths['state'].read_bytes(), raw)
                self.assertEqual(self.raw_policy(), policy)
                if gz.exists():
                    gz.unlink()

    def test_unreadable_authority_blocks_without_default_table(self):
        self.d.stop()
        source = self.paths['state'].read_bytes()
        for kind in ('corrupt', 'missing-db'):
            with self.subTest(kind=kind):
                if kind == 'corrupt':
                    self.engine.path.write_bytes(b'not sqlite')
                else:
                    self.engine.path.unlink()
                with self.assertRaises((sqlite3.Error, ValueError)):
                    self.prepare()
                self.assertEqual(self.paths['state'].read_bytes(), source)
                self.assertFalse(self.engine.path.exists() and kind == 'missing-db')

    def test_live_or_answering_daemon_refuses(self):
        self.d.stop()
        source = self.paths['state'].read_bytes()
        self.d.up = True
        with self.assertRaisesRegex(ValueError, 'Otro pueued responde'):
            self.prepare()
        self.d.up = False
        self.paths['pid'].write_text('4242\n')
        with control.locked(self.directory), self.assertRaisesRegex(ValueError, 'sigue vivo'):
            self.engine.prepare(self.paths, control.PUEUED_VERSION, name_of=lambda pid: 'pueued')
        self.assertEqual(self.paths['state'].read_bytes(), source)
        self.assertTrue(self.paths['pid'].exists())
        with control.locked(self.directory), self.assertRaisesRegex(ValueError, '4.0.4'):
            self.engine.prepare(self.paths, 'pueued 4.0.5', name_of=lambda pid: None)
        self.prepare()  # dead or reused PID: removed after the queue is written
        self.assertFalse(self.paths['pid'].exists())

    def test_day_night_crossing_during_preparation(self):
        self.now = utc('2026-11-15T04:59:59')
        for g, n in control.desk_profile(utc('2026-11-14T16:00:00')).items():
            self.d.queue['groups'][g]['parallel_tasks'] = n
        self.d.stop()
        original = control.atomic_replace
        crossed = []

        def write(*args, **kw):
            original(*args, **kw)
            if not crossed:
                crossed.append(True)
                self.now = utc('2026-11-15T05:00:01')  # event crosses after the first write
        with patch.object(control, 'atomic_replace', side_effect=write):
            self.prepare()
        q, s = self.queue(), self.state()
        self.assertEqual({g: q['groups'][g]['parallel_tasks'] for g in control.GROUP_LIMITS},
                         control.desk_profile(self.now))
        self.assertEqual(s['schedule_event'], utc('2026-11-15T05:00:00'))
        self.assertEqual(s['desired'], control.desk_profile(self.now))

    def test_expired_advance_and_adjustments_are_not_restored(self):
        s = self.state()
        s['overrides'] = dict(jale=dict(slots=4, actor='Felix', action_id='fixture-000001', requested_at=self.now,
                                        expires_at=control.schedule_at(self.now)[1]))
        s['mode_override'] = dict(mode='noche', actor='Felix', action_id='fixture-000002', requested_at=self.now,
                                  expires_at=control.schedule_at(self.now)[1])
        s['desired'] = dict(control.mode_profile('noche'), jale=4)
        with self.engine.connect(write=True) as db:
            self.engine.store(db, s)
        self.d.stop()
        self.now = utc('2026-11-15T06:00:00')  # restart after the event
        self.prepare()
        s = self.state()
        self.assertEqual((s['overrides'], s['mode_override']), ({}, None))
        self.assertEqual({g: self.queue()['groups'][g]['parallel_tasks'] for g in control.GROUP_LIMITS},
                         control.mode_profile('noche'))

    def test_stop_records_interrupted_and_snapshot_identity(self):
        self.d.queue['tasks'] = {'0': task(0, 'jale', running()), '1': task(1, 'otros', queued())}
        with control.locked(self.directory):
            self.assertEqual(self.engine.record_stop(), [0])
        # Stock shutdown already killed it: preparar finds Done/Killed, the index keeps the identity.
        self.d.queue['tasks']['0']['status'] = done('Killed')
        self.d.stop()
        self.prepare(); self.d.start()
        self.assertEqual([(i['id'], i['created_at']) for i in self.engine.view()['interrupted']], [(0, STAMP)])
        self.now += 86401
        self.assertEqual(self.engine.view()['interrupted'], [])

    def test_interrupted_index_is_bounded(self):
        self.d.queue['tasks'] = {str(i): task(i, 'jale', running()) for i in range(80)}
        self.d.stop()
        self.prepare()
        try:
            ids = [i['id'] for i in self.state()['startup']['interrupted']]
        except ValueError as error:  # an unbounded index leaves the authority unreadable
            self.fail('La autoridad quedó ilegible: ' + str(error))
        self.assertEqual(ids, list(range(16, 80)))

    def test_maintenance_order_marker_and_no_reopen(self):
        commands = []
        marker = self.directory / 'maintenance'

        def run(*argv):
            commands.append((argv, marker.exists()))
        control.maintenance(self.directory, 'entrar', run)
        self.assertEqual([c for c, _ in commands], [
            ('systemctl', '--user', 'stop', 'pueue-desk-mode.timer', 'pueue-desk-guard.timer'),
            ('systemctl', '--user', 'stop', 'pueue-desk-guard.service', 'pueue-desk-mode.service'),
            ('systemctl', '--user', 'stop', 'pueued.service')])
        self.assertTrue(all(m for _, m in commands))  # durable marker precedes every stop
        self.d.stop(); self.prepare(); self.d.start()
        with self.assertRaises(ValueError):
            self.guard()  # reconcile refuses in maintenance; nothing reopens
        self.assertFalse(any(c[0] == 'start' for c in self.d.calls))
        commands.clear()
        control.maintenance(self.directory, 'salir', run)
        self.assertFalse(marker.exists())
        self.assertEqual([c for c, _ in commands], [
            ('systemctl', '--user', 'start', 'pueued.service'),
            ('systemctl', '--user', 'start', 'pueue-desk-mode.timer', 'pueue-desk-guard.timer')])
        with self.assertRaises(ValueError):
            control.maintenance(self.directory, 'salir', run)

    def test_units_order_and_restart_policy(self):
        unit = (BASE / 'units/pueued.service').read_text()
        lines = [l for l in unit.splitlines() if l and not l.startswith('#')]
        for expected in ('StartLimitIntervalSec=0', 'Restart=always', 'RestartSec=5', 'KillMode=mixed',
                         'WorkingDirectory=%h', 'ExecStart=@HOME@/.local/bin/pueued',
                         'ExecStartPre=/usr/bin/python3 -I @HOME@/.local/lib/pao-pueue-desk/control.py preparar --host desk',
                         'ExecStop=-/usr/bin/python3 -I @HOME@/.local/lib/pao-pueue-desk/control.py parada --host desk'):
            self.assertIn(expected, lines)
        self.assertNotIn('Requires=pueue-desk-mode.service', unit)  # a quota failure never restarts pueued
        for name in ('pueue-desk-guard.service', 'pueue-desk-mode.service'):
            text = (BASE / 'units' / name).read_text()
            self.assertIn('Requisite=pueued.service', text)  # a timer never starts pueued
            self.assertNotIn('Requires=', text)

    def test_legacy_desk_without_authority_validates_only(self):
        engine = control.MiniControl(self.d, self.root / 'legacy', host='desk')
        self.d.queue['tasks'] = {'0': task(0, 'jale', running())}
        self.d.stop()
        source = self.paths['state'].read_bytes()
        with control.locked(self.root / 'legacy'):
            self.assertEqual(engine.prepare(self.paths, control.PUEUED_VERSION, name_of=lambda p: None), dict(prepared=False))
        self.assertEqual(self.paths['state'].read_bytes(), source)
        self.assertFalse((self.root / 'legacy/control.sqlite3').exists())

    def test_main_preparar_rejects_on_mini(self):
        with patch.object(control.sys, 'argv', ['control.py', 'preparar', '--host', 'mini']), \
             redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            control.main()


class OW1Tests(StartupFixture):
    def legacy_guard(self, directory, pueue):
        with control.locked(directory):
            try:
                control.guard(pueue, Path('/nonexistent'), directory, host='desk')
            except ValueError as error:
                self.fail('El tick abortó: ' + str(error))  # pre-fix O-W1: every tick

    def tick(self, readings=None):
        try:
            self.guard(readings)
        except ValueError as error:
            self.fail('El tick abortó: ' + str(error))

    def test_legacy_suites_open_empty_three_ticks_attend_other_groups(self):
        directory = self.root / 'legacy'
        directory.mkdir()
        (directory / 'guardia-state.json').write_text(json.dumps(['suites', 'hasito']))
        self.d.queue['groups']['suites'] = dict(status='Running', parallel_tasks=2)
        self.d.queue['groups']['hasito']['status'] = 'Paused'
        for tick in range(3):
            self.legacy_guard(directory, self.d)  # pre-fix: ValueError every tick
        self.assertEqual(self.d.queue['groups']['hasito']['status'], 'Running')  # its own pause released
        self.assertEqual(json.loads((directory / 'guardia-state.json').read_text()), [])
        self.assertEqual(self.d.queue['groups']['suites']['status'], 'Running')  # never retired incidentally
        with patch.object(control, 'pressure', return_value=dict(self.low, io=60)):
            self.legacy_guard(directory, self.d)
        # rapidas only stops for memory (#118).
        self.assertEqual({self.d.queue['groups'][g]['status'] for g in control.GROUP_LIMITS if g != 'rapidas'}, {'Paused'})

    def test_authorized_retire_then_ticks_and_tasks_block_it(self):
        self.d.queue['groups']['suites'] = dict(status='Running', parallel_tasks=2)
        self.d.stop()
        self.boot()
        self.assertEqual(self.state()['startup']['held']['suites'], 'fuera_del_reparto')
        with control.locked(self.directory):
            self.engine.retire('suites')
        self.assertNotIn('suites', self.d.queue['groups'])
        s = self.state()
        self.assertNotIn('suites', s['reasons']); self.assertNotIn('suites', s['startup']['held'])
        for tick in range(3):
            self.tick()
        self.d.queue['groups']['pg5434'] = dict(status='Paused', parallel_tasks=1)
        self.d.queue['tasks']['9'] = task(9, 'pg5434', queued())
        with control.locked(self.directory), self.assertRaisesRegex(ValueError, 'tiene tareas'):
            self.engine.retire('pg5434')
        self.assertIn('pg5434', self.d.queue['groups'])
        with control.locked(self.directory), self.assertRaises(ValueError):
            self.engine.retire('jale')

    def test_crashed_retire_is_finished_by_the_guard(self):
        self.d.queue['groups']['suites'] = dict(status='Running', parallel_tasks=2)
        self.d.stop(); self.boot()
        with patch.object(self.engine, 'store', side_effect=OSError('caída')), \
             control.locked(self.directory), self.assertRaises(OSError):
            self.engine.retire('suites')
        self.assertNotIn('suites', self.d.queue['groups'])
        self.assertIn('suites', self.state()['reasons'])
        for tick in range(3):
            self.tick(dict(self.low, io=60) if tick == 1 else None)
        self.assertNotIn('suites', self.state()['reasons'])
        self.assertTrue(any(c[0] == 'pause' for c in self.d.calls))  # other groups attended


class OW2Tests(StartupFixture):
    COMPLEX = {
        'env-map': 'daemon:\n  env_vars:\n    TOKEN: x\n',
        'env-flow': 'daemon:\n  env_vars: {"TOKEN": "x"}\n',
        'shell-list': 'daemon:\n  shell_command:\n  - bash\n',
        'shell-flow': 'daemon:\n  shell_command: ["bash", "-c", "{{ pueue_command_string }}"]\n',
        'custom-dir': 'shared:\n  pueue_directory: /srv/pueue\n',
        'custom-socket': 'shared:\n  unix_socket_path: /tmp/otro.socket\n',
        'tcp': 'shared:\n  use_unix_socket: false\n',
        'compressed': 'daemon:\n  compress_state_file: true\n',
        'anchor': 'shared: &a\n  host: x\n',
    }

    def installer(self):
        spec = importlib.util.spec_from_file_location('install_desk_u34', BASE / 'install-desk.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        library = self.home / '.local/lib/pao-pueue-desk'
        library.mkdir(parents=True, exist_ok=True)
        (library / 'control.py').write_text('# U3.3 controller\n')
        units = self.home / '.config/systemd/user'
        units.mkdir(parents=True, exist_ok=True)
        for name in module.UNITS:
            (units / name).write_text('# old ' + name + '\n')
        return module

    def fake_systemd(self, env_text=None, main_pid='321'):
        env_text = 'XDG_RUNTIME_DIR=' + str(self.run) + '\n' if env_text is None else env_text
        self.commands, self.unit = [], dict(MainPID=main_pid, ActiveState='active', NeedDaemonReload='no',
                                            Restart='on-failure', KillMode='control-group', ExecStartPre='')

        def run(*argv):
            self.commands.append(argv)
            out = ''
            if argv[2:3] == ('show-environment',):
                out = env_text
            elif argv[2:3] == ('show',):
                out = ''.join(k + '=' + v + '\n' for k, v in self.unit.items())
            elif argv[2:3] == ('daemon-reload',):
                text = (self.home / '.config/systemd/user/pueued.service').read_text()
                self.unit.update(Restart='always' if 'Restart=always' in text else 'on-failure',
                                 KillMode='mixed' if 'KillMode=mixed' in text else 'control-group',
                                 ExecStartPre='{ argv[]=... preparar }' if 'preparar' in text else '')
            return subprocess.CompletedProcess(argv, 0, out, '')
        return run

    def fingerprint(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def install(self, module, dry_run, env_text=None, pid=None):
        Original = control.MiniControl
        factory = lambda p, d, **kw: Original(self.d, d, clock=lambda: self.now, generation=lambda: 'gen-%d' % self.d.gen, **kw)
        with patch.object(module, 'run', side_effect=self.fake_systemd(env_text)), \
             patch.object(control, 'MiniControl', side_effect=factory), \
             patch.object(control, 'daemon_version', return_value=control.PUEUED_VERSION), \
             patch.object(control, 'mini_generation', return_value='gen-1'), redirect_stdout(io.StringIO()) as out:
            module.install(self.home, pid or self.paths['pid'], control, dry_run=dry_run)
        return out.getvalue()

    def test_complex_configuration_rejected_before_any_effect(self):
        module = self.installer()
        self.d.stop(); self.d.up = True  # live daemon with its queue on disk
        config = self.home / '.config/pueue/pueue.yml'
        config.parent.mkdir(parents=True, exist_ok=True)
        cases = dict(self.COMPLEX, **{'env-path': None})
        for name, text in cases.items():
            for dry in (True, False):
                with self.subTest(name=name, dry=dry):
                    if text is None:
                        config.unlink(missing_ok=True)
                        env = 'XDG_RUNTIME_DIR=' + str(self.run) + '\nPUEUE_CONFIG_PATH=/srv/pueue.yml\n'
                    else:
                        config.write_text(text); env = None
                    before = self.fingerprint()
                    with self.assertRaises(ValueError):
                        self.install(module, dry, env)
                    self.assertEqual(self.fingerprint(), before)
                    self.assertTrue(all(c[2] in ('is-active', 'show-environment') for c in self.commands), self.commands)
                    self.assertTrue(self.d.up)
                    self.assertEqual(self.d.calls, [])
                    # preparar applies the same function on the unit's next start.
                    with self.assertRaises(ValueError):
                        control.host_gate(self.home, control.manager_environment(env or ''), version=control.PUEUED_VERSION)
        config.write_text('---\nshared:\n  pueue_directory: null\n  use_unix_socket: true\ndaemon:\n  compress_state_file: false\n'
                          '  env_vars: {}\n  shell_command: null\n  callback: null\nclient: {}\nprofiles: {}\n')
        self.assertEqual(control.host_gate(self.home, self.env, version=control.PUEUED_VERSION)[0]['state'], self.paths['state'])

    def test_deploy_desk_preflight_rejects_before_linger_or_shutdown(self):
        config = self.home / '.config/pueue/pueue.yml'
        config.parent.mkdir(parents=True)
        config.write_text(self.COMPLEX['env-map'])
        for name in ('pueue', 'pueued'):  # never executed: subprocess.run is a fake below
            binary = self.home / '.local/bin' / name
            binary.parent.mkdir(parents=True, exist_ok=True); binary.write_text(''); binary.chmod(0o755)
        calls = []

        def fake(argv, **kw):
            calls.append(argv)
            out = 'pueued 4.0.4\n' if argv[-1] == '--version' else ''
            return subprocess.CompletedProcess(argv, 0, out, '')
        scope = dict(control.__dict__, STATE_DIR=self.root / 'legacy', FILES={})
        exec((BASE / 'remote.py').read_text(), scope)
        with patch.object(scope['subprocess'], 'run', side_effect=fake), patch.dict(scope, Pueue=lambda: self.d), \
             patch.object(scope['Path'], 'home', return_value=self.home):
            for dry in (True, False):
                calls.clear()
                with self.assertRaises(ValueError):
                    scope['remote']('install', dry)
                self.assertEqual(calls, [['systemctl', '--user', 'show-environment']])
        self.assertEqual(self.d.calls, [])

    def test_seco_and_install_migrate_startup_and_load_unit_without_restart(self):
        module = self.installer()
        s = json.loads(self.raw_policy()); del s['startup']
        with self.engine.connect(write=True) as db:
            db.execute('UPDATE policy SET body=? WHERE id=1', (control.canonical(s),))
        old_rev = s['revision']
        before = self.fingerprint()
        out = self.install(module, True)
        self.assertEqual(self.fingerprint(), before)
        self.assertIn('Comprobado (lectura)', out)
        self.assertEqual({c[2] for c in self.commands}, {'is-active', 'show-environment'})
        out = self.install(module, False)
        body = json.loads(self.raw_policy())
        self.assertEqual(body.get('startup'), control.startup_default())
        self.assertEqual(body['revision'], old_rev)
        units = self.home / '.config/systemd/user'
        self.assertIn('ExecStartPre=/usr/bin/python3 -I ' + str(self.home), (units / 'pueued.service').read_text())
        self.assertEqual((self.home / '.local/lib/pao-pueue-desk/units.pre-u34/pueued.service').read_text(),
                         '# old pueued.service\n')
        self.assertEqual((self.home / '.local/lib/pao-pueue-desk/control.pre-u34.py').read_text(), '# U3.3 controller\n')
        verbs = [c[2] for c in self.commands]
        self.assertNotIn('restart', verbs)
        self.assertFalse(any('pueued.service' in c and c[2] in ('stop', 'start', 'restart', 'kill') for c in self.commands))
        self.assertIn('daemon-reload', verbs)
        self.assertIn('MainPID 321', out)
        self.assertTrue(self.d.up); self.assertEqual(self.d.calls, [])

    def test_adopted_pid_path_must_match_preparar(self):
        module = self.installer()
        other = self.paths['data'] / 'pueue.pid'  # what Pueue uses without XDG_RUNTIME_DIR
        before = self.fingerprint()
        for dry in (True, False):
            with self.assertRaisesRegex(ValueError, 'otro pid_path'):
                self.install(module, dry, env_text='', pid=other)
        self.assertEqual(self.fingerprint(), before)

    def test_open_receipts_reject_without_effects_even_without_migration(self):
        module = self.installer()
        with self.engine.connect(write=True) as db:
            db.execute('INSERT INTO host_actions VALUES (?,?,?)', ('open-000001', 'x', json.dumps(
                dict(request=dict(action_id='open-000001'), response=dict(status='applying')))))
        for dry in (True, False):
            before = self.fingerprint()
            with self.assertRaisesRegex(ValueError, 'recibos abiertos'):
                self.install(module, dry)
            self.assertEqual(self.fingerprint(), before)
            self.assertFalse(any(c[2] in ('stop', 'start', 'daemon-reload') for c in self.commands))

    def test_maintenance_refuses_install_without_effects(self):
        module = self.installer()
        (self.directory / 'maintenance').write_text('mantenimiento\n')
        for dry in (True, False):
            before = self.fingerprint()
            with self.assertRaisesRegex(ValueError, 'Mantenimiento'):
                self.install(module, dry)
            self.assertEqual(self.fingerprint(), before)
            self.assertEqual(self.commands, [])  # timers are never restarted

    def test_unit_mismatch_after_reload_fails_loudly(self):
        module = self.installer()
        run = self.fake_systemd()

        def changed(*argv):
            result = run(*argv)
            if argv[2:3] == ('daemon-reload',):
                self.unit['MainPID'] = '999'  # pueued was restarted behind our back
            return result
        Original = control.MiniControl
        factory = lambda p, d, **kw: Original(self.d, d, clock=lambda: self.now, generation=lambda: 'gen-%d' % self.d.gen, **kw)
        with patch.object(module, 'run', side_effect=changed), patch.object(control, 'MiniControl', side_effect=factory), \
             patch.object(control, 'daemon_version', return_value=control.PUEUED_VERSION), \
             patch.object(control, 'mini_generation', return_value='gen-1'), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'no coincide'):
                module.install(self.home, self.paths['pid'], control)
        self.assertTrue((self.directory / 'slots.disabled').exists())  # switch kept for a retry


if __name__ == '__main__':
    unittest.main()
