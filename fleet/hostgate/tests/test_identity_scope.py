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

    def test_helpers_real_eof_linked_and_legacy_survives_then_autoexits(self):
        import subprocess
        import sys
        import time
        script = Path(__file__).parent / 'support/helper.py'
        for mode in ('linked', 'legacy'):
            with tempfile.TemporaryDirectory() as tmp:
                result = subprocess.run([sys.executable, '-B', str(script), 'parent-' + mode, tmp],
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                identities = json.loads(result.stdout)
                self.assertNotEqual(identities['parent'], identities['helper'])
                ready, ended = Path(tmp) / 'helper-ready', Path(tmp) / 'helper-ended'
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline: time.sleep(0.02)
                self.assertTrue(ready.exists())
                if mode == 'legacy': self.assertFalse(ended.exists())
                while not ended.exists() and time.monotonic() < deadline: time.sleep(0.02)
                self.assertEqual(ended.read_text(), mode)

    def test_owner_fsync_failure_preserves_protection(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'owner.json'
            record = dict(schema='fleet/host-owner@1', runId='r', jobId='j', attemptId='a',
                          key=dict(storeId='s', reservationId='r', generation=1), physicalHostId='node',
                          executorId='unix', boot='b', pid=1, start=[1], started=1,
                          ended=None, root=None, recordSeq=1, scope={'status': 'unconfirmed'})
            with patch('hostgate.owner_record.os.fsync', side_effect=OSError(28, 'fixture full')):
                with self.assertRaises(OSError): publish_owner(record, path)
            self.assertEqual(read_owner(path)['protection'], 'uncertain')
            self.assertEqual(list(Path(tmp).iterdir()), [])
