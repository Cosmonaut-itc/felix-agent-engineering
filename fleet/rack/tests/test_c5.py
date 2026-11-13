"""C5: the local channel adds origin "rack-panel" only for runs that declare it; /tmp fakes only."""
import safety
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import test_actions as T
from test_actions import accion, serve, REF, request, payload
from test_c4 import SSH, wire, envelope, desk
from test_rack import BASE

# Captured before any test patches subprocess.Popen (AuthorizationTests does).
REAL_POPEN = subprocess.Popen
JOB = 'job-AbCd1234'
CAPS = dict(queued=True, running=True, reason=None, cancel_origin=True)

# Synthetic controller: logs every request, answers capabilities from caps.json
# (or raw/sleep variants), and cancel/status from a fixed receipt.
CONTROLLER = r'''import fcntl,json,os,sys,time
from pathlib import Path
here=Path(__file__).parent
r=json.load(sys.stdin)
with open(here/'log.jsonl','a') as log: log.write(json.dumps(r)+'\n')
if r['op']=='capabilities':
    mode=json.loads((here/'probe.json').read_text()) if (here/'probe.json').exists() else {'caps':None}
    if mode.get('lock'):
        held=open(here/'rack-control.lock','w'); fcntl.flock(held,fcntl.LOCK_EX)
        (here/'probe.pid').write_text(str(os.getpid()))
    if mode.get('sleep'): time.sleep(mode['sleep'])
    if 'raw' in mode: sys.stdout.write(mode['raw']); sys.exit(mode.get('code',0))
    a=dict(schema=mode.get('schema','rack-cancel-result@1'),ref=mode.get('ref',r['ref']))
    a['capabilities']=mode['caps'] if mode['caps'] is not None else dict(queued=True,running=True,reason=None)
    print(json.dumps(a)); sys.exit(mode.get('code',0))
if r['op']=='cancel':
    lock=open(here/'rack-control.lock','w')
    try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB); (here/'cancel-lock.txt').write_text('free')
    except OSError: (here/'cancel-lock.txt').write_text('held')
    if (here/'cancel-sleep').exists(): time.sleep(float((here/'cancel-sleep').read_text()))
    if (here/'lost').exists(): sys.stdout.write('{broken'); sys.exit(0)
status=(here/'status.txt').read_text() if (here/'status.txt').exists() else 'cancelled'
print(json.dumps(dict(schema='rack-cancel-result@1',ref=r['ref'],action_id=r['action_id'],status=status,
    result=dict(exit=130) if status=='cancelled' else None,error=None)))
'''


def write_controller(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONTROLLER)
    return path


def seen(path):
    log = path.parent / 'log.jsonl'
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def set_probe(path, **mode):
    (path.parent / 'probe.json').write_text(json.dumps(mode))


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rack-c5-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.processes = []
        self.addCleanup(self.reap_all)

    def reap_all(self):
        for process in self.processes:
            if process.poll() is None:
                # Only the exact session created by this test.
                try: os.killpg(process.pid, 9)
                except ProcessLookupError: pass
                process.wait(3)
            for stream in (process.stdin, process.stdout):
                if stream and not stream.closed: stream.close()

    def runner(self, argv, **options):
        for value in argv[2:]:
            self.assertTrue(str(Path(value).resolve()).startswith(str(Path('/tmp').resolve()) + '/'), argv)
        # Every previous controller is already ended and reaped before a new launch.
        for process in self.processes:
            self.assertIsNotNone(process.returncode, 'previous controller not reaped before next launch')
            with self.assertRaises(ChildProcessError): os.waitpid(process.pid, os.WNOHANG)
        process = REAL_POPEN(argv, **options)
        self.processes.append(process)
        return process


class LocalChannelTests(Fixture):
    """Mini executor and desk dispatcher: probe then exactly one cancel with the enum."""
    def setUp(self):
        super().setUp()
        self.mini = write_controller(self.root / 'mini/.local/lib/remote-run/run-control.py')
        set_probe(self.mini, caps=CAPS)

    def test_mini_post_probes_then_one_cancel_with_origin(self):
        actions = accion.Actions(T.HOSTS, self.root / 'journal', payload,
            executor=accion.Executor(home=self.root / 'mini', runner=self.runner), clock=lambda: 1000)
        self.assertEqual(actions.post(request())[1]['status'], 'cancelled')
        log = seen(self.mini)
        self.assertEqual(log, [dict(schema='rack-cancel@1', op='capabilities', ref=REF),
                               dict(wire(), origin='rack-panel')])

    def test_desk_dispatcher_adds_origin_and_ssh_envelope_never_carries_it(self):
        controllers = {k: str(write_controller(self.root / 'desk' / k / Path(v).name))
                       for k, v in desk.CONTROLLERS.items()}
        for path in controllers.values(): set_probe(Path(path), caps=CAPS)
        shim = self.root / 'fake_ssh.py'
        shim.write_text(SHIM.format(base=str(BASE), root=str(self.root / 'desk'), controllers=repr(controllers),
                                    record=str(self.root / 'envelopes.jsonl')))
        def ssh(argv, **options):
            self.assertEqual(argv, SSH)
            return self.runner(['/usr/bin/python3', '-I', str(shim)], **options)
        hosts = serve.load_hosts(BASE / 'hosts.json')
        data = payload(); data['hosts'][0]['id'] = 'desk'
        jale = dict(data['hosts'][0]['snapshot']['tasks'][0], id=78)
        jale['control'] = dict(launcher='jale', ref=JOB, expected=dict(task_id=78, state='Queued'), reason=None)
        data['hosts'][0]['snapshot']['tasks'].append(jale)
        actions = accion.Actions(hosts, self.root / 'journal', lambda: data,
            executor=accion.Executor(home=self.root, runner=ssh), clock=lambda: 1000)
        for launcher, ref, task in (('remote-run', REF, 77), ('jale', JOB, 78)):
            with self.subTest(launcher=launcher):
                body = request(host='desk', launcher=launcher, ref=ref, action_id='c5-panel-' + launcher,
                               expected=dict(task_id=task, state='Queued'))
                self.assertEqual(actions.post(body)[1]['status'], 'cancelled')
                path = Path(controllers[launcher])
                self.assertEqual(seen(path), [dict(schema='rack-cancel@1', op='capabilities', ref=ref),
                    dict(schema='rack-cancel@1', op='cancel', ref=ref, action_id=body['action_id'],
                         expected=body['expected'], origin='rack-panel')])
        envelopes = [json.loads(line) for line in (self.root / 'envelopes.jsonl').read_text().splitlines()]
        self.assertEqual(len(envelopes), 2)
        for value in envelopes:
            # The SSH envelope is byte-for-byte the pre-C5 shape: no origin anywhere.
            self.assertNotIn('origin', json.dumps(value))
            self.assertEqual(set(value), {'schema', 'launcher', 'request'})
            self.assertEqual(set(value['request']), {'schema', 'op', 'ref', 'action_id', 'expected'})


# Fake SSH: records the exact stdin envelope, then runs the real dispatcher.
SHIM = '''import importlib.util,json,sys
from pathlib import Path
guard=importlib.util.spec_from_file_location('safety','{base}/tests/safety.py')
safety=importlib.util.module_from_spec(guard);guard.loader.exec_module(safety)
spec=importlib.util.spec_from_file_location('desk','{base}/desk_action.py')
desk=importlib.util.module_from_spec(spec);spec.loader.exec_module(desk)
desk.HOME=Path('{root}');desk.SWITCH=desk.HOME/'desk.disabled'
desk.CONTROLLERS={controllers}
raw=desk.read_request(sys.stdin.buffer)
with open('{record}','a') as f: f.write(bytes(raw).decode()+'\\n')
answer,code=desk.handle(raw)
print(json.dumps(answer));sys.exit(code)
'''


class DegradationTests(Fixture):
    """Old controllers, undeclared runs and malformed answers: previous body, cancel still sent."""
    def setUp(self):
        super().setUp()
        self.path = write_controller(self.root / 'controller/run-control.py')

    def send(self, timeout=12):
        try:
            return desk.send(['/usr/bin/python3', '-I', str(self.path)], wire(), self.runner,
                             dict(HOME=str(self.root), PATH='/usr/bin:/bin', TMPDIR='/tmp'), timeout)
        except Exception as error:
            self.fail('a failed probe blocked the cancel: ' + type(error).__name__)

    def test_only_exact_true_from_valid_answer_enables_origin(self):
        cases = {
            'old controller': dict(caps=dict(queued=True, running=True, reason=None)),
            'undeclared run': dict(caps=dict(queued=True, running=True, reason=None, cancel_origin=False)),
            'one': dict(caps=dict(CAPS, cancel_origin=1)),
            'string': dict(caps=dict(CAPS, cancel_origin='true')),
            'null': dict(caps=dict(CAPS, cancel_origin=None)),
            'queued not bool': dict(caps=dict(CAPS, queued=1)),
            'caps not object': dict(caps=[True]),
            'wrong schema': dict(caps=CAPS, schema='rack-cancel-result@2'),
            'wrong ref': dict(caps=CAPS, ref='20261112-184713-ffffff'),
            'garbage': dict(raw='PRIVATE not json'),
            'oversized': dict(raw='x' * 20000),
            'exit 2': dict(caps=CAPS, code=2),
            'crash': dict(raw='', code=1),
        }
        for name, mode in cases.items():
            with self.subTest(name=name):
                (self.path.parent / 'log.jsonl').unlink(missing_ok=True)
                set_probe(self.path, **mode)
                code, answer = self.send()
                self.assertEqual((code, answer['status']), (0, 'cancelled'))
                log = seen(self.path)
                self.assertEqual([r['op'] for r in log], ['capabilities', 'cancel'])
                self.assertEqual(log[1], wire())
        set_probe(self.path, caps=CAPS)
        (self.path.parent / 'log.jsonl').unlink()
        self.assertEqual(self.send()[1]['status'], 'cancelled')
        self.assertEqual(seen(self.path)[1], dict(wire(), origin='rack-panel'))

    def test_probe_timeout_still_cancels_with_previous_body(self):
        set_probe(self.path, caps=CAPS, sleep=30)
        started = time.monotonic()
        code, answer = self.send(timeout=10.6)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual((code, answer['status']), (0, 'cancelled'))
        self.assertEqual(seen(self.path)[1], wire())


class DispatcherScopeTests(Fixture):
    """The real desk parser stays closed; status/capabilities do one call, no probe."""
    def setUp(self):
        super().setUp()
        self.controllers = {k: str(write_controller(self.root / k / Path(v).name)) for k, v in desk.CONTROLLERS.items()}
        for path in self.controllers.values(): set_probe(Path(path), caps=CAPS)
        for name, value in [('HOME', self.root), ('SWITCH', self.root / 'actions.disabled'),
                            ('CONTROLLERS', self.controllers)]:
            mock = patch.object(desk, name, value); mock.start(); self.addCleanup(mock.stop)
        self.run_env = patch.object(desk, 'environment', return_value=dict(HOME=str(self.root), PATH='/usr/bin:/bin', TMPDIR='/tmp'))
        self.run_env.start(); self.addCleanup(self.run_env.stop)

    def handle(self, data):
        return desk.handle(json.dumps(data).encode(), runner=self.runner)

    def test_parser_rejects_origin_in_envelope_and_inner_request(self):
        for op in ('cancel', 'status', 'capabilities'):
            for data in (dict(envelope(op=op), request=dict(wire(op=op), origin='rack-panel')),
                         dict(envelope(op=op), origin='rack-panel')):
                with self.subTest(op=op, data=data):
                    answer, code = self.handle(data)
                    self.assertEqual((code, answer['error']['code']), (2, 'invalid_request'))
        self.assertEqual(self.processes, [])

    def test_status_and_capabilities_make_exactly_one_call(self):
        for launcher in self.controllers:
            for op in ('status', 'capabilities'):
                with self.subTest(launcher=launcher, op=op):
                    path = Path(self.controllers[launcher])
                    (path.parent / 'log.jsonl').unlink(missing_ok=True)
                    self.handle(envelope(launcher, op))
                    self.assertEqual(seen(path), [wire(launcher, op)])
                    # Even with a budget that would leave probe margin, send never probes them.
                    (path.parent / 'log.jsonl').unlink()
                    desk.send(['/usr/bin/python3', '-I', str(path)], wire(launcher, op), self.runner,
                              desk.environment(), 12)
                    self.assertEqual(seen(path), [wire(launcher, op)])

    def test_cancel_uses_twelve_inner_seconds_others_keep_ten(self):
        timeouts = []
        def fake(argv, request, runner, env, timeout):
            timeouts.append((request['op'], timeout))
            if request['op'] == 'capabilities':
                return 0, dict(schema='rack-cancel-result@1', ref=request['ref'], capabilities=CAPS)
            return 0, T.receipt(request)
        with patch.object(desk, 'exchange', side_effect=fake):
            for op in ('cancel', 'status', 'capabilities'):
                self.handle(envelope(op=op))
        self.assertEqual(timeouts[0], ('capabilities', 2))
        self.assertEqual(timeouts[1][0], 'cancel')
        # Instant probe: cancel gets its 10 s reserve plus the unused probe remainder.
        self.assertGreater(timeouts[1][1], 11.9)
        self.assertLessEqual(timeouts[1][1], 12)
        self.assertEqual(timeouts[2:], [('status', 10), ('capabilities', 10)])


class BudgetTests(Fixture):
    """One monotonic deadline: the probe never eats the cancel reserve."""
    def run_send(self, timeout, probe_cost):
        now = [100.0]; calls = []
        def fake(argv, request, runner, env, limit):
            calls.append((request['op'], limit, now[0]))
            if request['op'] == 'capabilities':
                now[0] += min(probe_cost, limit)
                if probe_cost >= limit: raise subprocess.TimeoutExpired(argv, limit)
                return 0, dict(schema='rack-cancel-result@1', ref=request['ref'], capabilities=CAPS)
            now[0] += 1
            return 0, T.receipt(request)
        with patch.object(desk, 'exchange', side_effect=fake), patch.object(desk.time, 'monotonic', lambda: now[0]):
            try: desk.send(['/tmp/fake'], wire(), None, {}, timeout)
            except subprocess.TimeoutExpired: calls.append(('timeout', None, now[0]))
        return calls

    def test_slow_probe_keeps_cancel_window_and_reduced_budget_skips_probe(self):
        self.assertEqual(self.run_send(12, 5), [('capabilities', 2, 100.0), ('cancel', 10, 102.0)])
        self.assertEqual(self.run_send(12, .5), [('capabilities', 2, 100.0), ('cancel', 11.5, 100.5)])
        self.assertEqual(self.run_send(10.5, 5), [('capabilities', .5, 100.0), ('cancel', 10, 100.5)])
        for timeout in (10, 7, 3):
            with self.subTest(timeout=timeout):
                self.assertEqual(self.run_send(timeout, 5), [('cancel', timeout, 100.0)])

    def test_no_late_effect_after_the_deadline(self):
        now = [0.0]; ops = []
        def stuck(argv, request, runner, env, limit):
            ops.append(request['op']); now[0] += 50
            raise subprocess.TimeoutExpired(argv, limit)
        with patch.object(desk, 'exchange', side_effect=stuck), patch.object(desk.time, 'monotonic', lambda: now[0]):
            with self.assertRaises(subprocess.TimeoutExpired):
                desk.send(['/tmp/fake'], wire(), None, {}, 12)
        self.assertEqual(ops, ['capabilities'])

    def test_probe_holding_stdout_and_lock_is_killed_and_reaped_before_cancel(self):
        path = write_controller(self.root / 'controller/run-control.py')
        set_probe(path, caps=CAPS, lock=True, sleep=60)
        started = time.monotonic()
        code, answer = desk.send(['/usr/bin/python3', '-I', str(path)], wire(), self.runner,
                                 dict(HOME=str(self.root), PATH='/usr/bin:/bin', TMPDIR='/tmp'), 12)
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual((code, answer['status']), (0, 'cancelled'))
        self.assertEqual(len(self.processes), 2)
        self.assertEqual(self.processes[0].returncode, -9)
        # runner() asserted the probe was reaped before cancel started; the lock was free.
        self.assertEqual((path.parent / 'cancel-lock.txt').read_text(), 'free')
        self.assertEqual(seen(path)[1], wire())


class JournalTests(Fixture):
    """HTTP POST, duplicate, lost answer and restart: one cancel, then only status."""
    def test_one_cancel_per_action_id_across_duplicate_lost_answer_restart_and_capability_change(self):
        case = T.AuthorizationTests(methodName='test_full_http_roundtrip_and_no_cors')
        case.setUp(); self.addCleanup(case.doCleanups)
        path = write_controller(case.root / '.local/lib/remote-run/run-control.py')
        set_probe(path, caps=CAPS)
        (path.parent / 'lost').touch()
        case.actions.executor = accion.Executor(home=case.root, runner=self.runner)
        code, answer = case.handler()
        self.assertEqual((code, answer['status']), (200, 'unknown'))
        (path.parent / 'lost').unlink()
        (path.parent / 'status.txt').write_text('cancelling')
        set_probe(path, caps=dict(CAPS, cancel_origin=False))
        self.assertEqual(case.handler()[1]['status'], 'unknown')
        self.assertEqual(case.handler(method='GET', path='/api/accion/action-0001')[1]['status'], 'cancelling')
        restarted = accion.Actions(T.HOSTS, case.root, payload,
            executor=accion.Executor(home=case.root, runner=self.runner), clock=lambda: 1000)
        self.assertEqual(restarted.post(request())[1]['status'], 'cancelling')
        (path.parent / 'status.txt').write_text('cancelled')
        self.assertEqual(restarted.get('action-0001')[1]['status'], 'cancelled')
        log = seen(path)
        self.assertEqual([r['op'] for r in log], ['capabilities', 'cancel', 'status', 'status'])
        self.assertEqual(log[1], dict(wire(), origin='rack-panel'))
        self.assertEqual([r for r in log if r['op'] == 'cancel'], [log[1]])


class CompatibilityTests(Fixture):
    """Compatibility is per path: the desk channel decides, not the panel server."""
    def setUp(self):
        super().setUp()
        self.controllers = {k: str(write_controller(self.root / 'desk' / k / Path(v).name))
                            for k, v in desk.CONTROLLERS.items()}
        for path in self.controllers.values(): set_probe(Path(path), caps=CAPS)
        self.shim = self.root / 'fake_ssh.py'
        self.shim.write_text(SHIM.format(base=str(BASE), root=str(self.root / 'desk'),
            controllers=repr(self.controllers), record=str(self.root / 'envelopes.jsonl')))

    def actions(self, name):
        def ssh(argv, **options):
            self.assertEqual(argv, SSH)
            return self.runner(['/usr/bin/python3', '-I', str(self.shim)], **options)
        data = payload(); data['hosts'][0]['id'] = 'desk'
        jale = dict(data['hosts'][0]['snapshot']['tasks'][0], id=78)
        jale['control'] = dict(launcher='jale', ref=JOB, expected=dict(task_id=78, state='Queued'), reason=None)
        data['hosts'][0]['snapshot']['tasks'].append(jale)
        return accion.Actions(serve.load_hosts(BASE / 'hosts.json'), self.root / name, lambda: data,
                              executor=accion.Executor(home=self.root, runner=ssh), clock=lambda: 1000)

    def bodies(self, prefix):
        return [request(host='desk', launcher='remote-run', ref=REF, action_id=prefix + '-rr'),
                request(host='desk', launcher='jale', ref=JOB, action_id=prefix + '-jale',
                        expected=dict(task_id=78, state='Queued'))]

    def cancels(self, launcher):
        return [r for r in seen(Path(self.controllers[launcher])) if r['op'] == 'cancel']

    def test_new_desk_channel_adds_origin_old_desk_channel_never_does(self):
        # The server's SSH envelope is unchanged by C5, so an old server sends this same stdin.
        new = self.actions('new')
        for body in self.bodies('newdesk'):
            self.assertEqual(new.post(body)[1]['status'], 'cancelled')
        # An old desk channel calls exchange directly. The shim reloads desk_action
        # from disk, so emulate it by patching the module file's send at import.
        self.shim.write_text(self.shim.read_text().replace(
            'desk.CONTROLLERS=', 'desk.send=lambda a,r,ru,e,t: desk.exchange(a,r,ru,e,desk.TIMEOUT)\ndesk.CONTROLLERS='))
        old = self.actions('old')
        for body in self.bodies('olddesk'):
            self.assertEqual(old.post(body)[1]['status'], 'cancelled')
        for launcher in self.controllers:
            with self.subTest(launcher=launcher):
                found = self.cancels(launcher)
                self.assertEqual(len(found), 2)
                self.assertEqual(found[0].get('origin'), 'rack-panel')
                self.assertNotIn('origin', found[1])

    def test_old_and_new_local_channels_on_the_mini(self):
        mini = write_controller(self.root / 'mini/.local/lib/remote-run/run-control.py')
        set_probe(mini, caps=CAPS)
        def run(name, action_id):
            actions = accion.Actions(T.HOSTS, self.root / name, payload,
                executor=accion.Executor(home=self.root / 'mini', runner=self.runner), clock=lambda: 1000)
            return actions.post(request(action_id=action_id))[1]['status']
        with patch.object(accion.desk_channel, 'send', accion.desk_channel.exchange):
            self.assertEqual(run('old', 'oldmini-0001'), 'cancelled')
        self.assertEqual(run('new', 'newmini-0001'), 'cancelled')
        log = [r for r in seen(mini) if r['op'] == 'cancel']
        self.assertEqual([r.get('origin') for r in log], [None, 'rack-panel'])


if __name__ == '__main__':
    unittest.main()
