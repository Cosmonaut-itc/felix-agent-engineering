"""R1 regressions, synthetic controllers/DOM only; IDs map to the review report."""
import safety
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import test_actions as T
from test_actions import HOSTS, REF, accion, payload, request, snapshot


class JournalRegressions(unittest.TestCase):
    setUp = T.ActionsTests.setUp
    reopen = T.ActionsTests.reopen
    fake_executor = T.ActionsTests.fake_executor

    def test_b2_open_action_blocks_other_id_and_capabilities_after_restart(self):
        for status in accion.CANCEL_OPEN:
            with self.subTest(status=status):
                self.response = status
                self.actions.post(request())
                self.actions = self.reopen()
                self.assertEqual(self.actions.get('action-0001')[1]['status'], status)
                before = len(self.calls)
                code, answer = self.actions.post(request(action_id='second-0002'))
                self.assertEqual(code, 409)
                self.assertIn('action-0001', answer['error']['message'])
                cap = request(op='capabilities'); del cap['action_id']
                self.assertIn('pendiente', self.actions.post(cap)[1]['error']['message'])
                self.assertEqual(len(self.calls), before)
                # Same new ID remains bound to its rejected body.
                self.assertEqual(self.actions.post(request(action_id='second-0002', ref='20991231-235959-ffffff'))[0],409)
                self.response='cancelled'
                self.actions.get('action-0001')
                self.assertEqual(self.actions.post(request(action_id='third-0003'))[1]['status'],'cancelled')
                with self.actions.connect() as db: db.execute('DELETE FROM actions')
                self.calls.clear()

    def test_b4_recognized_launchers_in_otros_and_pg5434_reach_capabilities(self):
        home=Path('/synthetic-home')
        for group in ('otros','pg5434'):
            raw=dict(id=77,group=group,path=str(home/'remote-run/runs'/REF))
            control=snapshot.task_control(raw,home,'Queued')
            self.assertIsNone(control['reason'])
            row=self.data['hosts'][0]['snapshot']['tasks'][0]
            row.update(group=group,control=control)
            cap=request(op='capabilities');del cap['action_id']
            self.assertTrue(self.actions.post(cap)[1]['capabilities']['queued'])
            self.assertEqual(self.actions.post(request(action_id='group-'+group+'-001'))[1]['status'],'cancelled')
            self.assertIsNotNone(snapshot.task_control(dict(raw,path='/unrecognized'),home,'Queued')['reason'])


class ExecutorRegressions(unittest.TestCase):
    def test_a2_deep_controller_response_is_http_unknown_and_reconciles(self):
        case=T.AuthorizationTests(methodName='test_full_http_roundtrip_and_no_cors')
        case.setUp()
        try:
            effect=case.root/'effect'
            T.TransportTests().controller(case.root,
                'import json,sys\nfrom pathlib import Path\nr=json.load(sys.stdin)\n'
                'if r["op"]=="cancel":\n Path('+repr(str(effect))+').write_text("effect")\n'
                ' sys.stdout.write("["*1100+"0"+"]"*1100)\n'
                'else:\n print(json.dumps(dict(schema="rack-cancel-result@1",ref=r["ref"],'
                'action_id=r["action_id"],status="cancelled",result=dict(exit=130),error=None)))\n')
            case.actions.executor=accion.Executor(home=case.root)
            code,answer=case.handler()
            self.assertTrue(effect.exists())
            self.assertEqual((code,answer['status']),(200,'unknown'))
            with case.actions.connect() as db:
                self.assertEqual(json.loads(db.execute('SELECT response FROM actions').fetchone()[0])['status'],'unknown')
            self.assertEqual(case.handler(method='GET',path='/api/accion/action-0001')[1]['status'],'cancelled')
        finally:case.doCleanups()
        self.assertFalse(case.root.exists())

    def test_a3_live_stdout_is_bounded_killed_reaped_and_unknown(self):
        with tempfile.TemporaryDirectory(prefix='rack-r1-overflow-',dir='/tmp') as folder:
            root=Path(folder)
            T.TransportTests().controller(root, 'import os,time\n'
                'for _ in range(256): os.write(1,b"x"*4096)\ntime.sleep(60)\n')
            processes=[]; captured=[]; real_read=os.read
            def runner(*args,**kwargs):
                process=subprocess.Popen(*args,**kwargs);processes.append(process);return process
            def read(fd,size):
                chunk=real_read(fd,size)
                if processes and fd==processes[0].stdout.fileno():captured.append(len(chunk))
                return chunk
            actions=accion.Actions(HOSTS,root,payload,
                executor=accion.Executor(home=root,runner=runner,timeout=2),clock=lambda:1000)
            with patch.object(accion.os,'read',side_effect=read):
                self.assertEqual(actions.post(request())[1]['status'],'unknown')
            self.assertGreater(sum(captured),16384)
            self.assertLessEqual(sum(captured),16385)
            process=processes[0]
            self.assertEqual(process.returncode,-accion.signal.SIGKILL)
            self.assertTrue(process.stdout.closed and process.stdin.closed)
            with self.assertRaises(ChildProcessError):os.waitpid(process.pid,os.WNOHANG)
            with self.assertRaises(ProcessLookupError):os.kill(process.pid,0)
            print('stdout captured:',sum(captured),'bytes; own group killed; controller reaped; action unknown')


class UIRegressions(unittest.TestCase):
    def run_ui(self, scenario):
        node=shutil.which('node')
        if not node:self.skipTest('node unavailable')
        base=Path(__file__).resolve().parents[1]
        harness=(base/'tests/ui-check.js').read_text().split('(async()=>{',1)[0]
        probe=(base/'tests/r1-ui-check.js').read_text()
        data=dict(js=(base/'index.html').read_text().split('<script>',1)[1].split('</script>',1)[0],
                  fixture=payload(),scenario=scenario)
        answer=subprocess.run([node,'-e',harness+probe],input=json.dumps(data),text=True,capture_output=True,timeout=10)
        self.assertEqual(answer.returncode,0,answer.stderr)
        print(answer.stdout.strip())

    def test_a1_new_attempt_visible_old_cancelled_attempt_hidden_after_reload(self):self.run_ui('a1')
    def test_b1_pending_visible_for_done_and_stashed(self):self.run_ui('b1')
    def test_b5_missing_receipt_resends_same_body_once_without_notice_loop(self):self.run_ui('b5')
