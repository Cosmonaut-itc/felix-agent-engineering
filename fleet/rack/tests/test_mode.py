"""U3.3 public/forced host-control contract with synthetic Pueue and transport."""
import copy
import json
import sqlite3
import subprocess
import unittest
from unittest.mock import patch
import test_desk_slots as desk_tests
from test_slots import accion, BASE, control, FakePueue


class ModeChannelTests(unittest.TestCase):
    def setUp(self):
        desk_tests.DeskChannelTests.setUp(self)
        self.payload['hosts'][0]['snapshot']['control'] = self.engine.view()
    test_mode_http_owner_host_origin = desk_tests.DeskChannelTests.test_desk_authorization_still_fresh_owner_host_origin

    def test_old_host_mode_rejected_before_transport_and_status_still_allowed(self):
        before = self.engine.view()
        self.actions.executor = lambda *a, **kw: self.calls.append(a[2]) or dict(
            schema='rack-host-result@1', status='unknown', action_id=a[2]['action_id'])
        for index, view in enumerate((None, dict(capabilities=dict(mode=False)), dict(capabilities={}))):
            request = self.request(ident='mode-old-'+str(index)+'-0001')
            self.payload['hosts'][0]['snapshot'] = {} if view is None else dict(control=view)
            code, answer = self.actions.post(request)
            after = self.engine.view()
            print('old-host', json.dumps(dict(shape=index, code=code, status=answer['status'],
                error=answer.get('error'), revisions=[before['revision'], after['revision']],
                vectors=[before['slots'], after['slots']], calls=self.calls), ensure_ascii=False))
            self.assertEqual((code, answer['status'], answer.get('error')),
                (409, 'rejected', dict(code='unsupported', message='Falta actualizar el control de este host.')))
            self.assertEqual(after, before)
            self.assertEqual((self.calls, self.p.calls), ([], []))
        # Compatibility gates must not prevent status for an already accepted ID.
        self.payload['hosts'][0]['snapshot'] = dict(control=before)
        request = self.request(ident='mode-status-001')
        self.assertEqual(self.actions.post(request)[1]['status'], 'unknown')
        self.payload['hosts'][0]['snapshot'] = {}
        self.now += 2
        self.actions.get(request['action_id'])
        self.assertEqual([c['op'] for c in self.calls], ['mode', 'status'])

    def request(self,mode='dia',ident='mode-channel-01',dry_run=False):
        v=self.engine.view()
        return dict(schema=accion.SCHEMA,op='mode',host='desk',action_id=ident,dry_run=dry_run,
                    expected={k:v[k] for k in ('revision','policy_hash','generation','mode','slots')},change=dict(mode=mode))

    def test_mode_public_full_table_undo_and_host_notes_are_sanitized(self):
        r=self.request()
        code,answer=self.actions.post(r)
        self.assertEqual((code,answer['status']),(200,'applied'))
        result=answer['result']
        self.assertEqual(result['mode'],'dia');self.assertEqual(result['cap'],14)
        self.assertEqual(result['observed'],control.mode_profile('dia'))
        self.assertEqual(result['mode_note'],'Adelantado · vuelve a noche a las 23:00')
        self.assertNotIn('before_mode_override',json.dumps(answer))
        undo=self.request(ident='mode-undo-0001');undo.update(op='undo',undo_of=r['action_id']);del undo['change']
        code,answer=self.actions.post(undo)
        self.assertEqual((code,answer['status']),(200,'applied'))
        self.assertEqual(answer['result']['mode'],'noche')
        self.assertEqual(answer['result']['mode_note'],'Automático · cambia a día a las 08:00')
        self.assertIsNone(answer['result']['expires_at'])
        try:
            safe=accion.host_channel.control(self.engine.view(),'desk')
        except ValueError as error:
            self.fail('El host válido debe conservar sus notas: '+str(error))
        self.assertTrue(safe['capabilities']['mode'])
        self.assertEqual(safe.get('mode_pending',{}).get('dia'),'Aplicando horario de día…')
        bad=copy.deepcopy(self.engine.view());bad['mode_note']='private command must never escape'
        with self.assertRaises(ValueError):accion.host_channel.control(bad,'desk')
        bad=copy.deepcopy(self.engine.view());bad['mode_pending']['dia']='arbitrary'
        with self.assertRaises(ValueError):accion.host_channel.control(bad,'desk')

    def test_host_tables_and_mode_notes_reject_opposite_mode_and_invalid_hours(self):
        view = self.engine.view()
        safe = accion.host_channel.control(view, 'desk')
        self.assertEqual(safe.get('tables'), {m:control.mode_profile(m) for m in ('dia','noche')})
        with self.assertRaises(ValueError):accion.host_channel.control(dict(view, mode_note=None), 'desk')
        for key in ('mode_note', 'mode_detail', 'slots_note'):
            for hour in ('24:00', '29:59'):
                bad = copy.deepcopy(view)
                bad[key] = dict(mode_note='Automático · cambia a día a las ',
                    mode_detail='Ajustes manuales hasta las ', slots_note='Lugares ajustados · hasta las ')[key] + hour
                with self.subTest(key=key, hour=hour), self.assertRaises(ValueError):
                    accion.host_channel.control(bad, 'desk')
        for mode, text in (('dia','día'), ('noche','noche')):
            for prefix in ('Automático · cambia a ', 'Adelantado · vuelve a '):
                bad = dict(view, mode=mode, cap=14 if mode=='dia' else 18, mode_note=prefix+text+' a las 08:00')
                with self.assertRaises(ValueError):accion.host_channel.control(bad, 'desk')
        for tables in (None, {}, dict(dia=control.mode_profile('dia')), dict(dia=control.mode_profile('noche'),noche=control.mode_profile('dia')),
                       dict(dia=dict(control.mode_profile('dia'), hasito=True), noche=control.mode_profile('noche'))):
            with self.subTest(tables=tables), self.assertRaises(ValueError):
                accion.host_channel.control(dict(view, tables=tables), 'desk')
        # Result notes use the same mode-sensitive whitelist, preserving next_event.
        request = self.request(); wire = {k:v for k,v in request.items() if k not in ('schema','host')}
        wire['schema'] = control.HOST_SCHEMA
        response = self.engine.handle(wire)
        safe = accion.host_channel.sanitize(request, response, 'mode')
        self.assertEqual(safe['result']['next_event'], response['result']['next_event'])
        bad = copy.deepcopy(response); bad['result']['mode_note']='Automático · cambia a día a las 08:00'
        with self.assertRaises(ValueError):accion.host_channel.sanitize(request, bad, 'mode')

    def test_mode_schema_closed_and_same_mode_rejection_bound_after_lost_post(self):
        r=self.request()
        for change in ({'mode':'fijo'},{'mode':True},{'mode':'dia','actor':'Felix'},{'delta':-1}):
            with self.assertRaises(ValueError):accion.validate(dict(r,change=change))
        for key in ('actor','expires_at','launcher','ref'):
            with self.assertRaises(ValueError):accion.validate(dict(r,**{key:'forbidden'}))
        r=self.request('noche')
        def lost(*a,**kw):self.execute(*a,**kw);raise OSError('lost')
        self.actions.executor=lost
        self.assertEqual(self.actions.post(r)[1]['status'],'unknown')
        self.actions.executor=self.execute
        code,answer=self.actions.get(r['action_id'])
        self.assertEqual((code,answer['status'],(answer.get('error') or {}).get('code')),(409,'rejected','same_mode'))
        self.assertEqual([c['op'] for c in self.calls],['mode','status'])
        self.assertEqual(self.p.calls,[])

    def test_unknown_mode_never_replays_and_another_body_conflicts(self):
        r=self.request()
        def lost(*a,**kw):
            with patch.object(self.engine,'reconcile',side_effect=OSError('offline')):self.execute(*a,**kw)
            raise OSError('lost')
        self.actions.executor=lost
        self.assertEqual(self.actions.post(r)[1]['status'],'unknown')
        self.assertEqual(self.actions.post(r)[1]['status'],'unknown')
        changed=copy.deepcopy(r);changed['change']['mode']='noche'
        self.assertEqual(self.actions.post(changed)[1]['error']['code'],'idempotency_conflict')
        self.assertEqual(self.p.calls,[])
        self.engine.reconcile();self.actions.executor=self.execute
        self.assertEqual(self.actions.get(r['action_id'])[1]['status'],'applied')
        self.assertEqual([c['op'] for c in self.calls],['mode','status'])

    def test_missed_timer_mode_conflict_recovers_bound_receipt(self):
        self.actions.post(self.request())
        self.now+=1;self.payload['now']=self.now
        r=self.request('noche','mode-missed-001')
        def lost(*a,**kw):self.execute(*a,**kw);raise OSError('lost')
        self.actions.executor=lost
        self.assertEqual(self.actions.post(r)[1]['status'],'unknown')
        self.actions.executor=self.execute
        code,answer=self.actions.get(r['action_id'])
        self.assertEqual((code,answer['status'],(answer.get('error') or {}).get('code')),(409,'conflict','stale_state'))
        self.assertTrue(answer.get('effects'))
        self.assertEqual(self.engine.view()['slots'],control.desk_profile(self.now))
        self.assertEqual([c['op'] for c in self.calls],['mode','mode','status'])

    def test_dry_run_mode_zero_receipts_zero_rpc_old_host_and_mini(self):
        self.assertEqual(self.actions.post(self.request(dry_run=True))[1]['status'],'dry_run')
        with self.actions.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM actions').fetchone()[0],0)
        self.assertEqual(self.p.calls,[])
        old=dict(schema='rack-cancel-result@1',error=dict(code='invalid_request'))
        self.assertEqual(accion.host_channel.sanitize(self.request(),old,'mode')['status'],'unsupported')
        v=self.engine.view();v['capabilities']['mode']=False
        self.assertFalse(accion.host_channel.control(v,'desk')['capabilities']['mode'])
        p=FakePueue();engine=control.MiniControl(p,self.root/'mini',clock=lambda:self.now,generation=lambda:'mini-1')
        with control.locked(engine.directory):engine.adopt()
        v=engine.view();r=self.request();r.update(host='mini',expected={k:v[k] for k in ('revision','policy_hash','generation','mode','slots')})
        accion.validate(r)
        wire={k:v for k,v in r.items() if k not in ('host','schema')};wire['schema']=control.HOST_SCHEMA
        self.assertEqual(engine.handle(wire)['status'],'unsupported')
        self.assertEqual(p.calls,[])

    def test_forced_mode_envelope_uses_existing_fixed_controller(self):
        desk=accion.desk_channel
        wire={k:v for k,v in self.request().items() if k not in ('schema','host')};wire['schema']=control.HOST_SCHEMA
        envelope=dict(schema='rack-desk-action@1',target='host-control',request=wire)
        calls=[]
        def exchange(argv,request,runner,env,timeout):
            calls.append((argv,request))
            return 0,self.engine.handle(request)
        with patch.object(desk,'HOST_CONTROLLER',self.root/'controller'), patch.object(desk.Path,'is_file',return_value=True), \
             patch.object(desk,'exchange',side_effect=exchange):
            answer=desk.handle_host(envelope,None)
        self.assertEqual(answer['status'],'applied')
        self.assertEqual(calls[0][0],['/usr/bin/python3','-I',str(self.root/'controller'),'rack','--host','desk'])
        self.assertEqual(calls[0][1],wire)

    def fixture(self):
        v=accion.host_channel.control(self.engine.view(),'desk')
        return dict(now=self.now,tickets={},hosts=[dict(id='desk',name='Felix DESKTOP · WSL',fresh=True,age=0,
            estimates={},actions=dict(reason=None),snapshot=dict(control=v,
            groups=[dict(name=g,slots=n,status='Running') for g,n in v['slots'].items()],tasks=[],
            cpu=dict(cores=12,load=[1,1,1]),mem=dict(total=32*1024**3,available=24*1024**3,swap_total=0,swap_used=0)))])

    def test_reply_time_is_stamped_per_reply_and_never_journaled(self):
        r=self.request();_,first=self.actions.respond('post',r)
        self.assertEqual((first['status'],first['served_at']),('applied',self.now))
        self.now+=30
        _,again=self.actions.respond('get',r['action_id'])
        self.assertEqual((again['status'],again['served_at']),('applied',self.now))
        with sqlite3.connect(self.actions.path) as db:
            stored=[json.loads(body) for body, in db.execute('SELECT response FROM actions')]
        self.assertTrue(stored);self.assertTrue(all('served_at' not in body for body in stored))

    def test_mode_production_ui_states_undo_toast_and_unknown_no_resend(self):
        fixture=self.fixture()
        r=self.request();_,applied=self.actions.respond('post',r)
        source=(BASE/'index.html').read_text().split('<script>',1)[1].split('</script>',1)[0]
        run=subprocess.run(['node',str(BASE/'tests/mode-ui-check.js')],input=json.dumps(dict(js=source,fixture=fixture,applied=applied)),
                           text=True,capture_output=True,timeout=10)
        self.assertEqual(run.returncode,0,run.stdout+run.stderr)
        self.assertIn('UI mode OK',run.stdout)

    def check_ui(self, check):
        source=(BASE/'index.html').read_text().split('<script>',1)[1].split('</script>',1)[0]
        result=subprocess.run(['node',str(BASE/'tests/mode-ui-check.js')],
            input=json.dumps(dict(js=source,fixture=self.fixture(),check=check)),
            text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        print(result.stdout.strip())

    def test_ui_conflict_and_slots_note_coexist_on_both_hosts(self):
        self.check_ui('conflict')

    def test_ui_undo_target_both_directions_survives_reload(self):
        self.check_ui('undo')

    def test_ui_pending_has_one_note_by_mode_segment(self):
        self.check_ui('pending')

    def test_ui_uses_host_destination_table_for_foreign_work(self):
        self.check_ui('tables')
