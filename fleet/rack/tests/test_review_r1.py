"""C4 review R1: blocked durable I/O, bounded desk status and manual recovery."""
import safety
import json
import sqlite3
import subprocess
import tempfile
import time
import shutil
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from test_actions import accion, serve, request, receipt, payload
import test_deadlines as D
import test_actions as T


class ReviewActionsTests(unittest.TestCase):
    read = D.DeadlineTests.read
    execute = D.DeadlineTests.execute
    close_threads = D.DeadlineTests.close_threads
    stored = D.DeadlineTests.stored
    spawn = D.DeadlineTests.spawn
    join = D.DeadlineTests.join

    def setUp(self):
        D.DeadlineTests.setUp(self)
        self.wall = 1000.0
        self.actions.clock = lambda: self.wall

    def reopen(self):
        self.actions = accion.Actions(self.hosts, self.root, self.read, self.actions.executor,
            clock=lambda:self.wall, timer=lambda:self.now)

    def test_b_reproduction_missing_controller_receipt_has_bounded_transports(self):
        body = request(host='desk', launcher='jale', ref='job-AbCd1234')
        self.data['hosts'][1]['snapshot']['tasks'][0]['control'].update(launcher='jale', ref=body['ref'])
        def missing(h,l,w,timeout=None):
            self.calls.append(w['op'])
            if w['op'] == 'cancel': raise ValueError('synthetic ssh 255')
            return receipt(w, 'rejected', 'not_found')
        self.actions.executor = missing
        self.assertEqual(self.actions.post(body)[1]['status'], 'unknown')
        statuses = [self.actions.get(body['action_id'])[1]['status'] for _ in range(5)]
        self.assertEqual(statuses, ['unknown']*5)
        self.assertEqual(self.calls, ['cancel','status'])
        self.assertEqual(self.actions.post(dict(body, action_id='second-0002'))[0],409)
        print('R1-B-1: POST unknown; 5 GET '+str(statuses)+'; new POST=409; transports='+str(self.calls))
        closed = self.actions.close_receipt(body['action_id'])
        self.assertEqual(closed['status'], 'rejected')
        self.assertEqual(self.actions.get(body['action_id'])[1], closed)
        self.assertEqual(self.calls, ['cancel','status'])
        self.assertEqual(self.actions.post(dict(body, action_id='retry-0003'))[1]['status'], 'unknown')
        self.assertEqual(self.calls, ['cancel','status','cancel'])
        print('R1-B-1: manual close=rejected/manually_closed; new cancel admitted; terminal GET transports=0')

    def test_desk_intervals_first_minute_then_30_seconds_and_persist_after_restart(self):
        for status in ('unknown','cancel_unconfirmed','cancelling'):
            with self.subTest(status=status):
                with self.actions.connect() as db: db.execute('DELETE FROM actions')
                self.wall = 1000
                body=request(host='desk')
                def execute(h,l,w,timeout=None):
                    self.calls.append(w['op'])
                    return receipt(w,status)
                self.actions.executor=execute
                self.actions.post(body)
                self.actions.get(body['action_id'])
                self.calls.clear()
                self.wall=1001.999
                self.actions.get(body['action_id'])
                self.assertEqual(self.calls,[])
                self.wall=1002
                self.actions.get(body['action_id'])
                self.assertEqual(self.calls,['status'])
                self.reopen()
                self.actions.get(body['action_id'])
                self.assertEqual(self.calls,['status'])
                self.wall=1059
                self.actions.get(body['action_id'])
                self.calls.clear()
                self.wall=1060
                self.actions.get(body['action_id'])
                self.wall=1088.999
                self.actions.get(body['action_id'])
                self.assertEqual(self.calls,[])
                self.wall=1089
                self.actions.get(body['action_id'])
                self.assertEqual(self.calls,['status'])
                # Thirty seconds is a fixed cap, not an ever-growing delay.
                self.wall=1119
                self.actions.get(body['action_id'])
                self.assertEqual(self.calls,['status','status'])

    def test_simultaneous_tabs_issue_one_status_and_other_actions_have_own_interval(self):
        body=request(host='desk')
        self.actions.executor=lambda h,l,w,timeout=None:receipt(w,'unknown')
        self.actions.post(body)
        entered=threading.Event()
        def block(h,l,w,timeout=None):
            self.calls.append(w['op']);entered.set()
            self.assertTrue(self.release.wait(3))
            return receipt(w,'cancel_unconfirmed')
        self.actions.executor=block
        first=self.spawn(lambda:self.actions.get(body['action_id']))
        self.assertTrue(entered.wait(2))
        tabs=[self.spawn(lambda:self.actions.get(body['action_id'])) for _ in range(6)]
        for worker in tabs:self.assertEqual(self.join(worker)[1]['status'],'unknown')
        self.release.set();self.assertEqual(self.join(first)[1]['status'],'cancel_unconfirmed')
        self.actions.get(body['action_id'])
        self.assertEqual(self.calls,['status'])
        other=request(host='desk',action_id='other-0002',ref='20261112-184714-abcdef',expected=dict(task_id=78,state='Queued'))
        self.actions.post(other)
        self.calls.clear()
        self.actions.get(other['action_id'])
        self.assertEqual(self.calls,['status'])

    def test_mini_still_consults_every_get(self):
        self.actions.executor=lambda h,l,w,timeout=None:(self.calls.append(w['op']) or receipt(w,'unknown'))
        body=request()
        self.actions.post(body)
        for _ in range(5):self.actions.get(body['action_id'])
        self.assertEqual(self.calls,['cancel']+['status']*5)

    def test_manual_close_durable_idempotent_rejects_missing_terminal_and_unblocks(self):
        body=request(host='desk')
        self.actions.executor=lambda h,l,w,timeout=None:receipt(w,'cancel_unconfirmed')
        self.actions.post(body)
        with self.assertRaisesRegex(ValueError,'No existe'):self.actions.close_receipt('missing-000')
        closed=self.actions.close_receipt(body['action_id'])
        self.assertEqual((closed['status'],closed['error']['code']),('rejected','manually_closed'))
        self.assertTrue(closed['error']['message'].startswith('Recibo cerrado a mano'))
        with self.actions.connect() as db:
            updated=db.execute('SELECT updated FROM actions WHERE action_id=?',(body['action_id'],)).fetchone()[0]
            self.assertEqual(db.execute('PRAGMA synchronous').fetchone()[0],2)
        self.wall+=10
        self.reopen()
        self.assertEqual(self.actions.close_receipt(body['action_id']),closed)
        self.assertEqual(self.actions.post(body)[1],closed)
        with self.actions.connect() as db:
            self.assertEqual(db.execute('SELECT updated FROM actions WHERE action_id=?',(body['action_id'],)).fetchone()[0],updated)
        self.assertEqual(self.actions.post(dict(body,expected=dict(task_id=77,state='Running')))[0],409)
        self.actions.executor=self.execute
        other=dict(body,action_id='new-attempt-0002')
        self.assertEqual(self.actions.post(other)[1]['status'],'cancelled')
        with self.assertRaisesRegex(ValueError,'ya es terminal'):self.actions.close_receipt(other['action_id'])
        self.assertEqual(self.stored(body),closed)

    def test_manual_cli_and_late_transport_share_sqlite_writer_lock(self):
        # A separate Actions instance models the CLI process; no shared RLock.
        body=request(host='desk')
        self.actions.executor=lambda h,l,w,timeout=None:receipt(w,'unknown')
        self.actions.post(body)
        entered=threading.Event()
        def block(h,l,w,timeout=None):
            entered.set();self.assertTrue(self.release.wait(3));return receipt(w,'cancelled')
        self.actions.executor=block
        first=self.spawn(lambda:self.actions.get(body['action_id']))
        self.assertTrue(entered.wait(2))
        admin=accion.Actions([],self.root,lambda:None)
        closed=admin.close_receipt(body['action_id'])
        self.release.set()
        self.assertEqual(self.join(first)[1],closed)
        self.assertEqual(self.actions.get(body['action_id'])[1],closed)
        self.assertEqual(self.stored(body),closed)


class HTTPJournalTests(unittest.TestCase):
    def test_a_slow_commit_returns_before_ui_deadline_and_never_launches_late(self):
        # Reproduction A adapted to the HTTP boundary. Virtual fsync consumes 46 s,
        # but is held by an Event so the handler can return at 44 (14 for Mini).
        for host,budget in (('desk',44),('mini',14)):
            with self.subTest(host=host):
                case=T.AuthorizationTests(methodName='test_full_http_roundtrip_and_no_cors');case.setUp()
                release,entered=threading.Event(),threading.Event()
                workers=[]
                try:
                    case.actions.hosts={h['id']:h for h in serve.load_hosts(Path(__file__).resolve().parents[1]/'hosts.json')}
                    case.data['hosts'][0]['id']=host
                    now=[1000.0];case.actions.timer=lambda:now[0]
                    real_connect=sqlite3.connect
                    class SlowCommit(sqlite3.Connection):
                        def __exit__(db,*exc):
                            entered.set()
                            self.assertTrue(release.wait(3))
                            now[0]=1046
                            return super().__exit__(*exc)
                    def connect(*args,**kwargs):return real_connect(*args,factory=SlowCommit,**kwargs)
                    def wait(done,timeout):
                        self.assertTrue(entered.wait(2))
                        workers.extend(case.actions.workers)
                        self.assertEqual(timeout,budget)
                        now[0]=1000+timeout
                        return False
                    with patch.object(accion.sqlite3,'connect',side_effect=connect),patch.object(case.actions,'wait',side_effect=wait):
                        code,answer=case.handler(request(host=host))
                        elapsed=now[0]-1000
                        self.assertEqual((code,answer['status']),(200,'unknown'))
                        self.assertEqual({k:answer[k] for k in ('action_id','host','launcher','ref')},
                                         {k:request(host=host)[k] for k in ('action_id','host','launcher','ref')})
                        self.assertLess(elapsed,budget+1)
                        self.assertEqual(case.calls,[])
                        release.set()
                        for worker in workers:worker.join(2);self.assertFalse(worker.is_alive())
                    self.assertEqual(case.calls,[])
                    with case.actions.connect() as db:
                        stored=json.loads(db.execute('SELECT response FROM actions').fetchone()[0])
                    self.assertEqual(stored['status'],'unknown')
                    self.assertEqual(case.actions.post(request(host=host))[1],stored)
                    print(f'R1-A-1 {host}: slow_journal_commit=46.0; response_at={elapsed:.1f}; UI_budget={budget+1}; HTTP={code}; status={answer["status"]}; executor_calls={len(case.calls)}; workers_alive=False')
                finally:
                    release.set()
                    for worker in workers:worker.join(3)
                    case.doCleanups()

    def test_get_slow_journal_and_post_slow_result_or_transport_return_unknown(self):
        for host,budget in (('desk',44),('mini',14)):
            for phase in ('get','save','transport'):
                with self.subTest(host=host,phase=phase):
                    case=T.AuthorizationTests(methodName='test_full_http_roundtrip_and_no_cors');case.setUp()
                    release,entered=threading.Event(),threading.Event();workers=[]
                    try:
                        now=[1000.0];case.actions.timer=lambda:now[0]
                        case.actions.hosts={h['id']:h for h in serve.load_hosts(Path(__file__).resolve().parents[1]/'hosts.json')}
                        case.data['hosts'][0]['id']=host
                        body=request(host=host)
                        if phase=='get':
                            case.response='unknown';case.actions.post(body)
                            case.actions=accion.Actions(list(case.actions.hosts.values()),case.root,lambda:case.data,
                                executor=case.fake_executor,clock=lambda:case.now,timer=lambda:now[0])
                        original=case.actions.save
                        def slow(*args,**kwargs):
                            if phase=='transport':case.calls.append(args[2])
                            entered.set();self.assertTrue(release.wait(3));now[0]=1046
                            if phase=='save':return original(*args,**kwargs)
                            if phase=='transport':return receipt(args[2])
                            return real_connect(*args,**kwargs)
                        real_connect=sqlite3.connect
                        target=patch.object(accion.sqlite3,'connect',side_effect=slow) if phase=='get' else patch.object(case.actions,'save' if phase=='save' else 'executor',side_effect=slow)
                        def wait(done,timeout):
                            self.assertTrue(entered.wait(2));workers.extend(case.actions.workers)
                            now[0]+=timeout;return False
                        with target,patch.object(case.actions,'wait',side_effect=wait):
                            code,answer=case.handler(body,method='GET' if phase=='get' else 'POST',path='/api/accion/'+body['action_id'] if phase=='get' else '/api/accion')
                            self.assertEqual((code,answer['status']),(200,'unknown'))
                            self.assertEqual(answer['host'],host)
                            self.assertLessEqual(now[0]-1000,budget)
                            release.set()
                            for worker in workers:worker.join(2);self.assertFalse(worker.is_alive())
                        self.assertFalse(case.actions.active(body['action_id']))
                        case.response='cancelled';case.now+=2
                        self.assertEqual(case.actions.post(body)[1]['action_id'],body['action_id'])
                        self.assertEqual(case.actions.get(body['action_id'])[1]['status'],'cancelled')
                        self.assertEqual(sum(w['op']=='cancel' for w in case.calls),1)
                    finally:
                        release.set()
                        for worker in workers:worker.join(3)
                        case.doCleanups()


class AdminAndUIReviewTests(unittest.TestCase):
    def test_existing_journal_migrates_without_changing_old_receipt_or_identity(self):
        with tempfile.TemporaryDirectory(prefix='rack-r1-migrate-',dir='/tmp') as folder:
            body=request(host='desk')
            answer=accion.result(body,'unknown')
            with sqlite3.connect(str(Path(folder)/'actions.sqlite3')) as db:
                db.execute('CREATE TABLE actions (action_id TEXT PRIMARY KEY COLLATE BINARY, digest TEXT NOT NULL, '
                           'request TEXT NOT NULL,response TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL)')
                db.execute('INSERT INTO actions VALUES (?,?,?,?,?,?)',
                           (body['action_id'],'old-digest',json.dumps(body),json.dumps(answer),1000,1000))
            actions=accion.Actions([],folder,lambda:None)
            self.assertEqual(actions.identities[body['action_id']],body)
            with actions.connect() as db:
                row=db.execute('SELECT digest,request,response,created,updated,last_status FROM actions').fetchone()
            self.assertEqual(row,('old-digest',json.dumps(body),json.dumps(answer),1000,1000,None))

    def test_cli_closes_only_open_ids_without_transport(self):
        with tempfile.TemporaryDirectory(prefix='rack-r1-cli-',dir='/tmp') as folder:
            data=payload()
            actions=accion.Actions([],folder,lambda:data,executor=lambda *a,**kw:None,clock=lambda:1000)
            # Populate the same durable intents as the server, using a local fake.
            actions.hosts={'mini':T.HOSTS[0]}
            actions.executor=lambda h,l,w,timeout=None:receipt(w,'unknown')
            body=request()
            actions.post(body)
            command=['/usr/bin/python3','-I',str(Path(accion.__file__)), '--state-dir',folder,'close-receipt']
            def run(action_id):return subprocess.run(command+[action_id],capture_output=True,text=True,timeout=5)
            first=run(body['action_id'])
            self.assertEqual(first.returncode,0,first.stderr)
            closed=json.loads(first.stdout)
            self.assertEqual(closed['error']['code'],'manually_closed')
            again=run(body['action_id'])
            self.assertEqual(again.returncode,0,again.stderr)
            self.assertEqual(json.loads(again.stdout),closed)
            self.assertEqual(actions.get(body['action_id'])[1],closed)
            self.assertEqual(run('missing-000').returncode,2)
            actions.executor=lambda h,l,w,timeout=None:receipt(w)
            actions.post(request(action_id='terminal-0002'))
            self.assertEqual(run('terminal-0002').returncode,2)
            self.assertEqual(run('../invalid').returncode,2)

    def test_ui_manual_close_shows_server_message_and_stops_polling_after_reload(self):
        node=shutil.which('node')
        self.assertIsNotNone(node,'Node is required for the UI regression')
        base=Path(accion.__file__).parent
        data=payload();data['hosts'][0]['id']='desk'
        body=request(host='desk')
        with tempfile.TemporaryDirectory(prefix='rack-r1-ui-',dir='/tmp') as folder:
            actions=accion.Actions(serve.load_hosts(base/'hosts.json'),folder,lambda:data,
                executor=lambda h,l,w,timeout=None:receipt(w,'unknown'),clock=lambda:1000)
            actions.post(body)
            answer=actions.close_receipt(body['action_id'])
            self.assertEqual(actions.get(body['action_id'])[1],answer)
        harness=(base/'tests/ui-check.js').read_text().split('(async()=>{',1)[0]
        probe=r"""
(async()=>{
 const run=s=>vm.runInContext(s,context);
 run(`globalThis.found={h:S.hosts[0],t:S.hosts[0].tasks[0]};
      globalThis.action={request:{...actionRequest(found,'cancel'),action_id:'action-0001'},task:{...found.t},status:'unknown'};
      globalThis.key=actionKey(found.h,found.t);PENDING.set(key,action);acceptAction(key,action,{status:'unknown'})`);
 let polls=0;
 context.fetch=async url=>{
   if(url==='/api/rack.json')return {ok:true,json:async()=>input.fixture};
   polls++;return {ok:true,json:async()=>input.answer};
 };
 await run('pollAction(key,action)');
 assert.equal(run('PENDING.size'),0);
 assert.match(elements.toast.innerHTML,/Recibo cerrado a mano/);
 assert.equal(JSON.parse(storage['rack-actions']).length,0);
 const pending=timers.filter(t=>t.ms===2000);
 for(const timer of pending)await timer.fn();
 await run('pollAction(key,action)');
 assert.equal(polls,1,'manual closure kept polling');
 const reload={...context};vm.createContext(reload);vm.runInContext(input.js,reload);
 assert.equal(vm.runInContext('PENDING.size',reload),0);
 console.log('manual close UI: message shown; pending=0; stale timers and reload issue no more GET');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
        result=subprocess.run([node,'-e',harness+probe],input=json.dumps(dict(
            js=(base/'index.html').read_text().split('<script>',1)[1].split('</script>',1)[0],fixture=data,answer=answer)),
            capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        print(result.stdout.strip())

    def test_actual_http_wait_times_out_blocked_io_without_virtual_waiter(self):
        # Shorten only the budget, not Event.wait: exercise the real HTTP boundary.
        case=T.AuthorizationTests(methodName='test_full_http_roundtrip_and_no_cors');case.setUp()
        release,entered=threading.Event(),threading.Event();workers=[]
        try:
            original=sqlite3.connect
            def slow_connect(*args,**kwargs):
                entered.set();self.assertTrue(release.wait(3));return original(*args,**kwargs)
            with patch.object(accion.sqlite3,'connect',side_effect=slow_connect),patch.object(accion,'request_budget',return_value=.08),patch.object(accion,'RECEIPT_RESERVE',0):
                started=time.monotonic()
                code,answer=case.handler()
                elapsed=time.monotonic()-started
                self.assertTrue(entered.is_set())
                self.assertEqual((code,answer['status']),(200,'unknown'))
                self.assertGreaterEqual(elapsed,.07)
                self.assertLess(elapsed,.5)
                self.assertEqual(case.calls,[])
                workers=list(case.actions.workers)
                release.set()
                for worker in workers:worker.join(2);self.assertFalse(worker.is_alive())
            self.assertEqual(case.calls,[])
        finally:
            release.set()
            for worker in workers:worker.join(3)
            case.doCleanups()
