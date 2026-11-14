"""Opt-in adoption/rollback gates against a synthetic launchd, PID file and Pueue."""
import io
import json
from pathlib import Path
import subprocess
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import test_pueue_mini as legacy
from test_slots import control

installer = legacy.installer


class SlotsInstallTests(unittest.TestCase):
    def setUp(self):
        self.fake = legacy.InstallerTests()
        self.fake.setUp(); self.addCleanup(self.fake.doCleanups)
        self.root = self.fake.root
        self.state = self.root/'.local/state/pao/pueue-mini'
        self.pid = self.root/'Library/Application Support/pueue/pueue.pid'
        self.pid.parent.mkdir(parents=True);self.pid.write_text('999\n')
        self.fake.write_state(dict(tasks={},groups={g:dict(status='Running',parallel_tasks=n)
                                  for g,n in control.MINI_PROFILE.items()}))
        self.bootstrap_switch = []
        def run(argv,check=True,**kwargs):
            if argv[0] == '/bin/ps':
                self.assertEqual(argv,['/bin/ps','-p','999','-o','lstart=','-o','comm='])
                return subprocess.CompletedProcess(argv,0,'Fri Nov 13 11:00:00 2026 /opt/homebrew/bin/pueued\n','')
            if argv[0] == installer.LAUNCHCTL and argv[1] == 'bootstrap':
                self.bootstrap_switch.append((self.state/'slots.disabled').exists())
            return self.fake.fake_run(argv,check,**kwargs)
        mocked=patch.object(installer.subprocess,'run',side_effect=run)
        mocked.start();self.addCleanup(mocked.stop)

    def engine(self):
        return control.MiniControl(control.Pueue(installer.PUEUE),self.state)

    def test_opt_in_is_readonly_in_seco(self):
        before={str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with redirect_stdout(io.StringIO()): self.fake.install('instalar','--cuotas','--seco')
        self.assertEqual(self.fake.actions,[])
        self.assertEqual({str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()},before)

    def test_profile_mismatch_refuses_before_bootout_or_writes(self):
        state=json.loads((self.root/'pueue.json').read_text());state['groups']['rapidas']['parallel_tasks']=1
        self.fake.write_state(state)
        with self.assertRaises(ValueError):self.fake.install('instalar','--cuotas')
        self.assertFalse(self.state.exists())
        self.assertFalse(any(a[1] == 'bootout' for a in self.fake.actions))

    def test_atomic_adoption_enables_only_after_bootstrap_and_reinstall_preserves_receipts(self):
        self.fake.install('instalar','--cuotas')
        self.assertEqual(self.bootstrap_switch,[True])
        self.assertFalse((self.state/'slots.disabled').exists())
        view=self.engine().view();self.assertTrue(view['capabilities']['slots'])
        before=(self.state/'control.sqlite3').read_bytes()
        self.fake.install('instalar')  # adoption remains opt-in across later upgrades
        self.assertEqual((self.state/'control.sqlite3').read_bytes(),before)
        self.assertTrue(self.fake.service_loaded)
        self.assertFalse(any(a[1] in ('parallel','start') for a in self.fake.actions))

    def test_failed_bootstrap_retains_disable_and_never_opens_pauses_directly(self):
        self.fake.failing={'bootstrap'}
        with self.assertRaises(Exception) as raised:self.fake.install('instalar','--cuotas')
        self.assertIsInstance(raised.exception,RuntimeError)
        self.assertTrue((self.state/'slots.disabled').exists())
        self.assertFalse(self.engine().view()['capabilities']['slots'])
        self.assertFalse(any(a[1] == 'start' for a in self.fake.actions))
        self.assertTrue((self.state/'control.adopted').exists())

    def test_existing_switch_is_preserved_and_uninstall_cannot_discard_desire(self):
        self.state.mkdir(parents=True);(self.state/'slots.disabled').touch()
        self.fake.install('instalar','--cuotas')
        self.assertTrue((self.state/'slots.disabled').exists())
        self.fake.actions.clear()
        with self.assertRaises(BaseException) as raised:self.fake.install('desinstalar')
        self.assertIsInstance(raised.exception,SystemExit)
        self.assertEqual(self.fake.actions,[])
        self.assertTrue((self.state/'control.sqlite3').exists())


if __name__=='__main__':unittest.main()
