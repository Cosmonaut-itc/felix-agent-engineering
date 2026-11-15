"""U3.2: complete desk path with fake SSH, private controllers and no real binaries."""
import safety
import copy
import io
import json
import os
import importlib.util
from contextlib import redirect_stdout
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from test_slots import control, FakePueue, accion, BASE
import test_actions


class DeskChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='rack-u32-',dir='/tmp');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.now=1794664799.0  # 2026-11-14 13:59:59 UTC
        self.p=FakePueue();self.p.groups={g:dict(status='Running',parallel_tasks=n) for g,n in control.desk_profile(self.now).items()}
        self.engine=control.MiniControl(self.p,self.root/'host',clock=lambda:self.now,generation=lambda:'linux-proc-v1:'+'1'*64,host='desk')
        with control.locked(self.engine.directory):self.engine.adopt()
        self.host=dict(id='desk',transport='ssh',actions=dict(transport='ssh',reason=None,
                       controllers=copy.deepcopy(accion.CONTROLLERS),argv=list(accion.DESK_ARGV)))
        self.payload=dict(now=self.now,hosts=[dict(id='desk',fresh=True,age=0,snapshot={})])
        self.calls=[]
        def execute(host,target,wire,timeout):
            self.assertEqual(target,'host-control');self.calls.append(copy.deepcopy(wire))
            return self.engine.handle(wire)
        self.execute=execute
        self.actions=accion.Actions([self.host],self.root/'rack',lambda:self.payload,execute,clock=lambda:self.now)

    def request(self,ident='desk-slots-0001',dry_run=False):
        v=self.engine.view()
        return dict(schema=accion.SCHEMA,op='slots',host='desk',action_id=ident,dry_run=dry_run,
                    expected={k:v[k] for k in ('revision','policy_hash','generation','mode','slots')},change=dict(group='jale',delta=-1))

    def test_desk_post_undo_and_sanitized_expiration_no_private_fields(self):
        request=self.request();code,answer=self.actions.post(request)
        self.assertEqual((code,answer['status']),(200,'applied'))
        self.assertEqual(answer['result']['expires_at'],1794664800.0)
        self.assertEqual(answer['result']['observed']['jale'],6)
        self.assertNotIn('before_overrides',json.dumps(answer))
        undo=self.request('desk-undo-0001');undo.update(op='undo',undo_of=request['action_id']);del undo['change']
        self.assertEqual(self.actions.post(undo)[1]['status'],'applied')
        self.assertEqual(self.p.calls,[('jale',6),('jale',7)])

    def test_lost_post_queries_same_id_then_event_aborts_pending(self):
        def lost(*a,**kw):
            with patch.object(self.engine,'reconcile',side_effect=OSError('RPC lost')):self.execute(*a,**kw)
            raise OSError('transport lost')
        r=self.request();self.actions.executor=lost
        self.assertEqual(self.actions.post(r)[1]['status'],'unknown')
        self.assertEqual(self.actions.post(r)[1]['status'],'unknown')
        self.now+=1;self.payload['now']=self.now
        self.assertTrue(self.engine.reconcile())
        self.actions.executor=self.execute
        answer=self.actions.get(r['action_id'])[1]
        self.assertEqual(answer['status'],'aborted')
        self.assertEqual(answer['error']['code'],'expired')
        self.assertEqual([c['op'] for c in self.calls],['slots','status'])
        self.assertIsNone(answer['result']['undo_of'])

    def test_lost_expired_undo_rejection_is_terminal_without_repeating(self):
        r=self.request();self.actions.post(r)
        undo=self.request('desk-undo-0001');undo.update(op='undo',undo_of=r['action_id']);del undo['change']
        self.now+=1;self.payload['now']=self.now
        def lose(*a,**kw):self.execute(*a,**kw);raise OSError('lost reply')
        self.actions.executor=lose
        self.assertEqual(self.actions.post(undo)[1]['status'],'unknown')
        self.actions.executor=self.execute
        code,answer=self.actions.get(undo['action_id'])
        self.assertEqual((code,answer['status'],answer['error']['code']),(409,'conflict','undo_conflict'))
        self.assertEqual(self.p.calls,[('jale',6)])

    def test_lost_missed_timer_conflict_recovers_bound_receipt_without_resend(self):
        self.actions.post(self.request())
        self.now+=1;self.payload['now']=self.now
        r=self.request('missed-lost-0001')
        def lose(*a,**kw):self.execute(*a,**kw);raise OSError('lost schedule response')
        self.actions.executor=lose
        self.assertEqual(self.actions.post(r)[1]['status'],'unknown')
        calls=copy.deepcopy(self.p.calls)
        self.actions.executor=self.execute
        code,answer=self.actions.get(r['action_id'])
        self.assertEqual((code,answer['status'],answer.get('effects')),(409,'conflict',True))
        self.assertEqual(answer['control']['slots'],control.desk_profile(self.now))
        self.assertEqual(self.p.calls,calls)
        self.assertEqual([c['op'] for c in self.calls],['slots','slots','status'])
        raw=self.engine.handle(dict(schema=control.HOST_SCHEMA,op='status',action_id=r['action_id']))
        for field,value in (('schedule_recovery',None),('schedule_recovery',True),('schedule_recovery',self.now-3600),
                            ('receipt_digest','0'*64),('control',None),('error',dict(code='busy'))):
            with self.subTest(field=field,value=value), self.assertRaises(ValueError):
                accion.host_channel.sanitize(r,dict(raw,**{field:value}),'status')

    def test_host_old_unsupported_and_unknown_receipts_never_resend(self):
        old=dict(schema='rack-cancel-result@1',error=dict(code='invalid_request'))
        self.actions.executor=lambda *a,**k:old
        self.assertEqual(self.actions.post(dict(schema=accion.SCHEMA,op='host_capabilities',host='desk'))[1]['status'],'unsupported')
        self.assertEqual(self.actions.post(self.request())[0],409)
        r=self.request('desk-unknown-01')
        self.actions.executor=lambda *a,**k:dict(schema='rack-host-result@1',status='unknown',action_id=r['action_id'])
        self.assertEqual(self.actions.post(r)[1]['status'],'unknown')
        self.actions.executor=lambda *a,**k:old
        self.assertEqual(self.actions.get(r['action_id'])[1]['status'],'unknown')
        self.assertEqual(self.p.calls,[])

    def test_host_digest_different_body_and_dry_run_are_separate(self):
        before=self.engine.path.read_bytes()
        self.assertEqual(self.actions.post(self.request(dry_run=True))[1]['status'],'dry_run')
        self.assertEqual(self.engine.path.read_bytes(),before)
        self.assertEqual(self.p.calls,[])
        r=self.request();self.actions.post(r)
        other=copy.deepcopy(r);other['change']['group']='otros'
        self.assertEqual(self.actions.post(other)[0],409)
        self.assertEqual(self.p.calls,[('jale',6)])

    def test_executor_forced_envelope_fixed_key_expanded_paths_no_shell(self):
        calls=[]
        def exchange(argv,request,runner,env,timeout):
            calls.append((argv,request,env,timeout))
            return 0,dict(schema='rack-host-result@1',status='unknown',action_id=request['request']['action_id'])
        wire=dict(schema=control.HOST_SCHEMA,op='status',action_id='desk-slots-0001')
        with patch.object(accion.desk_channel,'exchange',side_effect=exchange):
            answer=accion.Executor(home=self.root)(self.host,'host-control',wire,timeout=3)
        self.assertEqual(answer['status'],'unknown')
        argv,envelope,env,timeout=calls[0]
        self.assertEqual(argv,[a.replace('~/',str(self.root)+'/') for a in accion.DESK_ARGV])
        self.assertEqual(envelope,dict(schema='rack-desk-action@1',target='host-control',request=wire))
        self.assertEqual(timeout,3);self.assertEqual(set(env),{'HOME','PATH','TMPDIR'})
        self.assertNotIn('launcher',envelope)
        self.assertNotIn(wire['action_id'],' '.join(argv))

    def test_dispatcher_fixed_controller_and_closed_envelope_and_status_disabled(self):
        desk=accion.desk_channel
        controller=self.root/'control.py'
        controller.write_text('import json,sys\nr=json.load(sys.stdin)\nprint(json.dumps(dict(schema="rack-host-result@1",action_id=r.get("action_id"),status="unknown")))\n')
        processes=[]
        def runner(argv,**kwargs):
            self.assertEqual(argv,['/usr/bin/python3','-I',str(controller),'rack','--host','desk'])
            self.assertTrue(kwargs['start_new_session']);self.assertEqual(kwargs['cwd'],'/')
            process=subprocess.Popen(argv,**kwargs);processes.append(process);return process
        wire=dict(schema=control.HOST_SCHEMA,op='status',action_id='desk-slots-0001')
        envelope=dict(schema='rack-desk-action@1',target='host-control',request=wire)
        with patch.object(desk,'HOST_CONTROLLER',controller),patch.object(desk,'SWITCH',self.root/'switch'):
            desk.SWITCH.touch()
            answer,code=desk.handle(json.dumps(envelope).encode(),runner)
            self.assertEqual((code,answer['status']),(0,'unknown'))
            for invalid in (dict(envelope,launcher='jale'),dict(envelope,target='other'),dict(envelope,argv=['sh']),
                            dict(envelope,request=dict(wire,op='mode')),dict(envelope,request=dict(wire,actor='Felix'))):
                self.assertEqual(desk.handle(json.dumps(invalid).encode(),runner)[1],2)
        self.assertEqual(len(processes),1);self.assertTrue(all(p.poll() is not None for p in processes))

    def test_desk_host_capabilities_snapshots_and_ui(self):
        v=self.engine.view();safe=accion.host_channel.control(v,'desk')
        self.assertEqual((safe['cap'],safe['mode']),(18,'noche'))
        self.assertEqual(safe['next_event'],1794664800)
        for key,value in (('cap',7),('mode','fijo'),('expires_at',True),('next_event',True)):
            with self.assertRaises(ValueError):accion.host_channel.control(dict(v,**{key:value}),'desk')
        fixture=dict(now=self.now,tickets={},hosts=[dict(id='desk',name='Felix DESKTOP · WSL',fresh=True,age=0,estimates={},actions=dict(reason=None),
            snapshot=dict(control=safe,groups=[dict(name=g,slots=n,status='Running') for g,n in control.desk_profile(self.now).items()],
                          tasks=[],cpu=dict(cores=12,load=[1,1,1]),mem=dict(total=32*1024**3,available=24*1024**3,swap_total=0,swap_used=0)))])
        source=(BASE/'index.html').read_text().split('<script>',1)[1].split('</script>',1)[0]
        result=subprocess.run(['node',str(BASE/'tests/slots-ui-check.js')],input=json.dumps(dict(js=source,fixture=fixture,scenario='desk')),
                              text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('UI desk OK',result.stdout)

    def test_foreign_work_diagnostic_is_sanitized_and_host_rejection_is_terminal(self):
        self.actions.post(self.request())
        original=self.p.status
        def status():
            value=original()
            value['tasks']={'1':dict(group='outside',status={'Running':{}},command='private-secret',envs={'token':'private-secret'})}
            return value
        with patch.object(self.p,'status',side_effect=status):
            raw=self.engine.view()
            safe=accion.host_channel.control(raw,'desk')
            self.assertEqual(safe.get('foreign_work'),1)
            self.assertNotIn('private-secret',json.dumps(safe))
            for invalid in (True,-1,'1',{}):
                with self.assertRaises(ValueError):accion.host_channel.control(dict(raw,foreign_work=invalid),'desk')
            r=self.request('foreign-channel-01');r['change']['delta']=1
            code,answer=self.actions.post(r)
            self.assertEqual((code,answer['status'],(answer.get('error') or {}).get('code')),
                             (409,'rejected','foreign_work'))
            self.assertEqual(self.actions.get(r['action_id'])[1]['status'],'rejected')
            self.assertEqual(self.p.calls,[('jale',6)])

    def test_desk_authorization_still_fresh_owner_host_origin(self):
        self.owner=Mock();self.owner.allows.return_value=True;self.data=self.payload
        for headers in ({'Origin':None},{'Origin':'http://evil:8788'},{'Host':'evil:8788'},{'X-Rack-Action':None}):
            self.assertEqual(test_actions.AuthorizationTests.handler(self,self.request(),**headers)[0],403)
        self.assertEqual(self.p.calls,[])
        self.assertEqual(test_actions.AuthorizationTests.handler(self,self.request())[0],200)
        self.owner.allows.assert_called_with('100.100.0.2',fresh=True)


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

WRAPPER = """import importlib.util,json,sys,os
from pathlib import Path
root=Path(__file__).resolve().parent
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
c=load('control',{control!r});fake=load('fake',root/'fakepueue.py')
assert sys.argv[1:]==['rack','--host','desk'],sys.argv
assert sys.flags.isolated==1
(root/'seen.json').write_text(json.dumps(dict(argv=sys.argv,isolated=sys.flags.isolated)))
c.Pueue=lambda *a:fake.FilePueue(root/'pueue.json')
c.desk_generation=lambda *a:'linux-proc-v1:'+'d'*64
Original=c.MiniControl
c.MiniControl=lambda p,d,**kw:Original(p,d,clock=lambda:1794664799.,**kw)
c.main()
"""


class DeskForcedChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='rack-u32-forced-',dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        (self.root/'fakepueue.py').write_text(FAKE_PUEUE)
        self.controller=self.root/'control.py'
        self.controller.write_text(WRAPPER.format(control=str(BASE.parent/'pueue-desk/control.py')))
        self.table=control.desk_profile(1794664799.)
        groups={g:dict(status='Running',parallel_tasks=n) for g,n in self.table.items()}
        groups['default']=dict(status='Paused',parallel_tasks=1)
        (self.root/'pueue.json').write_text(json.dumps(dict(groups=groups,tasks={})))
        spec=importlib.util.spec_from_file_location('file_pueue',self.root/'fakepueue.py')
        fake=importlib.util.module_from_spec(spec);spec.loader.exec_module(fake)
        self.directory=self.root/'home/.local/state/pao/pueue-desk'
        self.engine=control.MiniControl(fake.FilePueue(self.root/'pueue.json'),self.directory,
                                       clock=lambda:1794664799.,generation=lambda:'linux-proc-v1:'+'d'*64,host='desk')
        with control.locked(self.directory):self.engine.adopt()
        shim=self.root/'fake_ssh.py'
        shim.write_text("import importlib.util,json,sys,subprocess\nfrom pathlib import Path\n"
          +"def load(name,path):\n s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m\n"
          +"load('safety',"+repr(str(BASE/'tests/safety.py'))+")\n"
          +"desk=load('desk',"+repr(str(BASE/'desk_action.py'))+")\n"
          +"root=Path("+repr(str(self.root))+");desk.HOME=root/'home';desk.SWITCH=desk.HOME/'.local/state/pao/rack/actions.disabled'\n"
          +"desk.HOST_CONTROLLER=root/'control.py'\nprocesses=[]\n"
          +"def runner(argv,**kw):\n p=subprocess.Popen(argv,**kw);processes.append(p);return p\n"
          +"answer,code=desk.handle(sys.stdin.buffer.read(),runner)\n"
          +"assert all(p.poll() is not None for p in processes)\n"
          +"(root/'reaped.json').write_text(json.dumps(dict(children=len(processes),reaped=True)))\n"
          +"print(json.dumps(answer));sys.exit(code)\n")
        self.processes=[];self.argv=[]
        def runner(argv,**kw):
            self.argv.append(argv)
            expected=[a.replace('~/',str(self.root)+'/') for a in accion.DESK_ARGV]
            self.assertEqual(argv,expected)
            process=subprocess.Popen(['/usr/bin/python3','-I',str(shim)],**kw)
            self.processes.append(process);return process
        host=dict(id='desk',transport='ssh',actions=dict(transport='ssh',reason=None,
                  controllers=copy.deepcopy(accion.CONTROLLERS),argv=list(accion.DESK_ARGV)))
        payload=dict(now=1794664799.,hosts=[dict(id='desk',fresh=True,age=0,snapshot={})])
        self.actions=accion.Actions([host],self.root/'rack',lambda:payload,
                                   accion.Executor(home=self.root,runner=runner),clock=lambda:1794664799.)
        self.addCleanup(self.reaped)

    def reaped(self):
        self.assertTrue(all(p.poll() is not None for p in self.processes))
        if self.processes:
            self.assertTrue(json.loads((self.root/'reaped.json').read_text())['reaped'])

    def calls(self):
        target=self.root/'pueue.calls'
        return [json.loads(line) for line in target.read_text().splitlines()] if target.exists() else []

    def capabilities(self):
        return self.actions.post(dict(schema=accion.SCHEMA,op='host_capabilities',host='desk'))[1]

    def request(self, ident='forced-slots-0001', delta=-1):
        v=self.capabilities()
        return dict(schema=accion.SCHEMA,op='slots',host='desk',action_id=ident,dry_run=False,
                    expected={k:v[k] for k in ('revision','policy_hash','generation','mode','slots')},
                    change=dict(group='jale',delta=delta))

    def test_end_to_end_forced_slots_undo_receipts_and_ensayo(self):
        caps=self.capabilities()
        self.assertEqual((caps.get('status'),caps.get('slots'),caps.get('cap')),('ok',self.table,18))
        self.assertTrue(caps['capabilities']['slots'])
        r=self.request()
        self.assertEqual(self.actions.post(dict(r,action_id='forced-trial-001',dry_run=True))[1]['status'],'dry_run')
        self.assertEqual(self.calls(),[])
        code,answer=self.actions.post(r)
        self.assertEqual((code,answer['status']),(200,'applied'))
        self.assertEqual(answer['result']['expires_at'],1794664800.)
        self.assertEqual(self.calls(),[['parallel','jale',6]])
        self.assertEqual(self.actions.get(r['action_id'])[1],answer)
        self.assertEqual(self.actions.post(r)[1],answer)
        self.assertEqual(len(self.calls()),1)
        undo=self.request('forced-undo-0001');del undo['change'];undo.update(op='undo',undo_of=r['action_id'])
        self.assertEqual(self.actions.post(undo)[1]['status'],'applied')
        self.assertEqual(self.calls(),[['parallel','jale',6],['parallel','jale',7]])
        seen=json.loads((self.root/'seen.json').read_text())
        self.assertEqual((seen['argv'][1:],seen['isolated']),(['rack','--host','desk'],1))
        self.assertEqual(json.loads((self.root/'reaped.json').read_text()),dict(children=1,reaped=True))

    def test_unadopted_and_missing_controller_degrade_without_effects(self):
        # Only our synthetic authority is removed; no shared daemon is contacted.
        for path in self.directory.iterdir():path.unlink()
        self.directory.rmdir()
        caps=self.capabilities()
        self.assertEqual(caps['status'],'unsupported')
        self.assertFalse(caps['capabilities']['slots'])
        r=dict(schema=accion.SCHEMA,op='slots',host='desk',action_id='forced-absent-01',dry_run=False,
               expected=dict(revision=1,policy_hash=control.DESK_POLICY_HASH,generation='linux-proc-v1:'+'d'*64,mode='noche',slots=self.table),
               change=dict(group='jale',delta=-1))
        self.assertEqual(self.actions.post(r)[1]['status'],'unsupported')
        self.assertFalse(self.directory.exists())
        self.controller.unlink()
        self.assertEqual(self.capabilities()['status'],'unsupported')
        self.assertEqual(self.calls(),[])

    def test_forced_channel_cap_and_host_switch_reject_before_effect(self):
        code,answer=self.actions.post(self.request(delta=1))
        self.assertEqual((code,answer['status'],(answer.get('error') or {}).get('code')),
                         (409,'rejected','invalid_request'))
        switch=self.directory/'slots.disabled';switch.touch()
        r=dict(schema=accion.SCHEMA,op='slots',host='desk',action_id='forced-disabled1',dry_run=False,
               expected={k:self.engine.view()[k] for k in ('revision','policy_hash','generation','mode','slots')},
               change=dict(group='jale',delta=-1))
        self.assertEqual(self.actions.post(r)[1]['error']['code'],'disabled')
        self.assertEqual(self.calls(),[])


if __name__=='__main__':unittest.main()
