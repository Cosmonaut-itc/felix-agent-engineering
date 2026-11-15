"""DESK-GEN: desk daemon identity without the wall clock. Fake /proc, ps, Pueue and systemd only."""
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch
from test_slots import control
import test_desk_slots
from test_desk_slots import DeskFixture

NEW = 'linux-proc-v1:'
LEGACY = 'ab' * 32               # lstart hash stored before DESK-GEN
CURRENT = NEW + 'c' * 64         # stable generation of the live daemon
BOOT = '0f8b3a2e-1c4d-4e5f-9a6b-7c8d9e0f1a2b'
HIGH = dict(io=90, cpu=90, memory=8 * 1024**3)
LOW = dict(io=0, cpu=0, memory=8 * 1024**3)


def stat_line(pid, start=956, state='S', comm='pueued', fields=None):
    rest = ('1 {p} {p} 0 -1 4194560 1234 0 0 0 10 5 0 0 20 0 3 0 {s} 123456789 2000 18446744073709551615 '
            '1 1 0 0 0 0 0 0 0 0 0 0 0 17 3 0 0 0 0 0').format(p=pid, s=start)
    if fields is not None:
        rest = ' '.join(rest.split()[:fields])
    return '{} ({}) {} {}\n'.format(pid, comm, state, rest)


class FakeDesk:
    """A /proc tree and a ps whose lstart follows the adjustable wall clock, as in WSL."""
    def __init__(self, root):
        self.proc = root / 'proc'
        self.pid_file = root / 'pueue.pid'
        self.lstart = 'Sat Nov 14 08:44:13 2026'
        self.ps = []
        (self.proc / 'sys/kernel/random').mkdir(parents=True)
        (self.proc / 'sys/kernel/random/boot_id').write_text(BOOT + '\n')
        self.process(362)
        self.pid_file.write_text('362')

    def process(self, pid, **kw):
        (self.proc / str(pid)).mkdir(exist_ok=True)
        (self.proc / str(pid) / 'stat').write_text(stat_line(pid, **kw))

    def run(self, argv, **kwargs):
        if argv[:1] == ['/bin/ps']:
            self.ps.append(argv)
            return subprocess.CompletedProcess(argv, 0, self.lstart + ' /usr/bin/pueued\n', '')
        if argv == ['/usr/bin/docker', 'info']:
            return subprocess.CompletedProcess(argv, 0, '', '')
        raise AssertionError('No real binaries: ' + str(argv))


class DeskWorld(DeskFixture):
    """Desk engine with the real identity reader over a fake /proc (no injected generation)."""
    def setUp(self):
        super().setUp()
        self.desk = FakeDesk(self.root)
        for manager in (patch.object(control, 'PROC', self.desk.proc, create=True),
                        patch.object(control.subprocess, 'run', side_effect=self.desk.run)):
            manager.start(); self.addCleanup(manager.stop)
        self.real = control.MiniControl(self.p, self.root / 'real', clock=lambda: self.now, host='desk')
        with control.locked(self.real.directory):
            self.real.adopt(self.desk.pid_file)

    def generation(self):
        return self.real.generation(dict(pid_file=str(self.desk.pid_file)))

    def real_state(self):
        with self.real.connect() as db:
            return self.real.state(db)


class ClockShiftTests(DeskWorld):
    """Astra §5, 42–43 and the panel: an lstart shift with the same process is not a restart."""
    def test_lstart_shift_with_same_process_keeps_generation(self):
        first = self.generation()
        for lstart in ('Sat Nov 14 08:44:12 2026', 'Sat Nov 14 08:44:14 2026', 'Fri Nov 14 08:44:13 2031',
                       'Sun Jan  1 00:00:00 2017'):
            with self.subTest(lstart=lstart):
                self.desk.lstart = lstart
                self.assertEqual(self.generation(), first)
        self.assertRegex(first, r'^linux-proc-v1:[0-9a-f]{64}$')
        self.assertEqual(self.real_state()['generation'], first)
        self.assertEqual(self.desk.ps, [], 'El desk no consulta ps ni lstart')

    def test_pressure_pause_reopens_after_lstart_shift_without_ambiguous(self):
        self.real.guard(HIGH)
        self.assertEqual(self.p.groups['jale']['status'], 'Paused')
        self.assertEqual(self.real_state()['reasons']['jale'], ['pressure'])
        self.desk.lstart = 'Sat Nov 14 08:44:12 2026'  # 23:24:40Z on 2026-11-14, same pid 362
        with patch.object(control, 'pressure', return_value=LOW):
            self.real.guard(LOW)
        reasons = self.real_state()['reasons']
        self.assertEqual(self.p.groups['jale']['status'], 'Running')
        self.assertFalse(any('ambiguous' in r for g, r in reasons.items() if g in control.GROUP_LIMITS), reasons)
        self.assertFalse(self.real.view()['recovery_pending'])

    def test_panel_expected_generation_survives_lstart_shift(self):
        v = self.real.view()
        request = dict(schema=control.HOST_SCHEMA, op='slots', action_id='desk-gen-00001', dry_run=False,
                       expected={k: v[k] for k in ('revision', 'policy_hash', 'generation', 'mode', 'slots')},
                       change=dict(group='jale', delta=-1))
        self.desk.lstart = 'Sat Nov 14 08:44:11 2026'
        self.assertEqual(self.real.handle(request)['status'], 'applied')

    def test_real_restart_still_marks_paused_groups_ambiguous(self):
        self.real.guard(HIGH)
        self.desk.process(363); self.desk.pid_file.write_text('363')
        self.real.reconcile()
        self.assertIn('ambiguous', self.real_state()['reasons']['jale'])
        self.assertEqual(self.p.groups['jale']['status'], 'Paused')


class IdentityTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory(prefix='desk-gen-opus-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.desk = FakeDesk(Path(self.tmp.name))
        for manager in (patch.object(control, 'PROC', self.desk.proc),
                        patch.object(control.subprocess, 'run', side_effect=self.desk.run)):
            manager.start(); self.addCleanup(manager.stop)

    def generation(self):
        return control.desk_generation(self.desk.pid_file)

    def perturbed(self, field):
        """Stat results of the pid file with exactly one signature field changed."""
        real_stat, real_fstat = os.stat, os.fstat
        target = real_stat(self.desk.pid_file).st_ino
        def shift(st):
            values = {k: getattr(st, k) for k in ('st_mode', 'st_dev', 'st_ino', 'st_mtime_ns', 'st_ctime_ns', 'st_size')}
            values[field] += 1
            return SimpleNamespace(**values)
        def stat(path, *a, **k):
            st = real_stat(path, *a, **k)
            return shift(st) if st.st_ino == target else st
        def fstat(fd):
            st = real_fstat(fd)
            return shift(st) if st.st_ino == target else st
        return patch.multiple(control.os, stat=stat, fstat=fstat)

    def test_every_restart_signal_changes_generation(self):
        base = self.generation()
        self.assertEqual(self.generation(), base)
        def changed(label):
            with self.subTest(signal=label):
                self.assertNotEqual(self.generation(), base)
        (self.desk.proc / 'sys/kernel/random/boot_id').write_text('1f8b3a2e-1c4d-4e5f-9a6b-7c8d9e0f1a2b\n')
        changed('boot_id: same pid 362 and starttime after a VM reboot')
        (self.desk.proc / 'sys/kernel/random/boot_id').write_text(BOOT + '\n')
        self.desk.process(362, start=957)
        changed('starttime: distro restarted inside the same VM')
        self.desk.process(362)
        self.assertEqual(self.generation(), base)
        stamp = self.desk.pid_file.stat()
        self.desk.process(363); self.desk.pid_file.write_text('363')
        os.utime(self.desk.pid_file, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        changed('pid')
        self.desk.pid_file.write_text('362')
        os.utime(self.desk.pid_file, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        changed('pid file rewritten with its mtime restored (ctime)')
        base = self.generation()
        for field in ('st_dev', 'st_ino', 'st_mtime_ns', 'st_ctime_ns', 'st_size'):
            with self.perturbed(field):
                changed('pid file ' + field)
        self.assertEqual(self.generation(), base)
        self.assertEqual(self.desk.ps, [])

    def test_unreadable_or_foreign_identity_fails_closed(self):
        boot = self.desk.proc / 'sys/kernel/random/boot_id'
        stat = self.desk.proc / '362/stat'
        cases = {
            'boot_id absent': lambda: boot.unlink(),
            'boot_id malformed': lambda: boot.write_text(BOOT.upper() + '\n'),
            'process absent': lambda: stat.unlink(),
            'other command': lambda: stat.write_text(stat_line(362, comm='bash')),
            'parenthesis in comm': lambda: stat.write_text(stat_line(362, comm='x) (pueued')),
            'other pid in stat': lambda: stat.write_text(stat_line(363)),
            'zombie': lambda: stat.write_text(stat_line(362, state='Z')),
            'dead': lambda: stat.write_text(stat_line(362, state='X')),
            'unknown state': lambda: stat.write_text(stat_line(362, state='Q')),
            'short stat': lambda: stat.write_text(stat_line(362, fields=17)),
            'starttime not numeric': lambda: stat.write_text(stat_line(362, start='9x6')),
            'starttime over u64': lambda: stat.write_text(stat_line(362, start=2**64)),
            'pid text': lambda: self.desk.pid_file.write_text('abc'),
            'pid zero': lambda: self.desk.pid_file.write_text('0'),
            'pid leading zero': lambda: self.desk.pid_file.write_text('0362'),
            'pid unicode digit': lambda: self.desk.pid_file.write_text('36²'),
            'pid file absent': lambda: self.desk.pid_file.unlink(),
        }
        for label, damage in cases.items():
            with self.subTest(case=label):
                import tempfile
                with tempfile.TemporaryDirectory(prefix='desk-gen-opus-', dir='/tmp') as folder:
                    self.desk = FakeDesk(Path(folder))
                    boot = self.desk.proc / 'sys/kernel/random/boot_id'
                    stat = self.desk.proc / '362/stat'
                    with patch.object(control, 'PROC', self.desk.proc), \
                         patch.object(control.subprocess, 'run', side_effect=self.desk.run):
                        self.generation()  # nominal reading works
                        damage()
                        with self.assertRaises((OSError, ValueError)):
                            self.generation()
                    self.assertEqual(self.desk.ps, [], 'Sin fallback a ps')

    def test_change_between_snapshots_or_during_read_fails_closed(self):
        original = control._desk_snapshot
        def restart_after_first(*args):
            value = original(*args)
            self.desk.process(362, start=957)
            return value
        with patch.object(control, '_desk_snapshot', side_effect=restart_after_first):
            with self.assertRaisesRegex(ValueError, 'cambió durante la lectura'):
                self.generation()
        self.desk.process(362)
        real_stat, real_fstat = os.stat, os.fstat
        replaced = lambda st: SimpleNamespace(st_mode=st.st_mode, st_dev=st.st_dev, st_ino=st.st_ino + 1,
                                              st_mtime_ns=st.st_mtime_ns, st_ctime_ns=st.st_ctime_ns, st_size=st.st_size)
        target = real_stat(self.desk.pid_file).st_ino
        def stat(path, *a, **k):
            st = real_stat(path, *a, **k)
            return replaced(st) if st.st_ino == target else st
        with patch.object(control.os, 'stat', stat):
            with self.assertRaisesRegex(ValueError, 'pid file cambió'):
                self.generation()
        calls = []
        def fstat(fd):
            st = real_fstat(fd); calls.append(fd)
            return replaced(st) if len(calls) % 2 == 0 else st
        with patch.object(control.os, 'fstat', fstat):
            with self.assertRaisesRegex(ValueError, 'pid file cambió'):
                self.generation()
        self.generation()

    def test_real_proc_requires_linux(self):
        with patch.object(control, 'PROC', Path('/proc')), patch.object(control.sys, 'platform', 'darwin'):
            with self.assertRaisesRegex(ValueError, 'Linux'):
                self.generation()

    def test_mini_generation_value_unchanged_and_never_reads_proc(self):
        pid_file = self.desk.pid_file
        pid_file.write_text('999\n')
        mtime = pid_file.stat().st_mtime_ns
        def ps(argv, **kwargs):
            self.assertEqual(argv, ['/bin/ps', '-p', '999', '-o', 'lstart=', '-o', 'comm='])
            return subprocess.CompletedProcess(argv, 0, 'Fri Nov 13 11:00:00 2026 /opt/homebrew/bin/pueued\n', '')
        golden = hashlib.sha256(('999:' + str(mtime) + ':Fri Nov 13 11:00:00 2026 /opt/homebrew/bin/pueued').encode()).hexdigest()
        engine = control.MiniControl(None, self.desk.proc.parent / 'mini', host='mini')
        for platform in ('darwin', 'linux'):
            with self.subTest(platform=platform), patch.object(control.sys, 'platform', platform), \
                 patch.object(control, 'PROC', self.desk.proc.parent / 'absent'), \
                 patch.object(control.subprocess, 'run', side_effect=ps):
                try:
                    value = engine.generation(dict(pid_file=str(pid_file)))
                except (OSError, ValueError) as error:
                    self.fail('La Mini no debe leer /proc: ' + repr(error))
                self.assertEqual(value, golden)


class StoredGenerationTests(DeskFixture):
    def stored(self, value, startup=False):
        state = self.state()
        state['generation'] = value
        if startup:
            state['generation'] = CURRENT
            state['startup_version'] = 1
            state['startup'] = dict(phase='ready', source_hash='0' * 64, target_hash='0' * 64, statuses={}, groups={},
                                    holds={}, interruptions={}, previous_generation=value)
        with self.engine.connect(write=True) as db:
            db.execute('UPDATE policy SET body=? WHERE id=1', (control.canonical(state),))
        with self.engine.connect() as db:
            return self.engine.state(db)

    def test_desk_accepts_only_legacy_or_stable_format(self):
        for value in (LEGACY, CURRENT):
            for startup in (False, True):
                with self.subTest(value=value, startup=startup):
                    self.stored(value, startup)
        for value in ('desk-1', 'daemon-1', NEW + 'c' * 63, NEW + 'C' * 64, 'linux-proc-v2:' + 'c' * 64,
                      NEW + 'c' * 64 + '\n', 'AB' * 32, 'ab' * 31):
            for startup in (False, True):
                with self.subTest(value=value, startup=startup), self.assertRaises(ValueError):
                    self.stored(value, startup)

    def test_mini_keeps_its_opaque_generation(self):
        from test_slots import FakePueue
        engine = control.MiniControl(FakePueue(), self.root / 'mini', generation=lambda: 'daemon-1')
        with control.locked(engine.directory):
            engine.adopt(self.root / 'pid')
        with engine.connect() as db:
            self.assertEqual(engine.state(db)['generation'], 'daemon-1')


class ConversionInstallTests(DeskFixture):
    """Felix, 2026-11-15: convert the legacy value only while no desk group is Paused."""
    # Borrowed helpers; importing the class itself would rerun its tests here.
    systemctl = test_desk_slots.DeskInstallTests.systemctl
    enter_context = test_desk_slots.DeskInstallTests.enter_context
    migration_fixture = test_desk_slots.DeskInstallTests.migration_fixture

    def setUp(self):
        super().setUp()
        (self.root / 'pid').write_text('321\n')
        self.enter_context(patch.object(control, 'validate_configuration', return_value=dict(data=self.root, pid_file=self.root / 'pid')))
        self.enter_context(patch.object(control, 'read_queue', return_value=dict(tasks={}, groups={})))
        for g in ('default', 'ajeno'):
            self.p.groups[g]['status'] = 'Running'

    def install_fixture(self, stored=LEGACY, startup=None):
        spec = importlib.util.spec_from_file_location('install_desk_gen', Path(control.__file__).with_name('install-desk.py'))
        installer = importlib.util.module_from_spec(spec); spec.loader.exec_module(installer)
        home = self.root / 'gen-home'
        library = home / '.local/lib/pao-pueue-desk'; library.mkdir(parents=True)
        target = library / 'control.py'; target.write_text('# U3.4 instalado\n')
        directory = home / '.local/state/pao/pueue-desk'
        Original = control.MiniControl
        factory = lambda p, d, **kw: Original(p, d, clock=lambda: self.now, generation=lambda: self.gen, **kw)
        engine = factory(self.p, directory, host='desk')
        self.gen = stored
        with control.locked(directory):
            engine.adopt(self.root / 'pid')
        v = engine.view()
        receipt = engine.handle(dict(schema=control.HOST_SCHEMA, op='slots', action_id='before-gen-001', dry_run=False,
                                     expected={k: v[k] for k in ('revision', 'policy_hash', 'generation', 'mode', 'slots')},
                                     change=dict(group='jale', delta=-1)))
        self.assertEqual(receipt['status'], 'applied')
        with engine.connect(write=True) as db:
            state = engine.state(db)
            state['startup_version'] = 1
            state['reasons']['otros'] = ['manual']  # a Running group may keep manual ownership
            if startup is not None:
                state['startup'] = dict(phase=startup, source_hash='0' * 64, target_hash='0' * 64, statuses={},
                                        groups={}, holds={}, interruptions={}, previous_generation=stored)
            engine.store(db, state)
        self.p.calls.clear()
        self.commands, self.hooks = [], {}
        def run(*args):
            hook = self.hooks.get(args[2])
            if hook:
                hook(args)
            return self.systemctl(home, self.commands, *args)
        for manager in (patch.object(control, 'Pueue', return_value=self.p),
                        patch.object(control, 'MiniControl', side_effect=factory),
                        patch.object(control, 'desk_generation', side_effect=lambda p: self.gen, create=True),
                        patch.object(control, 'mini_generation', side_effect=lambda p: self.gen),
                        patch.object(installer, 'run', side_effect=run)):
            self.enter_context(manager)
        self.gen = CURRENT
        return installer, home, engine, target

    def authority(self, engine):
        with engine.connect() as db:
            return (json.loads(db.execute('SELECT body FROM policy WHERE id=1').fetchone()[0]),
                    db.execute('SELECT * FROM host_actions').fetchall())

    def install(self, installer, home, dry_run=False):
        with redirect_stdout(io.StringIO()) as out:
            installer.install(home, self.root / 'pid', control, dry_run=dry_run)
        return out.getvalue()

    def assert_no_effects(self, before):
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual(self.p.calls, [])
        self.assertTrue(all(c[2] in ('is-active', 'show-environment', 'show') for c in self.commands), self.commands)

    def test_every_group_running_converts_and_preserves_reasons_revision_desire_receipts(self):
        installer, home, engine, target = self.install_fixture()
        (before, receipts) = self.authority(engine)
        self.install(installer, home)
        after, kept = self.authority(engine)
        self.assertEqual(after['generation'], CURRENT)
        self.assertEqual(after, dict(before, generation=CURRENT))
        self.assertEqual(after['reasons']['otros'], ['manual'])
        self.assertEqual((after['revision'], after['desired'], after['overrides']), (before['revision'], before['desired'], before['overrides']))
        self.assertEqual(kept, receipts)
        self.assertFalse(any('ambiguous' in r for r in after['reasons'].values()))
        self.assertFalse((engine.directory / 'maintenance').exists())
        self.assertFalse((engine.directory / 'slots.disabled').exists())
        self.assertFalse(any(c[0] in ('pause', 'start') for c in self.p.calls))
        self.assertFalse(engine.view()['recovery_pending'])
        self.install(installer, home)  # idempotent once converted
        self.assertEqual(self.authority(engine)[0], after)

    def test_seco_reports_conversion_or_stop_without_writes(self):
        installer, home, engine, target = self.install_fixture()
        before = self.fingerprint()
        self.assertIn('convertiría', self.install(installer, home, dry_run=True))
        self.assert_no_effects(before)
        self.p.groups['default']['status'] = 'Paused'
        self.p.groups['rapidas']['status'] = 'Paused'
        with redirect_stdout(io.StringIO()) as out, self.assertRaisesRegex(ValueError, 'default, rapidas'):
            installer.install(home, self.root / 'pid', control, dry_run=True)
        self.assertIn('se detendría', out.getvalue())
        self.assertIn('default, rapidas', out.getvalue())
        self.assert_no_effects(before)

    def assert_paused_stops_before_effects(self, *paused):
        installer, home, engine, target = self.install_fixture()
        for g in paused:
            self.p.groups[g]['status'] = 'Paused'
        before = self.fingerprint()
        for dry in (True, False):
            self.commands.clear()
            with self.assertRaisesRegex(ValueError, 'grupos Paused: ' + ', '.join(sorted(paused))), \
                 redirect_stdout(io.StringIO()):
                installer.install(home, self.root / 'pid', control, dry_run=dry)
            self.assert_no_effects(before)
        self.assertEqual(self.authority(engine)[0]['generation'], LEGACY)

    def test_paused_default_stops_before_effects(self):
        self.assert_paused_stops_before_effects('default')

    def test_paused_rapidas_stops_before_effects(self):
        self.assert_paused_stops_before_effects('rapidas')

    def test_paused_managed_and_foreign_groups_stop_before_effects(self):
        self.assert_paused_stops_before_effects('jale', 'ajeno')

    def assert_paused_between_checks_keeps_maintenance(self, when):
        installer, home, engine, target = self.install_fixture()
        def pause(args):
            if when == 'daemon-reload' or args[3:] == installer.SERVICES:
                self.p.groups['otros']['status'] = 'Paused'
        self.hooks[when] = pause
        with self.assertRaisesRegex(ValueError, 'grupos Paused: otros'), redirect_stdout(io.StringIO()):
            installer.install(home, self.root / 'pid', control)
        self.assertTrue((engine.directory / 'maintenance').exists())
        self.assertTrue((engine.directory / 'slots.disabled').exists())
        self.assertFalse(any(c[2] == 'start' for c in self.commands))
        self.assertFalse(any(c[0] in ('pause', 'start') for c in self.p.calls))
        state = self.authority(engine)[0]
        self.assertNotIn('ambiguous', state['reasons'].get('otros', []))
        return target, state

    def test_group_paused_after_stopping_guard_stops_before_publishing(self):
        target, state = self.assert_paused_between_checks_keeps_maintenance('stop')
        self.assertEqual(target.read_text(), '# U3.4 instalado\n')
        self.assertEqual(state['generation'], LEGACY)

    def test_group_paused_before_lifting_maintenance_keeps_it(self):
        target, state = self.assert_paused_between_checks_keeps_maintenance('daemon-reload')
        self.assertEqual(state['generation'], CURRENT)

    def test_daemon_change_during_install_keeps_maintenance(self):
        installer, home, engine, target = self.install_fixture()
        def restart(args):
            if args[3:] == installer.SERVICES:
                self.gen = NEW + 'd' * 64
        self.hooks['stop'] = restart
        with self.assertRaisesRegex(ValueError, 'generación del daemon cambió'), redirect_stdout(io.StringIO()):
            installer.install(home, self.root / 'pid', control)
        self.assertTrue((engine.directory / 'maintenance').exists())
        self.assertEqual(self.authority(engine)[0]['generation'], LEGACY)
        self.assertEqual(target.read_text(), '# U3.4 instalado\n')

    def assert_startup_stops_before_effects(self, phase):
        installer, home, engine, target = self.install_fixture(startup=phase)
        before = self.fingerprint()
        for dry in (True, False):
            self.commands.clear()
            with self.assertRaisesRegex(ValueError, 'fase ' + phase), redirect_stdout(io.StringIO()):
                installer.install(home, self.root / 'pid', control, dry_run=dry)
            self.assert_no_effects(before)

    def test_prepared_startup_stops_before_effects(self):
        self.assert_startup_stops_before_effects('prepared')

    def test_intent_startup_stops_before_effects(self):
        self.assert_startup_stops_before_effects('intent')

    def test_ready_startup_converts_and_keeps_its_history(self):
        installer, home, engine, target = self.install_fixture(startup='ready')
        before = self.authority(engine)[0]
        self.install(installer, home)
        after = self.authority(engine)[0]
        self.assertEqual(after['generation'], CURRENT)
        self.assertEqual(after['startup'], before['startup'])

    def test_stable_generation_equal_is_untouched_and_different_keeps_restart_behaviour(self):
        installer, home, engine, target = self.install_fixture(stored=CURRENT)
        self.p.groups['jale']['status'] = 'Paused'
        with engine.connect(write=True) as db:
            state = engine.state(db); state['reasons']['jale'] = ['pressure']; engine.store(db, state)
        before = self.authority(engine)
        self.assertIn('vigente', self.install(installer, home, dry_run=True))
        self.install(installer, home)
        self.assertEqual(self.authority(engine), before)
        self.gen = NEW + 'e' * 64  # a real restart after the stable value was stored
        self.assertIn('otro daemon', self.install(installer, home, dry_run=True))
        self.install(installer, home)
        after = self.authority(engine)[0]
        self.assertEqual(after['generation'], self.gen)
        self.assertEqual(after['reasons']['jale'], ['ambiguous', 'pressure'])

    def test_table_migration_and_conversion_commit_in_one_transaction(self):
        self.gen = LEGACY
        installer, new, home, old, engine, target, commands = self.migration_fixture()
        for g in ('default', 'ajeno'):
            self.p.groups[g]['status'] = 'Running'
        self.gen = CURRENT
        with old.connect(write=True) as db:
            before = json.loads(db.execute('SELECT body FROM policy WHERE id=1').fetchone()[0])
            db.execute("CREATE TRIGGER refuse_generation BEFORE UPDATE ON policy WHEN instr(NEW.body, 'linux-proc-v1') > 0 "
                       "BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        with self.assertRaises(sqlite3.Error), redirect_stdout(io.StringIO()):
            installer.install(home, self.root / 'pid', new)
        with old.connect(write=True) as db:
            self.assertEqual(json.loads(db.execute('SELECT body FROM policy WHERE id=1').fetchone()[0]), before)
            db.execute('DROP TRIGGER refuse_generation')
        (engine.directory / 'maintenance').unlink()
        with redirect_stdout(io.StringIO()):
            installer.install(home, self.root / 'pid', new)
        with old.connect() as db:
            after = json.loads(db.execute('SELECT body FROM policy WHERE id=1').fetchone()[0])
        self.assertEqual((after['generation'], after['policy_hash'], after['revision']),
                         (CURRENT, new.DESK_POLICY_HASH, before['revision'] + 1))


if __name__ == '__main__':
    unittest.main()
