"""C4: fixed desk dispatcher, exact SSH boundary and desk UI; only /tmp fakes."""
import safety
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

from test_rack import module, BASE
from test_actions import accion, serve, REF, request, receipt, payload

desk = module('desk_action')
SSH = ['/usr/bin/ssh', '-F', '/dev/null', '-T',
       '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
       '-o', 'IdentityAgent=none', '-o', 'ConnectTimeout=10',
       '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=~/.ssh/known_hosts',
       '-o', 'ClearAllForwardings=yes', '-o', 'PermitLocalCommand=no',
       '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
       '-i', '~/.ssh/id_ed25519_pao_rack_accion', 'Felix@felix-desktop']


def wire(launcher='remote-run', op='cancel'):
    value = dict(schema='rack-cancel@1', op=op, ref=REF if launcher == 'remote-run' else 'job-AbCd1234')
    if op != 'capabilities': value['action_id'] = 'action-0001'
    if op == 'cancel': value['expected'] = dict(task_id=77, state='Queued')
    return value


def envelope(launcher='remote-run', op='cancel'):
    return dict(schema='rack-desk-action@1', launcher=launcher, request=wire(launcher, op))


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rack-c4-dispatch-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.controllers = {k: str(self.root / k / Path(v).name) for k,v in desk.CONTROLLERS.items()}
        self.processes = []
        for name in self.controllers:
            self.controller(name)
        for name,value in [('HOME', self.root), ('SWITCH', self.root / 'actions.disabled'), ('CONTROLLERS', self.controllers)]:
            mock = patch.object(desk, name, value); mock.start(); self.addCleanup(mock.stop)

    def controller(self, launcher, source=None):
        path = Path(self.controllers[launcher]); path.parent.mkdir(exist_ok=True)
        source = source or '''import json,os,sys
from pathlib import Path
r=json.load(sys.stdin)
Path(__file__).with_suffix('.seen').write_text(json.dumps(dict(request=r,argv=sys.argv,
 env=dict(os.environ),cwd=os.getcwd(),tty=[os.isatty(0),os.isatty(1),os.isatty(2)],isolated=sys.flags.isolated)))
a=dict(schema='rack-cancel-result@1',ref=r['ref'])
if r['op']=='capabilities': a['capabilities']=dict(queued=True,running=True,reason=None)
else: a.update(action_id=r['action_id'],status='cancelled',result=dict(exit=130),error=None)
print(json.dumps(a))
'''
        path.write_text(source)
        return path

    def run_fake(self, argv, **options):
        self.assertIn(argv[-1], self.controllers.values())
        self.assertEqual(argv[:2], ['/usr/bin/python3', '-I'])
        self.assertEqual(options['env'], dict(HOME=str(self.root), PATH='/usr/bin:/bin', TMPDIR='/tmp'))
        self.assertEqual(options['cwd'], '/')
        self.assertEqual(options['stderr'], subprocess.DEVNULL)
        self.assertTrue(options['start_new_session'])
        process = subprocess.Popen(argv, **options); self.processes.append(process)
        return process

    def handle(self, data):
        return desk.handle(json.dumps(data).encode(), runner=self.run_fake)

    def assert_reaped(self):
        for process in self.processes:
            self.assertIsNotNone(process.returncode)
            self.assertTrue(process.stdin.closed and process.stdout.closed)
            with self.assertRaises(ChildProcessError): os.waitpid(process.pid, os.WNOHANG)
            with self.assertRaises(ProcessLookupError): os.kill(process.pid, 0)

    def test_fixed_table_each_launcher_and_all_operations(self):
        production = module('desk_action')
        self.assertEqual(production.CONTROLLERS, {
            'remote-run':'/home/felix/.local/lib/remote-run/run-control.py',
            'jale':'/home/felix/.local/lib/jale/pueue-control.py'})
        self.assertEqual(str(production.SWITCH), '/home/felix/.local/state/pao/rack/actions.disabled')
        with patch.dict(os.environ, {'SSH_ORIGINAL_COMMAND':'ignored', 'GH_TOKEN':'secret',
                                    'PYTHONPATH':'/evil', 'JALE_PUEUE_ROOT':'/evil'}):
            for launcher in self.controllers:
                for op in ('capabilities','cancel','status'):
                    answer,code = self.handle(envelope(launcher, op))
                    self.assertEqual(code,0)
                    self.assertEqual(answer['ref'],wire(launcher,op)['ref'])
                    path = Path(self.controllers[launcher])
                    self.assertTrue(path.with_suffix('.seen').exists(), 'wrong fixed controller chosen')
                    seen = json.loads(path.with_suffix('.seen').read_text())
                    self.assertEqual(seen['request'],wire(launcher,op))
                    self.assertEqual(seen['argv'], [str(path)])
                    self.assertEqual(seen['isolated'],1)
                    self.assertEqual(seen['tty'],[False]*3)
                    for name in ('SSH_ORIGINAL_COMMAND','GH_TOKEN','PYTHONPATH','JALE_PUEUE_ROOT'):
                        self.assertNotIn(name,seen['env'])
        self.assert_reaped()

    def test_closed_envelopes_unknown_launcher_and_exact_types(self):
        invalid = [dict(envelope(), extra=True), dict(envelope(), argv=['bad']),
                   dict(envelope(), controller='/tmp/no'), dict(envelope(), schema='bad'),
                   dict(envelope(), request=dict(wire(), extra=True)),
                   dict(envelope(), request=dict(wire(), ref='../escape')),
                   dict(envelope(), request=dict(wire(), expected=dict(task_id=True,state='Queued'))),
                   dict(envelope(), request=dict(wire(), expected=dict(task_id=1,state=['Queued']))),
                   dict(envelope(), request=dict(wire(), op='exec')), [], True]
        for data in invalid:
            with self.subTest(data=data):
                answer,code = self.handle(data)
                self.assertEqual((code,answer['status'],answer['error']['code']), (2,'rejected','invalid_request'))
        answer,code = self.handle(dict(envelope(),launcher='unknown'))
        self.assertEqual((code,answer['error']['code']), (2,'unsupported'))
        for raw in (b' ' * 16385, b'{"schema":1,"schema":2}', b'\xff', b'['*1100):
            self.assertEqual(desk.handle(raw)[1],2)
        self.assertEqual(self.processes,[])

    def test_switch_blocks_cancel_and_capabilities_but_allows_status(self):
        desk.SWITCH.touch()
        answer,code = self.handle(envelope())
        self.assertEqual((code,answer['status']), (0,'rejected'))
        self.assertEqual(answer['error']['code'],'unsupported')
        self.assertEqual(self.handle(envelope(op='capabilities'))[0]['capabilities']['queued'],False)
        self.assertEqual(self.processes,[])
        self.assertEqual(self.handle(envelope(op='status'))[0]['status'],'cancelled')
        desk.SWITCH.unlink()
        self.assertEqual(self.handle(envelope())[0]['status'],'cancelled')
        self.assert_reaped()

    def test_missing_timeout_overflow_and_failed_controller(self):
        Path(self.controllers['remote-run']).unlink()
        self.assertEqual(self.handle(envelope())[1],3)
        self.assertEqual(self.processes,[])
        self.controller('remote-run', 'import time\ntime.sleep(60)\n')
        with patch.object(desk, 'TIMEOUT', .5):
            answer,code = self.handle(envelope())
        self.assertEqual((code,answer['status']), (4,'unknown'))
        self.controller('remote-run', 'import os,time\nfor _ in range(256):os.write(1,b"x"*4096)\ntime.sleep(60)\n')
        read = os.read; sizes=[]
        def bounded(fd,size):
            chunk = read(fd,size); sizes.append(len(chunk)); return chunk
        with patch.object(desk.os,'read',side_effect=bounded):
            answer,code = self.handle(envelope())
        self.assertEqual((code,answer['status']), (5,'unknown'))
        self.assertEqual(sum(sizes),16385)
        self.controller('remote-run', 'import sys\nprint("PRIVATE /some/path")\nsys.exit(1)\n')
        # Invalid output is bounded and never forwarded, regardless of exit status.
        answer,code = self.handle(envelope())
        self.assertEqual((code,answer['status']), (5,'unknown'))
        self.assertNotIn('PRIVATE',json.dumps(answer))
        self.controller('remote-run','import json,sys\nr=json.load(sys.stdin)\nprint(json.dumps(dict(schema="rack-cancel-result@1",ref=r["ref"])))\nsys.exit(9)\n')
        self.assertEqual(self.handle(envelope())[1],6)
        self.assert_reaped()

    def test_stdin_deadline_and_python38_syntax(self):
        import ast
        ast.parse((BASE/'desk_action.py').read_text(),feature_version=(3,8))
        reader,writer=os.pipe()
        try:
            with os.fdopen(reader,'rb') as stream, patch.object(desk.time,'monotonic',side_effect=[0,6]):
                with self.assertRaises(ValueError):desk.read_request(stream)
        finally:os.close(writer)

    def test_cli_closed_stdin_body_and_no_arguments(self):
        # Invalid bodies stop before any installed path can be reached.
        for raw,args in [(b'{}',[]),(b'x'*16385,[]),(b'{}',['bad'])]:
            process = subprocess.run(['/usr/bin/python3','-I',str(BASE/'desk_action.py')]+args,
                input=raw,capture_output=True,timeout=5)
            self.assertEqual(process.returncode,2)
            self.assertEqual(json.loads(process.stdout)['error']['code'],'invalid_request')


class FakeProcess:
    """OS pipes exercise the production selector without launching ssh."""
    def __init__(self, answer, code, calls):
        incoming, writer = os.pipe(); reader, outgoing = os.pipe()
        self.stdin,self.stdout = os.fdopen(outgoing,'wb'),os.fdopen(incoming,'rb')
        self.reader,self.returncode,self.calls = reader,code,calls
        os.write(writer,json.dumps(answer).encode()); os.close(writer)
    def wait(self,timeout):
        self.calls.append((json.loads(os.read(self.reader,16384)),timeout));os.close(self.reader)


class SSHTests(unittest.TestCase):
    def setUp(self):
        self.host = serve.load_hosts(BASE/'hosts.json')[1]
        self.calls=[]
    def runner(self,argv,**options):
        self.calls.append((argv,options))
        return FakeProcess(receipt(wire()),0,self.calls)

    def test_exact_ssh_argv_stdin_only_environment_and_budget(self):
        self.assertEqual(self.host['actions']['argv'],SSH)
        with patch.dict(os.environ,{'SSH_AUTH_SOCK':'secret','GH_TOKEN':'secret'}):
            answer=accion.Executor(runner=self.runner,home='/synthetic-home')(self.host,'remote-run',wire())
        argv,options=self.calls[0]
        self.assertEqual(argv,SSH)
        self.assertEqual(self.calls[1][0],envelope())
        self.assertLessEqual(self.calls[1][1],40)
        self.assertGreater(self.calls[1][1],35)
        self.assertEqual(options['env'],dict(HOME='/synthetic-home',PATH='/usr/bin:/bin:/opt/homebrew/bin',TMPDIR='/tmp'))
        self.assertEqual(options['stdin'],subprocess.PIPE)
        self.assertEqual(options['stdout'],subprocess.PIPE)
        self.assertEqual(options['stderr'],subprocess.DEVNULL)
        self.assertEqual(options['cwd'],'/')
        self.assertTrue(options['start_new_session'])
        self.assertEqual(answer['status'],'cancelled')
        self.assertNotIn(REF,' '.join(argv))
        self.assertNotIn('action-0001',' '.join(argv))

    def test_other_argv_controller_paths_and_host_rejected(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            path=Path(folder)/'hosts.json'
            for change in (dict(argv=SSH+['extra']),dict(argv=['/bin/sh']),
                           dict(argv=[s.replace('StrictHostKeyChecking=yes','StrictHostKeyChecking=no') for s in SSH]),
                           dict(controllers={'remote-run':'/tmp/other.py'}),
                           dict(controllers={'jale':accion.CONTROLLERS['remote-run']}),dict(extra=True)):
                host=copy.deepcopy(self.host);host['actions'].update(change)
                path.write_text(json.dumps([host]))
                with self.subTest(change=change):
                    with self.assertRaises(ValueError):serve.load_hosts(path)
                    with self.assertRaises(ValueError):accion.Executor(runner=self.runner)(host,'remote-run',wire())
            for change in (dict(id='other'),dict(transport='local')):
                host=dict(self.host,**change);path.write_text(json.dumps([host]))
                with self.assertRaises(ValueError):serve.load_hosts(path)
                with self.assertRaises(ValueError):accion.Executor(runner=self.runner)(host,'remote-run',wire())
        self.assertEqual(self.calls,[])

    def test_ssh_failure_is_unknown_no_repeated_cancel_and_same_status(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            data=payload();data['hosts'][0]['id']='desk'
            def failing(argv,**options):
                self.assertEqual(argv,SSH)
                return FakeProcess(receipt(wire()),255,self.calls)
            executor=accion.Executor(runner=failing,home=folder)
            actions=accion.Actions([self.host],folder,lambda:data,executor=executor,clock=lambda:1000)
            body=request(host='desk')
            self.assertEqual(actions.post(body)[1]['status'],'unknown')
            self.assertEqual(actions.post(body)[1]['status'],'unknown')
            self.assertEqual(len(self.calls),1)
            self.assertEqual(actions.get(body['action_id'])[1]['status'],'unknown')
            self.assertEqual([c[0]['request']['op'] for c in self.calls],['cancel','status'])
            self.assertEqual(self.calls[1][0]['request']['action_id'],body['action_id'])

    def test_ssh_start_timeout_and_malformed_response_are_unknown(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            data=payload();data['hosts'][0]['id']='desk'
            for failure in (OSError('PRIVATE'),subprocess.TimeoutExpired('fake-ssh',40)):
                actions=accion.Actions([self.host],Path(folder)/type(failure).__name__,lambda:data,
                    executor=accion.Executor(runner=Mock(side_effect=failure)),clock=lambda:1000)
                self.assertEqual(actions.post(request(host='desk'))[1]['status'],'unknown')
            def malformed(*args,**kwargs):return FakeProcess({'schema':'bad'},0,self.calls)
            actions=accion.Actions([self.host],Path(folder)/'malformed',lambda:data,
                executor=accion.Executor(runner=malformed),clock=lambda:1000)
            self.assertEqual(actions.post(request(host='desk'))[1]['status'],'unknown')


class DeskUITests(unittest.TestCase):
    def test_both_desk_launchers_enabled_in_paused_groups(self):
        node=shutil.which('node')
        if not node:self.skipTest('node unavailable')
        hosts=serve.load_hosts(BASE/'hosts.json')
        self.assertIsNone(accion.transport_reason(hosts[1],'remote-run'))
        self.assertIsNone(accion.transport_reason(hosts[1],'jale'))
        data=payload();data['hosts'][0].update(id='desk',name='Felix DESKTOP',actions={'reason':None})
        data['hosts'][0]['snapshot']['groups'][0]['status']='Paused'
        harness=(BASE/'tests/ui-check.js').read_text().split('(async()=>{',1)[0]
        probe=r'''
(async()=>{
 const run=source=>vm.runInContext(source,context);
 assert.match(elements.app.innerHTML,/Admisión pausada/);
 const button={disabled:true},reason=element();
 context.menu={querySelector:s=>s==='[data-cancel]'?button:reason};
 for(const [launcher,ref] of [['remote-run','20261112-184713-d976b4'],['jale','job-AbCd1234']]){
  context.launcher=launcher;context.ref=ref;
  run(`S.hosts[0].tasks[0].control.launcher=launcher;S.hosts[0].tasks[0].control.ref=ref;
       globalThis.found={h:S.hosts[0],t:S.hosts[0].tasks[0]}`);
  assert.equal(run('cancelReason(found)'),null);
  const sent=[];
  context.fetch=async(url,options)=>{
   if(url==='/api/rack.json')return {ok:true,json:async()=>input.fixture};
   sent.push(JSON.parse(options.body));return {json:async()=>({schema:'rack-action@1',capabilities:{queued:true,running:true}})};
  };
  await run('prepareCancel(menu,found)');assert.equal(button.disabled,false);
  assert.equal(sent[0].host,'desk');assert.equal(sent[0].launcher,launcher);
  context.fetch=async()=>({json:async()=>({schema:'rack-action@1',status:'cancelling'})});
  run(`confirmCancel(found);`);assert.match(dialog.paragraph.textContent,/Felix DESKTOP/);dialog.close('si');
  await Promise.resolve();await Promise.resolve();
  assert.match(elements.app.innerHTML,/Cancelando…/);
  run(`{const [key,action]=[...PENDING][0];acceptAction(key,action,{status:'cancelled',result:{exit:130}})}`);
  assert.match(elements.toast.innerHTML,/cancelada/);
  run('CANCELLED.clear();render(fixture)');
 }
 console.log('desk UI: both launchers enabled in paused groups; pending then cancelled');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result=subprocess.run([node,'-e',harness+probe],input=json.dumps(dict(
            js=(BASE/'index.html').read_text().split('<script>',1)[1].split('</script>',1)[0],fixture=data)),
            capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
