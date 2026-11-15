"""SEN-6417: guard fields from snapshot to panel. Synthetic files and DOM; no real host."""
import safety  # fail closed before any subprocess
import datetime
import html
import json
import re
import shutil
import sqlite3
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import test_rack as rack_tests
from test_rack import snapshot, serve
from test_slots import BASE

NOW = 1794664800  # 2026-11-20T16:00:00Z
GIB = 1024 ** 3


def iso(seconds):
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).isoformat()


def guard(action='mantener', cpu=22, io=5, memory=20 * GIB, bloqueos=(), admitted=None, age=10):
    """A guard block as serve publishes it, with the installed desk thresholds."""
    return dict(action=action, at=iso(NOW - age), cpu_pause=30, cpu_resume=15,
                pressure=dict(cpu=cpu, io=io, memory=memory, at=iso(NOW - age)),
                thresholds=dict(normal={'pause': {'io': 40, 'cpu': 30, 'memory': 4 * GIB}, 'resume': {'io': 20, 'cpu': 15, 'memory': 6 * GIB}},
                                rapidas={'pause': {'memory': 4 * GIB}, 'resume': {'memory': 6 * GIB}}),
                last_result=dict(evento='retenido', causa=None, bloqueos=list(bloqueos), at=iso(NOW - age), grupo=None, id=None, restante_ms=None),
                last_admission=None if admitted is None else dict(at=iso(NOW - admitted), grupo='otros', id=41))


def task(ident, state):
    return dict(id=ident, group='otros', priority=0, state=state, name='synthetic', scope=None, ticket=None, project=None, repo=None, sha=None,
                branch=None, subject=None, result=None, enqueued=NOW - 300, start=NOW - 200 if state == 'Running' else None, end=None)


class GuardCellBase(unittest.TestCase):
    def render(self, guard_block, reasons=('pressure',), status='Paused', slots=2, running=0, queued=1, holds=None):
        tasks = [task(i, 'Running') for i in range(1, running + 1)] + [task(100 + i, 'Queued') for i in range(queued)]
        snap = dict(schema='rack-host@1', host='desk', taken=NOW, os='linux', cpu=None, mem=None, disk=None, errors=[],
                    groups=[dict(name='otros', status=status, slots=slots)], tasks=tasks, guard=guard_block)
        snap = serve.validate_snapshot(snap, 'desk')
        # The panel reads the published control as is; its validator is covered by the startup tests.
        snap['control'] = dict(pause_reasons={'otros': list(reasons)} if status == 'Paused' else {},
                               startup_holds=holds or {}, startup_waiting={})
        fixture = dict(now=NOW, tickets={}, hosts=[dict(id='desk', name='Felix DESKTOP · WSL', fresh=True, age=0, estimates={},
                                                        actions=dict(reason=None), snapshot=snap)])
        source = (BASE / 'index.html').read_text().split('<script>', 1)[1].split('</script>', 1)[0]
        result = subprocess.run(['node', str(BASE / 'tests/guard-cell-check.js')], input=json.dumps(dict(js=source, fixture=fixture)),
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        lane = out['html'].split('<div class="lane">', 1)[1].split('<div class="lane">', 1)[0]
        text = lambda s: re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', s))).strip()
        return text(lane), out['html'], out['notice']


class GuardCellTests(GuardCellBase):
    """Five of the six panel states of a paused group with free slots; the sixth, full, is a regression below."""

    def test_high_pressure_says_held_by_the_binding_metric_and_how_it_trickles(self):
        lane, _, _ = self.render(guard('pausar', cpu=34))
        self.assertIn('Retenido por presión CPU 34 % · entra de a una bajo 30', lane)
        self.assertNotIn('Libre', lane)
        lane, _, _ = self.render(guard('pausar', cpu=34, memory=3 * GIB))
        self.assertIn('Retenido por presión memoria libre 3.0 GB · entra de a una sobre 4 GB', lane)
        self.assertNotIn('CPU', lane)
        lane, _, _ = self.render(guard('pausar', cpu=10, io=55))
        self.assertIn('E/S 55 % · entra de a una bajo 40', lane)
        self.assertNotIn('CPU', lane)

    def test_mid_band_says_trickle_reopen_threshold_and_last_admission(self):
        lane, _, _ = self.render(guard('mantener', cpu=22, admitted=40))
        self.assertIn('Entra de a una CPU 22 % · abre bajo 15 · última hace 40 s', lane)
        self.assertNotIn('Libre', lane)
        lane, _, _ = self.render(guard('mantener', cpu=10, io=25))
        self.assertIn('Entra de a una E/S 25 % · abre bajo 20', lane)
        self.assertNotIn('CPU', lane)
        self.assertNotIn('última', lane)

    def test_blocking_gate_states_its_cause_instead_of_pressure(self):
        lane, _, _ = self.render(guard('mantener', bloqueos=[dict(grupo=None, operacion='one', causa='docker')]))
        self.assertIn('Retenido Docker no responde', lane)
        self.assertNotIn('Entra de a una', lane)
        lane, _, _ = self.render(guard('mantener', bloqueos=[dict(grupo=None, operacion='one', causa='admission_unknown')]))
        self.assertIn('admisión sin confirmar · resolver con control.py resolver-admision', lane)

    def test_without_guard_data_the_state_is_unknown_never_free(self):
        for block in (None, guard(age=600), dict(guard(), pressure=None)):
            lane, _, _ = self.render(block)
            self.assertIn('Retenido por presión estado de la guardia desconocido', lane)
            self.assertNotIn('Libre', lane)

    def test_ambiguous_and_startup_share_one_cell_without_host_repeat(self):
        lane, page, notice = self.render(guard(), reasons=('ambiguous', 'startup'), holds={'otros': 'pausa de origen desconocido'})
        self.assertIn('Retenido al reiniciar pausa de origen desconocido · resolver con control.py reanudar otros', lane)
        self.assertEqual(page.count('control.py reanudar otros'), 1)
        self.assertNotIn('Pausa tras reinicio', notice)
        lane, _, notice = self.render(guard(), reasons=('ambiguous',))
        self.assertIn('Pausa de origen desconocido resolver con control.py reanudar otros', lane)
        self.assertNotIn('control.py', notice)


class GuardCellRegressionTests(GuardCellBase):
    """Already true on main and must stay true: full and open groups."""

    def test_full_group_says_nothing_about_the_pause(self):
        lane, _, _ = self.render(guard('pausar', cpu=34), slots=1, running=1)
        for word in ('Retenido', 'Entra de a una', 'presión', 'Libre', 'Pausa'):
            self.assertNotIn(word, lane)

    def test_open_group_still_shows_free(self):
        lane, _, _ = self.render(guard('reanudar', cpu=5), status='Running')
        self.assertEqual(lane.count('Libre'), 2)
        self.assertNotIn('Retenido', lane)


class Fields(dict):
    def __getitem__(self, key):
        value = self.get(key)
        return Fields(value) if isinstance(value, dict) else value


class GuardSnapshotTests(unittest.TestCase):
    setUp = rack_tests.SnapshotTests.setUp
    fake_pueue = rack_tests.SnapshotTests.fake_pueue

    def home(self, controller=True, admission=True, rows=()):
        state = self.root / '.local/state/pao/pueue-desk'
        state.mkdir(parents=True)
        if controller:
            lib = self.root / '.local/lib/pao-pueue-desk'
            lib.mkdir(parents=True)
            shutil.copyfile(str(BASE.parent / 'pueue-desk/control.py'), str(lib / 'control.py'))
        with sqlite3.connect(str(state / 'control.sqlite3')) as db:
            db.execute('CREATE TABLE policy(id INTEGER PRIMARY KEY, body TEXT)')
            if admission:
                db.execute('CREATE TABLE guard_admission (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)')
                confirmed = dict(utc=iso(NOW - 40), grupo='otros', id=41, created_at=iso(NOW - 900), outcome='confirmed')
                db.execute('INSERT INTO guard_admission VALUES (1, ?)', (json.dumps(dict(version=1, last_attempt=confirmed, last_confirmed=confirmed)),))
        (state / 'guardia.log').write_text(''.join(json.dumps(r) + '\n' for r in rows))
        proc = self.root / 'proc'
        (proc / 'pressure').mkdir(parents=True)
        (proc / 'pressure/cpu').write_text('some avg10=20.00 avg60=22.50 avg300=10.00 total=1\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n')
        (proc / 'pressure/io').write_text('some avg10=9.00 avg60=9.00 avg300=1.00 total=1\nfull avg10=4.00 avg60=4.00 avg300=1.00 total=1\n')
        (proc / 'meminfo').write_text('MemTotal:       32000000 kB\nMemAvailable:   20000000 kB\n')
        return proc

    def collect(self, proc):
        with patch.object(snapshot, 'linux_capacity', return_value=None):
            data = snapshot.collect('linux', 'desk', pueue=self.fake_pueue({}), home=self.root, proc=proc, now=NOW)
        # A missing guard or field reads as None, so an old snapshot fails by assertion, not by KeyError.
        return data, Fields(serve.validate_snapshot(data, 'desk')['guard'] or {})

    def test_mid_band_publishes_pressure_installed_thresholds_same_tick_result_and_last_admission(self):
        proc = self.home(rows=[dict(utc=iso(NOW - 70), evento='decision', accion='mantener', tick='a'),
                               dict(utc=iso(NOW - 70), evento='retenido', tick='a', causa='docker',
                                    bloqueos=[dict(grupo=None, operacion='one', causa='docker')]),
                               dict(utc=iso(NOW - 10), evento='decision', accion='mantener', tick='b'),
                               dict(utc=iso(NOW - 10), evento='admision_una', tick='b', causa=None, bloqueos=[], grupo='otros', id=41)])
        data, published = self.collect(proc)
        self.assertEqual(published['action'], 'mantener')
        self.assertEqual(published['pressure'], dict(cpu=22.5, io=4.0, memory=20000000 * 1024, at=iso(NOW)))
        self.assertEqual(published['thresholds']['normal']['resume'], {'io': 20, 'cpu': 15, 'memory': 6 * GIB})
        self.assertEqual(published['thresholds']['rapidas'], {'pause': {'memory': 4 * GIB}, 'resume': {'memory': 6 * GIB}})
        self.assertEqual((published['last_result']['evento'], published['last_result']['bloqueos']), ('admision_una', []))
        self.assertEqual(published['last_admission'], dict(at=iso(NOW - 40), grupo='otros', id=41))
        self.assertEqual([e for e in data['errors'] if e['stage'].startswith('guard')], [])

    def test_result_of_another_pass_is_never_attributed_to_the_last_decision(self):
        proc = self.home(rows=[dict(utc=iso(NOW - 70), evento='decision', accion='pausar', tick='a'),
                               dict(utc=iso(NOW - 70), evento='retenido', tick='a', causa='docker',
                                    bloqueos=[dict(grupo=None, operacion='resume', causa='docker')]),
                               dict(utc=iso(NOW - 10), evento='decision', accion='mantener', tick='b')])
        _, published = self.collect(proc)
        self.assertEqual(published['action'], 'mantener')
        self.assertIsNone(published['last_result'])

    def test_old_controller_and_old_log_publish_old_fields_and_unknown_rest(self):
        proc = self.home(controller=False, admission=False, rows=[dict(utc=iso(NOW - 10), evento='decision', accion='pausar')])
        data, published = self.collect(proc)
        self.assertEqual((published['action'], published['cpu_pause'], published['cpu_resume']), ('pausar', 30, 15))
        self.assertEqual((published['thresholds'], published['last_result'], published['last_admission']), (None, None, None))
        self.assertEqual((published['pressure'] or {}).get('cpu'), 22.5)
        self.assertEqual([e for e in data['errors'] if e['stage'].startswith('guard')], [])

    def test_unreadable_pressure_is_null_with_its_error_and_keeps_the_rest(self):
        proc = self.home(rows=[dict(utc=iso(NOW - 10), evento='decision', accion='mantener', tick='b')])
        (proc / 'pressure/io').write_text('some avg10=1\n')
        data, published = self.collect(proc)
        self.assertIsNone(published['pressure'])
        self.assertEqual((published['last_admission'] or {}).get('grupo'), 'otros')
        self.assertIn('guard_pressure', [e['stage'] for e in data['errors']])


class GuardServeTests(unittest.TestCase):
    def published(self, block):
        snap = dict(schema='rack-host@1', host='desk', taken=NOW, os='linux', cpu=None, mem=None, disk=None, errors=[],
                    groups=[], tasks=[], guard=block)
        published = serve.validate_snapshot(snap, 'desk')['guard']
        return None if published is None else Fields(published)

    def test_pre_sen_6417_guard_is_accepted_with_unknown_new_fields(self):
        old = dict(action='pausar', at=iso(NOW), cpu_pause=30, cpu_resume=15)
        self.assertEqual(self.published(old), dict(old, pressure=None, thresholds=None, last_result=None, last_admission=None))
        self.assertIsNone(self.published(None))

    def test_invalid_core_drops_guard_and_invalid_parts_drop_alone(self):
        self.assertIsNone(self.published(dict(guard(), action='borrar')))
        self.assertIsNone(self.published(dict(guard(), at='ayer')))
        bad = guard(bloqueos=[dict(grupo=None, operacion='one', causa='otra cosa')])
        bad['pressure']['cpu'] = float('nan')
        bad['envs'] = {'GH_T': 'x'}
        bad['last_admission'] = dict(at=iso(NOW), grupo='otros', id=-1)
        published = self.published(bad)
        self.assertEqual((published['pressure'], published['last_result'], published['last_admission']), (None, None, None))
        self.assertEqual(published['thresholds'], guard()['thresholds'])
        self.assertEqual(published['action'], 'mantener')
        self.assertNotIn('envs', published)


if __name__ == '__main__':
    unittest.main()
