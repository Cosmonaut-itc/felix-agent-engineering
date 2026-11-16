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
        # Returning to A must reproduce its exact checkout, including its bytes.
        replacement = line if profile == self.a else 'MINI_PROFILE = '+repr(profile)
        (self.candidate/'control.py').write_text(source.replace(line, replacement))
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
        module = installer.importlib.util.module_from_spec
        def module_spec(name, path):
            if name == 'mini_install':
                return type('Spec', (), {'loader':type('Loader', (), {
                    'create_module':lambda *a:mini,
                    'exec_module':lambda *a:None})()})()
            return loader(name, path)
        def from_spec(spec):
            return mini if not hasattr(spec, 'name') else module(spec)
        output = io.StringIO(); error = None
        h.calls.clear(); h.p.calls.clear(); h.trace.clear()
        with patch.object(mini, 'control', self.new), patch.object(mini, 'ROOT', self.candidate), \
                patch.object(mini, 'Manager', return_value=h.m), \
                patch.object(installer.importlib.util, 'spec_from_file_location', side_effect=module_spec), \
                patch.object(installer.importlib.util, 'module_from_spec', side_effect=from_spec), \
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
        with patch.object(h.engine, 'store', side_effect=checked_store): output = self.migrate()
        state = h.policy()
        self.assert_announcement(output, state, 'conservar ajustes')
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
        self.assert_announcement(output, dict(self.before, revision=self.before['revision']+1,
                                             desired=dict(self.b, rapidas=2)), 'conservar ajustes')
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
        before = self.snapshot()
        error, output = self.install(dry=True)
        self.assertIsNone(error)
        expected = dict(self.before, revision=self.before['revision']+1, desired=self.new.MINI_PROFILE)
        self.assert_announcement(output, expected, 'retirar ajustes')
        self.assertEqual(self.snapshot(), before)
        output = self.migrate()
        state = self.h.policy()
        self.assert_announcement(output, state, 'retirar ajustes')
        self.assertEqual(state, dict(self.before, policy_hash=self.new.POLICY_HASH,
                                     desired=self.new.MINI_PROFILE, overrides={}, revision=self.before['revision']+1))
        self.assertEqual(self.h.engine.view()['cap'], 7)
        self.assertEqual(self.actions(), self.receipts)
        self.assert_no_manager_effects()

    def assert_announcement(self, output, state, adjustments):
        self.assertIn('revisión '+str(state['revision']), output)
        self.assertIn('desired '+json.dumps(state['desired'], sort_keys=True), output)
        self.assertIn(adjustments, output)

    def test_terminal_other_than_done_rejects_old_hash_without_effects(self):
        h = self.h
        for phase in ('aborted', 'withdrawn'):
            with self.subTest(phase=phase):
                state = copy.deepcopy(self.before)
                state['mini_handoff'].update(target='withdraw', phase=phase,
                    source=dict(owner='prepared', pid=999999, generation=h.gen), retained_owner='prepared')
                with h.engine.connect(write=True) as db: h.engine.store(db, state)
                before = self.snapshot()
                for dry in (True, False):
                    error, _ = self.install(dry)
                    self.assertIsNotNone(error, phase+' no acredita una migración')
                    self.assertEqual(self.snapshot(), before)
                    self.assertEqual(h.p.calls, [])
                    self.assert_no_manager_effects()

    def test_done_with_live_homebrew_rejects_old_hash_without_effects(self):
        h = self.h
        h.loaded.discard(mini.LABEL); h.loaded.add(mini.BREW); h.owner = mini.BREW
        # Disabled can still be loaded until bootout: isolate the live-owner gate.
        h.disabled[mini.BREW] = True; h.disabled[mini.LABEL] = True
        before = self.snapshot()
        for dry in (True, False):
            error, _ = self.install(dry)
            self.assertIsNotNone(error)
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(h.p.calls, [])
            self.assert_no_manager_effects()

    def test_noninstall_rejects_old_hash_without_effects(self):
        h = self.h
        before = self.snapshot()
        with patch.object(mini, 'control', self.new), patch.object(mini, 'ROOT', self.candidate):
            for dry in (True, False):
                h.calls.clear(); h.p.calls.clear()
                with self.assertRaises(ValueError): h.m.change('withdraw', dry=dry)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(h.p.calls, [])
                self.assertEqual(h.calls, [], 'Un hash viejo se rechaza antes de consultar el dueño vivo')
                self.assert_no_manager_effects()

    def test_same_profile_different_library_withdrawn_and_aborted_converge(self):
        self.select(self.a)
        h = self.h
        with patch.object(mini, 'control', self.new), patch.object(mini, 'ROOT', self.candidate):
            h.m.change('withdraw'); h.finish()
            with (self.candidate/'mini.py').open('a') as stream:
                stream.write('\n# Different checkout, same profile.\n')
            for phase in ('withdrawn', 'aborted'):
                with self.subTest(phase=phase):
                    state = h.policy()
                    state['mini_handoff'].update(phase=phase, target='install' if phase == 'aborted' else 'withdraw',
                        source=dict(owner='homebrew', pid=999999, generation=h.gen), retained_owner='homebrew')
                    h.m.save(state, 'test:terminal')
                    before = self.snapshot(); h.calls.clear(); h.p.calls.clear(); h.trace.clear()
                    for dry in (True, False):
                        try: h.m.change('withdraw', dry=dry)
                        except ValueError as error: self.fail('Repetir retirada debe converger: '+str(error))
                        self.assertFalse((h.directory/'table-migration.json').exists())
                        if dry or phase == 'withdrawn': self.assertEqual(self.snapshot(), before)
                    self.assertEqual(h.policy()['mini_handoff']['phase'], 'withdrawn')
                    self.assertEqual(h.p.calls, [])
                    self.assert_no_manager_effects()
                    for _ in range(2): self.assertTrue(h.m._recover(h.config))
            try: h.m.change('install'); h.finish()
            except ValueError as error: self.fail('Reinstalar tras retirada debe funcionar: '+str(error))
            self.assertEqual(h.owner, mini.LABEL)
            self.assert_installed_healthy()

    def test_aborted_pao_same_profile_converges_before_library_repair(self):
        self.select(self.a); h = self.h
        state = h.policy()
        state['mini_handoff'].update(phase='aborted', target='withdraw',
            source=dict(owner='prepared', pid=999999, generation=h.gen), retained_owner='prepared')
        h.m.save(state, 'test:aborted')
        with (self.candidate/'mini.py').open('a') as stream: stream.write('\n# Different checkout.\n')
        installed = {p:p.read_bytes() for p in h.m.library.rglob('*') if p.is_file()}
        error, _ = self.install()
        self.assertIsNone(error, 'Convergencia desde aborted sin migración: '+str(error))
        self.assertEqual(h.policy()['mini_handoff']['phase'], 'done')
        self.assertFalse((h.directory/'table-migration.json').exists())
        self.assertEqual({p:p.read_bytes() for p in installed}, installed)
        self.assertEqual(h.trace, ['handoff:converged'])
        self.migrate(); self.assert_installed_healthy()

    def test_same_hash_library_repair_rejects_unaccredited_owner_before_intent(self):
        self.select(self.a); h = self.h
        with (self.candidate/'mini.py').open('a') as stream: stream.write('\n# Repair candidate.\n')
        for invalid in ('generation', 'startup', 'brew_enabled'):
            with self.subTest(invalid=invalid):
                state = copy.deepcopy(self.before)
                if invalid == 'generation': state['generation'] = 'previous-daemon'
                elif invalid == 'startup': state['startup']['phase'] = 'prepared'
                else: h.disabled[mini.BREW] = False
                with h.engine.connect(write=True) as db: h.engine.store(db, state)
                before = self.snapshot()
                try:
                    for dry in (True, False):
                        error, _ = self.install(dry)
                        self.assertIsNotNone(error, invalid+' debe rechazarse antes de intención')
                        self.assertEqual(self.snapshot(), before)
                        self.assertEqual(h.p.calls, [])
                        self.assert_no_manager_effects()
                finally:
                    h.disabled[mini.BREW] = True
                    with h.engine.connect(write=True) as db: h.engine.store(db, self.before)

    def installed_manager(self):
        h = self.h
        spec = importlib.util.spec_from_file_location('table_installed_mini', h.m.library/'mini.py')
        runtime = importlib.util.module_from_spec(spec); spec.loader.exec_module(runtime)
        manager = runtime.Manager(home=h.home, pueue=h.p, directory=h.directory, launch=h.launch,
                                  checkpoint=h.trace.append, configuration=h.config)
        manager.engine = runtime.control.MiniControl(h.p, h.directory, generation=lambda:h.gen, clock=lambda:h.now)
        return manager, runtime

    def login_installed(self):
        h = self.h; checkout = h.m
        h.m, runtime = self.installed_manager()
        try:
            h.login()
            self.assertTrue(h.p.online, 'El login debe arrancar desde la biblioteca instalada')
            # The existing guard is also reconstructed at login and completes the
            # new generation before accepting an unrelated profile change.
            h.m, runtime = self.installed_manager()
            with control.locked(h.directory):
                ready = h.m._recover(h.config)
                if ready:
                    with patch.object(runtime.control, 'pressure_mini', return_value=LOW):
                        h.m.engine.guard(LOW)
        finally:
            h.m = checkout

    def assert_installed_healthy(self):
        h = self.h
        installed, _ = self.installed_manager()
        try: state = installed.state()
        except ValueError as error: self.fail('Biblioteca y autoridad deben coincidir: '+str(error))
        self.assertEqual(state['policy_hash'], self.new.POLICY_HASH)
        self.assertEqual((h.m.library/'control.py').read_bytes(), (self.candidate/'control.py').read_bytes())
        self.assertEqual(self.actions(), self.receipts)
        self.assertFalse((h.directory/'table-migration.json').exists())
        self.assertTrue(h.p.online)

    def cut_matrix(self, profile, login=False):
        self.migrate()
        checkpoints = list(self.h.trace)
        self.assertTrue(checkpoints)
        for boundary in checkpoints:
            with self.subTest(boundary=boundary, profile=profile, login=login):
                trial = MiniTableTests(); trial.setUp()
                try:
                    def crash(phase):
                        if phase == boundary: raise handoff.Crash(phase)
                    trial.h.m.checkpoint = crash
                    with self.assertRaises(handoff.Crash): trial.install()
                    trial.h.m.checkpoint = trial.h.trace.append
                    if login: trial.login_installed()
                    trial.select(trial.a if profile == 'A' else trial.b)
                    trial.migrate()
                    trial.assert_installed_healthy()
                    trial.login_installed()
                    trial.assert_installed_healthy()
                finally: trial.doCleanups()

    def test_every_table_cut_then_reinstall_a(self): self.cut_matrix('A')

    def test_every_table_cut_then_reinstall_b(self): self.cut_matrix('B')

    def test_every_table_cut_then_login_and_reinstall(self):
        self.cut_matrix('A', login=True)
        # A second independent matrix also fixes login followed by B.
        trial = MiniTableTests(); trial.setUp()
        try: trial.cut_matrix('B', login=True)
        finally: trial.doCleanups()

    def settle_installed(self):
        h = self.h
        for _ in range(8):
            h.m, runtime = self.installed_manager()
            h.engine = h.m.engine; h.runtime = runtime.control
            h.boot()
            with control.locked(h.directory): h.m._recover(h.config)
            with h.engine.connect() as db: state = h.engine.state(db, policy_migration=True)
            if state['mini_handoff']['phase'] == 'done' and not (h.directory/'table-migration.json').exists():
                h.m, runtime = self.installed_manager()
                h.engine = h.m.engine; h.runtime = runtime.control
                self.assertTrue(h.p.online)
                return
        self.fail('Mantenimiento y tabla deben converger hasta done sin intención')

    def maintenance_cut_matrix(self, profile):
        self.migrate(); checkpoints = list(self.h.trace)
        for boundary in checkpoints:
            with self.subTest(boundary=boundary, profile=profile):
                trial = MiniTableTests(); trial.setUp()
                try:
                    h = trial.h
                    def crash(phase):
                        if phase == boundary: raise handoff.Crash(phase)
                    h.m.checkpoint = crash
                    with self.assertRaises(handoff.Crash): trial.install()
                    h.m.checkpoint = h.trace.append
                    checkout = h.m
                    h.m, _ = trial.installed_manager()
                    before = trial.snapshot(); h.calls.clear(); h.p.calls.clear()
                    try: h.m.maintenance()
                    except ValueError:
                        # A partially published B runtime cannot read authority A.
                        # This rejection must precede marker, authority and stops.
                        self.assertEqual(trial.snapshot(), before)
                        self.assertEqual(h.calls, []); self.assertEqual(h.p.calls, [])
                    else:
                        self.assertFalse(h.p.online)
                        self.assertEqual(h.m.state()['mini_handoff']['phase'], 'maintenance')
                        before_wait = trial.snapshot()
                        with control.locked(h.directory):
                            self.assertFalse(h.m._recover(h.config))
                        self.assertEqual(trial.snapshot(), before_wait, 'Mantenimiento espera sin efectos')
                        try: h.m.maintenance(resume=True)
                        except ValueError as error: self.fail('Continuar debe permitir reconstruir el dueño: '+str(error))
                    try: trial.settle_installed()
                    except ValueError as error: self.fail('Los ticks deben converger: '+str(error))
                    self.assertEqual(trial.actions(), trial.receipts)
                    self.assertIn('manual', h.m.state()['reasons']['suites'])
                    self.assertEqual(h.p.groups['suites']['status'], 'Paused')
                    self.assertFalse(any(c[0] == 'start' for c in h.p.calls), 'Recuperación no abre grupos')
                    trial.login_installed()
                    h.m = checkout
                    trial.select(trial.a if profile == 'A' else trial.b)
                    trial.migrate(); trial.assert_installed_healthy()
                    trial.login_installed(); trial.assert_installed_healthy()
                finally: trial.doCleanups()

    def test_every_table_cut_then_maintenance_continue_login_reinstall_a(self):
        self.maintenance_cut_matrix('A')

    def test_every_table_cut_then_maintenance_continue_login_reinstall_b(self):
        self.maintenance_cut_matrix('B')

    def test_pending_table_waits_only_for_owned_install_maintenance_with_matching_runtime(self):
        h = self.h
        def crash(phase):
            if phase == 'table:intent': raise handoff.Crash(phase)
        h.m.checkpoint = crash
        with self.assertRaises(handoff.Crash): self.install()
        h.m.checkpoint = h.trace.append
        installed, _ = self.installed_manager()
        for invalid in (None, 'target', 'ownership', 'runtime'):
            for phase in ('maintenance', 'switch', 'verify'):
                with self.subTest(invalid=invalid, phase=phase):
                    state = copy.deepcopy(self.before)
                    state['mini_handoff'].update(phase=phase, maintenance_owned=True)
                    if invalid == 'target': state['mini_handoff']['target'] = 'withdraw'
                    elif invalid == 'ownership': state['mini_handoff']['maintenance_owned'] = False
                    elif invalid == 'runtime': state = h.m.table_plan(state)
                    with h.engine.connect(write=True) as db: h.engine.store(db, state)
                    before = self.snapshot(); h.calls.clear(); h.p.calls.clear()
                    with control.locked(h.directory):
                        if invalid:
                            with self.assertRaises(ValueError): installed.recover_table()
                        else:
                            try: self.assertIsNone(installed.recover_table())
                            except ValueError as error: self.fail('La tabla debe esperar al relevo: '+str(error))
                    self.assertEqual(self.snapshot(), before)
                    self.assertEqual(h.calls, []); self.assertEqual(h.p.calls, [])

    def test_durable_intent_precedes_every_library_effect(self):
        h = self.h; publish = h.m.publish; intents = []
        def checked_publish(target, data):
            if h.m.library in target.parents:
                journal = h.directory/'table-migration.json'
                self.assertTrue(journal.is_file(), 'Intención durable antes de publicar biblioteca')
                intents.append(json.loads(journal.read_text()))
            publish(target, data)
        with patch.object(h.m, 'publish', side_effect=checked_publish): self.migrate()
        self.assertTrue(intents)
        self.assertTrue(all(i['source_hash'] == self.before['policy_hash'] and
                            i['target_hash'] == self.new.POLICY_HASH for i in intents))

    def test_repeat_repairs_library_even_when_authority_hash_matches(self):
        self.migrate()
        (self.h.m.library/'control.py').write_bytes((ROOT/'control.py').read_bytes())
        before = self.h.policy()
        self.migrate()
        self.assertEqual(self.h.policy(), before)
        self.assert_installed_healthy()

    def test_install_publishes_reorder_helper_without_restart(self):
        # A library from before U3.6 lacks only the helper; same profile, so no migration.
        h = self.h; self.select(self.a)
        helper = h.m.library/control.EDITOR_NAME
        self.assertTrue(helper.is_file(), 'La instalación publica el ayudante de reordenar')
        helper.unlink(); before = h.policy(); generation = h.gen
        error, _ = self.install()
        self.assertIsNone(error, str(error))
        self.assertTrue(helper.is_file(), 'La reinstalación repone el ayudante')
        self.assertEqual(helper.read_bytes(), (self.candidate/control.EDITOR_NAME).read_bytes())
        self.assertEqual(h.policy(), before); self.assertEqual(h.gen, generation)
        self.assert_no_manager_effects()
        self.assertFalse(any(c[0] in ('pause', 'start', 'restart', 'shutdown', 'kill') for c in h.p.calls), h.p.calls)
        self.assert_installed_healthy()

    def test_intent_from_before_the_helper_still_recovers(self):
        h = self.h
        def crash(phase):
            if phase == 'table:intent': raise handoff.Crash(phase)
        h.m.checkpoint = crash
        try:
            with self.assertRaises(handoff.Crash): self.install()
        finally: h.m.checkpoint = h.trace.append
        journal = h.directory/'table-migration.json'
        value = json.loads(journal.read_text()); self.assertIn(control.EDITOR_NAME, value['files'])
        del value['files'][control.EDITOR_NAME]; journal.write_text(json.dumps(value))
        self.migrate()
        self.assertFalse(journal.exists())
        self.assertEqual(h.policy()['policy_hash'], self.new.POLICY_HASH)

    def test_recovery_rejects_unaccredited_intent_without_effects(self):
        h = self.h
        def crash(phase):
            if phase == 'table:intent': raise handoff.Crash(phase)
        h.m.checkpoint = crash
        try:
            with self.assertRaises(handoff.Crash): self.install()
        finally: h.m.checkpoint = h.trace.append
        journal = h.directory/'table-migration.json'
        self.assertTrue(journal.is_file())
        intent = journal.read_bytes()
        for invalid in ('source', 'target', 'files', 'phase', 'brew', 'pending'):
            with self.subTest(invalid=invalid):
                value = json.loads(intent); state = copy.deepcopy(self.before)
                if invalid == 'source': value['source_hash'] = 'f'*64
                elif invalid == 'target': value['target_hash'] = 'f'*64
                elif invalid == 'files': value['files']['../elsewhere'] = 'invalid'
                elif invalid == 'phase':
                    state['mini_handoff'].update(phase='aborted',
                        retained_owner=state['mini_handoff']['source']['owner'])
                elif invalid == 'brew': h.loaded.add(mini.BREW)
                with h.engine.connect(write=True) as db:
                    h.engine.store(db, state)
                    if invalid == 'pending':
                        db.execute('INSERT INTO host_actions VALUES(?,?,?)', ('pending-0001', 'digest',
                                   json.dumps(dict(response=dict(status='applying')))))
                journal.write_text(json.dumps(value))
                before = self.snapshot()
                try:
                    error, _ = self.install()
                    self.assertIsNotNone(error, invalid+' no acredita recuperación')
                    self.assertEqual(self.snapshot(), before)
                    self.assertEqual(h.p.calls, [])
                    self.assert_no_manager_effects()
                finally:
                    h.loaded.discard(mini.BREW)
                    journal.write_bytes(intent)
                    with h.engine.connect(write=True) as db:
                        h.engine.store(db, self.before)
                        db.execute('DELETE FROM host_actions WHERE action_id=?', ('pending-0001',))

    def test_installed_guard_recovers_without_checkout(self):
        h = self.h
        boundary = 'publish:renamed:.local/lib/pao-pueue-mini/control.py'
        def crash(phase):
            if phase == boundary: raise handoff.Crash(phase)
        h.m.checkpoint = crash
        try:
            with self.assertRaises(handoff.Crash): self.install()
        finally: h.m.checkpoint = h.trace.append
        # Move the checkout out of reach; only the durable installed bundle exists.
        self.candidate.rename(h.home/'checkout-unavailable')
        installed, runtime = self.installed_manager()
        try:
            with control.locked(h.directory): self.assertTrue(installed._recover(h.config))
        except ValueError as error: self.fail('La guardia instalada debe recuperar: '+str(error))
        self.assertEqual(installed.state()['policy_hash'], self.new.POLICY_HASH)
        self.assertEqual(self.actions(), self.receipts)
        self.assertFalse((h.directory/'table-migration.json').exists())

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
