"""Harness aislado: sólo regla 1, HOME temporal, nunca ~/remote-run real.

La costura de df existe únicamente en la copia temporal creada aquí. Para
comparar con main: LIMPIEZA_TEST_SOURCE=/ruta/a/git-archive-origin-main.
"""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest


REPO = Path(__file__).resolve().parents[4]
SOURCE = Path(os.environ.get('LIMPIEZA_TEST_SOURCE', REPO))
HOSTS = SOURCE / 'scripts/hosts'
spec = importlib.util.spec_from_file_location('inventario_emergencia', HOSTS / 'inventario/inventario.py')
inv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inv)


class EmergenciaWorkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / 'home'
        self.rr = self.home / 'remote-run'
        (self.rr / 'work').mkdir(parents=True)
        (self.rr / 'runs').mkdir()
        self.rr.chmod(0o700)
        (self.rr / 'work').chmod(0o700)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.now = time.time()
        self.config = {'free': [39], 'cwd': 'ok', 'hook': ''}
        self.executable('lsof', '''#!/usr/bin/python3 -B
import json, os, subprocess, sys
from pathlib import Path
root = Path(os.environ['HARNESS'])
c = json.loads((root / 'config.json').read_text())
args = sys.argv[1:]
with (root / 'lsof.calls').open('a') as f:
    f.write(json.dumps(args) + '\\n')
if args == ['-a', '-d', 'cwd', '-Fn']:
    if c['cwd'] == 'fail':
        sys.exit(2)
    if c['cwd'] == 'warning':
        print('captura parcial', file=sys.stderr)
    if c['cwd'] == 'empty':
        sys.exit(0)
    if c['cwd'] == 'malformed':
        print('p123\\nnno-verificable')
        sys.exit(0)
    if 'cwd_records' in c:
        print(c['cwd_records'].replace('$FIXTURE', str(root)))
        sys.exit(0)
    if 'pid' in c:
        r = subprocess.run(['/usr/sbin/lsof', '-a', '-p', str(c['pid']), '-d', 'cwd', '-Fn'],
                           capture_output=True, text=True)
        print(r.stdout, end='')
        print(r.stderr, end='', file=sys.stderr)
        sys.exit(r.returncode)
    print('p123\\nfcwd\\nn' + c.get('cwd_path', str(root)))
elif '+D' in args:
    if c['hook'] == 'lock':
        (Path(args[-1]) / '.remote-run.lock').write_text('nuevo')
    sys.exit(1)  # la nueva capa cwd debe vetar incluso si +D no lo detecta
else:
    print('p123\\nn' + str(root))
''')
        self.executable('ps', '''#!/usr/bin/python3 -B
import json, os, sys
from pathlib import Path
if sys.argv[1] == '-axww':
    sys.exit(0)
if sys.argv[1] == '-U':
    root = Path(os.environ['HARNESS'])
    c = json.loads((root / 'config.json').read_text())
    p = root / 'ps.index'
    i = int(p.read_text()) if p.exists() else 0
    p.write_text(str(i + 1))
    snapshots = c.get('ps_snapshots', [str(c.get('pid', 123)) + ' S'])
    print(snapshots[min(i, len(snapshots) - 1)])
    sys.exit(c.get('ps_rc', 0))
os.execv('/bin/ps', ['ps'] + sys.argv[1:])
''')
        self.executable('find', '''#!/usr/bin/python3 -B
import json, os, subprocess, sys, time
from pathlib import Path
root = Path(os.environ['HARNESS'])
c = json.loads((root / 'config.json').read_text())
r = subprocess.run(['/usr/bin/find'] + sys.argv[1:])
if sys.argv[1].startswith('./') and c['hook'].startswith('run'):
    run = Path(os.environ['HOME']) / 'remote-run/runs/nueva'
    run.mkdir(exist_ok=True)
    if c['hook'] == 'run-mirror':
        (run / 'meta').write_text('mirror=remote-run/work/copia\\ncmd=true\\n')
    os.utime(run, (time.time() + 2,) * 2)
sys.exit(r.returncode)
''')
        self.executable('stat', '''#!/usr/bin/python3 -B
import json, os, sys, time
from pathlib import Path
c = json.loads((Path(os.environ['HARNESS']) / 'config.json').read_text())
if sys.argv[1:3] == ['-f', '%m']:
    if c['hook'] == 'mtime':
        os.utime(sys.argv[3], None)
os.execv('/usr/bin/stat', ['stat'] + sys.argv[1:])
''')
        (self.root / 'free.py').write_text('''import json, os
from pathlib import Path
root = Path(os.environ['HARNESS'])
p = root / 'free.index'
i = int(p.read_text()) if p.exists() else 0
values = json.loads((root / 'config.json').read_text())['free']
p.write_text(str(i + 1))
print(values[min(i, len(values) - 1)])
''')
        script = (HOSTS / 'limpieza-disco/limpieza-disco.sh').read_text()
        script = script.split('# 3. remote-run/runs:', 1)[0]
        script = script.replace(
            'export PATH="$HOME/.bun/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"',
            'export PATH=' + shlex.quote(str(self.bin)) + ':/usr/bin:/bin:/usr/sbin:/sbin')
        old_free = "libre_gb() { df -g /System/Volumes/Data | awk 'NR==2{print $4}'; }"
        self.assertIn(old_free, script)
        script = script.replace(old_free, 'libre_gb() { /usr/bin/python3 -B "$HARNESS/free.py"; }')
        script += '\nprintf "HARNESS_UMBRALES=%s,%s,%s,%s\\n" "$EMERG" "$PYTEST_MIN" "$CODEX_MIN" "$CLAUDE_MIN"\n'
        self.script = self.root / 'regla1.sh'
        self.script.write_text(script)
        shutil.copyfile(HOSTS / 'inventario/inventario.py', self.root / 'pao-inventario.py')

    def executable(self, name, text):
        p = self.bin / name
        p.write_text(text)
        p.chmod(0o755)

    def edad(self, p, horas):
        os.utime(p, (self.now - horas * 3600,) * 2)

    def copia(self, name='copia', horas=7):
        p = self.rr / 'work' / name
        p.mkdir()
        (p / 'dato').write_bytes(b'x' * 8192)
        self.edad(p / 'dato', horas)
        self.edad(p, horas)
        return p

    def ejecutar(self, dry=True, rc=0):
        (self.root / 'config.json').write_text(json.dumps(self.config))
        result = subprocess.run(['bash', str(self.script)] + (['--dry-run'] if dry else []),
                                env=dict(os.environ, HOME=str(self.home), HARNESS=str(self.root)),
                                capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, rc, result.stdout + result.stderr)
        self.assertEqual(result.stderr, '', result.stderr)
        self.log = result.stdout
        self.seleccion = [line.split(' (', 1)[0].rsplit('/work/', 1)[1]
                          for line in self.log.splitlines() if '[dry-run] borraría ' in line]
        return self.seleccion

    def tamanos_simulados(self, tamanos):
        # Sólo la copia temporal: nunca asignar decenas de GB para probar el freno.
        with (self.root / 'pao-inventario.py').open('a') as f:
            f.write('\n\ndef tamano(ruta):\n    return ' + repr(tamanos) + '[ruta.name]\n')

    def test_captura_parcial_completa_seguida_de_incompleta_veta_todo(self):
        p = self.copia()
        process = subprocess.Popen(['/bin/sleep', '120'], cwd=p)
        self.addCleanup(process.wait)
        self.addCleanup(process.terminate)
        real = subprocess.run(['/usr/sbin/lsof', '-a', '-p', str(process.pid), '-d', 'cwd', '-Fn'],
                              capture_output=True, text=True, check=True)
        self.assertIn(str(p), real.stdout)
        self.config['cwd_records'] = 'p123\nfcwd\nn$FIXTURE\np456\nfcwd'
        self.assertEqual(self.ejecutar(), [])
        self.assertIn('EMERGENCIA work sin borrar', self.log)
        self.assertIn('captura', self.log)
        self.assertNotIn('[dry-run] borraría', self.log)

    def test_captura_campos_sueltos_duplicados_y_rutas_invalidas_vetan(self):
        self.copia()
        for registro in ('n$FIXTURE', 'fcwd\nn$FIXTURE', 'p123', 'p123\nn$FIXTURE',
                         'p123\nfcwd\nn', 'p123\nfcwd\nnrelativa',
                         'p123\nfcwd\nn$FIXTURE\nn$FIXTURE',
                         'p123\nfcwd\nn$FIXTURE\np123\nfcwd\nn$FIXTURE',
                         'p123\nfcwd\nn$FIXTURE\np456\nfcwd\nn',
                         'p123\nfcwd\nn$FIXTURE\nfotro', 'pno-pid\nfcwd\nn$FIXTURE'):
            with self.subTest(registro=registro):
                self.config['cwd_records'] = registro
                self.assertEqual(self.ejecutar(), [])
                self.assertIn('captura', self.log)

    def test_pid_propio_ausente_y_aun_vivo_cierra(self):
        self.copia()
        self.config['ps_snapshots'] = ['123 S\n456 S']
        self.assertEqual(self.ejecutar(), [])
        self.assertIn('cobertura', self.log)
        self.assertIn('456', self.log)
        self.assertEqual((self.root / 'ps.index').read_text(), '2')

    def test_pid_ausente_terminado_y_zombie_no_vetan(self):
        self.copia()
        self.config['ps_snapshots'] = ['123 S\n456 S\n789 Z', '123 S']
        self.assertEqual(self.ejecutar(), ['copia'])
        self.assertEqual((self.root / 'ps.index').read_text(), '2')

    def test_ps_no_verificable_cierra(self):
        self.copia()
        for cambios in ({'ps_rc': 2}, {'ps_snapshots': ['no-pid S']},
                        {'ps_snapshots': ['']}, {'ps_snapshots': ['123']},
                        {'ps_snapshots': ['123 S\n456 S', 'salida parcial']}):
            with self.subTest(cambios=cambios):
                self.config = dict(free=[39], cwd='ok', hook='', **cambios)
                (self.root / 'ps.index').unlink(missing_ok=True)
                self.assertEqual(self.ejecutar(), [])
                self.assertIn('EMERGENCIA work sin borrar', self.log)

    def test_permisos_remote_run_y_work_cierran(self):
        self.copia()
        for ruta in (self.rr, self.rr / 'work'):
            for bit in (0o040, 0o010, 0o004, 0o001):
                with self.subTest(ruta=ruta.name, bit=oct(bit)):
                    ruta.chmod(0o700 | bit)
                    try:
                        self.assertEqual(self.ejecutar(), [])
                        self.assertIn('permisos', self.log)
                    finally:
                        ruta.chmod(0o700)

    def test_r3_df_fijo_no_frena_por_estimacion_y_rename(self):
        for nombre, horas in (('vieja', 9), ('intermedia', 8), ('nueva', 7)):
            self.copia(nombre, horas)
        self.tamanos_simulados(dict.fromkeys(('vieja', 'intermedia', 'nueva'), 11 * 1073741824))
        self.config['free'] = [39]
        self.ejecutar(dry=False)
        self.assertEqual(list((self.rr / 'work').iterdir()), [])
        self.assertEqual(self.log.count('renombrada '), 3)
        self.assertEqual(self.log.count('borrado '), 3)
        self.assertLess(self.log.index('/work/vieja a '), self.log.index('/work/intermedia a '))
        self.assertIn('3 copias; total 35433480192 bytes', self.log)
        self.assertNotIn('freno', self.log)
        self.assertIn('libre 39 GB', self.log)
        self.assertEqual((self.root / 'free.index').read_text(), '7')
        state = self.home / 'Library/Application Support/limpieza-disco'
        self.assertEqual(list(state.glob('borrando-*')), [])

    def test_freno_real_df_llega_antes_que_estimacion(self):
        for nombre, horas in (('vieja', 9), ('intermedia', 8), ('nueva', 7)):
            self.copia(nombre, horas)
        self.config['free'] = [39, 39, 60]
        self.ejecutar(dry=False)
        self.assertEqual(sorted(p.name for p in (self.rr / 'work').iterdir()), ['intermedia', 'nueva'])
        self.assertEqual(self.log.count('renombrada '), 1)
        self.assertIn('freno en 60 GB', self.log)
        self.assertEqual((self.root / 'free.index').read_text(), '3')

    def test_r3_estimacion_supera_objetivo_pero_sigue_hasta_df_60(self):
        for nombre, horas in (('vieja', 9), ('intermedia', 8), ('nueva', 7)):
            self.copia(nombre, horas)
        self.tamanos_simulados(dict.fromkeys(('vieja', 'intermedia', 'nueva'), 11 * 1073741824))
        self.config['free'] = [39, 39, 41, 41, 41, 41, 60]
        self.ejecutar(dry=False)
        self.assertEqual(list((self.rr / 'work').iterdir()), [])
        self.assertEqual(self.log.count('borrado '), 3)
        self.assertIn('freno en 60 GB (df)', self.log)
        self.assertEqual((self.root / 'free.index').read_text(), '7')

    def test_r3_dry_run_enumera_todo_sin_promesa_de_recuperacion(self):
        for nombre, horas in (('vieja', 9), ('intermedia', 8), ('nueva', 7)):
            self.copia(nombre, horas)
        self.tamanos_simulados(dict.fromkeys(('vieja', 'intermedia', 'nueva'), 11 * 1073741824))
        self.assertEqual(self.ejecutar(), ['vieja', 'intermedia', 'nueva'])
        self.assertEqual(len(list((self.rr / 'work').iterdir())), 3)
        self.assertNotIn('freno', self.log)

    def test_r3_solo_index_reciente_se_borra_en_emergencia(self):
        p = self.copia()
        git = p / '.git'
        git.mkdir()
        (git / 'index').write_text('refresco de stat')
        # Crear/reemplazar index también cambia el directorio .git.
        self.edad(p, 7)
        self.ejecutar(dry=False)
        self.assertFalse(p.exists(), self.log)
        self.assertIn('borrado ', self.log)

    def test_r3_metadatos_git_ignorados_sin_cambiar_modo_normal(self):
        p = self.copia(horas=8 * 24)
        git = p / '.git'
        git.mkdir()
        for nombre in ('index', 'FETCH_HEAD', 'ORIG_HEAD', 'index.lock'):
            (git / nombre).write_text('metadatos')
        self.edad(p, 8 * 24)
        self.assertEqual(self.ejecutar(), ['copia'])
        self.config['free'] = [40]
        (self.root / 'free.index').unlink()
        self.assertEqual(self.ejecutar(), [])

    def test_r3_cambio_reciente_fuera_de_git_incluso_profundo_se_conserva(self):
        p = self.copia()
        sub = p / 'src' / 'profundo'
        sub.mkdir(parents=True)
        for nombre in ('index', 'FETCH_HEAD', 'ORIG_HEAD', 'index.lock'):
            (sub / nombre).write_text('trabajo real')
        self.edad(sub, 7)
        self.edad(sub.parent, 7)
        self.edad(p, 7)
        self.ejecutar(dry=False)
        self.assertTrue(p.exists(), self.log)
        self.assertIn('algo cambió', self.log)

    def test_r3_git_head_reciente_sigue_vetando(self):
        p = self.copia()
        git = p / '.git'
        git.mkdir()
        (git / 'HEAD').write_text('ref: refs/heads/main')
        self.edad(p, 7)
        self.ejecutar(dry=False)
        self.assertTrue(p.exists(), self.log)
        self.assertIn('algo cambió', self.log)

    def test_r3_git_symlink_reciente_no_es_refresco_de_metadatos(self):
        p = self.copia()
        (p / '.git').symlink_to(self.root / 'git-externo')
        self.edad(p, 7)
        self.ejecutar(dry=False)
        self.assertTrue(p.exists(), self.log)
        self.assertIn('algo cambió', self.log)

    def test_r3_nombre_con_glob_no_oculta_git_anidado(self):
        p = self.copia('copia*')
        git = p / 'src' / '.git'
        git.mkdir(parents=True)
        (git / 'index').write_text('cambio fuera del .git de la copia')
        for ruta in (git, git.parent, p):
            self.edad(ruta, 7)
        self.ejecutar(dry=False)
        self.assertTrue(p.exists(), self.log)
        self.assertIn('algo cambió', self.log)

    def test_r3_nombre_con_metacaracteres_admite_index_reciente(self):
        p = self.copia('copia*[x]?')
        git = p / '.git'
        git.mkdir()
        (git / 'index').write_text('refresco de metadatos')
        self.edad(p, 7)
        self.ejecutar(dry=False)
        self.assertFalse(p.exists(), self.log)

    def test_r3_lock_en_subdirectorio_git_sigue_vetando(self):
        p = self.copia()
        refs = p / '.git' / 'refs'
        refs.mkdir(parents=True)
        (refs / 'main.lock').write_text('actualizando refs')
        self.edad(refs, 7)
        self.edad(refs.parent, 7)
        self.edad(p, 7)
        self.ejecutar(dry=False)
        self.assertTrue(p.exists(), self.log)
        self.assertIn('algo cambió', self.log)

    def test_log_normal_conserva_7_dias(self):
        self.config['free'] = [41]
        p = self.copia(horas=8 * 24)
        self.edad(p / 'dato', 48)
        self.assertEqual(self.ejecutar(), [])
        self.assertIn('algo cambió en los últimos 7 días', self.log)
        self.edad(p / 'dato', 8 * 24)
        self.config['hook'] = 'mtime'
        self.assertEqual(self.ejecutar(), [])
        self.assertIn('el mtime directo tiene menos de 7 días', self.log)

    def test_df_inicial_no_numerico_sale_1_antes_de_reglas(self):
        self.copia(horas=8 * 24)
        self.config['free'] = ['desconocido']
        self.assertEqual(self.ejecutar(rc=1), [])
        self.assertIn('sin borrar: espacio libre no verificable', self.log)
        self.assertNotIn('inicio (', self.log)
        self.assertFalse((self.root / 'lsof.calls').exists())

    def test_normal_y_frontera_40_conservan_siete_dias(self):
        for gb in (41, 40):
            with self.subTest(gb=gb):
                self.config['free'] = [gb]
                if not (self.rr / 'work/copia').exists():
                    self.copia()
                    self.copia('vieja', 8 * 24)
                self.assertEqual(self.ejecutar(), ['vieja'])
                self.assertNotIn('EMERGENCIA work', self.log)
                calls = (self.root / 'lsof.calls').read_text()
                self.assertNotIn('["-a", "-d", "cwd", "-Fn"]', calls)

    def test_emergencia_7h_si_5h_no_y_registro(self):
        vieja = self.copia('siete', 7)
        self.copia('cinco', 5)
        self.assertEqual(self.ejecutar(), ['siete'])
        self.assertIn('EMERGENCIA work', self.log)
        self.assertIn('último uso', self.log)
        self.assertIn('tamaño', self.log)
        self.assertIn('1 copias; total', self.log)
        self.assertIn('rename previo', self.log)
        self.assertTrue((vieja / 'dato').exists())
        calls = [json.loads(x) for x in (self.root / 'lsof.calls').read_text().splitlines()]
        self.assertEqual(calls.count(['-a', '-d', 'cwd', '-Fn']), 1)

    def test_lock_no_borrable(self):
        p = self.copia(horas=8 * 24)
        (p / '.remote-run.lock').write_text('desconocida')
        self.edad(p, 8 * 24)
        self.copia('libre')
        self.assertEqual(self.ejecutar(), ['libre'])

    def test_mirror_vivo_y_dudoso_protegidos(self):
        self.copia('viva', 8 * 24)
        self.copia('dudosa', 8 * 24)
        self.copia('libre')
        for nombre in ('viva', 'dudosa'):
            run = self.rr / 'runs' / nombre
            run.mkdir()
            (run / 'meta').write_text('mirror=remote-run/work/' + nombre + '\ncmd=true\n')
            (run / 'started').write_text(str(self.now))
            if nombre == 'viva':
                (run / 'pid').write_text(str(os.getpid()))
        self.assertEqual(self.ejecutar(), ['libre'])

    def test_cwd_real_descendiente_y_prefijo_hermano(self):
        p = self.copia(horas=8 * 24)
        sub = p / 'sub'
        sub.mkdir()
        self.edad(sub, 8 * 24)
        self.edad(p, 8 * 24)
        self.copia('copia-hermana')
        process = subprocess.Popen(['/bin/sleep', '120'], cwd=sub)
        self.addCleanup(process.wait)
        self.addCleanup(process.terminate)
        self.config['pid'] = process.pid
        self.assertEqual(self.ejecutar(), ['copia-hermana'])
        self.assertIn('cwd visible dentro', self.log)

    def test_cwd_real_directo(self):
        p = self.copia(horas=8 * 24)
        process = subprocess.Popen(['/bin/sleep', '120'], cwd=p)
        self.addCleanup(process.wait)
        self.addCleanup(process.terminate)
        self.config['pid'] = process.pid
        self.assertEqual(self.ejecutar(), [])
        self.assertIn('cwd visible dentro', self.log)

    def test_cwd_realpath_resuelve_alias(self):
        p = self.copia(horas=8 * 24)
        alias = self.root / 'alias'
        alias.symlink_to(p, target_is_directory=True)
        self.config['cwd_path'] = str(alias)
        self.copia('libre')
        self.assertEqual(self.ejecutar(), ['libre'])

    def test_otros_umbrales_sin_cambios(self):
        for gb, umbrales in ((14, '1,180,1440,1440'), (15, '0,720,4320,4320'),
                             (39, '0,720,4320,4320'), (40, '0,720,4320,4320')):
            with self.subTest(gb=gb):
                self.config['free'] = [gb]
                self.ejecutar()
                self.assertIn('HARNESS_UMBRALES=' + umbrales, self.log)

    def test_cwd_falla_cerrado(self):
        self.copia(horas=8 * 24)
        for fallo in ('fail', 'warning', 'empty', 'malformed'):
            with self.subTest(fallo=fallo):
                self.config['cwd'] = fallo
                self.assertEqual(self.ejecutar(), [])

    def test_orden_ultimo_uso_y_freno_60_medido_tras_cada_copia(self):
        self.copia('a-mas-nueva', 7)
        self.copia('z-mas-vieja', 9)
        self.copia('m-intermedia', 8)
        # inicio, antes/después del primero, antes/después del segundo.
        self.config['free'] = [39, 39, 59, 59, 60]
        self.assertEqual(self.ejecutar(), ['z-mas-vieja', 'm-intermedia'])
        self.assertIn('freno en 60 GB', self.log)
        self.assertEqual((self.root / 'free.index').read_text(), '5')

    def test_freno_si_ya_se_recuperaron_60(self):
        self.copia()
        self.config['free'] = [39, 60]
        self.assertEqual(self.ejecutar(), [])
        self.assertIn('freno en 60 GB', self.log)

    def test_espacio_no_verificable_detiene(self):
        self.copia()
        self.config['free'] = [39, 'desconocido']
        self.assertEqual(self.ejecutar(), [])
        self.assertIn('espacio libre no verificable', self.log)

    def test_bloqueo_global_no_borra(self):
        self.copia(horas=8 * 24)
        run = self.rr / 'runs/dudosa'
        run.mkdir()
        (run / 'started').write_text(str(self.now))
        self.assertEqual(self.ejecutar(), [])
        self.assertIn('bloqueo global', self.log)

    def test_corrida_nueva_sigue_vetando(self):
        self.copia()
        self.config['hook'] = 'run-mirror'
        self.assertEqual(self.ejecutar(), [])
        self.assertIn('corrida nueva asociada', self.log)

    def test_corrida_nueva_sin_meta_sigue_vetando(self):
        self.copia()
        self.config['hook'] = 'run-sin-meta'
        self.assertEqual(self.ejecutar(), [])
        self.assertIn('corrida nueva sin meta', self.log)

    def test_find_hijo_reciente_veta(self):
        p = self.copia()
        self.edad(p / 'dato', 5)
        self.assertEqual(self.ejecutar(), [])
        self.assertIn('algo cambió', self.log)

    def test_mtime_directo_repetido_veta(self):
        self.copia()
        self.config['hook'] = 'mtime'
        self.assertEqual(self.ejecutar(), [])
        self.assertIn('mtime directo tiene menos', self.log)

    def test_lock_tardio_veta(self):
        self.copia()
        self.config['hook'] = 'lock'
        self.assertEqual(self.ejecutar(), [])
        self.assertIn('apareció un lock', self.log)

    def test_inventario_umbral_explicito_y_sin_filtracion(self):
        self.copia('siete', 7)
        self.copia('cinco', 5)
        default = inv.inventariar(self.rr)
        self.assertTrue(all(x['clase'] == 'reciente' for x in default['copias']))
        emergencia = inv.inventariar(self.rr, copia_minutos=360)
        clases = {Path(x['ruta']).name: x['clase'] for x in emergencia['copias']}
        self.assertEqual(clases, {'siete': 'inactiva', 'cinco': 'reciente'})
        self.assertEqual(emergencia['reglas']['copiaDias'], .25)
        self.assertEqual(inv.inventariar(self.rr)['reglas']['copiaDias'], 7)
        with self.assertRaises(ValueError):
            inv.inventariar(self.rr, copia_minutos=1)


if __name__ == '__main__':
    unittest.main()
