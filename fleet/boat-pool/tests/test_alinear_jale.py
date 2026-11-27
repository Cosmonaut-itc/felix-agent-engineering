import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PILOTO = '''\
import os
from pathlib import Path
import subprocess
import sys
import time

assert sys.argv[1:] == ['alinear']
assert sys.flags.isolated and sys.dont_write_bytecode
sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
with Path(os.environ['LLAMADAS_PRUEBA']).open('a') as llamadas:
    print(sha, file=llamadas)
codigo = int(os.environ['CODIGO_PRUEBA'])
print('vm-uno ya-al-dia revision_vm=demo', flush=True)
if codigo:
    print('vm-dos fallo:preparacion revision_vm=demo ' + 'bx_' + 'falso123',
          file=sys.stderr, flush=True)
if codigo == 9:
    time.sleep(4)
sys.exit(codigo)
'''


class AlinearJale(unittest.TestCase):
    def setUp(self):
        temporal = tempfile.TemporaryDirectory()
        self.addCleanup(temporal.cleanup)
        self.base = Path(temporal.name)
        home = self.base / 'home'
        home.mkdir()
        self.config = self.base / 'config/boat-pool/alinear-jale.json'
        self.config.parent.mkdir(parents=True)
        self.estado = self.base / 'state/boat-pool/alinear-jale'
        self.worktree = self.estado / 'main'
        self.llamadas = self.base / 'llamadas.txt'
        self.avisos = self.base / 'avisos.txt'
        osascript = self.base / 'osascript'
        osascript.write_text('#!/bin/sh\nprintf "%s\\n" "$3" >> "$AVISOS_PRUEBA"\n')
        osascript.chmod(0o700)
        self.env = {
            **os.environ,
            'HOME': str(home),
            'XDG_CONFIG_HOME': str(self.config.parents[1]),
            'XDG_STATE_HOME': str(self.estado.parents[1]),
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
            'GIT_AUTHOR_NAME': 'Prueba', 'GIT_AUTHOR_EMAIL': 'prueba@example.org',
            'GIT_COMMITTER_NAME': 'Prueba', 'GIT_COMMITTER_EMAIL': 'prueba@example.org',
            'ALINEAR_JALE_OSASCRIPT': str(osascript),
            'LLAMADAS_PRUEBA': str(self.llamadas), 'AVISOS_PRUEBA': str(self.avisos),
        }
        self.remoto, self.repo = self.base / 'remoto', self.base / 'clon de Jale'
        self.git(self.base, 'init', '-q', '-b', 'main', str(self.remoto))
        piloto = self.remoto / 'scripts/test-hosts/boat-pilot.py'
        piloto.parent.mkdir(parents=True)
        piloto.write_text(PILOTO, encoding='utf-8')
        self.git(self.remoto, 'add', '.')
        self.git(self.remoto, 'commit', '-qm', 'Piloto falso')
        self.git(self.base, 'clone', '-q', str(self.remoto), str(self.repo))
        self.inicial = self.git(self.repo, 'rev-parse', 'origin/main')

    def git(self, repo, *args):
        return subprocess.run(
            ['git', '-C', str(repo), *args], env=self.env, check=True,
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()

    def avanzar_main(self):
        (self.remoto / 'runner.txt').write_text('runner nuevo\n')
        self.git(self.remoto, 'add', 'runner.txt')
        self.git(self.remoto, 'commit', '-qm', 'Cambia el runner')
        return self.git(self.remoto, 'rev-parse', 'HEAD')

    def configurar(self, activo=True):
        self.config.write_text(json.dumps({'repo': str(self.repo), 'activo': activo}))

    def pasada(self, codigo=0, *, esperado=None, args=None, **extra):
        result = subprocess.run(
            [sys.executable, '-IB', *(args or [str(ROOT / 'alinear-jale')])],
            env={**self.env, 'CODIGO_PRUEBA': str(codigo), **extra},
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, codigo if esperado is None else esperado, result.stderr)
        self.assertEqual(result.stdout, '')

    def ultimo_estado(self):
        return json.loads((self.estado / 'estado.json').read_text())

    def test_inactivo_no_ejecuta_git_ni_el_piloto(self):
        self.avanzar_main()
        traza = self.base / 'git.trace'
        for activo in (None, False, 1):
            with self.subTest(activo=activo):
                if activo is not None:
                    self.configurar(activo)
                self.pasada(GIT_TRACE=str(traza))
                self.assertFalse(traza.exists())
                self.assertFalse(self.estado.exists())
                self.assertFalse(self.llamadas.exists())
                self.assertFalse(self.avisos.exists())
                self.assertEqual(self.git(self.repo, 'rev-parse', 'origin/main'), self.inicial)

    def test_main_nuevo_actualiza_el_worktree_y_un_exito_se_conserva(self):
        self.configurar()
        self.pasada()
        self.assertEqual(self.git(self.worktree, 'rev-parse', 'HEAD'), self.inicial)
        nuevo = self.avanzar_main()
        self.pasada()
        self.assertEqual(self.git(self.worktree, 'rev-parse', 'HEAD'), nuevo)
        self.assertEqual(self.git(self.worktree, 'rev-parse', '--abbrev-ref', 'HEAD'), 'HEAD')
        self.assertEqual(self.git(self.repo, 'rev-parse', 'HEAD'), self.inicial)
        self.assertEqual((self.worktree / 'runner.txt').read_text(), 'runner nuevo\n')
        estado = self.ultimo_estado()
        self.assertEqual(estado['sha'], nuevo)
        self.assertEqual(estado['codigo'], 0)
        self.assertTrue(estado['hora'])
        self.assertEqual((self.estado / estado['log']).read_text(), estado['salida'])
        self.pasada()
        self.assertEqual(self.llamadas.read_text().splitlines(), [self.inicial, nuevo])
        self.assertEqual(len(list(self.estado.glob('alinear-*.log'))), 2)

    def test_reintenta_fallo_y_avisa_una_vez_con_ids_ocultos(self):
        self.configurar()
        self.pasada(1)
        self.pasada(1)
        self.assertEqual(self.llamadas.read_text().splitlines(), [self.inicial, self.inicial])
        aviso = self.avisos.read_text()
        self.assertEqual(aviso.count('fallo:preparacion'), 1)
        self.assertNotIn('ya-al-dia', aviso)
        self.assertIn('[id de Boat]', aviso)
        estado = self.ultimo_estado()
        self.assertEqual(estado['codigo'], 1)
        self.assertIn('ya-al-dia', estado['salida'])
        logs = list(self.estado.glob('alinear-*.log'))
        self.assertEqual(len(logs), 2)
        for salida in (aviso, estado['salida'], *(log.read_text() for log in logs)):
            self.assertNotIn('bx_' + 'falso123', salida)
            self.assertIn('fallo:preparacion', salida)

    def test_worktree_sucio_avisa_y_conserva_los_cambios(self):
        self.configurar()
        self.pasada()
        nuevo = self.avanzar_main()
        archivo = self.worktree / 'local.txt'
        archivo.write_text('cambio local')
        self.pasada(esperado=2)
        self.pasada(esperado=2)
        self.assertEqual(archivo.read_text(), 'cambio local')
        self.assertEqual(self.git(self.worktree, 'rev-parse', 'HEAD'), self.inicial)
        self.assertEqual(self.ultimo_estado()['sha'], nuevo)
        self.assertEqual(self.llamadas.read_text().splitlines(), [self.inicial])
        self.assertEqual(self.avisos.read_text().count('está sucio'), 1)

    def test_fallo_git_y_codigo_inesperado_generan_avisos(self):
        self.configurar()
        self.pasada(7, esperado=2)
        self.assertEqual(self.ultimo_estado()['codigo'], 7)
        self.git(self.repo, 'remote', 'set-url', 'origin', str(self.base / 'ausente'))
        self.pasada(esperado=2)
        self.assertEqual(self.ultimo_estado()['etapa'], 'git')
        self.assertEqual(self.llamadas.read_text().splitlines(), [self.inicial])
        aviso = self.avisos.read_text()
        self.assertIn('código 7', aviso)
        self.assertIn('git fetch', aviso)
        self.git(self.repo, 'remote', 'set-url', 'origin', str(self.remoto))
        self.pasada(7, esperado=2)
        self.assertEqual(self.avisos.read_text(), aviso)

    def test_vence_plazo_y_registra_salida_parcial(self):
        self.configurar()
        args = ['-c',
                'import runpy, sys; main = runpy.run_path(sys.argv[1])["main"]; '
                'main.__globals__["PLAZO_ALINEAR"] = 2; sys.exit(main())',
                str(ROOT / 'alinear-jale')]
        self.pasada(9, esperado=2, args=args)
        estado = self.ultimo_estado()
        self.assertEqual(estado['codigo'], 'plazo')
        self.assertIn('ya-al-dia', estado['salida'])
        self.assertIn('fallo:preparacion', (self.estado / estado['log']).read_text())
        self.assertIn('superó el plazo', self.avisos.read_text())


class Instalacion(unittest.TestCase):
    def test_launchagent_y_ejemplo_inactivo(self):
        with tempfile.TemporaryDirectory() as temporal:
            base = Path(temporal)
            home, stubs = base / 'home', base / 'stubs'
            home.mkdir()
            stubs.mkdir()
            log = base / 'comandos.txt'
            for nombre in ('plutil', 'launchctl'):
                stub = stubs / nombre
                stub.write_text('#!/bin/sh\nprintf "%s %s\\n" "${0##*/}" "$*" >> "$LOG_PRUEBA"\n'
                                '[ "${1:-}" != print ]\n')
                stub.chmod(0o700)
            resultado = subprocess.run(
                [str(ROOT / 'install-alinear-jale.sh')],
                env={**os.environ, 'HOME': str(home), 'LOG_PRUEBA': str(log),
                     'XDG_CONFIG_HOME': str(base / 'config'), 'XDG_STATE_HOME': str(base / 'state'),
                     'PATH': str(stubs) + ':/usr/bin:/bin'},
                capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(resultado.returncode, 0, resultado.stderr)
            config = base / 'config/boat-pool'
            self.assertFalse((config / 'alinear-jale.json').exists())
            self.assertIs(json.loads((config / 'alinear-jale.example.json').read_text())['activo'], False)
            self.assertTrue(os.access(home / '.local/bin/alinear-jale', os.X_OK))
            self.assertTrue((base / 'state/boat-pool/alinear-jale').is_dir())
            agent = home / 'Library/LaunchAgents/local.pao.boat-alinear-jale.plist'
            plist = plistlib.loads(agent.read_bytes())
            self.assertEqual(plist['Label'], 'local.pao.boat-alinear-jale')
            self.assertIs(plist['RunAtLoad'], True)
            self.assertEqual(plist['StartInterval'], 300)
            self.assertIn('exec "$HOME/.local/bin/alinear-jale" 2>> "$state/launchd.err.log"',
                          plist['ProgramArguments'][-1])
            self.assertEqual(log.read_text().splitlines()[-1], f'launchctl bootstrap gui/{os.getuid()} {agent}')
