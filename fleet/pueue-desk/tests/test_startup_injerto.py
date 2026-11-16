"""Judge triggers and negative counterparts of the five bounded grafts; no host calls."""
import copy
import io
import json
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
from test_startup import StartupTests, InstallerStartupTests, control, task, running, done


class StartupGraftTests(StartupTests):
    setUp=StartupTests.setUp
    prepare=StartupTests.prepare
    boot=StartupTests.boot
    write=StartupTests.write
    state=StartupTests.state
    fingerprint=StartupTests.fingerprint

    def test_a_w1_cli_resolves_existing_foreign_without_changing_profile(self):
        self.queue['groups']['suites']=dict(status='Paused',parallel_tasks=3)
        self.write();self.prepare();self.boot()
        before=self.state()['desired'].copy()
        for group in ('default','suites'):
            rc=None
            with patch.object(control,'STATE_DIR',self.engine.directory),patch.object(control,'Pueue',return_value=self.p), \
                 patch.object(control,'MiniControl',return_value=self.engine),patch.object(control,'pressure',return_value=self.low), \
                 patch.object(control.sys,'argv',['control.py','reanudar',group]),redirect_stderr(io.StringIO()):
                try:control.main()
                except SystemExit as error:rc=error.code
            self.assertIsNone(rc,'CLI must accept retained existing groups')
            self.assertEqual(self.p.groups[group]['status'],'Running')
            self.assertNotIn(group,self.engine.view()['startup_holds'])
            self.assertEqual(self.state()['desired'],before)
        with patch.object(control,'pressure',return_value=self.low),patch.object(self.p,'call',return_value=''):
            with self.assertRaises(ValueError):self.engine.manual('does-not-exist',resume=True)
        self.assertNotIn(('start','-g','does-not-exist'),self.p.calls)

    def test_a_w2_external_running_releases_startup_and_second_pause_is_manual(self):
        self.prepare();self.boot();self.p.groups['jale']['status']='Running'
        state=self.state();state['startup']['holds']['jale']='motivo anterior'
        with self.engine.connect(write=True) as db:self.engine.store(db,state)
        with patch.object(control,'pressure',return_value=dict(self.low,io=30)):
            self.engine.guard(dict(self.low,io=30))
        after_running=copy.deepcopy(self.state())
        self.p.groups['jale']['status']='Paused';self.p.calls.clear()
        with patch.object(control,'pressure',return_value=self.low):self.engine.guard(self.low)
        self.assertNotIn('startup',after_running['reasons']['jale'])
        self.assertNotIn('jale',after_running['startup']['holds'])
        self.assertEqual(self.p.groups['jale']['status'],'Paused')
        self.assertNotIn(('start','-g','jale'),self.p.calls)
        self.assertEqual(self.state()['reasons']['jale'],['manual'])

    def test_external_start_failed_ownership_write_leaves_fence(self):
        self.prepare();self.boot();self.p.groups['jale']['status']='Running'
        original=self.engine.store
        def store(db,state):
            if 'startup' not in state['reasons']['jale']:raise OSError('disk failure after external start')
            return original(db,state)
        with patch.object(self.engine,'store',side_effect=store),patch.object(control,'pressure',return_value=dict(self.low,io=30)):
            with self.assertRaises(OSError):self.engine.guard(dict(self.low,io=30))
        self.assertTrue(self.engine.pause_fence('jale').exists())
        self.p.groups['jale']['status']='Paused';self.p.calls.clear()
        with patch.object(control,'pressure',return_value=self.low):self.engine.guard(self.low)
        self.assertNotIn(('start','-g','jale'),self.p.calls)

    def test_a_w5_six_automatic_waits_are_not_manual_holds(self):
        self.prepare();self.boot()
        with patch.object(control,'pressure',return_value=self.low),patch.object(control.subprocess,'run',side_effect=OSError('Docker absent')):
            self.engine.guard(self.low)
        view=self.engine.view()
        self.assertFalse(set(view['startup_holds']) & set(control.GROUP_LIMITS))
        self.assertEqual(view.get('startup_waiting'),{g:'cuotas_sensores' for g in control.GROUP_LIMITS})
        self.assertTrue(all(self.p.groups[g]['status']=='Paused' for g in control.GROUP_LIMITS))

    def test_foreign_image_during_intent_blocks_without_writes(self):
        self.queue['tasks']={'0':task(0,running())};self.write()
        with self.assertRaises(RuntimeError):
            self.prepare(boundary=lambda phase: (_ for _ in ()).throw(RuntimeError()) if phase=='intent' else None)
        changed=json.loads(self.path.read_text());changed['tasks']['0']['status']=done('Killed')
        self.path.write_text(json.dumps(changed));before=self.fingerprint()
        with self.assertRaises(ValueError):self.prepare()
        self.assertEqual(self.fingerprint(),before)

    def test_pressure_manual_and_foreign_pauses_keep_startup_docker_gate(self):
        state=self.state();state['reasons']['jale']=['pressure'];state['reasons']['otros']=['manual']
        with self.engine.connect(write=True) as db:self.engine.store(db,state)
        for g in ('jale','otros','ajeno'):self.queue['groups'][g]['status']='Paused'
        self.write();self.prepare();self.boot()
        self.assertIn('startup',self.state()['reasons']['jale'])
        self.assertIn('ajeno',self.engine.view()['startup_holds'])
        with patch.object(control,'pressure',return_value=self.low),patch.object(control.subprocess,'run',side_effect=OSError('Docker absent')):
            self.engine.guard(self.low)
            for g in ('jale','otros','ajeno','default'):
                with self.assertRaises(ValueError):self.engine.manual(g,resume=True)
        self.assertFalse(any(c[0]=='start' for c in self.p.calls))
        self.assertIn('manual',self.state()['reasons']['otros'])
        self.assertIn('ambiguous',self.state()['reasons']['ajeno'])


class InstallerGraftTests(unittest.TestCase):
    setUp=InstallerStartupTests.setUp
    fingerprint=InstallerStartupTests.fingerprint

    def test_a_w3_service_cwd_and_manager_xdg_reject_before_effects(self):
        # Production validator, fixtures supply binary versions only. CLI cwd/env
        # deliberately differ from the future pueued.service's cwd and manager.
        checkout=self.home/'checkout';checkout.mkdir()
        effective_data=self.home/'.local/share/pueue';effective_data.mkdir(parents=True)
        (effective_data/'state.json').write_bytes((self.data/'state.json').read_bytes())
        self.pid=effective_data/'pueue.pid'
        self.pid.write_text('321\n')  # MainPID of the fake pueued.service
        with self.engine.connect(write=True) as db:
            state=self.engine.state(db);state['pid_file']=str(self.pid);self.engine.store(db,state)
        manager_config=self.home/'manager-config';(manager_config/'pueue').mkdir(parents=True)
        (self.home/'pueue.yml').write_text('daemon:\n  env_vars:\n    TZ: UTC\n')
        for source in ('cwd','manager-xdg'):
            if source=='manager-xdg':
                (self.home/'pueue.yml').unlink()
                (manager_config/'pueue/pueue.yml').write_text('daemon:\n  shell_command:\n    - sh\n')
                self.manager='XDG_CONFIG_HOME='+str(manager_config)+'\n'
            for dry in (True,False):
                before=self.fingerprint();self.syscalls.clear();rejected=False
                with patch.object(control,'validate_configuration',side_effect=self.original_validator), \
                     patch.object(control.sys,'platform','linux'),patch.object(control.Path,'cwd',return_value=checkout), \
                     patch.dict(control.os.environ,{},clear=True),redirect_stdout(io.StringIO()):
                    try:self.installer.install(self.home,self.pid,control,dry_run=dry)
                    except (ValueError,OSError):rejected=True
                self.assertTrue(rejected,'Installer must validate service cwd and manager XDG')
                self.assertEqual(self.fingerprint(),before)
                self.assertTrue(all(c[2] in ('show-environment','show','is-active') for c in self.syscalls))
                self.assertEqual(self.p.calls,[])

    def test_timer_services_require_running_daemon_without_starting_it(self):
        units=Path(control.__file__).parent/'units'
        for name in ('pueue-desk-mode.service','pueue-desk-guard.service'):
            source=(units/name).read_text()
            self.assertIn('Requisite=pueued.service',source)
            self.assertNotIn('Requires=pueued.service',source)
            self.assertIn('ConditionPathExists=!@HOME@/.local/state/pao/pueue-desk/maintenance',source)

    def test_loaded_unit_mismatch_retains_maintenance_and_no_timer_restart(self):
        for key,bad in (('MainPID','999'),('ActiveState','failed'),('NeedDaemonReload','yes'),('Restart','on-failure'),('KillMode','control-group'),('OOMPolicy','stop'),('StartLimitIntervalUSec','10s'),('ExecStartPre','/other/control.py preparar')):
            with self.subTest(key=key):
                self.loaded_overrides={key:bad};self.syscalls.clear()
                with redirect_stdout(io.StringIO()),self.assertRaises(ValueError):self.installer.install(self.home,self.pid,control)
                self.assertTrue((self.directory/'maintenance').exists())
                self.assertFalse(any(c[2]=='start' for c in self.syscalls))
                self.assertFalse(any(c[2] in ('restart','stop') and 'pueued.service' in c for c in self.syscalls))

    def test_adopted_missing_or_corrupt_queue_rejected_without_effects(self):
        path=self.data/'state.json'
        for missing in (True,False):
            if missing:path.unlink()
            else:path.write_text('{')
            before=self.fingerprint()
            for dry in (True,False):
                self.syscalls.clear()
                with self.assertRaises((ValueError,OSError)):self.installer.install(self.home,self.pid,control,dry_run=dry)
                self.assertEqual(self.fingerprint(),before)
                self.assertTrue(all(c[2] in ('show-environment','is-active','show') for c in self.syscalls))


del StartupTests, InstallerStartupTests

if __name__=='__main__':unittest.main()
