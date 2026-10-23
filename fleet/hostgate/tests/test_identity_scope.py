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
            path = Path(tmp) / 'owner.json'
            self.assertEqual(read_owner(path)['protection'], 'uncertain')
            record = dict(schema='fleet/host-owner@1', runId='r', jobId='j', attemptId='a',
                          key=dict(storeId='s', reservationId='r', generation=1), physicalHostId='node',
                          executorId='unix', boot='b', pid=1, start=[1, 2], started=1,
                          ended=None, root={'exit': 0, 'childAlive': True}, recordSeq=1,
                          scope={'status': 'unconfirmed'})
            publish_owner(record, path)
            self.assertEqual(read_owner(path)['protection'], 'protect')
            record['ended'] = 2
            with self.assertRaises(ValueError): publish_owner(record, path)
            path.write_text('{')
            self.assertEqual(read_owner(path)['protection'], 'uncertain')
