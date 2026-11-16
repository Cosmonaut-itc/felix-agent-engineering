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
    self.engine.coordinated_reorder = True
    self.engine.reorder_cost = .001
    self.engine.edit_priority = lambda *a: fake_edit(self,*a)
    self.pressure = patch.object(control,'pressure_mini',return_value=dict(load=0,memory=1))
    self.pressure.start(); self.addCleanup(self.pressure.stop)


def fake_edit(self, action, ident, priority, deadline):
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
    order.insert(1,order.pop(4))
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
    before=self.fingerprint();self.engine.view();self.engine.handle(self.order_request('dryagain-000001',dry=True))
    self.assertEqual(self.fingerprint(),before);self.assertEqual(self.p.calls,[])


def test_paused_reasons_manual_guard_ambiguous_and_startup(self):
    self.change_state(startup_version=1,startup=dict(phase='ready',statuses={},groups={},holds={},interruptions={},source_hash='a'*64,target_hash='b'*64))
    for reason in ('pressure','manual','ambiguous','startup','handoff'):
        self.change_state(reasons={'suites':[reason]})
        self.p.groups['suites']['status']='Paused'
        r=self.order_request('paused-'+reason+'-0001')
        result=self.engine.handle(r)
        self.assertEqual(result['error']['code'],'paused',result)
        self.assertIn(reason,result['pause_reasons']);self.assertEqual(self.p.calls,[])
    self.change_state(reasons={});self.p.groups['suites']['status']='Running'
    self.engine.coordinated_reorder=False
    self.assertFalse(self.engine.view()['capabilities']['reorder'])
    self.assertEqual(self.engine.handle(self.order_request('contract-000001'))['error']['code'],'uncoordinated')
    self.assertEqual(self.p.calls,[])


def test_concurrent_before_compare_aborts_add_cancel_and_start(self):
    base=copy.deepcopy(self.tasks)
    for index,change in enumerate(('add','cancel','start')):
        self.tasks=copy.deepcopy(base);self.p.groups['suites']['status']='Running'
        def boundary(phase):
            if phase=='paused':
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
    self.assertEqual(undo['error']['code'],'undo_conflict',undo)
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
    self.assertEqual(result['error']['code'],'pause_budget');self.assertEqual(self.p.calls,[])


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


for name,fn in list(globals().items()):
    if name.startswith('test_'):setattr(ReorderTests,name,fn)


class HelperTests(unittest.TestCase):
    def test_multiline_unicode_whitespace_null_and_bytes(self):
        helper=control.priority_helper()
        for quote in ('"""', "'''", '"', "'"):
            cmd='hello\npriority = 999\nα' if len(quote)==3 else 'héllo'
            raw=('[3]\nid = 3\noriginal_command = '+quote+cmd+quote+'\npath = "/synthetic"\npriority\t= -12  \n').encode()
            out=helper.document(raw,3,4)
            self.assertEqual(out,raw.replace(b'priority\t= -12',b'priority\t= 4'))
        for malformed in (b'[3]\npriority=0\npriority=1\n',b'[3]\npriority=1\n[4]\n',b'[3]\npriority=true\n',b'priority=1\n'):
            with self.assertRaises(ValueError):helper.document(malformed,3,2)
        t=task(3);t['command']='expanded'
        with self.assertRaises(ValueError):helper.preflight(t)

    def test_desk_remains_disabled(self):
        self.assertFalse(control.MiniControl.coordinated_reorder)


if __name__=='__main__':unittest.main()
