"""Mini planner -> authority -> snapshot -> sanitizer -> publication -> real UI JS."""
import safety
import copy
import json
from pathlib import Path
import sqlite3
import subprocess
import threading
import unittest
from unittest.mock import Mock, patch
import test_slots as slots_tests
from test_slots import control, BASE
from test_rack import snapshot, serve
from test_startup import MESSAGE, STAMP


class MiniStartupTests(unittest.TestCase):
    def fixture(self, normal=False):
        test=slots_tests.ChannelTests();test.setUp()
        try:
            home=test.root
            controller=home/'.local/lib/pao-pueue-mini/control.py'
            controller.parent.mkdir(parents=True);controller.write_text('# synthetic controller')
            with test.engine.connect() as db:state=test.engine.state(db)
            groups=copy.deepcopy(test.p.groups)
            groups['suites']['status']='Paused'
            raw=dict(id=1,group='rapidas',created_at=STAMP,path='/synthetic',envs={},command='synthetic',original_command='synthetic',priority=0,
                     dependencies=[],label=None,status={'Running':dict(enqueued_at=STAMP,start=STAMP)})
            queue=dict(tasks={} if normal else {'1':raw},groups=groups)
            if not normal:
                state,queue=control.startup_plan(queue,state,test.engine.clock(),home,control.MINI_PROFILE)
                state['startup']['phase']='ready';state['startup_version']=1
                state['startup']['holds']['suites']='pausa manual <script>'
            test.p.groups=copy.deepcopy(queue['groups'])
            with test.engine.connect(write=True) as db:test.engine.store(db,state)
            authority=home/'.local/state/pao/pueue-mini/control.sqlite3'
            authority.parent.mkdir(parents=True)
            with test.engine.connect() as db:
                with sqlite3.connect(str(authority)) as target:db.backup(target)
            view=test.engine.view()
            with patch.object(snapshot,'command',return_value=json.dumps(queue)),patch.object(snapshot,'read_control',return_value=view), \
                 patch.object(snapshot,'mac_capacity',return_value=(dict(cores=10,load=[.2,.2,.2]),dict(total=32*1024**3,available=24*1024**3,swap_total=0,swap_used=0))), \
                 patch.object(snapshot,'mini_guard',return_value=None),patch.object(snapshot,'disk',return_value=None):
                collected=snapshot.collect('macos','mini',pueue='/synthetic/pueue',home=home,now=1794664800+3*86400)
            collected=serve.validate_snapshot(collected,'mini')
            cache=serve.Rack.__new__(serve.Rack)
            cache.hosts=[dict(id='mini',name='Mac Mini',transport='local')]
            cache.good={'mini':collected};cache.errors={};cache.lock=threading.Lock();cache.tickets={}
            cache.history=Mock();cache.history.estimate.return_value=None
            cache.publish(1794664800+3*86400)
            return cache.payload
        finally:test.doCleanups()

    def test_mini_snapshot_reads_own_authority_and_preserves_old_attributed_task(self):
        payload=self.fixture();snap=payload['hosts'][0]['snapshot']
        self.assertEqual([t['id'] for t in snap['tasks']],[1])
        self.assertEqual(snap['tasks'][0]['interruption_reason'],MESSAGE)
        self.assertNotIn('origin',snap['tasks'][0]['control'])
        self.assertIn('suites',snap['control']['startup_holds'])
        self.assertEqual(snap['control']['desired'],control.MINI_PROFILE)
        self.assertEqual(snap['control']['cap'],7)
        self.assertFalse(snap['control']['capabilities']['reorder'])
        self.assertNotIn('envs',json.dumps(payload));self.assertNotIn('original_command',json.dumps(payload))

    def test_mini_ui_interruption_hold_and_normal_unchanged(self):
        source=(BASE/'index.html').read_text().split('<script>',1)[1].split('</script>',1)[0]
        for normal in (False,True):
            result=subprocess.run(['node',str(BASE/'tests/slots-ui-check.js')],input=json.dumps(dict(js=source,fixture=self.fixture(normal),scenario='mini-normal' if normal else 'mini-startup')),
                                  text=True,capture_output=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn('UI Mini',result.stdout)


if __name__=='__main__':unittest.main()
