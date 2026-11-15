"""SEN-6417: admisión de a una bajo presión media. Dobles privados en /tmp.

Pueue, sensores, Docker y el reloj de arranque son falsos. `start <id>` del doble
arranca la tarea sin tocar el estado del grupo, como Pueue 4.0.4.
"""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from test_slots import control, FakePueue
from test_desk_slots import utc

GIB = 1024**3
LOW = dict(io=0, cpu=5, memory=8 * GIB)
MID = dict(io=0, cpu=22, memory=8 * GIB)
HIGH = dict(io=0, cpu=40, memory=8 * GIB)
SECRET = 'CENTINELA-SECRETO-6417'
SUMMARIES = ('admision_una', 'tope_alcanzado', 'retenido', 'guard_fin')
BOOT = '0f0e0d0c-0b0a-4908-8706-050403020100'
SECOND = 10**9


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def stamp(minute, second=0):
    return '2026-11-14T09:%02d:%02d.123456789+00:00' % (minute, second)


def task(ident, status, group='jale', created=None, deps=()):
    return dict(id=ident, created_at=created or stamp(0), command='run ' + SECRET, original_command='run ' + SECRET,
                path='/synthetic/' + SECRET, envs={'TOKEN': SECRET}, group=group, dependencies=list(deps),
                priority=0, label=None, status=status)


def queued(minute):
    return {'Queued': dict(enqueued_at=stamp(minute))}


def running(minute=1):
    return {'Running': dict(enqueued_at=stamp(0), start=stamp(minute))}


def done(result='Success'):
    return {'Done': dict(enqueued_at=stamp(0), start=stamp(1), end=stamp(2), result=result)}


class TaskPueue(FakePueue):
    """`start <id>` arranca sólo esa tarea; el grupo conserva su estado."""
    def __init__(self):
        super().__init__()
        self.tasks = {}
        self.start_fault = None  # None, 'before', 'after', 'crash', 'noop'
        self.on_start = None
        self.clock = None  # inicio civil que Pueue escribe al arrancar

    def status(self):
        self.boundary()
        if self.unreadable:
            raise ValueError('Estado ilegible')
        return dict(groups=copy.deepcopy(self.groups), tasks=copy.deepcopy(self.tasks))

    def call(self, *args):
        if args[0] == 'start' and len(args) == 2:
            self.calls.append(args)
            if self.on_start:
                self.on_start()
            if self.start_fault == 'before':
                raise subprocess.CalledProcessError(1, ['fake-pueue'])
            if self.start_fault == 'crash':
                raise KeyboardInterrupt('caída simulada entre reserva y confirmación')
            row = self.tasks[str(args[1])]
            if self.start_fault != 'noop' and 'Queued' in row['status']:
                row['status'] = {'Running': dict(enqueued_at=row['status']['Queued']['enqueued_at'],
                                                 start=iso(self.clock()))}
            if self.start_fault == 'after':
                raise subprocess.CalledProcessError(1, ['fake-pueue'])
            return ''
        return super().call(*args)


class TrickleFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='sen6417-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'pid').write_text('321\n')
        self.now = utc('2026-11-14T10:00:00')  # noche: jale 7, pesadas 3
        self.gen = 'linux-proc-v1:' + '1' * 64
        self.boot, self.ns = BOOT, 3600 * SECOND
        self.docker_ok = True
        self.p = TaskPueue()
        self.p.clock = lambda: self.now
        self.p.groups = {g: dict(parallel_tasks=n, status='Running') for g, n in control.desk_profile(self.now).items()}
        self.p.groups.update(default=dict(parallel_tasks=1, status='Paused'))
        self.engine = self.new_engine()
        with control.locked(self.engine.directory):
            self.engine.adopt(self.root / 'pid')
        state = self.state()
        state['startup_version'] = 1  # como deja install-desk.py
        self.store(state)

        def fake_run(argv, **kwargs):
            if argv == ['/usr/bin/docker', 'info']:
                if not self.docker_ok:
                    raise subprocess.CalledProcessError(1, argv)
                return subprocess.CompletedProcess(argv, 0, '', '')
            raise AssertionError('Ningún binario real: ' + str(argv))
        for manager in (patch.object(control.subprocess, 'run', side_effect=fake_run),
                        patch.object(control, 'admission_clock', lambda: (self.boot, self.ns), create=True)):
            manager.start()
            self.addCleanup(manager.stop)
        self.tick(HIGH)  # la guardia pausa los grupos normales por presión
        self.p.calls.clear()

    def new_engine(self):
        return control.MiniControl(self.p, self.root / 'host', clock=lambda: self.now,
                                   generation=lambda: self.gen, host='desk')

    def state(self):
        with self.engine.connect() as db:
            return self.engine.state(db)

    def store(self, state):
        with self.engine.connect(write=True) as db:
            self.engine.store(db, state)

    def tick(self, readings, fresh=None, engine=None, swallow=()):
        with patch.object(control, 'pressure', return_value=fresh or readings):
            try:
                (engine or self.engine).guard(readings)
            except swallow:
                pass

    def advance(self, seconds):
        self.now += seconds
        self.ns += int(seconds * SECOND)

    def records(self):
        path = self.engine.directory / 'guardia.log'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def summary(self):
        rows = [r for r in self.records() if r['evento'] in SUMMARIES]
        self.assertTrue(rows, 'La pasada debe dejar un resumen con su causa en guardia.log')
        return rows[-1]

    def causes(self, row):
        return {(b['grupo'], b['operacion'], b['causa']) for b in row.get('bloqueos', [])}

    def starts(self):
        return [c for c in self.p.calls if c[0] == 'start' and len(c) == 2]

    def group_starts(self):
        return [c for c in self.p.calls if c[:2] == ('start', '-g')]


class TrickleRedTests(TrickleFixture):
    """Contratos que fallan por aserción sobre origin/main (punto 15)."""

    def test_mid_band_admits_one_fifo_task_and_keeps_pause_and_pressure(self):
        self.p.tasks = {'10': task(10, queued(5), 'jale'), '11': task(11, queued(1), 'pesadas'),
                        '12': task(12, queued(3), 'otros')}
        self.tick(MID)
        self.assertEqual(self.starts(), [('start', 11)], 'Banda media: exactamente la tarea más antigua del desk')
        self.assertEqual(self.group_starts(), [])
        for g in ('jale', 'pesadas', 'otros'):
            self.assertEqual(self.p.groups[g]['status'], 'Paused')
            self.assertEqual(self.state()['reasons'][g], ['pressure'])
        row = self.summary()
        self.assertEqual((row['evento'], row['grupo'], row['id']), ('admision_una', 'pesadas', 11))
        self.assertIn((None, 'one', 'global_one'), {(None, b['operacion'], b['causa']) for b in row['bloqueos']})
        self.p.calls.clear()
        self.advance(10)
        self.tick(MID)
        self.assertEqual(self.starts(), [], 'La pasada siguiente del mismo minuto no admite otra')
        self.assertEqual(self.summary()['evento'], 'tope_alcanzado')
        self.assertEqual(self.p.groups['pesadas']['status'], 'Paused')
        self.assertEqual(self.state()['reasons']['pesadas'], ['pressure'])

    def test_global_cap_survives_new_instance_and_counts_from_completion(self):
        self.p.tasks = {'10': task(10, queued(1)), '11': task(11, queued(2)), '12': task(12, queued(3))}
        def slow():  # el RPC tarda 5 s: la ventana se cuenta desde la confirmación
            self.advance(5)
        self.p.on_start = slow
        self.tick(MID)
        self.assertEqual(self.starts(), [('start', 10)])
        self.p.on_start = None
        completed = self.ns
        self.p.calls.clear()
        for elapsed in (10, 55, 59.999):
            self.ns = completed + int(elapsed * SECOND)
            self.tick(MID, engine=self.new_engine())  # otra instancia del controlador
            self.assertEqual(self.starts(), [], 'A %s s de confirmar no hay otra admisión' % elapsed)
            row = self.summary()
            self.assertEqual(row['evento'], 'tope_alcanzado')
            self.assertGreater(row['restante_ms'], 0)
        self.ns = completed + 60 * SECOND
        self.tick(MID, engine=self.new_engine())
        self.assertEqual(self.starts(), [('start', 11)])

    def test_quota_full_group_is_skipped_and_free_group_admits_once(self):
        self.p.tasks = {str(i): task(i, running(), 'jale') for i in range(1, 8)}  # jale 7/7
        self.p.tasks.update({'20': task(20, queued(1), 'jale'), '21': task(21, queued(4), 'pesadas'),
                             '22': task(22, running(), 'pesadas'), '23': task(23, queued(6), 'pesadas')})
        self.tick(MID)
        self.assertEqual(self.starts(), [('start', 21)], 'jale lleno: sólo entra pesadas')
        self.assertIn(('jale', 'one', 'quota'), self.causes(self.summary()))
        self.p.calls.clear()
        self.p.tasks['24'] = task(24, {'Paused': running()['Running']}, 'pesadas')  # pesadas 3/3 con una pausada
        self.advance(61)
        self.tick(MID)
        self.assertEqual(self.starts(), [], 'Una tarea Paused ocupa lugar: nunca se supera la cuota')
        self.assertIn(('pesadas', 'one', 'quota'), self.causes(self.summary()))

    def test_every_hold_cause_is_logged_for_resume_and_for_one(self):
        cases = ('fresh_sample', 'docker', 'generation', 'desired', 'event', 'admission')
        for operation, readings, other in (('resume', LOW, MID), ('one', MID, HIGH)):
            for cause in cases:
                with self.subTest(operation=operation, cause=cause):
                    self.setUp()
                    self.p.tasks = {'10': task(10, queued(1))}
                    fresh = other if cause == 'fresh_sample' else None
                    reconcile = patch.object(self.engine, 'reconcile', return_value=True)
                    if cause == 'docker':
                        self.docker_ok = False
                    elif cause == 'generation':
                        reconcile.start(); self.gen = 'linux-proc-v1:' + '2' * 64
                    elif cause == 'desired':
                        reconcile.start(); self.p.groups['otros']['parallel_tasks'] = 1
                    elif cause == 'event':
                        reconcile.start(); self.now = utc('2026-11-14T14:00:01')
                    elif cause == 'admission':
                        state = self.state()
                        state['startup'] = dict(phase='prepared', statuses={}, groups={}, holds={}, interruptions={},
                                                source_hash='0' * 64, target_hash='0' * 64)
                        self.store(state)
                    try:
                        self.tick(readings, fresh=fresh, swallow=(ValueError,))
                    finally:
                        reconcile.stop() if cause in ('generation', 'desired', 'event') else None
                    self.assertEqual(self.starts() + self.group_starts(), [])
                    row = self.summary()
                    self.assertEqual(row['evento'], 'retenido')
                    found = {(b['operacion'], b['causa']) for b in row['bloqueos']}
                    self.assertIn((operation, cause), found)
                    self.doCleanups()

    def test_high_pressure_admits_nothing_and_says_so_when_queue_and_slot_wait(self):
        self.p.tasks = {'10': task(10, queued(1))}
        self.tick(HIGH)
        self.assertEqual(self.starts() + self.group_starts(), [])
        self.assertIn((None, 'one', 'pressure_high'), self.causes(self.summary()))

    def test_failed_start_stays_unknown_and_blocks_until_evidence(self):
        self.p.tasks = {'10': task(10, queued(1)), '11': task(11, queued(2))}
        self.p.start_fault = 'before'
        self.tick(MID, swallow=(subprocess.CalledProcessError,))
        self.assertEqual(self.starts(), [('start', 10)])
        self.assertIn((None, 'one', 'start_failed'), {(None, b['operacion'], b['causa']) for b in self.summary()['bloqueos']})
        self.p.start_fault = None
        self.p.calls.clear()
        for _ in range(3):
            self.advance(300)
            self.tick(MID)
            self.assertEqual(self.starts(), [], 'Un resultado incierto no se repite ni libera otro permiso')
            row = self.summary()
            self.assertEqual((row['evento'], row['causa']), ('tope_alcanzado', 'admission_unknown'))
        # Evidencia: la misma identidad aparece Running. Se confirma y se espera un minuto desde ahí.
        self.p.tasks['10']['status'] = {'Running': dict(enqueued_at=stamp(1), start=iso(self.now - 600))}
        self.advance(1)
        self.tick(MID)
        self.assertEqual(self.starts(), [])
        self.advance(60)
        self.tick(MID)
        self.assertEqual(self.starts(), [('start', 11)])

    def test_crash_after_reservation_is_never_retried_without_evidence(self):
        self.p.tasks = {'10': task(10, queued(1))}
        self.p.start_fault = 'crash'
        self.tick(MID, swallow=(KeyboardInterrupt,))
        self.assertEqual(self.starts(), [('start', 10)])
        self.p.start_fault = 'noop'
        self.p.tasks['10']['status'] = queued(1)
        self.p.calls.clear()
        self.advance(600)
        self.tick(MID, engine=self.new_engine())
        self.assertEqual(self.starts(), [])
        self.assertEqual(self.summary()['causa'], 'admission_unknown')

    def test_reused_id_with_other_creation_is_not_evidence(self):
        self.p.tasks = {'10': task(10, queued(1))}
        self.p.start_fault = 'before'
        self.tick(MID, swallow=(subprocess.CalledProcessError,))
        self.p.start_fault = None
        self.p.tasks = {'10': task(10, running(9), created=stamp(8)), '11': task(11, queued(2))}
        self.p.calls.clear()
        self.advance(120)
        self.tick(MID)
        self.assertEqual(self.starts(), [])
        self.assertEqual(self.summary()['causa'], 'admission_unknown')

    def test_clock_rules_backwards_reboot_and_civil_time(self):
        self.p.tasks = {'10': task(10, queued(1)), '11': task(11, queued(2)), '12': task(12, queued(3))}
        self.tick(MID)
        self.assertEqual(self.starts(), [('start', 10)])
        self.p.calls.clear()
        self.now += 3600  # el reloj civil salta una hora: no habilita nada
        self.ns += 5 * SECOND
        self.tick(MID)
        self.assertEqual(self.starts(), [])
        self.ns -= 30 * SECOND  # retroceso del reloj de arranque: retiene
        self.tick(MID)
        self.assertEqual(self.starts(), [])
        self.assertEqual(self.summary()['causa'], 'clock')
        self.boot, self.ns = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee', 30 * SECOND  # reinicio de WSL
        self.tick(MID)
        self.assertEqual(self.starts(), [], 'Tras otro arranque se esperan 60 s de uptime')
        self.ns = 60 * SECOND
        self.tick(MID)
        self.assertEqual(self.starts(), [('start', 11)])

    def test_pending_dependency_is_skipped_and_satisfied_dependency_admits(self):
        self.p.tasks = {'5': task(5, running(), 'otros'), '10': task(10, queued(1), deps=[5]),
                        '11': task(11, queued(2), 'pesadas', deps=[99])}
        self.tick(MID)
        self.assertEqual(self.starts(), [])
        self.assertIn(('jale', 'one', 'dependencies'), self.causes(self.summary()))
        self.p.tasks['5']['status'] = done()
        self.tick(MID)
        self.assertEqual(self.starts(), [('start', 10)])

    def test_low_pressure_reopens_as_today_and_ignores_the_cap(self):
        self.p.tasks = {'10': task(10, queued(1)), '11': task(11, queued(2))}
        self.tick(MID)
        self.assertEqual(self.starts(), [('start', 10)])
        self.p.calls.clear()
        self.advance(5)
        self.tick(LOW)
        normal = [g for g in control.GROUP_LIMITS if g != 'rapidas']
        self.assertEqual(sorted(c[2] for c in self.group_starts()), sorted(normal))
        self.assertEqual(self.starts(), [])
        self.assertTrue(all(self.p.groups[g]['status'] == 'Running' for g in normal))

    def test_nothing_secret_reaches_the_log(self):
        self.p.tasks = {'10': task(10, queued(1)), '11': task(11, queued(2))}
        self.tick(MID)
        self.p.start_fault = 'after'
        self.advance(61)
        self.tick(MID, swallow=(subprocess.CalledProcessError,))
        self.tick(HIGH)
        text = (self.engine.directory / 'guardia.log').read_text()
        self.assertEqual(len(self.starts()), 2)
        self.assertNotIn(SECRET, text)
        for row in self.records():
            self.assertLessEqual(len(json.dumps(row)), 4096)


class TrickleRegressionTests(TrickleFixture):
    """Conducta que ya existe y debe conservarse."""

    def test_paused_group_with_running_task_keeps_pressure_then_low_reopens(self):
        self.p.tasks = {'10': task(10, running())}
        for _ in range(2):
            self.tick(MID)
            self.assertEqual(self.p.groups['jale']['status'], 'Paused')
            self.assertEqual(self.state()['reasons']['jale'], ['pressure'])
        self.tick(LOW)
        self.assertEqual(self.p.groups['jale']['status'], 'Running')
        self.assertEqual(self.state()['reasons']['jale'], [])

    def test_rapidas_manual_and_ambiguous_never_trickle(self):
        self.p.groups['rapidas']['status'] = 'Paused'
        state = self.state()
        state['reasons'].update(rapidas=['pressure'], jale=['manual'], pesadas=['ambiguous', 'pressure'])
        self.store(state)
        self.p.tasks = {'10': task(10, queued(1), 'rapidas'), '11': task(11, queued(2), 'jale'),
                        '12': task(12, queued(3), 'pesadas')}
        self.tick(MID)
        self.assertEqual(self.starts(), [])

    def test_mini_mid_band_is_unchanged(self):
        p = FakePueue()
        engine = control.MiniControl(p, self.root / 'mini', clock=lambda: self.now, generation=lambda: 'daemon-1')
        with control.locked(self.root / 'mini'):
            engine.adopt()
        engine.guard(dict(load=3.0, memory=4))
        p.calls.clear()
        for _ in range(2):
            engine.guard(dict(load=1.5, memory=1))
        self.assertEqual(p.calls, [])
        events = [json.loads(l)['evento'] for l in (self.root / 'mini/guardia.log').read_text().splitlines()]
        self.assertEqual(set(events), {'lectura', 'decision'})


class TrickleRecoveryTests(TrickleFixture):
    """`control.py resolver-admision`: sólo bajo mantenimiento; sin evidencia sigue retenida."""

    def unknown(self):
        self.p.tasks = {'10': task(10, queued(1)), '11': task(11, queued(2))}
        self.p.start_fault = 'before'
        self.tick(MID)
        self.p.start_fault = None
        self.p.calls.clear()
        return self.engine.admission()

    def test_needs_maintenance_and_evidence_or_explicit_no_effect(self):
        before = self.unknown()
        self.assertEqual(before['last_attempt']['outcome'], 'unknown')
        with self.assertRaises(ValueError):
            self.engine.resolve_admission()
        (self.engine.directory / 'maintenance').write_text('')
        with self.assertRaises(ValueError):
            self.engine.resolve_admission()
        self.assertEqual(self.engine.admission(), before, 'Sin evidencia la fila no cambia')
        self.assertEqual(self.engine.resolve_admission(no_effect=True), 'not_sent')
        (self.engine.directory / 'maintenance').unlink()
        self.tick(MID)
        self.assertEqual(self.starts(), [], 'La resolución cuenta como intento: un minuto desde ahí')
        self.advance(60)
        self.tick(MID)
        self.assertEqual(self.starts(), [('start', 10)])

    def test_evidence_confirms_and_reboot_closes_as_not_sent(self):
        self.unknown()
        (self.engine.directory / 'maintenance').write_text('')
        self.p.tasks['10']['status'] = {'Running': dict(enqueued_at=stamp(1), start=iso(self.now))}
        self.assertEqual(self.engine.resolve_admission(), 'confirmed')
        self.assertEqual(self.engine.admission()['last_confirmed']['id'], 10)
        self.setUp()
        self.unknown()
        (self.engine.directory / 'maintenance').write_text('')
        self.boot, self.ns = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee', 90 * SECOND
        self.assertEqual(self.engine.resolve_admission(), 'not_sent')
        self.assertEqual(self.engine.admission()['last_attempt']['boot_id'], self.boot)
        self.doCleanups()


if __name__ == '__main__':
    unittest.main()
