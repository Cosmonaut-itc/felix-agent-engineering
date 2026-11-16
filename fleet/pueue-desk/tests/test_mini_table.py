"""MINI_PROFILE migrations: real SQLite/files, fake Pueue and launchd only."""
import copy
from contextlib import redirect_stdout
import fcntl
import importlib.util
import io
import json
import shutil
import sqlite3
import unittest
from unittest.mock import patch

import test_handoff as handoff
from test_handoff import Harness, LOW, ROOT, control, mini


class MiniTableTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(control.subprocess, 'run', side_effect=handoff.HandoffTests.no_real_process)
        guard.start(); self.addCleanup(guard.stop)
        self.h = h = Harness(self)
        h.m.change('install'); h.finish()
        view = h.engine.view()
        receipt = dict(schema=control.HOST_SCHEMA, op='slots', action_id='table-slots-0001', dry_run=False,
                       expected={k:view[k] for k in ('revision', 'policy_hash', 'generation', 'mode', 'slots')},
                       change=dict(group='rapidas', delta=-1))
        self.assertEqual(h.engine.handle(receipt)['status'], 'applied')
        # Keep pressure/manual ownership in the migration, independently of live status.
        with h.engine.connect(write=True) as db:
            state = h.engine.state(db)
            state['reasons']['rapidas'] = ['pressure']
            h.engine.store(db, state)
        h.p.groups['rapidas']['status'] = 'Paused'; h.save_queue()
        self.before = h.policy(); self.receipts = self.actions()
        self.a = dict(control.MINI_PROFILE)
        self.b = dict(self.a, suites=2, default=2)
        self.candidate = h.home/'candidate'
        shutil.copytree(ROOT, self.candidate, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'tests'))
        self.select(self.b)

    def select(self, profile):
        source = (ROOT/'control.py').read_text()
        line = next(line for line in source.splitlines() if line.startswith('MINI_PROFILE = '))
        (self.candidate/'control.py').write_text(source.replace(line, 'MINI_PROFILE = '+repr(profile)))
        spec = importlib.util.spec_from_file_location('table_candidate', self.candidate/'control.py')
        new = importlib.util.module_from_spec(spec); spec.loader.exec_module(new)
        self.new = new
        self.h.engine = new.MiniControl(self.h.p, self.h.directory,
                                       generation=lambda:self.h.gen, clock=lambda:self.h.now)
        self.h.m.engine = self.h.engine; self.h.runtime = new

    def actions(self):
        with self.h.engine.connect() as db:
            return db.execute('SELECT * FROM host_actions ORDER BY action_id').fetchall()

    def snapshot(self):
        # Bytes alone would miss a gratuitous SQLite UPDATE or republishing the same files.
        return {str(p.relative_to(self.h.home)):(p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino)
                for p in self.h.home.rglob('*') if p.is_file()}

    def install(self, dry=False):
        h = self.h
        # Exercise the CLI dispatch as well as Manager.change, without a real subprocess.
        spec = importlib.util.spec_from_file_location('table_installer', ROOT/'install-mini.py')
        installer = importlib.util.module_from_spec(spec); spec.loader.exec_module(installer)
        loader = installer.importlib.util.spec_from_file_location
        def module_spec(name, path):
            if name == 'mini_install':
                return type('Spec', (), {'loader':type('Loader', (), {
                    'create_module':lambda *a:mini,
                    'exec_module':lambda *a:None})()})()
            return loader(name, path)
        output = io.StringIO(); error = None
        h.calls.clear(); h.p.calls.clear(); h.trace.clear()
        with patch.object(mini, 'control', self.new), patch.object(mini, 'ROOT', self.candidate), \
                patch.object(mini, 'Manager', return_value=h.m), \
                patch.object(installer.importlib.util, 'spec_from_file_location', side_effect=module_spec), \
                patch.object(installer.importlib.util, 'module_from_spec', return_value=mini), \
                patch.object(installer.sys, 'argv', ['install-mini.py', 'instalar', '--relevo']+(['--seco'] if dry else [])), \
                redirect_stdout(output):
            try: installer.main()
            except (ValueError, TypeError, KeyError, sqlite3.Error) as caught: error = caught
        return error, output.getvalue()

    def migrate(self):
        error, output = self.install()
        self.assertIsNone(error, 'La reinstalación debe migrar la tabla: '+str(error))
        try: self.h.engine.view()
        except ValueError as caught: error = caught
        self.assertIsNone(error, 'La autoridad debe aceptar el perfil nuevo: '+str(error))
        return output

    def assert_no_manager_effects(self):
        self.assertTrue(all(c[0] in ('print', 'print-disabled') for c in self.h.calls), self.h.calls)
        self.assertFalse(any(c[0] in ('pause', 'start') for c in self.h.p.calls), self.h.p.calls)

    def test_table_a_to_b_preserves_authority_and_guard_remains_healthy(self):
        h = self.h; generation = h.gen
        with self.assertRaises(ValueError): h.engine.view()  # runtime remains strict
        store = h.engine.store
        def checked_store(db, state):
            with (h.directory/'control.lock').open('rb') as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return store(db, state)
        with patch.object(h.engine, 'store', side_effect=checked_store): self.migrate()
        state = h.policy()
        self.assertEqual(state, dict(self.before, policy_hash=self.new.POLICY_HASH,
                                     desired=dict(self.b, rapidas=2), revision=self.before['revision']+1))
        self.assertEqual(self.actions(), self.receipts)
        self.assertEqual(h.engine.view()['slots'], state['desired'])
        self.assertEqual(h.gen, generation)
        self.assert_no_manager_effects()
        self.assertEqual(h.p.calls, [('parallel', 'suites', 2), ('parallel', 'default', 2)])
        self.assertEqual((h.m.library/'control.py').read_bytes(), (self.candidate/'control.py').read_bytes())
        with patch.object(self.new, 'pressure_mini', return_value=LOW):
            h.engine.guard(LOW)
        self.assertEqual(h.engine.view()['policy_hash'], self.new.POLICY_HASH)
        self.assertEqual(h.p.groups['suites']['status'], 'Paused')

    def test_repeat_install_does_not_write_or_restart(self):
        self.migrate(); before = self.snapshot()
        self.migrate()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.h.p.calls, [])
        self.assert_no_manager_effects()

    def test_seco_announces_migration_without_writing(self):
        before = self.snapshot()
        error, output = self.install(dry=True)
        self.assertIsNone(error, 'El ensayo debe acreditar la tabla anterior: '+str(error))
        self.assertIn('migración de tabla', output)
        self.assertIn(self.new.POLICY_HASH, output)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.h.p.calls, [])
        self.assert_no_manager_effects()
        with self.assertRaises(ValueError): self.h.engine.view()

    def test_return_to_table_a_migrates_again(self):
        self.migrate(); self.select(self.a); self.migrate()
        self.assertEqual(self.h.policy(), dict(self.before, revision=self.before['revision']+2))
        self.assertEqual(self.actions(), self.receipts)
        self.assertEqual(self.h.engine.view()['slots'], self.before['desired'])
        self.assert_no_manager_effects()

    def test_running_task_survives_profile_change_without_drain(self):
        h = self.h
        h.p.tasks = {'0': handoff.task(0, handoff.running(), 'default')}; h.save_queue()
        before = copy.deepcopy(h.p.tasks); generation = h.gen
        self.select(dict(suites=4, default=1, rapidas=2))
        self.migrate()
        self.assertEqual(h.p.tasks, before)
        self.assertEqual(h.gen, generation)
        self.assertEqual(h.engine.view()['cap'], 7)
        self.assertEqual(h.engine.view()['slots'], dict(suites=4, default=1, rapidas=2))
        self.assert_no_manager_effects()

    def test_sqlite_update_failure_keeps_old_policy_and_receipts(self):
        error, _ = self.install(dry=True)
        self.assertIsNone(error, 'El control sano debe admitir el ensayo: '+str(error))
        h = self.h
        with h.engine.connect(write=True) as db:
            db.execute("CREATE TRIGGER refuse_table BEFORE UPDATE ON policy BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        error, _ = self.install()
        self.assertIsInstance(error, sqlite3.Error)
        with h.engine.connect() as db:
            self.assertEqual(json.loads(db.execute('SELECT body FROM policy WHERE id=1').fetchone()[0]), self.before)
        self.assertEqual(self.actions(), self.receipts)
        self.assertEqual(h.p.calls, [])
        self.assert_no_manager_effects()

    def test_override_that_does_not_fit_is_retired_like_desk(self):
        # Base 3/3/1 fits, but retaining the old manual rapidas=2 would total 8.
        self.select(dict(suites=3, default=3, rapidas=1))
        self.migrate()
        state = self.h.policy()
        self.assertEqual(state, dict(self.before, policy_hash=self.new.POLICY_HASH,
                                     desired=self.new.MINI_PROFILE, overrides={}, revision=self.before['revision']+1))
        self.assertEqual(self.h.engine.view()['cap'], 7)
        self.assertEqual(self.actions(), self.receipts)
        self.assert_no_manager_effects()

    def test_unprovable_state_rejects_before_any_effect(self):
        h = self.h
        # First prove the valid control case is accepted, so this regression fails on main too.
        error, _ = self.install(dry=True)
        self.assertIsNone(error, 'El control sano debe admitir el ensayo: '+str(error))
        for invalid in ('applying', 'partial', 'unknown', 'handoff', 'startup', 'prepared', 'generation',
                        'hash', 'desired', 'reasons', 'sqlite'):
            with self.subTest(invalid=invalid):
                state = copy.deepcopy(self.before)
                if invalid == 'sqlite':
                    backup = h.engine.path.read_bytes(); h.engine.path.write_bytes(b'not sqlite')
                else:
                    with h.engine.connect(write=True) as db:
                        if invalid in ('applying', 'partial', 'unknown'):
                            db.execute('INSERT INTO host_actions VALUES(?,?,?)', ('pending-0001', 'digest',
                                       json.dumps(dict(response=dict(status=invalid)))))
                        elif invalid == 'handoff': state['mini_handoff']['phase'] = 'verify'
                        elif invalid == 'startup': state['startup']['phase'] = 'intent'
                        elif invalid == 'prepared': state['startup']['phase'] = 'prepared'
                        elif invalid == 'generation': state['generation'] = 'previous-daemon'
                        elif invalid == 'hash': state['policy_hash'] = 'broken'
                        elif invalid == 'desired': state['desired']['suites'] = True
                        elif invalid == 'reasons': state['reasons']['suites'] = ['alien']
                        h.engine.store(db, state)
                try:
                    before = self.snapshot()
                    for dry in (True, False):
                        error, _ = self.install(dry)
                        self.assertIsNotNone(error, 'Debe rechazar '+invalid)
                        self.assertEqual(self.snapshot(), before)
                        self.assertEqual(h.p.calls, [])
                        self.assert_no_manager_effects()
                finally:
                    if invalid == 'sqlite': h.engine.path.write_bytes(backup)
                    else:
                        with h.engine.connect(write=True) as db:
                            h.engine.store(db, self.before)
                            db.execute('DELETE FROM host_actions WHERE action_id=?', ('pending-0001',))
        for profile in (dict(suites=4, default=2, rapidas=3), dict(self.b, suites=True)):
            self.select(profile); before = self.snapshot()
            for dry in (True, False):
                error, _ = self.install(dry)
                self.assertIsNotNone(error, 'Perfil incompatible debe rechazarse')
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(h.p.calls, [])
                self.assert_no_manager_effects()
        self.select(self.b)
        spec = importlib.util.spec_from_file_location('legacy_installer', ROOT/'install-mini.py')
        installer = importlib.util.module_from_spec(spec); spec.loader.exec_module(installer)
        before = self.snapshot()
        with patch.object(installer.Path, 'home', return_value=h.home), \
                patch.object(installer.sys, 'argv', ['install-mini.py', 'instalar', '--cuotas']):
            with self.assertRaisesRegex(ValueError, 'legacy'): installer.main()
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__': unittest.main()
