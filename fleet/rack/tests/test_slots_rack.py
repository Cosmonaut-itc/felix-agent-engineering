"""Lugares por grupo (U3.1) en el panel: contrato, diario, transporte y UI.

El control del host corre en proceso contra un Pueue falso; ningún binario del host.
"""
import safety
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_rack import module, sample

serve, snapshot = module('serve'), module('snapshot')
accion = serve.accion
BASE = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('u31_control', BASE.parent / 'pueue-desk/control.py')
control = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(control)

HOST_CONTROL = '~/.local/lib/pao-pueue-mini/control.py'
MINI = dict(id='mini', name='Mac Mini', transport='local', argv=['false'],
            actions=dict(transport='local', reason=None, host_control=HOST_CONTROL,
                         controllers={'remote-run': accion.CONTROLLERS['remote-run']}))
DESK = dict(id='desk', name='Felix DESKTOP', transport='ssh', argv=['false'],
            actions=dict(transport='ssh', reason=None, argv=accion.DESK_ARGV, controllers=dict(accion.CONTROLLERS)))
PROFILE = dict(control.MINI_PROFILE)


class Pueue:
    def __init__(self):
        self.groups = {g: {'status': 'Running', 'parallel_tasks': n} for g, n in PROFILE.items()}
        self.log = []

    def status(self):
        self.log.append(['status'])
        return {'groups': copy.deepcopy(self.groups), 'tasks': {}}

    def call(self, *args):
        self.log.append([str(a) for a in args])
        assert args[0] == 'parallel', args
        self.groups[args[3]]['parallel_tasks'] = int(args[1])

    def parallel(self, group, value):
        self.call('parallel', control.validate_limit(value), '-g', group)

    def vector(self):
        return {g: self.groups[g]['parallel_tasks'] for g in PROFILE}

    def parallels(self):
        return [c for c in self.log if c[0] == 'parallel']


class Bridge:
    """Executor del panel que entrega el cuerpo a control.rack, como haría stdin."""
    def __init__(self, test):
        self.test, self.wires, self.fail, self.lose, self.on_call = test, [], False, False, None

    def host(self, host, request, timeout):
        self.wires.append(copy.deepcopy(request))
        if self.on_call:
            self.on_call(request)
        if self.fail:
            self.fail = False
            raise ValueError('transporte caído antes del host')
        answer = control.rack(json.dumps(request), self.test.pueue, self.test.state)
        if self.lose:
            self.lose = False
            raise ValueError('respuesta perdida después del efecto')
        return answer

    def __call__(self, *args, **kwargs):
        raise AssertionError('Cancelar no se usa aquí')


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'host-state'
        self.state.mkdir()
        self.pueue = Pueue()
        control.adopt(self.state, self.pueue.status())
        (self.state / 'control.lock').touch()
        self.pueue.log.clear()
        guard = patch.object(subprocess, 'Popen', side_effect=AssertionError('Sin transporte real'))
        guard.start()
        self.addCleanup(guard.stop)
        self.now, self.age, self.serial = 1000, 0, 0
        self.bridge = Bridge(self)
        self.hosts = [MINI, DESK]
        self.actions = accion.Actions(self.hosts, self.root / 'panel', self.read, executor=self.bridge,
                                      clock=lambda: self.now)

    def view(self):
        return accion.host_view(control.capabilities(self.pueue, self.state))

    def read(self):
        rows = []
        for host in self.hosts:
            snap = dict(sample(), host=host['id'], groups=[dict(name=g, status='Running', slots=n)
                                                           for g, n in self.pueue.vector().items()])
            if host['id'] == 'mini' and self.snapshot_control is not False:
                snap['control'] = self.view() if self.snapshot_control is None else self.snapshot_control
            rows.append(dict(id=host['id'], name=host['name'], fresh=self.age <= 45, age=self.age, snapshot=snap))
        return dict(schema='rack@1', now=self.now, tickets={}, hosts=rows)

    snapshot_control = None

    def request(self, op='slots', host='mini', group='suites', delta=-1, dry=False, action_id=None, **extra):
        if action_id is None:
            self.serial += 1
            action_id = f'panel-{self.serial:04d}'
        view = self.view()
        value = dict(schema='rack-action@1', op=op, host=host, action_id=action_id, dry_run=dry,
                     expected=dict(revision=view['revision'], policy_hash=view['policy_hash'], slots=view['desired']))
        if op == 'slots':
            value['change'] = dict(group=group, delta=delta)
        value.update(extra)
        return value

    def journal(self):
        with sqlite3.connect(str(self.root / 'panel/actions.sqlite3')) as db:
            return {k: json.loads(v) for k, v in db.execute('SELECT action_id,response FROM actions')}


class ContractTests(Fixture):
    def test_closed_public_schema(self):
        good = self.request()
        accion.validate(copy.deepcopy(good))
        accion.validate(self.request('undo', undo_of='panel-9999'))
        bad = []
        for change in (dict(delta=True), dict(delta=0), dict(delta=2), dict(group='ajeno'), dict(extra=1)):
            value = copy.deepcopy(good)
            value['change'].update(change)
            bad.append(value)
        for edit in (lambda v: v.update(dry_run=0), lambda v: v.update(extra=1), lambda v: v.pop('change'),
                     lambda v: v['expected']['slots'].update(suites=True),
                     lambda v: v['expected'].update(policy_hash='x' * 64),
                     lambda v: v['expected'].update(revision=True),
                     lambda v: v.update(launcher='remote-run'), lambda v: v.update(op='mode')):
            value = copy.deepcopy(good)
            edit(value)
            bad.append(value)
        bad.append(self.request('undo', undo_of='panel-0001', action_id='panel-0001'))
        for value in bad:
            with self.assertRaises(ValueError, msg=value):
                accion.validate(value)

    def test_host_control_config_is_fixed(self):
        hosts = serve.load_hosts(BASE / 'hosts.json')
        mini, desk = (next(h for h in hosts if h['id'] == i) for i in ('mini', 'desk'))
        self.assertEqual(mini['actions']['host_control'], HOST_CONTROL)
        self.assertNotIn('host_control', desk['actions'] or {})
        self.assertIsNone(accion.host_reason(mini))
        self.assertEqual(accion.host_reason(desk), accion.HOST_ERRORS['unsupported'])
        for actions in (dict(MINI['actions'], host_control='~/.local/bin/otro.py'),
                        dict(DESK['actions'], host_control=HOST_CONTROL)):
            with self.assertRaises(ValueError):
                accion.action_config(actions)

    def test_host_view_and_sanitize_whitelist(self):
        raw = control.capabilities(self.pueue, self.state)
        self.assertEqual(accion.host_view(raw), dict(slots=True, undo=True, revision=0, policy_hash=control.policy_hash(),
            cap=7, min=1, max=8, desired=PROFILE, observed=PROFILE, pending=None, reason=None))
        for edit in (lambda c: c['control'].update(cap=0), lambda c: c['control'].update(min=0),
                     lambda c: c['control'].update(reason='/Users/x secreto'),
                     lambda c: c['control']['desired'].update(suites=True),
                     lambda c: c['capabilities'].update(slots='sí'), lambda c: c.update(status='rejected')):
            value = copy.deepcopy(raw)
            edit(value)
            self.assertIsNone(accion.host_view(value))
        drift = copy.deepcopy(raw)
        drift['control']['reason'] = 'drift'
        self.assertEqual(accion.host_view(drift)['reason'], accion.HOST_ERRORS['drift'])
        self.assertFalse(accion.host_view(drift)['slots'])
        request = self.request()
        answer = control.reply(request, 'conflict', 'stale_state', 3, dict(before=PROFILE, desired=PROFILE, observed=PROFILE))
        answer['error']['message'] = '/Users/secreto'
        clean = accion.sanitize_host(request, answer)
        self.assertEqual(clean['error'], dict(code='stale_state', message=accion.HOST_ERRORS['stale_state']))
        self.assertEqual(clean['host'], 'mini')
        for edit in (lambda a: a.update(status='hecho'), lambda a: a['result'].update(ruta='/x'),
                     lambda a: a['error'].update(code='not_found'), lambda a: a.update(action_id='otro-0001'),
                     lambda a: a['result'].update(desired={'suites': -1})):
            value = copy.deepcopy(answer)
            edit(value)
            with self.assertRaises(ValueError):
                accion.sanitize_host(request, value)

    def test_snapshot_passes_only_the_view(self):
        raw = control.capabilities(self.pueue, self.state)
        data = dict(sample(), control=raw)
        self.assertEqual(serve.validate_snapshot(data, 'mini')['control'], accion.host_view(raw))
        self.assertIsNone(serve.validate_snapshot(dict(sample(), control={'ruta': '/x'}), 'mini')['control'])
        self.assertNotIn('control', serve.validate_snapshot(sample(), 'mini'))


class FlowTests(Fixture):
    def test_dry_run_has_no_journal_row_and_no_effect(self):
        code, answer = self.actions.post(self.request(dry=True))
        self.assertEqual((code, answer['status']), (200, 'dry_run'))
        self.assertEqual(answer['result']['plan'], dict(decreases=['suites'], increases=[]))
        self.assertEqual(self.journal(), {})
        self.assertEqual(self.bridge.wires[0]['dry_run'], True)
        self.assertEqual((self.pueue.parallels(), control.capabilities(self.pueue, self.state)['control']['revision']), ([], 0))

    def test_intent_commits_before_transport_and_wire_is_exact(self):
        seen = []
        self.bridge.on_call = lambda wire: seen.append(self.journal().get(wire['action_id']))
        request = self.request()
        code, answer = self.actions.post(copy.deepcopy(request))
        self.assertEqual(seen, [accion.host_result(request, 'unknown')])
        self.assertEqual(self.bridge.wires, [dict(schema='rack-host-control@1', op='slots', action_id=request['action_id'],
                                                  dry_run=False, expected=request['expected'], change=request['change'])])
        self.assertEqual((code, answer['status'], answer['revision']), (200, 'applied', 1))
        self.assertEqual(set(answer), {'schema', 'action_id', 'op', 'host', 'status', 'revision', 'result', 'error'})
        self.assertEqual(self.pueue.vector(), dict(PROFILE, suites=2))
        # Mismo ID y cuerpo: el recibo guardado, sin transporte. Otro cuerpo: conflicto.
        self.assertEqual(self.actions.post(copy.deepcopy(request)), (200, answer))
        other = dict(request, change=dict(group='rapidas', delta=-1))
        self.assertEqual(self.actions.post(other)[0], 409)
        self.assertEqual(len(self.bridge.wires), 1)
        # Deshacer con la revisión nueva.
        undo = self.request('undo', undo_of=request['action_id'])
        code, back = self.actions.post(undo)
        self.assertEqual((code, back['status'], back['result']['undo_of']), (200, 'applied', request['action_id']))
        self.assertEqual(self.pueue.vector(), PROFILE)

    def test_rejections_bind_the_id_before_any_transport(self):
        old = self.request()
        old['expected']['revision'] = 5
        for name, age, request, http, status, code in (
                ('instantánea vieja', 60, self.request(), 409, 'conflict', 'stale_state'),
                ('revisión vieja', 0, old, 409, 'conflict', 'stale_state'),
                ('desk', 0, self.request(host='desk'), 409, 'rejected', 'unsupported')):
            self.age = age
            answer = self.actions.post(request)
            self.assertEqual((answer[0], answer[1]['status'], (answer[1]['error'] or {}).get('code')), (http, status, code), name)
            self.assertIn(request['action_id'], self.journal())
        self.snapshot_control = False
        answer = self.actions.post(self.request())
        self.assertEqual((answer[0], (answer[1]['error'] or {}).get('code')), (409, 'unsupported'))
        self.snapshot_control = None
        (self.root / 'panel/actions.disabled').touch()
        self.assertEqual(self.actions.post(self.request())[0], 403)
        (self.root / 'panel/actions.disabled').unlink()
        self.assertEqual(self.bridge.wires, [])
        # Un cambio abierto en el diario bloquea el siguiente del mismo host.
        self.bridge.fail = True
        self.assertEqual(self.actions.post(self.request())[1]['status'], 'unknown')
        answer = self.actions.post(self.request(group='rapidas'))
        self.assertEqual((answer[0], (answer[1]['error'] or {}).get('code')), (409, 'busy'))

    def test_unknown_get_resends_same_body_once_and_applies_once(self):
        request = self.request()
        self.bridge.fail = True
        code, answer = self.actions.post(copy.deepcopy(request))
        self.assertEqual((code, answer['status']), (200, 'unknown'))
        code, answer = self.actions.get(request['action_id'])
        self.assertEqual((code, answer['status']), (200, 'applied'))
        self.assertEqual([w['op'] for w in self.bridge.wires], ['slots', 'status', 'slots'])
        self.assertEqual(self.bridge.wires[0], self.bridge.wires[2])
        self.assertEqual(self.pueue.parallels(), [['parallel', '2', '-g', 'suites']])
        self.assertEqual(self.actions.get(request['action_id']), (200, answer))
        self.assertEqual(len(self.bridge.wires), 3)
        # Efecto hecho y respuesta perdida: el estado lo encuentra; no hay reenvío ni segundo efecto.
        lost = self.request(group='rapidas')
        self.bridge.lose = True
        self.assertEqual(self.actions.post(copy.deepcopy(lost))[1]['status'], 'unknown')
        code, answer = self.actions.get(lost['action_id'])
        self.assertEqual((code, answer['status']), (200, 'applied'))
        self.assertEqual([w['op'] for w in self.bridge.wires[3:]], ['slots', 'status'])
        self.assertEqual(self.pueue.parallels()[1:], [['parallel', '2', '-g', 'rapidas']])
        print('\nRACK unknown: transporte caído -> GET status not_found -> reenvío idéntico -> applied (1 efecto); '
              'respuesta perdida -> GET status applied (sin reenvío)')


class TransportTests(unittest.TestCase):
    STUB = ('import json, os, sys\n'
            'from pathlib import Path\n'
            'Path(os.environ["HOME"], "llamada.json").write_text(json.dumps(dict(argv=sys.argv[1:], stdin=sys.stdin.read(),\n'
            '    env=sorted(os.environ))))\n'
            'print(json.dumps(dict(schema="rack-host-result@1", status="ok", capabilities={}, control={})))\n')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.path = self.home / '.local/lib/pao-pueue-mini/control.py'
        self.path.parent.mkdir(parents=True)
        self.path.write_text(self.STUB)

    def test_executor_host_fixed_argv_and_stdin(self):
        wire = dict(schema='rack-host-control@1', op='status', action_id='panel-0001')
        answer = accion.Executor(home=self.home).host(MINI, wire, timeout=5)
        self.assertEqual(answer['schema'], 'rack-host-result@1')
        call = json.loads((self.home / 'llamada.json').read_text())
        self.assertEqual(call['argv'], ['rack', '--host', 'mini'])
        self.assertEqual(json.loads(call['stdin']), wire)
        self.assertNotIn('SSH_AUTH_SOCK', call['env'])
        for host in (DESK, dict(MINI, actions=dict(MINI['actions'], host_control=None))):
            with patch.object(subprocess, 'Popen', side_effect=AssertionError('no debe lanzar')):
                with self.assertRaises(ValueError):
                    accion.Executor(home=self.home).host(host, wire, timeout=5)

    def test_snapshot_reads_capabilities_only_on_macos_with_control(self):
        self.assertEqual(snapshot.host_control(self.home, 'macos')['schema'], 'rack-host-result@1')
        call = json.loads((self.home / 'llamada.json').read_text())
        self.assertEqual((call['argv'], json.loads(call['stdin'])),
                         (['rack', '--host', 'mini'], dict(schema='rack-host-control@1', op='capabilities')))
        self.assertIsNone(snapshot.host_control(self.home, 'linux'))
        self.assertIsNone(snapshot.host_control(self.home / 'otro', 'macos'))

    def test_real_controller_without_adoption_never_reaches_pueue(self):
        # El control.py real en un HOME temporal sin instalar: responde not_adopted sin leer Pueue.
        shutil.copy(BASE.parent / 'pueue-desk/control.py', self.path)
        view = accion.host_view(snapshot.host_control(self.home, 'macos'))
        self.assertEqual((view['slots'], view['reason']), (False, accion.HOST_ERRORS['not_adopted']))
        wire = dict(schema='rack-host-control@1', op='slots', action_id='panel-0001', dry_run=True,
                    expected=dict(revision=0, policy_hash='a' * 64, slots=PROFILE), change=dict(group='suites', delta=-1))
        answer = accion.Executor(home=self.home).host(MINI, wire, timeout=5)
        self.assertEqual((answer['status'], answer['error']), ('rejected', {'code': 'not_adopted'}))
        self.assertFalse((self.home / '.local/state').exists())


class UITests(unittest.TestCase):
    def test_slots_ui_states(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node no disponible')
        js = (BASE / 'index.html').read_text().split('<script>', 1)[1].split('</script>', 1)[0]
        from test_actions import payload
        view = dict(slots=True, undo=True, revision=4, policy_hash='a' * 64, cap=7, min=1, max=8,
                    desired=PROFILE, observed=PROFILE, pending=None, reason=None)
        result = subprocess.run([node, str(BASE / 'tests/ui-slots-check.js')],
                                input=json.dumps(dict(js=js, fixture=payload(), control=view)),
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('UI SLOTS OK', result.stdout)
        print('\nUI LUGARES:\n' + result.stdout)


if __name__ == '__main__':
    unittest.main()
