"""R1 regressions: real controller/SQLite, exclusively fake processes and launchd."""
import copy
import io
import json
from pathlib import Path
import plistlib
import subprocess
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
import test_handoff as handoff
from test_handoff import Harness, Crash, mini, control, LOW, task, running, done


class R1Tests(unittest.TestCase):
    setUp = handoff.HandoffTests.setUp
    no_real_process = staticmethod(handoff.HandoffTests.no_real_process)

    def abort(self, h, target):
        if target == 'withdraw':h.m.change('install');h.finish()
        h.p.tasks = {'0':task(0,running(),'default')};h.save_queue()
        h.m.change(target,wait=10);h.now+=11
        self.assertTrue(h.m._recover(h.config))
        self.assertEqual(h.policy()['mini_handoff']['phase'],'aborted')

    def test_aborted_withdraw_restarts_prepared_owner_and_login(self):
        h=self.h;self.abort(h,'withdraw')
        self.assertEqual(h.owner,mini.LABEL)
        self.assertEqual(h.policy()['mini_handoff'].get('retained_owner'),'prepared')
        h.p.online=False;h.owner=None;h.p.calls.clear();h.trace.clear()
        h.boot()
        self.assertTrue(h.p.online,'aborted withdrawal must allow its retained wrapper to exec')
        self.assertEqual(h.owner,mini.LABEL)
        self.assertIn('start:prepared',h.trace)
        self.assertTrue(all(g['status']=='Paused' for g in h.p.groups.values()))
        self.assertFalse(any(c[0]=='start' for c in h.p.calls),'preparer must not admit')
        h.p.groups['default']['parallel_tasks']=8
        with patch.object(control,'pressure_mini',return_value=dict(load=4,memory=1)):
            self.assertTrue(h.m._recover(h.config));h.engine.guard(dict(load=4,memory=1))
        self.assertEqual(h.p.groups['default']['parallel_tasks'],1)
        self.assertEqual(h.p.groups['default']['status'],'Paused')
        h.login();h.recover()
        self.assertEqual(h.owner,mini.LABEL);self.assertTrue(h.p.online)
        self.assertTrue(h.disabled[mini.BREW]);self.assertNotIn(mini.BREW,h.loaded)
        self.assertEqual(h.p.groups['suites']['status'],'Paused')
        self.assertEqual(h.p.groups['default']['status'],'Running')
        self.assertEqual(h.p.tasks['0']['status']['Done']['result'],'Killed')
        print('BOT-1: aborted withdrawal -> prepared exec; recovery/login owner=LABEL online=True; Brew disabled')

    def test_install_after_aborted_withdraw_retains_source_enable_and_login(self):
        h=self.h;self.abort(h,'withdraw')
        h.p.tasks['0']['status']=done();h.save_queue()
        h.m.change('install');h.finish();h.login();h.finish()
        self.assertEqual(h.owner,mini.LABEL);self.assertTrue(h.p.online)
        self.assertTrue(h.disabled[mini.BREW]);self.assertFalse(h.disabled[mini.LABEL])
        self.assertEqual(h.p.groups['suites']['status'],'Paused')

    def test_legacy_aborted_withdraw_is_repaired_by_existing_job(self):
        h=self.h;self.abort(h,'withdraw')
        state=h.policy();state['mini_handoff'].pop('retained_owner',None)
        h.m.save(state,'test:legacy-abort')
        h.login()  # wrapper cannot guess ownership before the job repairs it
        with control.locked(h.directory):self.assertTrue(h.m._recover(h.config))
        self.assertEqual(h.policy()['mini_handoff'].get('retained_owner'),'prepared')
        h.recover()
        self.assertEqual(h.owner,mini.LABEL);self.assertTrue(h.p.online)

    def test_retained_owner_is_validated_and_never_authorizes_brew_or_inflight_withdraw(self):
        h=self.h;h.m.change('install');h.finish()
        for target,phase,owner in (('withdraw','switch','prepared'),('install','aborted','homebrew')):
            state=h.policy();state['mini_handoff'].update(target=target,phase=phase,retained_owner=owner)
            h.m.save(state,'test:closed');h.p.online=False
            with self.assertRaises(ValueError):h.m.start(execute=lambda *_:self.fail('unauthorized exec'))
        state=h.policy();state['mini_handoff'].update(phase='done',target='install',retained_owner='unknown')
        h.m.save(state,'test:invalid-owner')
        with self.assertRaises(ValueError):h.m._recover(h.config)

    def test_cancel_between_stop_record_and_final_drain_read_has_no_restart_reason(self):
        for target in ('install','withdraw'):
            with self.subTest(target=target):
                h=Harness(self)
                if target=='withdraw':h.m.change('install');h.finish()
                h.p.tasks={'0':task(0,running(),'default')};h.save_queue()
                record=h.engine.record_stop
                def cancel():
                    record();h.p.tasks['0']['status']=done('Killed');h.save_queue()
                    self.assertFalse(any(c[0]=='bootout' for c in h.calls))
                h.calls.clear();h.engine.record_stop=cancel
                h.m.change(target,wait=600);h.finish()
                self.assertEqual(h.policy()['mini_handoff']['phase'],'done' if target=='install' else 'withdrawn')
                self.assertIsNone(h.policy()['startup']['interruptions'].get('0'))
                self.assertEqual(h.p.tasks['0']['status']['Done']['result'],'Killed')
                print('R1-sol-2:',target,'after_handoff: done; cancelled_task_reason: None; task_status: Killed')

    def test_maintenance_after_either_abort_returns_same_owner(self):
        for target in ('install','withdraw'):
            h=Harness(self);self.abort(h,target);owner=h.owner
            h.m.maintenance();self.assertFalse(h.p.online)
            h.m.maintenance(resume=True);h.finish();h.login();h.recover()
            self.assertEqual(h.owner,owner,'continuar must not complete an aborted handoff')
            self.assertTrue(h.p.online)
            self.assertEqual(h.policy()['mini_handoff']['target'],'install' if owner==mini.LABEL else 'withdraw')
            self.assertEqual(h.p.groups['suites']['status'],'Paused')
            self.assertEqual(h.loaded & {mini.BREW,mini.LABEL},{owner})
            print('R1-opus-2:',target,'maintenance/continue owner retained')

    def test_homebrew_404_exact_plist_is_accepted_and_other_arguments_rejected(self):
        h=self.h
        value=dict(Label=mini.BREW,ProgramArguments=['/opt/homebrew/opt/pueue/bin/pueued','--verbose'],
                   WorkingDirectory='/opt/homebrew/var',StandardOutPath='/opt/homebrew/var/log/pueued.log',
                   StandardErrorPath='/opt/homebrew/var/log/pueued.log',RunAtLoad=True)
        path=h.agents/(mini.BREW+'.plist')
        # No Homebrew is installed on the laptop: pin the same executable path
        # in the fake rather than relying on a real Homebrew symlink.
        with patch.object(mini,'DAEMON',value['ProgramArguments'][0]):
            path.write_bytes(plistlib.dumps(value));before=h.fingerprint()
            try:
                with redirect_stdout(io.StringIO()):h.m.change('install',dry=True)
            except ValueError as error:self.fail('stock Homebrew plist rejected: '+str(error))
            self.assertEqual(h.fingerprint(),before)
            for change in (dict(ProgramArguments=['/other/pueued','--verbose']),
                           dict(ProgramArguments=[mini.DAEMON,'--config','other']),
                           dict(ProgramArguments=[mini.DAEMON,'--help']),
                           dict(ProgramArguments=[mini.DAEMON,'--verbose','--verbose']),
                           dict(EnvironmentVariables={'PUEUE_CONFIG_PATH':'other'}),
                           dict(EnvironmentVariables={}),dict(WorkingDirectory='/other')):
                path.write_bytes(plistlib.dumps(dict(value,**change)));before=h.fingerprint()
                with self.assertRaises(ValueError):h.m.change('install',dry=True)
                self.assertEqual(h.fingerprint(),before)
        print('R1-opus-3: exact Homebrew 4.0.4 plist accepted; altered binary/argv/env rejected')

    def test_cwd_config_selection_must_match_source_and_destination(self):
        h=self.h
        value=plistlib.loads((h.agents/(mini.BREW+'.plist')).read_bytes())
        value['WorkingDirectory']='/opt/homebrew/var'
        (h.agents/(mini.BREW+'.plist')).write_bytes(plistlib.dumps(value))
        (h.home/'pueue.yml').write_text('{}')
        before=h.fingerprint()
        with self.assertRaisesRegex(ValueError,'configuración.*distint'):
            h.m.change('install',dry=True)
        self.assertEqual(h.fingerprint(),before)
        # The standard macOS config wins over BOTH working directories.
        standard=h.home/'Library/Application Support/pueue/pueue.yml'
        standard.parent.mkdir(parents=True);standard.write_text('{}')
        try:
            with redirect_stdout(io.StringIO()):h.m.change('install',dry=True)
        except ValueError as error:self.fail('same standard configuration rejected: '+str(error))
        standard.write_text('{"shared":{"pid_path":"/other/pid"}}')
        with patch.object(control.sys,'platform','darwin'), \
             patch.object(control.subprocess,'check_output',side_effect=lambda argv,**kw:'pueue 4.0.4' if argv[0]=='client' else 'pueued 4.0.4'):
            with self.assertRaisesRegex(ValueError,'pid_path'):
                control.validate_configuration(home=h.home,cwd=h.home,env={},pueue='client',daemon='daemon')

    def test_blocked_start_records_safe_reason_and_bounded_log(self):
        h=self.h;h.m.change('install');h.finish();h.p.online=False;h.owner=None
        (h.data/'state.json').write_text('{"invalid":')
        with self.assertRaises(ValueError):h.m.start(execute=lambda *_:self.fail('corrupt queue exec'))
        log=h.directory/'arranque.log'
        self.assertTrue(log.is_file(),'blocked start must retain a diagnostic')
        record=json.loads(log.read_text().splitlines()[-1])
        self.assertEqual(record['motivo'],'Cola JSON ilegible')
        self.assertEqual(record.get('detalle_json'),'Expecting value')
        self.assertEqual(log.stat().st_mode & 0o777,0o600)
        message='Cola cambió durante preparación; conservar archivos'
        with patch.object(control,'prepare_start',side_effect=ValueError(message)):
            with self.assertRaises(ValueError):h.m.start(execute=lambda *_:None)
        self.assertEqual(json.loads(log.read_text().splitlines()[-1])['motivo'],message)
        secret='cmd=SECRET envs=TOKEN'
        for error in (ValueError(secret),json.JSONDecodeError(secret,'',0)):
            with patch.object(control,'prepare_start',side_effect=error):
                with self.assertRaises(ValueError):h.m.start(execute=lambda *_:None)
            self.assertNotIn(secret,log.read_text())
        with patch.object(h.m,'loaded',return_value=None):
            for _ in range(1200):
                with self.assertRaises(ValueError):h.m.start(execute=lambda *_:None)
        self.assertEqual((h.directory/'arranque.log.lock').read_bytes(),b'')
        files=[p for p in h.directory.glob('arranque.log*') if p.name != 'arranque.log.lock']
        self.assertEqual({p.name for p in files},{'arranque.log','arranque.log.1'})
        self.assertTrue(all(p.stat().st_size<=65536 for p in files))
        self.assertTrue(all('SECRET' not in p.read_text() and 'TOKEN' not in p.read_text() for p in files))
        print('R1-opus-4: reason recorded; mode 0600; two logs <=64 KiB; arbitrary exception content excluded')

    def test_foreign_handoff_pause_is_resolvable_on_completion_and_abort(self):
        for target,aborted in (('install',False),('withdraw',False),('install',True),('withdraw',True)):
            with self.subTest(target=target,aborted=aborted):
                h=Harness(self)
                if target=='withdraw':h.m.change('install');h.finish()
                # A foreign group can have been explicitly resolved before this handoff.
                state=h.policy();state['reasons']['ajeno']=[];h.m.save(state,'test:foreign-open')
                h.p.groups['ajeno']['status']='Running';h.save_queue()
                if aborted:
                    h.p.tasks={'0':task(0,running(),'default')};h.save_queue()
                    h.m.change(target,wait=10);h.now+=11;h.recover()
                else:h.m.change(target);h.finish()
                self.assertEqual(h.p.groups['ajeno']['status'],'Paused')
                reasons=set(h.policy()['reasons']['ajeno'])
                self.assertFalse(reasons & {'handoff','pressure'},'foreign pause has no guard to release pressure')
                self.assertIn('ambiguous',reasons)
                self.assertNotIn('ajeno',h.policy()['desired'])
                with patch.object(control,'pressure_mini',return_value=dict(load=4,memory=1)):
                    with self.assertRaises(ValueError):h.engine.manual('ajeno',resume=True)
                with patch.object(control,'pressure_mini',return_value=LOW):h.engine.manual('ajeno',resume=True)
                self.assertEqual(h.p.groups['ajeno']['status'],'Running','documented reanudar must resolve')
                print('BOT-2:',target,'aborted='+str(aborted),'foreign pause resolvable through reanudar')


if __name__=='__main__':unittest.main()
