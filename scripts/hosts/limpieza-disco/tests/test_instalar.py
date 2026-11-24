"""Prueba la generación del LaunchAgent sin instalarlo."""
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest


DIRECTORIO = Path(__file__).resolve().parents[1]


class InstalarTests(unittest.TestCase):
    def test_plist_usa_home_remoto_y_conserva_el_job(self):
        template = DIRECTORIO / 'com.cosmonaut.limpieza-disco.plist'
        installer = (DIRECTORIO / 'instalar.sh').read_text()
        renderer = installer.split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'launch-agent.plist'
            home = '/Users/u'
            for account_home in (home, home + ' & equipo'):
                with self.subTest(home=account_home):
                    subprocess.run([sys.executable, '-I', '-', str(template), str(output), account_home],
                                   input=renderer, text=True, check=True)
                    self.assertEqual(plistlib.loads(output.read_bytes()), {
                        'Label': 'com.cosmonaut.limpieza-disco',
                        'ProgramArguments': ['/bin/bash', account_home + '/bin/limpieza-disco.sh'],
                        'StartInterval': 3600,
                        'RunAtLoad': True,
                        'LowPriorityIO': True,
                        'Nice': 10,
                        'StandardOutPath': account_home + '/Library/Logs/limpieza-disco.log',
                        'StandardErrorPath': account_home + '/Library/Logs/limpieza-disco.log',
                    })
