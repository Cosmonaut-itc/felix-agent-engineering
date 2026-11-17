"""SEN-6495: selección conservadora y pasada aislada de la guardia."""
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('prune_control', ROOT / 'control.py')
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)
NOW = datetime(2026, 11, 16, tzinfo=timezone.utc).timestamp()


def done(age=90000, result='Success'):
    return dict(status={'Done': dict(end=datetime.fromtimestamp(NOW-age, timezone.utc).isoformat(), result=result)}, dependencies=[])


class PruneTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='sen6495-test-'))
        self.p = Mock()
        self.p.status.return_value = dict(tasks={'1': done()}, groups={})
        self.p.call.side_effect = lambda *args: 'Tasks removed from list: '+', '.join(args[1:])+'\n'

    def select(self, tasks):
        self.assertTrue(callable(getattr(control, 'select_prune', None)), 'select_prune debe existir')
        before = deepcopy(tasks)
        result = control.select_prune(tasks, NOW)
        self.assertEqual(tasks, before)
        return result

    def run_pass(self, now=NOW):
        self.assertTrue(callable(getattr(control, 'prune_history', None)), 'prune_history debe existir')
        return control.prune_history(self.p, self.root, now=now)

    def test_I1_only_exact_Done_and_unexpected_shapes(self):
        cases = [None, 'Done', {}, {'Other': {}}, dict(done()['status'], Running={}), {'Done': None},
                 {'Done': {'end': None}}, {'Done': {'end': 'wrong'}},
                 {'Done': {'end': '2026-01-01T00:00:00'}},
                 {'Done': {'end': '2026-01-01T00:00:00Z'}},
                 {'Done': {'end': '2026-01-01T00:00:00Z', 'result': {}}}]
        for status in cases:
            with self.subTest(status=status):
                ids, counts = self.select({'1': dict(status=status, dependencies=[])})
                self.assertEqual(ids, [])
                self.assertEqual(counts['omitidas'], 1)
        for variant in ('Queued', 'Stashed', 'Locked', 'Running', 'Paused'):
            self.p.status.return_value['tasks'] = {'1': dict(status={variant: {}}, dependencies=[]), '2': done()}
            self.run_pass(now=NOW+4000)
            self.p.call.assert_called_with('remove', '2')
            (self.root/'poda.json').unlink()
        self.assertEqual(self.select({'1': None})[1]['omitidas'], 1)

    def test_I2_retention_boundaries_and_results(self):
        for result, threshold in [('Success', 86400), ({'Failed': 1}, 86400),
                                  ({'FailedToSpawn': 'synthetic'}, 86400), ('Errored', 86400),
                                  ('DependencyFailed', 86400), ('Killed', 604800)]:
            for delta, expected in [(-1, []), (0, []), (1, ['1'])]:
                with self.subTest(result=result, delta=delta):
                    self.assertEqual(self.select({'1': done(threshold+delta, result)})[0], expected)
        self.assertEqual(self.select({'1': done(100, 'Killed')})[1]['retenidas_killed'], 1)
        self.assertEqual(self.select({'1': done(100)})[1]['retenidas'], 1)

    def test_I2_nanoseconds_and_timezone_never_shorten_retention(self):
        for end, now, expected in [
            ('2026-11-14T23:59:59.999999999Z', NOW, ['1']),
            ('2026-11-15T00:00:00.000000001Z', NOW, []),
            ('2026-11-14T18:00:00-06:00', NOW, []),
            ('2026-11-15T00:00:00.000000239Z', NOW+0.0000002384185791015625, []),
        ]:
            task=done()
            task['status']['Done']['end']=end
            with self.subTest(end=end):
                self.assertEqual(control.select_prune({'1': task}, now)[0], expected)

    def test_I3_dependencies_of_every_nonterminal_and_malformed_task(self):
        for status in ('Queued', 'Stashed', 'Locked', 'Running', 'Paused', 'unexpected'):
            tasks = {'1': done(), '2': dict(status={status: {}}, dependencies=[1])}
            ids, counts = self.select(tasks)
            self.assertEqual(ids, [])
            self.assertEqual(counts['dependencias'], 1)
        self.assertEqual(self.select({'1': done(), '2': dict(done(), dependencies=[1])})[0], ['1', '2'])
        # An unreadable dependency list cannot certify any candidate as unreferenced.
        self.assertEqual(self.select({'1': done(), '2': dict(status='Queued', dependencies=None)})[0], [])

    def test_I4_prune_errors_never_change_guard_result_and_outside_lock(self):
        self.assertTrue(callable(getattr(control, 'prune_history', None)))
        with patch.object(control, 'STATE_DIR', self.root), patch.object(control, 'Pueue', return_value=self.p), patch.object(control, 'MiniControl'), patch.object(control, 'guard') as guard, patch.object(control, 'prune_history') as prune, patch('sys.argv', ['control.py', 'guardia']):
            def fail(*args, **kwargs):
                with (self.root/'control.lock').open('a') as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                raise RuntimeError('private detail')
            prune.side_effect = fail
            try:
                result = control.main()
            except Exception as error:
                self.fail('La poda propagó '+type(error).__name__)
            self.assertIsNone(result)
            prune.assert_called_once()
            guard.side_effect = ValueError('guard failed')
            prune.reset_mock()
            with self.assertRaisesRegex(ValueError, 'guard failed'):
                control.main()
            prune.assert_not_called()
        self.p.status.side_effect = OSError('private detail')
        self.run_pass()
        self.assertNotIn('private detail', (self.root/'guardia.log').read_text())
        with patch.object(control, 'log', side_effect=OSError('log unavailable')):
            self.run_pass(NOW+4000)

    def test_I5_switch_no_Pueue_calls(self):
        (self.root/'poda.off').touch()
        self.run_pass()
        self.p.assert_not_called()
        self.p.status.assert_not_called()
        self.p.call.assert_not_called()

    def test_I6_cadence_and_cap_retry(self):
        self.run_pass()
        self.run_pass(NOW+3599)
        self.assertEqual(self.p.status.call_count, 1)
        self.run_pass(NOW+3600)
        self.assertEqual(self.p.status.call_count, 2)
        (self.root/'poda.json').unlink()
        self.p.status.return_value['tasks'] = {str(i): done() for i in range(501)}
        self.p.call.reset_mock()
        self.run_pass(NOW+4000)
        self.assertEqual(sum(len(c.args)-1 for c in self.p.call.call_args_list), 500)
        self.assertTrue(all(len(c.args)==101 for c in self.p.call.call_args_list))
        self.run_pass(NOW+4001)
        self.assertEqual(self.p.status.call_count, 4)
        record=json.loads((self.root/'guardia.log').read_text().splitlines()[-1])
        self.assertEqual(record['tope'], 1)

    def test_failed_batch_continues_and_counts_without_sensitive_data(self):
        self.p.status.return_value['tasks'] = {str(i): done() for i in range(201)}
        calls = []
        def remove(*args):
            calls.append(args)
            if len(calls) == 1:
                raise subprocess.CalledProcessError(1, ['secret'])
            return 'Tasks removed from list: '+', '.join(args[1:])+'\n'
        self.p.call.side_effect = remove
        self.run_pass()
        self.assertEqual(self.p.call.call_count, 3)
        record=json.loads((self.root/'guardia.log').read_text().splitlines()[-1])
        self.assertEqual(record['lotes_fallidos'], 1)
        self.assertEqual(record['podadas'], 101)
        self.assertEqual(record['sin_confirmar'], 100)
        self.assertNotIn('secret', json.dumps(record))

    def test_M4_partial_success_exit_zero_counts_acknowledged_ids_only(self):
        self.p.status.return_value['tasks'] = {str(i): done() for i in (1, 2, 3)}
        self.p.call.side_effect = None
        self.p.call.return_value = 'Tasks removed from list: 1, 3\nThe command failed for tasks: 2\n'
        self.run_pass()
        record=json.loads((self.root/'guardia.log').read_text().splitlines()[-1])
        self.assertEqual((record['podadas'], record['sin_confirmar'], record['lotes_fallidos']), (2, 1, 1))
        self.assertEqual(self.p.status.call_count, 1)

    def test_unrecognized_or_inconsistent_acknowledgement_is_not_a_removal_count(self):
        self.p.call.side_effect = None
        for output in ('', 'Tasks removed from list: 999\n',
                       'Tasks removed from list: 1, 1\n',
                       'Tasks removed from list: 1\nTasks removed from list: 1\n'):
            with self.subTest(output=output):
                self.p.call.return_value=output
                self.run_pass()
                record=json.loads((self.root/'guardia.log').read_text().splitlines()[-1])
                self.assertEqual((record['podadas'], record['sin_confirmar'], record['lotes_fallidos']), (0, 1, 1))
                (self.root/'poda.json').unlink()

    def test_nonblocking_own_lock_and_clock_rollback(self):
        with (self.root/'poda.lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.run_pass()
        self.p.status.assert_not_called()
        self.run_pass()
        self.run_pass(NOW-1)
        self.assertEqual(self.p.status.call_count, 1)

    def test_unreadable_marker_clock_and_status_are_retained(self):
        self.assertTrue(callable(getattr(control, 'prune_history', None)))
        for contents in ('broken', '{"at": 0, "capped": "yes"}'):
            (self.root/'poda.json').write_text(contents)
            self.run_pass()
            self.p.status.assert_not_called()
        (self.root/'poda.json').unlink()
        self.run_pass(float('nan'))
        self.p.status.assert_not_called()
        self.p.status.side_effect = OSError('secret')
        self.run_pass()
        self.run_pass(NOW+1)
        self.assertEqual(self.p.status.call_count, 1)
        self.p.call.assert_not_called()
        self.assertTrue(json.loads((self.root/'guardia.log').read_text().splitlines()[-1])['error'])

    def admission(self, outcome):
        attempt = dict(boot_id='00000000-0000-4000-8000-000000000000', reserved_ns=5,
                       completed_ns=7 if outcome in ('confirmed', 'not_sent') else None,
                       at='2026-11-14T00:00:00+00:00', generation='a'*64, grupo='hasito', id=1,
                       created_at='2026-11-14T00:00:00+00:00', enqueued_at='2026-11-14T00:00:00+00:00',
                       outcome=outcome)
        confirmed = dict(at=attempt['at'], grupo='hasito', id=1) if outcome == 'confirmed' else None
        body = control.canonical(dict(version=1, last_attempt=attempt, last_confirmed=confirmed))
        db = sqlite3.connect(self.root/'control.sqlite3')
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS guard_admission (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)')
            db.execute('INSERT OR REPLACE INTO guard_admission VALUES (1,?)', (body,))
        db.close()

    def test_I7_uncertain_admission_keeps_its_execution_evidence(self):
        ids, counts = control.select_prune({'1': done(), '2': done()}, NOW, protected={'1'})
        self.assertEqual((ids, counts['admision'], counts['dependencias']), (['2'], 1, 0))
        self.p.status.return_value = dict(tasks={'1': done(), '2': done()}, groups={})
        for step, outcome in enumerate(('unknown', 'reserved')):
            self.admission(outcome)
            self.run_pass(NOW+step*4000)
            removed = [ident for call in self.p.call.call_args_list for ident in call.args[1:]]
            self.assertEqual(removed, ['2'], outcome+': la poda quitó la evidencia del permiso')
            self.assertEqual(json.loads((self.root/'guardia.log').read_text().splitlines()[-1])['admision'], 1)
            self.p.call.reset_mock()
        for step, outcome in enumerate(('confirmed', 'not_sent'), 2):
            self.admission(outcome)
            self.run_pass(NOW+step*4000)
            removed = [ident for call in self.p.call.call_args_list for ident in call.args[1:]]
            self.assertEqual(removed, ['1', '2'], outcome+': un permiso resuelto no retiene historial')
            self.p.call.reset_mock()

    def test_I7_unreadable_admission_retains_everything(self):
        (self.root/'control.sqlite3').write_text('not a database')
        self.run_pass()
        self.p.call.assert_not_called()
        self.assertTrue(json.loads((self.root/'guardia.log').read_text().splitlines()[-1])['error'])
        (self.root/'control.sqlite3').unlink()
        db = sqlite3.connect(self.root/'control.sqlite3')
        with db:
            db.execute('CREATE TABLE guard_admission (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)')
            db.execute("INSERT INTO guard_admission VALUES (1,'{}')")
        db.close()
        self.run_pass(NOW+4000)
        self.p.call.assert_not_called()
        self.assertTrue(json.loads((self.root/'guardia.log').read_text().splitlines()[-1])['error'])

    def test_policy_and_deterministic_oldest_first(self):
        policy = dict(control.PRUNE_POLICY, limit=1, batch=1, retention=1)
        ids, counts=control.select_prune({'9': done(90002), '2': done(90001)}, NOW, policy)
        self.assertEqual(ids, ['9'])
        self.assertEqual(counts['tope'], 1)


if __name__ == '__main__':
    unittest.main()
