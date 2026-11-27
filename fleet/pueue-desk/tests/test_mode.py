"""U3.3, synthetic clock/Pueue/sensors; all files stay in our /tmp fixtures."""
import copy
import io
import json
import importlib.util
import sqlite3
import subprocess
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from test_desk_slots import DeskFixture, utc
from test_slots import control, FakePueue


class ModeTests(DeskFixture):
    def test_aborted_receipt_after_opposite_event_has_coherent_mode_cap_notes_and_vector(self):
        self.go_day()
        request = self.mode_request('noche', 'receipt-expired1')
        with patch.object(self.engine, 'reconcile', side_effect=OSError('offline')):
            self.assertEqual(self.engine.handle(request)['status'], 'unknown')
        self.now = utc('2026-11-15T14:00:00')
        self.assertTrue(self.engine.reconcile())
        result = self.status(request)
        self.assertEqual((result['status'], result['error']['code']), ('aborted','expired'))
        payload = result['result']
        self.assertEqual(result['revision'], self.state()['revision'])
        self.assertEqual((payload['mode'], payload['cap'], payload['desired'], payload['observed']),
            ('dia', 15, control.mode_profile('dia'), control.mode_profile('dia')))
        self.assertEqual(payload['mode_note'], 'Automático · cambia a noche a las 23:00')
        for key in ('mode_detail','slots_note','expires_at','undo_of'):
            self.assertIsNone(payload[key], key)
        self.assertEqual(payload['schedule_event'], self.now)
        self.assertEqual(payload['next_event'], control.schedule_at(self.now)[1])

    def test_missing_tzdata_disables_only_mode_and_keeps_slots_undo_and_utc_recovery(self):
        mode = self.mode_request()
        slots = self.request('tzdata-slots-01')
        before = self.state(); self.p.calls.clear()
        with patch.object(control, 'ZoneInfo', side_effect=KeyError('no tzdata')):
            view = self.engine.handle(dict(schema=control.HOST_SCHEMA, op='capabilities'))
            self.assertEqual(view['status'], 'ok')
            self.assertEqual((view['capabilities']['mode'], view['capabilities']['slots'], view['capabilities']['undo']),
                             (False, True, True))
            self.assertIsNone(view['mode_note'])
            self.assertEqual(view['tables'], {m:control.mode_profile(m) for m in ('dia','noche')})
            result = self.engine.handle(mode)
            self.assertEqual((result['status'], (result.get('error') or {}).get('code'), result.get('effects')), ('rejected','unsupported',False))
            self.assertEqual(self.state(), before); self.assertEqual(self.p.calls, [])
            self.assertEqual(self.engine.handle(slots)['status'], 'applied')
            undo = self.request('tzdata-undo-001', undo_of=slots['action_id'])
            self.assertEqual(self.engine.handle(undo)['status'], 'applied')
            self.assertEqual(self.state()['desired'], before['desired'])
            self.now = control.schedule_at(self.now)[1]
            self.assertTrue(self.engine.reconcile())
            self.assertEqual(self.state()['desired'], control.desk_profile(self.now))
            self.assertIsNone(self.state()['mode_override'])

    def test_notes_cross_event_rejects_mode_undo_and_ensayo_before_acceptance(self):
        for undo in (False, True):
            for dry in (False, True):
                with self.subTest(undo=undo, dry_run=dry):
                    t = ModeTests(); t.setUp()
                    try:
                        t.go_day()
                        if undo:
                            t.apply_mode('noche', 'notes-before-01')
                            request = t.request('notes-cross-001', undo_of='notes-before-01', dry_run=dry)
                        else:
                            request = t.mode_request('noche', 'notes-cross-001', dry_run=dry)
                        boundary = control.schedule_at(t.now)[1]
                        t.now = boundary - .1
                        before = t.state(); files = t.fingerprint(); t.p.calls.clear()
                        original = control.mode_notes
                        def crossing(*args):
                            result = original(*args)
                            t.now = boundary
                            return result
                        with patch.object(control, 'mode_notes', side_effect=crossing):
                            result = t.engine.handle(request)
                        after = t.state()
                        print('notes-cross-event', json.dumps(dict(undo=undo, dry_run=dry,
                            status=result['status'], error=result.get('error'), effects=result.get('effects'),
                            revisions=[before['revision'], after['revision']],
                            vectors=[before['desired'], after['desired']], calls=t.p.calls), ensure_ascii=False))
                        self.assertEqual((result['status'], result.get('error'), result.get('effects')),
                            ('conflict', dict(code='undo_conflict' if undo else 'stale_state', message=
                                'El horario o los lugares cambiaron; no se puede deshacer este ajuste' if undo else
                                'Los lugares cambiaron. Actualiza antes de decidir.'), False))
                        self.assertEqual(after, before)
                        self.assertEqual(t.p.calls, [])
                        if dry:
                            self.assertEqual(t.fingerprint(), files)
                        else:
                            self.assertEqual(t.status(request)['error'], result['error'])
                    finally:
                        t.doCleanups()

    def mode_request(self, mode='dia', ident='mode-00000001', dry_run=False):
        r = self.request(ident, dry_run=dry_run)
        r.update(op='mode', change=dict(mode=mode))
        return r

    def apply_mode(self, mode, ident='mode-00000001'):
        return self.engine.handle(self.mode_request(mode, ident))

    def go_day(self):
        self.now = utc('2026-11-14T22:00:00')  # 16:00 Mexico, day schedule
        self.assertTrue(self.engine.reconcile())
        self.p.calls.clear()

    def test_complete_destination_caps_clear_adjustments_and_reductions_first(self):
        self.go_day()
        self.assertEqual(self.engine.handle(self.request())['status'], 'applied')
        self.assertTrue(self.state()['overrides'])
        night = self.apply_mode('noche')
        self.assertEqual(night['status'], 'applied')
        self.assertEqual(night['result']['desired'], control.mode_profile('noche'))
        self.assertEqual((night['result']['mode'], night['result']['cap']), ('noche',20))
        self.assertEqual(self.state()['overrides'], {})
        # Redistribute at night so the day transition has both decreases/increases.
        target = dict(control.mode_profile('noche'), otros=1)
        self.set_desire(target)
        self.assertTrue(self.engine.reconcile())
        self.p.calls.clear()
        day = self.apply_mode('dia', 'mode-day-00001')
        self.assertEqual(day['status'], 'applied')
        self.assertEqual(day['result']['observed'], control.mode_profile('dia'))
        self.assertEqual(self.p.calls, [('parallel','caja',1), ('parallel','hasito',1), ('parallel','jale',5),
                                       ('parallel','pesadas',2), ('parallel','otros',2)])
        self.assertEqual((self.engine.view()['mode'], self.engine.view()['cap']), ('dia',15))
        self.assertIsNone(self.state()['mode_override'])
        self.assertEqual(self.state()['overrides'], {})

    def test_same_mode_preserves_adjustments_and_has_no_rpc(self):
        self.engine.handle(self.request())
        before = self.state()
        self.p.calls.clear()
        answer = self.apply_mode('noche')
        self.assertEqual((answer['status'],(answer.get('error') or {}).get('code'),answer.get('effects')), ('rejected','same_mode',False))
        self.assertEqual(self.state(), before)
        self.assertEqual(self.p.calls, [])

    def test_critical_example_notes_expire_even_when_event_confirms_same_mode(self):
        self.go_day()
        result = self.apply_mode('noche')
        expiry = utc('2026-11-15T05:00:00')
        self.assertEqual(result['result']['expires_at'], expiry)
        self.assertEqual(self.state()['mode_override']['expires_at'], expiry)
        self.assertEqual(self.engine.view()['mode_note'], 'Adelantado · vuelve a día a las 08:00')
        self.assertEqual(self.engine.view()['mode_detail'], 'Ajustes manuales hasta las 23:00')
        self.assertIsNone(self.engine.view()['slots_note'])
        self.engine.handle(self.request('night-slots-001'))
        self.assertEqual(self.engine.view()['slots_note'], 'Lugares ajustados · hasta las 23:00')
        before = self.fingerprint()
        self.now = expiry
        view = self.engine.view()  # missed timer: virtual expiry, zero writes
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual(view['mode'], 'noche')
        self.assertEqual(view['mode_note'], 'Automático · cambia a día a las 08:00')
        self.assertIsNone(view['mode_detail'])
        self.assertIsNone(view['expires_at'])
        self.assertTrue(self.engine.reconcile())
        self.assertEqual(self.state()['overrides'], {})
        self.assertIsNone(self.state()['mode_override'])
        self.assertEqual(view['revision'], self.state()['revision'])
        self.assertEqual(self.engine.view()['slots'], control.mode_profile('noche'))

    def test_notes_automatic_and_day_advanced(self):
        self.assertEqual(self.engine.view()['mode_note'], 'Automático · cambia a día a las 08:00')
        result = self.apply_mode('dia')
        self.assertEqual(result['result']['mode_note'], 'Adelantado · vuelve a noche a las 23:00')
        self.assertEqual(self.engine.view()['mode_detail'], 'Ajustes manuales hasta las 08:00')
        self.now += 1
        self.assertTrue(self.engine.reconcile())
        self.assertEqual(self.engine.view()['mode_note'], 'Automático · cambia a noche a las 23:00')
        self.assertEqual(self.engine.view()['mode_pending'], dict(dia='Aplicando horario de día…', noche='Aplicando horario de noche…'))
        self.assertEqual(self.engine.view()['mode_partial'], 'Horario aplicado parcialmente · revisando…')
        self.assertEqual(self.engine.view()['mode_unknown'], 'Cambio sin confirmar · consultando…')

    def test_strict_next_event_exact_boundary_and_missed_entire_day(self):
        self.assertEqual(control.schedule_at(utc('2026-11-14T14:00:00'))[1],utc('2026-11-15T05:00:00'))
        self.now += 1
        self.assertTrue(self.engine.reconcile())
        self.assertEqual(control.schedule_at(self.now)[1], utc('2026-11-15T05:00:00'))
        self.assertEqual(self.apply_mode('noche')['result']['expires_at'], utc('2026-11-15T05:00:00'))
        revision = self.state()['revision']
        self.now += 86400
        self.assertTrue(self.engine.reconcile())
        self.assertEqual(self.state()['revision'], revision+1)
        self.assertIsNone(self.state()['mode_override'])
        self.assertEqual(self.engine.view()['mode'], 'dia')

    def test_mode_undo_restores_original_mode_advance_adjustments_expirations(self):
        self.go_day()
        self.assertEqual(self.apply_mode('noche')['status'], 'applied')
        self.now += 300
        self.engine.handle(self.request('night-minus-001'))
        before = self.state()
        self.now += 300
        self.assertEqual(self.apply_mode('dia','mode-back-0001')['status'], 'applied')
        self.now += 300
        undo = self.engine.handle(self.request('mode-undo-0001', undo_of='mode-back-0001'))
        self.assertEqual(undo['status'], 'applied')
        after = self.state()
        for key in ('desired','overrides','mode_override','schedule_event'):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(undo['result']['mode'], 'noche')
        self.assertEqual(undo['result']['expires_at'], utc('2026-11-15T05:00:00'))

    def test_undo_after_adjustment_or_event_or_observed_drift_conflicts_no_rpc(self):
        self.go_day()
        self.apply_mode('noche')
        self.engine.handle(self.request('after-mode-001'))
        self.p.calls.clear()
        result = self.engine.handle(self.request('undo-later-0001',undo_of='mode-00000001'))
        self.assertEqual((result['status'],(result.get('error') or {}).get('code'),result.get('effects')), ('conflict','undo_conflict',False))
        self.assertEqual(self.p.calls, [])
        self.apply_mode('dia','second-mode-01')
        undo = self.request('undo-expiry-001', undo_of='second-mode-01')
        self.now = utc('2026-11-15T05:00:00')
        self.p.calls.clear()
        rpc_before = self.p.rpc
        result = self.engine.handle(undo)
        self.assertEqual(self.p.rpc, rpc_before, 'Expired undo must not query Pueue')
        self.assertEqual((result.get('error') or {}).get('code'), 'undo_conflict')
        self.assertEqual(self.p.calls, [])
        self.assertTrue(self.engine.reconcile())
        self.apply_mode('dia','third-mode-001')
        undo = self.request('undo-drift-0001',undo_of='third-mode-001')
        self.p.groups['otros']['parallel_tasks'] = 1
        self.p.calls.clear()
        self.assertEqual((self.engine.handle(undo).get('error') or {}).get('code'), 'undo_conflict')
        self.assertEqual(self.p.calls, [])

    def foreign(self):
        original = self.p.status
        def status():
            value = original(); value['tasks'] = {'1':dict(group='default',status='Queued')}
            return value
        return patch.object(self.p,'status',side_effect=status)

    def test_foreign_work_vetoes_entire_mode_and_undo_increases_but_allows_decrease(self):
        with self.foreign():
            # Night -> day only decreases; undo raises and is vetoed entirely.
            self.assertEqual(self.apply_mode('dia')['status'], 'applied')
            before = self.state()
            self.p.calls.clear()
            r = self.engine.handle(self.request('foreign-undo-01',undo_of='mode-00000001'))
            self.assertEqual((r['status'],(r.get('error') or {}).get('code'),r.get('effects')), ('rejected','foreign_work',False))
            self.assertEqual(self.state(),before)
            self.assertEqual(self.p.calls, [])
            r = self.apply_mode('noche','foreign-mode-01')
            self.assertEqual((r.get('error') or {}).get('code'), 'foreign_work')
            self.assertEqual(self.p.calls, [])
        self.assertEqual(self.apply_mode('noche','foreign-gone-01')['status'], 'applied')

    def test_dry_run_and_capabilities_never_recover_pending_or_expired_state(self):
        self.go_day()
        before = self.fingerprint()
        r = self.engine.handle(self.mode_request('noche',dry_run=True))
        self.assertEqual(r['status'],'dry_run')
        self.assertEqual(r['result']['desired'], control.mode_profile('noche'))
        self.assertEqual(r['result']['expires_at'], utc('2026-11-15T05:00:00'))
        self.assertEqual(self.fingerprint(),before)
        self.assertEqual(self.p.calls, [])
        with patch.object(self.engine,'reconcile',side_effect=OSError('offline')):
            self.assertEqual(self.apply_mode('noche')['status'],'unknown')
        before = self.fingerprint()
        self.p.calls.clear()
        self.engine.view()
        self.engine.handle(self.mode_request('dia','dry-pending-01',True))
        self.now = utc('2026-11-15T05:00:00')
        self.engine.handle(self.mode_request('dia','dry-expired-01',True))
        self.engine.handle(dict(schema=control.HOST_SCHEMA,op='capabilities'))
        self.assertEqual(self.fingerprint(),before)
        self.assertEqual(self.p.calls, [])

    def test_lost_id_is_never_repeated_and_different_body_conflicts(self):
        self.go_day()
        r = self.mode_request('noche')
        with patch.object(self.engine,'reconcile',side_effect=OSError('offline')):
            self.assertEqual(self.engine.handle(r)['status'],'unknown')
        self.p.calls.clear()
        self.assertEqual(self.engine.handle(r)['status'],'applying')
        changed = copy.deepcopy(r); changed['change']['mode']='dia'
        self.assertEqual((self.engine.handle(changed).get('error') or {}).get('code'),'idempotency_conflict')
        self.assertEqual(self.p.calls, [])
        self.assertTrue(self.engine.reconcile())
        self.assertEqual(self.status(r)['status'],'applied')

    def test_failure_after_each_parallel_recovers_same_desire_with_visible_partial(self):
        self.go_day()
        original = self.p.parallel
        calls = []
        for count in (1,2,3):
            self.p.calls.clear()
            index = [0]
            def broken(g,n):
                original(g,n); index[0]+=1
                if index[0]==count: raise subprocess.CalledProcessError(1,['fake-rpc'])
            with patch.object(self.p,'parallel',side_effect=broken):
                result = self.apply_mode('noche','rpc-failure-'+str(count))
            self.assertEqual(result['status'],'partial')
            self.assertEqual(result['result']['desired'],control.mode_profile('noche'))
            self.assertTrue(self.engine.reconcile())
            self.assertEqual(self.status(dict(action_id='rpc-failure-'+str(count)))['status'],'applied')
            self.assertEqual(self.apply_mode('dia','rpc-reset-'+str(count))['status'],'applied')

    def test_event_in_final_read_before_receipt_aborts_mode_and_undo(self):
        self.go_day()
        original = self.engine.generation
        for undo in (False,True):
            if undo:
                # Set up away from event with a manual night, then undo it.
                self.now = utc('2026-11-15T22:00:00'); self.engine.reconcile()
                self.apply_mode('noche','boundary-night1')
                request = self.request('boundary-undo01',undo_of='boundary-night1')
            else:
                request = self.mode_request('noche','boundary-mode01')
            self.now = control.schedule_at(self.now)[1]-.1
            seen = [0]
            def generation(state):
                seen[0]+=1
                if seen[0]==6: self.now=control.schedule_at(self.now)[1]
                return original(state)
            with patch.object(self.engine,'generation',side_effect=generation):
                result = self.engine.handle(request)
            self.assertEqual(result['status'],'aborted')
            self.assertEqual(self.engine.view()['slots'],control.desk_profile(self.now))
            self.assertIsNone(self.state()['mode_override'])

    def test_mode_never_starts_or_stops_tasks_when_reducing_20_to_15(self):
        original = self.p.status
        def status():
            s=original();s['tasks']={str(i):dict(group='jale',status={'Running':{}}) for i in range(7)};return s
        with patch.object(self.p,'status',side_effect=status):
            self.assertEqual(self.apply_mode('dia')['status'],'applied')
            self.assertEqual(len(self.p.status()['tasks']),7)
        self.assertTrue(all(a[0]=='parallel' for a in self.p.calls))
        self.assertEqual(self.p.groups['jale']['parallel_tasks'],5)

    def test_corrupt_mode_metadata_keeps_protective_closing_and_rapidas_ladder(self):
        original = self.state()
        for bad in ({'mode':'noche'}, dict(mode='dia',actor='Felix',action_id='corrupt-0001',
                    requested_at=10**400,expires_at=control.schedule_at(self.now)[1])):
            with self.engine.connect(write=True) as db:self.engine.store(db,dict(original,mode_override=bad))
            for g in control.GROUP_LIMITS:self.p.groups[g]['status']='Running'
            self.p.calls.clear()
            with self.assertRaises((ValueError,KeyError,ArithmeticError)):
                self.engine.guard(dict(io=90,cpu=90,memory=1024**3))
            self.assertEqual([a for a in self.p.calls if a[0]=='start'],[])
            self.assertCountEqual([a[-1] for a in self.p.calls if a[0]=='pause'], control.GROUP_LIMITS.keys())
        with self.engine.connect(write=True) as db:self.engine.store(db,original)
        self.p.calls.clear()
        # All emergency pauses are fenced; a later healthy guard cannot claim them.
        with patch.object(control,'pressure',return_value=self.low):self.engine.guard(self.low)
        self.assertEqual([a for a in self.p.calls if a[0]=='start'],[])

    def test_mini_rejects_mode_and_keeps_fixed_profile(self):
        p = FakePueue(); engine = control.MiniControl(p,self.root/'mini',clock=lambda:self.now,generation=lambda:self.gen)
        with control.locked(engine.directory):engine.adopt(self.root/'mini-pid')
        v=engine.view();r=self.mode_request();r['expected']={k:v[k] for k in ('revision','policy_hash','generation','mode','slots')}
        result=engine.handle(r)
        self.assertEqual((result['status'],result.get('effects')),('unsupported',False))
        self.assertFalse(engine.view()['capabilities']['mode'])
        self.assertEqual(engine.view()['mode'],'fijo')
        self.assertEqual(p.calls,[])
        self.assertIsNone(engine.view()['slots_note'])
        down=dict(r,op='slots',action_id='mini-note-0001',change=dict(group='suites',delta=-1))
        adjusted=engine.handle(down)
        self.assertEqual(adjusted['status'],'applied')
        self.assertEqual(engine.view()['slots_note'],'Lugares ajustados · hasta otro cambio manual')
        changed=copy.deepcopy(r);changed['change']['mode']='noche'
        self.assertEqual((engine.handle(changed).get('error') or {}).get('code'),'idempotency_conflict')


    def test_undo_revision_conflicts_even_when_later_adjustments_restore_same_vector(self):
        self.go_day();self.apply_mode('noche')
        self.engine.handle(self.request('later-minus-001'))
        self.engine.handle(self.request('later-plus-0001',delta=1))
        self.p.calls.clear()
        result=self.engine.handle(self.request('undo-samevec-01',undo_of='mode-00000001'))
        self.assertEqual((result.get('error') or {}).get('code'),'undo_conflict')
        self.assertEqual(self.p.calls,[])

    def test_mode_lock_busy_is_terminal_and_has_no_effects(self):
        request=self.mode_request()
        out=io.StringIO();before=self.fingerprint()
        with patch.object(control,'STATE_DIR',self.engine.directory), patch.object(control,'Pueue',return_value=self.p), \
             patch.object(control,'wait_rack_lock',return_value=False), \
             patch.object(control.sys,'argv',['control.py','rack','--host','desk']), \
             patch.object(control.sys,'stdin',io.TextIOWrapper(io.BytesIO(json.dumps(request).encode()))), redirect_stdout(out):
            control.main()
        answer=json.loads(out.getvalue())
        self.assertEqual((answer['status'],(answer.get('error') or {}).get('code'),answer.get('effects')),('rejected','lock_busy',False))
        self.assertEqual(self.fingerprint(),before);self.assertEqual(self.p.calls,[])

    def test_mode_event_revalidated_immediately_before_each_receipt(self):
        self.go_day();original=self.engine.reply;crossed=[False]
        def reply(request,status,*args,**kwargs):
            if status=='applied' and not crossed[0]:
                crossed[0]=True;self.now=control.schedule_at(self.now)[1]
            return original(request,status,*args,**kwargs)
        with patch.object(self.engine,'reply',side_effect=reply):result=self.apply_mode('noche')
        self.assertTrue(crossed[0]);self.assertEqual(result['status'],'aborted')
        self.assertIsNone(self.state()['mode_override'])
        self.assertEqual(self.engine.view()['mode_note'],'Automático · cambia a día a las 08:00')

    def test_guard_final_read_with_mode_advance_crossing_event_never_starts(self):
        self.go_day();self.apply_mode('noche')
        state=self.state();state['reasons']['jale']=['pressure'];self.p.groups['jale']['status']='Paused'
        with self.engine.connect(write=True) as db:self.engine.store(db,state)
        original=self.engine.observed;count=[0]
        def observe():
            value=original();count[0]+=1
            if count[0]==3:self.now=control.schedule_at(self.now)[1]
            return value
        self.p.calls.clear();failure=None
        with patch.object(self.engine,'observed',side_effect=observe), patch.object(control,'pressure',return_value=self.low):
            try:self.engine.guard(self.low)
            except ValueError as error:failure=error
        self.assertIsNotNone(failure)
        self.assertEqual([a for a in self.p.calls if a[0]=='start'],[])
        self.assertEqual(self.p.groups['jale']['status'],'Paused')

    def test_same_mode_after_missed_timer_does_not_restore_slots_silently(self):
        self.go_day();self.apply_mode('noche');self.engine.handle(self.request('same-lower-0001'))
        self.now=control.schedule_at(self.now)[1]
        before=self.state();self.p.calls.clear()
        result=self.apply_mode('noche','same-expired-01')
        self.assertEqual((result['status'],(result.get('error') or {}).get('code'),result.get('effects')),('rejected','same_mode',False))
        self.assertEqual(self.state(),before);self.assertEqual(self.p.calls,[])


class ModeMigrationTests(DeskFixture):
    # Reuse only the fixture, not ModeTests cases (loader below selects these names).
    def test_existing_adopted_authority_migrates_preserving_receipts_and_expirations(self):
        self.engine.handle(self.request())
        with self.engine.connect(write=True) as db:
            prior=self.engine.state(db);prior['startup_version']=1;del prior['mode_override'];self.engine.store(db,prior)
            receipts=db.execute('SELECT * FROM host_actions').fetchall()
        spec=importlib.util.spec_from_file_location('mode_installer',control.Path(control.__file__).with_name('install-desk.py'))
        installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)
        before=self.fingerprint()
        with installer.authority_lock(self.engine.directory):plan=installer.policy_plan(self.engine)
        self.assertEqual(self.fingerprint(),before)
        self.assertEqual(plan,dict(prior,mode_override=None))
        with self.engine.connect(write=True) as db:self.engine.store(db,plan)
        with self.engine.connect() as db:
            self.assertEqual(db.execute('SELECT * FROM host_actions').fetchall(),receipts)
        self.assertEqual(self.state()['overrides'],prior['overrides'])
        self.assertEqual(self.state()['revision'],prior['revision'])
        self.p.calls.clear()
        for status in ('applying','partial','unknown'):
            with self.engine.connect(write=True) as db:
                self.engine.store(db,prior)
                db.execute('INSERT OR REPLACE INTO host_actions VALUES (?,?,?)',('open-migrate-01','digest',json.dumps(dict(response=dict(status=status)))))
            before=self.fingerprint()
            with self.assertRaisesRegex(ValueError,'recibos abiertos'):
                with installer.authority_lock(self.engine.directory):installer.policy_plan(self.engine)
            self.assertEqual(self.fingerprint(),before)
            self.assertEqual(self.p.calls,[])


class ModeScheduleTests(unittest.TestCase):
    def test_event_is_strict_at_exact_boundary(self):
        self.assertEqual(control.schedule_at(utc('2026-11-14T14:00:00'))[1],utc('2026-11-15T05:00:00'))
        self.assertEqual(control.schedule_at(utc('2026-11-15T05:00:00'))[1],utc('2026-11-15T14:00:00'))

class ModeInstallTests(DeskFixture):
    def systemctl(self, home, commands, *args):
        commands.append(args)
        values=dict(MainPID='321',ActiveState='active',NeedDaemonReload='no',Restart='always',KillMode='mixed',OOMPolicy='continue',StartLimitIntervalUSec='0',
                    ExecStartPre='/usr/bin/python3 -I '+str(home/'.local/lib/pao-pueue-desk/control.py')+' preparar --host desk')
        text=''.join(k+'='+v+'\n' for k,v in values.items()) if args[2]=='show' else ''
        return subprocess.CompletedProcess(args,0,text,'')

    def setup_install(self):
        self.engine.handle(self.request())
        home=self.root/'installed'
        directory=home/'.local/state/pao/pueue-desk';directory.parent.mkdir(parents=True)
        self.engine.directory.rename(directory)
        self.engine.directory=directory;self.engine.path=directory/'control.sqlite3'
        library=home/'.local/lib/pao-pueue-desk';library.mkdir(parents=True)
        target=library/'control.py';target.write_text('# U3.2 instalado\n')
        with self.engine.connect(write=True) as db:
            original=self.engine.state(db);del original['mode_override'];self.engine.store(db,original)
            receipts=db.execute('SELECT * FROM host_actions').fetchall()
        spec=importlib.util.spec_from_file_location('mode_install_complete',control.Path(control.__file__).with_name('install-desk.py'))
        installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)
        Original=control.MiniControl
        factory=lambda p,d,**kw:Original(p,d,clock=lambda:self.now,generation=lambda:self.gen,**kw)
        commands=[]
        for manager in (patch.object(control,'validate_configuration',return_value=dict(data=self.root,pid_file=self.root/'pid')),
                        patch.object(control,'read_queue',return_value=dict(tasks={},groups={})),
                        patch.object(control,'Pueue',return_value=self.p),
                        patch.object(control,'MiniControl',side_effect=factory),
                        patch.object(control,'desk_generation',return_value=self.gen),
                        patch.object(installer,'run',side_effect=lambda *a:self.systemctl(home,commands,*a))):
            manager.start();self.addCleanup(manager.stop)
        return installer,home,target,original,receipts,commands

    def test_additive_seco_and_actual_install_preserve_policy_receipts_and_daemon(self):
        installer,home,target,original,receipts,commands=self.setup_install()
        before=self.fingerprint();self.p.calls.clear()
        with redirect_stdout(io.StringIO()) as out:installer.install(home,self.root/'pid',control,dry_run=True)
        self.assertIn('migración aditiva',out.getvalue())
        self.assertEqual(self.fingerprint(),before);self.assertEqual(self.p.calls,[])
        self.assertTrue(all(c[2] in ('is-active','show-environment','show') for c in commands))
        with redirect_stdout(io.StringIO()):installer.install(home,self.root/'pid',control)
        self.assertEqual(self.state(),dict(original,mode_override=None,startup_version=1))
        with self.engine.connect() as db:self.assertEqual(db.execute('SELECT * FROM host_actions').fetchall(),receipts)
        self.assertEqual(self.p.calls,[])
        self.assertEqual(target.read_bytes(),(installer.SOURCE/'control.py').read_bytes())
        self.assertEqual((target.parent/'control.pre-mode.py').read_text(),'# U3.2 instalado\n')
        self.assertFalse((self.engine.directory/'slots.disabled').exists())
        self.assertFalse(any('pueued.service' in c and c[2]!='show' for c in commands))

    def test_both_install_paths_reject_open_receipts_before_switch_publish_or_stop(self):
        installer,home,target,original,receipts,commands=self.setup_install()
        for status in ('applying','partial','unknown'):
            with self.engine.connect(write=True) as db:
                db.execute('INSERT OR REPLACE INTO host_actions VALUES (?,?,?)',
                    ('open-install-001','digest',json.dumps(dict(response=dict(status=status)))))
            before=self.fingerprint();self.p.calls.clear()
            for dry in (True,False):
                commands.clear()
                with self.assertRaisesRegex(ValueError,'recibos abiertos'),redirect_stdout(io.StringIO()):
                    installer.install(home,self.root/'pid',control,dry_run=dry)
                self.assertEqual(self.fingerprint(),before);self.assertEqual(self.p.calls,[])
                self.assertTrue(all(c[2] in ('is-active','show-environment','show') for c in commands))
