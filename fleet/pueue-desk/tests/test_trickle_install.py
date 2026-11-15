"""SEN-6417: install-desk.py crea el permiso una vez, lo conserva y respalda el controlador previo."""
from contextlib import redirect_stdout
import io
import json
import unittest
from test_startup import InstallerStartupTests, control

TABLE = 'CREATE TABLE guard_admission (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)'
EMPTY = dict(version=1, last_attempt=None, last_confirmed=None)


class TrickleInstallTests(unittest.TestCase):
    setUp = InstallerStartupTests.setUp
    fingerprint = InstallerStartupTests.fingerprint

    def without_permit(self):
        with self.engine.connect(write=True) as db:
            db.execute('DROP TABLE IF EXISTS guard_admission')  # autoridad anterior a SEN-6417

    def permit(self, body=None):
        with self.engine.connect(write=body is not None) as db:
            if body is not None:
                db.execute(TABLE.replace('CREATE TABLE', 'CREATE TABLE IF NOT EXISTS'))
                db.execute('INSERT OR REPLACE INTO guard_admission VALUES (1,?)', (json.dumps(body),))
            if not db.execute("SELECT count(*) FROM sqlite_master WHERE name='guard_admission'").fetchone()[0]:
                return None
            row = db.execute('SELECT body FROM guard_admission WHERE id=1').fetchone()
            return row and json.loads(row[0])

    def test_dry_run_reports_the_permit_plan_without_writing(self):
        self.without_permit()
        before = self.fingerprint()
        out = io.StringIO()
        with redirect_stdout(out):
            self.installer.install(self.home, self.pid, control, dry_run=True)
        self.assertIn('SECO, permiso de admisión de a una: crear guard_admission vacía', out.getvalue())
        self.assertEqual(self.fingerprint(), before)
        self.assertIsNone(self.permit())

    def test_install_creates_empty_permit_and_reinstall_keeps_it(self):
        self.without_permit()
        with redirect_stdout(io.StringIO()):
            self.installer.install(self.home, self.pid, control)
        self.assertEqual(self.permit(), EMPTY, 'La instalación crea el permiso vacío')
        kept = dict(EMPTY, last_confirmed=dict(utc='2026-11-14T13:00:00+00:00', grupo='jale', id=7))
        self.permit(kept)
        with redirect_stdout(io.StringIO()):
            self.installer.install(self.home, self.pid, control)
        self.assertEqual(self.permit(), kept, 'Reinstalar no reinicializa el permiso')

    def test_corrupt_permit_stops_before_effects_and_in_dry_run(self):
        self.permit(dict(version=2))
        before = self.fingerprint()
        for dry in (True, False):
            with self.subTest(dry=dry), redirect_stdout(io.StringIO()):
                rejected = False
                try:
                    self.installer.install(self.home, self.pid, control, dry_run=dry)
                except ValueError:
                    rejected = True
                self.assertTrue(rejected, 'Un permiso ilegible detiene la instalación')
            self.assertEqual(self.fingerprint(), before)

    def test_dated_backup_keeps_the_immediately_previous_controller(self):
        (self.library / 'control.py.pre-u34').write_text('# controlador U3.4\n')
        with redirect_stdout(io.StringIO()):
            self.installer.install(self.home, self.pid, control)
        dated = sorted(self.library.glob('control.py.pre-2*'))
        self.assertEqual(len(dated), 1)
        self.assertRegex(dated[0].name, r'^control\.py\.pre-[0-9]{8}T[0-9]{6}Z$')
        self.assertEqual(dated[0].read_text(), '# previous controller\n')
        self.assertEqual((self.library / 'control.py.pre-u34').read_text(), '# controlador U3.4\n')


del InstallerStartupTests

if __name__ == '__main__':
    unittest.main()
