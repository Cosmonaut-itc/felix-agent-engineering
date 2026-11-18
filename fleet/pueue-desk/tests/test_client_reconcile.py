"""CC2: fake daemon/evidence, private journals, real flock/collector processes."""
import copy
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch
import test_client_actions as cc1
import test_reorder
import test_trickle
import test_handoff

control,a,c=cc1.control,cc1.a,cc1.c


def finish_request():
    return next(copy.deepcopy(v['request']) for v in cc1.CORPUS['valid'] if
                v['request']['op']=='finish' and v['request']['mode']=='rack-queued')


class Evidence(cc1.Evidence):
    def __init__(self,identity,directory):
        super().__init__(identity)
        self.directory=Path(directory)
        self.settled=False
        self.effect_applied=False
        self.effect_receipts={}
        self.finish=None
        self.slow=0
        self.result_lock=False

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
                    **(dict(effect_applied=copy.deepcopy(self.effect_receipts.get(
                        tuple(r[k] for k in ('launcher','ref','action_id','phase'))))) if self.effect_applied else {}))


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
        # Synthetic launcher authority publishes a phase/process receipt before
        # recovery. recover only reads that artifact, not a daemon-derived claim.
        self.evidence.effect_receipts[tuple(r[k] for k in ('launcher','ref','action_id','phase'))] = dict(
            {k:r[k] for k in ('launcher','ref','action_id','phase')},pid=process['pid'],birth=process['birth'])
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
    reconcile=a.Reconciler(self.engine,self.evidence,birth=lambda pid:None)
    return reconcile.run(offline=offline)


def state(self):
    with self.engine.connect() as db:return self.engine.state(db)


def prepare(self):
    data=self.root/'offline';data.mkdir(exist_ok=True)
    path=data/'state.json'
    path.write_text(json.dumps(dict(groups=copy.deepcopy(self.p.groups),tasks=copy.deepcopy(self.tasks))))
    with control.locked(self.engine.directory),patch.object(control,'assert_offline'):
        return control.prepare_start(self.engine,path,dict(data=data,pid_file=Path(self.state()['pid_file'])))


ReconcileTests.setUp=setup
ReconcileTests.orphan=orphan
ReconcileTests.run_reconcile=run_reconcile
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
                        with control.locked(self.engine.directory):self.engine.guard(dict(load=0,memory=1))
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
    with control.locked(self.engine.directory):self.engine.guard(dict(load=4,memory=1))
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
    with self.engine.connect() as db:checked=db.execute('SELECT checked FROM client_open').fetchone()[0]
    reconciler.copies(None)
    with self.engine.connect() as db:self.assertGreater(db.execute('SELECT checked FROM client_open').fetchone()[0],checked)


def collection_lock_holder(path,pipe,seconds):
    with open(path,'rb') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        pipe.send('locked');time.sleep(seconds)
    pipe.close()


def test_whole_guard_tick_protects_before_independent_contended_collection(self):
    self.orphan();self.evidence.identity=copy.deepcopy(self.after)
    self.evidence.settled=True;self.evidence.effect_applied=True
    ctx=multiprocessing.get_context('fork');parent,child=ctx.Pipe()
    holder=None;events=[];continued=[];generations=[]
    gather=a.Reconciler.bounded_gather;generation=self.engine.generation
    legacy_guard=self.engine._legacy_guard
    def protective(*args,**kwargs):
        continued.append('guard')
        with (self.engine.directory/'control.lock').open('rb') as handle:
            with self.assertRaises(BlockingIOError):fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return legacy_guard(*args,**kwargs)
    def collect(reconciler,copies,offline):
        nonlocal holder
        proposals=gather(reconciler,copies,offline)
        holder=ctx.Process(target=collection_lock_holder,args=(str(self.engine.directory/'control.lock'),child,.6))
        holder.start();self.assertTrue(parent.poll(2));self.assertEqual(parent.recv(),'locked')
        return proposals
    def read_generation(*args):
        generations.append(time.monotonic());return generation(*args)
    started=time.monotonic();aborted=False
    try:
        with patch.object(a.Reconciler,'bounded_gather',collect),patch.object(a.Reconciler,'boundary',events.append),patch.object(self.engine,'generation',read_generation),patch.object(self.engine,'_legacy_guard',side_effect=protective):
            try:
                with control.locked(self.engine.directory):self.engine.guard(dict(load=4,memory=1))
            except RuntimeError as error:
                self.assertEqual(str(error),'client_collection_lock_busy');aborted=True
        elapsed=time.monotonic()-started
        print('whole-guard-tick seconds=%.3f generations=%d events=%s aborted=%s'%(elapsed,len(generations),events,aborted),flush=True)
        self.assertLess(elapsed,.45,'full tick includes protective guard, copy, gather, apply attempt and generation reads')
        self.assertFalse(aborted,'an independent collection never aborts the protective guard')
        self.assertEqual(continued,['guard'],'protective guard runs before independent collection')
        self.assertEqual(events,['copied','collected']);self.assertGreaterEqual(len(generations),3,'protective guard and collection generation reads')
        self.assertTrue(self.actions.reservation(self.r)['open'])
        self.assertEqual(self.actions.record(self.r)['response']['status'],'unknown')
    finally:
        if holder:
            holder.join(2)
            if holder.is_alive():holder.terminate();holder.join()
            self.assertEqual(holder.exitcode,0)
        parent.close();child.close()
    with control.locked(self.engine.directory):self.engine.guard(dict(load=4,memory=1))
    self.assertEqual(self.actions.record(self.r)['response']['status'],'held','a contended action is retried on a later tick')


def test_whole_guard_tick_with_contention_after_application(self):
    self.orphan();self.evidence.identity=copy.deepcopy(self.after)
    self.evidence.settled=True;self.evidence.effect_applied=True;self.evidence.slow=.025
    ctx=multiprocessing.get_context('fork');parent,child=ctx.Pipe()
    holder=None;generations=[]
    apply=a.Reconciler.apply;generation=self.engine.generation
    def apply_then_contend(reconciler,*args):
        nonlocal holder
        changed=apply(reconciler,*args)
        self.assertTrue(changed)
        holder=ctx.Process(target=collection_lock_holder,args=(str(self.engine.directory/'control.lock'),child,.04))
        holder.start();self.assertTrue(parent.poll(2));self.assertEqual(parent.recv(),'locked')
        return changed
    def read_generation(*args):
        generations.append(time.monotonic());time.sleep(.005);return generation(*args)
    started=time.monotonic()
    try:
        with patch.object(a.Reconciler,'apply',apply_then_contend),patch.object(self.engine,'generation',read_generation),patch.object(control,'pressure_mini',return_value=dict(load=4,memory=1)):
            with control.locked(self.engine.directory):self.engine.guard(dict(load=4,memory=1))
        elapsed=time.monotonic()-started
        print('whole-guard-tick-success seconds=%.3f generations=%d'%(elapsed,len(generations)),flush=True)
        self.assertLess(elapsed,1.5)
        self.assertGreaterEqual(len(generations),3,'copy, apply and guard authority reads included')
        self.assertEqual(self.actions.record(self.r)['response']['status'],'held')
        self.assertTrue((self.engine.directory/'guardia.log').is_file())
    finally:
        if holder:
            holder.join(2)
            if holder.is_alive():holder.terminate();holder.join()
            self.assertEqual(holder.exitcode,0)
        parent.close();child.close()



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


class StartAuthorizationTests(test_handoff.HandoffTests):
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


for name in dir(test_handoff.HandoffTests):
    if name.startswith('test_') and name not in StartAuthorizationTests.__dict__:setattr(StartAuthorizationTests,name,None)


if __name__=='__main__':unittest.main()
