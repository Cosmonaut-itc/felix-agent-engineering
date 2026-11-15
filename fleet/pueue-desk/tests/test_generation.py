"""DESK-GEN: private proc/Pueue/systemd doubles; no installed host state."""
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from contextlib import redirect_stdout
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from test_slots import control, FakePueue

PREFIX = 'linux-proc-v1:'
BOOT = '12345678-1234-1234-1234-123456789abc'


class GenerationFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='desk-gen-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.proc = self.root/'proc'
        self.boot = self.proc/'sys/kernel/random/boot_id'
        self.boot.parent.mkdir(parents=True)
        self.boot.write_text(BOOT+'\n')
        self.pid = self.root/'pid'; self.pid.write_text('321\n')
        self.stat = self.proc/'321/stat'; self.stat.parent.mkdir()
        self.stat.write_text(self.record())
        self.ps = 'Sat Nov 14 08:44:13 2026 pueued'
        self.now = 1794672000.0  # one fixed UTC event, never the wall clock
        self.p = FakePueue()
        self.p.groups = {g:dict(status='Running', parallel_tasks=n) for g,n in control.desk_profile(self.now).items()}
        self.p.groups['default'] = dict(status='Running', parallel_tasks=1)
        self.engine = control.MiniControl(self.p, self.root/'authority', host='desk', clock=lambda:self.now)
        for manager in (patch.object(control, 'PROC', self.proc, create=True),
                        patch.object(control.sys, 'platform', 'linux'),
                        patch.object(control.subprocess, 'run', side_effect=self.process)):
            manager.start(); self.addCleanup(manager.stop)
        with control.locked(self.engine.directory):
            self.engine.adopt(self.pid)
        self.p.calls.clear()

    @staticmethod
    def record(pid='321', comm='pueued', status='S', ticks='956'):
        return pid+' ('+comm+') '+' '.join([status]+['0']*18+[ticks]+['0']*8)+'\n'

    def process(self, argv, **kwargs):
        if argv[0] == '/bin/ps':
            return subprocess.CompletedProcess(argv, 0, self.ps+'\n', '')
        if argv[:2] == ['/usr/bin/docker', 'info']:
            return subprocess.CompletedProcess(argv, 0, '', '')
        raise AssertionError('Unexpected subprocess: '+repr(argv))

    def state(self):
        with self.engine.connect() as db:return self.engine.state(db)

    def save(self, state):
        with self.engine.connect(write=True) as db:self.engine.store(db, state)

    def generation(self):return self.engine.generation(dict(pid_file=str(self.pid)))

    def request(self, ident='generation-slots-001'):
        v = self.engine.view()
        return dict(schema=control.HOST_SCHEMA, op='slots', action_id=ident, dry_run=False,
                    expected={k:v[k] for k in ('revision','policy_hash','generation','mode','slots')},
                    change=dict(group='otros', delta=-1))

    def fingerprint(self):
        return {str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}


class DeskGenerationTests(GenerationFixture):
    def test_wall_clock_jumps_keep_identity(self):
        original = self.generation()
        for ps in ('Sat Nov 14 08:44:12 2026 pueued', 'Sat Nov 14 08:44:14 2026 pueued',
                   'Tue Jan 01 00:00:00 2002 pueued', 'Thu Jan 01 00:00:00 2099 pueued'):
            self.ps = ps
            (self.proc/'stat').write_text('btime 123456\n')
            self.assertEqual(self.generation(), original)
        self.assertRegex(original, '^linux-proc-v1:[0-9a-f]{64}$')

    def test_boot_ticks_and_pid_each_change_identity(self):
        original = self.generation()
        self.boot.write_text('87654321-1234-1234-1234-123456789abc\n')
        self.assertNotEqual(self.generation(), original, 'boot_id')
        self.boot.write_text(BOOT+'\n')
        self.stat.write_text(self.record(ticks='957'))
        self.assertNotEqual(self.generation(), original, 'starttime')
        self.stat.write_text(self.record())
        other = self.proc/'322/stat'; other.parent.mkdir(); other.write_text(self.record(pid='322'))
        self.pid.write_text('322\n')
        self.assertNotEqual(self.generation(), original, 'pid')

    def test_pid_alone_changes_identity_with_fixed_file_signature(self):
        signature = [1, 2, 3, 4, 4]
        with patch.object(control, '_pid_signature', return_value=signature, create=True):
            original = self.generation()
            other = self.proc/'322/stat'; other.parent.mkdir(); other.write_text(self.record(pid='322'))
            self.pid.write_text('322\n')
            self.assertNotEqual(self.generation(), original)

    def test_each_pid_file_signal_changes_identity(self):
        original = self.generation()
        stat, fstat = Path.stat, os.fstat
        for field in ('st_dev','st_ino','st_mtime_ns','st_ctime_ns','st_size'):
            def changed(value):
                values = {k:getattr(value,k) for k in ('st_dev','st_ino','st_mtime_ns','st_ctime_ns','st_size')}
                values[field] += 1
                return SimpleNamespace(**values)
            with self.subTest(field=field), \
                 patch.object(Path, 'stat', lambda p,*a,**k:changed(stat(p,*a,**k)) if p == self.pid else stat(p,*a,**k)), \
                 patch.object(os, 'fstat', lambda fd:changed(fstat(fd))):
                self.assertNotEqual(self.generation(), original, field)

    def test_rewrite_with_restored_mtime_changes_identity(self):
        original = self.generation(); before = self.pid.stat()
        self.pid.write_text('321\n'); os.utime(self.pid, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertNotEqual(self.generation(), original)

    def test_guard_reopens_pressure_pause_after_lstart_jump(self):
        low = dict(io=0,cpu=0,memory=8*1024**3)
        with patch.object(control,'pressure',return_value=low):
            self.engine.guard(dict(low, cpu=40))
            self.assertEqual(self.p.groups['jale']['status'], 'Paused')
            self.ps = 'Sat Nov 14 08:44:12 2026 pueued'
            self.engine.guard(low)
        self.assertEqual(self.p.groups['jale']['status'], 'Running')
        self.assertNotIn('ambiguous', self.state()['reasons']['jale'])

    def test_expected_survives_clock_jump_and_real_change_rejects(self):
        request = self.request()
        self.ps = 'Sat Nov 14 08:44:12 2026 pueued'
        self.assertEqual(self.engine.handle(request)['status'], 'applied')
        request = self.request('generation-slots-002')
        request['change']['group'] = 'jale'
        self.stat.write_text(self.record(ticks='999'))
        self.assertTrue(self.engine.reconcile())
        self.p.calls.clear()
        self.assertNotEqual(self.engine.handle(request)['status'], 'applied')
        self.assertEqual(self.p.calls, [])

    def test_bad_proc_fails_closed(self):
        cases = [self.record(pid='322'), self.record(comm='bash'), self.record(comm='x) (pueued'),
                 self.record(status='Z'), self.record(status='X'), self.record(status='?'),
                 '321 (pueued) S 0\n', self.record(ticks='-1'), self.record(ticks='١'),
                 self.record(ticks=str(2**64)), '321 pueued S '+ '0 '*30]
        for record in cases:
            with self.subTest(record=record):
                self.stat.write_text(record)
                with self.assertRaises((OSError,ValueError)):self.generation()
        self.stat.write_text(self.record()); self.boot.write_text('invalid\n')
        with self.assertRaises((OSError,ValueError)):self.generation()
        self.boot.unlink()
        with self.assertRaises((OSError,ValueError)):self.generation()

    def test_pid_ascii_and_missing_proc_fail_closed(self):
        for text in ('0','-1','٣٢١','321 322',''):
            self.pid.write_text(text)
            with self.subTest(text=text), self.assertRaises((OSError,ValueError)):self.generation()
        self.pid.write_text('321\n'); self.stat.unlink()
        with self.assertRaises((OSError,ValueError)):self.generation()

    def test_proc_io_error_never_falls_back_to_ps(self):
        read = Path.read_text
        def denied(path,*a,**kw):
            if path == self.stat:raise PermissionError('private proc double')
            return read(path,*a,**kw)
        with patch.object(Path,'read_text',denied):
            with self.assertRaises((OSError,ValueError)):self.generation()

    def test_snapshot_race_and_pid_path_replacement_fail_closed(self):
        read = Path.read_text
        for replacement in (False, True):
            self.stat.write_text(self.record()); reads = []
            def racing(path, *args, **kwargs):
                text = read(path,*args,**kwargs)
                if path == self.stat:
                    reads.append(1)
                    if len(reads) == 1:
                        if replacement:
                            new = self.root/'new-pid'; new.write_text('321\n'); new.replace(self.pid)
                        else:self.stat.write_text(self.record(ticks='957'))
                return text
            with self.subTest(replacement=replacement), patch.object(Path,'read_text',racing):
                with self.assertRaises((OSError,ValueError)):self.generation()

    def test_pid_route_metadata_must_match_open_file(self):
        stat, fstat = Path.stat, os.fstat
        reads=[]
        def pinned(fd):
            reads.append(fd)
            return fstat(fd)
        def replaced(path,*a,**kw):
            value=stat(path,*a,**kw)
            if path != self.pid or len(reads) % 2:return value
            fields={k:getattr(value,k) for k in ('st_dev','st_ino','st_mtime_ns','st_ctime_ns','st_size')}
            fields['st_ino']+=1
            return SimpleNamespace(**fields)
        with patch.object(Path,'stat',replaced), patch.object(os,'fstat',pinned):
            with self.assertRaises((OSError,ValueError)):self.generation()

    def test_mini_golden_never_reads_proc(self):
        expected = hashlib.sha256(('321:'+str(self.pid.stat().st_mtime_ns)+':'+self.ps).encode()).hexdigest()
        mini = control.MiniControl(self.p,self.root/'mini',host='mini')
        read = Path.read_text
        def no_proc(path,*a,**kw):
            self.assertFalse(str(path).startswith(str(self.proc)), 'Mini read proc')
            return read(path,*a,**kw)
        with patch.object(Path,'read_text',no_proc):
            self.assertEqual(mini.generation(dict(pid_file=str(self.pid))),expected)

    def test_desk_formats_strict_mini_unchanged(self):
        original = self.state()
        for value in ('opaque','a'*63,'linux-proc-v1:'+'a'*63,'linux-proc-v2:'+'a'*64,'A'*64):
            self.save(dict(original,generation=value))
            with self.subTest(value=value), self.assertRaises(ValueError):self.state()
        self.save(original)
        self.assertEqual(control.mini_generation(self.pid), hashlib.sha256(
            ('321:'+str(self.pid.stat().st_mtime_ns)+':'+self.ps).encode()).hexdigest())

    def test_mini_opaque_authority_still_accepted_on_linux(self):
        p = FakePueue()
        mini = control.MiniControl(p,self.root/'mini',host='mini',generation=lambda:'opaque-token')
        with control.locked(mini.directory):mini.adopt(self.pid)
        self.assertEqual(mini.view()['generation'],'opaque-token')


class GenerationInstallTests(GenerationFixture):
    def setUp(self):
        super().setUp()
        spec = importlib.util.spec_from_file_location('generation_install',Path(control.__file__).with_name('install-desk.py'))
        self.installer = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.installer)
        self.home = self.root/'home'; self.library = self.home/'.local/lib/pao-pueue-desk'
        self.library.mkdir(parents=True); (self.library/'control.py').write_text('# old\n')
        # Move the private authority to the home expected by the installer.
        directory = self.home/'.local/state/pao/pueue-desk'; directory.parent.mkdir(parents=True)
        self.engine.directory.rename(directory); self.engine.directory=directory; self.engine.path=directory/'control.sqlite3'
        self.commands=[]; self.hook=lambda args:None
        for manager in (patch.object(control,'Pueue',return_value=self.p),
                        patch.object(control,'MiniControl',return_value=self.engine),
                        patch.object(control,'validate_configuration',return_value=dict(data=self.root,pid_file=self.pid)),
                        patch.object(control,'read_queue',return_value=dict(tasks={},groups={})),
                        patch.object(self.installer,'run',side_effect=self.systemctl)):
            manager.start(); self.addCleanup(manager.stop)
        self.legacy = control.mini_generation(self.pid)
        state = self.state(); state.update(generation=self.legacy,startup_version=1)
        self.save(state)

    def systemctl(self,*args):
        self.commands.append(args); self.hook(args)
        values=dict(MainPID='321',ActiveState='active',NeedDaemonReload='no',Restart='always',KillMode='mixed',OOMPolicy='continue',
                    ExecStartPre='/usr/bin/python3 -I '+str(self.library/'control.py')+' preparar --host desk')
        text=''.join(k+'='+v+'\n' for k,v in values.items()) if args[2]=='show' else ''
        return subprocess.CompletedProcess(args,0,text,'')

    def install(self,dry=False):
        with redirect_stdout(io.StringIO()) as out:
            self.installer.install(self.home,self.pid,control,dry_run=dry)
        return out.getvalue()

    def test_running_conversion_preserves_authority_receipts_and_is_idempotent(self):
        # Genuine receipt and override; retain their exact bytes through conversion.
        self.save(dict(self.state(),generation=self.generation()))
        self.assertEqual(self.engine.handle(self.request())['status'],'applied')
        before=self.state(); before.update(generation=self.legacy,reasons={'jale':['pressure'],'otros':['manual']})
        self.save(before)
        with self.engine.connect() as db:receipts=db.execute('SELECT * FROM host_actions').fetchall()
        self.ps='Sat Nov 14 08:44:11 2026 pueued'
        expected=self.generation(); self.p.calls.clear()
        self.install()
        self.assertRegex(self.state()['generation'],'^linux-proc-v1:[0-9a-f]{64}$')
        self.assertEqual(self.state(),dict(before,generation=expected))
        with self.engine.connect() as db:self.assertEqual(db.execute('SELECT * FROM host_actions').fetchall(),receipts)
        self.assertEqual(self.p.calls,[])
        self.install(); self.assertEqual(self.state(),dict(before,generation=expected))
        self.assertFalse((self.engine.directory/'maintenance').exists())

    def test_legacy_paused_any_group_refuses_before_effects(self):
        for group in ('jale','default','rapidas'):
            for dry in (True,False):
                with self.subTest(group=group,dry=dry):
                    self.p.groups[group]['status']='Paused'; before=self.fingerprint(); self.commands.clear()
                    with self.assertRaisesRegex(ValueError,group):self.install(dry)
                    self.assertEqual(self.fingerprint(),before)
                    self.assertTrue(all(c[2] in ('show','show-environment','is-active') for c in self.commands))
                    self.p.groups[group]['status']='Running'

    def test_seco_explains_conversion_without_writes(self):
        before=self.fingerprint()
        self.assertIn('convertir',self.install(True).lower())
        self.assertEqual(self.fingerprint(),before); self.assertEqual(self.p.calls,[])

    def test_prepared_and_intent_refuse_without_effects(self):
        original=self.state()
        for phase in ('intent','prepared'):
            startup=dict(phase=phase,previous_generation=self.legacy,statuses={},groups={},holds={},interruptions={},
                         source_hash='a'*64,target_hash='b'*64)
            self.save(dict(original,startup=startup)); before=self.fingerprint()
            for dry in (True,False):
                with self.assertRaisesRegex(ValueError,phase):self.install(dry)
                self.assertEqual(self.fingerprint(),before)

    def test_paused_after_workers_stop_keeps_maintenance(self):
        def hook(args):
            if args[2]=='stop' and args[3:] == self.installer.SERVICES:self.p.groups['default']['status']='Paused'
        self.hook=hook; before=self.state()
        with self.assertRaisesRegex(ValueError,'default'):self.install()
        self.assertEqual(self.state(),before)
        self.assertEqual((self.library/'control.py').read_text(),'# old\n')
        self.assertTrue((self.engine.directory/'maintenance').exists())
        self.assertTrue((self.engine.directory/'slots.disabled').exists())
        self.assertFalse(any(c[2]=='start' for c in self.commands))

    def test_paused_at_commit_rolls_back_and_keeps_maintenance(self):
        store=self.engine.store; before=self.state()
        def racing(db,state):
            store(db,state)
            self.p.groups['rapidas']['status']='Paused'
        with patch.object(self.engine,'store',side_effect=racing):
            with self.assertRaisesRegex(ValueError,'rapidas'):self.install()
        self.assertEqual(self.state(),before)
        self.assertTrue((self.engine.directory/'maintenance').exists())

    def test_paused_before_release_keeps_maintenance(self):
        def hook(args):
            if args[2]=='daemon-reload':self.p.groups['jale']['status']='Paused'
        self.hook=hook
        with self.assertRaisesRegex(ValueError,'jale'):self.install()
        self.assertTrue((self.engine.directory/'maintenance').exists())
        self.assertFalse(any(c[2]=='start' for c in self.commands))

    def test_generation_change_before_release_keeps_maintenance(self):
        def hook(args):
            if args[2]=='daemon-reload':self.stat.write_text(self.record(ticks='999'))
        self.hook=hook
        with self.assertRaises(ValueError):self.install()
        self.assertTrue((self.engine.directory/'maintenance').exists())
        self.assertFalse(any(c[2]=='start' for c in self.commands))

    def test_mainpid_mismatch_refuses_before_effects(self):
        self.pid.write_text('322\n'); path=self.proc/'322/stat';path.parent.mkdir();path.write_text(self.record(pid='322'))
        before=self.fingerprint()
        with self.assertRaisesRegex(ValueError,'MainPID'):self.install()
        self.assertEqual(self.fingerprint(),before)

    def test_prepared_with_current_generation_refuses_without_effects(self):
        startup=dict(phase='prepared',previous_generation=self.generation(),statuses={},groups={},holds={},
                     interruptions={},source_hash='a'*64,target_hash='b'*64)
        self.save(dict(self.state(),generation=self.generation(),startup=startup)); before=self.fingerprint()
        for dry in (True,False):
            with self.assertRaisesRegex(ValueError,'prepared'):self.install(dry)
            self.assertEqual(self.fingerprint(),before)
        self.assertFalse(any(c[2] in ('stop','start') for c in self.commands))

    def test_mainpid_change_after_workers_stop_refuses_before_publishing(self):
        stopped=[False]; properties=self.installer.unit_properties
        def hook(args):
            if args[2]=='stop' and args[3:]==self.installer.SERVICES:stopped[0]=True
        def changed():
            return dict(properties(),MainPID='999') if stopped[0] else properties()
        self.hook=hook; before=self.state()
        with patch.object(self.installer,'unit_properties',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'MainPID'):self.install()
        self.assertEqual(self.state(),before)
        self.assertEqual((self.library/'control.py').read_text(),'# old\n')
        self.assertTrue((self.engine.directory/'maintenance').exists())
        self.assertFalse(any(c[2]=='start' for c in self.commands))

    def test_unknown_group_status_refuses_legacy_conversion(self):
        self.p.groups['default']['status']='Unknown'; before=self.fingerprint()
        for dry in (True,False):
            with self.assertRaisesRegex(ValueError,'desconocida'):self.install(dry)
            self.assertEqual(self.fingerprint(),before)

    def test_new_distinct_generation_uses_normal_restart_reconciliation(self):
        # New format is never converted to hide a real restart.
        self.save(dict(self.state(),generation=PREFIX+'a'*64))
        self.p.groups['default']['status']='Paused'
        try:self.install()
        except ValueError as error:self.fail('New-format restart must retain normal reconciliation: '+str(error))
        self.assertEqual(self.state()['generation'],self.generation())
        self.assertIn('ambiguous',self.state()['reasons']['default'])

    def test_ready_startup_links_convert_only_matching_reference(self):
        original=self.state()
        for previous in (self.legacy, 'b'*64):
            startup=dict(phase='ready',previous_generation=previous,statuses={},groups={},holds={},interruptions={},
                         source_hash='a'*64,target_hash='b'*64)
            self.save(dict(original,startup=startup))
            self.install()
            expected=self.generation() if previous == self.legacy else previous
            self.assertEqual(self.state()['startup']['previous_generation'],expected)

    def test_conversion_transaction_failure_preserves_both_references(self):
        startup=dict(phase='ready',previous_generation=self.legacy,statuses={},groups={},holds={},interruptions={},
                     source_hash='a'*64,target_hash='b'*64)
        self.save(dict(self.state(),startup=startup)); before=self.state()
        with self.engine.connect(write=True) as db:
            db.execute("CREATE TRIGGER refuse_generation BEFORE UPDATE ON policy BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        import sqlite3
        with self.assertRaises(sqlite3.Error):self.install()
        self.assertEqual(self.state(),before)
        self.assertTrue((self.engine.directory/'maintenance').exists())

    def test_view_and_ensayo_never_convert_legacy_authority(self):
        before=self.fingerprint()
        view=self.engine.handle(dict(schema=control.HOST_SCHEMA,op='capabilities'))
        request=self.request(); request['dry_run']=True
        self.engine.handle(request)
        self.assertTrue(view['recovery_pending'])
        self.assertEqual(self.fingerprint(),before)
        self.assertEqual(self.p.calls,[])

    def test_preflight_plan_is_not_reused_after_workers_stop(self):
        def hook(args):
            if args[2]=='stop' and args[3:] == self.installer.SERVICES:
                self.save(dict(self.state(),revision=7,reasons={'otros':['manual']}))
        self.hook=hook
        self.install()
        self.assertEqual(self.state()['revision'],7)
        self.assertEqual(self.state()['reasons'],{'otros':['manual']})

    def test_pause_after_preflight_refuses_before_marks(self):
        preflight=self.installer.preflight
        def racing(*a,**kw):
            engine=preflight(*a,**kw); self.p.groups['default']['status']='Paused'; return engine
        before=self.fingerprint()
        with patch.object(self.installer,'preflight',side_effect=racing):
            with self.assertRaisesRegex(ValueError,'default'):self.install()
        self.assertEqual(self.fingerprint(),before)
        self.assertFalse(any(c[2]=='stop' for c in self.commands))

    def test_daemon_change_at_commit_rolls_back(self):
        store=self.engine.store; before=self.state()
        def racing(db,state):
            store(db,state); self.stat.write_text(self.record(ticks='999'))
        with patch.object(self.engine,'store',side_effect=racing):
            with self.assertRaises(ValueError):self.install()
        self.assertEqual(self.state(),before)
        self.assertTrue((self.engine.directory/'maintenance').exists())

    def test_invalid_previous_generation_is_rejected_without_writes(self):
        startup=dict(phase='ready',previous_generation='unknown',statuses={},groups={},holds={},interruptions={},
                     source_hash='a'*64,target_hash='b'*64)
        self.save(dict(self.state(),startup=startup)); before=self.fingerprint()
        with self.assertRaises(ValueError):self.install()
        self.assertEqual(self.fingerprint(),before)


if __name__ == '__main__':unittest.main()
