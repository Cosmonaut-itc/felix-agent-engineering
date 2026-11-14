"""U3.2: lugares del desk por el canal forzado de C4/C5; sólo dobles en /tmp."""
import safety
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from test_rack import module, BASE
from test_actions import accion, serve
from test_c4 import SSH, FakeProcess

desk = module('desk_action')
spec = importlib.util.spec_from_file_location('desk_slots_control', BASE.parent / 'pueue-desk/control.py')
control = importlib.util.module_from_spec(spec); spec.loader.exec_module(control)


def code_of(answer):
    # A missing error is an assertion failure, not a crash of the test.
    return (answer.get('error') or {}).get('code')


FAKE_PUEUE = '''import json
from pathlib import Path

class FilePueue:
    """Pueue en un JSON de /tmp; nunca el binario real."""
    def __init__(self, path):
        self.path, self.timeout = Path(path), None
    def status(self):
        return json.loads(self.path.read_text())
    def save(self, state, call):
        self.path.write_text(json.dumps(state))
        with self.path.with_suffix('.calls').open('a') as stream:
            stream.write(json.dumps(call) + '\\n')
    def parallel(self, group, number):
        state = self.status(); state['groups'][group]['parallel_tasks'] = number
        self.save(state, ['parallel', group, number])
    def call(self, *args):
        assert args[0] in ('pause', 'start'), args
        state = self.status(); state['groups'][args[-1]]['status'] = 'Paused' if args[0] == 'pause' else 'Running'
        self.save(state, list(args))
'''

WRAPPER = '''import importlib.util,json,sys
from pathlib import Path
root=Path(__file__).resolve().parent
def load(name, path):
    spec=importlib.util.spec_from_file_location(name, path); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
c=load('control', {control!r}); fake=load('fake', root/'fakepueue.py')
Path(root/'seen.json').write_text(json.dumps(dict(argv=sys.argv, isolated=sys.flags.isolated)))
c.Pueue=lambda *a: fake.FilePueue(root/'pueue.json')
c.desk_generation=lambda *a, **k: 'daemon-1'
c.main()
'''


def shim(root, host_control):
    path = root / 'fake_ssh.py'
    path.write_text("import importlib.util,json,sys\nfrom pathlib import Path\n"
        + "guard=importlib.util.spec_from_file_location('safety'," + repr(str(BASE / 'tests/safety.py')) + ")\n"
        + "safety=importlib.util.module_from_spec(guard);guard.loader.exec_module(safety)\n"
        + "spec=importlib.util.spec_from_file_location('desk'," + repr(str(BASE / 'desk_action.py')) + ")\n"
        + "desk=importlib.util.module_from_spec(spec);spec.loader.exec_module(desk)\n"
        + "desk.HOME=Path(" + repr(str(root / 'home')) + ");desk.SWITCH=desk.HOME/'.local/state/pao/rack/actions.disabled'\n"
        + "desk.HOST_CONTROL=" + repr(str(host_control)) + "\n"
        + "answer,code=desk.handle(desk.read_request(sys.stdin.buffer))\nprint(json.dumps(answer));sys.exit(code)\n")
    return path


def table():
    return {g: row[control.event_mode(control.last_event(time.time()))] for g, row in control.GROUP_LIMITS.items()}


class DeskChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='rack-u32-opus-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'fakepueue.py').write_text(FAKE_PUEUE)
        self.controller = self.root / 'control.py'
        self.controller.write_text(WRAPPER.format(control=str(BASE.parent / 'pueue-desk/control.py')))
        groups = {g: dict(status='Running', parallel_tasks=n) for g, n in table().items()}
        groups['default'] = dict(status='Running', parallel_tasks=1)
        (self.root / 'pueue.json').write_text(json.dumps(dict(groups=groups, tasks={})))
        fake = importlib.util.spec_from_file_location('fakepueue', self.root / 'fakepueue.py')
        self.fake = importlib.util.module_from_spec(fake); fake.loader.exec_module(self.fake)
        self.state = self.root / 'home/.local/state/pao/pueue-desk'
        self.engine = control.DeskControl(self.fake.FilePueue(self.root / 'pueue.json'), self.state,
                                          generation=lambda: 'daemon-1')
        with control.locked(self.state):
            self.engine.adopt()
        self.hosts = serve.load_hosts(BASE / 'hosts.json')
        self.desk = next(h for h in self.hosts if h['id'] == 'desk')
        self.payload = dict(now=1000, hosts=[dict(id='desk', fresh=True, age=0, snapshot={})])
        self.ssh = shim(self.root, self.controller)
        self.processes, self.argv = [], []
        def runner(argv, **options):
            self.argv.append(argv)
            process = subprocess.Popen(['/usr/bin/python3', '-I', str(self.ssh)], **options)
            self.processes.append(process)
            return process
        self.actions = accion.Actions(self.hosts, self.root / 'rack', lambda: self.payload,
                                      accion.Executor(home=self.root, runner=runner), clock=lambda: 1000)
        self.addCleanup(self.reaped)

    def reaped(self):
        for process in self.processes:
            self.assertIsNotNone(process.poll())

    def calls(self):
        target = self.root / 'pueue.calls'
        return [json.loads(line) for line in target.read_text().splitlines()] if target.exists() else []

    def request(self, ident='desk-slots-0001', group='jale', delta=-1, dry_run=False, control_view=None):
        view = control_view or self.actions.post(dict(schema=accion.SCHEMA, op='host_capabilities', host='desk'))[1]
        return dict(schema=accion.SCHEMA, op='slots', host='desk', action_id=ident, dry_run=dry_run,
                    expected={k: view[k] for k in ('revision', 'generation', 'policy_hash', 'mode', 'slots')},
                    change=dict(group=group, delta=delta))

    def test_end_to_end_slots_undo_and_receipt_over_the_forced_channel(self):
        code, caps = self.actions.post(dict(schema=accion.SCHEMA, op='host_capabilities', host='desk'))
        self.assertEqual(code, 200)
        self.assertEqual(caps['capabilities'], dict(slots=True, undo=True, dry_run=True, mode=False, reorder=False,
                                                    restart_queue=False))
        self.assertEqual((caps['slots'], caps['cap']), (table(), sum(table().values())))
        self.assertIsNone(caps['expires_at'])
        request = self.request(control_view=caps)
        trial = self.actions.post(dict(request, action_id='desk-trial-0001', dry_run=True))[1]
        self.assertEqual((trial['status'], self.calls()), ('dry_run', []))
        code, answer = self.actions.post(request)
        self.assertEqual((code, answer['status']), (200, 'applied'))
        expires = control.next_event(control.last_event(time.time()))
        self.assertEqual(answer['result']['expires_at'], expires)
        self.assertEqual(self.calls(), [['parallel', 'jale', table()['jale'] - 1]])
        self.assertEqual(self.actions.get(request['action_id'])[1], answer)
        self.assertEqual(self.actions.post(request)[1], answer)
        self.assertEqual(len(self.calls()), 1)
        view = self.actions.post(dict(schema=accion.SCHEMA, op='host_capabilities', host='desk'))[1]
        self.assertEqual(view['expires_at'], expires)
        undo = self.request('desk-undo-00001', control_view=view)
        del undo['change']; undo.update(op='undo', undo_of=request['action_id'])
        self.assertEqual(self.actions.post(undo)[1]['status'], 'applied')
        self.assertEqual(self.calls()[-1], ['parallel', 'jale', table()['jale']])
        self.assertTrue(all(argv == SSH for argv in self.argv))
        seen = json.loads((self.root / 'seen.json').read_text())
        self.assertEqual((seen['argv'][1:], seen['isolated']), (['rack', '--host', 'desk'], 1))

    def test_cap_and_switch_are_decided_on_the_desk(self):
        code, answer = self.actions.post(self.request(delta=1))
        self.assertEqual((code, answer['status'], code_of(answer)), (409, 'rejected', 'invalid_request'))
        self.assertIn('14 de día y 18 de noche', answer['error']['message'])
        switch = self.root / 'home/.local/state/pao/rack/actions.disabled'
        switch.parent.mkdir(parents=True); switch.touch()
        code, answer = self.actions.post(self.request('desk-slots-0002'))
        self.assertEqual((code, code_of(answer)), (403, 'disabled'))
        self.assertIn('en el desk', answer['error']['message'])
        self.assertEqual(self.calls(), [])

    def test_old_desk_without_controller_answers_unsupported_without_effects(self):
        self.controller.unlink()
        _, caps = self.actions.post(dict(schema=accion.SCHEMA, op='host_capabilities', host='desk'))
        self.assertFalse(caps['capabilities']['slots'])
        view = dict(revision=1, generation='daemon-1', policy_hash=control.DESK_POLICY_HASH, mode='dia', slots=table())
        code, answer = self.actions.post(self.request(control_view=view))
        self.assertEqual((code, answer['status'], code_of(answer)), (409, 'unsupported', 'unsupported'))
        self.assertEqual(self.calls(), [])

    def test_envelope_names_only_the_target_and_keeps_the_ssh_window(self):
        calls = []
        answer = dict(schema='rack-host-result@1', status='unknown', action_id='desk-slots-0001')
        def runner(argv, **options):
            calls.append((argv, options)); return FakeProcess(answer, 0, calls)
        wire = dict(schema='rack-host-control@1', op='status', action_id='desk-slots-0001')
        response = accion.Executor(runner=runner, home='/synthetic-home')(self.desk, 'host-control', wire, timeout=100)
        self.assertEqual(response['status'], 'unknown')
        self.assertEqual(calls[0][0], SSH)
        self.assertEqual(calls[1][0], dict(schema='rack-desk-action@1', target='host-control', request=wire))
        self.assertLessEqual(calls[1][1], accion.DESK_TIMEOUT); self.assertGreater(calls[1][1], 35)
        self.assertNotIn('desk-slots-0001', ' '.join(calls[0][0]))
        # Desk with a local channel, or the Mini over SSH: never this transport.
        for host in (dict(self.desk, transport='local'), dict(next(h for h in self.hosts if h['id'] == 'mini'), transport='ssh')):
            self.assertEqual(accion.Executor(runner=runner)(host, 'host-control', wire)['status'], 'unsupported')
        self.assertEqual(len(calls), 2)

    def test_aborted_receipt_is_terminal_over_get_and_unbound_rejection_stays_unknown(self):
        answers = []
        def executor(host, target, wire, timeout):
            answers.append(wire)
            if wire['op'] == 'slots':
                raise OSError('respuesta perdida')
            return dict(schema='rack-host-result@1', action_id=wire['action_id'], status='aborted',
                        error=dict(code='expired', message='x'), revision=3,
                        result=dict(before=table(), desired=table(), observed=table(), expires_at=None))
        self.actions.executor = executor
        view = dict(revision=1, generation='daemon-1', policy_hash=control.DESK_POLICY_HASH, mode='dia', slots=table())
        request = self.request(control_view=view)
        self.assertEqual(self.actions.post(request)[1]['status'], 'unknown')
        code, answer = self.actions.get(request['action_id'])
        self.assertEqual((code, answer['status'], code_of(answer)), (200, 'aborted', 'expired'))
        self.assertEqual(answer['error']['message'], 'Venció con el cambio de horario; rige la tabla del modo nuevo.')
        self.assertEqual(self.actions.get(request['action_id'])[1], answer)
        self.assertEqual([w['op'] for w in answers], ['slots', 'status'])


class DeskProfileTests(unittest.TestCase):
    def request(self, **changes):
        value = dict(schema='rack-action@1', op='slots', host='desk', action_id='desk-slots-0001', dry_run=False,
                     expected=dict(revision=1, policy_hash='a' * 64, generation='g', mode='dia', slots=table()),
                     change=dict(group='jale', delta=-1))
        value.update(changes)
        return value

    def view(self, **changes):
        value = dict(schema='rack-host-result@1', status='ok', capabilities=dict(slots=True, undo=True, dry_run=True,
                     mode=False, reorder=False, restart_queue=False), revision=1, policy_hash='a' * 64, generation='g',
                     mode='dia', cap=14, slots=table(), desired=table(), drift=False, recovery_pending=False,
                     pause_reasons={}, expires_at=None)
        value.update(changes)
        return value

    def test_closed_desk_vector_modes_and_groups(self):
        self.assertEqual(accion.validate(self.request()), self.request())
        mini = dict(suites=3, default=1, rapidas=3)
        for bad in (dict(change=dict(group='default', delta=-1)), dict(change=dict(group='suites', delta=-1)),
                    dict(expected=dict(self.request()['expected'], mode='fijo')),
                    dict(expected=dict(self.request()['expected'], slots=mini)),
                    dict(expected=dict(self.request()['expected'], slots=dict(table(), jale=True))),
                    dict(expected=dict(self.request()['expected'], slots=dict(table(), jale=9))),
                    dict(host='laptop')):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                accion.validate(self.request(**bad))
        with self.assertRaises(ValueError):
            accion.validate(dict(self.request(), host='mini'))

    def test_capabilities_view_bounds_cap_expiry_and_mode(self):
        channel = accion.host_channel
        self.assertEqual(channel.control(self.view(expires_at=1794718800), 'desk')['expires_at'], 1794718800)
        self.assertEqual(channel.control(self.view(mode='noche', cap=18), 'desk')['cap'], 18)
        for bad in (dict(mode='fijo'), dict(cap=5), dict(cap=49), dict(cap=True), dict(expires_at=float('inf')),
                    dict(expires_at='mañana'), dict(slots=dict(suites=3, default=1, rapidas=3))):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                channel.control(self.view(**bad), 'desk')
        with self.assertRaises(ValueError):
            channel.control(dict(self.view(slots=dict(suites=3, default=1, rapidas=3), desired=dict(suites=3, default=1,
                            rapidas=3), mode='fijo', cap=7), expires_at=1794718800), 'mini')
        off = channel.control(self.view(capabilities=dict(self.view()['capabilities'], slots=False)), 'desk')
        self.assertEqual(off['reason'], 'Los ajustes de lugares están apagados en el desk.')

    def test_desk_snapshot_validation_uses_the_desk_profile(self):
        snapshot = dict(schema='rack-host@1', host='desk', taken=1000, tasks=[], groups=[], cpu=None, mem=None,
                        disk=None, guard=None, errors=[], control=self.view(expires_at=1794718800))
        try:
            data = serve.validate_snapshot(snapshot, 'desk')
        except ValueError as error:
            self.fail('desk control rejected: ' + str(error))
        self.assertEqual((data['control']['cap'], data['control']['expires_at']), (14, 1794718800))
        with self.assertRaises(ValueError):
            serve.validate_snapshot(dict(snapshot, host='mini'), 'mini')

    def test_result_codes_bound_to_their_status(self):
        channel, request = accion.host_channel, self.request()
        base = dict(schema='rack-host-result@1', action_id=request['action_id'])
        payload = dict(before=table(), desired=table(), observed=table(), expires_at=1794718800)
        answer = channel.sanitize(request, dict(base, status='applied', result=payload, revision=2), 'slots')
        self.assertEqual(answer['result']['expires_at'], 1794718800)
        for status, code in (('applied', 'expired'), ('conflict', 'expired'), ('aborted', None),
                             ('rejected', 'undo_expired'), ('aborted', 'undo_expired')):
            with self.subTest(status=status, code=code), self.assertRaises(ValueError):
                channel.sanitize(request, dict(base, status=status, error=dict(code=code) if code else None), 'slots')
        self.assertEqual(channel.sanitize(request, dict(base, status='conflict', effects=False,
                         error=dict(code='undo_expired')), 'slots')['error']['message'],
                         'El ajuste venció con el cambio de horario. No se puede deshacer.')
        self.assertIn('14 de día y 18 de noche', channel.sanitize(request, dict(base, status='rejected', effects=False,
                      error=dict(code='invalid_request')), 'slots')['error']['message'])


class DeskDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='rack-u32-opus-desk-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.controller = self.root / 'control.py'
        for name, value in (('HOME', self.root), ('SWITCH', self.root / 'actions.disabled'),
                            ('HOST_CONTROL', str(self.controller))):
            mock = patch.object(desk, name, value); mock.start(); self.addCleanup(mock.stop)

    def envelope(self, **request):
        body = dict(schema='rack-host-control@1', op='slots', action_id='desk-slots-0001', dry_run=False)
        body.update(request)
        return json.dumps(dict(schema=desk.SCHEMA, target='host-control', request=body)).encode()

    def test_production_controller_path_is_fixed(self):
        self.assertEqual(module('desk_action').HOST_CONTROL, '/home/felix/.local/lib/pao-pueue-desk/control.py')

    def test_missing_controller_by_operation(self):
        for op, status in (('capabilities', 'unsupported'), ('slots', 'unsupported'), ('status', 'unknown')):
            body = self.envelope(op=op) if op != 'capabilities' else json.dumps(dict(
                schema=desk.SCHEMA, target='host-control', request=dict(schema='rack-host-control@1', op=op))).encode()
            answer, code = desk.handle(body)
            self.assertEqual((code, answer['status'], code_of(answer)), (0, status, 'unsupported'), op)

    def test_closed_envelope(self):
        good = json.loads(self.envelope())
        for bad in (dict(good, launcher='remote-run'), dict(good, target='other'), dict(good, extra=1),
                    dict(good, request=dict(good['request'], op='mode')),
                    dict(good, request=dict(good['request'], action_id='x')),
                    dict(good, request=dict(good['request'], schema='rack-cancel@1')),
                    dict(good, request=[1])):
            with self.subTest(bad=bad):
                answer, code = desk.handle(json.dumps(bad).encode())
                self.assertEqual((code, code_of(answer)), (2, 'invalid_request'))
        self.assertFalse(self.controller.exists())

    def test_controller_isolated_and_bad_answers_are_uncertain(self):
        self.controller.write_text('import json,os,sys\nfrom pathlib import Path\nr=json.load(sys.stdin)\n'
            'Path(__file__).with_suffix(".seen").write_text(json.dumps(dict(argv=sys.argv,isolated=sys.flags.isolated,'
            'cwd=os.getcwd(),request=r)))\n'
            'print(json.dumps(dict(schema="rack-host-result@1",status="applied",action_id=r["action_id"])))\n')
        answer, code = desk.handle(self.envelope())
        self.assertEqual((code, answer['status']), (0, 'applied'))
        seen = json.loads(self.controller.with_suffix('.seen').read_text())
        self.assertEqual((seen['argv'][1:], seen['isolated'], seen['cwd']), (['rack', '--host', 'desk'], 1, '/'))
        self.assertEqual(seen['request']['action_id'], 'desk-slots-0001')
        for source, expected in (('print(json.dumps(dict(schema="rack-host-result@1",status="applied",action_id="otro-00000001")))', 'unknown'),
                                 ('print("no es json")', 'unknown'),
                                 ('import sys\nsys.exit(3)', 'unknown'),
                                 ('import time\ntime.sleep(5)', 'unknown')):
            with self.subTest(source=source), patch.object(desk, 'HOST_TIMEOUT', 1):
                self.controller.write_text('import json,sys\nr=json.load(sys.stdin)\n' + source + '\n')
                answer, code = desk.handle(self.envelope())
                self.assertEqual((code, answer['status'], code_of(answer)), (0, expected, 'recovery_error'))
        self.controller.write_text('print("no es json")\n')
        answer, _ = desk.handle(json.dumps(dict(schema=desk.SCHEMA, target='host-control',
                                request=dict(schema='rack-host-control@1', op='capabilities'))).encode())
        self.assertEqual((answer['status'], answer['effects']), ('recovery_error', False))

    def test_cancellation_dispatch_is_unchanged(self):
        answer, code = desk.handle(json.dumps(dict(schema=desk.SCHEMA, launcher='otro', request={})).encode())
        self.assertEqual((code, code_of(answer)), (2, 'unsupported'))


class DeskUITests(unittest.TestCase):
    def test_production_ui_desk_slots(self):
        tmp = tempfile.TemporaryDirectory(prefix='rack-u32-opus-ui-', dir='/tmp'); self.addCleanup(tmp.cleanup)
        now = 1794668400  # 2026-11-14 15:00 UTC: día
        day = {g: row['dia'] for g, row in control.GROUP_LIMITS.items()}
        night = {g: row['noche'] for g, row in control.GROUP_LIMITS.items()}
        p = type('P', (), {})()
        groups = {g: dict(status='Running', parallel_tasks=n) for g, n in day.items()}
        p.status = lambda: dict(groups=copy.deepcopy(groups), tasks={})
        engine = control.DeskControl(p, Path(tmp.name), clock=lambda: now, generation=lambda: 'daemon-1')
        with control.locked(engine.directory): engine.adopt()
        view = accion.host_channel.control(engine.view(), 'desk')
        fixture = dict(now=now, tickets={}, hosts=[dict(id='desk', name='Felix DESKTOP', fresh=True, age=0, estimates={},
            actions=dict(reason=None), snapshot=dict(control=view,
            groups=[dict(name=g, slots=n, status='Running') for g, n in day.items()] + [dict(name='default', slots=1, status='Running')],
            tasks=[], cpu=dict(cores=16, load=[1, 1, 1]), mem=dict(total=32 * 1024**3, available=20 * 1024**3, swap_total=0, swap_used=0)))])
        source = (BASE / 'index.html').read_text().split('<script>', 1)[1].split('</script>', 1)[0]
        result = subprocess.run(['node', str(BASE / 'tests/slots-ui-check.js')], text=True, capture_output=True, timeout=10,
            input=json.dumps(dict(js=source, fixture=fixture, scenario='desk', expires_at=control.next_event(now),
                                  night=dict(mode='noche', cap=18, slots=night, desired=night))))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('UI slots desk OK', result.stdout)


if __name__ == '__main__':
    unittest.main()
