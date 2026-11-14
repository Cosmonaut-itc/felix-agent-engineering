"""U3.1 channel integration; never invoke installed controllers or shared hosts."""
import safety
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock
import test_actions

accion = test_actions.accion
BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('slots_control', BASE.parent/'pueue-desk/control.py')
control = importlib.util.module_from_spec(spec); spec.loader.exec_module(control)


class FakePueue:
    def __init__(self):
        self.groups = {g:dict(status='Running',parallel_tasks=n) for g,n in control.MINI_PROFILE.items()}
        self.calls = []
    def status(self): return dict(tasks={},groups=copy.deepcopy(self.groups))
    def parallel(self, group, n):
        self.calls.append((group,n)); self.groups[group]['parallel_tasks'] = n


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='rack-u31-',dir='/tmp'); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.p = FakePueue()
        self.host = dict(id='mini',transport='local',actions=dict(transport='local',reason=None,
                    controllers={'remote-run':accion.CONTROLLERS['remote-run']}))
        self.engine = control.MiniControl(self.p,self.root/'host',clock=lambda:1000,generation=lambda:'daemon-1')
        with control.locked(self.engine.directory): self.engine.adopt()
        self.calls = []; self.payload = dict(now=1000,hosts=[dict(id='mini',fresh=True,age=0,snapshot={})])
        def executor(host,target,request,timeout):
            self.assertEqual(target,'host-control'); self.assertLessEqual(timeout,10)
            self.calls.append(copy.deepcopy(request))
            with control.locked(self.engine.directory): return self.engine.handle(request)
        self.executor = executor
        self.actions = accion.Actions([self.host],self.root/'rack',lambda:self.payload,executor,clock=lambda:1000)

    def request(self, ident='slots-00000001', dry_run=False):
        view = self.engine.view()
        return dict(schema=accion.SCHEMA,op='slots',host='mini',action_id=ident,dry_run=dry_run,
                    expected={k:view[k] for k in ('revision','generation','policy_hash','mode','slots')},
                    change=dict(group='suites',delta=-1))

    def test_public_host_variants_closed_without_breaking_cancel(self):
        request = self.request(); self.assertEqual(accion.validate(request), request)
        for key,value in (('actor','Felix'),('launcher','remote-run'),('ref','anything'),('origin','rack-panel')):
            with self.assertRaises(ValueError): accion.validate(dict(request,**{key:value}))
        for value in (True,0,9):
            changed=copy.deepcopy(request);changed['expected']['slots']['suites']=value
            with self.assertRaises(ValueError): accion.validate(changed)
        changed=copy.deepcopy(request);changed['change']['delta']=True
        with self.assertRaises(ValueError): accion.validate(changed)
        for op in ('mode','reorder'):
            with self.assertRaises(ValueError): accion.validate(dict(request,op=op))
        for status in ('partial','applying'):
            cancel=test_actions.request()
            wire=dict(schema='rack-cancel@1',ref=cancel['ref'],action_id=cancel['action_id'])
            with self.assertRaises(ValueError):accion.sanitize(cancel,test_actions.receipt(wire,status=status),'cancel')

    def test_post_get_undo_and_unknown_do_not_repeat_effect(self):
        request = self.request(); code, answer = self.actions.post(request)
        self.assertEqual((code,answer['status']), (200,'applied')); self.assertEqual(self.p.calls, [('suites',2)])
        self.assertEqual(self.actions.post(request)[1],answer); self.assertEqual(self.actions.get(request['action_id'])[1],answer)
        self.assertEqual(len(self.calls),1)
        changed=copy.deepcopy(request);changed['change']['group']='rapidas'
        self.assertEqual(self.actions.post(changed)[0],409)
        undo=self.request('undo-00000001');undo.update(op='undo',undo_of=request['action_id']);del undo['change']
        self.assertEqual(self.actions.post(undo)[1]['status'],'applied');self.assertEqual(self.p.calls,[('suites',2),('suites',3)])

    def test_dry_run_and_capabilities_do_not_create_receipts_or_recover(self):
        before=self.engine.path.read_bytes()
        self.assertEqual(self.actions.post(self.request(dry_run=True))[1]['status'],'dry_run')
        _,answer=self.actions.post(dict(schema=accion.SCHEMA,op='host_capabilities',host='mini'))
        self.assertTrue(answer['capabilities']['slots']);self.assertFalse(answer['capabilities']['mode'])
        self.assertFalse(answer['capabilities']['restart_queue'])
        with self.actions.connect() as db: self.assertEqual(db.execute('SELECT count(*) FROM actions').fetchone()[0],0)
        self.assertEqual(self.engine.path.read_bytes(),before);self.assertEqual(self.p.calls,[])

    def test_lost_transport_answer_recovered_only_by_status_across_rack_restart(self):
        def lose(host,target,request,timeout):
            self.executor(host,target,request,timeout);raise OSError('lost answer')
        self.actions.executor=lose;request=self.request()
        self.assertEqual(self.actions.post(request)[1]['status'],'unknown')
        self.assertEqual(self.actions.post(request)[1]['status'],'unknown')
        restarted=accion.Actions([self.host],self.root/'rack',lambda:self.payload,self.executor,clock=lambda:1000)
        self.assertEqual(restarted.get(request['action_id'])[1]['status'],'applied')
        self.assertEqual([r['op'] for r in self.calls],['slots','status']);self.assertEqual(self.p.calls,[('suites',2)])

    def test_unknown_host_receipt_is_not_repeated_and_blocks_other_host_mutation(self):
        def unknown(host,target,request,timeout):
            self.calls.append(request)
            return dict(schema='rack-host-result@1',action_id=request.get('action_id'),status='unknown')
        self.actions.executor=unknown;request=self.request();self.actions.post(request)
        for _ in range(3): self.assertEqual(self.actions.get(request['action_id'])[1]['status'],'unknown')
        self.assertEqual([r['op'] for r in self.calls],['slots','status','status','status'])
        self.assertEqual(self.actions.post(self.request('slots-00000002'))[0],409);self.assertEqual(len(self.calls),4)

    def test_host_downgrade_cannot_turn_uncertain_status_into_terminal_unsupported(self):
        self.actions.executor=lambda host,target,request,timeout: dict(schema='rack-host-result@1',
            action_id=request.get('action_id'),status='unknown')
        request=self.request();self.actions.post(request)
        self.actions.executor=lambda *a,**k:dict(schema='rack-cancel-result@1',error=dict(code='invalid_request'))
        self.assertEqual(self.actions.get(request['action_id'])[1]['status'],'unknown')
        self.assertEqual(self.p.calls,[])


    def test_lost_durable_rejection_recovers_terminal_without_resend(self):
        request = self.request()
        request['expected']['revision'] = 99
        def lose(host, target, wire, timeout):
            self.executor(host, target, wire, timeout)
            raise OSError('lost rejection only')
        self.actions.executor = lose
        self.assertEqual(self.actions.post(request)[1]['status'], 'unknown')
        self.actions.executor = self.executor
        code, answer = self.actions.get(request['action_id'])
        self.assertEqual((code, answer['status']), (409, 'conflict'))
        self.assertEqual(self.actions.get(request['action_id']), (code, answer))
        self.assertEqual(self.actions.post(request), (code, answer))
        self.assertEqual([r['op'] for r in self.calls], ['slots', 'status'])
        self.assertEqual(self.p.calls, [])
        self.assertEqual(self.actions.post(self.request('slots-00000002'))[1]['status'], 'applied')

    def test_unbound_or_effectful_terminal_and_generic_unsupported_remain_unknown(self):
        request = self.request()
        self.actions.executor = lambda *a, **k: dict(schema='rack-host-result@1',
            action_id=request['action_id'], status='unknown')
        self.actions.post(request)
        wire = {k:v for k,v in request.items() if k not in ('schema','host')}
        wire['schema'] = control.HOST_SCHEMA
        import hashlib
        digest = hashlib.sha256(control.canonical(dict(host='mini', request=wire)).encode()).hexdigest()
        for status, binding, effects in (('conflict', None, False), ('rejected', '0'*64, False),
                                         ('rejected', digest, True), ('unsupported', digest, False)):
            self.actions.executor = lambda *a, **k: dict(schema='rack-host-result@1',
                action_id=request['action_id'],status=status,receipt_digest=binding,effects=effects)
            self.assertEqual(self.actions.get(request['action_id'])[1]['status'], 'unknown')
        self.assertEqual(self.p.calls, [])

    def test_host_http_conflict_capacity_applying_and_duplicates(self):
        stale = self.request('stale-00000001'); stale['expected']['revision'] = 99
        self.assertEqual(self.actions.post(stale)[0], 409)
        self.assertEqual(self.actions.post(stale)[0], 409)
        self.assertEqual(self.actions.get(stale['action_id'])[0], 409)
        full = self.request('full-000000001'); full['change']['delta'] = 1
        self.assertEqual(self.actions.post(full)[0], 409)
        self.assertEqual(self.actions.post(full)[0], 409)
        self.assertEqual(self.actions.get(full['action_id'])[0], 409)
        self.actions.executor = lambda host,target,wire,timeout: dict(schema='rack-host-result@1',
            action_id=wire['action_id'],status='applying')
        pending = self.request('pending-0000001')
        self.assertEqual(self.actions.post(pending)[0], 202)
        self.assertEqual(self.actions.post(pending)[0], 202)
        self.assertEqual(self.actions.get(pending['action_id'])[0], 202)

    def test_cancel_mutation_anchor_is_unique_and_targets_original_channel(self):
        source = (BASE/'accion.py').read_text()
        anchor = "if remaining < MIN_TRANSPORT:\n            return result(request"
        self.assertEqual(source.count(anchor), 1)
        # A-W4: the old single-line anchor appears in both channels.
        import runpy
        campaign = runpy.run_path(str(BASE/'tests/check_mutations.py'))
        case = next(c for c in campaign['CASES'] if c[0] == 'request-minimum')
        self.assertEqual(case[2], anchor)

    def test_freshness_and_disabled_close_host_mutations_but_allow_status(self):
        self.payload['hosts'][0]['fresh']=False
        self.assertEqual(self.actions.post(self.request())[0],409);self.assertEqual(self.calls,[])
        self.payload['hosts'][0]['fresh']=True;(self.root/'rack/actions.disabled').touch()
        self.assertEqual(self.actions.post(self.request('slots-00000002'))[0],403);self.assertEqual(self.calls,[])

    def test_desk_old_and_missing_controller_degrade_without_transport(self):
        executor=accion.Executor(home=self.root)
        self.assertEqual(executor(self.host,'host-control',{})['status'],'unsupported')
        self.assertEqual(executor(dict(self.host,id='desk',transport='ssh'),'host-control',{})['status'],'unsupported')
        old=dict(schema='rack-cancel-result@1',error=dict(code='invalid_request'))
        self.assertEqual(accion.host_channel.sanitize(self.request(),old,'slots')['status'],'unsupported')
        with self.assertRaises(ValueError): accion.host_channel.sanitize(self.request(),{},'slots')

    def test_fixed_local_controller_path_isolated_stdin_and_no_fallback(self):
        controller=self.root/'.local/lib/pao-pueue-mini/control.py';controller.parent.mkdir(parents=True)
        controller.write_text('import json,sys\nr=json.load(sys.stdin)\nprint(json.dumps(dict(schema="rack-host-result@1",status="unknown",action_id=r["action_id"])))\n')
        calls=[]
        def runner(argv,**kwargs):
            calls.append((argv,kwargs));return subprocess.Popen(argv,**kwargs)
        response=accion.Executor(home=self.root,runner=runner)(self.host,'host-control',dict(schema=control.HOST_SCHEMA,op='status',action_id='slots-00000001'),timeout=3)
        self.assertEqual(response['status'],'unknown')
        self.assertEqual(calls[0][0],['/usr/bin/python3','-I',str(controller),'rack','--host','mini'])
        self.assertTrue(calls[0][1]['start_new_session']);self.assertEqual(calls[0][1]['cwd'],'/')

    def test_installed_controller_is_forbidden_by_audit_hook(self):
        error = None
        try:
            subprocess.run(['/usr/bin/python3', '-I', '/not-a-fixture/control.py'], check=True, capture_output=True)
        except (AssertionError, subprocess.CalledProcessError) as caught:
            error = caught
        self.assertIsInstance(error, AssertionError)
        self.assertIn('real host binary: control.py', str(error))

    def test_sanitize_observed_desired_and_no_private_output(self):
        answer=self.actions.post(self.request())[1]
        self.assertEqual(answer['result']['observed']['suites'],2);self.assertEqual(answer['result']['desired']['suites'],2)
        self.assertNotIn('before_overrides',json.dumps(answer))
        value=self.engine.view();value.update(command='private',path='private',actor='private')
        self.assertNotIn('private',json.dumps(accion.host_channel.control(value)))
        value['slots']['suites']=True
        with self.assertRaises(ValueError): accion.host_channel.control(value)

    def test_host_http_uses_same_fresh_owner_host_origin_and_closed_json_gate(self):
        self.owner=Mock();self.owner.allows.return_value=True;self.data=self.payload
        handler=lambda **headers: test_actions.AuthorizationTests.handler(self, self.request(), **headers)
        for headers in ({'Origin':None},{'Origin':'http://evil:8788'},{'Host':'evil:8788'},
                        {'X-Rack-Action':None},{'Tailscale-Funnel-Request':'?1'}):
            self.assertEqual(handler(**headers)[0],403)
        self.assertEqual(self.p.calls,[])
        code,answer=handler()
        self.assertEqual((code,answer['status']),(200,'applied'))
        self.owner.allows.assert_called_with('100.100.0.2',fresh=True)
        self.owner.allows.return_value=False
        self.assertEqual(handler()[0],403)

    def test_production_ui_slots(self):
        fixture=dict(now=1000,tickets={},hosts=[dict(id='mini',name='Mac Mini',fresh=True,age=0,estimates={},actions=dict(reason=None),
            snapshot=dict(control=self.engine.view(),groups=[dict(name=g,slots=n,status='Running') for g,n in control.MINI_PROFILE.items()],
            tasks=[],cpu=dict(cores=10,load=[1,1,1]),mem=dict(total=16*1024**3,available=12*1024**3,swap_total=0,swap_used=0)))])
        source=(BASE/'index.html').read_text().split('<script>',1)[1].split('</script>',1)[0]
        result=subprocess.run(['node',str(BASE/'tests/slots-ui-check.js')],input=json.dumps(dict(js=source,fixture=fixture)),text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr);self.assertIn('UI slots OK',result.stdout)


if __name__=='__main__': unittest.main()
