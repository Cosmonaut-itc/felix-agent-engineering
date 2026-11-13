"""C4 graft: bounded admission and concurrent receipts, virtual time and /tmp only."""
import safety
import copy
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

import test_actions as T
from test_actions import accion, serve, request, receipt, payload
from test_rack import BASE


class DeadlineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rack-c4-deadline-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1000.0
        self.hosts = serve.load_hosts(BASE / 'hosts.json')
        self.calls = []
        self.data = payload()
        mini = self.data['hosts'][0]
        desk = copy.deepcopy(mini); desk['id'] = 'desk'
        other = copy.deepcopy(desk['snapshot']['tasks'][0])
        other['id'] = 78
        other['control'].update(ref='20261112-184714-abcdef', expected=dict(task_id=78, state='Queued'))
        desk['snapshot']['tasks'].append(other)
        self.data['hosts'].append(desk)
        self.actions = accion.Actions(self.hosts, self.root, self.read, self.execute,
                                     clock=lambda:1000, timer=lambda:self.now)
        self.release = threading.Event()
        self.threads = []
        self.addCleanup(self.close_threads)
        guard = patch.object(accion.subprocess, 'Popen', side_effect=AssertionError('No processes'))
        guard.start(); self.addCleanup(guard.stop)

    def read(self):
        return self.data

    def execute(self, host, launcher, wire, timeout=None):
        self.calls.append((host['id'], copy.deepcopy(wire), timeout, self.now))
        if wire['op'] == 'capabilities':
            return dict(schema='rack-cancel-result@1', ref=wire['ref'],
                        capabilities=dict(queued=True, running=True, reason=None))
        return receipt(wire)

    def close_threads(self):
        self.release.set()
        for thread, _, _ in self.threads:
            thread.join(3)
            self.assertFalse(thread.is_alive(), 'test left a worker alive')

    def spawn(self, function):
        answers, errors = [], []
        def run():
            try: answers.append(function())
            except BaseException as error: errors.append(error)
        thread = threading.Thread(target=run)
        self.threads.append((thread, answers, errors))
        thread.start()
        return thread, answers, errors

    def join(self, worker):
        thread, answers, errors = worker
        thread.join(2)
        self.assertFalse(thread.is_alive(), 'request queued behind a transport')
        self.assertEqual(errors, [])
        return answers[0]

    def stored(self, body):
        with self.actions.connect() as db:
            return json.loads(db.execute('SELECT response FROM actions WHERE action_id=?',
                                         (body['action_id'],)).fetchone()[0])

    def other(self, host='desk', **changes):
        return request(host=host, action_id='parallel-0002', **changes)

    def test_judge_reproduction_bounded_lock_while_desk_inflight(self):
        entered = threading.Event()
        def block(host, launcher, wire, timeout=None):
            self.execute(host, launcher, wire, timeout)
            entered.set()
            self.assertTrue(self.release.wait(3))
            return receipt(wire)
        self.actions.executor = block
        first = self.spawn(lambda:self.actions.post(request(host='desk')))
        self.assertTrue(entered.wait(2))
        second = self.other(ref='20261112-184714-abcdef', expected=dict(task_id=78, state='Queued'))
        real = threading.RLock()
        waits = []
        class ObservedLock:
            def acquire(lock, timeout):
                waits.append(timeout)
                # Judge's 46 s holder: unlike the old unbounded __enter__, leave
                # when admission expires. Advance virtual time only by that wait.
                self.now += timeout
                return real.acquire(timeout=0)
            def release(lock): real.release()
        self.actions.lock = ObservedLock()
        with real:
            # Hold the real RLock from another thread, because an RLock reenters.
            queued = self.spawn(lambda:self.actions.post(second))
            code, answer = self.join(queued)
        elapsed = self.now - 1000
        self.assertEqual(code, 503)
        self.assertEqual(answer['status'], 'unknown')
        self.assertEqual(answer['error']['code'], 'deadline')
        self.assertIn('No se lanzó', answer['error']['message'])
        self.assertLessEqual(elapsed, 44)
        self.assertLessEqual(waits[0], .5)
        self.assertEqual(len(self.calls), 1)
        self.assertNotEqual(self.calls[0][1]['ref'], second['ref'])
        self.release.set(); self.join(first)
        print('judge: desk transport in flight; lock holder=46 virtual_s; '
              'second executor_started_at=never; response_at='+str(elapsed)+
              '; UI_budget=45; status='+answer['status']+'; workers_alive=False')

    def test_journal_time_is_subtracted_and_no_launch_without_minimum(self):
        original = self.actions.connect
        @contextmanager
        def delayed(deadline=None):
            with original(deadline) as db:
                yield db
            # Simulate a slow durable intent commit; still within the UI budget.
            self.now = 1042.5
        with patch.object(self.actions, 'connect', delayed):
            code, answer = self.actions.post(request(host='desk'))
        self.assertEqual((code, answer['status'], answer['error']['code']), (200, 'rejected', 'deadline'))
        self.assertEqual(self.calls, [])
        self.assertLess(self.now - 1000, 45)
        self.assertEqual(self.stored(request()), answer)
        self.now = 1100
        self.assertEqual(self.actions.post(request(host='desk'))[1], answer)
        self.assertEqual(self.actions.post(request(host='desk', expected=dict(task_id=77,state='Running')))[0],409)

    def test_remaining_budget_reaches_executor_for_each_host_and_operation(self):
        for host, limit, total in (('desk',40,44), ('mini',10,14)):
            for op in ('cancel', 'capabilities', 'status'):
                with self.subTest(host=host, op=op):
                    body = request(host=host, action_id='budget-'+host+'-'+op)
                    self.now = 1000
                    if op == 'status':
                        self.actions.post(body)
                        self.actions.save(body, accion.result(body, 'unknown'))
                    self.now = 1010 if host == 'mini' else 1040
                    self.calls.clear()
                    if op == 'status': answer = self.actions.get(body['action_id'], started=1000)[1]
                    else:
                        if op == 'capabilities': body=dict(body,op=op);del body['action_id']
                        answer = self.actions.post(body, started=1000)[1]
                    self.assertEqual(answer['status'], 'cancelled' if op != 'capabilities' else 'capabilities')
                    self.assertEqual(len(self.calls), 1)
                    self.assertLessEqual(self.calls[0][2], min(limit,total-(self.now-1000)-1))
                    self.assertGreaterEqual(self.calls[0][2], 1)

    def test_late_effect_is_unknown_and_same_receipt_reconciles(self):
        def late(host, launcher, wire, timeout=None):
            self.calls.append(wire)
            self.now += timeout + .1
            return receipt(wire)
        self.actions.executor = late
        body = request(host='mini')
        # Only 3 s transport budget remains; simulate an executor returning late.
        self.now = 1010
        self.assertEqual(self.actions.post(body, started=1000)[1]['status'], 'unknown')
        self.assertEqual(self.stored(body)['status'], 'unknown')
        self.assertEqual(self.actions.post(body)[1]['status'], 'unknown')
        self.assertEqual(len(self.calls), 1)
        self.actions.executor = self.execute
        self.assertEqual(self.actions.get(body['action_id'])[1]['status'], 'cancelled')
        self.assertEqual(self.calls[-1][1]['op'], 'status')
        self.assertEqual(self.calls[-1][1]['action_id'], body['action_id'])

    def test_concurrent_post_and_get_desk_desk_and_desk_mini(self):
        for first_op in ('POST', 'GET'):
            for second_op in ('POST', 'GET'):
                for host in ('desk', 'mini'):
                    with self.subTest(first=first_op, second=second_op, host=host):
                        self.release.clear()
                        with self.actions.connect() as db: db.execute('DELETE FROM actions')
                        first_body=request(host='desk')
                        second_body=self.other(host=host, ref='20261112-184714-abcdef' if host=='desk' else request()['ref'],
                            expected=dict(task_id=78 if host=='desk' else 77,state='Queued'))
                        for op, body in ((first_op,first_body), (second_op,second_body)):
                            if op == 'GET':
                                self.actions.post(body)
                                self.actions.save(body,accion.result(body,'unknown'))
                        self.calls.clear()
                        entered = threading.Event()
                        def block(h,l,w,timeout=None):
                            self.execute(h,l,w,timeout)
                            if w['action_id']==first_body['action_id']:
                                entered.set(); self.assertTrue(self.release.wait(3))
                            return receipt(w)
                        self.actions.executor=block
                        first=self.spawn(lambda:self.actions.post(first_body) if first_op=='POST' else self.actions.get(first_body['action_id']))
                        self.assertTrue(entered.wait(2))
                        second=self.spawn(lambda:self.actions.post(second_body) if second_op=='POST' else self.actions.get(second_body['action_id']))
                        try:
                            self.assertEqual(self.join(second)[1]['status'],'cancelled')
                            self.assertFalse(self.release.is_set())
                        finally:
                            self.release.set()
                            self.join(first)
                            self.actions.executor=self.execute
                        self.assertEqual(len(self.calls),2)

    def test_inflight_cancel_duplicate_conflict_pending_and_get_do_not_act(self):
        entered = threading.Event()
        def block(h,l,w,timeout=None):
            self.execute(h,l,w,timeout); entered.set(); self.assertTrue(self.release.wait(3))
            return receipt(w)
        self.actions.executor=block
        body=request(host='desk')
        first=self.spawn(lambda:self.actions.post(body))
        self.assertTrue(entered.wait(2))
        stored=self.stored(body)
        self.assertEqual(stored['status'],'unknown')
        for call in (lambda:self.actions.get(body['action_id']),lambda:self.actions.post(body)):
            self.assertEqual(self.join(self.spawn(call)),(200,stored))
        changed=dict(body,expected=dict(task_id=77,state='Running'))
        self.assertEqual(self.join(self.spawn(lambda:self.actions.post(changed)))[0],409)
        second=dict(body,action_id='pending-0002')
        rejection=self.join(self.spawn(lambda:self.actions.post(second)))
        self.assertEqual(rejection[0],409)
        self.assertIn(body['action_id'],rejection[1]['error']['message'])
        cap=dict(body,op='capabilities');del cap['action_id']
        self.assertIn(body['action_id'],self.join(self.spawn(lambda:self.actions.post(cap)))[1]['error']['message'])
        self.assertEqual(len(self.calls),1)
        self.release.set();self.assertEqual(self.join(first)[1]['status'],'cancelled')
        self.assertEqual(self.actions.get(body['action_id'])[1]['status'],'cancelled')
        self.assertEqual(self.stored(body)['status'],'cancelled')
        self.assertEqual(len(self.calls),1)

    def test_inflight_status_cannot_overwrite_later_result_or_repeat_cancel(self):
        body=request(host='desk')
        self.actions.post(body)
        self.actions.save(body,accion.result(body,'unknown'))
        self.calls.clear()
        entered=threading.Event()
        def status(h,l,w,timeout=None):
            self.execute(h,l,w,timeout); entered.set();self.assertTrue(self.release.wait(3))
            return receipt(w,'cancelling')
        self.actions.executor=status
        first=self.spawn(lambda:self.actions.get(body['action_id']))
        self.assertTrue(entered.wait(2))
        for function in (lambda:self.actions.get(body['action_id']),lambda:self.actions.post(body)):
            self.assertEqual(self.join(self.spawn(function))[1]['status'],'unknown')
        self.assertEqual(len(self.calls),1)
        self.release.set();self.assertEqual(self.join(first)[1]['status'],'cancelling')
        self.actions.executor=self.execute
        self.assertEqual(self.actions.get(body['action_id'])[1]['status'],'cancelled')
        self.assertEqual(self.actions.post(body)[1]['status'],'cancelled')
        self.assertEqual(self.stored(body)['status'],'cancelled')
        self.assertEqual([c[1]['op'] for c in self.calls],['status','status'])

    def test_failed_completion_releases_flight_and_keeps_durable_unknown(self):
        with patch.object(self.actions,'save',side_effect=sqlite3.OperationalError('fake crash')):
            with self.assertRaises(sqlite3.OperationalError):self.actions.post(request())
        self.assertFalse(self.actions.active(request()['action_id']))
        self.assertEqual(self.stored(request())['status'],'unknown')
        self.assertEqual(self.actions.get(request()['action_id'])[1]['status'],'cancelled')
        self.assertEqual([c[1]['op'] for c in self.calls],['cancel','status'])


class HTTPDeadlineTests(unittest.TestCase):
    def test_whois_and_body_time_count_from_http_entry(self):
        case=T.AuthorizationTests(methodName='test_full_http_roundtrip_and_no_cors');case.setUp()
        self.addCleanup(case.doCleanups)
        now=[0.0];case.actions.timer=lambda:now[0]
        budgets=[]
        def execute(h,l,w,timeout=None):budgets.append(timeout);now[0]+=timeout;return receipt(w)
        case.actions.executor=execute
        def owner(*a,**kw):now[0]+=3;return True
        case.owner.allows=owner
        read1=io.BytesIO.read1
        def slow_read(stream,n=-1):now[0]+=5;return read1(stream,n)
        # BytesIO is immutable: install the body through the handler dispatch hook.
        original=serve.Handler.action
        class SlowBody(io.BytesIO):
            def read1(self,n=-1):return slow_read(self,n)
        def action(handler):
            handler.rfile=SlowBody(handler.rfile.read())
            return original(handler)
        with patch.object(serve.Handler,'action',action):
            code,answer=case.handler()
        self.assertEqual((code,answer['status']),(200,'cancelled'))
        self.assertLessEqual(budgets[0],5)
        self.assertLessEqual(now[0],14)

    def test_whois_does_not_serialize_other_requests(self):
        owner=serve.OwnerCheck('owner','/fake/tailscale')
        entered,release=threading.Event(),threading.Event()
        answers=[];processes=[]
        def runner(argv,**kw):
            process=Mock(returncode=0)
            def communicate(timeout):
                if argv[-1]=='first':entered.set();self.assertTrue(release.wait(3))
                return json.dumps(dict(UserProfile=dict(LoginName='owner'))),None
            process.communicate.side_effect=communicate
            processes.append(process);return process
        first=threading.Thread(target=lambda:answers.append(owner.allows('first',fresh=True)))
        second=threading.Thread(target=lambda:answers.append(owner.allows('second',fresh=True)))
        with patch.object(serve.subprocess,'Popen',side_effect=runner):
            first.start();self.assertTrue(entered.wait(2));second.start()
            try:
                second.join(1)
                self.assertFalse(second.is_alive(),'whois queue consumed the HTTP budget')
            finally:
                release.set();first.join(3);second.join(3)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(answers,[True,True])
