"""U3.2: lugares del desk con Pueue en memoria, reloj y generación inyectados; SQLite sólo en /tmp."""
import copy
from contextlib import redirect_stdout
from datetime import datetime, timezone
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

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('control_desk_slots', BASE / 'control.py')
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)

H = 3600
DAY0 = datetime(2026, 11, 14, tzinfo=timezone.utc).timestamp()
TABLE = {m: {g: row[m] for g, row in control.GROUP_LIMITS.items()} for m in ('dia', 'noche')}
LOW = dict(io=0, cpu=0, memory=16 * 1024**3)
ERRORS = (OSError, sqlite3.Error, ValueError, KeyError, TypeError, subprocess.SubprocessError)


def code_of(answer):
    # A missing error is an assertion failure, not a crash of the test.
    return (answer.get('error') or {}).get('code')


class FakePueue:
    def __init__(self, table):
        self.groups = {g: dict(parallel_tasks=n, status='Running') for g, n in table.items()}
        # `default` queda fuera del reparto; `ajeno` es una pausa que no es nuestra.
        self.groups['default'] = dict(parallel_tasks=1, status='Running')
        self.groups['ajeno'] = dict(parallel_tasks=4, status='Paused')
        self.calls, self.rpc, self.fail_at, self.unreadable = [], 0, None, False

    def boundary(self):
        self.rpc += 1
        if self.rpc == self.fail_at:
            raise subprocess.CalledProcessError(1, ['fake-rpc'])

    def status(self):
        self.boundary()
        if self.unreadable:
            raise ValueError('Estado ilegible')
        return dict(groups=copy.deepcopy(self.groups), tasks={})

    def parallel(self, group, number):
        self.calls.append(('parallel', group, number))
        self.groups[group]['parallel_tasks'] = number
        self.boundary()  # la RPC puede surtir efecto y perder la respuesta

    def call(self, *args):
        self.calls.append(args)
        if args[0] not in ('pause', 'start'):
            raise AssertionError('Sin atajos a Pueue real')
        self.groups[args[-1]]['status'] = 'Paused' if args[0] == 'pause' else 'Running'
        self.boundary()


class DeskSlotsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='u32-opus-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.gen = 'daemon-1'
        self.make(DAY0 + 15 * H)  # día; el último evento fue a las 14:00 UTC

    def make(self, now):
        self.now = now
        self.dir = self.root / f'estado-{len(list(self.root.iterdir()))}'
        self.p = FakePueue(TABLE[control.event_mode(control.last_event(now))])
        self.engine = control.DeskControl(self.p, self.dir, clock=lambda: self.now, generation=lambda: self.gen)
        with control.locked(self.dir):
            self.engine.adopt()
        self.p.calls.clear()

    def request(self, ident='slots-00000001', delta=-1, group='jale', op='slots', dry_run=False, **extra):
        view = self.engine.view()
        e = {k: view[k] for k in ('revision', 'generation', 'policy_hash', 'mode', 'slots')}
        return dict(schema=control.HOST_SCHEMA, op=op, action_id=ident, dry_run=dry_run, expected=e,
                    **({'change': dict(group=group, delta=delta)} if op == 'slots' else {}), **extra)

    def undo(self, ident, of, dry_run=False):
        return self.request(ident, op='undo', undo_of=of, dry_run=dry_run)

    def status(self, ident):
        return self.engine.handle(dict(schema=control.HOST_SCHEMA, op='status', action_id=ident))

    def guard(self, readings=LOW):
        with patch.object(self.engine, 'sample', return_value=readings):
            self.engine.guard(readings)

    def slots(self):
        return {g: self.p.groups[g]['parallel_tasks'] for g in control.GROUP_LIMITS}

    def fingerprint(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def overrides(self):
        with self.engine.connect() as db:
            return self.engine.state(db)['overrides']

    # Validación del host

    def test_host_limits_booleans_zero_nine_default_and_mode(self):
        for value in (True, False, 0, 9, -1, 1.5, '2'):
            with self.subTest(value=value):
                r = self.request(); r['expected']['slots']['jale'] = value
                with self.assertRaises(ValueError):
                    self.engine.handle(r)
        for delta in (True, 0, 2, '-1'):
            r = self.request(); r['change']['delta'] = delta
            with self.assertRaises(ValueError): self.engine.handle(r)
        # `default` queda fuera del reparto y `fijo` es el modo de la Mini.
        r = self.request(group='default')
        with self.assertRaises(ValueError): self.engine.handle(r)
        r = self.request(); r['expected']['mode'] = 'fijo'
        with self.assertRaises(ValueError): self.engine.handle(r)
        r = self.request(); del r['expected']['slots']['rapidas']
        with self.assertRaises(ValueError): self.engine.handle(r)
        r = self.request(); r['expected']['slots']['default'] = 1
        with self.assertRaises(ValueError): self.engine.handle(r)
        self.assertEqual(self.p.calls, [])

    def test_cap_fourteen_by_day_and_eighteen_by_night_counts_paused_groups(self):
        view = self.engine.view()
        self.assertEqual((view['mode'], view['cap'], sum(view['slots'].values())), ('dia', 14, 14))
        self.p.groups['pesadas']['status'] = 'Paused'
        refused = self.engine.handle(self.request('mas-dia-0001', delta=1, group='hasito'))
        self.assertEqual((refused['status'], code_of(refused)), ('rejected', 'invalid_request'))
        self.assertIn('14 de día y 18 de noche', refused['error']['message'])
        self.assertEqual(self.p.calls, [])
        self.assertEqual(self.engine.handle(self.request('menos-dia-001'))['status'], 'applied')
        added = self.engine.handle(self.request('mas-dia-0002', delta=1, group='hasito'))
        self.assertEqual(added['status'], 'applied')
        self.assertEqual(sum(self.slots().values()), 14)
        self.make(DAY0 + 6 * H)  # noche
        view = self.engine.view()
        self.assertEqual((view['mode'], view['cap'], view['slots']), ('noche', 18, TABLE['noche']))
        self.assertEqual(self.engine.handle(self.request('mas-noche-01', delta=1))['status'], 'rejected')
        control.validate_target(TABLE['noche'], TABLE['noche'], 18, self.engine.GROUPS)
        with self.assertRaises(ValueError):
            control.validate_target(TABLE['noche'], dict(TABLE['noche'], hasito=3), 18, self.engine.GROUPS)

    def test_seen_vector_revision_policy_generation_and_mode_checked_on_host(self):
        for key, value in (('revision', 99), ('policy_hash', control.POLICY_HASH), ('generation', 'otro'),
                           ('mode', 'noche'), ('slots', dict(TABLE['dia'], jale=4))):
            r = self.request('conflicto-' + key.replace('_', '-')); r['expected'][key] = value
            answer = self.engine.handle(r)
            self.assertEqual((answer['status'], code_of(answer)), ('conflict', 'stale_state'), key)
        # Vector vivo: un cambio fuera del panel invalida lo que Felix vio.
        r = self.request('vivo-00000001')
        self.p.groups['otros']['parallel_tasks'] = 1
        self.assertEqual(code_of(self.engine.handle(r)), 'stale_state')
        self.assertEqual(self.p.calls, [])

    def test_policy_hash_names_table_and_hours(self):
        self.assertNotEqual(control.DESK_POLICY_HASH, control.POLICY_HASH)
        self.assertEqual(self.engine.view()['policy_hash'], control.DESK_POLICY_HASH)
        other = control.hashlib.sha256(json.dumps(dict(limits=control.GROUP_LIMITS, hours=(6, 14)),
                                                  sort_keys=True).encode()).hexdigest()
        self.assertNotEqual(other, control.DESK_POLICY_HASH)
        # The hash names both the table and the 05:00/14:00 UTC events.
        named = control.hashlib.sha256(json.dumps(dict(limits=control.GROUP_LIMITS, hours=[5, 14]),
                                                  sort_keys=True).encode()).hexdigest()
        self.assertEqual(named, control.DESK_POLICY_HASH)
        with self.engine.connect(write=True) as db:
            body = json.loads(db.execute('SELECT body FROM policy').fetchone()[0])
            db.execute('UPDATE policy SET body=?', (control.canonical(dict(body, policy_hash=other)),))
        answer = self.engine.handle(dict(schema=control.HOST_SCHEMA, op='capabilities'))
        self.assertEqual(answer['status'], 'recovery_error')
        with self.assertRaises(ValueError): self.guard()
        self.assertEqual(self.p.calls, [])

    # Vencimiento por evento

    def test_adjust_an_instant_before_the_event_expires_at_it(self):
        event = DAY0 + 29 * H  # 05:00 UTC del día siguiente
        self.now = event - 0.001
        applied = self.engine.handle(self.request())
        self.assertEqual(applied['status'], 'applied')
        self.assertEqual(applied['result']['expires_at'], event)
        self.assertEqual(self.engine.view()['expires_at'], event)
        self.assertEqual(self.overrides()['jale']['expires_at'], event)
        self.now = event
        before = self.fingerprint()
        view = self.engine.view()  # lectura: muestra la tabla nueva sin escribirla
        self.assertEqual((view['mode'], view['desired'], view['expires_at']), ('noche', TABLE['noche'], None))
        self.assertTrue(view['recovery_pending'] and view['drift'])
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual(self.p.calls[1:], [])
        self.guard()
        self.assertEqual(self.slots(), TABLE['noche'])
        self.assertEqual(self.overrides(), {})
        view = self.engine.view()
        self.assertEqual((view['revision'], view['drift'], view['recovery_pending']), (applied['revision'] + 1, False, False))
        self.assertIn('"evento": "evento"', (self.dir / 'guardia.log').read_text())

    def test_missed_timer_converges_to_current_table_on_the_first_entry(self):
        self.engine.handle(self.request())
        self.engine.handle(self.request('mas-00000001', delta=1, group='otros'))
        self.p.calls.clear()
        self.now += 24 * H  # pasaron el evento de 05:00 y el de 14:00 sin timer
        view = self.engine.view()
        self.assertEqual((view['mode'], view['desired']), ('dia', TABLE['dia']))
        # La primera entrada real es un clic de Felix sobre lo visto antes del evento:
        # guarda el evento y repone la tabla, pero su clic no se aplica sobre otra base.
        stale = self.engine.handle(self.request('tras-evento-1', group='rapidas'))
        self.assertEqual((stale['status'], code_of(stale)), ('conflict', 'stale_state'))
        self.assertEqual((self.slots(), self.overrides()), (TABLE['dia'], {}))
        answer = self.engine.handle(self.request('tras-evento-2', group='rapidas'))
        self.assertEqual(answer['status'], 'applied')
        self.assertEqual(self.slots(), dict(TABLE['dia'], rapidas=2))
        self.assertEqual(set(self.overrides()), {'rapidas'})
        self.assertEqual(answer['result']['expires_at'], DAY0 + 29 * H + 24 * H)

    def test_timer_entry_keeps_adjustment_before_event_and_applies_new_table_after(self):
        self.engine.handle(self.request())
        self.p.calls.clear()
        with redirect_stdout(io.StringIO()):
            self.engine.apply()
        self.assertEqual(self.slots(), dict(TABLE['dia'], jale=4))
        self.assertEqual(self.p.calls, [])
        self.now = DAY0 + 29 * H
        with redirect_stdout(io.StringIO()) as output:
            self.engine.apply()
        self.assertEqual(self.slots(), TABLE['noche'])
        self.assertIn('Modo noche', output.getvalue())

    def test_periodic_guard_entry_dispatches_to_the_desk_authority(self):
        self.engine.handle(self.request())
        self.now = DAY0 + 29 * H  # the mode timer did not run
        with patch.object(control, 'pressure', return_value=LOW), \
             patch.object(control, 'engine_for', return_value=self.engine), \
             patch.object(self.engine, 'sample', return_value=LOW):
            control.guard(self.p, None, self.dir)
        self.assertEqual((self.slots(), self.overrides()), (TABLE['noche'], {}))

    def test_aplicar_cli_with_authority_keeps_the_adjustment_until_the_event(self):
        self.engine.handle(self.request())
        self.p.calls.clear()
        with patch.object(control, 'Pueue', return_value=self.p), patch.object(control, 'STATE_DIR', self.dir), \
             patch.object(control, 'engine_for', return_value=self.engine), \
             patch.object(control.sys, 'argv', ['control.py', 'aplicar']), redirect_stdout(io.StringIO()):
            control.main()
        self.assertEqual(self.slots(), dict(TABLE['dia'], jale=4))
        self.assertFalse(any(c[0] == 'parallel' for c in self.p.calls), self.p.calls)

    def test_schedule_metadata_inconsistent_with_event_is_illegible(self):
        with self.engine.connect(write=True) as db:
            body = json.loads(db.execute('SELECT body FROM policy').fetchone()[0])
            body.update(mode='noche', desired=TABLE['noche'])  # event still 14:00 UTC (día)
            db.execute('UPDATE policy SET body=?', (control.canonical(body),))
        answer = self.engine.handle(dict(schema=control.HOST_SCHEMA, op='capabilities'))
        self.assertEqual(answer['status'], 'recovery_error')
        self.assertEqual(self.p.calls, [])

    def test_undo_before_event_restores_exact_values(self):
        first = self.engine.handle(self.request())
        after_first = self.overrides()
        second = self.engine.handle(self.request('mas-00000001', delta=1, group='hasito'))
        self.assertEqual((first['status'], second['status']), ('applied', 'applied'))
        undone = self.engine.handle(self.undo('deshacer-00001', 'mas-00000001'))
        self.assertEqual(undone['status'], 'applied')
        self.assertEqual(self.slots(), dict(TABLE['dia'], jale=4))
        self.assertEqual(self.overrides(), after_first)
        self.make(DAY0 + 15 * H)
        self.engine.handle(self.request())
        self.engine.handle(self.undo('deshacer-00002', 'slots-00000001'))
        self.assertEqual((self.slots(), self.overrides()), (TABLE['dia'], {}))
        self.assertIsNone(self.engine.view()['expires_at'])

    def test_undo_after_event_is_rejected_without_effects(self):
        self.engine.handle(self.request())
        self.now = DAY0 + 29 * H
        before = self.fingerprint()
        trial = self.engine.handle(self.undo('deshacer-ensayo', 'slots-00000001', dry_run=True))
        self.assertEqual((trial['status'], code_of(trial), trial['effects']), ('conflict', 'undo_expired', False))
        self.assertEqual(self.fingerprint(), before)
        self.guard()  # el evento ya se guardó y rige la tabla de noche
        self.p.calls.clear()
        state = self.engine.view()
        answer = self.engine.handle(self.undo('deshacer-00001', 'slots-00000001'))
        self.assertEqual((answer['status'], code_of(answer)), ('conflict', 'undo_expired'))
        self.assertIn('venció con el cambio de horario', answer['error']['message'])
        self.assertEqual(self.p.calls, [])
        self.assertEqual(self.slots(), TABLE['noche'])
        self.assertEqual(self.engine.view()['revision'], state['revision'])

    def test_pending_intent_superseded_by_event_is_aborted(self):
        with patch.object(self.p, 'parallel', side_effect=OSError('RPC caída')):
            answer = self.engine.handle(self.request())
        self.assertEqual(answer['status'], 'partial')
        self.now = DAY0 + 29 * H
        self.guard()
        answer = self.status('slots-00000001')
        self.assertEqual((answer['status'], code_of(answer)), ('aborted', 'expired'))
        self.assertEqual(answer['result']['desired'], TABLE['noche'])
        self.assertIsNone(answer['result']['undo_of'])
        self.assertEqual(self.engine.view()['pending_actions'], [])

    # Orden y tope

    def test_event_applies_reductions_before_increases(self):
        self.make(DAY0 + 6 * H)  # noche 18
        self.engine.handle(self.request('menos-0000001', group='rapidas'))
        self.engine.handle(self.request('mas-000000001', delta=1, group='hasito'))
        self.p.calls.clear()
        self.now = DAY0 + 14 * H
        self.guard()
        order = [(g, n) for _, g, n in self.p.calls if _ == 'parallel']
        self.assertEqual(order[-1], ('rapidas', 3))
        self.assertEqual({g for g, _ in order[:-1]}, {'hasito', 'jale', 'pesadas'})
        self.assertEqual(self.slots(), TABLE['dia'])

    def test_above_cap_only_monotonic_reduction(self):
        self.p.groups['jale']['parallel_tasks'] = 8  # 17 por fuera del panel
        self.assertEqual(self.engine.handle(self.request('mas-00000001', delta=1, group='otros'))['status'], 'conflict')
        self.assertTrue(self.engine.reconcile())
        self.assertEqual(self.slots(), TABLE['dia'])
        with self.assertRaises(ValueError):
            control.validate_target(dict(TABLE['dia'], jale=8), dict(TABLE['dia'], jale=7, otros=3), 14, self.engine.GROUPS)
        control.validate_target(dict(TABLE['dia'], jale=8), dict(TABLE['dia'], jale=7), 14, self.engine.GROUPS)

    # Fallos, ensayo e idempotencia

    def test_failure_after_each_rpc_is_visible_and_guard_completes(self):
        seen = set()
        for boundary in range(1, 7):
            with self.subTest(boundary=boundary):
                self.make(DAY0 + 15 * H)
                ident = f'falla-{boundary:08d}'
                request = self.request(ident)
                self.p.rpc, self.p.fail_at = 0, boundary
                answer = self.engine.handle(request)
                self.p.fail_at = None
                seen.add(answer['status'])
                self.assertIn(answer['status'], ('applied', 'partial', 'unknown'))
                if answer['status'] == 'unknown':
                    self.assertEqual(code_of(answer), 'recovery_error')
                if answer['status'] == 'partial':
                    self.assertEqual(answer['result']['desired'], dict(TABLE['dia'], jale=4))
                self.guard()
                final = self.status(ident)
                accepted = self.overrides() != {}
                self.assertEqual(final['status'], 'applied' if accepted else 'unknown')
                self.assertEqual(self.slots(), dict(TABLE['dia'], jale=4) if accepted else TABLE['dia'])
                # Un ID cuya respuesta se perdió nunca se reenvía como efecto nuevo.
                self.assertEqual(self.status(ident)['status'], final['status'])
        self.assertTrue({'partial', 'unknown'} <= seen, seen)

    def test_dry_run_has_no_effects_even_after_an_elapsed_event(self):
        before = self.fingerprint()
        trial = self.engine.handle(self.request(dry_run=True))
        self.assertEqual((trial['status'], trial['effects']), ('dry_run', False))
        self.assertEqual(trial['result']['expires_at'], DAY0 + 29 * H)
        self.assertEqual(trial['result']['rpc'], [['parallel', '4', '-g', 'jale']])
        self.now = DAY0 + 29 * H
        # El ensayo no recupera: con el evento sin aplicar, lo visto difiere del deseo.
        trial = self.engine.handle(self.request('ensayo-000002', delta=-1, group='pesadas', dry_run=True))
        self.assertEqual((trial['status'], code_of(trial), trial['effects']), ('conflict', 'stale_state', False))
        self.assertEqual(self.fingerprint(), before)
        self.guard()
        self.p.calls.clear()
        before = self.fingerprint()
        trial = self.engine.handle(self.request('ensayo-000003', delta=-1, group='pesadas', dry_run=True))
        self.assertEqual((trial['status'], trial['result']['desired']), ('dry_run', dict(TABLE['noche'], pesadas=2)))
        self.assertEqual(trial['result']['expires_at'], DAY0 + 38 * H)
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual(self.p.calls, [])

    def test_same_id_other_body_and_unknown_id_never_repeat(self):
        request = self.request()
        one = self.engine.handle(request)
        self.assertEqual(self.engine.handle(request), one)
        changed = copy.deepcopy(request); changed['change']['group'] = 'otros'
        self.assertEqual(code_of(self.engine.handle(changed)), 'idempotency_conflict')
        self.assertEqual(len([c for c in self.p.calls if c[0] == 'parallel']), 1)
        self.assertEqual(self.status('nunca-visto-01')['status'], 'unknown')
        self.assertEqual(len([c for c in self.p.calls if c[0] == 'parallel']), 1)

    def test_old_host_without_authority_answers_unsupported_from_the_real_cli(self):
        home = self.root / 'casa-antigua'
        home.mkdir()
        harness = '''
import importlib.util,sys
spec=importlib.util.spec_from_file_location('cli',sys.argv[1]);c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
class Fake:
 def __getattr__(self,name):raise AssertionError('host antiguo llamó a Pueue')
c.Pueue=lambda *a:Fake()
sys.argv=['control.py','rack','--host','desk'];c.main()
'''
        for body, status in ((dict(schema=control.HOST_SCHEMA, op='capabilities'), 'unsupported'),
                             (dict(schema=control.HOST_SCHEMA, op='status', action_id='viejo-0000001'), 'unknown'),
                             (self.request('viejo-0000002'), 'unsupported')):
            result = subprocess.run(['/usr/bin/python3', '-I', '-c', harness, str(BASE / 'control.py')],
                                    input=json.dumps(body), text=True, capture_output=True, timeout=5,
                                    env=dict(os.environ, HOME=str(home)))
            self.assertEqual(result.returncode, 0, result.stderr)
            answer = json.loads(result.stdout)
            self.assertEqual((answer['status'], code_of(answer)), (status, 'unsupported'))
        self.assertEqual(list(home.rglob('*')), [])

    # Guardia, reinicio y estado ilegible

    def test_desk_guard_and_rapidas_ladder_with_authority(self):
        self.guard(dict(io=50, cpu=0, memory=16 * 1024**3))
        paused = {g for g, row in self.p.groups.items() if row['status'] == 'Paused'}
        self.assertEqual(paused, set(control.GROUP_LIMITS) - {'rapidas'} | {'ajeno'})
        self.assertEqual(self.engine.view()['pause_reasons']['jale'], ['pressure'])
        self.guard(dict(io=0, cpu=0, memory=3 * 1024**3))
        self.assertEqual(self.p.groups['rapidas']['status'], 'Paused')
        self.assertEqual(self.p.groups['default']['status'], 'Running')
        self.guard(LOW)
        self.assertEqual({g for g, row in self.p.groups.items() if row['status'] == 'Paused'}, {'ajeno'})

    def test_restart_lost_parallel_is_reconciled_without_opening_pauses(self):
        self.engine.handle(self.request())
        self.p.calls.clear()
        self.gen = 'daemon-2'
        for g in control.GROUP_LIMITS:
            self.p.groups[g]['parallel_tasks'] = 1
        self.p.groups['otros']['status'] = 'Paused'
        self.guard()
        self.assertEqual(self.slots(), dict(TABLE['dia'], jale=4))
        self.assertEqual(self.p.groups['otros']['status'], 'Paused')
        self.assertIn('ambiguous', self.engine.view()['pause_reasons']['otros'])
        self.assertFalse(any(c[0] == 'start' for c in self.p.calls))

    def test_illegible_state_never_applies_table_nor_opens_groups(self):
        self.engine.handle(self.request())
        self.p.groups['otros']['status'] = 'Paused'
        self.p.calls.clear()
        for damage in ('corrupta', 'ausente'):
            with self.subTest(damage=damage):
                if damage == 'corrupta':
                    self.engine.path.write_bytes(b'no es sqlite')
                else:
                    self.engine.path.unlink()
                self.assertEqual(self.engine.handle(dict(schema=control.HOST_SCHEMA, op='capabilities'))['status'],
                                 'recovery_error')
                self.assertEqual(self.engine.handle(self.request_blind())['status'], 'unknown')
                with self.assertRaises(ERRORS): self.guard(LOW)
                with self.assertRaises(ERRORS), redirect_stdout(io.StringIO()): self.engine.apply()
                with self.assertRaises(ERRORS): self.guard(dict(io=50, cpu=0, memory=16 * 1024**3))
                self.assertFalse(any(c[0] in ('parallel', 'start') for c in self.p.calls), self.p.calls)
                self.assertEqual(self.p.groups['jale']['status'], 'Paused')  # sigue cerrando
                self.assertEqual(self.p.groups['otros']['status'], 'Paused')
                self.assertEqual(self.slots()['jale'], 4)
                with self.assertRaises(ValueError): self.engine.adopt()

    def request_blind(self):
        return dict(schema=control.HOST_SCHEMA, op='slots', action_id='ciego-0000001', dry_run=False,
                    expected=dict(revision=2, generation=self.gen, policy_hash=control.DESK_POLICY_HASH,
                                  mode='dia', slots=dict(TABLE['dia'], jale=4)), change=dict(group='otros', delta=-1))

    def test_switch_rejects_new_effects_and_answers_disabled(self):
        (self.dir / 'slots.disabled').touch()
        answer = self.engine.handle(self.request())
        self.assertEqual((answer['status'], code_of(answer)), ('rejected', 'disabled'))
        self.assertIn('en el desk', answer['error']['message'])
        view = self.engine.view()
        self.assertFalse(view['capabilities']['slots'])
        self.assertEqual((view['capabilities']['mode'], view['capabilities']['reorder'],
                          view['capabilities']['restart_queue']), (False, False, False))
        self.assertEqual(self.p.calls, [])


class DeskInstallerTests(unittest.TestCase):
    """remote.py tal como lo arma deploy.py, con binarios falsos y /proc falso."""

    def setUp(self):
        import test_pueue_desk
        self.fixture = test_pueue_desk.Fixture('setUp')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        (self.root / 'managed').touch()
        pid = self.root / '.local/share/pueue/pueue.pid'
        pid.parent.mkdir(parents=True)
        pid.write_text('4242\n')
        self.proc = self.root / 'proc'
        (self.proc / '4242').mkdir(parents=True)
        (self.proc / '4242/comm').write_text('pueued\n')
        (self.proc / '4242/stat').write_text('4242 (pueued) S ' + ' '.join(str(n) for n in range(4, 53)) + '\n')
        self.state = self.root / '.local/state/pao/pueue-desk'

    def program(self):
        core = (BASE / 'control.py').read_text().split('if __name__ == "__main__":')[0]
        files = {'control.py': (BASE / 'control.py').read_text()}
        files.update({p.name: p.read_text() for p in sorted((BASE / 'units').iterdir())})
        namespace = {'__name__': 'remoto'}
        exec(compile(core + '\nFILES = ' + repr(files) + '\n' + (BASE / 'remote.py').read_text(), 'remote', 'exec'),
             namespace)
        namespace['PROC'] = self.proc
        return namespace

    def run_remote(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            self.program()['remote'](*args)
        return output.getvalue()

    def engine(self):
        return control.DeskControl(control.Pueue(self.root / '.local/bin/pueue'), self.state, proc=self.proc)

    def test_adoption_then_reinstall_never_applies_bare_table(self):
        dry = self.run_remote('install', True, True, None)
        self.assertIn('adoptar deseo/propiedad', dry)
        self.assertFalse(self.state.exists())
        output = self.run_remote('install', False, True, None)
        self.assertIn('Autoridad adoptada', output)
        self.assertTrue((self.state / 'control.sqlite3').exists())
        self.assertFalse(os.path.lexists(self.state / 'slots.disabled'))
        engine = self.engine()
        view = engine.view()
        table = TABLE[view['mode']]
        self.assertEqual((view['desired'], view['slots']), (table, table))
        r = dict(schema=control.HOST_SCHEMA, op='slots', action_id='instalar-0001', dry_run=False,
                 expected={k: view[k] for k in ('revision', 'generation', 'policy_hash', 'mode', 'slots')},
                 change=dict(group='jale', delta=-1))
        with control.locked(self.state):
            self.assertEqual(engine.handle(r)['status'], 'applied')
        # Reinstalar sin --cuotas detecta la autoridad y conserva el ajuste de Felix.
        self.run_remote('install', False, False, None)
        self.assertEqual(self.fixture.pueue.status()['groups']['jale']['parallel_tasks'], table['jale'] - 1)
        self.assertEqual(engine.view()['desired'], dict(table, jale=table['jale'] - 1))
        with self.assertRaises(RuntimeError):
            self.run_remote('withdraw', False, False, None)
        self.assertTrue((self.root / '.config/systemd/user/pueued.service').exists())

    def test_operator_switch_is_preserved_and_failed_adoption_stays_closed(self):
        self.state.mkdir(parents=True)
        (self.state / 'slots.disabled').write_text('Felix\n')
        self.run_remote('install', False, True, None)
        self.assertEqual((self.state / 'slots.disabled').read_text(), 'Felix\n')
        self.assertTrue((self.state / 'control.sqlite3').exists())

    def test_adoption_refused_without_identified_daemon_and_switch_stays(self):
        (self.proc / '4242/comm').write_text('otro\n')
        with self.assertRaises(ValueError):
            self.run_remote('install', False, True, None)
        self.assertFalse(control.has_authority(self.state) if self.state.exists() else False)
        (self.proc / '4242/comm').write_text('pueued\n')
        # Sin pueued administrado, no se adopta nada.
        (self.root / 'managed').unlink()
        with self.assertRaises(RuntimeError):
            self.run_remote('install', False, True, None)
        self.assertFalse(self.state.exists() and control.has_authority(self.state))


if __name__ == '__main__':
    unittest.main()
