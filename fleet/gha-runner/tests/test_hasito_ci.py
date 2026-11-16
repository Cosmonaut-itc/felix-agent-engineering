import tarfile
import unittest

import test_instalar as base

TOKEN = base.TOKEN

LINUX_SHA = 'baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40'
LINUX_PATH = '/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin'
MAC_PATH = '/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin'
CONFIG = '''#!/bin/bash
printf 'args=%s\\npath=%s\\n' "$*" "$PATH" >> "$FAKE_ROOT/config.log"
[ "$ACTIONS_RUNNER_INPUT_TOKEN" = "$EXPECTED_TOKEN" ] || exit 9
[ ! -e "$FAKE_ROOT/config-falla" ] || exit 7
if [ "$1" = remove ]; then rm .runner; else : > .runner; fi
'''
SYSTEMCTL = '''#!/bin/bash
printf '%s\\n' "$*" >> "$FAKE_ROOT/systemctl.log"
case "$2" in
  is-active) [ -e "$FAKE_ROOT/cargado" ] ;;
  stop) rm -f "$FAKE_ROOT/cargado" ;;
  enable)
    : > "$FAKE_ROOT/cargado"
    [ ! -e "$FAKE_ROOT/mudo" ] || exit 0
    [ ! -e "$FAKE_ROOT/arranque-falla" ] || exit 8
    echo 'Listening for Jobs' >> "$HOME/.local/state/pao-gha-runner/hasito-ci.log" ;;
esac
'''


class HasitoCI(unittest.TestCase):
    setUp = base.Instalador.setUp
    correr = base.Instalador.correr
    leer = base.Instalador.leer

    def preparar(self, sistema):
        (self.fake / 'uname').write_text(sistema + '\n')
        self.entorno['EXPECTED_TOKEN'] = TOKEN
        stubs = self.raiz / 'stubs'
        for nombre, cuerpo in {
            'systemctl': SYSTEMCTL,
            'dpkg-query': '''#!/bin/bash
case "$(cat "$FAKE_ROOT/faltantes" 2>/dev/null)" in *"${@: -1}"*) exit 1 ;; esac
printf 'install ok installed'
''',
            'ldconfig': '''#!/bin/bash
cat "$FAKE_ROOT/bibliotecas"
''',
        }.items():
            (stubs / nombre).write_text(cuerpo)
            (stubs / nombre).chmod(0o755)
        (self.fake / 'bibliotecas').write_text('\n'.join(
            f'  {lib} (libc6,x86-64) => /lib/{lib}' for lib in
            ('libc.so.6', 'libgcc_s.so.1', 'libstdc++.so.6', 'libgssapi_krb5.so.2',
             'libkrb5.so.3', 'libicuuc.so.74', 'libicui18n.so.74', 'libicudata.so.74',
             'libssl.so.3', 'libcrypto.so.3', 'libz.so.1', 'liblttng-ust.so.1')))
        config = self.raiz / 'origen/config.sh'
        config.write_text(CONFIG)
        with tarfile.open(self.fake / 'paquete.tgz', 'w:gz') as paquete:
            paquete.add(config, 'config.sh')
            paquete.add(self.raiz / 'origen/bin', 'bin')
        self.dir = self.home / 'actions-runner-hasito-ci'
        self.servicio = (self.home / 'Library/LaunchAgents/local.pao.gha-runner-hasito-ci.plist'
                         if sistema == 'Darwin arm64' else
                         self.home / '.config/systemd/user/local.pao.gha-runner-hasito-ci.service')

    def instalar(self):
        return self.correr(token=TOKEN, papel='hasito-ci')

    def test_seco_en_ambos_sistemas_anuncia_sin_escribir(self):
        for sistema, paquete, nombre in (
                ('Darwin arm64', 'osx-arm64', 'mini-hasito-ci'),
                ('Linux x86_64', 'linux-x64', 'desk-hasito-ci')):
            with self.subTest(sistema=sistema):
                self.preparar(sistema)
                directorios = {p for p in self.raiz.rglob('*') if p.is_dir()}
                antes = {p: p.read_bytes() for p in self.raiz.rglob('*') if p.is_file()}
                resultado = self.correr('--seco', papel='hasito-ci')
                self.assertEqual(resultado.returncode, 0, resultado.stderr)
                self.assertIn(f'actions-runner-{paquete}-2.338.2.tar.gz', resultado.stdout)
                self.assertIn(f'Registrar {nombre}', resultado.stdout)
                self.assertIn(str(self.servicio), resultado.stdout)
                self.assertNotIn('HOOK_JOB_STARTED', resultado.stdout)
                despues = {p: p.read_bytes() for p in self.raiz.rglob('*') if p.is_file()}
                self.assertEqual(antes, despues)
                self.assertEqual(directorios, {p for p in self.raiz.rglob('*') if p.is_dir()})
                print(f'\nSECO {sistema}:\n{resultado.stdout}', end='')

    def test_registro_token_fuera_de_argv_y_archivos_y_path_fijo(self):
        for sistema, ruta, nombre in (('Darwin arm64', MAC_PATH, 'mini-hasito-ci'),
                                      ('Linux x86_64', LINUX_PATH, 'desk-hasito-ci')):
            with self.subTest(sistema=sistema):
                self.preparar(sistema)
                resultado = self.instalar()
                self.assertEqual(resultado.returncode, 0, resultado.stderr)
                self.assertIn('--url https://github.com/SEN-HASO/Hasito', self.leer('config.log'))
                self.assertIn(f'--name {nombre} --labels hasito-ci', self.leer('config.log'))
                self.assertIn(f'path={ruta}', self.leer('config.log'))
                self.assertNotIn(TOKEN, resultado.stdout + resultado.stderr)
                for archivo in self.raiz.rglob('*'):
                    if archivo.is_file():
                        self.assertNotIn(TOKEN.encode(), archivo.read_bytes(), archivo)
                self.assertEqual((self.dir / '.path').read_text(), ruta + '\n')
                self.assertIn(ruta, self.servicio.read_text())
                self.assertNotIn('HOOK_JOB_STARTED', self.servicio.read_text())
                self.assertFalse((self.home / '.local/lib/pao-gha-runner').exists())
                if (self.dir / '.env').exists():
                    self.assertNotIn('HOOK_JOB_STARTED', (self.dir / '.env').read_text())
                self.assertEqual(self.correr('--retirar', token=TOKEN, papel='hasito-ci').returncode, 0)

    def test_unidad_linux_prioridad_path_y_servicio_de_usuario(self):
        self.preparar('Linux x86_64')
        resultado = self.instalar()
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        unidad = self.servicio.read_text()
        for linea in ('Nice=10', 'CPUWeight=10',
                      f'Environment="PATH={LINUX_PATH}"', 'WantedBy=default.target',
                      f'ExecStart="{self.dir}/runsvc.sh"', 'KillMode=control-group'):
            self.assertIn(linea, unidad)
        self.assertNotIn('IOWeight=', unidad)
        self.assertNotIn('IOSchedulingClass=', unidad)
        self.assertIn('--user daemon-reload', self.leer('systemctl.log'))
        self.assertIn('--user enable --now', self.leer('systemctl.log'))
        self.assertNotIn('svc.sh', self.leer('systemctl.log'))

    def test_reinstalar_no_registra_y_corrige_path_en_ambos_sistemas(self):
        for sistema in ('Darwin arm64', 'Linux x86_64'):
            with self.subTest(sistema=sistema):
                self.preparar(sistema)
                self.assertEqual(self.instalar().returncode, 0)
                descarga = self.leer('curl.log')
                registro = self.leer('config.log')
                (self.dir / '.path').write_text('/ruta/del/shell\n')
                resultado = self.correr(papel='hasito-ci')
                self.assertEqual(resultado.returncode, 0, resultado.stderr)
                self.assertEqual(self.leer('curl.log'), descarga)
                self.assertEqual(self.leer('config.log'), registro)
                self.assertNotIn('/ruta/del/shell', (self.dir / '.path').read_text())
                self.assertEqual(self.correr('--retirar', token=TOKEN, papel='hasito-ci').returncode, 0)

    def test_retirar_renombra_y_conserva_en_ambos_sistemas(self):
        for sistema in ('Darwin arm64', 'Linux x86_64'):
            with self.subTest(sistema=sistema):
                self.preparar(sistema)
                self.assertEqual(self.instalar().returncode, 0)
                contenido = self.servicio.read_bytes()
                (self.dir / 'evidencia').write_text('conservar')
                resultado = self.correr('--retirar', token=TOKEN, papel='hasito-ci')
                self.assertEqual(resultado.returncode, 0, resultado.stderr)
                self.assertIn('args=remove', self.leer('config.log'))
                for archivo in self.raiz.rglob('*'):
                    if archivo.is_file():
                        self.assertNotIn(TOKEN.encode(), archivo.read_bytes(), archivo)
                self.assertFalse((self.fake / 'cargado').exists())
                self.assertFalse(self.dir.exists())
                self.assertFalse(self.servicio.exists())
                retirados = list(self.home.glob('actions-runner-hasito-ci.retirado-*'))
                self.assertEqual(len(retirados), 1)
                self.assertEqual((retirados[0] / 'evidencia').read_text(), 'conservar')
                unidades = list(self.servicio.parent.glob(self.servicio.name + '.retirado-*'))
                self.assertEqual(len(unidades), 1)
                self.assertEqual(unidades[0].read_bytes(), contenido)
                if sistema.startswith('Linux'):
                    self.assertIn('--user disable', self.leer('systemctl.log'))

    def test_config_fallida_no_carga_servicio_en_ambos_sistemas(self):
        for sistema in ('Darwin arm64', 'Linux x86_64'):
            with self.subTest(sistema=sistema):
                self.preparar(sistema)
                (self.fake / 'config-falla').touch()
                resultado = self.instalar()
                self.assertEqual(resultado.returncode, 7, resultado.stderr)
                self.assertIn('--name ', self.leer('config.log'))
                self.assertFalse((self.fake / 'cargado').exists())
                self.assertFalse(self.servicio.exists())
                self.assertNotIn('bootstrap', self.leer('launchctl.log'))
                self.assertNotIn('enable --now', self.leer('systemctl.log'))

    def test_si_no_escucha_suelta_servicio_en_ambos_sistemas(self):
        for sistema in ('Darwin arm64', 'Linux x86_64'):
            with self.subTest(sistema=sistema):
                self.preparar(sistema)
                (self.fake / 'mudo').touch()
                resultado = self.instalar()
                self.assertEqual(resultado.returncode, 1, resultado.stderr)
                self.assertIn('no llegó a escuchar', resultado.stderr)
                self.assertFalse((self.fake / 'cargado').exists())

    def test_arranque_linux_fallido_no_deja_unidad_activa(self):
        self.preparar('Linux x86_64')
        (self.fake / 'arranque-falla').touch()
        resultado = self.instalar()
        self.assertNotEqual(resultado.returncode, 0)
        self.assertFalse((self.fake / 'cargado').exists())
        self.assertIn('--user disable', self.leer('systemctl.log'))

    def test_papel_en_sistema_equivocado_nombra_host_correcto(self):
        self.preparar('Linux x86_64')
        for papel in ('guard', 'datos', 'hasito-noche'):
            for modo in ((), ('--seco',), ('--retirar',)):
                with self.subTest(papel=papel, modo=modo):
                    resultado = self.correr(*modo, papel=papel)
                    self.assertNotEqual(resultado.returncode, 0)
                    self.assertIn('Mini', resultado.stderr)
                    self.assertEqual(list(self.home.iterdir()), [])
        self.preparar('Linux aarch64')
        resultado = self.correr('--seco', papel='hasito-ci')
        self.assertNotEqual(resultado.returncode, 0)
        self.assertIn('Mini', resultado.stderr)
        self.assertIn('desk', resultado.stderr)

    def test_sha256_distinto_aborta_antes_de_extraer_en_ambos_sistemas(self):
        for sistema, sha in (('Darwin arm64', '25a065af409ffa317ab4d5a3b614dd15fc9c40f2172bc1b35a1b1258e919a388'),
                             ('Linux x86_64', LINUX_SHA)):
            with self.subTest(sistema=sistema):
                self.preparar(sistema)
                (self.fake / 'sha-mal').touch()
                resultado = self.instalar()
                self.assertEqual(resultado.returncode, 1, resultado.stderr)
                self.assertIn('sha256', resultado.stderr)
                self.assertIn(sha, self.leer('shasum.in'))
                self.assertFalse((self.dir / 'config.sh').exists())
                self.assertFalse((self.fake / 'cargado').exists())

    def test_dependencias_faltantes_se_listan_antes_de_registrar(self):
        self.preparar('Linux x86_64')
        (self.fake / 'faltantes').write_text('libicu74 liblttng-ust1t64')
        resultado = self.instalar()
        self.assertNotEqual(resultado.returncode, 0)
        self.assertIn('libicu74', resultado.stderr)
        self.assertIn('liblttng-ust1t64', resultado.stderr)
        self.assertEqual(self.leer('config.log'), '')
        self.assertFalse((self.fake / 'cargado').exists())

    def test_biblioteca_no_resuelta_falla_aunque_dpkg_diga_instalado(self):
        self.preparar('Linux x86_64')
        (self.fake / 'bibliotecas').write_text('libc.so.6 (libc6,x86-64) => /lib/libc.so.6\n')
        resultado = self.instalar()
        self.assertNotEqual(resultado.returncode, 0)
        self.assertIn('libicu74', resultado.stderr)
        self.assertIn('libssl3t64', resultado.stderr)
        self.assertEqual(self.leer('config.log'), '')


if __name__ == '__main__':
    unittest.main()
