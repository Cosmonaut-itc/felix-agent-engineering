import ctypes
import json
import os
import tempfile
import unittest
from pathlib import Path
from hostgate.identity import ProcessTable, Identity, parse_linux_stat, DarwinBSDInfo
from hostgate.owner_record import publish_owner, read_owner


class Boundaries(unittest.TestCase):
    def test_linux_exact_stat_parentheses(self):
        stat = '42 (name ) ( with spaces) S 2 3 4 ' + '0 ' * 15 + '9999999 0'
        self.assertEqual(parse_linux_stat(stat), ('S', 2, 3, 4, (9999999,)))
        with self.assertRaises(ValueError): parse_linux_stat('partial')

    def test_darwin_structure_and_live_identity_read_only(self):
        self.assertEqual(ctypes.sizeof(DarwinBSDInfo), 136)
        table = ProcessTable('unix', host='Mini-Fixture')
        observation = table.identity(os.getpid())
        self.assertEqual(observation.status, 'same')
        self.assertEqual(table.verify(observation.identity).status, 'same')
        self.assertTrue(observation.identity.start_token)

    def test_owner_record_partial_and_root_terminal_protect(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / 'owner.json'
            self.assertEqual(read_owner(path)['protection'], 'uncertain')
            record = dict(schema='fleet/host-owner@1', runId='r', jobId='j', attemptId='a',
                          key=dict(storeId='s', reservationId='r', generation=1), physicalHostId='node',
                          executorId='unix', boot='b', pid=1, start=[1, 2], started=1,
                          ended=None, root={'pid': 2, 'start': [2, 3], 'exit': 0, 'childAlive': True}, recordSeq=1,
                          scope={'status': 'unconfirmed'})
            publish_owner(record, path)
            self.assertEqual(read_owner(path)['protection'], 'protect')
            record['ended'] = 2
            with self.assertRaises(ValueError): publish_owner(record, path)
            path.write_text('{')
            self.assertEqual(read_owner(path)['protection'], 'uncertain')

    def test_owner_fsync_failure_preserves_protection(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / 'owner.json'
            record = dict(schema='fleet/host-owner@1', runId='r', jobId='j', attemptId='a',
                          key=dict(storeId='s', reservationId='r', generation=1), physicalHostId='node',
                          executorId='unix', boot='b', pid=1, start=[1], started=1,
                          ended=None, root=None, recordSeq=1, scope={'status': 'unconfirmed'})
            with patch('hostgate.owner_record.os.fsync', side_effect=OSError(28, 'fixture full')):
                with self.assertRaises(OSError): publish_owner(record, path)
            self.assertEqual(read_owner(path)['protection'], 'uncertain')
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_linux_proc_fixture_exact_token_reuse_zombie_and_missing(self):
        from unittest.mock import patch
        from dataclasses import replace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            boot = root / 'sys/kernel/random'
            boot.mkdir(parents=True)
            (boot / 'boot_id').write_text('boot-1')
            process = root / '42'
            process.mkdir()
            stat = '42 (fixture ) name) S 2 3 4 ' + '0 ' * 15 + '9999999 0'
            (process / 'stat').write_text(stat)
            (process / 'status').write_text('Uid:\t501\t501\t501\t501\n')
            table = ProcessTable('unix', 'host', root)
            with patch('hostgate.identity.sys.platform', 'linux'):
                observed = table.identity(42)
                self.assertEqual(observed.status, 'same')
                self.assertEqual(observed.identity.start_token, (9999999,))
                self.assertEqual(table.verify(replace(observed.identity, start_token=(9999998,))).status,
                                 'confirmed_absent_or_different')
                (process / 'stat').write_text(stat.replace(' S ', ' Z '))
                self.assertEqual(table.identity(42).status, 'confirmed_absent_or_different')
                self.assertEqual(table.identity(99).status, 'confirmed_absent_or_different')
                (process / 'stat').write_text('partial')
                self.assertEqual(table.identity(42).status, 'unverifiable')

    def test_owner_root_identity_must_be_exact_and_separate(self):
        from hostgate.owner_record import _validate
        record = dict(schema='fleet/host-owner@1', runId='r', jobId='j', attemptId='a',
                      key=dict(storeId='s', reservationId='r', generation=1), physicalHostId='node',
                      executorId='unix', boot='b', pid=1, start=[1, 2], started=1,
                      ended=None, root=None, recordSeq=1, scope={'status': 'unconfirmed'})
        for root in ('root', {}, {'pid': 1, 'start': [2]}, {'pid': 2},
                     {'pid': True, 'start': [2]}, {'pid': 2, 'start': []},
                     {'pid': 2, 'start': [True]}):
            with self.subTest(root=root), self.assertRaises(ValueError):
                _validate(dict(record, root=root))
        self.assertEqual(_validate(dict(record, root={'pid': 2, 'start': [2, 3]}))['ended'], None)

    def test_directory_fsync_after_replace_keeps_published_protection(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / 'owner.json'
            record = dict(schema='fleet/host-owner@1', runId='r', jobId='j', attemptId='a',
                          key=dict(storeId='s', reservationId='r', generation=1), physicalHostId='node',
                          executorId='unix', boot='b', pid=1, start=[1, 2], started=1,
                          ended=None, root={'pid': 2, 'start': [2, 3]}, recordSeq=1,
                          scope={'status': 'unconfirmed'})
            publish_owner(record, path)
            with patch('hostgate.owner_record.os.fsync', side_effect=[None, OSError(5, 'directory I/O')]):
                with self.assertRaises(OSError): publish_owner(dict(record, recordSeq=2), path)
            self.assertEqual(read_owner(path)['protection'], 'protect')
            self.assertIsNone(read_owner(path)['record']['ended'])
            self.assertEqual([p.name for p in path.parent.iterdir()], ['owner.json'])

    def test_owner_rejects_production_symlink_and_stale_sequence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            link = root / 'link'; link.symlink_to(root, target_is_directory=True)
            record = dict(schema='fleet/host-owner@1', runId='r', jobId='j', attemptId='a',
                          key=dict(storeId='s', reservationId='r', generation=1), physicalHostId='node',
                          executorId='unix', boot='b', pid=1, start=[1, 2], started=1,
                          ended=None, root=None, recordSeq=2, scope={'status': 'unconfirmed'})
            with self.assertRaises(ValueError): publish_owner(record, link / 'owner.json')
            publish_owner(record, root / 'owner.json')
            with self.assertRaises(ValueError): publish_owner(dict(record, recordSeq=1), root / 'owner.json')
            self.assertEqual(read_owner(root / 'owner.json')['record']['recordSeq'], 2)
