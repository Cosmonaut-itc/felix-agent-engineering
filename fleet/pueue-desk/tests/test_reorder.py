"""U3.6: only synthetic tasks, fake Pueue/processes and a private SQLite file."""
import copy
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
import test_slots
control = test_slots.control

STAMP = '2026-11-16T12:00:00+00:00'


def task(i, priority=0, group='suites'):
    return dict(id=i, created_at=STAMP, group=group, command='true', original_command='true',
                path='/synthetic', envs={'SYNTHETIC':'sí'}, dependencies=[], label=None,
                priority=priority, status={'Queued':dict(enqueued_at=STAMP)})


class Crash(BaseException):
    pass


class ReorderTests(test_slots.SlotsTests):
    # Do not inherit the existing slots cases into this directed test class.
    pass


for _name in dir(test_slots.SlotsTests):
    if _name.startswith('test_'):
        setattr(ReorderTests, _name, None)


def setup(self):
    test_slots.SlotsTests.setUp(self)
    self.change_state(pid_file=str(self.root/'pueue.pid'))
    old_call=self.p.call
    def call(*args):
        if args[0]=='enqueue':
            self.p.calls.append(args)
            for ident in args[1:]:self.tasks[ident]['status']={'Queued':dict(enqueued_at=STAMP)}
            return ''
        return old_call(*args)
    self.p.call=call
    self.tasks = {str(i):task(i,p) for i,p in enumerate([0,-2,-2,-8,-12],1)}
    self.p.status = lambda: dict(groups=copy.deepcopy(self.p.groups),tasks=copy.deepcopy(self.tasks))
    self.engine.reorder_aliases=lambda state:{}
    self.engine.coordinated_reorder = True
    self.engine.reorder_host_ready=lambda state:True
    self.engine.reorder_overhead = 0
    self.engine.reorder_cost = .001
    self.engine.edit_priority = lambda *a: fake_edit(self,*a)
    self.pressure = patch.object(control,'pressure_mini',return_value=dict(load=0,memory=1))
    self.pressure.start(); self.addCleanup(self.pressure.stop)


def fake_edit(self, action, ident, priority, deadline):
    if set(self.tasks[str(ident)]['status'])!={'Queued'}:raise ValueError('fake stale task')
    action['editing_id'] = ident
    self.engine.reorder_save(action,'edit_intent')
    t=self.tasks[str(ident)]; t['status']={'Locked':dict(previous_status=t['status'])}
    self.engine.reorder_boundary('client_sent')
    t['priority']=priority; t['status']=t['status']['Locked']['previous_status']
    self.engine.reorder_boundary('edit_effect')
    self.engine.reorder_save(action,'editing')


ReorderTests.setUp = setup


def request(self, ident='reorder-0000001', undo=None, dry=False):
    view=self.engine.view()['reorder_groups']['suites']
    expected=view['expected']
    order=[r['id'] for r in expected['queue']]
    if not undo:order.insert(1,order.pop(4))
    return dict(schema=control.HOST_SCHEMA, op='undo' if undo else 'reorder',action_id=ident,
                dry_run=dry,expected=expected, **(dict(undo_of=undo) if undo else dict(change=dict(order=order))))


ReorderTests.order_request=request


def test_normal_and_exact_undo(self):
    r=self.order_request();before=copy.deepcopy(self.tasks)
    answer=self.engine.handle(r)
    self.assertEqual(answer['status'],'applied',answer)
    self.assertEqual([r['id'] for r in answer['result']['desired']],[1,5,2,3,4])
    self.assertEqual([r['priority'] for r in answer['result']['before']],[0,-2,-2,-8,-12])
    self.assertEqual(self.p.calls,[('pause','--wait','-g','suites'),('start','-g','suites')])
    self.assertEqual(self.engine.handle(r)['status'],'applied')
    self.assertEqual(len(self.p.calls),2)
    for i,t in before.items():
        self.assertEqual({k:v for k,v in t.items() if k!='priority'}, {k:v for k,v in self.tasks[i].items() if k!='priority'})
    undo=self.engine.handle(self.order_request('undo-order-0001',undo=r['action_id']))
    self.assertEqual(undo['status'],'applied',undo)
    self.assertEqual(self.tasks,before)


def test_every_boundary_recovers_and_duplicate_is_historic(self):
    phases=['prepared','pause_effect','paused','edit_intent','client_sent','edit_effect','editing','edited','releasing','start_effect','terminal']
    initial=copy.deepcopy(self.tasks)
    for n,phase in enumerate(phases):
        self.tasks=copy.deepcopy(initial);self.p.groups['suites']['status']='Running'
        ident='boundary-'+str(n).zfill(8)
        r=self.order_request(ident)
        def boundary(p):
            if p==phase:
                raise Crash(p)
        self.engine.reorder_boundary=boundary
        with self.assertRaises(Crash):self.engine.handle(r)
        self.engine.reorder_boundary=lambda p:None
        for _ in range(2):self.assertTrue(self.engine.reconcile())
        result=self.engine.handle(r)
        self.assertIn(result['status'],('applied','aborted'),(phase,result))
        calls=len(self.p.calls)
        self.assertEqual(self.engine.handle(r),result)
        self.assertEqual(len(self.p.calls),calls)
        self.assertEqual(self.p.groups['suites']['status'],'Running',phase)
        self.assertFalse(any('Locked' in t['status'] for t in self.tasks.values()),phase)


def test_prepared_intent_precedes_pause(self):
    old=self.p.call
    def call(*args):
        if args[0]=='pause':
            self.assertEqual(self.engine.reorder_actions()[-1]['phase'],'prepared')
            with self.engine.connect() as db:self.assertEqual(self.engine.state(db)['reasons']['suites'],['reorder'])
        return old(*args)
    self.p.call=call
    self.assertEqual(self.engine.handle(self.order_request())['status'],'applied')


def test_dry_capabilities_have_no_effect_even_with_pending(self):
    r=self.order_request(dry=True);before=self.fingerprint()
    self.assertEqual(self.engine.handle(r)['status'],'dry_run')
    self.engine.view();self.assertEqual(self.fingerprint(),before);self.assertEqual(self.p.calls,[])
    self.engine.reorder_boundary=lambda p: (_ for _ in ()).throw(Crash()) if p=='prepared' else None
    with self.assertRaises(Crash):self.engine.handle(self.order_request('pending-0000001'))
    self.engine.reorder_boundary=lambda p:None
    before=self.fingerprint();self.engine.view();self.engine.handle(self.order_request('dryagain-000001',dry=True))
    self.assertEqual(self.fingerprint(),before);self.assertEqual(self.p.calls,[])


def test_paused_reasons_manual_guard_ambiguous_and_startup(self):
    self.change_state(startup_version=1,startup=dict(phase='ready',statuses={},groups={},holds={},interruptions={},source_hash='a'*64,target_hash='b'*64))
    for reason in ('pressure','manual','ambiguous','startup','handoff'):
        self.change_state(reasons={'suites':[reason]})
        self.p.groups['suites']['status']='Paused'
        r=self.order_request('paused-'+reason+'-0001')
        result=self.engine.handle(r)
        self.assertEqual((result.get('error') or {}).get('code'),'paused',result)
        self.assertIn(reason,result['pause_reasons']);self.assertEqual(self.p.calls,[])
    self.change_state(reasons={});self.p.groups['suites']['status']='Running'
    self.engine.coordinated_reorder=False
    self.assertFalse(self.engine.view()['capabilities']['reorder'])
    self.assertEqual((self.engine.handle(self.order_request('contract-000001')).get('error') or {}).get('code'),'uncoordinated')
    self.assertEqual(self.p.calls,[])


def test_concurrent_before_compare_aborts_add_cancel_and_start(self):
    base=copy.deepcopy(self.tasks)
    for index,change in enumerate(('add','cancel','start')):
        self.tasks=copy.deepcopy(base);self.p.groups['suites']['status']='Running'
        def boundary(phase):
            if phase=='pause_effect':
                if change=='add':self.tasks['8']=task(8)
                if change=='cancel':self.tasks.pop('3')
                if change=='start':self.tasks['3']['status']={'Running':dict(enqueued_at=STAMP,start=STAMP)}
        self.engine.reorder_boundary=boundary
        result=self.engine.handle(self.order_request('race-'+str(index).zfill(8)))
        self.assertEqual(result['status'],'aborted',result)
        self.assertTrue(all(t['priority']==base[i]['priority'] for i,t in self.tasks.items() if i in base))
    self.engine.reorder_boundary=lambda p:None


def test_add_after_compare_preserved_but_strict_undo_conflicts(self):
    def boundary(phase):
        if phase=='edit_intent':self.tasks['8']=task(8)
    self.engine.reorder_boundary=boundary
    r=self.order_request();answer=self.engine.handle(r)
    self.assertEqual(answer['status'],'applied',answer)
    self.assertIn('8',self.tasks);self.assertEqual(self.tasks['8']['priority'],0)
    self.engine.reorder_boundary=lambda p:None
    before=copy.deepcopy(self.tasks);calls=len(self.p.calls)
    undo=self.engine.handle(self.order_request('undo-race-0001',undo=r['action_id']))
    self.assertEqual((undo.get('error') or {}).get('code'),'undo_conflict',undo)
    self.assertEqual(self.tasks,before);self.assertEqual(len(self.p.calls),calls)


def test_new_pressure_transfers_pause_without_start(self):
    self.pressure.stop()
    with patch.object(control,'pressure_mini',return_value=dict(load=4,memory=1)):
        answer=self.engine.handle(self.order_request())
    self.assertEqual(answer['status'],'applied',answer)
    self.assertEqual(self.p.groups['suites']['status'],'Paused')
    self.assertNotIn(('start','-g','suites'),self.p.calls)
    with self.engine.connect() as db:self.assertEqual(self.engine.state(db)['reasons']['suites'],['pressure'])


def test_aliases_and_individual_paused_stashed_reject_before_pause(self):
    for scenario in ('alias','Paused','Stashed','Locked'):
        base=copy.deepcopy(self.tasks)
        r=self.order_request('unsafe-'+scenario+'-0001')
        if scenario=='alias':self.tasks['1']['command']='expanded'
        else:self.tasks['8']=dict(task(8),status={scenario:{}})
        result=self.engine.handle(r)
        self.assertIn((result.get('error') or {}).get('code'),('edit_unsafe','queue_conflict'),result)
        self.assertEqual(self.p.calls,[])
        self.tasks=base


def test_limits_permutation_i32_booleans_and_budget(self):
    for delta in ('duplicate','bool','missing','large','priority','extra'):
        r=self.order_request()
        if delta=='duplicate':r['change']['order'][0]=r['change']['order'][1]
        if delta=='bool':r['change']['order'][0]=True
        if delta=='missing':r['change']['order'].pop()
        if delta=='large':r['expected']['queue']*=13
        if delta=='priority':r['expected']['queue'][0]['priority']=2**31
        if delta=='extra':r['script']='attack'
        with self.assertRaises(ValueError):self.engine.handle(r)
    self.engine.reorder_cost=1
    result=self.engine.handle(self.order_request('budget-0000001'))
    self.assertEqual((result.get('error') or {}).get('code'),'pause_budget');self.assertEqual(self.p.calls,[])


def test_rpc_uses_remaining_common_budget_and_never_sends_after_expiry(self):
    client=control.Pueue('only-a-double');client.timeout=1;client.deadline=5
    answer=subprocess.CompletedProcess([],0,'synthetic','')
    with patch.object(control.time,'monotonic',return_value=4.8),patch.object(control.subprocess,'run',return_value=answer) as run:
        self.assertEqual(client.call('status','--json'),'synthetic')
        self.assertAlmostEqual(run.call_args.kwargs['timeout'],.2)
    with patch.object(control.time,'monotonic',return_value=5),patch.object(control.subprocess,'run') as run:
        with self.assertRaises(TimeoutError):client.call('start','-g','suites')
        run.assert_not_called()


def test_calibrated_admission_rejects_64_before_pause_and_accepts_five(self):
    self.engine.reorder_overhead=control.MiniControl.reorder_overhead
    self.engine.reorder_cost=control.MiniControl.reorder_cost
    self.assertTrue(self.engine.view()['reorder_groups']['suites']['enabled'])
    self.assertEqual(self.engine.handle(self.order_request('calibrated-five-001'))['status'],'applied')
    self.p.calls.clear()
    self.tasks={str(i):task(i) for i in range(1,65)}
    r=self.order_request('calibrated-64-001');r['change']['order']=list(range(64,0,-1))
    answer=self.engine.handle(r)
    self.assertEqual((answer.get('error') or {}).get('code'),'pause_budget',answer)
    self.assertEqual(self.p.calls,[])



def test_revocation_after_acceptance_still_recovers(self):
    self.engine.reorder_boundary=lambda p:(_ for _ in ()).throw(Crash()) if p=='client_sent' else None
    r=self.order_request()
    with self.assertRaises(Crash):self.engine.handle(r)
    (self.root/'slots.disabled').touch()
    self.engine.reorder_boundary=lambda p:None
    self.assertTrue(self.engine.reconcile())
    self.assertEqual(self.engine.handle(r)['status'],'aborted')
    self.assertEqual(self.p.groups['suites']['status'],'Running')


def test_identity_reuse_retains_and_no_resurrection_of_cancel(self):
    self.engine.reorder_boundary=lambda p:(_ for _ in ()).throw(Crash()) if p=='client_sent' else None
    with self.assertRaises(Crash):self.engine.handle(self.order_request())
    self.engine.reorder_boundary=lambda p:None
    self.tasks['1']['created_at']='2026-11-16T12:01:00+00:00'
    with self.assertRaises(ValueError):self.engine.reconcile()
    self.assertEqual(self.p.groups['suites']['status'],'Paused')
    self.assertFalse(any(a[0]=='enqueue' for a in self.p.calls))


def test_orphan_editor_pid_reuse_and_revocation(self):
    helper=control.priority_helper();root=self.root/'edit-test';root.mkdir(mode=0o700)
    a=dict(request={'action_id':'orphan-0000001'},phase='edit_intent',process=dict(root=str(root),client=dict(pid=123,birth='old')))
    with patch.object(control,'priority_helper',return_value=helper),patch.object(helper,'birth',return_value='new'),patch.object(control.os,'kill') as kill,patch.object(self.engine,'reorder_save'):
        self.assertTrue(self.engine.stop_editor(a));kill.assert_not_called()
    self.assertTrue((root/'revoked').exists())
    a['process']=dict(root=str(root),client=dict(pid=123,birth='old'))
    with patch.object(control,'priority_helper',return_value=helper),patch.object(helper,'birth',return_value='old'),patch.object(control.os,'kill') as kill:
        self.assertFalse(self.engine.stop_editor(a));kill.assert_called_once_with(123,15)


def test_offline_locked_owned_before_stock_stashed_foreign_untouched(self):
    self.engine.reorder_boundary=lambda p:(_ for _ in ()).throw(Crash()) if p=='client_sent' else None
    with self.assertRaises(Crash):self.engine.handle(self.order_request())
    self.engine.reorder_boundary=lambda p:None
    queue=self.p.status();queue['tasks']['9']=dict(task(9),status={'Stashed':dict(enqueue_at=None)})
    with self.engine.connect() as db:state=self.engine.state(db)
    image=self.engine.reorder_offline(queue,state,self.root)
    self.assertEqual(image['tasks']['1']['status'],{'Queued':dict(enqueued_at=STAMP)})
    self.assertEqual(image['tasks']['9']['status'],queue['tasks']['9']['status'])
    (self.root/'task_logs').mkdir();(self.root/'task_logs/1.log').touch()
    with self.assertRaises(ValueError):self.engine.reorder_offline(queue,state,self.root)


def test_unknown_lock_concurrent_never_enqueued_or_opened(self):
    def boundary(phase):
        if phase=='edited':self.tasks['9']=dict(task(9),status={'Locked':dict(previous_status={'Queued':dict(enqueued_at=STAMP)})})
    self.engine.reorder_boundary=boundary
    result=self.engine.handle(self.order_request())
    self.assertEqual(result['status'],'unknown')
    self.assertEqual(self.p.groups['suites']['status'],'Paused')
    self.assertNotIn(('enqueue','9'),self.p.calls)


def test_second_fresh_sensor_after_start_intent_blocks_open(self):
    self.pressure.stop()
    with patch.object(control,'pressure_mini',side_effect=[dict(load=0,memory=1),dict(load=4,memory=1)]):
        result=self.engine.handle(self.order_request())
    self.assertEqual(result['status'],'applied')
    self.assertEqual(self.p.groups['suites']['status'],'Paused')
    self.assertNotIn(('start','-g','suites'),self.p.calls)


def test_undo_stale_identity_priority_or_expiry_never_pauses(self):
    r=self.order_request();self.assertEqual(self.engine.handle(r)['status'],'applied')
    for n,scenario in enumerate(('priority','created','expired','missing','none')):
        before=copy.deepcopy(self.tasks);now=self.now
        if scenario=='priority':self.tasks['1']['priority']+=9
        if scenario=='created':self.tasks['1']['created_at']='2026-11-16T12:01:00Z'
        if scenario=='expired':self.now+=61
        if scenario=='missing':self.tasks.pop('1')
        if scenario=='none':self.tasks.clear()
        calls=len(self.p.calls);request=self.order_request('strict-undo-'+str(n).zfill(5),undo=r['action_id'])
        result=self.engine.handle(request)
        self.assertEqual((result.get('error') or {}).get('code'),'undo_conflict',result)
        self.assertEqual(len(self.p.calls),calls)
        self.tasks=before;self.now=now


def test_stale_etag_revision_policy_generation_reject_without_pause(self):
    for n,key in enumerate(('revision','admission_revision','queue_etag','policy_hash','generation')):
        r=self.order_request('stale-order-'+str(n).zfill(5))
        r['expected'][key]=999 if key in ('revision','admission_revision') else 'f'*64
        result=self.engine.handle(r)
        self.assertEqual(result['status'],'conflict',result)
        self.assertEqual(self.p.calls,[])


def test_64_pending_65_rejects_and_views_are_pure(self):
    self.tasks={str(i):task(i,-i) for i in range(1,65)}
    before=self.fingerprint()
    r=self.order_request(dry=True);r['change']['order']=list(range(64,0,-1))
    self.assertEqual(self.engine.handle(r)['status'],'dry_run')
    self.assertEqual(self.fingerprint(),before);self.assertEqual(self.p.calls,[])
    self.tasks['65']=task(65,-65)
    view=self.engine.view()['reorder_groups']['suites']
    self.assertFalse(view['enabled']);self.assertIn('demasiado larga',view['reason'])
    r['expected']['queue'].append(dict(id=65,identity=self.engine.task_identity(self.tasks['65']),priority=-65));r['change']['order'].append(65)
    with self.assertRaises(ValueError):self.engine.handle(r)
    self.assertEqual(self.p.calls,[])

def test_preparer_intent_queue_complete_boundaries_recover_owned_lock(self):
    self.engine.reorder_boundary=lambda p:(_ for _ in ()).throw(Crash()) if p=='client_sent' else None
    r=self.order_request()
    with self.assertRaises(Crash):self.engine.handle(r)
    self.engine.reorder_boundary=lambda p:None
    initial=self.p.status();initial['tasks']['9']=dict(task(9),status={'Stashed':dict(enqueue_at=None)})
    self.change_state(pid_file=str(self.root/'pueue.pid'))
    queuepath=self.root/'state.json';config=dict(data=self.root,pid_file=self.root/'pueue.pid')
    for phase in ('intent','queue','complete'):
        control.atomic_json(queuepath,initial)
        with self.engine.connect() as db:state=self.engine.state(db)
        state.pop('startup',None);state['reasons']={'suites':['reorder']}
        with self.engine.connect(write=True) as db:self.engine.store(db,state)
        def boundary(name):
            if name==phase:raise Crash()
        with patch.object(self.p,'status',side_effect=subprocess.CalledProcessError(1,['fake-offline'])),patch.object(control.subprocess,'run',return_value=subprocess.CompletedProcess([],1,'','')):
            with self.assertRaises(Crash):control.prepare_start(self.engine,queuepath,config,boundary=boundary)
            image=control.prepare_start(self.engine,queuepath,config)
            again=control.prepare_start(self.engine,queuepath,config)
        self.assertEqual(image,again)
        self.assertEqual(image['tasks']['1']['status'],{'Queued':dict(enqueued_at=STAMP)})
        self.assertEqual(image['tasks']['9']['status'],{'Stashed':dict(enqueue_at=None)})
        self.assertTrue(all(row['status']=='Paused' for row in image['groups'].values()))


def test_no_log_evidence_before_enqueue_and_offline_identity(self):
    self.engine.reorder_boundary=lambda p:(_ for _ in ()).throw(Crash()) if p=='client_sent' else None
    with self.assertRaises(Crash):self.engine.handle(self.order_request())
    self.engine.reorder_boundary=lambda p:None
    with self.engine.connect() as db:state=self.engine.state(db)
    queue=self.p.status();queue['tasks']['1']['created_at']='2026-11-16T12:01:00Z'
    with self.assertRaises(ValueError):self.engine.reorder_offline(queue,state,self.root)
    (self.root/'task_logs').mkdir();(self.root/'task_logs/1.log').touch()
    with self.assertRaises(ValueError):self.engine.reconcile()
    self.assertNotIn(('enqueue','1'),self.p.calls)
    self.assertEqual(self.p.groups['suites']['status'],'Paused')


def test_request_bytes_limit_before_any_effect(self):
    r=self.order_request(dry=True)
    with patch.object(control,'canonical',return_value='x'*16385):
        with self.assertRaises(ValueError):self.engine.handle(r)
    self.assertEqual(self.p.calls,[])


def test_cancel_during_edit_restores_only_surviving_queued(self):
    initial=copy.deepcopy(self.tasks)
    def boundary(phase):
        if phase=='edit_effect' and '2' in self.tasks:
            self.tasks['2']['status']={'Done':dict(enqueued_at=STAMP,start=STAMP,end=STAMP,result='Killed')}
    self.engine.reorder_boundary=boundary
    r=self.order_request();result=self.engine.handle(r)
    self.assertEqual(result['status'],'unknown')
    self.engine.reorder_boundary=lambda phase:None
    self.assertTrue(self.engine.reconcile())
    result=self.engine.handle(r)
    self.assertEqual(result['status'],'aborted',result)
    self.assertIn('Done',self.tasks['2']['status'])
    for ident,t in self.tasks.items():
        if 'Queued' in t['status']:self.assertEqual(t['priority'],initial[ident]['priority'])
    self.assertEqual(self.p.groups['suites']['status'],'Running')
    self.assertNotIn(('enqueue','2'),self.p.calls)


def test_cancel_after_edited_keeps_historic_success_and_strict_undo(self):
    def boundary(phase):
        if phase=='edited':self.tasks['2']['status']={'Done':dict(enqueued_at=STAMP,start=STAMP,end=STAMP,result='Killed')}
    self.engine.reorder_boundary=boundary
    r=self.order_request();result=self.engine.handle(r)
    self.assertEqual(result['status'],'applied',result)
    self.assertIn('Done',self.tasks['2']['status'])
    self.assertNotIn(('enqueue','2'),self.p.calls)


def test_every_private_identity_field_blocks_foreign_repair(self):
    self.engine.reorder_boundary=lambda p:(_ for _ in ()).throw(Crash()) if p=='client_sent' else None
    with self.assertRaises(Crash):self.engine.handle(self.order_request())
    self.engine.reorder_boundary=lambda p:None
    initial=copy.deepcopy(self.tasks['1'])
    changed=dict(created_at='2026-11-16T12:01:00Z',group='rapidas',command='different',original_command='different',path='/other-synthetic',envs={'NEW':'value'},dependencies=[99],label='different')
    for field,value in changed.items():
        self.tasks['1']=dict(copy.deepcopy(initial),**{field:value})
        with self.assertRaises(ValueError,msg=field):self.engine.reconcile()
        self.assertNotIn(('enqueue','1'),self.p.calls)
        self.assertEqual(self.p.groups['suites']['status'],'Paused')


def test_capabilities_do_not_create_bytecode_even_when_python_allows_it(self):
    import sys
    source=self.root/'source';source.mkdir()
    (source/'editor_prioridad.py').write_bytes(Path(control.__file__).with_name('editor_prioridad.py').read_bytes())
    before=self.fingerprint()
    with patch.object(control,'__file__',str(source/'control.py')),patch.object(control,'_PRIORITY_HELPER',None),patch.object(sys,'dont_write_bytecode',False),patch.object(sys,'pycache_prefix',None):
        self.assertTrue(self.engine.view()['capabilities']['reorder'])
        self.assertEqual(self.fingerprint(),before)


def test_failed_prepared_transaction_has_no_rpc_or_orphan_owner(self):
    r=self.order_request()
    with self.engine.connect(write=True) as db:
        db.execute("CREATE TRIGGER reject_u36 BEFORE INSERT ON host_actions BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        before=copy.deepcopy(self.engine.state(db))
    result=self.engine.handle(r)
    self.assertEqual(result['status'],'unknown')
    with self.engine.connect() as db:
        self.assertEqual(self.engine.state(db),before)
        self.assertEqual(db.execute('SELECT count(*) FROM host_actions').fetchone()[0],0)
    self.assertEqual(self.p.calls,[])


def test_unprepared_host_cannot_enable_or_pause(self):
    self.engine.reorder_host_ready=control.MiniControl.reorder_host_ready.__get__(self.engine)
    self.assertFalse(self.engine.view()['capabilities']['reorder'])
    result=self.engine.handle(self.order_request())
    self.assertEqual((result.get('error') or {}).get('code'),'unsupported',result)
    self.assertEqual(self.p.calls,[])


def test_stock_stashed_without_preparer_is_not_resurrected(self):
    self.engine.reorder_boundary=lambda p:(_ for _ in ()).throw(Crash()) if p=='client_sent' else None
    with self.assertRaises(Crash):self.engine.handle(self.order_request())
    self.engine.reorder_boundary=lambda p:None
    self.tasks['1']['status']={'Stashed':dict(enqueue_at=None)};self.gen='daemon-2'
    with self.assertRaises(ValueError):self.engine.reconcile()
    self.assertNotIn(('enqueue','1'),self.p.calls)
    self.assertIn('Stashed',self.tasks['1']['status'])


def test_rejected_id_remains_terminal_after_conditions_change(self):
    self.engine.coordinated_reorder=False
    request=self.order_request('rejected-order-01')
    rejected=self.engine.handle(request)
    self.assertEqual(rejected['error']['code'],'uncoordinated')
    self.engine.coordinated_reorder=True
    self.assertEqual(self.engine.handle(request),rejected)
    changed=copy.deepcopy(request);changed['change']['order'].reverse()
    self.assertEqual(self.engine.handle(changed)['error']['code'],'idempotency_conflict')
    self.assertEqual(self.p.calls,[])
    self.assertTrue(self.engine.reconcile())
    self.assertEqual(self.engine.handle(request),rejected)



def test_alias_added_after_enqueue_rejects_before_any_effect(self):
    r=self.order_request('new-alias-00001')
    self.engine.reorder_aliases=lambda state:{'true':'changed'}
    self.assertEqual(self.engine.handle(r)['error']['code'],'edit_unsafe')
    self.assertEqual(self.p.calls,[])
    self.assertTrue(all(t['command']=='true' for t in self.tasks.values()))
    self.assertFalse(self.engine.view()['reorder_groups']['suites']['enabled'])


def test_daemon_config_not_edit_client_selects_effective_alias_file(self):
    source=self.root/'daemon.json';aliases=self.root/'daemon-aliases.yml'
    client=self.root/'client-aliases.yml';client.write_text('{}')
    aliases.write_text('true: changed\n')
    source.write_text(json.dumps(dict(shared=dict(alias_file=str(aliases)))))
    (self.root/'pueue.pid').write_text('43')
    self.p.daemon_config=source
    self.p.edit_settings=dict(shared=dict(alias_file=str(client)))
    with self.engine.connect() as db:state=self.engine.state(db)
    actual=control.MiniControl.reorder_aliases.__get__(self.engine)
    with patch.object(control.priority_helper(),'birth',return_value='darwin:9999999999:0'):
        self.assertEqual(actual(state),{'true':'changed'})
    with patch.object(control.priority_helper(),'birth',return_value='darwin:1:0'):
        with self.assertRaises(ValueError):actual(state)
    source.write_text('{"shared": {"alias_file": "relative"}}')
    with patch.object(control.priority_helper(),'birth',return_value='darwin:9999999999:0'):
        with self.assertRaises(ValueError):actual(state)


def test_reorder_reads_and_recovery_are_scoped_but_guard_reads_unbounded(self):
    seen=[];old=self.p.status
    def status():
        seen.append((getattr(self.p,'timeout',None),getattr(self.p,'deadline',None)))
        return old()
    self.p.status=status
    r=self.order_request();seen.clear()
    self.assertEqual(self.engine.handle(r)['status'],'applied')
    self.assertTrue(seen)
    for timeout,deadline in seen:
        self.assertIsNotNone(timeout)
        self.assertLessEqual(timeout,1)
        self.assertIsNotNone(deadline)
    self.assertIsNone(self.p.timeout);self.assertIsNone(self.p.deadline)
    self.engine.reorder_boundary=lambda p:(_ for _ in ()).throw(Crash()) if p=='prepared' else None
    with self.assertRaises(Crash):self.engine.handle(self.order_request('scope-recovery-001'))
    self.engine.reorder_boundary=lambda p:None
    seen.clear();self.assertTrue(self.engine.reconcile())
    self.assertTrue(all(timeout is not None and timeout<=1 and deadline is not None for timeout,deadline in seen))
    self.assertIsNone(self.p.timeout);self.assertIsNone(self.p.deadline)
    seen.clear();self.engine.reconcile()
    self.assertTrue(any(timeout is None for timeout,_ in seen))


def test_failed_edit_preserves_safe_diagnostic_and_recovery_finishes(self):
    def fail(action,ident,priority,deadline):
        raise subprocess.CalledProcessError(2,['PRIVATE_COMMAND'],stderr='PRIVATE_ENV')
    self.engine.edit_priority=fail
    r=self.order_request();answer=self.engine.handle(r)
    self.assertEqual(answer['status'],'unknown')
    a=self.engine.reorder_actions()[-1]
    self.assertEqual(a.get('failure'),dict(kind='CalledProcessError',phase='paused',exit_code=2))
    self.assertNotIn('PRIVATE_',json.dumps(a))
    self.assertTrue(self.engine.reconcile())
    self.assertEqual(self.engine.handle(r)['status'],'aborted')


for name,fn in list(globals().items()):
    if name.startswith('test_'):setattr(ReorderTests,name,fn)


class HelperTests(unittest.TestCase):
    def test_multiline_unicode_whitespace_null_and_bytes(self):
        helper=control.priority_helper()
        for quote in ('"""', "'''", '"', "'"):
            cmd='hello\npriority = 999\nα' if len(quote)==3 else 'héllo'
            raw=('[3]\nid = 3\ncommand = '+quote+cmd+quote+'\npath = "/synthetic"\npriority\t= -12  \n').encode()
            out=helper.document(raw,3,4)
            self.assertEqual(out,raw.replace(b'priority\t= -12',b'priority\t= 4'))
        for malformed in (b'[3]\npriority=0\npriority=1\n',b'[3]\npriority=1\n[4]\n',b'[3]\npriority=true\n',b'priority=1\n'):
            with self.assertRaises(ValueError):helper.document(malformed,3,2)
        t=task(3);t['command']='expanded'
        with self.assertRaises(ValueError):helper.preflight(t)

    def test_helper_checks_real_locked_previous_queued_before_write(self):
        helper=control.priority_helper();t=task(1)
        t['status']={'Locked':dict(previous_status={'Queued':dict(enqueued_at=STAMP)})}
        snapshot=dict(tasks={'1':t},groups={'suites':dict(status='Paused')})
        plan=dict(id=1,expected_priority=0,identity=control.MiniControl.task_identity(t))
        helper.verify_locked(plan,snapshot)
        for case in ('stashed','running','priority','fields'):
            altered=copy.deepcopy(snapshot)
            if case=='stashed':altered['tasks']['1']['status']['Locked']['previous_status']={'Stashed':dict(enqueue_at=None)}
            if case=='running':altered['groups']['suites']['status']='Running'
            if case=='priority':altered['tasks']['1']['priority']=99
            if case=='fields':altered['tasks']['1']['command']='different'
            with self.assertRaises(ValueError):helper.verify_locked(plan,altered)

    def test_helper_entrypoint_rejects_stashed_previous_without_writing_toml(self):
        import tempfile
        import sys
        helper=control.priority_helper()
        with tempfile.TemporaryDirectory(prefix='u36-helper-',dir='/tmp') as folder:
            root=Path(folder);target=root/'edit/tasks.toml';target.parent.mkdir(mode=0o700)
            raw=b'[1]\nid = 1\ncommand = "true"\npath = "/synthetic"\npriority = 0\n'
            target.write_bytes(raw)
            t=task(1);t['status']={'Locked':dict(previous_status={'Stashed':dict(enqueue_at=None)})}
            snapshot=dict(tasks={'1':t},groups={'suites':dict(status='Paused')})
            plan=dict(id=1,priority=7,deadline=control.time.monotonic()+1,expected_priority=0,identity=control.MiniControl.task_identity(t),binary='/tmp/only-a-fake',client=dict(pid=42,birth='proof'),daemon=dict(pid=43,birth='proof'))
            control.atomic_json(root/'plan.json',plan)
            with patch.object(helper,'birth',return_value='proof'),patch.object(helper.subprocess,'run',return_value=subprocess.CompletedProcess([],0,json.dumps(snapshot),'')),patch.object(sys,'argv',[str(helper.__file__),str(root/'plan.json'),str(target)]):
                with self.assertRaises(ValueError):helper.main()
            self.assertEqual(target.read_bytes(),raw)

    def test_semantic_toml_fingerprint_precedes_write(self):
        helper=control.priority_helper()
        raw=b'[1]\nid = 1\ncommand = "true"\npath = "/synthetic"\npriority = 0\n'
        plan=dict(expected_priority=0,fingerprint=helper.fingerprint('true','/synthetic',None))
        self.assertEqual(helper.document(raw,1,7,plan),raw.replace(b'priority = 0',b'priority = 7'))
        for key in (b'command = "true"',b'path = "/synthetic"'):
            with self.assertRaises(ValueError):helper.document(raw.replace(key,key[:-1]+b'changed"'),1,7,plan)
        with self.assertRaises(ValueError):helper.document(raw.replace(b'priority = 0',b'priority = 9'),1,7,plan)
        with self.assertRaises(ValueError):helper.document(raw+b'label = "new"\n',1,7,plan)
        # Decoded strings, including TOML's initial multiline newline and Unicode.
        quoted=b'[1]\nid = 1\ncommand = \'\'\'\ntrue\'\'\'\npath = "/synthetic"\npriority = 0\n'
        self.assertIn(b'priority = 7',helper.document(quoted,1,7,plan))

    def test_desk_remains_disabled(self):
        self.assertFalse(control.MiniControl.coordinated_reorder)



class InstallTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from contextlib import contextmanager
        self.temp=tempfile.TemporaryDirectory(prefix='u36-install-',dir='/tmp');self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name);self.library=self.home/'.local/lib/pao-pueue-mini';self.library.mkdir(parents=True)
        self.directory=self.home/'.local/state/pao/pueue-mini';self.directory.mkdir(parents=True)
        (self.directory/'control.lock').touch();(self.library/'control.py').write_text('previous-controller\n')
        spec=control.importlib.util.spec_from_file_location('u36_installer',control.Path(control.__file__).with_name('install-reorder.py'))
        self.installer=control.importlib.util.module_from_spec(spec);spec.loader.exec_module(self.installer)
        self.state=dict(mini_handoff=dict(phase='done'),generation='verified',pid_file=str(self.home/'pid'),desired=control.MINI_PROFILE)
        self.actions=[]
        outer=self
        class DB:
            def execute(self,*args):return [(json.dumps(a),) for a in outer.actions]
        class Engine:
            @contextmanager
            def connect(self):yield DB()
            def state(self,db):return outer.state
            def generation(self,state):return 'verified'
            def observed(self):return dict(control.MINI_PROFILE),{}
        self.engine=Engine();self.config=dict(pid_file=self.home/'pid',data=self.home)

    def files(self):return {str(p.relative_to(self.home)):p.read_bytes() for p in self.home.rglob('*') if p.is_file()}

    def test_installer_load_cannot_write_bytecode_before_preflight(self):
        import sys
        source=self.home/'source';source.mkdir()
        path=Path(self.installer.__file__)
        (source/'control.py').write_bytes(path.with_name('control.py').read_bytes())
        before=self.files()
        namespace=dict(__file__=str(source/'install-reorder.py'),__name__='u36_dry_import')
        with patch.object(sys,'dont_write_bytecode',False),patch.object(sys,'pycache_prefix',None):
            exec(compile(path.read_bytes(),str(path),'exec'),namespace)
        self.assertEqual(before,self.files())

    def test_dry_validates_before_every_effect_and_no_restart(self):
        import io
        from contextlib import redirect_stdout
        before=self.files()
        with redirect_stdout(io.StringIO()),patch.object(self.installer.c,'validate_configuration',return_value=self.config) as validation:
            self.installer.install(self.home,dry=True,engine=self.engine)
        validation.assert_called_once();self.assertEqual(before,self.files())
        with patch.object(self.installer.c,'validate_configuration',side_effect=ValueError('unsafe')):
            with self.assertRaises(ValueError):self.installer.install(self.home,engine=self.engine)
        self.assertEqual(before,self.files())

    def test_publish_helper_before_controller_and_rollback_no_pending(self):
        calls=[];original=self.installer.publish
        def publish(path,raw):calls.append(path.name);return original(path,raw)
        with patch.object(self.installer,'publish',side_effect=publish):
            self.installer.install(self.home,engine=self.engine,config=self.config)
        self.assertEqual(calls,['control.py.u36-before','editor_prioridad.py','control.py'])
        self.assertEqual((self.library/'control.py.u36-before').read_text(),'previous-controller\n')
        self.actions=[dict(kind='reorder',phase='edit_intent',response=dict(status='unknown'))]
        before=self.files()
        with self.assertRaises(ValueError):self.installer.install(self.home,withdraw=True,engine=self.engine,config=self.config)
        self.assertEqual(before,self.files())
        self.actions=[];self.installer.install(self.home,withdraw=True,engine=self.engine,config=self.config)
        self.assertEqual((self.library/'control.py').read_text(),'previous-controller\n')
        self.assertTrue((self.library/'editor_prioridad.py').is_file())


if __name__=='__main__':unittest.main()
