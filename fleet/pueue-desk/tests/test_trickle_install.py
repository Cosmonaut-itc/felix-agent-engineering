"""Additive permit migration through the actual installer and private host double."""
import test_generation as generation_tests
from test_slots import control
import unittest

class AdmissionInstallTests(unittest.TestCase):
    def setUp(self):
        self.f=generation_tests.GenerationInstallTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        # Exercise current-generation deployment while pressure pauses are retained.
        self.f.save(dict(self.f.state(),generation=self.f.generation()))

    def table(self):
        with self.f.engine.connect() as db:
            exists=db.execute("SELECT name FROM sqlite_master WHERE name='guard_admission'").fetchone()
            self.assertIsNotNone(exists,'Installer must add the durable permit')
            return db.execute('SELECT body FROM guard_admission').fetchone()[0]

    def test_seco_missing_table_readonly_then_installs_and_preserves(self):
        with self.f.engine.connect(write=True) as db:db.execute('DROP TABLE IF EXISTS guard_admission')
        before=self.f.fingerprint();out=self.f.install(True)
        self.assertEqual(before,self.f.fingerprint())
        self.assertIn('guard_admission',out)
        self.f.install();row=self.table()
        self.f.install();self.assertEqual(self.table(),row)
        self.assertGreaterEqual(len(list(self.f.library.glob('control.py.pre-sen6417-*'))),2)
        self.assertEqual(self.f.p.calls,[])

    def test_corrupt_permit_refuses_before_publication(self):
        self.table()
        with self.f.engine.connect(write=True) as db:db.execute("UPDATE guard_admission SET body='{}'")
        before=self.f.fingerprint()
        with self.assertRaises((ValueError,KeyError)):self.f.install()
        self.assertEqual(before,self.f.fingerprint())

if __name__=='__main__':unittest.main()
