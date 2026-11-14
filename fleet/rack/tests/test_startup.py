"""Restart reasons, no launcher receipts, private SQLite and synthetic UI."""
import safety
import copy
import json
import sqlite3
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch
import test_rack as rack_tests
from test_rack import snapshot, serve, task as row_task
import test_desk_slots as desk_tests
from test_slots import BASE

MESSAGE='Interrumpida al reiniciar Pueue; relanzar manualmente'
STAMP='2026-11-14T13:00:00+00:00'


class StartupSnapshotTests(unittest.TestCase):
    setUp = rack_tests.SnapshotTests.setUp
    fake_pueue = rack_tests.SnapshotTests.fake_pueue
    # Reuse fixture helpers, not inherited tests.
    def test_interruption_requires_durable_exact_identity_and_killed_and_preserves_cancel(self):
        authority=self.root/'.local/state/pao/pueue-desk/control.sqlite3';authority.parent.mkdir(parents=True)
        db=sqlite3.connect(str(authority));db.execute('CREATE TABLE policy(id INTEGER PRIMARY KEY,body TEXT)')
        db.execute('INSERT INTO policy VALUES(1,?)',(json.dumps(dict(startup=dict(interruptions={str(i):dict(created_at=STAMP,message=MESSAGE) for i in (1,2,3)}))),));db.commit();db.close()
        def task(ident,created_at=STAMP,result='Killed'):
            return dict(id=ident,group='jale',created_at=created_at,path='/synthetic',envs={},command='synthetic',original_command='synthetic',priority=0,
                        status={'Done':dict(enqueued_at=STAMP,start=STAMP,end=STAMP,result=result)})
        tasks={str(i):task(i) for i in (1,4)};tasks['2']=task(2,created_at='2026-11-14T12:00:00Z');tasks['3']=task(3,result='Success')
        with patch.object(snapshot,'linux_capacity',return_value=None),patch.object(snapshot,'desk_guard',return_value=None):
            data=snapshot.collect('linux','desk',pueue=self.fake_pueue(tasks),home=self.root,now=1794664800)
        self.assertEqual([t['id'] for t in data['tasks'] if t.get('interruption_reason')],[1])
        self.assertEqual(data['tasks'][0]['interruption_reason'],MESSAGE)
        self.assertFalse(any('origin' in (t.get('control') or {}) for t in data['tasks']))
        self.assertNotIn('envs',json.dumps(data));self.assertNotIn('original_command',json.dumps(data))


class StartupPublicationTests(unittest.TestCase):
    def test_publish_retains_only_attributed_interruption_and_snapshot_rejects_false_origin(self):
        host=dict(id='desk',name='Desk',transport='ssh')
        cache=serve.Rack.__new__(serve.Rack)
        import threading
        cache.hosts=[host];cache.good={'desk':dict(schema='rack-host@1',host='desk',taken=1000,groups=[],tasks=[
            row_task(id=1,group='jale',state='Done',result='Killed',interruption_reason=MESSAGE),
            row_task(id=2,group='jale',state='Done',result='Killed'),row_task(id=3,group='jale',state='Queued')])}
        cache.errors={};cache.lock=threading.Lock();cache.tickets={}
        from unittest.mock import Mock
        cache.history=Mock();cache.history.estimate.return_value=None
        cache.publish(1000)
        self.assertEqual([t['id'] for t in cache.payload['hosts'][0]['snapshot']['tasks']],[1,3])
        good=serve.validate_snapshot(cache.good['desk'],'desk')
        self.assertEqual(good['tasks'][0]['interruption_reason'],MESSAGE)
        for field,value in (('result','Success'),('interruption_reason','Cancelada desde el panel'),('state','Queued')):
            changed=copy.deepcopy(cache.good['desk']);changed['tasks'][0][field]=value
            with self.assertRaises(ValueError):serve.validate_snapshot(changed,'desk')


class StartupUITests(unittest.TestCase):
    def fixture(self):
        test=desk_tests.DeskChannelTests();test.setUp()
        try:
            c=test.engine.view();c.update(pause_reasons={'jale':['startup','ambiguous']},startup_holds={'jale':'pausa de origen desconocido <script>'})
            safe=serve.accion.host_channel.control(c,'desk')
            return dict(now=test.now,tickets={},hosts=[dict(id='desk',name='Felix DESKTOP · WSL',fresh=True,age=0,estimates={},actions=dict(reason=None),
                snapshot=dict(control=safe,groups=[dict(name=g,slots=n,status='Paused') for g,n in test.engine.view()['slots'].items()]+[dict(name='default',slots=1,status='Paused')],
                    tasks=[dict(row_task(id=1,group='jale',state='Done',result='Killed'),name='Corrida sintética',interruption_reason=MESSAGE)],
                    cpu=dict(cores=12,load=[1,1,1]),mem=dict(total=32*1024**3,available=24*1024**3,swap_total=0,swap_used=0)))])
        finally:test.doCleanups()

    def test_production_ui_interruption_hold_resolution_escape_and_normal_host(self):
        source=(BASE/'index.html').read_text().split('<script>',1)[1].split('</script>',1)[0]
        result=subprocess.run(['node',str(BASE/'tests/slots-ui-check.js')],input=json.dumps(dict(js=source,fixture=self.fixture(),scenario='startup')),
                              text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('UI startup OK',result.stdout)

    def test_sanitizer_retains_only_valid_hold_strings(self):
        c=self.fixture()['hosts'][0]['snapshot']['control']
        self.assertIn('jale',c['startup_holds'])
        self.assertEqual(c['startup_holds']['jale'],'pausa de origen desconocido <script>')
        for value in ([],{'jale':None},{'jale':'x'*301}):
            with self.assertRaises(ValueError):serve.accion.host_channel.control(dict(c,startup_holds=value),'desk')


if __name__=='__main__':unittest.main()


class StartupPipelineTests(unittest.TestCase):
    def fixture(self, normal=False):
        # Production planner/view, collect, validation and publication. Every host
        # boundary is injected; no fixture bypasses the terminal publication filter.
        import threading
        from unittest.mock import Mock
        test=desk_tests.DeskChannelTests();test.setUp()
        try:
            c=desk_tests.control
            home=test.root
            controller=home/'.local/lib/pao-pueue-desk/control.py'
            controller.parent.mkdir(parents=True);controller.write_text('# injected read only')
            with test.engine.connect() as db:state=test.engine.state(db)
            groups=copy.deepcopy(test.p.groups)
            groups.update(default=dict(status='Running',parallel_tasks=1))
            if not normal:groups['suites']=dict(status='Paused',parallel_tasks=3)
            raw=dict(id=1,group='default',created_at=STAMP,path=str(home/'synthetic'),envs={},command='synthetic',original_command='synthetic',priority=0,
                     dependencies=[],label=None,status={'Running':dict(enqueued_at=STAMP,start=STAMP)})
            queue=dict(tasks={} if normal else {'1':raw},groups=groups)
            if not normal:
                state,queue=c.startup_plan(queue,state,test.now,home,c.GROUP_LIMITS)
                state['startup']['phase']='ready'
                state['startup_version']=1
                # Preserve a specific safe message, exercising HTML escaping.
                state['startup']['holds']['suites']='pausa de origen desconocido <script>'
            test.p.groups=copy.deepcopy(queue['groups'])
            with test.engine.connect(write=True) as db:test.engine.store(db,state)
            authority=home/'.local/state/pao/pueue-desk/control.sqlite3'
            authority.parent.mkdir(parents=True)
            with test.engine.connect() as db:
                with sqlite3.connect(str(authority)) as target:db.backup(target)
            view=test.engine.view()
            with patch.object(snapshot,'command',return_value=json.dumps(queue)),patch.object(snapshot,'read_control',return_value=view), \
                 patch.object(snapshot,'linux_capacity',return_value=(dict(cores=12,load=[1,1,1]),dict(total=32*1024**3,available=24*1024**3,swap_total=0,swap_used=0))), \
                 patch.object(snapshot,'desk_guard',return_value=None),patch.object(snapshot,'disk',return_value=None):
                collected=snapshot.collect('linux','desk',pueue='/synthetic/pueue',home=home,now=test.now)
            try:collected=serve.validate_snapshot(collected,'desk')
            except ValueError as error:self.fail('Production pipeline produced an invalid snapshot: '+str(error))
            cache=serve.Rack.__new__(serve.Rack)
            cache.hosts=[dict(id='desk',name='Felix DESKTOP · WSL',transport='ssh')]
            cache.good={'desk':collected};cache.errors={};cache.lock=threading.Lock();cache.tickets={}
            cache.history=Mock();cache.history.estimate.return_value=None
            cache.publish(test.now)
            return cache.payload
        finally:test.doCleanups()

    def render(self, scenario):
        source=(BASE/'index.html').read_text().split('<script>',1)[1].split('</script>',1)[0]
        result=subprocess.run(['node',str(BASE/'tests/slots-ui-check.js')],input=json.dumps(dict(js=source,fixture=self.fixture(),scenario=scenario)),
                              text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_a_w4_snapshot_validate_publish_render_default_interrupted_and_empty_hidden(self):
        self.assertEqual([t['id'] for t in self.fixture()['hosts'][0]['snapshot']['tasks']],[1])
        self.render('startup-default')

    def test_a_w5_pipeline_six_waits_one_hold_and_no_redundant_ssh(self):
        self.render('startup-waits')

    def test_wait_codes_are_closed_and_disjoint_from_holds(self):
        view=self.fixture()['hosts'][0]['snapshot']['control']
        self.assertEqual(view.get('startup_waiting'),{g:'cuotas_sensores' for g in desk_tests.control.GROUP_LIMITS})
        for value in ([],{'jale':'<script>'},{'suites':'cuotas_sensores'}):
            with self.assertRaises(ValueError):serve.accion.host_channel.control(dict(view,startup_waiting=value),'desk')

    def test_attributed_interruption_survives_ordinary_terminal_age_cutoff(self):
        test=StartupSnapshotTests();test.setUp()
        try:
            authority=test.root/'.local/state/pao/pueue-desk/control.sqlite3';authority.parent.mkdir(parents=True)
            with sqlite3.connect(str(authority)) as db:
                db.execute('CREATE TABLE policy(id INTEGER PRIMARY KEY,body TEXT)')
                db.execute('INSERT INTO policy VALUES(1,?)',(json.dumps(dict(startup=dict(interruptions={'1':dict(created_at=STAMP,message=MESSAGE)}))),))
            raw=dict(id=1,group='default',created_at=STAMP,path='/synthetic',envs={},command='synthetic',original_command='synthetic',priority=0,
                     status={'Done':dict(enqueued_at=STAMP,start=STAMP,end=STAMP,result='Killed')})
            with patch.object(snapshot,'linux_capacity',return_value=None),patch.object(snapshot,'desk_guard',return_value=None):
                data=snapshot.collect('linux','desk',pueue=test.fake_pueue({'1':raw}),home=test.root,now=1794664800+3*86400)
            self.assertEqual([t['id'] for t in data['tasks'] if t.get('interruption_reason')],[1])
        finally:test.doCleanups()
