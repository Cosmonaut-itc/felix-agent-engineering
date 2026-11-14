import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'instalar.sh'
TOKEN = 'token-de-prueba'
RUTA = '/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin'

CONFIG = '''#!/bin/bash
{ printf 'args=%s\\n' "$*"; printf 'token=%s\\n' "$ACTIONS_RUNNER_INPUT_TOKEN"; printf 'path=%s\\n' "$PATH"; } >> "$FAKE_ROOT/config.log"
if [ "$1" = remove ]; then [ ! -e "$FAKE_ROOT/sin-registro" ] && rm .runner; else : > .runner; fi
'''
STUBS = {
    'uname': '#!/bin/bash\ncat "$FAKE_ROOT/uname"\n',
    'sleep': '#!/bin/bash\n',
    'curl': '#!/bin/bash\necho "$*" >> "$FAKE_ROOT/curl.log"\ncp "$FAKE_ROOT/paquete.tgz" "$3"\n',
    'shasum': '#!/bin/bash\ncat > "$FAKE_ROOT/shasum.in"\n[ ! -e "$FAKE_ROOT/sha-mal" ]\n',
    'launchctl': '''#!/bin/bash
echo "$*" >> "$FAKE_ROOT/launchctl.log"
case "$1" in
  print) [ -e "$FAKE_ROOT/cargado" ] ;;
  bootout) rm "$FAKE_ROOT/cargado" ;;
  bootstrap)
    : > "$FAKE_ROOT/cargado"
    [ -e "$FAKE_ROOT/mudo" ] || echo 'Listening for Jobs' >> "$HOME/Library/Logs/pao-gha-runner-jale-guard.log" ;;
esac
''',
}


class Instalador(unittest.TestCase):
    def setUp(self):
        temporal = tempfile.TemporaryDirectory()
        self.addCleanup(temporal.cleanup)
        self.raiz = Path(temporal.name)
        self.home = self.raiz / 'home'
        self.fake = self.raiz / 'fake'
        stubs = self.raiz / 'stubs'
        for carpeta in (self.home, self.fake, stubs):
            carpeta.mkdir()
        for nombre, cuerpo in STUBS.items():
            (stubs / nombre).write_text(cuerpo)
            (stubs / nombre).chmod(0o755)
        (self.fake / 'uname').write_text('Darwin arm64\n')
        origen = self.raiz / 'origen'
        (origen / 'bin').mkdir(parents=True)
        (origen / 'config.sh').write_text(CONFIG)
        (origen / 'config.sh').chmod(0o755)
        (origen / 'bin/runsvc.sh').write_text('#!/bin/bash\n# runsvc de prueba\n')
        with tarfile.open(self.fake / 'paquete.tgz', 'w:gz') as paquete:
            paquete.add(origen / 'config.sh', 'config.sh')
            paquete.add(origen / 'bin', 'bin')
        self.entorno = dict(os.environ, HOME=str(self.home), FAKE_ROOT=str(self.fake),
                            PATH=f"{stubs}:{os.environ['PATH']}", PAO_GHA_RUNNER_ESPERA='2')
        self.dir = self.home / 'actions-runner-jale-guard'
        self.plist = self.home / 'Library/LaunchAgents/local.pao.gha-runner-jale-guard.plist'

    def correr(self, *args, token=None, papel='guard'):
        return subprocess.run(['bash', str(SCRIPT), *args, *([papel] if papel else [])], env=self.entorno,
                              text=True, capture_output=True,
                              **({'input': token + '\n'} if token else {'stdin': subprocess.DEVNULL}))

    def leer(self, nombre):
        ruta = self.fake / nombre
        return ruta.read_text() if ruta.exists() else ''

    def test_instala_y_registra_con_el_token_fuera_de_argv(self):
        resultado = self.correr(token=TOKEN)
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertIn('escuchando', resultado.stdout)
        registro = self.leer('config.log')
        self.assertIn('--url https://github.com/SEN-HASO/Jale', registro)
        self.assertIn('--name mini-jale-guard --labels jale-guard', registro)
        self.assertIn(f'token={TOKEN}\n', registro)
        self.assertIn(f'path={RUTA}\n', registro)
        self.assertNotIn(TOKEN, registro.splitlines()[0])
        self.assertNotIn(TOKEN, resultado.stdout + resultado.stderr)
        # La actualización del runner localiza el servicio por <raíz>/runsvc.sh.
        self.assertIn(f'<string>{self.dir}/runsvc.sh</string>', self.plist.read_text())
        self.assertEqual((self.dir / 'runsvc.sh').read_text(), '#!/bin/bash\n# runsvc de prueba\n')
        self.assertTrue(os.access(self.dir / 'runsvc.sh', os.X_OK))
        self.assertIn(f'bootstrap gui/{os.getuid()} {self.plist}', self.leer('launchctl.log'))
        self.assertEqual(list(self.dir.glob('*.tar.gz')), [])

    def test_reinstalar_no_descarga_ni_registra_otra_vez(self):
        self.assertEqual(self.correr(token=TOKEN).returncode, 0)
        resultado = self.correr()
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        # El arranque anterior ya dejó «Listening» en el log: sólo cuenta lo que escribe éste.
        (self.fake / 'mudo').touch()
        self.assertEqual(self.correr().returncode, 1)
        (self.fake / 'mudo').unlink()
        self.assertEqual(self.correr().returncode, 0)
        self.assertEqual(len(self.leer('curl.log').splitlines()), 1)
        self.assertEqual(self.leer('config.log').count('args='), 1)
        self.assertEqual(self.leer('launchctl.log').count('bootout'), 3)
        self.assertEqual(self.leer('launchctl.log').count('bootstrap'), 4)

    def test_un_paquete_alterado_no_se_extrae(self):
        (self.fake / 'sha-mal').touch()
        resultado = self.correr(token=TOKEN)
        self.assertEqual(resultado.returncode, 1)
        self.assertIn('sha256', resultado.stderr)
        self.assertIn('25a065af409ffa317ab4d5a3b614dd15fc9c40f2172bc1b35a1b1258e919a388', self.leer('shasum.in'))
        self.assertFalse((self.dir / 'config.sh').exists())
        self.assertEqual(self.leer('launchctl.log'), '')

    def test_sin_token_no_descarga(self):
        resultado = self.correr()
        self.assertEqual(resultado.returncode, 2)
        self.assertIn('token de registro', resultado.stderr)
        self.assertEqual(self.leer('curl.log'), '')

    def test_otro_sistema_no_instala(self):
        (self.fake / 'uname').write_text('Linux x86_64\n')
        resultado = self.correr(token=TOKEN)
        self.assertEqual(resultado.returncode, 1)
        self.assertIn('macOS arm64', resultado.stderr)
        self.assertEqual(self.leer('curl.log'), '')

    def test_seco_no_escribe(self):
        resultado = self.correr('--seco')
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertEqual(sorted(p.name for p in self.fake.iterdir()), ['paquete.tgz', 'uname'])

    def test_un_runner_que_no_escucha_falla_con_la_ruta_del_log(self):
        (self.fake / 'mudo').touch()
        resultado = self.correr(token=TOKEN)
        self.assertEqual(resultado.returncode, 1)
        self.assertIn('pao-gha-runner-jale-guard.log', resultado.stderr)

    def test_retirar_da_de_baja_y_conserva_los_archivos(self):
        self.assertEqual(self.correr(token=TOKEN).returncode, 0)
        resultado = self.correr('--retirar', token='token-de-retiro')
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertIn('args=remove\ntoken=token-de-retiro\n', self.leer('config.log'))
        self.assertFalse((self.fake / 'cargado').exists())
        self.assertFalse(self.dir.exists())
        self.assertFalse(self.plist.exists())
        self.assertEqual(len(list(self.home.glob('actions-runner-jale-guard.retirado-*'))), 1)
        self.assertEqual(len(list(self.plist.parent.glob('*.plist.retirado-*'))), 1)

    def test_retirar_termina_aunque_github_ya_no_tenga_el_registro(self):
        self.assertEqual(self.correr(token=TOKEN).returncode, 0)
        (self.fake / 'sin-registro').touch()
        resultado = self.correr('--retirar', token='token-de-retiro')
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertIn('no confirmó la baja', resultado.stderr)
        self.assertFalse(self.dir.exists())
        (self.fake / 'sin-registro').unlink()
        self.assertEqual(self.correr(token=TOKEN).returncode, 0)
        self.assertIn('--replace', self.leer('config.log').splitlines()[-3])

    def test_retirar_sin_registro_local_avisa_que_github_puede_tenerlo(self):
        self.dir.mkdir()
        resultado = self.correr('--retirar')
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertIn('bórralo en GitHub', resultado.stderr)
        self.assertEqual(self.leer('config.log'), '')
        self.assertEqual(len(list(self.home.glob('actions-runner-jale-guard.retirado-*'))), 1)

    def test_retirar_sin_token_no_descarga_el_servicio(self):
        self.assertEqual(self.correr(token=TOKEN).returncode, 0)
        resultado = self.correr('--retirar')
        self.assertEqual(resultado.returncode, 2)
        self.assertTrue((self.fake / 'cargado').exists())

    def test_cada_papel_tiene_su_registro_y_su_directorio(self):
        (self.fake / 'mudo').touch()
        self.assertEqual(self.correr(token=TOKEN, papel='datos').returncode, 1)
        self.assertIn('--name mini-jale-datos --labels jale-datos', self.leer('config.log'))
        self.assertTrue((self.home / 'actions-runner-jale-datos/.runner').exists())
        self.assertFalse(self.dir.exists())

    def test_argumentos_invalidos(self):
        for args in (['--otro', 'guard'], ['--seco', '--retirar', 'guard'], ['guard', 'datos'], ['otro'], []):
            resultado = subprocess.run(['bash', str(SCRIPT), *args], env=self.entorno, text=True,
                                       capture_output=True, stdin=subprocess.DEVNULL)
            self.assertEqual(resultado.returncode, 2, args)
            self.assertEqual(list(self.home.iterdir()), [], args)


if __name__ == '__main__':
    unittest.main()
