"""U3.3 Día/Noche through the rack channel: fake executor, private controller, no real binaries."""
import safety
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from test_slots import control, FakePueue, accion, BASE

DAY = 1794686400.0  # 2026-11-14 22:00 UTC = 16:00 en México, horario de día
NOCHE = {'hasito': 2, 'jale': 7, 'jale-verify': 1, 'otros': 2, 'pesadas': 3, 'rapidas': 3}


class DeskModeChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='rack-u33-opus-', dir='/tmp'); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.now = DAY
        self.p = FakePueue(); self.p.groups = {g: dict(status='Running', parallel_tasks=n) for g, n in control.desk_profile(self.now).items()}
        self.engine = control.MiniControl(self.p, self.root/'host', clock=lambda: self.now, generation=lambda: 'desk-1', host='desk')
        with control.locked(self.engine.directory): self.engine.adopt()
        self.host = dict(id='desk', transport='ssh', actions=dict(transport='ssh', reason=None,
                         controllers=copy.deepcopy(accion.CONTROLLERS), argv=list(accion.DESK_ARGV)))
        self.payload = dict(now=self.now, hosts=[dict(id='desk', fresh=True, age=0, snapshot={})])
        self.refresh()
        self.calls = []
        def execute(host, target, wire, timeout):
            self.assertEqual(target, 'host-control'); self.calls.append(copy.deepcopy(wire))
            return self.engine.handle(wire)
        self.execute = execute
        self.actions = accion.Actions([self.host], self.root/'rack', lambda: self.payload, execute, clock=lambda: self.now)

    def refresh(self):
        self.payload['now'] = self.now
        self.payload['hosts'][0]['snapshot'] = dict(control=accion.host_channel.control(self.engine.view(), 'desk'))

    def request(self, mode='noche', ident='desk-mode-00001', dry_run=False, host='desk'):
        v = self.engine.view()
        return dict(schema=accion.SCHEMA, op='mode', host=host, action_id=ident, dry_run=dry_run,
                    expected={k: v[k] for k in ('revision', 'policy_hash', 'generation', 'mode', 'slots')}, change=dict(mode=mode))

    def undo(self, of, ident='desk-undo-00001'):
        r = self.request(ident=ident); del r['change']; r.update(op='undo', undo_of=of)
        return r

    def test_channel_mode_and_undo_sanitized_with_host_notes(self):
        r = self.request(); code, answer = self.actions.post(r)
        self.assertEqual((code, answer['status']), (200, 'applied'))
        self.assertEqual(self.calls[0], dict(schema='rack-host-control@1', op='mode', action_id=r['action_id'],
                                             dry_run=False, expected=r['expected'], change=dict(mode='noche')))
        result = answer['result']
        self.assertEqual((result['mode'], result['cap'], result['observed'], result['expires_at']),
                         ('noche', 18, NOCHE, 1794718800.0))
        self.assertEqual(result['notes'], dict(mode='Adelantado · vuelve a día a las 08:00', detail='Ajustes manuales hasta las 23:00'))
        self.assertNotIn('before_mode_override', json.dumps(answer))
        self.assertEqual(self.actions.post(r)[1], answer)  # same ID is read, never re-sent
        self.assertEqual([c['op'] for c in self.calls], ['mode'])
        self.refresh()
        safe = self.payload['hosts'][0]['snapshot']['control']
        self.assertEqual((safe['mode'], safe['cap'], safe['notes']['mode']), ('noche', 18, 'Adelantado · vuelve a día a las 08:00'))
        code, back = self.actions.post(self.undo(r['action_id']))
        self.assertEqual((code, back['status'], back['result']['mode'], back['result']['cap']), (200, 'applied', 'dia', 14))
        self.assertEqual(back['result']['notes'], dict(mode='Automático · cambia a noche a las 23:00', detail=None))
        self.assertEqual(self.p.calls[3:], [('hasito', 1), ('jale', 5), ('pesadas', 2)])

    def test_old_or_incapable_desk_never_receives_mode(self):
        for control_view in (None, dict(capabilities=dict(mode=False)), dict(capabilities={})):
            with self.subTest(control=control_view):
                self.payload['hosts'][0]['snapshot'] = {} if control_view is None else dict(control=control_view)
                code, answer = self.actions.post(self.request(ident='desk-mode-old-1'))
                self.assertEqual(code, 409)
                self.assertEqual((answer.get('error') or {}).get('message'), 'Falta actualizar el control de este host.')
        self.assertEqual((self.calls, self.p.calls), ([], []))
        # A controller that predates U3.3 answers its explicit unsupported shape.
        self.refresh()
        old = dict(schema='rack-host-result@1', action_id='desk-mode-old-2', status='unsupported',
                   error=dict(code='unsupported', message='x'))
        self.actions.executor = lambda *a, **k: old
        self.assertEqual(self.actions.post(self.request(ident='desk-mode-old-2'))[1]['status'], 'unsupported')
        self.assertEqual(self.p.calls, [])

    def test_mini_and_malformed_mode_requests_are_400_before_any_effect(self):
        mini = self.request(host='mini')  # otherwise valid for the Mini: fixed mode and its own vector
        mini['expected'] = dict(mini['expected'], mode='fijo', slots=dict(control.MINI_PROFILE))
        bad = [mini, self.request(host='mini'), dict(self.request(), change=dict(mode='fijo')),
               dict(self.request(), change=dict(mode='noche', extra=1)), dict(self.request(), change='noche')]
        undo_with_change = self.undo('desk-mode-00001'); undo_with_change['change'] = dict(mode='dia')
        for request in bad + [undo_with_change]:
            with self.subTest(request=request.get('change')):
                with self.assertRaises(ValueError):  # serve.py answers 400 to a ValueError
                    self.actions.post(request)
        self.assertEqual((self.calls, self.p.calls), ([], []))

    def test_same_mode_and_foreign_work_are_terminal_rejections(self):
        code, answer = self.actions.post(self.request('dia'))
        self.assertEqual((code, answer['status'], (answer.get('error') or {}).get('code'), (answer.get('error') or {}).get('message')),
                         (409, 'rejected', 'same_mode', 'Ese horario ya está vigente.'))
        self.assertEqual(self.actions.get('desk-mode-00001')[1]['status'], 'rejected')
        original = self.p.status
        def foreign():
            value = original(); value['tasks'] = {'1': dict(group='ajeno', status={'Running': {}})}
            return value
        with patch.object(self.p, 'status', side_effect=foreign):
            code, answer = self.actions.post(self.request(ident='desk-mode-ajeno1'))
        self.assertEqual((code, (answer.get('error') or {}).get('code')), (409, 'foreign_work'))
        self.assertEqual(self.p.calls, [])

    def test_lost_reply_is_unknown_then_read_by_status_without_resend(self):
        r = self.request()
        def lose(*a, **k):
            self.execute(*a, **k); raise OSError('lost reply')
        self.actions.executor = lose
        self.assertEqual(self.actions.post(r)[1]['status'], 'unknown')
        self.actions.executor = self.execute
        answer = self.actions.get(r['action_id'])[1]
        self.assertEqual((answer['status'], answer['result']['mode']), ('applied', 'noche'))
        self.assertEqual([c['op'] for c in self.calls], ['mode', 'status'])
        self.assertEqual(len(self.p.calls), 3)

    def test_ensayo_is_separate_and_leaves_authority_untouched(self):
        before = self.engine.path.read_bytes()
        code, answer = self.actions.post(self.request(dry_run=True))
        self.assertEqual((code, answer['status']), (200, 'dry_run'))
        self.assertEqual(self.engine.path.read_bytes(), before)
        self.assertEqual(self.p.calls, [])
        self.assertEqual(self.actions.post(self.request())[1]['status'], 'applied')

    def test_control_view_rejects_forged_notes_and_tables(self):
        v = self.engine.view(); safe = accion.host_channel.control(v, 'desk')
        self.assertEqual(safe['tables'], dict(dia=control.mode_table('dia'), noche=control.mode_table('noche')))
        self.assertEqual(safe['notes'], dict(mode='Automático · cambia a noche a las 23:00', detail=None))
        forged = [dict(mode='Automático · cambia a día a las 08:00', detail=None),  # wrong mode for 'dia'
                  dict(mode='Automático · cambia a noche a las 23:00 <script>', detail=None),
                  dict(mode='Automático · cambia a noche a las 25:00', detail=None),
                  dict(mode='Automático · cambia a noche a las 23:00', detail='token=private-secret'),
                  dict(mode='Automático · cambia a noche a las 23:00'), None, 'texto']
        for notes in forged:
            with self.subTest(notes=notes), self.assertRaises(ValueError):
                accion.host_channel.control(dict(v, notes=notes), 'desk')
        for tables in (None, dict(dia=control.mode_table('dia')), dict(dia=NOCHE, noche=NOCHE),
                       dict(dia=control.mode_table('dia'), noche=dict(NOCHE, ajeno=1))):
            with self.subTest(tables=tables), self.assertRaises(ValueError):
                accion.host_channel.control(dict(v, tables=tables), 'desk')
        r = self.request()
        wire = {k: v for k, v in r.items() if k != 'host'}
        wire['schema'] = 'rack-host-control@1'
        raw = self.engine.handle(wire)
        self.assertEqual(accion.host_channel.sanitize(r, raw, 'mode')['result']['mode'], 'noche')
        for field, value in (('mode', 'fijo'), ('cap', 14), ('notes', dict(mode='x', detail=None))):
            with self.subTest(field=field), self.assertRaises(ValueError):
                accion.host_channel.sanitize(r, dict(raw, result=dict(raw['result'], **{field: value})), 'mode')

    def ui_fixture(self):
        return dict(now=self.now, tickets={}, hosts=[dict(id='desk', name='Felix DESKTOP · WSL', fresh=True, age=0, estimates={},
            actions=dict(reason=None), snapshot=dict(control=accion.host_channel.control(self.engine.view(), 'desk'),
            groups=[dict(name=g, slots=n, status='Running') for g, n in control.desk_profile(self.now).items()],
            tasks=[], cpu=dict(cores=12, load=[1, 1, 1]), mem=dict(total=32*1024**3, available=24*1024**3, swap_total=0, swap_used=0)))])

    def test_ui_mode_control_states_synthetic(self):
        source = (BASE/'index.html').read_text().split('<script>', 1)[1].split('</script>', 1)[0]
        result = subprocess.run(['node', str(BASE/'tests/slots-ui-check.js')],
                                input=json.dumps(dict(js=source, fixture=self.ui_fixture(), scenario='mode')),
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('UI mode OK', result.stdout)

    def test_undo_conflict_message_through_channel(self):
        r = self.request(); self.actions.post(r)
        self.engine.handle(dict(schema='rack-host-control@1', op='slots', action_id='otro-ajuste-01', dry_run=False,
                                expected=self.request()['expected'], change=dict(group='jale', delta=-1)))
        self.refresh()
        code, answer = self.actions.post(self.undo(r['action_id']))
        self.assertEqual((code, answer['status'], (answer.get('error') or {}).get('code')), (409, 'conflict', 'undo_conflict'))
        self.assertEqual((answer.get('error') or {}).get('message'), 'El horario o los lugares cambiaron; no se puede deshacer este ajuste')

if __name__ == '__main__': unittest.main()
