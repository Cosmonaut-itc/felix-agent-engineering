"""U3.2: complete desk path with fake SSH, private controllers and no real binaries."""
import safety
import copy
import io
import json
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
        self.engine=control.MiniControl(self.p,self.root/'host',clock=lambda:self.now,generation=lambda:'desk-1',host='desk')
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

    def test_desk_authorization_still_fresh_owner_host_origin(self):
        self.owner=Mock();self.owner.allows.return_value=True;self.data=self.payload
        for headers in ({'Origin':None},{'Origin':'http://evil:8788'},{'Host':'evil:8788'},{'X-Rack-Action':None}):
            self.assertEqual(test_actions.AuthorizationTests.handler(self,self.request(),**headers)[0],403)
        self.assertEqual(self.p.calls,[])
        self.assertEqual(test_actions.AuthorizationTests.handler(self,self.request())[0],200)
        self.owner.allows.assert_called_with('100.100.0.2',fresh=True)


if __name__=='__main__':unittest.main()
