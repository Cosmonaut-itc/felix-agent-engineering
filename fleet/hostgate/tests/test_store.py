import concurrent.futures
import hashlib
import sqlite3
from hostgate.tests.test_contract import Harness
from hostgate.tests.support.world import request
from hostgate.admission import Hostgate
from hostgate.store import ShadowStore, StoreError


class Storage(Harness):
    def test_concurrent_budget_and_parallel_replay(self):
        def reserve(i):
            return Hostgate(self.config, self.world).reservar(request('parallel-' + str(i), str(i)), self.auth)
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
        db.unlink()
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
                out, err = process.communicate(timeout=15)
                self.assertIn(process.returncode, (0, 1, 4), err)
                replies.append(json.loads(out))
        finally:
            for process in processes:
                if process.poll() is None: process.wait(timeout=15)
        self.assertEqual(sum(r['wouldGrant'] for r in replies), 2)
        self.assertEqual(self.view()['budget']['retained']['slots'], 2)
        successful = next(r for r in replies if r['wouldGrant'])
        retry = subprocess.run([sys.executable, '-B', '-m', 'hostgate.tests.support.client',
                                str(self.config.state_dir), successful['operationId']],
                               capture_output=True, text=True, timeout=15)
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertTrue(json.loads(retry.stdout)['replayed'])
        self.assertFalse(json.loads(retry.stdout)['wouldGrant'])
