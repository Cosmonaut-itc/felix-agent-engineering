"""SEN-6417: current sensors, durable history, whitelist and approved merged cell."""
import safety
import copy
import inspect
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from test_rack import snapshot, serve, sample, BASE

NOW=1794672000
AT='2026-11-14T16:00:00+00:00'
TICK='1'*24

class SnapshotTrickleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir='/tmp');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.source=self.root/'.local/lib/pao-pueue-desk/control.py';self.source.parent.mkdir(parents=True)
        shutil.copy(BASE.parent/'pueue-desk/control.py',self.source)
        self.log=self.root/'.local/state/pao/pueue-desk/guardia.log';self.log.parent.mkdir(parents=True)
        self.log.write_text(json.dumps(dict(evento='decision',accion='mantener',utc=AT,tick_id=TICK))+'\n'+
            json.dumps(dict(evento='retenido',utc=AT,tick_id=TICK,bloqueos=[dict(grupo=None,operacion='one',causa='docker')]))+'\n')
        self.proc=self.root/'proc';(self.proc/'pressure').mkdir(parents=True)
        (self.proc/'pressure/cpu').write_text('some avg60=22\n')
        (self.proc/'pressure/io').write_text('full avg60=0\n')
        (self.proc/'meminfo').write_text('MemAvailable: 8388608 kB\n')

    def read_guard(self,now=NOW,errors=None):
        if 'proc' not in inspect.signature(snapshot.desk_guard).parameters:
            guard=snapshot.desk_guard(self.root)
            self.assertIsNotNone(guard,'mantener debe publicarse con sensores actuales')
            return guard
        return snapshot.desk_guard(self.root,self.proc,now,errors)

    def guard(self):return self.read_guard()

    def test_current_pressure_keep_iso_and_middle_band(self):
        guard=self.guard()
        self.assertIsNotNone(guard)
        self.assertEqual(guard['action'],'mantener')
        self.assertEqual(guard['at'],AT)
        self.assertEqual(guard['pressure']['cpu'],22)
        self.assertEqual(guard['thresholds']['normal']['pause']['io'],40)
        self.assertEqual(guard['last_result']['bloqueos'][0]['causa'],'docker')
        data=sample();data.update(host='desk',guard=guard)
        self.assertEqual(serve.validate_snapshot(data,'desk')['guard'],guard)

    def test_old_or_incomplete_tick_never_reuses_blocker(self):
        self.log.write_text(self.log.read_text()+json.dumps(dict(evento='decision',accion='mantener',utc=AT,tick_id='2'*24))+'\n')
        guard=self.guard()
        self.assertIsNotNone(guard)
        self.assertIsNone(guard['last_result'])
        self.assertEqual(guard['status'],'incomplete')
        self.assertIsNone(self.read_guard(NOW+121)['last_result'])

    def test_secret_fields_and_bool_pressure_are_rejected(self):
        for guard in (dict(action='mantener',envs={'x':'SECRET'}),dict(action='mantener',pressure=dict(at=AT,cpu=True,io=0,memory=1))):
            data=sample();data['guard']=guard
            with self.assertRaises(ValueError):serve.validate_snapshot(data,'mini')

    def test_corrupt_durable_projection_is_error(self):
        path=self.log.parent/'control.sqlite3'
        with sqlite3.connect(str(path)) as db:
            db.execute('CREATE TABLE guard_admission (id INTEGER PRIMARY KEY,body TEXT)')
            db.execute('INSERT INTO guard_admission VALUES (1,?)',('{"version":1,"last_attempt":null,"last_confirmed":{"at":"bad","grupo":"jale","id":1}}',))
        errors=[];guard=self.read_guard(errors=errors)
        self.assertIsNotNone(guard)
        self.assertIsNone(guard['last_admission'])
        self.assertTrue(any(e['stage']=='guard_admission' for e in errors))

    def test_durable_history_survives_log_rotation(self):
        confirmed=dict(at=AT,grupo='jale',id=7)
        attempt=dict(confirmed,boot_id='12345678-1234-1234-1234-123456789abc',reserved_ns=100,
            completed_ns=200,generation='linux-proc-v1:'+'1'*64,created_at=AT,enqueued_at=AT,outcome='confirmed')
        with sqlite3.connect(str(self.log.parent/'control.sqlite3')) as db:
            db.execute('CREATE TABLE guard_admission (id INTEGER PRIMARY KEY,body TEXT)')
            db.execute('INSERT INTO guard_admission VALUES (1,?)',(json.dumps(dict(version=1,last_attempt=attempt,last_confirmed=confirmed)),))
        self.log.write_text('')
        self.assertEqual(self.guard()['last_admission'],confirmed)

    def test_incoherent_permit_and_multiple_rows_do_not_publish_history(self):
        path=self.log.parent/'control.sqlite3'
        value=dict(version=1,last_attempt=None,last_confirmed=dict(at=AT,grupo='jale',id=1))
        with sqlite3.connect(str(path)) as db:
            db.execute('CREATE TABLE guard_admission (id INTEGER PRIMARY KEY,body TEXT)')
            db.execute('INSERT INTO guard_admission VALUES (1,?)',(json.dumps(value),))
        errors=[];guard=self.read_guard(errors=errors)
        self.assertIsNone(guard['last_admission'])
        self.assertIn('guard_admission',[e['stage'] for e in errors])
        with sqlite3.connect(str(path)) as db:db.execute('INSERT INTO guard_admission VALUES (2,?)',(json.dumps(value),))
        errors=[];guard=self.read_guard(errors=errors)
        self.assertIsNone(guard['last_admission'])
        self.assertIn('guard_admission',[e['stage'] for e in errors])

    def test_unknown_operation_is_accepted_without_inventing_decision(self):
        guard=self.guard();guard['last_result']['bloqueos']=[dict(grupo=None,operacion='unknown',causa='fresh_sample')]
        data=sample();data.update(host='desk',guard=guard)
        serve.validate_snapshot(data,'desk')

    def test_bad_sensor_and_unsafe_threshold_ast_no_fallback(self):
        self.assertIsNotNone(self.guard())
        (self.proc/'pressure/io').write_text('full avg60=nan\n')
        self.source.write_text('PRESSURE_THRESHOLDS = __import__("os").environ\n')
        errors=[];guard=self.read_guard(errors=errors)
        self.assertIsNotNone(guard)
        self.assertIsNone(guard['pressure']);self.assertIsNone(guard['thresholds'])
        self.assertEqual({e['stage'] for e in errors},{'guard_pressure','guard_thresholds'})

class MergedCellTests(unittest.TestCase):
    def fixture(self):
        fixture=json.loads((BASE/'tests/rack-fixture.json').read_text())
        fixture['now']=NOW
        entry=fixture['hosts'][0];fixture['hosts']=[entry];entry.update(id='desk',name='Desk · WSL',fresh=True)
        host=entry['snapshot']
        host.update(host='desk',taken=NOW,tasks=[dict(id=101,group='jale',state='Running',priority=0,start=NOW-300,name='Dirigida',ticket='SEN-6417'),dict(id=102,group='jale',state='Queued',priority=0,start=None,enqueued=NOW-120,name='Dirigida',ticket='SEN-6417')],control=dict(pause_reasons={'jale':['pressure']}),
                    groups=[dict(name='jale',status='Paused',slots=5)])
        host['guard']=dict(action='pausar',at=AT,status='fresh',pressure=dict(at=AT,cpu=34,io=0,memory=8*1024**3),
            thresholds=dict(normal=dict(pause=dict(cpu=30,io=40,memory=4*1024**3),resume=dict(cpu=15,io=20,memory=6*1024**3)),
                            rapidas=dict(pause=dict(memory=4*1024**3),resume=dict(memory=6*1024**3))),
            last_result=dict(evento='retenido',tick_id=TICK,at=AT,bloqueos=[dict(grupo=None,operacion='one',causa='pressure_high')]),last_admission=None)
        return fixture,host

    def check(self,fixture,expected,absent=(),held=True):
        js=(BASE/'index.html').read_text().split('<script>')[1].split('</script>')[0]
        result=subprocess.run(['node',str(BASE/'tests/trickle-ui-check.js')],input=json.dumps(dict(js=js,fixture=fixture,expected=expected,absent=absent,held=held)),capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        return result.stdout

    def test_high(self):
        f,h=self.fixture();self.check(f,'Retenido por presión',('Libre',))

    def test_middle(self):
        f,h=self.fixture();g=h['guard'];g['pressure']['cpu']=22;g['action']='mantener';g['last_result']['bloqueos']=[]
        g['last_admission']=dict(at='2026-11-14T15:59:20+00:00',grupo='jale',id=1)
        self.check(f,'última hace 40 s',('Libre',))

    def test_gate(self):
        f,h=self.fixture();g=h['guard'];g['pressure']['cpu']=22;g['action']='mantener';g['last_result']['bloqueos'][0]['causa']='docker'
        self.check(f,'Esperando Docker',('Entra de a una','Libre'))

    def test_full_silent(self):
        f,h=self.fixture();h['tasks']=[dict(id=i,group='jale',state='Running',priority=0,start=NOW-60,usage=None) for i in range(5)]
        self.check(f,'slot busy',('slot held','Retenido por presión'),held=False)

    def test_ambiguous_in_cell(self):
        f,h=self.fixture();h['control'].update(pause_reasons={'jale':['ambiguous','startup']},startup_holds={'jale':'pausa de origen desconocido'})
        out=self.check(f,'Pausa de origen desconocido',('Libre','Retenido al reiniciar'))
        self.assertEqual(out.count('Pausa de origen desconocido'),1)

    def test_unknown(self):
        f,h=self.fixture();h['control']=None;h['guard']=None
        self.check(f,'Estado de la guardia desconocido',('Libre',))

    def test_adverse_freshness_and_local_dependency(self):
        f,h=self.fixture();g=h['guard'];g['pressure']['cpu']=22;g['action']='mantener'
        g['last_result']['bloqueos']=[dict(grupo='jale',operacion='one',causa='dependency')]
        self.check(f,'Esperando dependencia',('Entra de a una','Libre'))
        for change in ('incomplete','thresholds','future'):
            f,h=self.fixture();g=h['guard'];g['pressure']['cpu']=22;g['action']='mantener'
            if change=='incomplete':g['last_result']=None
            if change=='thresholds':g['thresholds']=None
            if change=='future':g['pressure']['at']='2026-11-15T16:00:00+00:00'
            self.check(f,'Estado de la guardia desconocido',('Entra de a una','Libre'))
        f,h=self.fixture();h['guard']['action']='mantener'
        self.check(f,'Retenido por presión',('Entra de a una','Libre'))

    def test_io_memory_and_open(self):
        for kind,value,label in (('io',42,'E/S 42'),('memory',3*1024**3,'Memoria 3')):
            f,h=self.fixture();h['guard']['pressure'].update(cpu=0);h['guard']['pressure'][kind]=value
            self.check(f,label,('CPU 0','Libre'))
        f,h=self.fixture();h['groups'][0]['status']='Running'
        self.check(f,'Libre',('slot held',),held=False)

if __name__=='__main__':unittest.main()
