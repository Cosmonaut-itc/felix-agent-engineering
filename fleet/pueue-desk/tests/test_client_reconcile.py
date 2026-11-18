"""CC2: fake daemon/evidence, private journals, real flock/collector processes."""
import copy
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import select
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import test_client_actions as cc1
import test_reorder
import test_trickle
import test_handoff

control,a,c=cc1.control,cc1.a,cc1.c
mini=test_handoff.mini


def finish_request():
    return next(copy.deepcopy(v['request']) for v in cc1.CORPUS['valid'] if
                v['request']['op']=='finish' and v['request']['mode']=='rack-queued')


class Evidence(cc1.Evidence):
    def __init__(self,identity,directory):
        super().__init__(identity)
        self.directory=Path(directory)
        self.settled=False
        self.effect_applied=False
        self.finish=None
        self.slow=0
        self.result_lock=False
        # The synthetic launcher's durable records: which child ran which phase.
        self.launcher_effects={}

    def recover(self,r,state,reservation,process,daemon):
        # Another open description detects a collector whose parent holds flock.
        with (self.directory/'control.lock').open('rb') as h:
            try:fcntl.flock(h,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise a.Unavailable('lock_busy')
        if self.slow:time.sleep(self.slow)
        if self.result_lock:
            (self.directory/'collecting').write_text('ready')
            with (self.directory/'result.lock').open('a') as h:fcntl.flock(h,fcntl.LOCK_EX)
        return dict(observation=dict(group=reservation['group'],identity=copy.deepcopy(self.identity)),
                    settled=self.settled,finish=copy.deepcopy(self.finish),
                    **({'effect':copy.deepcopy(self.launcher_effects[a.ClientActions.key(r)])}
                       if self.effect_applied and a.ClientActions.key(r) in self.launcher_effects else {}))


def write_then_die(directory,record,reserve):
    engine=control.MiniControl(cc1.test_slots.FakePueue(),Path(directory),generation=lambda:'daemon-1')
    with control.locked(engine.directory):a.ClientActions(engine).save(record,reserve)
    os._exit(0)


def result_owner(directory,pipe):
    directory=Path(directory)
    with (directory/'result.lock').open('a') as result:
        fcntl.flock(result,fcntl.LOCK_EX)
        pipe.send('result-held')
        deadline=time.monotonic()+3
        while not (directory/'collecting').exists() and time.monotonic()<deadline:time.sleep(.005)
        with (directory/'control.lock').open('rb') as h:
            try:
                fcntl.flock(h,fcntl.LOCK_EX|fcntl.LOCK_NB)
                pipe.send('control-acquired')
            except BlockingIOError:pipe.send('control-blocked')
    pipe.close()


class ReconcileTests(cc1.ActionTests):
    pass


for name in dir(cc1.ActionTests):
    if name.startswith('test_'):setattr(ReconcileTests,name,None)


def setup(self):
    cc1.setup(self)
    self.evidence=Evidence(self.r['expected'],self.engine.directory)
    self.actions=a.ClientActions(self.engine,self.evidence,rpc=lambda *args:self.rpc(*args))
    self.engine.client_adapter=self.evidence


def orphan(self,r=None,process='sent',status='unknown',die=False):
    r=copy.deepcopy(r or self.r)
    reserve=dict(launcher=r['launcher'],ref=r['ref'],action_id=r['action_id'],group='suites',mode=r['mode'],
                 identity=r['expected'],acceptance=r['acceptance'],hold_identity=None,open=True,uncertain=status=='unknown')
    if process is not None:
        process=dict(pid=999999,birth='synthetic:123',state=process)
        self.evidence.launcher_effects[a.ClientActions.key(r)]=dict(zip(a.Reconciler.EFFECT_FIELDS,a.ClientActions.key(r)+(process['pid'],process['birth'])))
    response=(self.actions.reply(r,'unknown','rpc_unresolved',identity=r['expected'],held=True) if status=='unknown'
              else self.actions.reply(r,status,identity=self.evidence.identity,held=True))
    record=dict(request=r,digest=a.digest(r),revision=0,process=process,response=response)
    if status != 'unknown':
        reserve['identity']=copy.deepcopy(self.evidence.identity)
        if status=='held':reserve['hold_identity']=copy.deepcopy(self.evidence.identity)
    if die:
        owner=multiprocessing.get_context('spawn').Process(target=write_then_die,args=(str(self.engine.directory),record,reserve))
        owner.start();owner.join(3)
        if owner.is_alive():owner.terminate();owner.join()
        self.assertEqual(owner.exitcode,0,'orphan owner committed and died')
    else:
        self.actions.save(record,reserve)
    return r


def run_reconcile(self,offline=None):
    # Like every productive entry: before, never inside, a caller's control.lock.
    return a.Reconciler(self.engine,self.evidence,birth=lambda pid:None).run(offline=offline)


def guard_tick(self,readings=None):
    """The order of control.main guardia: CC2 first, then the locked guard."""
    control.reconcile_before_lock(self.engine)
    with control.locked(self.engine.directory):
        return self.engine.guard(readings or dict(load=4,memory=1))


def state(self):
    with self.engine.connect() as db:return self.engine.state(db)


def prepare(self):
    data=self.root/'offline';data.mkdir(exist_ok=True)
    path=data/'state.json'
    path.write_text(json.dumps(dict(groups=copy.deepcopy(self.p.groups),tasks=copy.deepcopy(self.tasks))))
    config=dict(data=data,pid_file=Path(self.state()['pid_file']))
    control.reconcile_before_start(self.engine,lambda:config)  # as preparar and the wrapper do
    with control.locked(self.engine.directory),patch.object(control,'assert_offline'):
        return control.prepare_start(self.engine,path,config)


ReconcileTests.setUp=setup
ReconcileTests.orphan=orphan
ReconcileTests.run_reconcile=run_reconcile
ReconcileTests.guard_tick=guard_tick
ReconcileTests.state=state
ReconcileTests.prepare=prepare


def test_every_death_boundary_in_guard_and_preparer(self):
    baseline=copy.deepcopy(self.state())
    for entry in ('guard','prepare'):
        for stage in ('reserved','gated','sent','reaped','acceptance','published','reorder-owner'):
            with self.subTest(entry=entry,stage=stage):
                with self.engine.connect(write=True) as db:
                    for table in ('client_actions','client_reservations','client_open'):
                        if db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone():db.execute('DELETE FROM '+table)
                    db.execute('DELETE FROM host_actions')
                with self.engine.connect(write=True) as db:self.engine.store(db,copy.deepcopy(baseline))
                self.evidence.settled=False;self.evidence.finish=None
                self.evidence.identity=copy.deepcopy(self.r['expected'])
                self.p.groups['suites']['status']='Running'
                r=self.r
                if stage=='published':
                    r=cc1.request('remote-run-started')
                    self.evidence.identity=copy.deepcopy(cc1.CASES['remote-run-started']['response']['identity'])
                    self.orphan(r,status='started',die=True)
                    self.evidence.finish=finish_request();self.evidence.identity=self.evidence.finish['expected']
                    self.evidence.acceptance=self.evidence.finish['acceptance'];self.evidence.proof=self.evidence.finish['proof']
                    self.evidence.settled=True;self.evidence.effect_applied=True
                else:
                    if stage=='acceptance':
                        self.evidence.identity=copy.deepcopy(self.after)
                        self.orphan(status='held',die=True)
                    else:
                        self.orphan(process=None if stage=='reserved' else stage if stage in ('gated','reaped') else 'sent',die=True)
                    if stage=='reorder-owner':
                        order=self.order_request('dead-order-00001')
                        action=dict(kind='reorder',group='suites',phase='prepared',request=order,response=dict(status='applying'))
                        with self.engine.connect(write=True) as db:db.execute('INSERT INTO host_actions VALUES (?,?,?)',(order['action_id'],'a'*64,control.canonical(action)))
                with patch.object(self.engine,'reconcile',return_value=True):
                    if entry=='guard':
                        self.guard_tick(dict(load=0,memory=1))
                    else:
                        with patch.object(self.engine,'reorder_offline',side_effect=lambda q,s,d:q):self.prepare()
                reserve=self.actions.reservation(r)
                if stage in ('reserved','published'):
                    self.assertFalse(reserve['open'],(entry,stage,reserve))
                    if stage=='published':self.assertEqual(self.actions.record(finish_request())['response']['status'],'finished')
                else:
                    self.assertTrue(reserve['open']);self.assertIn(reserve.get('reconcile_reason'),c.REASONS)
                    self.assertTrue(self.engine.client_blocked('suites'))


def test_generation_revision_digest_and_reservation_cas(self):
    for version in ('generation','offline-generation','revision','digest','reserve-revision','reserve-digest','phase','closed'):
        with self.subTest(version=version):
            r=self.orphan();self.evidence.identity=copy.deepcopy(self.after);self.evidence.settled=True;self.evidence.effect_applied=True
            reconcile=a.Reconciler(self.engine,self.evidence,birth=lambda pid:None)
            offline=self.root/'nonexistent-offline-image' if version=='offline-generation' else None
            copies=reconcile.copies(offline);proposals=reconcile.bounded_gather(copies,offline)
            current=self.actions.record(r);reserve=self.actions.reservation(r)
            if version in ('generation','offline-generation'):self.gen='daemon-changed'
            elif version=='revision':current['revision']+=1
            elif version=='digest':
                current['request']['expected']['fingerprint']='d'*64
                current['digest']=a.digest(current['request'])
            elif version=='reserve-revision':reserve['revision']+=1
            elif version=='reserve-digest':reserve['digest']='d'*64
            elif version=='phase':reserve['phase']='finish'
            else:reserve['open']=False
            with self.engine.connect(write=True) as db:
                db.execute('UPDATE client_actions SET body=?',(a.canonical(current),))
                db.execute('UPDATE client_reservations SET body=?',(a.canonical(reserve),))
            before=self.fingerprint()
            self.assertFalse(reconcile.apply(copies[0],proposals[0],offline),'stale '+version)
            self.assertEqual(self.fingerprint(),before)
            self.gen='daemon-1'
            with self.engine.connect(write=True) as db:
                db.execute('DELETE FROM client_actions');db.execute('DELETE FROM client_reservations');db.execute('DELETE FROM client_open')


def test_real_result_lock_and_client_lock_have_no_deadlock(self):
    self.orphan();self.evidence.identity=copy.deepcopy(self.after)
    self.evidence.settled=True;self.evidence.effect_applied=True;self.evidence.result_lock=True
    ctx=multiprocessing.get_context('fork');parent,child=ctx.Pipe()
    owner=ctx.Process(target=result_owner,args=(str(self.engine.directory),child));owner.start();child.close()
    try:
        self.assertTrue(parent.poll(3));self.assertEqual(parent.recv(),'result-held')
        with patch.object(a.Reconciler,'seconds',1.5):self.assertEqual(self.run_reconcile(),1)
        self.assertTrue(parent.poll(3));self.assertEqual(parent.recv(),'control-acquired')
        self.assertEqual(self.actions.record(self.r)['response']['status'],'held','collection outside control.lock')
        owner.join(3);self.assertFalse(owner.is_alive());self.assertEqual(owner.exitcode,0)
        with control.locked(self.engine.directory):
            with (self.engine.directory/'control.lock').open('rb') as h:
                with self.assertRaises(BlockingIOError):fcntl.flock(h,fcntl.LOCK_EX|fcntl.LOCK_NB)
    finally:
        if owner.is_alive():owner.terminate()
        owner.join();parent.close()


def test_clock_two_equal_reads_and_child_exit_never_exclude_late_rpc(self):
    r=self.orphan(process='reaped');self.evidence.effect_applied=True;self.evidence.identity=copy.deepcopy(self.after);self.now+=10**9
    for _ in range(2):
        self.assertEqual(self.run_reconcile(),1)
        self.assertTrue(self.actions.reservation(r)['open'],'no expiry, equal reads, or child-exit release')
        self.assertTrue(self.actions.reservation(r)['uncertain'])
        self.assertEqual(self.actions.record(r)['response']['reason'],'rpc_unresolved')
        self.assertIsNone(self.actions.reservation(r)['hold_identity'])
    self.assertEqual(self.rpc_calls,[])


def test_birth_matches_live_child_keeps_unknown_even_with_claimed_settlement(self):
    self.orphan(process='gated');self.evidence.identity=copy.deepcopy(self.after);self.evidence.settled=True;self.evidence.effect_applied=True
    reconcile=a.Reconciler(self.engine,self.evidence,birth=lambda pid:'synthetic:123')
    self.assertEqual(reconcile.run(),1)
    self.assertEqual(self.actions.record(self.r)['response']['status'],'unknown')
    self.assertEqual(self.actions.record(self.r)['response']['reason'],'rpc_unresolved')
    self.assertIsNone(self.actions.reservation(self.r)['hold_identity'])


def test_synthetic_settlement_attributes_hold_then_durable_result_finishes(self):
    self.orphan();self.evidence.identity=copy.deepcopy(self.after);self.evidence.settled=True;self.evidence.effect_applied=True
    self.assertEqual(self.run_reconcile(),1)
    held=self.actions.record(self.r)['response'];self.assertEqual(held['status'],'held')
    self.assertEqual(self.actions.reservation(self.r)['hold_identity'],self.after)
    finish=finish_request();self.evidence.finish=finish;self.evidence.identity=copy.deepcopy(finish['expected'])
    self.evidence.acceptance=finish['acceptance'];self.evidence.proof=finish['proof']
    self.assertEqual(self.run_reconcile(),1)
    self.assertFalse(self.actions.reservation(self.r)['open'])
    self.assertEqual(self.actions.record(finish)['response']['status'],'finished')
    self.assertEqual(self.actions.record(self.r)['response'],held,'effect receipt unchanged')
    before=self.fingerprint();self.run_reconcile();self.assertEqual(self.fingerprint(),before)
    self.assertEqual(self.rpc_calls,[])


def test_terminal_never_resurrects_and_effect_receipts_stay_fixed(self):
    finish=finish_request();self.evidence.identity=finish['expected']
    self.orphan(cc1.request('remote-run-started'),status='started')
    self.evidence.finish=finish;self.evidence.settled=True;self.evidence.effect_applied=True
    self.evidence.acceptance=finish['acceptance'];self.evidence.proof=finish['proof']
    reconcile=a.Reconciler(self.engine,self.evidence,birth=lambda pid:None)
    copies=reconcile.copies(None);proposals=reconcile.bounded_gather(copies,None)
    self.assertTrue(reconcile.apply(copies[0],proposals[0],None))
    self.assertFalse(reconcile.apply(copies[0],proposals[0],None))
    self.assertFalse(self.actions.reservation(finish)['open'])
    self.assertEqual(self.actions.handle(finish)['status'],'finished')
    before=self.fingerprint();self.run_reconcile();self.assertEqual(self.fingerprint(),before)


def test_unsupported_has_reason_without_rpc_and_closes_only_unsent(self):
    for launcher in c.LAUNCHERS:
        r=copy.deepcopy(self.r);r['launcher']=launcher;r['mode']='own'
        if launcher=='vigilante':r=next(copy.deepcopy(v['request']) for v in cc1.CORPUS['valid'] if v['request']['op']=='signal-manual')
        self.orphan(r)
        with patch.object(self.p,'status',side_effect=AssertionError('unsupported must not read daemon')):
            self.assertEqual(a.Reconciler(self.engine,birth=lambda pid:None).run(),1)
        self.assertEqual(self.actions.record(r)['response']['reason'],'unsupported')
        self.assertTrue(self.actions.reservation(r)['open'])
        with self.engine.connect(write=True) as db:
            db.execute('DELETE FROM client_actions');db.execute('DELETE FROM client_reservations');db.execute('DELETE FROM client_open')
    self.orphan(process=None);self.assertEqual(a.Reconciler(self.engine).run(),1)
    self.assertFalse(self.actions.reservation(self.r)['open'])
    self.assertEqual(self.actions.record(self.r)['response']['effects'],False)
    self.assertEqual(self.rpc_calls,[])


def test_preparer_retains_unknown_stashed_across_daemon_restart(self):
    self.orphan();self.evidence.identity=copy.deepcopy(self.after)
    self.tasks['77']=test_reorder.task(77);self.tasks['77']['status']={'Stashed':{'enqueue_at':None}}
    before=copy.deepcopy(self.tasks['77']);image=self.prepare()
    self.assertEqual(image['tasks']['77'],before,'client Stashed is never startup/reorder property')
    self.assertTrue(self.actions.reservation(self.r)['open'],'prepare must not release')
    self.gen='restarted-daemon';self.run_reconcile()
    self.assertTrue(self.actions.reservation(self.r)['open'])
    self.assertTrue(self.engine.client_blocked('suites',opening=True))


def test_manual_pressure_startup_and_fence_survive_reconciliation(self):
    self.change_state(startup_version=1,startup=dict(phase='ready',statuses={},groups={},holds={},interruptions={},source_hash='a'*64,target_hash='b'*64))
    self.orphan()
    for reason in ('manual','pressure','startup'):
        self.change_state(reasons={'suites':[reason]});self.p.groups['suites']['status']='Paused'
        self.evidence.settled=True;self.evidence.effect_applied=True;self.evidence.identity=copy.deepcopy(self.after);self.run_reconcile()
        self.engine.pause_fence('suites').write_bytes(b'pressure\n' if reason=='pressure' else b'manual\n')
        self.assertEqual(self.engine.fence_reason('suites'),'pressure' if reason=='pressure' else 'manual')
        with control.locked(self.engine.directory),patch.object(control,'pressure_mini',return_value=dict(load=4,memory=1)):
            self.engine.guard(dict(load=4,memory=1))
        self.assertFalse(any(call[0]=='start' for call in self.p.calls),'no opening over '+reason)
        self.assertIn(reason,self.state()['reasons']['suites'])


def test_deferred_same_body_retries_but_effect_response_never_replays(self):
    self.change_state(reasons={'suites':['manual']});self.assertEqual(self.act()['status'],'held')
    r=cc1.request('remote-run-started');self.evidence.acceptance=copy.deepcopy(r['acceptance'])
    deferred=self.act(r);self.assertEqual(deferred['status'],'deferred');self.assertFalse(deferred['effects'])
    self.assertEqual(self.rpc_calls,[('stash',77)])
    query=dict(r,op='status',action_op=r['op']);self.assertEqual(self.act(query)['status'],'deferred')
    changed=copy.deepcopy(r);changed['expected']['fingerprint']='f'*64
    self.assertEqual(self.act(changed)['reason'],'idempotency_conflict')
    self.change_state(reasons={});self.after=copy.deepcopy(cc1.CASES['remote-run-started']['response']['identity'])
    applied=self.act(r);self.assertEqual(applied['status'],'started','exact deferred request can retry')
    revision=self.actions.record(r)['revision']
    self.actions=a.ClientActions(self.engine,self.evidence,rpc=lambda *args:self.fail('replayed effect'))
    self.assertEqual(self.act(r),applied);self.assertEqual(self.act(query)['status'],'started')
    self.assertEqual(self.actions.record(r)['revision'],revision)
    self.assertEqual(self.rpc_calls,[('stash',77),('start',77)])


def test_slow_collector_with_many_open_actions_has_one_batch_budget_and_fair_index(self):
    for n in range(40):
        r=copy.deepcopy(self.r);r['action_id']='many-'+str(n).zfill(8);self.orphan(r)
    self.evidence.slow=2;started=time.monotonic()
    self.guard_tick()
    self.assertLess(time.monotonic()-started,1.5,'guard must not wait per action')
    with self.engine.connect() as db:
        self.assertEqual(len(list(db.execute('SELECT checked FROM client_open WHERE checked>0'))),a.Reconciler.limit)
        plan=str(list(db.execute('EXPLAIN QUERY PLAN SELECT launcher,ref,action_id FROM client_open ORDER BY checked LIMIT 8')))
        self.assertIn('client_open_checked',plan)
    first={a.ClientActions.key(v['record']['request']) for v in a.Reconciler(self.engine).copies(None)}
    second={a.ClientActions.key(v['record']['request']) for v in a.Reconciler(self.engine).copies(None)}
    self.assertFalse(first&second,'rotating index must eventually revisit every orphan')
    self.assertTrue(all(v['open'] for v in a.reservations(self.engine)))


def test_bounded_legacy_index_migration_preserves_history_and_protects_hosts(self):
    for n in range(27):
        r=copy.deepcopy(self.r);r['action_id']='legacy-'+str(n).zfill(8);self.orphan(r)
    with self.engine.connect(write=True) as db:
        original=list(db.execute('SELECT body FROM client_actions ORDER BY action_id'))
        db.execute('DROP TABLE client_open');db.execute('DROP TABLE client_scan')
    self.assertTrue(self.engine.client_blocked('default',opening=True),'incomplete legacy index closes all groups')
    with self.assertRaises(ValueError):a.protected_ids(self.engine)
    reconciler=a.Reconciler(self.engine,self.evidence,birth=lambda pid:None)
    for n in range(4):
        copies=reconciler.copies(None)
        self.assertLessEqual(len(copies),a.Reconciler.limit)
        with self.engine.connect() as db:
            cursor,done=db.execute('SELECT cursor,done FROM client_scan').fetchone()
            self.assertLessEqual(cursor,(n+1)*8)
            self.assertEqual(list(db.execute('SELECT body FROM client_actions ORDER BY action_id')),original)
        if n<3:self.assertFalse(done)
    self.assertTrue(done)
    self.assertFalse(self.engine.client_blocked('default',opening=True))
    self.assertTrue(self.engine.client_blocked('suites',opening=True))
    self.assertEqual(a.protected_ids(self.engine),{'77'})


def test_missing_or_changed_authenticated_proof_keeps_reservation_and_acceptance(self):
    r=cc1.request('remote-run-started')
    self.evidence.identity=copy.deepcopy(cc1.CASES['remote-run-started']['response']['identity'])
    self.orphan(r,status='started')
    original=copy.deepcopy(self.actions.reservation(r)['acceptance'])
    finish=finish_request();self.evidence.finish=finish
    self.evidence.identity=finish['expected'];self.evidence.settled=True;self.evidence.effect_applied=True
    self.evidence.acceptance=finish['acceptance'];self.evidence.proof=None
    self.run_reconcile()
    self.assertTrue(self.actions.reservation(r)['open'])
    self.assertEqual(self.actions.reservation(r)['reconcile_reason'],'evidence_missing')
    self.assertIsNone(self.actions.record(finish))
    self.evidence.proof=finish['proof']
    self.evidence.finish['acceptance']['receipt']='f'*64
    self.evidence.finish['proof']['acceptance_receipt']='f'*64
    self.evidence.acceptance=self.evidence.finish['acceptance'];self.evidence.proof=self.evidence.finish['proof']
    self.run_reconcile()
    self.assertTrue(self.actions.reservation(r)['open'])
    self.assertEqual(self.actions.reservation(r)['acceptance'],original,'first acceptance fixed')
    self.assertIsNone(self.actions.record(finish))


def test_dead_reorder_owner_never_opens_over_retained_client_reservation(self):
    def die(phase):
        if phase=='edited':raise test_reorder.Crash()
    self.engine.reorder_boundary=die
    with self.assertRaises(test_reorder.Crash):self.engine.handle(self.order_request('orphan-order-0001'))
    self.engine.reorder_boundary=lambda phase:None
    self.orphan()
    self.run_reconcile()
    self.p.calls.clear()
    self.engine.reconcile()
    self.assertFalse(any(call[0]=='start' for call in self.p.calls),'dead reorder cannot open over unknown')
    self.assertEqual(self.p.groups['suites']['status'],'Paused')
    self.assertIn('pressure',self.state()['reasons']['suites'])
    self.assertTrue(self.actions.reservation(self.r)['open'])


def test_rejected_finish_receipt_cannot_be_replaced_by_reconciler(self):
    finish=finish_request();self.evidence.identity=finish['expected']
    self.orphan(cc1.request('remote-run-started'),status='started')
    rejected=dict(request=finish,digest=a.digest(finish),revision=0,process=None,
                  response=self.actions.reply(finish,'rejected','evidence_missing',held=True))
    self.actions.save(rejected)
    self.evidence.finish=finish;self.evidence.settled=True;self.evidence.effect_applied=True
    self.evidence.acceptance=finish['acceptance'];self.evidence.proof=finish['proof']
    before=copy.deepcopy(self.actions.record(finish))
    self.run_reconcile()
    self.assertEqual(self.actions.record(finish),before,'rejected terminal stays historical')
    self.assertTrue(self.actions.reservation(finish)['open'])


def test_client_reservation_prevents_online_and_offline_locked_adoption(self):
    self.engine.reorder_boundary=lambda p:(_ for _ in ()).throw(test_reorder.Crash()) if p=='client_sent' else None
    with self.assertRaises(test_reorder.Crash):self.engine.handle(self.order_request('locked-client-0001'))
    self.engine.reorder_boundary=lambda p:None
    self.orphan()
    queue=dict(groups=copy.deepcopy(self.p.groups),tasks=copy.deepcopy(self.tasks))
    with self.assertRaises(ValueError):self.engine.reorder_offline(queue,self.state(),self.root)
    self.assertEqual(queue['tasks'],self.tasks)
    with self.assertRaises(ValueError):self.engine.reconcile()
    self.assertFalse(any(call[0]=='enqueue' for call in self.p.calls))
    self.assertTrue(any('Locked' in t['status'] for t in self.tasks.values()))



def test_settled_external_stash_never_attributes_hold_or_allows_followups(self):
    self.r['mode']='own-legacy'
    self.orphan(process='gated');self.evidence.identity=copy.deepcopy(self.after)
    self.evidence.settled=True;self.evidence.effect_applied=False
    self.assertEqual(self.run_reconcile(),1)
    reserve=self.actions.reservation(self.r)
    self.assertEqual(self.actions.record(self.r)['response']['status'],'unknown')
    self.assertTrue(reserve['open']);self.assertTrue(reserve['uncertain'])
    self.assertIsNone(reserve['hold_identity'])
    for op in ('cancel-no-payload','retire-held'):
        r=dict(self.r,op=op,phase=op,expected=copy.deepcopy(self.after))
        if op=='cancel-no-payload':
            r['acceptance']=cc1.request('remote-run-started')['acceptance']
        self.evidence.acceptance=r['acceptance']
        response=self.act(r)
        self.assertEqual((response['status'],response['reason']),('rejected','evidence_missing'))
        self.assertIsNone(self.actions.reservation(r)['hold_identity'])
    self.assertEqual(self.rpc_calls,[])


def test_race_rejected_unsent_hold_closes_without_rewriting_receipt(self):
    for state in ('Locked','Stashed'):
        with self.subTest(state=state):
            with self.engine.connect(write=True) as db:
                for table in ('client_actions','client_reservations','client_open'):
                    if db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone():db.execute('DELETE FROM '+table)
            r=copy.deepcopy(self.r);r['action_id']='race-'+state.lower()+'-0001'
            changed=dict(r['expected'],state='Locked',enqueued_at=None) if state=='Locked' else self.after
            self.evidence.identity=copy.deepcopy(r['expected']);self.evidence.reads=0
            self.evidence.change_at={2:copy.deepcopy(changed)}
            response=self.act(r);before=copy.deepcopy(self.actions.record(r))
            self.assertEqual(response['status'],'rejected');self.assertTrue(self.actions.reservation(r)['open'])
            self.assertEqual(a.Reconciler(self.engine,birth=lambda p:None).run(),1)
            self.assertFalse(self.actions.reservation(r)['open'],'journal proves rejected before child launch')
            self.assertIsNone(self.actions.reservation(r)['hold_identity'])
            self.assertEqual(self.actions.record(r),before,'receipt, digest and revision fixed')
            self.assertEqual(self.act(r),response,'rejected is idempotent after closure')
    self.assertEqual(self.rpc_calls,[])


def test_rejected_hold_cannot_close_with_uncertainty_or_prior_effect(self):
    for prior in ('uncertain','effect','acceptance','child','attributed'):
        with self.subTest(prior=prior):
            r=copy.deepcopy(self.r);r['action_id']='reject-'+prior+'-0001'
            self.orphan(r,process=None)
            record=self.actions.record(r);reserve=self.actions.reservation(r)
            record['response']=self.actions.reply(r,'rejected','locked',held=True);reserve['uncertain']=False
            if prior=='uncertain':reserve['uncertain']=True
            elif prior=='child':record['process']=dict(state='injected')
            elif prior=='attributed':reserve['hold_identity']=copy.deepcopy(self.after)
            elif prior=='acceptance':reserve['acceptance']=dict(cc1.request('remote-run-started')['acceptance'],action_id=r['action_id'])
            else:
                old=dict(r,op='signal',phase='signal:TERM',signal='TERM',mode='own',
                         expected=copy.deepcopy(cc1.request('remote-run-started')['expected']))
                old['expected']=copy.deepcopy(cc1.CASES['remote-run-started']['response']['identity'])
                self.actions.save(dict(request=old,digest=a.digest(old),revision=0,process=dict(state='injected'),
                                       response=self.actions.reply(old,'signalled',identity=old['expected'],held=True)))
            self.actions.save(record,reserve)
            a.Reconciler(self.engine,birth=lambda p:None).run()
            self.assertTrue(self.actions.reservation(r)['open'],'no closure from '+prior)
    self.assertEqual(self.rpc_calls,[])


def test_non_deferred_receipts_never_reevaluate(self):
    for status in ('unknown','rejected','held'):
        with self.subTest(status=status):
            r=copy.deepcopy(self.r);r['action_id']='fixed-'+status+'-0001'
            response=self.actions.reply(r,status,'rpc_unresolved' if status=='unknown' else 'stale_state' if status=='rejected' else None,
                                        identity=self.after if status=='held' else None,held=status!='rejected')
            self.actions.save(dict(request=r,digest=a.digest(r),revision=0,process=None,response=response))
            before=self.actions.record(r)
            with patch.object(self.actions,'execute',return_value=self.actions.reply(r,'rejected','unsupported')) as execute:
                self.assertEqual(self.act(r),response)
                self.assertFalse(execute.called,'only deferred reevaluates: '+status)
            self.assertEqual(self.actions.record(r),before)


def test_identical_retention_skips_journal_write_but_advances_index(self):
    self.orphan()
    reconciler=a.Reconciler(self.engine,birth=lambda p:None)
    self.assertEqual(reconciler.run(),1)
    before=(self.actions.record(self.r),self.actions.reservation(self.r))
    with self.engine.connect() as db:checked=db.execute('SELECT checked FROM client_open').fetchone()[0]
    with patch.object(a.ClientActions,'save',wraps=self.actions.save) as save:
        reconciler.run()
        self.assertFalse(save.called,'identical retention must not write journal')
    self.assertEqual((self.actions.record(self.r),self.actions.reservation(self.r)),before,'journal revisions stay fixed')
    with self.engine.connect() as db:self.assertGreater(db.execute('SELECT checked FROM client_open').fetchone()[0],checked)


def collection_lock_holder(path,pipe,seconds):
    with open(path,'rb') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        pipe.send('locked');time.sleep(seconds)
    pipe.close()


LAUNCHER="import fcntl,sys,time\nh=open(sys.argv[1],'rb')\nprint('waiting',flush=True)\nfcntl.flock(h,fcntl.LOCK_EX)\nprint('acquired',flush=True)\ntime.sleep(float(sys.argv[2]))\n"


def test_queued_launcher_never_preempts_protective_guard(self):
    """R1-opus-1: a real launcher queued on control.lock behind the Mini guard."""
    with self.engine.connect(write=True) as db:
        for table in ('client_actions','client_reservations','client_open'):
            if db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone():db.execute('DELETE FROM '+table)
    record=dict(request=self.r,digest=a.digest(self.r),revision=0,process=None,
                response=self.actions.reply(self.r,'rejected','stale_state'))
    reserve=dict(launcher=self.r['launcher'],ref=self.r['ref'],action_id=self.r['action_id'],group='suites',mode=self.r['mode'],
                 identity=self.r['expected'],acceptance=None,hold_identity=None,open=False,uncertain=False)
    self.actions.save(record,reserve)
    with self.engine.connect() as db:self.assertEqual(list(db.execute('SELECT * FROM client_open')),[],'only closed reservations')
    for hold in (.15,.3):
        with self.subTest(hold=hold):
            calls=[];gathered=[]
            collector=patch.object(a.Reconciler,'bounded_gather',side_effect=lambda copies,offline:gathered.append(copies) or [])
            with patch.object(self.engine,'_legacy_guard',side_effect=lambda *x,**k:calls.append('protective-guard')),collector:
                control.reconcile_before_lock(self.engine)  # what control.main guardia runs first
                with control.locked(self.engine.directory):
                    launcher=subprocess.Popen([sys.executable,'-c',LAUNCHER,str(self.engine.directory/'control.lock'),str(hold)],
                                              stdout=subprocess.PIPE,close_fds=True,text=True)
                    self.addCleanup(launcher.stdout.close)
                    self.addCleanup(lambda p=launcher:(p.poll() is None and p.kill(),p.wait()))
                    self.assertEqual(launcher.stdout.readline().strip(),'waiting');time.sleep(.1)
                    self.assertEqual(self.engine.reconcile_clients(),0,'under a caller lock CC2 does nothing')
                    self.engine.guard(dict(load=40,memory=95))
                    ready=select.select([launcher.stdout],[],[],.2)[0]
                    self.assertEqual(ready,[],'the queued launcher entered while the guard held control.lock')
            self.assertEqual(launcher.stdout.readline().strip(),'acquired')
            self.assertEqual(launcher.wait(timeout=5),0)
            self.assertEqual(calls,['protective-guard'])
            self.assertEqual(gathered,[],'an empty index launches no collector process')
    # An open reservation inside the caller's section changes nothing either.
    r=self.orphan(process=None)
    with control.locked(self.engine.directory):
        self.assertEqual(self.engine.reconcile_clients(),0)
        with (self.engine.directory/'control.lock').open('rb') as h:
            with self.assertRaises(BlockingIOError):fcntl.flock(h,fcntl.LOCK_EX|fcntl.LOCK_NB)
    self.assertTrue(self.actions.reservation(r)['open'])
    self.assertEqual(control.reconcile_before_lock(self.engine),1)
    self.assertFalse(self.actions.reservation(r)['open'])


class Forged(Evidence):
    """Evidence whose adapter invents an effect proof instead of reading the launcher's record."""
    forge=None
    def recover(self,r,state,reservation,process,daemon):
        out=super().recover(r,state,reservation,process,daemon)
        if self.forge:self.forge(out,r,reservation,process)
        return out


def forged_case(self,r,forge=None,effect=None):
    self.evidence=Forged(r['expected'],self.engine.directory);self.engine.client_adapter=self.evidence
    self.actions=a.ClientActions(self.engine,self.evidence,rpc=lambda *args:self.rpc(*args))
    self.orphan(r,process='gated')
    self.evidence.identity=copy.deepcopy(self.after);self.evidence.settled=True
    if effect is not None:
        self.evidence.launcher_effects[a.ClientActions.key(r)]=effect;self.evidence.effect_applied=True
    self.evidence.forge=forge
    self.run_reconcile()


def assert_unattributed(self,r):
    reserve=self.actions.reservation(r)
    self.assertEqual(self.actions.record(r)['response']['status'],'unknown')
    self.assertTrue(reserve['open']);self.assertIsNone(reserve['hold_identity'])
    follow=dict(r,op='cancel-no-payload',phase='cancel-no-payload',expected=copy.deepcopy(self.after))
    follow['acceptance']=dict(cc1.request('remote-run-started')['acceptance'],action_id=r['action_id'])
    self.evidence.acceptance=follow['acceptance']
    response=self.act(follow)
    self.assertEqual((response['status'],response['reason']),('rejected','evidence_missing'))
    self.assertEqual(self.rpc_calls,[])


ReconcileTests.forged_case=forged_case
ReconcileTests.assert_unattributed=assert_unattributed


def test_effect_derived_from_daemon_never_attributes_hold(self):
    """R1-opus-2: an adapter that derives the effect from the daemon, not the launcher record."""
    def legacy(out,r,reservation,process):
        seen=out['observation']['identity']
        out['effect_applied']=seen['state']=='Stashed' and seen['task_id']==reservation['identity']['task_id']
    def truthy(out,r,reservation,process):out['effect']=True
    def from_daemon(out,r,reservation,process):
        seen=out['observation']['identity']
        out['effect']=dict(zip(a.Reconciler.EFFECT_FIELDS,a.ClientActions.key(r)+(seen['task_id'],str(seen['created_at']))))
    def from_request(out,r,reservation,process):
        out['effect']=dict(launcher=r['launcher'],ref=r['ref'],action_id=r['action_id'],phase=r['phase'])
    for name,forge in (('effect_applied',legacy),('effect-true',truthy),('daemon',from_daemon),('request',from_request)):
        with self.subTest(forge=name):
            r=copy.deepcopy(self.r);r['mode']='own-legacy';r['action_id']='derived-'+name.replace('_','-')+'-0001'
            self.rpc_calls.clear()
            self.forged_case(r,forge)
            self.assert_unattributed(r)


def test_effect_bound_to_key_phase_and_child_identity(self):
    """R1-opus-2: a launcher record for another key, phase or child never proves this effect."""
    for name in ('action','phase','birth','pid','missing','exact'):
        with self.subTest(change=name):
            r=copy.deepcopy(self.r);r['mode']='own-legacy';r['action_id']='bound-'+name+'-0001'
            effect=dict(zip(a.Reconciler.EFFECT_FIELDS,a.ClientActions.key(r)+(999999,'synthetic:123')))
            if name=='action':effect['action_id']='bound-other-00000001'
            elif name=='phase':effect['phase']='cancel-no-payload'
            elif name=='birth':effect['birth']='synthetic:124'
            elif name=='pid':effect['pid']=999998
            elif name=='missing':effect.pop('birth')
            self.rpc_calls.clear()
            self.forged_case(r,effect=effect)
            if name=='exact':
                # The positive synthetic still closes the cycle: hold attributed, no RPC.
                self.assertEqual(self.actions.record(r)['response']['status'],'held')
                self.assertEqual(self.actions.reservation(r)['hold_identity'],self.after)
                self.assertEqual(self.rpc_calls,[])
            else:
                self.assert_unattributed(r)


def test_autonomous_finish_preserves_deferred_request_digest(self):
    finish=finish_request();self.evidence.identity=finish['expected']
    self.orphan(cc1.request('remote-run-started'),status='started')
    old=copy.deepcopy(finish);old['proof']['receipt']='e'*64
    self.actions.save(dict(request=old,digest=a.digest(old),revision=0,process=None,
                           response=self.actions.reply(old,'deferred','host_action',held=True)))
    before=copy.deepcopy(self.actions.record(old))
    self.evidence.finish=finish;self.evidence.settled=True
    self.evidence.acceptance=finish['acceptance'];self.evidence.proof=finish['proof']
    self.run_reconcile()
    self.assertEqual(self.actions.record(old),before,'finish digest is fixed even for deferred')
    self.assertTrue(self.actions.reservation(old)['open'])
    self.evidence.finish=old;self.evidence.proof=old['proof']
    self.run_reconcile()
    self.assertFalse(self.actions.reservation(old)['open'],'same deferred body may finish')
    self.assertEqual(self.actions.record(old)['digest'],a.digest(old))


for name,value in list(globals().items()):
    if name.startswith('test_'):setattr(ReconcileTests,name,value)


class DeskReservationsTests(test_trickle.TrickleTests):
    def setUp(self):
        super().setUp()
        r=cc1.request();self.r=r;self.actions=a.ClientActions(self.engine)
        record=dict(request=r,digest=a.digest(r),revision=0,process=dict(state='injected'),
                    response=self.actions.reply(r,'unknown','rpc_unresolved',held=True))
        reserve=dict(launcher=r['launcher'],ref=r['ref'],action_id=r['action_id'],group='jale',mode=r['mode'],
                     identity=r['expected'],acceptance=None,hold_identity=None,open=True,uncertain=True)
        self.actions.save(record,reserve)
        self.tasks={k:v for k,v in self.tasks.items() if v['group']=='jale'}

    def test_desk_one_retains_client_reservation(self):
        self.run_guard();self.assertEqual(self.starts(),[],'desk _one must consult client reservations')
        self.assertEqual(self.p.groups['jale']['status'],'Paused');self.assertIn('reason',self.causes())

    def test_actual_guard_block_reaches_rack_snapshot_and_panel(self):
        import importlib.util
        rack=Path(__file__).resolve().parents[2]/'rack'
        def load(name):
            spec=importlib.util.spec_from_file_location('cc2_rack_'+name,rack/(name+'.py'))
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
        snapshot=load('snapshot');serve=load('serve')
        for sample in (self.mid,self.low):
            self.sample=sample;self.run_guard(sample)
            row=next(v for v in reversed(self.rows()) if 'bloqueos' in v)
            value=dict(last_result=dict(evento=row['evento'],tick_id=row['tick_id'],at=row['utc'],bloqueos=row['bloqueos']))
            try:self.assertEqual(snapshot.validate_guard(value),value)
            except ValueError as error:self.fail('actual guard rejected by snapshot: '+str(error))
            self.assertEqual(value['last_result']['bloqueos'][0]['causa'],'reason')
            # serve uses this same validator before exposing the snapshot to index.html.
            data=dict(schema='rack-host@1',host='desk',os='linux',taken=1000,cpu=None,mem=None,disk=None,
                      guard=value,groups=[dict(name='jale',status='Paused',slots=1)],tasks=[],errors=[])
            public=serve.validate_snapshot(data,'desk')
            self.assertEqual(public['guard']['last_result']['bloqueos'],row['bloqueos'])
            self.assertIn("reason:",(rack/'index.html').read_text())
            self.assertEqual(self.starts(),[])

    def test_desk_guard_retains_pressure_reason_and_unknown(self):
        self.sample=self.low;self.run_guard(self.low)
        self.assertEqual(self.p.groups['jale']['status'],'Paused','desk guard must retain unknown')
        self.assertEqual(self.state()['reasons']['jale'],['pressure']);self.assertTrue(self.actions.reservation(self.r)['open'])
        self.assertFalse(self.engine.view()['capabilities']['client'])


for name in dir(test_trickle.TrickleTests):
    if name.startswith('test_') and name not in DeskReservationsTests.__dict__:setattr(DeskReservationsTests,name,None)


def unsent_hold(h,action_id):
    """A hold the parent journaled and never sent: CC2 closes it when it can run."""
    r=cc1.request();r['action_id']=action_id
    actions=a.ClientActions(h.engine)
    record=dict(request=r,digest=a.digest(r),revision=0,process=None,
                response=actions.reply(r,'unknown','rpc_unresolved',identity=r['expected'],held=True))
    reserve=dict(launcher=r['launcher'],ref=r['ref'],action_id=r['action_id'],group='suites',mode=r['mode'],
                 identity=r['expected'],acceptance=None,hold_identity=None,open=True,uncertain=True)
    with control.locked(h.directory):actions.save(record,reserve)
    return r,actions


def caller_holds_lock(test,h):
    with (h.directory/'control.lock').open('rb') as other:
        with test.assertRaises(BlockingIOError,msg='work must run under the caller control.lock'):
            fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)


def lock_holder(path,pipe,seconds):
    with open(path,'rb') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        pipe.send('locked');time.sleep(seconds)
    pipe.close()


class StartAuthorizationTests(test_handoff.HandoffTests):
    def held_lock(self,path,seconds):
        """A real foreign holder (a guard tick, an admission) of a file lock."""
        ctx=multiprocessing.get_context('fork');parent,child=ctx.Pipe()
        holder=ctx.Process(target=lock_holder,args=(str(path),child,seconds));holder.start();child.close()
        def stop():
            holder.join(5)
            if holder.is_alive():holder.terminate();holder.join()
            parent.close()
            self.assertEqual(holder.exitcode,0)
        self.addCleanup(stop)
        self.assertTrue(parent.poll(3));self.assertEqual(parent.recv(),'locked')
        return holder

    def main_guard(self,spy=None):
        h=self.h;calls=[]
        guard=spy or (lambda *x,**k:calls.append('protective-guard'))
        with patch.object(control.Path,'home',return_value=h.home),patch.object(control,'Pueue',return_value=h.p), \
             patch.object(control,'MiniControl',return_value=h.engine),patch.object(control,'recover_mini',return_value=True), \
             patch.object(control,'prune_history'), patch.object(h.engine,'_legacy_guard',side_effect=guard), \
             patch.object(control.sys,'argv',['control.py','guardia','--host','mini']):
            control.main()
        return calls

    def test_start_rechecks_handoff_after_collection_window(self):
        h=self.h;h.m.change('install');h.finish()
        h.p.online=False;h.owner=None
        executed=[]
        def changed(*args,**kwargs):
            with h.engine.connect(write=True) as db:
                state=h.engine.state(db);state['mini_handoff']['phase']='maintenance';h.engine.store(db,state)
        with patch.object(control,'prepare_start',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'Autorización de arranque cambió'):
                h.m.start(execute=lambda *args:executed.append(args))
        self.assertEqual(executed,[])

    def test_every_productive_entry_reconciles_before_its_locked_work(self):
        """Point 5: guardia, preparar, the launchd wrapper and recuperar all run CC2 first."""
        h=self.h;h.m.change('install');h.finish()
        seen={}
        def checked(entry,r,actions,result=None):
            def spy(*args,**kwargs):
                caller_holds_lock(self,h)
                seen[entry]=actions.reservation(r)['open']
                return result
            return spy
        r,actions=unsent_hold(h,'entry-guardia-0001')
        with patch.object(control.Path,'home',return_value=h.home),patch.object(control,'Pueue',return_value=h.p), \
             patch.object(control,'MiniControl',return_value=h.engine),patch.object(control,'prune_history'), \
             patch.object(control,'guard',side_effect=checked('guardia',r,actions)), \
             patch.object(control.sys,'argv',['control.py','guardia','--host','mini']):
            control.main()
        for argv,entry in ((['--seco'],'preparar-seco'),([],'preparar')):
            r,actions=unsent_hold(h,'entry-'+entry+'-0001')
            with patch.object(control.Path,'home',return_value=h.home),patch.object(control,'Pueue',return_value=h.p), \
                 patch.object(control,'MiniControl',return_value=h.engine),patch.object(control,'start_configuration',return_value=h.config), \
                 patch.object(control,'prepare_start',side_effect=checked(entry,r,actions)), \
                 patch.object(control.sys,'argv',['control.py','preparar','--host','mini']+argv):
                control.main()
        h.p.online=False;h.owner=None
        r,actions=unsent_hold(h,'entry-arrancar-0001');executed=[]
        with patch.object(control,'prepare_start',side_effect=checked('arrancar',r,actions)):
            h.m.start(execute=lambda *args:executed.append(args))
        self.assertEqual(len(executed),1)
        r,actions=unsent_hold(h,'entry-recuperar-0001')
        with patch.object(mini,'Manager',return_value=h.m),patch.object(h.m,'_recover',side_effect=checked('recuperar',r,actions,False)), \
             patch.object(mini.sys,'argv',['mini.py','recuperar']):
            mini.main()
        self.assertEqual(seen,{'guardia':False,'preparar-seco':True,'preparar':False,'arrancar':False,'recuperar':False},
                         'every productive entry closes the unsent hold first; a dry run reconciles nothing')

    def test_whole_guard_tick_runs_protective_guard_despite_reconciler_failure_or_contention(self):
        """Point 4: CC2 never suppresses the guard; a contended pass is revisited later."""
        h=self.h
        r,actions=unsent_hold(h,'tick-failure-0001')
        with patch.object(h.engine,'reconcile_clients',side_effect=RuntimeError('collector bug')):
            try:calls=self.main_guard()
            except RuntimeError as error:self.fail('a reconciler failure suppressed the guard: '+str(error))
        self.assertEqual(calls,['protective-guard'])
        self.assertTrue(actions.reservation(r)['open'])
        self.assertEqual(self.main_guard(),['protective-guard'])
        self.assertFalse(actions.reservation(r)['open'],'the next tick reconciles')
        r,actions=unsent_hold(h,'tick-contended-0001')
        timing={}
        before=control.reconcile_before_lock
        def timed(*args,**kwargs):
            started=time.monotonic()
            try:return before(*args,**kwargs)
            finally:timing['before']=time.monotonic()-started
        def guard(*args,**kwargs):
            timing['guard_at']=time.monotonic()-timing['tick']
        self.held_lock(h.directory/'control.lock',.3)
        timing['tick']=time.monotonic()
        with patch.object(control,'reconcile_before_lock',side_effect=timed):
            self.assertEqual(self.main_guard(guard),[])
        total=time.monotonic()-timing['tick']
        print('\ntick-contention-0.3 before=%.3f guard_at=%.3f total=%.3f' % (timing['before'],timing['guard_at'],total))
        self.assertLess(timing['before'],.1,'the reconciler never waits for a busy control.lock')
        self.assertGreaterEqual(timing['guard_at'],.2,'the guard itself waits its own lock, as in main')
        self.assertTrue(actions.reservation(r)['open'],'a contended pass changes nothing')
        self.assertEqual(self.main_guard(),['protective-guard'])
        self.assertFalse(actions.reservation(r)['open'],'and a later tick revisits it')

    def test_contended_start_rejects_within_deadline_with_diagnostic(self):
        """R1-sol-1: Manager.start under a foreign holder of 0.7 s."""
        for when in ('before','after-collection','log-held'):
            with self.subTest(when=when):
                self.h=test_handoff.Harness(self);h=self.h
                h.m.change('install');h.finish();h.p.online=False;h.owner=None
                r,actions=unsent_hold(h,'busy-'+when+'-0001')
                before=(h.data/'state.json').read_bytes();executed=[]
                gather=a.Reconciler.bounded_gather
                def collect(reconciler,copies,offline):
                    proposals=gather(reconciler,copies,offline)
                    self.held_lock(h.directory/'control.lock',.7)  # a guard tick enters right after collection
                    return proposals
                if when=='before':self.held_lock(h.directory/'control.lock',.7)
                if when=='log-held':
                    (h.directory/'arranque.log').touch()
                    self.held_lock(h.directory/'arranque.log',.7);self.held_lock(h.directory/'control.lock',.7)
                started=time.monotonic()
                with patch.object(a.Reconciler,'bounded_gather',collect if when=='after-collection' else gather):
                    with self.assertRaises(ValueError) as raised:
                        h.m.start(execute=lambda *args:executed.append(args))
                elapsed=time.monotonic()-started
                print('\nstart-busy-%s elapsed=%.3f' % (when,elapsed))
                self.assertEqual(str(raised.exception),control.CONTROL_BUSY)
                self.assertLess(elapsed,.45);self.assertEqual(executed,[])
                self.assertEqual((h.data/'state.json').read_bytes(),before)
                self.assertTrue(actions.reservation(r)['open'])
                log=(h.directory/'arranque.log').read_text().splitlines()
                if when=='log-held':
                    # Documented: a stuck diagnostic writer loses this line, never the reject.
                    self.assertEqual(log,[])
                else:
                    self.assertEqual(json.loads(log[-1])['motivo'],control.CONTROL_BUSY)


for name in dir(test_handoff.HandoffTests):
    if name.startswith('test_') and name not in StartAuthorizationTests.__dict__:setattr(StartAuthorizationTests,name,None)


if __name__=='__main__':unittest.main()
