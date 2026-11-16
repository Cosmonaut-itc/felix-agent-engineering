"""U3.6 public channel / production UI, synthetic hosts only."""
import safety
import copy
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
import test_slots
import test_actions

c=test_slots.control
a=test_slots.accion
BASE=Path(__file__).resolve().parents[1]


class ReorderChannelTests(unittest.TestCase):
    def setUp(self):
        test_slots.ChannelTests.setUp(self)
        self.tasks={str(i):dict(id=i,created_at='2026-11-16T12:00:00+00:00',group='suites',
            original_command='true',command='true',path='/synthetic',envs={'PRIVATE_SENTINEL':'hidden'},
            dependencies=[],label=None,priority=-i,status={'Queued':dict(enqueued_at='2026-11-16T12:00:00+00:00')}) for i in range(1,6)}
        self.p.status=lambda:dict(groups=copy.deepcopy(self.p.groups),tasks=copy.deepcopy(self.tasks))
        self.p.call=lambda *args:self.p.groups['suites'].update(status='Paused' if args[0]=='pause' else 'Running')
        self.p.binary='fake-no-real-binary'
        self.engine.reorder_aliases=lambda state:{}
        self.engine.coordinated_reorder=True
        self.engine.reorder_host_ready=lambda state:True
        self.engine.reorder_overhead=0;self.engine.reorder_cost=.001
        def edit(action,ident,priority,deadline):
            self.engine.reorder_save(action,'edit_intent');self.tasks[str(ident)]['priority']=priority
            self.engine.reorder_save(action,'editing')
        self.engine.edit_priority=edit
        self.pressure=patch.object(c,'pressure_mini',return_value=dict(load=0,memory=1))
        self.pressure.start();self.addCleanup(self.pressure.stop)
        self.payload['hosts'][0]['snapshot']['control']=self.engine.view()

    def request(self,ident='public-order-0001',undo=None,dry_run=False):
        expected=self.engine.view()['reorder_groups']['suites']['expected']
        return dict(schema='rack-action@1',host='mini',op='undo' if undo else 'reorder',action_id=ident,
            dry_run=dry_run,expected=expected,**(dict(undo_of=undo) if undo else dict(change=dict(order=[1,5,2,3,4]))))

    def test_public_pipeline_and_exact_undo_opaque_only(self):
        r=self.request();answer=self.actions.post(r)[1]
        self.assertEqual(answer['status'],'applied',answer)
        self.assertEqual([row['id'] for row in answer['result']['desired']],[1,5,2,3,4])
        for private in ('PRIVATE_SENTINEL','/synthetic','command','enqueued_at','process'):
            self.assertNotIn(private,json.dumps(answer))
        self.assertEqual(self.actions.post(r)[1],answer)
        self.assertEqual(self.actions.get(r['action_id'])[1],answer)
        self.assertEqual([r['op'] for r in self.calls],['capabilities','reorder'])
        undo=self.actions.post(self.request('public-undo-00001',undo=r['action_id']))[1]
        self.assertEqual(undo['status'],'applied',undo)
        self.assertEqual([self.tasks[str(i)]['priority'] for i in range(1,6)],[-1,-2,-3,-4,-5])

    def test_unknown_get_no_replay_even_disabled(self):
        original=self.executor
        def lost(host,target,wire,timeout):
            answer=original(host,target,wire,timeout)
            if wire['op']!='capabilities':raise OSError('lost')
            return answer
        self.actions.executor=lost;r=self.request()
        self.assertEqual(self.actions.post(r)[1]['status'],'unknown')
        (self.root/'rack/actions.disabled').touch();self.actions.executor=original
        self.assertEqual(self.actions.get(r['action_id'])[1]['status'],'applied')
        self.assertEqual([call['op'] for call in self.calls],['capabilities','reorder','status'])

    def test_cancel_and_order_reserve_same_host(self):
        r=self.request()
        prior=test_actions.request();prior['host']='mini';prior['action_id']='pending-cancel-0001'
        with self.actions.connect() as db:
            db.execute('INSERT INTO actions VALUES (?,?,?,?,?,?,?)',(prior['action_id'],'opaque',json.dumps(prior),json.dumps(dict(status='unknown')),1000,1000,None))
            self.assertIsNotNone(self.actions.pending_reason(db,r))
        self.assertEqual(self.calls,[])

    def test_closed_fields_budget_and_old_host_translation(self):
        r=self.request();self.assertEqual(a.validate(r),r)
        for key,value in (('script','bad'),('path','bad'),('actor','Felix'),('launcher','remote-run')):
            with self.assertRaises(ValueError):a.validate(dict(r,**{key:value}))
        old=dict(schema='rack-cancel-result@1',error=dict(code='invalid_request'))
        self.assertEqual(a.host_channel.sanitize(r,old,'reorder')['status'],'unsupported')
        old=dict(schema='rack-host-result@1',action_id=r['action_id'],status='rejected',error=dict(code='invalid_request'))
        self.assertEqual(a.host_channel.sanitize(r,old,'reorder')['status'],'unsupported')
        undo=dict(r,op='undo',undo_of='previous-order-001');undo.pop('change')
        self.assertEqual(a.host_channel.sanitize(undo,old,'undo')['status'],'unsupported')
        with self.assertRaises(ValueError):a.decode(json.dumps(r).encode()+b' '*16384)
        r['expected']['queue'][0]['id']=True
        with self.assertRaises(ValueError):a.validate(r)

    def test_real_old_host_reorder_and_queue_undo_fail_before_mutation(self):
        fixture=json.loads((BASE/'tests/old-host-u36.json').read_text())
        self.assertTrue(fixture['source'].startswith('66e2c1e'))
        for op in ('reorder','undo'):
            r=self.request('old-actual-'+op+'-001',undo='previous-order-001' if op=='undo' else None)
            seen=[]
            def old(host,target,wire,timeout):
                seen.append(wire['op'])
                return fixture['capabilities'] if wire['op']=='capabilities' else fixture['responses'][op]
            self.actions.executor=old
            result=self.actions.post(r)[1]
            self.assertEqual(result['status'],'unsupported',result)
            self.assertIs(result['effects'],False)
            self.assertEqual(seen,['capabilities'])
            self.assertEqual(self.actions.get(r['action_id'])[1]['status'],'unsupported')
        self.assertEqual(self.p.calls if hasattr(self.p,'calls') else [],[])

    def test_current_unknown_after_probe_and_probe_failure_remain_unknown(self):
        original=self.executor
        for case in ('uncertain','probe_timeout','corrupt'):
            r=self.request('current-'+case+'-001')
            def executor(host,target,wire,timeout):
                if wire['op']=='capabilities':
                    if case=='probe_timeout':raise TimeoutError('slow')
                    if case=='corrupt':return {'broken':True}
                    return original(host,target,wire,timeout)
                return dict(schema='rack-host-result@1',action_id=r['action_id'],status='unknown',
                            error=dict(code='recovery_error'))
            self.actions.executor=executor
            self.assertEqual(self.actions.call(r,r['op'],self.actions.timer()+20)['status'],'unknown')

    def test_old_capabilities_without_reorder_metadata_never_dispatch(self):
        self.payload['hosts'][0]['snapshot']['control'].pop('reorder_groups')
        result=self.actions.post(self.request())[1]
        self.assertEqual((result.get('error') or {}).get('code'),'unsupported',result)
        self.assertEqual(self.calls,[])

    def test_view_sanitization_and_disabled_reason(self):
        v=a.host_channel.control(self.engine.view())
        self.assertTrue(v['capabilities']['reorder'])
        self.assertEqual(set(v['reorder_groups']),set(c.MINI_PROFILE))
        self.engine.coordinated_reorder=False
        v=a.host_channel.control(self.engine.view())
        self.assertFalse(v['capabilities']['reorder'])
        self.assertIn('coordinación',v['reorder_groups']['suites']['reason'])
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(v))

    def test_http_owner_host_and_origin_fail_closed(self):
        from unittest.mock import Mock
        self.owner=Mock();self.owner.allows.return_value=True;self.data=self.payload
        handler=lambda **headers:test_actions.AuthorizationTests.handler(self,self.request(),**headers)
        for headers in ({'Origin':None},{'Origin':'http://evil:8788'},{'Host':'evil:8788'},{'X-Rack-Action':None}):
            self.assertEqual(handler(**headers)[0],403)
        self.assertEqual(self.calls,[])
        self.assertEqual(handler()[1]['status'],'applied')
        self.owner.allows.assert_called_with('100.100.0.2',fresh=True)

    def test_64_receipt_crosses_transport_and_cancel_keeps_16k(self):
        self.tasks={str(i):dict(copy.deepcopy(self.tasks['1']),id=i,priority=-i) for i in range(1,65)}
        self.engine.reorder_cost=.001
        self.engine.reorder_budget=60  # serialization/transport test, not a timing benchmark
        r=self.request();r['change']['order']=list(range(64,0,-1))
        answer=self.actions.post(r)[1]
        self.assertEqual(answer['status'],'applied',answer)
        self.assertGreater(len(json.dumps(answer)),16384)
        controller=self.root/'.local/lib/pao-pueue-mini/control.py';controller.parent.mkdir(parents=True)
        wire_answer=dict(answer,schema='rack-host-result@1')
        controller.write_text('import json\nprint('+repr(json.dumps(wire_answer))+')\n')
        wire=dict(schema='rack-host-control@1',op='status',action_id=r['action_id'])
        try:result=a.Executor(home=self.root)(self.host,'host-control',wire,timeout=3)
        except Exception as error:self.fail('El recibo válido de 64 filas debe atravesar el transporte: '+str(error))
        self.assertEqual(result['status'],'applied')
        self.assertEqual(len(result['result']['before']),64)
        with self.assertRaises(ValueError):
            a.desk_channel.exchange(['/usr/bin/python3','-I',str(controller)],dict(schema='rack-cancel@1',op='status'),subprocess.Popen,{},3)

    def test_recovery_age_chain_identity_and_readonly(self):
        import sqlite3
        from test_rack import snapshot
        home=self.root/'age-home';path=home/'.local/state/pao/pueue-mini/control.sqlite3';path.parent.mkdir(parents=True)
        task=self.tasks['1'];identity=c.MiniControl.task_identity(task)
        stamps=['2026-11-16T12:00:00Z','2026-11-16T12:01:00Z','2026-11-16T12:02:00Z']
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE host_actions (body TEXT)')
            for before,after in zip(stamps,stamps[1:]):
                db.execute('INSERT INTO host_actions VALUES (?)',(json.dumps(dict(kind='reorder',recovered_enqueued={'1':dict(identity=identity,before=before,after=after)})),))
        before=path.read_bytes();links=snapshot.mini_reorder_ages(home)
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(snapshot.original_queue_age(task,stamps[-1],links),snapshot.epoch(stamps[0]))
        changed=dict(task,command='different')
        self.assertEqual(snapshot.original_queue_age(changed,stamps[-1],links),snapshot.epoch(stamps[-1]))
        self.assertEqual(snapshot.original_queue_age(task,'2026-11-16T12:03:00Z',links),snapshot.epoch('2026-11-16T12:03:00Z'))

    def test_production_ui_reorder(self):
        fixture=browser_fixture(self.engine.view())
        source=(BASE/'index.html').read_text().split('<script>',1)[1].split('</script>',1)[0]
        result=subprocess.run(['node',str(BASE/'tests/reorder-ui-check.js')],input=json.dumps(dict(js=source,fixture=fixture)),text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('UI reorder OK',result.stdout)


def browser_fixture(control=None):
    now=1794830400
    rows=[dict(id=i,group='suites',state='Queued',name='dirigida-contrato',ticket='SEN-'+str(6400+i),
               subject='Contrato de fila '+str(i),priority=-i,enqueued=now-90*i,start=None,end=None,result=None,
               scope=None,repo=None,sha=None,branch='feat/fila-'+str(i),project='Hasito',
               control=dict(launcher='remote-run',ref='20261116-120000-abcdef',expected=dict(task_id=i,state='Queued'),reason=None)) for i in range(1,6)]
    if control is None:
        queue=[dict(id=i,identity=str(i)*64,priority=-i) for i in range(1,6)]
        expected=dict(revision=1,admission_revision=1,policy_hash='a'*64,generation='synthetic',group='suites',queue_etag='b'*64,queue=queue)
        control=dict(schema='rack-host-result@1',status='ok',capabilities=dict(slots=True,undo=True,dry_run=True,mode=False,reorder=True,restart_queue=False),
            revision=1,policy_hash='a'*64,generation='synthetic',mode='fijo',cap=7,slots=dict(c.MINI_PROFILE),desired=dict(c.MINI_PROFILE),
            drift=False,recovery_pending=False,pause_reasons={},reorder_groups={'suites':dict(enabled=True,reason=None,owner=None,expected=expected)})
    snapshot=dict(schema='rack-host@1',host='mini',taken=now,control=control,tasks=rows,
        groups=[dict(name=g,slots=n,status='Running') for g,n in c.MINI_PROFILE.items()],cpu=dict(cores=10,load=[1,1,1]),
        mem=dict(total=16*1024**3,available=12*1024**3,swap_total=0,swap_used=0))
    return dict(schema='rack@1',now=now,tickets={},hosts=[dict(id='mini',name='Mac Mini',fresh=True,age=0,error=None,
         estimates={},actions=dict(reason=None),snapshot=snapshot)])


if __name__=='__main__':unittest.main()
