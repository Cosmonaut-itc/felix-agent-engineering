"""Synthetic contract, crash, authorization and transport tests; no real hosts."""
import safety
import copy
import io
import json
from http.client import HTTPMessage
from http.server import ThreadingHTTPServer
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock
import urllib.request
import urllib.error

from test_rack import module, sample, task

serve, snapshot = module('serve'), module('snapshot')
accion = serve.accion
REF = '20261112-184713-d976b4'
HOSTS = [dict(id='mini', name='Mac Mini', transport='local', argv=['false'], actions=dict(transport='local', reason=None)),
         dict(id='desk', name='Felix DESKTOP', transport='ssh', argv=['false'],
              actions=dict(transport='disabled', reason='canal de escritura del desk pendiente'))]


def request(**changes):
    value = dict(schema='rack-action@1', op='cancel', host='mini', launcher='remote-run', ref=REF,
                 action_id='action-0001', expected=dict(task_id=77, state='Queued'))
    value.update(changes)
    return value


def receipt(wire, status='cancelled', code=None, exit=130):
    return dict(schema='rack-cancel-result@1', ref=wire['ref'], action_id=wire.get('action_id'), status=status,
                error=dict(code=code, message='PRIVATE /credentials') if code else None,
                result=dict(exit=exit) if status in ('cancelled', 'already_finished') else None)


def payload(now=1000):
    row = task(id=77, state='Queued', ticket='SEN-123', name='contract')
    row['control'] = dict(launcher='remote-run', ref=REF, expected=dict(task_id=77, state='Queued'), reason=None)
    return dict(schema='rack@1', now=now, tickets={}, hosts=[dict(id='mini', name='Mac Mini', fresh=True, age=0,
        snapshot=dict(sample(), tasks=[row]), estimates={})])


class ActionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data, self.calls, self.now = payload(), [], 1000
        self.response = 'cancelled'
        self.executor = self.fake_executor
        # Real transport would fail this test immediately, before any process starts.
        guard = patch.object(subprocess, 'Popen', side_effect=AssertionError('No real transport'))
        guard.start(); self.addCleanup(guard.stop)
        self.actions = self.reopen()

    def reopen(self):
        return accion.Actions(HOSTS, self.root, lambda: self.data, executor=self.executor, clock=lambda: self.now)

    def fake_executor(self, host, launcher, wire):
        self.calls.append(copy.deepcopy(wire))
        if wire['op'] == 'capabilities':
            return dict(schema='rack-cancel-result@1', ref=REF,
                        capabilities=dict(queued=True, running=True, reason=None))
        return receipt(wire, self.response)

    def test_every_contract_result_and_error_sanitized(self):
        for status in ('cancelled', 'cancelling', 'already_finished', 'cancel_unconfirmed', 'unknown'):
            with self.subTest(status=status):
                self.response = status
                body = request(action_id='action-'+status.replace('_','-'))
                code, response = self.actions.post(body)
                self.assertEqual((code, response['status']), (200, status))
                self.assertEqual(self.calls[-1], dict(schema='rack-cancel@1', op='cancel', ref=REF,
                    action_id=body['action_id'], expected=body['expected']))
        for error in ('stale_state', 'not_found', 'unsupported', 'idempotency_conflict', 'invalid_request'):
            body = request(action_id='rejected-'+error.replace('_','-'))
            with patch.object(self.actions, 'executor', side_effect=lambda h,l,w: receipt(w, 'rejected', error)):
                response = self.actions.post(body)[1]
            self.assertEqual(response['error']['code'], error)
            self.assertNotIn('PRIVATE', json.dumps(response))
            if error == 'stale_state':
                self.assertIn('cambió', response['error']['message'])

    def test_id_is_bound_to_digest_and_duplicate_does_not_act(self):
        response = self.actions.post(request())
        self.assertEqual(self.actions.post(request()), response)
        # Same semantics, reordered object keys is still the same digest.
        self.assertEqual(self.actions.post(dict(reversed(list(request().items())))), response)
        conflict = self.actions.post(request(expected=dict(task_id=77, state='Running')))
        self.assertEqual(conflict[0], 409)
        self.assertEqual(conflict[1]['error']['code'], 'idempotency_conflict')
        self.assertEqual(len(self.calls), 1)
        again = self.reopen()
        self.assertEqual(again.post(request()), response)
        self.assertEqual(len(self.calls), 1)
        with sqlite3.connect(str(self.actions.path)) as db:
            digest, body = db.execute('SELECT digest,request FROM actions').fetchone()
        self.assertEqual(len(digest), 64)
        self.assertEqual(json.loads(body), request())
        self.assertEqual(self.actions.path.stat().st_mode & 0o777, 0o600)

    def test_intent_committed_before_effect_and_crash_before_effect(self):
        class Crash(BaseException): pass
        def crash(host, launcher, wire):
            with sqlite3.connect(str(self.actions.path)) as db:
                row = db.execute('SELECT response FROM actions').fetchone()
            self.assertEqual(json.loads(row[0])['status'], 'unknown')
            raise Crash()
        self.actions.executor = crash
        with self.assertRaises(Crash):
            self.actions.post(request())
        self.actions = self.reopen()
        self.assertEqual(self.actions.post(request())[1]['status'], 'unknown')
        self.assertEqual(self.calls, [])
        self.response = 'unknown'
        self.assertEqual(self.actions.get('action-0001')[1]['status'], 'unknown')
        self.assertEqual([w['op'] for w in self.calls], ['status'])

    def test_crash_after_effect_before_receipt_recovers_only_status(self):
        with patch.object(self.actions, 'save', side_effect=sqlite3.OperationalError('crash')):
            with self.assertRaises(sqlite3.OperationalError):
                self.actions.post(request())
        self.assertEqual([w['op'] for w in self.calls], ['cancel'])
        self.actions = self.reopen()
        self.assertEqual(self.actions.post(request())[1]['status'], 'unknown')
        self.assertEqual(self.actions.get('action-0001')[1]['status'], 'cancelled')
        self.assertEqual([w['op'] for w in self.calls], ['cancel', 'status'])
        self.actions.get('action-0001')
        self.assertEqual(len(self.calls), 2)

    def test_timeout_unknown_and_absent_launcher_receipt_is_not_no_effect(self):
        with patch.object(self.actions, 'executor', side_effect=subprocess.TimeoutExpired('fake', 10)):
            self.assertEqual(self.actions.post(request())[1]['status'], 'unknown')
        with patch.object(self.actions, 'executor', side_effect=lambda h,l,w: receipt(w, 'rejected', 'not_found')):
            answer = self.actions.get('action-0001')[1]
        self.assertEqual(answer['status'], 'unknown')
        self.assertEqual(self.actions.get('missing-0000')[1]['status'], 'unknown')
        self.response = 'cancelled'
        self.assertEqual(self.actions.get('action-0001')[1]['status'], 'cancelled')

    def test_progress_same_id_and_unconfirmed_remains_queryable(self):
        for status in ('cancelling', 'cancel_unconfirmed', 'unknown'):
            self.response = status
            body = request(action_id='progress-'+status.replace('_','-'))
            self.actions.post(body)
            self.assertEqual(self.actions.get(body['action_id'])[1]['status'], status)
            self.assertEqual(self.calls[-1], dict(schema='rack-cancel@1', op='status', ref=REF, action_id=body['action_id']))
            self.response = 'cancelled'
            self.assertEqual(self.actions.get(body['action_id'])[1]['status'], 'cancelled')

    def test_switch_does_not_break_reads_or_abandon_accepted_actions(self):
        self.response = 'cancelling'
        self.actions.post(request())
        (self.root / 'actions.disabled').touch()
        self.assertEqual(self.actions.post(request(action_id='disabled-01'))[0], 403)
        cap = request(op='capabilities'); del cap['action_id']
        self.assertIn('apagadas', self.actions.post(cap)[1]['error']['message'])
        self.assertEqual(self.data['hosts'][0]['snapshot']['tasks'][0]['id'], 77)
        self.response = 'cancelled'
        self.assertEqual(self.actions.get('action-0001')[1]['status'], 'cancelled')
        (self.root / 'actions.disabled').unlink()
        self.assertEqual(self.actions.post(request(action_id='enabled-001'))[1]['status'], 'cancelled')

    def test_rejected_intents_also_bind_id_to_digest(self):
        (self.root / 'actions.disabled').touch()
        self.assertEqual(self.actions.post(request())[0], 403)
        (self.root / 'actions.disabled').unlink()
        answer = self.actions.post(request(expected=dict(task_id=78, state='Queued')))
        self.assertEqual(answer[0], 409)
        self.assertEqual(answer[1]['error']['code'], 'idempotency_conflict')
        self.assertEqual(self.actions.post(request())[1]['error']['code'], 'disabled')
        self.assertEqual(self.calls, [])

    def test_capabilities_are_per_launcher_and_not_guessed_from_group(self):
        cap = request(op='capabilities'); del cap['action_id']
        self.assertTrue(self.actions.post(cap)[1]['capabilities']['queued'])
        for host, launcher, ref, reason in [('desk','remote-run',REF,'canal de escritura del desk pendiente'),
                ('mini','jale','job-AbCd1234','fuera de Pueue')]:
            answer = self.actions.post(dict(cap, host=host, launcher=launcher, ref=ref))[1]
            self.assertIn(reason, answer['error']['message'])
        with patch.object(self.actions, 'executor', return_value=dict(schema='rack-cancel-result@1', ref=REF,
                capabilities=dict(queued=False, running=False, reason='private-path'))):
            answer = self.actions.post(cap)[1]
            self.assertFalse(answer['capabilities']['running'])
            self.assertNotIn('private', json.dumps(answer))

    def test_stale_missing_old_or_ineligible_snapshot_blocks_transport(self):
        for number, alter in enumerate((lambda: self.data['hosts'][0].update(fresh=False),
                      lambda: self.data.update(now=900),
                      lambda: self.data['hosts'][0].update(age=50),
                      lambda: self.data['hosts'][0]['snapshot']['tasks'][0].update(state='Running'),
                      lambda: self.data['hosts'][0]['snapshot']['tasks'][0].update(group='otros'),
                      lambda: self.data['hosts'][0]['snapshot']['tasks'][0].update(group='pg5434'),
                      lambda: self.data['hosts'][0]['snapshot']['tasks'][0].update(control=None))):
            self.data = payload(); alter()
            self.assertEqual(self.actions.post(request(action_id='ineligible-'+str(number)))[0], 409)
        self.assertEqual(self.calls, [])

    def test_closed_request_shapes_types_and_invalid_controller_output(self):
        for changes in (dict(op='kill'),dict(op=[]),dict(schema='x'),dict(ref='../x'),dict(ref=REF.upper()),
                        dict(action_id='../bad-id'),dict(action_id=True),dict(expected=dict(task_id=True,state='Queued')),
                        dict(expected=dict(task_id=77,state='Paused')), dict(extra='forbidden'),dict(host=[])):
            with self.assertRaises((ValueError, TypeError)):
                self.actions.post(request(**changes))
        with self.assertRaises(ValueError): accion.decode(b'{"op":"a","op":"b"}')
        with self.assertRaises(ValueError): accion.decode(b' ' * 16385)
        for response in (None, {}, receipt(request(), 'cancelled', exit=0),
                         dict(receipt(request()), action_id='another-id'),
                         dict(receipt(request()), ref='another-ref'),
                         dict(receipt(request()), status='made-up')):
            body = request(action_id='invalid-'+str(len(self.calls)).zfill(8))
            with patch.object(self.actions, 'executor', return_value=response):
                self.assertEqual(self.actions.post(body)[1]['status'], 'unknown')
                # Clean the test row to test the next invalid result independently.
                with sqlite3.connect(str(self.actions.path)) as db: db.execute('DELETE FROM actions')

    def test_concurrent_same_id_calls_transport_once(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def block(h,l,w):
            calls.append(w); entered.set(); self.assertTrue(release.wait(2)); return receipt(w)
        self.actions.executor = block
        answers = []
        a = threading.Thread(target=lambda: answers.append(self.actions.post(request())))
        b = threading.Thread(target=lambda: answers.append(self.actions.post(request())))
        a.start(); self.assertTrue(entered.wait(2)); b.start(); release.set()
        a.join(3); b.join(3)
        self.assertFalse(a.is_alive() or b.is_alive())
        self.assertEqual(len(calls), 1)
        self.assertEqual(answers[0], answers[1])


class SnapshotControlTests(unittest.TestCase):
    def test_identity_is_derived_without_reading_private_files(self):
        home = Path('/synthetic-home')
        raw = dict(id=77, group='suites', path=str(home / 'remote-run/runs' / REF))
        with patch.object(Path, 'open', side_effect=AssertionError('No file reads')), patch.object(snapshot, 'command', side_effect=AssertionError('No commands')):
            self.assertEqual(snapshot.task_control(raw, home, 'Queued'),
                dict(launcher='remote-run', ref=REF, expected=dict(task_id=77, state='Queued'), reason=None))
            self.assertEqual(snapshot.task_control(raw, home, 'Running')['expected']['state'], 'Running')
            for group in ('otros','pg5434'):
                self.assertIn('grupo', snapshot.task_control(dict(raw,group=group),home,'Queued')['reason'])
            for ref in ('../../escape',REF+'x','garbage'):
                self.assertIsNone(snapshot.task_control(dict(raw,path=str(home/'remote-run/runs'/ref)),home,'Queued')['launcher'])
            jale = dict(id=2,group='jale',command='exec bash /synthetic-home/.local/state/jale/pueue/job-AbCd1234/task.sh')
            self.assertEqual(snapshot.task_control(jale,home,'Queued')['launcher'],'jale')
            self.assertIsNone(snapshot.task_control(dict(jale,command='exec bash /else/job-AbCd1234/task.sh'),home,'Queued')['launcher'])
            self.assertIn('Sólo',snapshot.task_control(raw,home,'Paused')['reason'])

    def test_control_is_validated_and_unknown_fields_cannot_escape(self):
        row = payload()['hosts'][0]['snapshot']['tasks'][0]
        data = dict(sample(), tasks=[row])
        self.assertEqual(serve.validate_snapshot(data, 'mini')['tasks'][0]['control'], row['control'])
        for control in ({}, dict(row['control'], command='PRIVATE'),dict(row['control'],ref='../bad'),
                        dict(row['control'],expected=dict(task_id=78,state='Queued'))):
            row = dict(row,control=control)
            self.assertIsNone(serve.validate_snapshot(dict(data,tasks=[row]),'mini')['tasks'][0]['control'])


class AuthorizationTests(unittest.TestCase):
    reopen = ActionsTests.reopen
    fake_executor = ActionsTests.fake_executor
    def handler(self, body=None, method='POST', path='/api/accion', **headers):
        h = object.__new__(serve.Handler)
        h.server = type('Server', (), dict(server_address=('100.100.0.1',8788), names=('mini',),
            owner_check=self.owner, actions=self.actions, rack=type('Rack', (),dict(read=lambda _:self.data))()))()
        h.client_address = ('100.100.0.2',1234)
        h.command, h.path = method, path
        h.headers = HTTPMessage()
        raw = json.dumps(body or request()).encode()
        values = {'Host':'mini:8788','Origin':'http://mini:8788','X-Rack-Action':'1',
                  'Content-Type':'application/json','Content-Length':str(len(raw))}
        values.update(headers)
        for key, value in values.items():
            if value is not None:
                for item in value if isinstance(value,list) else [value]: h.headers[key] = item
        h.rfile = io.BytesIO(raw)
        h.connection = Mock()
        answers=[]
        h.reply=lambda code,raw,*a,**k:answers.append((code,json.loads(raw) if raw else None))
        h.dispatch()
        return answers[0]

    def setUp(self):
        ActionsTests.setUp(self)
        self.login, self.whois_count, self.whois_fail = 'owner', 0, False
        self.owner = serve.OwnerCheck('owner','/fake/tailscale')
        def popen(argv, **kw):
            self.whois_count += 1
            process = Mock(returncode=1 if self.whois_fail else 0)
            process.communicate.return_value=(json.dumps(dict(UserProfile=dict(LoginName=self.login))),None)
            return process
        fake = patch.object(serve.subprocess,'Popen',side_effect=popen)
        fake.start(); self.addCleanup(fake.stop)

    def test_whois_is_fresh_for_every_mutation_even_after_cached_read(self):
        self.assertTrue(self.owner.allows('100.100.0.2'))
        self.assertEqual(self.handler()[0],200)
        self.login='another-owner'
        self.assertEqual(self.handler(request(action_id='second-0001'))[0],403)
        self.assertEqual(self.whois_count,3)
        self.assertEqual(len(self.calls),1)

    def test_each_auth_rejection_prevents_effect(self):
        for headers in ({'Origin':None},{'Origin':'http://evil.example:8788'},{'Origin':'http://mini:8788/'},
                        {'Origin':['http://mini:8788','http://mini:8788']},{'X-Rack-Action':None},
                        {'X-Rack-Action':'0'},{'Host':'evil.example:8788'},{'Host':None},
                        {'Host':['mini:8788','mini:8788']},{'Tailscale-Funnel-Request':'?1'},
                        {'Content-Type':'text/plain'},{'Sec-Fetch-Site':'cross-site'}):
            with self.subTest(headers=headers): self.assertEqual(self.handler(**headers)[0],403)
        for fail,login in ((True,'owner'),(False,'intruder')):
            self.whois_fail,self.login=fail,login
            self.assertEqual(self.handler()[0],403)
        self.assertEqual(self.calls,[])

    def test_missing_fetch_site_is_allowed_but_not_a_substitute_for_origin(self):
        self.assertEqual(self.handler()[0],200)
        self.assertEqual(self.handler(Origin=None,**{'Sec-Fetch-Site':'same-origin'})[0],403)

    def test_invalid_http_envelopes_and_receipt_query(self):
        for headers in ({'Content-Length':'16385'},{'Content-Length':None},{'Transfer-Encoding':'chunked'},
                        {'Content-Length':['1','1']}):
            self.assertEqual(self.handler(**headers)[0],400)
        self.assertEqual(self.handler(request(extra='not-allowed'))[0],400)
        self.assertEqual(self.handler()[0],200)
        self.assertEqual(self.handler(method='GET',path='/api/accion/action-0001')[1]['status'],'cancelled')
        self.assertEqual(self.handler(method='GET',path='/api/accion/missing-000')[1]['status'],'unknown')
        self.assertEqual(self.handler(method='DELETE')[0],405)
        self.assertEqual(self.handler(method='GET',path='/api/accion/../../x')[0],400)

    def test_full_http_roundtrip_and_no_cors(self):
        # Real HTTP server, synthetic owner and controller, no collector thread.
        server=ThreadingHTTPServer(('127.0.0.1',0),serve.Handler)
        server.actions,server.owner_check=self.actions,Mock()
        server.owner_check.allows.return_value=True
        server.rack=Mock();server.rack.read.return_value=self.data
        thread=threading.Thread(target=server.serve_forever);thread.start()
        def cleanup():server.shutdown();server.server_close();thread.join(3)
        self.addCleanup(cleanup)
        url='http://127.0.0.1:'+str(server.server_port)
        self.response='cancelling'
        req=urllib.request.Request(url+'/api/accion',data=json.dumps(request()).encode(),
            headers={'Origin':url,'Content-Type':'application/json','X-Rack-Action':'1'})
        with urllib.request.urlopen(req) as response:
            self.assertEqual(json.load(response)['status'],'cancelling')
            self.assertIsNone(response.headers.get('Access-Control-Allow-Origin'))
        self.response='cancelled'
        with urllib.request.urlopen(url+'/api/accion/action-0001') as response:
            self.assertEqual(json.load(response)['result'],{'exit':130})
        (self.root/'actions.disabled').touch()
        with urllib.request.urlopen(url+'/api/rack.json') as response:
            self.assertEqual(json.load(response)['schema'],'rack@1')
        server.owner_check.allows.assert_called_with('127.0.0.1',fresh=True)


class TransportTests(unittest.TestCase):
    def test_exact_argv_stdin_environment_no_tty_and_bounded_response(self):
        calls=[]
        class Process:
            returncode=0
            def communicate(self,raw,timeout):
                calls.append((json.loads(raw),timeout))
                output.write(json.dumps(receipt(json.loads(raw))).encode())
        def runner(argv,**kwargs):
            nonlocal output
            output=kwargs['stdout']
            calls.append((argv,kwargs))
            return Process()
        output=None
        executor=accion.Executor(runner=runner,home='/synthetic-home')
        wire=dict(schema='rack-cancel@1',op='cancel',ref=REF,action_id='action-0001',expected=dict(task_id=77,state='Queued'))
        with patch.dict('os.environ',{'GH_TOKEN':'secret','REMOTE_RUN_HOST':'desk','PYTHONPATH':'/evil'}):
            response=executor(HOSTS[0],'remote-run',wire)
        argv,options=calls[0]
        self.assertEqual(argv,['/usr/bin/python3','-I','/synthetic-home/.local/lib/remote-run/run-control.py'])
        self.assertEqual(options['env'],{'HOME':'/synthetic-home','PATH':'/usr/bin:/bin:/opt/homebrew/bin','TMPDIR':'/tmp'})
        self.assertEqual(options['stdin'],subprocess.PIPE)
        self.assertEqual(options['stderr'],subprocess.DEVNULL)
        self.assertTrue(options['start_new_session'])
        self.assertEqual(options['cwd'],'/')
        self.assertEqual(calls[1],(wire,10))
        self.assertEqual(response['status'],'cancelled')
        for host,launcher in ((HOSTS[1],'remote-run'),(HOSTS[0],'jale')):
            with self.assertRaises(ValueError):executor(host,launcher,wire)
        self.assertEqual(len(calls),2)

    def test_timeout_kills_only_own_process_group_and_reaps(self):
        process=Mock(pid=987654,returncode=-9)
        process.communicate.side_effect=[subprocess.TimeoutExpired('fake',10),(None,None)]
        with patch.object(accion.os,'killpg') as kill:
            with self.assertRaises(subprocess.TimeoutExpired):
                accion.Executor(runner=Mock(return_value=process),home='/synthetic-home')(HOSTS[0],'remote-run',{})
        kill.assert_called_once_with(987654,accion.signal.SIGKILL)
        self.assertEqual(process.communicate.call_count,2)

    def test_guard_rejects_real_binaries_before_execution(self):
        for argv in (['/usr/bin/ssh','desk'],['/opt/homebrew/bin/pueue','status'],
                     ['/Applications/Tailscale.app/Contents/MacOS/Tailscale','whois'],
                     ['/usr/bin/python3','-I',str(Path.home()/'.local/lib/remote-run/run-control.py')]):
            with self.assertRaises(AssertionError):subprocess.run(argv)


class UITests(unittest.TestCase):
    def test_production_js_dialog_pending_toast_reasons_and_network(self):
        import shutil
        node=shutil.which('node')
        if not node:self.skipTest('node no disponible para ejecutar JS; usar browser_fixture.py')
        base=Path(__file__).resolve().parents[1]
        js=(base/'index.html').read_text().split('<script>',1)[1].split('</script>',1)[0]
        result=subprocess.run([node,str(base/'tests/ui-check.js')],input=json.dumps(dict(js=js,fixture=payload())),
                              text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('UI OK',result.stdout)
