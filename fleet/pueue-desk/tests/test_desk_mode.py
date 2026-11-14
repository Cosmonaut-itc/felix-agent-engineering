"""U3.3 Día/Noche on the desk: fake clock, Pueue, daemon identity and /tmp authority."""
import copy
import io
import json
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from test_slots import control, FakePueue
from test_desk_slots import utc

DIA = {'hasito': 1, 'jale': 5, 'jale-verify': 1, 'otros': 2, 'pesadas': 2, 'rapidas': 3}
NOCHE = {'hasito': 2, 'jale': 7, 'jale-verify': 1, 'otros': 2, 'pesadas': 3, 'rapidas': 3}


class LivePueue(FakePueue):
    """Running tasks are visible; only parallel/pause/start are fake RPCs."""
    def __init__(self):
        super().__init__()
        self.tasks = {}

    def status(self):
        value = super().status()
        value['tasks'] = copy.deepcopy(self.tasks)
        return value


class DeskModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='u33-opus-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.build('host', utc('2026-11-14T22:00:00'))  # 16:00 en México, horario de día
        self.low = dict(io=0, cpu=0, memory=8 * 1024**3)

    def build(self, name, now):
        self.now, self.gen = now, 'desk-daemon-1'
        self.p = LivePueue()
        self.p.groups = {g: dict(parallel_tasks=n, status='Running') for g, n in control.desk_profile(now).items()}
        self.p.groups.update(default=dict(parallel_tasks=1, status='Paused'), ajeno=dict(parallel_tasks=6, status='Paused'))
        self.engine = control.MiniControl(self.p, self.root / name, clock=lambda: self.now,
                                          generation=lambda: self.gen, host='desk')
        with control.locked(self.engine.directory):
            self.engine.adopt(self.root / 'pid')
        self.p.calls.clear()

    def expected(self):
        v = self.engine.view()
        return {k: v[k] for k in ('revision', 'policy_hash', 'generation', 'mode', 'slots')}

    def mode(self, mode, ident='modo-00000001', dry_run=False):
        return dict(schema=control.HOST_SCHEMA, op='mode', action_id=ident, dry_run=dry_run,
                    expected=self.expected(), change=dict(mode=mode))

    def slots(self, group, delta, ident):
        return dict(schema=control.HOST_SCHEMA, op='slots', action_id=ident, dry_run=False,
                    expected=self.expected(), change=dict(group=group, delta=delta))

    def undo(self, of, ident='deshacer-0001'):
        return dict(schema=control.HOST_SCHEMA, op='undo', action_id=ident, dry_run=False,
                    expected=self.expected(), undo_of=of)

    def status(self, ident):
        return self.engine.handle(dict(schema=control.HOST_SCHEMA, op='status', action_id=ident))

    def state(self):
        with self.engine.connect() as db:
            return self.engine.state(db)

    def fingerprint(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def observed(self):
        return {g: self.p.groups[g]['parallel_tasks'] for g in control.GROUP_LIMITS}

    def rpcs(self):
        return [c for c in self.p.calls if c[0] == 'parallel']

    def test_day_to_night_full_destination_table_retires_adjustments_reductions_first(self):
        # A legitimate day adjustment above the night value: otros 3 needs jale 4.
        self.assertEqual(self.engine.handle(self.slots('jale', -1, 'lugar-jale-01'))['status'], 'applied')
        self.assertEqual(self.engine.handle(self.slots('otros', 1, 'lugar-otros-01'))['status'], 'applied')
        self.p.calls.clear()
        answer = self.engine.handle(self.mode('noche'))
        self.assertEqual(answer['status'], 'applied')
        self.assertEqual(self.observed(), NOCHE)
        self.assertEqual(answer['result']['desired'], NOCHE)
        self.assertEqual((answer['result']['mode'], answer['result']['cap']), ('noche', 18))
        self.assertEqual(self.rpcs()[0], ('parallel', 'otros', 2))  # the only reduction goes first
        self.assertEqual(sorted(c[1] for c in self.rpcs()[1:]), ['hasito', 'jale', 'pesadas'])
        state = self.state()
        self.assertEqual(state['overrides'], {})
        self.assertEqual(state['mode_override'], dict(mode='noche', actor='Felix', action_id='modo-00000001',
                         requested_at=self.now, expires_at=utc('2026-11-15T05:00:00')))
        view = self.engine.view()
        self.assertEqual((view['mode'], view['cap'], view['expires_at']), ('noche', 18, utc('2026-11-15T05:00:00')))
        self.assertEqual(view['notes'], dict(mode='Adelantado · vuelve a día a las 08:00',
                                             detail='Ajustes manuales hasta las 23:00'))
        # The destination cap holds afterwards: + is full at 18, − still works.
        self.assertEqual(self.engine.handle(self.slots('jale', 1, 'lleno-noche-01'))['error']['code'], 'invalid_request')
        self.assertEqual(self.engine.handle(self.slots('jale', -1, 'baja-noche-001'))['status'], 'applied')
        self.assertEqual(self.state()['mode_override']['action_id'], 'modo-00000001')

    def test_night_to_day_lowers_parallel_with_live_tasks_without_stopping_them(self):
        self.build('night', utc('2026-11-15T06:00:00'))  # 00:00 en México, horario de noche
        self.p.tasks = {str(i): dict(group='jale', status={'Running': {}}) for i in range(7)}
        answer = self.engine.handle(self.mode('dia'))
        self.assertEqual(answer['status'], 'applied')
        self.assertEqual(self.observed(), DIA)
        self.assertTrue(all(c[0] == 'parallel' for c in self.p.calls))  # no pause, kill or start
        self.assertEqual(len(self.p.tasks), 7)
        self.assertEqual(answer['result']['increases'] if 'increases' in answer['result'] else [], [])
        view = self.engine.view()
        self.assertEqual((view['mode'], view['cap']), ('dia', 14))
        self.assertEqual(view['notes'], dict(mode='Adelantado · vuelve a noche a las 23:00',
                                             detail='Ajustes manuales hasta las 08:00'))

    def test_current_mode_is_rejected_without_effects_even_with_adjustments(self):
        self.engine.handle(self.slots('jale', -1, 'lugar-jale-01'))
        self.p.calls.clear()
        before = self.state()
        answer = self.engine.handle(self.mode('dia'))
        self.assertEqual((answer['status'], (answer.get('error') or {}).get('code'), answer.get('effects')), ('rejected', 'same_mode', False))
        self.assertEqual(self.p.calls, [])
        self.assertEqual(self.state(), before)  # jale 4 is not silently reset to the table
        self.engine.handle(self.mode('noche', 'modo-noche-01'))
        self.p.calls.clear()
        again = self.engine.handle(self.mode('noche', 'modo-noche-02'))
        self.assertEqual((again.get('error') or {}).get('code'), 'same_mode')
        self.assertEqual(self.p.calls, [])

    def test_back_to_scheduled_mode_retires_adelanto(self):
        self.engine.handle(self.mode('noche'))
        answer = self.engine.handle(self.mode('dia', 'modo-dia-0001'))
        self.assertEqual(answer['status'], 'applied')
        self.assertEqual(self.observed(), DIA)
        state = self.state()
        self.assertIsNone(state['mode_override'])
        self.assertEqual(state['overrides'], {})
        view = self.engine.view()
        self.assertEqual((view['mode'], view['expires_at']), ('dia', None))
        self.assertEqual(view['notes'], dict(mode='Automático · cambia a noche a las 23:00', detail=None))

    def test_critical_example_expires_at_next_event_although_night_continues(self):
        self.engine.handle(self.mode('noche'))
        self.engine.handle(self.slots('jale', -1, 'lugar-jale-01'))
        self.assertEqual(self.engine.view()['notes']['detail'], 'Ajustes manuales hasta las 23:00')
        self.now = utc('2026-11-15T04:59:59')
        self.assertEqual(self.engine.view()['mode'], 'noche')
        self.assertIsNotNone(self.state()['mode_override'])
        # Exact boundary, timer not yet run: the pure view already expires both.
        self.now = utc('2026-11-15T05:00:00')
        self.p.calls.clear()
        view = self.engine.view()
        self.assertEqual(view['notes'], dict(mode='Automático · cambia a día a las 08:00', detail=None))
        self.assertEqual((view['mode'], view['desired'], view['expires_at']), ('noche', NOCHE, None))
        self.assertEqual(self.p.calls, [])
        self.assertTrue(self.engine.reconcile())
        self.assertEqual(self.observed(), NOCHE)
        state = self.state()
        self.assertEqual((state['mode_override'], state['overrides']), (None, {}))
        self.assertEqual(state['schedule_event'], utc('2026-11-15T05:00:00'))

    def test_missed_timers_converge_to_now_without_replaying_adelanto(self):
        self.engine.handle(self.mode('noche'))
        self.now = utc('2026-11-16T16:00:00')  # two days of events missed, day now
        self.assertTrue(self.engine.reconcile())
        self.assertEqual(self.observed(), DIA)
        self.assertIsNone(self.state()['mode_override'])
        self.assertEqual(self.engine.view()['notes']['mode'], 'Automático · cambia a noche a las 23:00')

    def test_event_crossing_last_blocking_read_rejects_mode_and_undo_without_effects(self):
        for op in ('mode', 'undo'):
            with self.subTest(op=op):
                self.build('cross-' + op, utc('2026-11-15T04:59:59'))
                prior = None
                if op == 'undo':
                    prior = self.mode('noche', 'modo-previo-01')
                    self.assertEqual(self.engine.handle(prior)['status'], 'applied')
                request = self.undo(prior['action_id']) if op == 'undo' else self.mode('noche')
                self.p.calls.clear()
                before = self.state()
                original = self.engine.generation
                def crossing(state):
                    value = original(state)
                    self.now = utc('2026-11-15T05:00:00')  # after status and identity reads
                    return value
                with patch.object(self.engine, 'generation', side_effect=crossing):
                    answer = self.engine.handle(request)
                self.assertEqual(answer['status'], 'conflict')
                self.assertIs(answer.get('effects'), False)
                self.assertEqual(self.p.calls, [])
                self.assertEqual(self.state(), before)

    def test_event_crossing_before_receipt_confirmation_aborts_adelanto(self):
        self.build('confirm', utc('2026-11-15T04:59:59'))
        original = self.engine.generation
        reads = [0]
        def crossing(state):
            value = original(state)
            reads[0] += 1
            if reads[0] == 4:  # final identity read inside reconcile
                self.now = utc('2026-11-15T05:00:00')
            return value
        with patch.object(self.engine, 'generation', side_effect=crossing):
            answer = self.engine.handle(self.mode('noche'))
        self.assertEqual((answer['status'], (answer.get('error') or {}).get('code')), ('aborted', 'expired'))
        self.assertIsNone(answer['result']['undo_of'])
        self.assertEqual(answer['result']['observed'], NOCHE)
        self.assertIsNone(self.state()['mode_override'])
        self.assertEqual(self.engine.view()['notes']['mode'], 'Automático · cambia a día a las 08:00')

    def test_undo_before_event_restores_mode_adelanto_adjustments_and_original_expirations(self):
        self.engine.handle(self.slots('jale', -1, 'lugar-jale-01'))
        original = self.state()
        self.now += 60
        first = self.mode('noche')
        self.assertEqual(self.engine.handle(first)['status'], 'applied')
        self.now += 60
        answer = self.engine.handle(self.undo(first['action_id']))
        self.assertEqual(answer['status'], 'applied')
        restored = self.state()
        for key in ('desired', 'overrides', 'mode_override', 'schedule_event'):
            self.assertEqual(restored[key], original[key], key)
        self.assertEqual(restored['overrides']['jale']['requested_at'], original['overrides']['jale']['requested_at'])
        self.assertEqual(self.observed(), dict(DIA, jale=4))
        # Undo of «back to the schedule» returns the adelanto with its original instant.
        forced = self.mode('noche', 'modo-noche-02')
        self.engine.handle(forced)
        adelanto = self.state()['mode_override']
        self.now += 60
        back = self.mode('dia', 'modo-dia-0002')
        self.engine.handle(back)
        self.now += 60
        self.assertEqual(self.engine.handle(self.undo(back['action_id'], 'deshacer-0002'))['status'], 'applied')
        self.assertEqual(self.state()['mode_override'], adelanto)
        self.assertEqual(self.observed(), NOCHE)

    def test_undo_after_event_or_other_adjustment_is_undo_conflict_without_rpc(self):
        first = self.mode('noche')
        self.engine.handle(first)
        self.engine.handle(self.slots('jale', -1, 'lugar-otro-01'))
        self.p.calls.clear()
        answer = self.engine.handle(self.undo(first['action_id']))
        self.assertEqual((answer['status'], (answer.get('error') or {}).get('code'), answer.get('effects')), ('conflict', 'undo_conflict', False))
        self.assertEqual(self.p.calls, [])
        self.build('after-event', utc('2026-11-15T04:00:00'))
        first = self.mode('noche')
        self.engine.handle(first)
        undo = self.undo(first['action_id'])
        self.now = utc('2026-11-15T05:00:00')  # timer did not run
        self.p.calls.clear()
        answer = self.engine.handle(undo)
        self.assertEqual((answer['status'], (answer.get('error') or {}).get('code'), answer.get('effects')), ('conflict', 'undo_conflict', False))
        self.assertEqual(self.p.calls, [])
        self.assertEqual((answer.get('error') or {}).get('message'), 'El horario o los lugares cambiaron; no se puede deshacer este ajuste')

    def test_foreign_work_vetoes_raising_adelanto_and_undo_but_lowering_passes(self):
        self.p.tasks = {'1': dict(group='ajeno', status={'Running': {}})}
        answer = self.engine.handle(self.mode('noche'))
        self.assertEqual((answer['status'], (answer.get('error') or {}).get('code'), answer.get('effects')), ('rejected', 'foreign_work', False))
        self.assertEqual(self.p.calls, [])
        self.assertEqual((answer.get('error') or {}).get('message'), 'Hay trabajo fuera del reparto. Resuélvelo antes de añadir lugares.')
        self.build('night', utc('2026-11-15T06:00:00'))
        self.p.tasks = {'1': dict(group='default', status={'Running': {}})}
        lower = self.mode('dia')
        self.assertEqual(self.engine.handle(lower)['status'], 'applied')
        self.p.calls.clear()
        raise_back = self.engine.handle(self.undo(lower['action_id']))
        self.assertEqual((raise_back.get('error') or {}).get('code'), 'foreign_work')
        self.assertEqual(self.p.calls, [])
        # The schedule itself is not vetoed: the next night event raises again.
        self.now = utc('2026-11-16T05:00:00')
        self.assertTrue(self.engine.reconcile())
        self.assertEqual(self.observed(), NOCHE)

    def test_failure_after_every_rpc_is_partial_or_unknown_then_recovers_without_replay(self):
        # Día→noche: three increases, each followed by a verifying status read.
        for boundary in range(1, 9):
            with self.subTest(boundary=boundary):
                self.build('fail-%d' % boundary, utc('2026-11-14T22:00:00'))
                request = self.mode('noche')
                self.p.rpc, self.p.fail_at = 0, boundary
                first = self.engine.handle(request)
                self.p.fail_at = None
                if 'receipt_digest' not in first:  # the read before acceptance failed: nothing accepted or moved
                    self.assertEqual((first['status'], (first.get('error') or {}).get('code')), ('unknown', 'recovery_error'))
                    self.assertEqual((self.p.calls, self.state()['desired']), ([], DIA))
                    continue
                self.assertIn(first['status'], ('applied', 'partial', 'unknown'))
                if first['status'] == 'applied':
                    continue
                calls = copy.deepcopy(self.p.calls)
                self.assertEqual(self.engine.handle(request)['status'], self.status('modo-00000001')['status'])
                self.assertEqual(self.p.calls, calls)  # same ID is read, never re-executed
                self.assertEqual(self.state()['desired'], NOCHE)
                self.assertIn(self.engine.handle(self.slots('jale', -1, 'durante-01'))['error']['code'], ('busy', 'stale_state'))
                self.assertTrue(self.engine.reconcile())
                self.assertEqual(self.status('modo-00000001')['status'], 'applied')
                self.assertEqual(self.observed(), NOCHE)

    def test_ensayo_plans_destination_without_receipt_or_effect(self):
        before = self.fingerprint()
        answer = self.engine.handle(self.mode('noche', dry_run=True))
        self.assertEqual(answer['status'], 'dry_run')
        self.assertEqual(answer['result']['expires_at'], utc('2026-11-15T05:00:00'))
        self.assertEqual(answer['result']['rpc'], [['parallel', '2', '-g', 'hasito'], ['parallel', '7', '-g', 'jale'],
                                                   ['parallel', '3', '-g', 'pesadas']])
        self.assertEqual(answer['result']['notes']['mode'], 'Adelantado · vuelve a día a las 08:00')
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual(self.p.calls, [])

    def test_same_id_other_body_unknown_id_and_crash_never_repeat(self):
        request = self.mode('noche')
        one = self.engine.handle(request)
        calls = copy.deepcopy(self.p.calls)
        self.assertEqual(self.engine.handle(request), one)
        other = copy.deepcopy(request)
        other['change']['mode'] = 'dia'
        self.assertEqual((self.engine.handle(other).get('error') or {}).get('code'), 'idempotency_conflict')
        self.assertEqual(self.status('nunca-visto-01')['status'], 'unknown')
        self.assertEqual(self.p.calls, calls)
        self.build('crash', utc('2026-11-14T22:00:00'))
        request = self.mode('noche')
        with patch.object(self.engine, 'reconcile', side_effect=OSError('crash before RPC')):
            self.assertEqual(self.engine.handle(request)['status'], 'unknown')
        self.assertEqual(self.p.calls, [])
        self.assertEqual(self.engine.handle(request)['status'], 'applying')
        self.assertEqual(self.p.calls, [])
        self.assertTrue(self.engine.reconcile())
        self.assertEqual(self.status(request['action_id'])['status'], 'applied')

    def test_lock_busy_mode_is_terminal_without_effects(self):
        request = self.mode('noche')
        output = io.StringIO()
        with control.locked(self.engine.directory), patch.object(control, 'STATE_DIR', self.engine.directory), \
             patch.object(control, 'Pueue', return_value=self.p), patch.object(control, 'RACK_LOCK_SECONDS', .03), \
             patch.object(control.sys, 'argv', ['control.py', 'rack', '--host', 'desk']), \
             patch.object(control.sys, 'stdin', io.TextIOWrapper(io.BytesIO(json.dumps(request).encode()))), \
             redirect_stdout(output):
            control.main()
        answer = json.loads(output.getvalue())
        self.assertEqual((answer['status'], (answer.get('error') or {}).get('code'), answer.get('effects')), ('rejected', 'lock_busy', False))
        self.assertEqual(self.p.calls, [])

    def test_mini_rejects_mode_without_effects(self):
        p = FakePueue()
        mini = control.MiniControl(p, self.root / 'mini', clock=lambda: 1234.0, generation=lambda: 'mini-1')
        with control.locked(mini.directory):
            mini.adopt()
        p.calls.clear()
        view = mini.view()
        self.assertFalse(view['capabilities']['mode'])
        self.assertNotIn('notes', view)
        with mini.connect() as db:
            before = mini.state(db)
        request = dict(schema=control.HOST_SCHEMA, op='mode', action_id='modo-mini-001', dry_run=False,
                       expected={k: view[k] for k in ('revision', 'policy_hash', 'generation', 'mode', 'slots')},
                       change=dict(mode='noche'))
        answer = mini.handle(request)
        self.assertEqual((answer['status'], (answer.get('error') or {}).get('code'), answer.get('effects')), ('unsupported', 'unsupported', False))
        self.assertEqual(p.calls, [])
        with mini.connect() as db:
            self.assertEqual(mini.state(db), before)
            self.assertNotIn('mode_override', json.loads(db.execute('SELECT body FROM policy').fetchone()[0]))
        for mode in ('dia', 'noche'):
            with self.assertRaises(ValueError):
                mini.handle(dict(request, expected=dict(request['expected'], mode=mode)))

    def test_exact_notes_in_mexico_city_from_absolute_instants(self):
        day, night = utc('2026-11-14T14:00:00'), utc('2026-11-15T05:00:00')
        forced = dict(mode='x')
        cases = [((day, None, {}), ('Automático · cambia a noche a las 23:00', None)),
                 ((night, None, {}), ('Automático · cambia a día a las 08:00', None)),
                 ((day, forced, {}), ('Adelantado · vuelve a día a las 08:00', 'Ajustes manuales hasta las 23:00')),
                 ((night, forced, {}), ('Adelantado · vuelve a noche a las 23:00', 'Ajustes manuales hasta las 08:00')),
                 ((day, None, {'jale': {}}), ('Automático · cambia a noche a las 23:00', 'Lugares ajustados · hasta las 23:00')),
                 ((night, None, {'jale': {}}), ('Automático · cambia a día a las 08:00', 'Lugares ajustados · hasta las 08:00')),
                 # Mexico has no DST since 2022: summer events land at the same local time.
                 ((utc('2026-07-01T14:00:00'), None, {}), ('Automático · cambia a noche a las 23:00', None))]
        for args, (mode, detail) in cases:
            self.assertEqual(control.mode_notes(*args), dict(mode=mode, detail=detail))
        with patch.object(control, 'clock_text', side_effect=KeyError('America/Mexico_City')):
            view = self.engine.view()
        self.assertFalse(view['capabilities']['mode'])
        self.assertTrue(view['capabilities']['slots'])
        with patch.object(control, 'clock_text', side_effect=KeyError('America/Mexico_City')):
            answer = self.engine.handle(self.mode('noche'))
        self.assertEqual((answer['status'], answer.get('effects')), ('unsupported', False))
        self.assertEqual(self.p.calls, [])

    def test_guard_and_rapidas_ladder_intact_under_adelanto(self):
        self.engine.handle(self.mode('noche'))
        self.p.calls.clear()
        high = dict(self.low, io=60)
        self.engine.guard(high)
        self.assertEqual(self.p.groups['rapidas']['status'], 'Running')
        for g in ('hasito', 'jale', 'jale-verify', 'otros', 'pesadas'):
            self.assertEqual(self.p.groups[g]['status'], 'Paused')
        self.assertEqual(self.observed(), NOCHE)  # the guard pauses, it never changes quotas
        self.engine.guard(dict(self.low, memory=3 * 1024**3))
        self.assertEqual(self.p.groups['rapidas']['status'], 'Paused')
        with patch.object(control, 'pressure', return_value=self.low):
            self.engine.guard(self.low)
        self.assertTrue(all(self.p.groups[g]['status'] == 'Running' for g in control.GROUP_LIMITS))
        self.assertEqual(self.state()['mode_override']['mode'], 'noche')
        self.assertFalse(any(c[0] == 'start' and c[-1] in ('default', 'ajeno') for c in self.p.calls))

    def test_unreadable_adelanto_never_applies_table_or_opens_but_still_closes(self):
        bad = [dict(mode='dia'), dict(expires_at=utc('2026-11-15T14:00:00')), dict(requested_at=float('inf')),
               dict(actor='otro'), dict(requested_at=utc('2026-11-14T13:00:00')), dict(action_id='x')]
        for number, change in enumerate(bad):
            with self.subTest(change=change):
                self.build('bad-%d' % number, utc('2026-11-14T22:00:00'))
                self.engine.handle(self.mode('noche'))
                self.engine.guard(dict(self.low, io=60))
                with self.engine.connect(write=True) as db:
                    body = json.loads(db.execute('SELECT body FROM policy').fetchone()[0])
                    body['mode_override'].update(change)
                    db.execute('UPDATE policy SET body=?', (json.dumps(body),))
                self.p.calls.clear()
                with self.assertRaises(ValueError):
                    self.engine.reconcile()
                with patch.object(control, 'pressure', return_value=self.low), self.assertRaises(ValueError):
                    self.engine.guard(self.low)
                self.assertFalse(any(c[0] in ('parallel', 'start') for c in self.p.calls))
                with patch.object(control, 'pressure', return_value=self.low), self.assertRaises(ValueError):
                    self.engine.guard(dict(self.low, memory=1024**3))
                self.assertEqual(self.p.groups['rapidas']['status'], 'Paused')
                self.assertEqual(self.engine.handle(dict(schema=control.HOST_SCHEMA, op='capabilities'))['status'],
                                 'recovery_error')

    def test_u32_authority_without_key_reads_as_no_adelanto_and_needs_no_migration(self):
        with self.engine.connect(write=True) as db:
            body = json.loads(db.execute('SELECT body FROM policy').fetchone()[0])
            del body['mode_override']
            db.execute('UPDATE policy SET body=?', (control.canonical(body),))
        self.assertIsNone(self.state()['mode_override'])
        self.assertEqual(self.engine.view()['notes']['mode'], 'Automático · cambia a noche a las 23:00')
        self.assertEqual(self.engine.handle(self.mode('noche'))['status'], 'applied')
        self.assertEqual(self.observed(), NOCHE)


    def test_switch_turns_off_mode_without_effects(self):
        (self.engine.directory / 'slots.disabled').touch()
        self.assertFalse(self.engine.view()['capabilities']['mode'])
        answer = self.engine.handle(self.mode('noche'))
        self.assertEqual((answer['status'], (answer.get('error') or {}).get('code'), answer.get('effects')), ('rejected', 'disabled', False))
        self.assertEqual(self.p.calls, [])

if __name__ == '__main__':
    unittest.main()
