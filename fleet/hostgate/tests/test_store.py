import concurrent.futures
import hashlib
import sqlite3
from hostgate.tests.test_contract import Harness
from hostgate.tests.support.world import request, retry_busy
from hostgate.admission import Hostgate
from hostgate.store import ShadowStore, StoreError


class Storage(Harness):
    def test_concurrent_budget_and_parallel_replay(self):
        def reserve(i):
            return retry_busy(lambda: Hostgate(self.config, self.world).reservar(request('parallel-' + str(i), str(i)), self.auth))
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            replies = list(pool.map(reserve, range(8)))
        self.assertEqual(sum(r['wouldGrant'] for r in replies), 2)
        self.assertEqual(self.view()['budget']['retained']['slots'], 2)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            replies = list(pool.map(lambda _: reserve(0), range(8)))
        self.assertTrue(all(r['originalResult'] == replies[0]['originalResult'] for r in replies))

    def test_missing_corrupt_wrong_store_never_recreated(self):
        db = self.config.state_dir / 'shadow.db'
        db.unlink()
        with self.assertRaises(StoreError): self.view()
        self.assertFalse(db.exists())
        db.write_bytes(b'corrupt')
        with self.assertRaises(StoreError): self.view()
        import shutil
        shutil.rmtree(self.config.state_dir)
        ShadowStore.initialize(self.config)
        with sqlite3.connect(str(db)) as conn:
            conn.execute("UPDATE metadata SET store_id='wrong'")
        with self.assertRaises(StoreError): self.view()

    def test_busy(self):
        conn = sqlite3.connect(str(self.config.state_dir / 'shadow.db'))
        self.addCleanup(conn.close)
        conn.execute('BEGIN IMMEDIATE')
        r = self.reserve()
        self.assertEqual(r['exitCode'], 4)
        conn.rollback()

    def test_commit_cuts_and_response_lost(self):
        for cut in ('before_write', 'before_commit', 'after_commit'):
            self.world.fault = cut
            r = self.reserve(cut, cut)
            self.assertEqual(r['exitCode'], 3)
            self.assertFalse(r['wouldGrant'])
            self.world.fault = None
            replay = self.reserve(cut, cut)
            self.assertTrue(replay['wouldGrant'] if cut != 'after_commit' else replay['replayed'])
            self.release(self.key(replay), 'release-' + cut)
        self.assertEqual(self.view()['budget']['retained']['slots'], 0)

    def test_queries_never_mutate_sqlite_or_wal(self):
        self.reserve()
        self.world.now += 40
        def hashes():
            return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in self.config.state_dir.glob('shadow.db*') if not p.name.endswith('-shm')}
        before = hashes()
        for _ in range(10): self.view()
        self.assertEqual(before, hashes())
        self.reserve('persist-expiry', 'persist-expiry')
        self.assertEqual(self.view()['reservations'][0]['state'], 'uncertain')

    def test_configuration_symlink_and_enforcing_rejected(self):
        from dataclasses import replace
        bad = self.config.state_dir.parent / 'link'
        bad.symlink_to(self.config.state_dir, target_is_directory=True)
        with self.assertRaises((ValueError, StoreError)):
            ShadowStore.initialize(replace(self.config, state_dir=bad))
        with self.assertRaises(ValueError): replace(self.config, mode='enforcing')

    def test_separate_cli_processes_shared_budget_and_replay(self):
        import json
        import subprocess
        import sys
        processes = [subprocess.Popen([sys.executable, '-B', '-m', 'hostgate.tests.support.client',
                                       str(self.config.state_dir), 'process-' + str(i)],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for i in range(8)]
        replies = []
        try:
            for process in processes:
                out, err = process.communicate(timeout=30)
                self.assertIn(process.returncode, (0, 1), err)
                replies.append(json.loads(out))
        finally:
            for process in processes:
                if process.poll() is None: process.wait(timeout=30)
        self.assertEqual(sum(r['wouldGrant'] for r in replies), 2)
        self.assertEqual(self.view()['budget']['retained']['slots'], 2)
        successful = next(r for r in replies if r['wouldGrant'])
        retry = subprocess.run([sys.executable, '-B', '-m', 'hostgate.tests.support.client',
                                str(self.config.state_dir), successful['operationId']],
                               capture_output=True, text=True, timeout=15)
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertTrue(json.loads(retry.stdout)['replayed'])
        self.assertFalse(json.loads(retry.stdout)['wouldGrant'])

    def test_replay_persists_expiry_without_renewing_or_regranting(self):
        original = self.reserve()
        before = self.view()['reservations'][0]['heartbeat']
        self.world.now += 40
        replay = self.reserve()
        self.assertFalse(replay['wouldGrant'])
        self.assertEqual(replay['originalResult'], original)
        view = self.view()
        self.assertEqual(view['reservations'][0]['state'], 'uncertain')
        self.assertEqual(view['reservations'][0]['heartbeat'], before)
        self.assertEqual(view['budget']['retained']['slots'], 1)

    def test_valid_checksum_does_not_authorize_invalid_rows(self):
        import copy
        import io
        import json
        from hostgate.cli import main
        from hostgate.protocol import canonical, digest
        self.reserve()
        valid = self.gate.store.read()
        changes = [lambda v: v['reservations'][0].update(state='invented'),
                   lambda v: v['reservations'][0].pop('owner'),
                   lambda v: v['reservations'][0]['key'].update(generation=True),
                   lambda v: v['pending'][0].update(reservationKey=None),
                   lambda v: v['generations'].update(job=99),
                   lambda v: v['reservations'][0]['owner'].update(startToken=[]),
                   lambda v: v['reservations'][0].update(heartbeat=10**400),
                   lambda v: v['pending'][0].update(heartbeat=10**400),
                   lambda v: v['pending'][0].update(created=10**400),
                   lambda v: v['pending'][0].update(deadline=10**400)]
        for change in changes:
            with self.subTest(change=change):
                view = copy.deepcopy(valid); change(view)
                with sqlite3.connect(str(self.gate.store.path)) as conn:
                    conn.execute('UPDATE host_view SET payload=?, checksum=?', (canonical(view), digest(view)))
                with self.assertRaises(StoreError): self.gate.store.read()
                out = io.StringIO()
                query = dict(schema=request()['schema'], operationId='state', operation='estado')
                code = main(['estado'], gate=self.gate, auth=self.auth,
                            stdin=io.StringIO(json.dumps(query)), stdout=out)
                self.assertEqual(code, 6)
                self.assertEqual(json.loads(out.getvalue())['reasons'][0]['code'], 'store-unavailable')
        with sqlite3.connect(str(self.gate.store.path)) as conn:
            conn.execute('UPDATE host_view SET payload=?, checksum=?', (canonical(valid), digest(valid)))

    def test_sqlite_full_io_and_secondary_cleanup_preserve_uncertainty(self):
        from unittest.mock import patch
        store = self.gate.store
        connect = store.connect
        for message, after_commit in (('database or disk is full', False), ('disk I/O error', False),
                                      ('disk I/O error', True)):
            class Connection:
                def __init__(self): self.conn = connect(writable=True)
                def execute(self, sql, *args):
                    if not after_commit and sql.startswith('UPDATE host_view'):
                        raise sqlite3.OperationalError(message)
                    return self.conn.execute(sql, *args)
                def commit(self):
                    self.conn.commit()
                    raise sqlite3.OperationalError(message)
                def rollback(self):
                    self.conn.rollback()
                    raise sqlite3.OperationalError('secondary rollback failure')
                def close(self):
                    self.conn.close()
                    raise sqlite3.OperationalError('secondary close failure')
            operation = 'storage-' + str(after_commit) + '-' + message
            with patch.object(store, 'connect', side_effect=lambda **kwargs: Connection()):
                reply = self.reserve(operation, operation)
            self.assertEqual(reply['exitCode'], 3)
            self.assertFalse(reply['wouldGrant'])
            replay = self.reserve(operation, operation)
            self.assertEqual(replay.get('replayed', False), after_commit)
            self.release(self.key(replay), 'release-' + operation)

    def test_digest_conflict_persists_expiry_without_changing_original(self):
        original = self.reserve()
        before = self.view()['reservations'][0]['heartbeat']
        self.world.now += 40
        conflict = self.gate.reservar(dict(request(), attemptId='other'), self.auth)
        self.assertEqual(conflict['exitCode'], 2)
        stored = self.gate.store.read()['reservations'][0]
        self.assertEqual(stored['state'], 'uncertain')
        self.assertEqual(stored['heartbeat'], before)
        replay = self.reserve()
        self.assertEqual(replay['originalResult'], original)
        self.assertFalse(replay['wouldGrant'])
        self.assertEqual(self.view()['budget']['retained']['slots'], 1)

    def test_state_reads_health_exactly_once(self):
        from unittest.mock import patch
        with patch.object(self.world, 'health', wraps=self.world.health) as health:
            state = self.view()
        self.assertEqual(health.call_count, 1)
        self.assertEqual(state['degraded'], bool(state['reasons']))

    def test_two_executors_shared_world_budget_and_observation_changes(self):
        import copy
        import json
        import subprocess
        import sys
        from dataclasses import replace
        from hostgate.model import identity_json
        from hostgate.tests.support.world import publish_world
        state = dict(now=100, boot=self.world.boot, executorBoots=self.world.executor_boots, processes={})
        for pid, executor in ((200, 'unix'), (300, 'other')):
            auth = self.world.auth(pid=pid, executor=executor)
            for identity in (auth.peer, auth.anchor): state['processes'][str(identity.pid)] = identity_json(identity)
        path = self.config.state_dir.parent / 'shared.json'
        publish_world(path, state)
        def argv(op, executor, pid, command='reservar'):
            return [sys.executable, '-B', '-m', 'hostgate.tests.support.client',
                    str(self.config.state_dir), op, executor, str(pid), str(path), command]
        processes = [subprocess.Popen(argv('executor-' + executor, executor, pid),
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for pid, executor in ((200, 'unix'), (300, 'other'))]
        replies = []
        for process in processes:
            out, err = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, err)
            replies.append(json.loads(out))
        self.assertTrue(all(r['wouldGrant'] for r in replies))
        def call(op, executor, pid, command='estado'):
            result = subprocess.run(argv(op, executor, pid, command), capture_output=True, text=True, timeout=30)
            return result.returncode, json.loads(result.stdout)
        for pid, executor in ((200, 'unix'), (300, 'other')):
            code, view = call('state-' + executor, executor, pid)
            self.assertEqual(code, 0)
            self.assertEqual(view['budget']['retained']['slots'], 2)
            self.assertEqual({r['executorId'] for r in view['reservations']}, {'unix', 'other'})
            self.assertIn('budget-exceeded', self.codes(call('third-' + executor, executor, pid, 'reservar')[1]))
        state['now'] += 11
        publish_world(path, state)
        for pid, executor in ((200, 'unix'), (300, 'other')):
            self.assertTrue(all(r['effectiveState'] == 'uncertain' for r in call('ttl-' + executor, executor, pid)[1]['reservations']))
        state['now'] = 100
        state['processes']['200']['startToken'] = [9, 9]
        publish_world(path, state)
        self.assertEqual(call('reused', 'unix', 200)[0], 5)
        observed = call('observes-reuse', 'other', 300)[1]['reservations']
        self.assertEqual(next(r for r in observed if r['executorId'] == 'unix')['effectiveState'], 'uncertain')
        state['processes']['200']['startToken'] = [1, 2]
        state['boot'] = 'new-physical'
        publish_world(path, state)
        for pid, executor in ((200, 'unix'), (300, 'other')):
            view = call('boot-' + executor, executor, pid)[1]
            self.assertTrue(view['drained'])
            self.assertEqual(view['budget']['retained']['slots'], 2)

    def test_trusted_initial_drain_cannot_be_reopened_by_query(self):
        from dataclasses import replace
        cfg = replace(self.config, state_dir=self.config.state_dir.parent / 'drained', drained=True)
        ShadowStore.initialize(cfg)
        gate = Hostgate(replace(cfg, drained=False), self.world)
        for _ in range(3): self.assertTrue(gate.estado({}, self.auth)['drained'])
        result = gate.reservar(request(), self.auth)
        self.assertIn('drained', self.codes(result))
        self.assertFalse(result['wouldGrant'])

    def test_corrupt_operation_is_store_unavailable_not_client_usage(self):
        self.reserve()
        with sqlite3.connect(str(self.gate.store.path)) as conn:
            conn.execute("UPDATE operations SET result='[]' WHERE operation_id='op'")
        result = self.reserve()
        self.assertEqual(result['exitCode'], 6)
        self.assertNotIn('originalResult', result)
        self.assertEqual(self.view()['budget']['retained']['slots'], 1)
