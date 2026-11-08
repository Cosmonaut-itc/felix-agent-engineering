"""Pasada completa aislada; el tope corto y PATH sólo cambian en el fixture.

LIMPIEZA_TEST_SOURCE permite ejecutar estos mismos tests contra origin/main.
Nunca consulta /private/tmp ni HOME reales y siempre usa --dry-run.
Los tests r5 sustituyen sólo borrar() para las cachés aisladas del fixture.
"""
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[4]
SOURCE = Path(os.environ.get('LIMPIEZA_TEST_SOURCE', REPO))
HOSTS = SOURCE / 'scripts/hosts'


class RecorridosAcotadosTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / 'home'
        self.rr = self.home / 'remote-run'
        (self.rr / 'work/copia').mkdir(parents=True)
        (self.rr / 'runs').mkdir()
        self.rr.chmod(0o700)
        (self.rr / 'work').chmod(0o700)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        (self.root / 'tmp').mkdir()
        self.state = self.home / 'Library/Application Support/limpieza-disco'
        self.state.mkdir(parents=True)
        self.lock = self.state / 'lock'
        self.config = {'block': ''}
        (self.rr / 'work/copia/dato').write_text('conservar si no se verifica')
        for folder in ('Documents', 'Desktop', 'OrbStack', 'app'):
            (self.home / folder / 'hijo/.next/cache').mkdir(parents=True)
            (self.home / folder / 'hijo/.next/cache/dato').write_text('cache')
        self.envejecer(self.home)
        # Evita que el glob sin coincidencias de la regla 5 produzca stderr.
        (self.home / '.codex/worktrees/reciente').mkdir(parents=True)
        self.executable('find', '''#!/usr/bin/python3 -B
import json, os, subprocess, sys, time
from pathlib import Path
root = Path(os.environ['HARNESS'])
c = json.loads((root / 'config.json').read_text())
a = sys.argv[1:]
home = os.environ['HOME']
with (root / 'find-roots').open('a') as f: f.write(a[0] + '\\n')
if Path(a[0]).name in c.get('find_tcc_block', []): time.sleep(30)
block = ((c['block'] == 'home' and a[0] in (home, home + '/app'))
         or (c['block'] == 'raiz' and a[0] in (home, home + '/aaa-bloqueada'))
         or (c['block'] == 'copia' and a[0] == './copia')
         or (c['block'] == 'next' and a[0].endswith('.next/cache'))
         or (c['block'] == 'pytest' and a[0].endswith('pytest-bt-viejo'))
         or (c['block'] == 'runs' and a[0].endswith('/remote-run/runs')))
if block:
    (root / 'blocked.pid').write_text(str(os.getpid()))
    if c['block'] == 'home':
        print(home + '/app/hijo/.next', flush=True)  # salida parcial
    time.sleep(30)
if a[-6:] == ['-type', 'd', '-name', '.next', '-print', '-prune']:
    # La misma expresión de poda, publicando cada directorio visitado.
    assert a[-6:] == ['-type', 'd', '-name', '.next', '-print', '-prune'], a
    visited = subprocess.run(['/usr/bin/find'] + a[:-6] + ['-type', 'd', '-print'],
                             capture_output=True, text=True, check=True)
    with (root / 'visited').open('a') as f: f.write(visited.stdout)
os.execv('/usr/bin/find', ['find'] + a)
''')
        self.executable('du', '''#!/usr/bin/python3 -B
import json, os, sys, time
from pathlib import Path
root = Path(os.environ['HARNESS'])
c = json.loads((root / 'config.json').read_text())
if c['block'] == 'du':
    print('999999999\\t' + sys.argv[-1], flush=True)
    time.sleep(30)
os.execv('/usr/bin/du', ['du'] + sys.argv[1:])
''')
        self.executable('lsof', '''#!/usr/bin/python3 -B
import json, os, sys, time
from pathlib import Path
c = json.loads((Path(os.environ['HARNESS']) / 'config.json').read_text())
if '+D' in sys.argv:
    if c['block'] == 'lsof': time.sleep(30)
    if c['block'] == 'lsof-parcial': print('captura parcial')
    sys.exit(1)
print('p123\\nn' + os.environ['HARNESS'])
''')
        self.executable('ps', '''#!/usr/bin/python3 -B
import os, sys
if sys.argv[1:] == ['-axww', '-o', 'command=']: sys.exit(0)
os.execv('/bin/ps', ['ps'] + sys.argv[1:])
''')
        self.executable('xattr', """#!/usr/bin/python3 -B
import json, os, sys, time
from pathlib import Path
root = Path(os.environ['HARNESS'])
c = json.loads((root / 'config.json').read_text())
name = Path(sys.argv[-1]).name
with (root / 'xattr-roots').open('a') as f: f.write(name + '\\n')
if name == c.get('xattr_error'): sys.exit(1)
if name == c.get('xattr_block') or name in c.get('xattr_tcc_block', []): time.sleep(30)
print(c.get('xattrs', {}).get(name, ''))
""")
        self.executable('stat', """#!/usr/bin/python3 -B
import json, os, sys
from pathlib import Path
root = Path(os.environ['HARNESS'])
c = json.loads((root / 'config.json').read_text())
if sys.argv[1:3] == ['-f', '%d']:
    path = sys.argv[-1]
    with (root / 'stat-roots').open('a') as f: f.write(path + '\\n')
    if Path(path).name == c.get('stat_error'): sys.exit(1)
    if Path(path).name == c.get('otro_dispositivo'):
        print(os.stat(path).st_dev + 1)
        sys.exit(0)
os.execv('/usr/bin/stat', ['stat'] + sys.argv[1:])
""")
        script = (HOSTS / 'limpieza-disco/limpieza-disco.sh').read_text()
        script = script.replace(
            'export PATH="$HOME/.bun/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"',
            'export PATH=' + shlex.quote(str(self.bin)) + ':/usr/bin:/bin:/usr/sbin:/sbin')
        script = script.replace("libre_gb() { df -g /System/Volumes/Data | awk 'NR==2{print $4}'; }",
                                'libre_gb() { echo 41; }')
        script = script.replace('RECORRIDO_SEGUNDOS=600', 'RECORRIDO_SEGUNDOS=0.5')
        script = script.replace('/private/tmp', str(self.root / 'tmp'))
        # Sólo la regla 7 puede borrar, y sólo dentro de este HOME temporal.
        # El resto de la pasada mantiene --dry-run, incluido simctl.
        self.executable('xcrun', '#!/bin/sh\nexit 0\n')
        script = script.replace('borrar "$c" "caché de Next.js,',
                                'borrar_cache_fixture "$c" "caché de Next.js,')
        script = script.replace('# 7. Cachés de Next.js', r'''borrar_cache_fixture() {
  if [ "${PODA_FIXTURE:-0}" = 1 ]; then
    case "$1" in
      "$HOME"/*/.next/cache|"$HOME"/*/.next/dev)
        rm -rf -- "$1" && log "borrado $1 ($2)" ;;
      *) return 1 ;;
    esac
  else
    borrar "$@"
  fi
}
# 7. Cachés de Next.js''')
        self.script = self.root / 'limpieza-disco.sh'
        self.script.write_text(script)
        shutil.copyfile(HOSTS / 'inventario/inventario.py', self.root / 'pao-inventario.py')

    def executable(self, name, text):
        p = self.bin / name
        p.write_text(text)
        p.chmod(0o755)

    def envejecer(self, p):
        for ruta in sorted(p.rglob('*'), reverse=True) + [p]:
            os.utime(ruta, (time.time() - 9 * 86400,) * 2)

    def ejecutar(self, poda_fixture=False):
        (self.root / 'config.json').write_text(json.dumps(self.config))
        start = time.monotonic()
        p = subprocess.Popen(['bash', str(self.script), '--dry-run'], start_new_session=True,
                             env=dict(os.environ, HOME=str(self.home), HARNESS=str(self.root),
                                      PODA_FIXTURE=str(int(poda_fixture))),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            out, err = p.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            out, err = p.communicate()
            self.fail('pasada no llegó a fin en 8 s:\n' + out + err)
        self.log = out
        self.assertEqual(p.returncode, 0, out + err)
        self.assertEqual(err, '', out + err)
        self.assertLess(time.monotonic() - start, 8)
        return out

    def cache_r5(self, nombre):
        cache = self.home / nombre / 'proyecto/.next/cache'
        cache.mkdir(parents=True)
        (cache / 'dato').write_text('fixture borrable')
        self.envejecer(self.home / nombre)
        return cache

    def test_r6_podas_tcc_antes_de_stat_xattr_y_find(self):
        podas = ('Downloads', 'Movies', 'Music', 'Pictures')
        protegidas = [self.cache_r5(p) for p in podas]
        self.config['find_tcc_block'] = podas
        for bloqueo in ('xattr', 'find'):
            with self.subTest(bloqueo=bloqueo):
                self.config['xattr_tcc_block'] = podas if bloqueo == 'xattr' else []
                posterior = self.cache_r5('zzz-posterior')
                out = self.ejecutar(poda_fixture=True)
                self.assertIn('fin (libre:', out)
                self.assertNotIn('tope ', out)
                for cache in protegidas:
                    self.assertTrue(cache.exists(), str(cache) + out)
                self.assertFalse(posterior.exists(), out)
                atributos = (self.root / 'xattr-roots').read_text().splitlines()
                stats = (self.root / 'stat-roots').read_text().splitlines()
                recorridas = (self.root / 'find-roots').read_text().splitlines()
                for nombre in podas:
                    self.assertNotIn(nombre, atributos)
                    self.assertNotIn(str(self.home / nombre), stats)
                    self.assertFalse(any(
                        p == str(self.home / nombre) or p.startswith(str(self.home / nombre) + '/')
                        for p in recorridas), recorridas)

    def test_r5_raiz_colgada_no_impide_podar_la_siguiente(self):
        bloqueada = self.cache_r5('aaa-bloqueada')
        posterior = self.cache_r5('zzz-posterior')
        self.config['block'] = 'raiz'
        out = self.ejecutar(poda_fixture=True)
        self.assertIn('tope 0.5 s: regla 7: candidatas ruta ' + str(self.home / 'aaa-bloqueada'), out)
        self.assertTrue(bloqueada.exists(), out)
        self.assertFalse(posterior.exists(), out)
        self.assertIn('borrado ' + str(posterior), out)
        self.assertIn('fin (libre:', out)

    def test_r5b_otro_dispositivo_conserva_raiz_y_sigue(self):
        cache = self.cache_r5('montada')
        posterior = self.cache_r5('zzz-posterior')
        self.config['otro_dispositivo'] = 'montada'
        out = self.ejecutar(poda_fixture=True)
        self.assertIn('regla 7: salto raíz ' + str(self.home / 'montada') + ': dispositivo distinto del home', out)
        self.assertTrue(cache.exists(), out)
        self.assertFalse(posterior.exists(), out)
        self.assertNotIn('montada', (self.root / 'xattr-roots').read_text().splitlines())
        consultadas = (self.root / 'stat-roots').read_text().splitlines()
        self.assertEqual(consultadas.count(str(self.home)), 1)
        self.assertIn('fin (libre:', out)

    def test_r5b_stat_raiz_fallido_conserva_raiz_y_sigue(self):
        cache = self.cache_r5('sin-stat')
        posterior = self.cache_r5('zzz-posterior')
        self.config['stat_error'] = 'sin-stat'
        out = self.ejecutar(poda_fixture=True)
        self.assertIn('regla 7: salto raíz ' + str(self.home / 'sin-stat') + ': falló stat del dispositivo', out)
        self.assertTrue(cache.exists(), out)
        self.assertFalse(posterior.exists(), out)
        self.assertNotIn('sin-stat', (self.root / 'xattr-roots').read_text().splitlines())

    def test_r5b_stat_home_fallido_salta_regla_entera(self):
        cache = self.cache_r5('zzz-posterior')
        self.config['stat_error'] = 'home'
        out = self.ejecutar(poda_fixture=True)
        self.assertIn('regla 7: salto regla: falló stat del dispositivo del home', out)
        self.assertTrue(cache.exists(), out)
        self.assertFalse((self.root / 'xattr-roots').exists())
        self.assertFalse((self.root / 'visited').exists())
        self.assertIn('fin (libre:', out)

    def test_r5b_macl_con_flags_conserva_raiz(self):
        cache = self.cache_r5('macl-flags')
        self.config['xattrs'] = {'macl-flags': 'com.apple.provenance\ncom.apple.macl#S'}
        out = self.ejecutar(poda_fixture=True)
        self.assertIn('regla 7: salto raíz ' + str(self.home / 'macl-flags') + ': com.apple.macl', out)
        self.assertTrue(cache.exists(), out)
        self.assertNotIn(str(cache.parent), (self.root / 'visited').read_text())

    def test_r5b_file_provider_con_flags_conserva_raiz(self):
        cache = self.cache_r5('proveedor-flags')
        self.config['xattrs'] = {'proveedor-flags': 'com.apple.file-provider-domain-id#B'}
        out = self.ejecutar(poda_fixture=True)
        self.assertIn('regla 7: salto raíz ' + str(self.home / 'proveedor-flags') + ': com.apple.file-provider-domain-id', out)
        self.assertTrue(cache.exists(), out)
        self.assertNotIn(str(cache.parent), (self.root / 'visited').read_text())

    def test_r5b_detached_con_flags_permite_podar(self):
        cache = self.cache_r5('detached-flags')
        self.config['xattrs'] = {'detached-flags': 'com.apple.fileprovider.detached#B'}
        out = self.ejecutar(poda_fixture=True)
        self.assertFalse(cache.exists(), out)
        self.assertIn('borrado ' + str(cache), out)

    def test_r5_macl_se_salta_con_motivo(self):
        cache = self.cache_r5('macl')
        self.config['xattrs'] = {'macl': 'com.apple.provenance\ncom.apple.macl'}
        out = self.ejecutar(poda_fixture=True)
        self.assertIn('regla 7: salto raíz ' + str(self.home / 'macl') + ': com.apple.macl', out)
        self.assertTrue(cache.exists(), out)
        self.assertNotIn(str(cache.parent), (self.root / 'visited').read_text())

    def test_r5_file_provider_se_salta_con_motivo(self):
        cache = self.cache_r5('proveedor')
        self.config['xattrs'] = {'proveedor': 'com.apple.file-provider-domain-id'}
        out = self.ejecutar(poda_fixture=True)
        self.assertIn('regla 7: salto raíz ' + str(self.home / 'proveedor') + ': com.apple.file-provider-domain-id', out)
        self.assertTrue(cache.exists(), out)

    def test_r5_xattr_fallido_conserva_raiz(self):
        cache = self.cache_r5('sin-atributos')
        self.config['xattr_error'] = 'sin-atributos'
        out = self.ejecutar(poda_fixture=True)
        self.assertIn('regla 7: salto raíz ' + str(self.home / 'sin-atributos') + ': falló xattr', out)
        self.assertTrue(cache.exists(), out)

    def test_r5_xattr_colgado_se_acota_y_sigue(self):
        cache = self.cache_r5('atributos-colgados')
        posterior = self.cache_r5('zzz-posterior')
        self.config['xattr_block'] = 'atributos-colgados'
        out = self.ejecutar(poda_fixture=True)
        self.assertIn('tope 0.5 s: regla 7: xattr ruta ' + str(self.home / 'atributos-colgados'), out)
        self.assertTrue(cache.exists(), out)
        self.assertFalse(posterior.exists(), out)
        self.assertIn('fin (libre:', out)

    def test_r5_podas_ocultas_y_symlinks(self):
        podas = ('Documents', 'Desktop', 'OrbStack', 'Library', '.Trash', '.cache',
                 '.bun', '.rustup', '.android', '.cargo', 'remote-run', '.local/state')
        protegidas = [self.cache_r5(p) for p in podas]
        ocultas = [self.cache_r5(p) for p in ('.oculta', '..oculta', '.local/permitida')]
        externas = self.root / 'externa/.next/cache'
        externas.mkdir(parents=True)
        (self.home / 'enlace').symlink_to(externas.parent.parent, target_is_directory=True)
        (self.home / 'archivo').write_text('no es directorio')
        for p in ('app/node_modules', 'app/.git'):
            protegidas.append(self.cache_r5(p))
        out = self.ejecutar(poda_fixture=True)
        self.assertIn('fin (libre:', out)
        for cache in protegidas:
            self.assertTrue(cache.exists(), str(cache) + out)
        for cache in ocultas:
            self.assertFalse(cache.exists(), str(cache) + out)
        self.assertTrue(externas.exists())
        self.assertTrue((self.root / 'xattr-roots').exists(), 'no comprobó xattrs de raíces')
        consultadas = (self.root / 'xattr-roots').read_text().splitlines()
        for p in podas[:-1] + ('enlace', 'archivo'):
            self.assertNotIn(p, consultadas)
        visited = (self.root / 'visited').read_text().splitlines()
        for cache in protegidas:
            self.assertNotIn(str(cache.parent), visited)

    def test_home_colgado_se_corta_descarta_parcial_y_llega_a_fin(self):
        self.config['block'] = 'home'
        out = self.ejecutar()
        self.assertIn('tope 0.5 s: regla 7: candidatas ruta ' + str(self.home), out)
        self.assertIn('fin (libre:', out)
        self.assertNotIn('caché de Next.js', out)
        pid = int((self.root / 'blocked.pid').read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertFalse(self.lock.exists())

    def test_corte_actividad_copia_veta_borrado(self):
        self.config['block'] = 'copia'
        out = self.ejecutar()
        self.assertIn('tope 0.5 s: regla 1: actividad', out)
        self.assertIn('falló find de actividad de la copia', out)
        self.assertNotIn('[dry-run] borraría ' + str(self.rr / 'work/copia'), out)
        self.assertIn('fin (libre:', out)
        self.assertTrue((self.rr / 'work/copia/dato').exists())

    def lock_viejo(self, pid=None, inicio=None, comando=None):
        self.lock.mkdir()
        if pid is not None:
            (self.lock / 'owner').write_text(f'{pid}\n{inicio}\n{comando}\n')
        os.utime(self.lock, (time.time() - 121 * 60,) * 2)

    def dueno(self):
        script = self.root / 'dueno-limpieza-disco.sh'
        script.write_text('while :; do sleep 1; done\n')
        p = subprocess.Popen(['bash', str(script)], start_new_session=True)
        def cerrar():
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()
        self.addCleanup(cerrar)
        inicio = subprocess.check_output(['ps', '-p', str(p.pid), '-o', 'lstart='],
                                         env=dict(os.environ, LC_ALL='C', TZ='UTC0'), text=True).rstrip('\n')
        comando = subprocess.check_output(['ps', '-p', str(p.pid), '-o', 'command='], text=True).rstrip('\n')
        return p.pid, inicio, comando

    def test_lock_viejo_dueno_vivo_no_recupera(self):
        self.lock_viejo(*self.dueno())
        out = self.ejecutar()
        self.assertIn('más de 120 min con dueño vivo', out)
        self.assertNotIn('inicio (', out)
        self.assertNotIn('[dry-run] borraría', out)
        self.assertTrue((self.lock / 'owner').exists())

    def test_lock_legado_viejo_dueno_vivo_no_recupera(self):
        self.dueno()
        self.lock_viejo()
        out = self.ejecutar()
        self.assertIn('más de 120 min con dueño vivo', out)
        self.assertNotIn('inicio (', out)
        self.assertTrue(self.lock.exists())

    def test_lock_viejo_dueno_muerto_recupera(self):
        p = subprocess.Popen(['true'])
        p.wait()
        self.lock_viejo(p.pid, 'inicio anterior', 'bash limpieza-disco.sh')
        out = self.ejecutar()
        self.assertIn('recuperado lock de más de 120 min sin dueño vivo', out)
        self.assertIn('fin (libre:', out)
        self.assertFalse(self.lock.exists())

    def test_pid_reciclado_no_es_dueno(self):
        pid, _, cmd = self.dueno()
        self.lock_viejo(pid, 'inicio de otro proceso', cmd)
        out = self.ejecutar()
        self.assertIn('recuperado lock', out)
        self.assertIn('fin (libre:', out)

    def programa_lock(self, env, permiso_denegado=False):
        script = self.script.read_text()
        programa = script.split("<<'PY_LOCK'\n", 1)[1].split('\nPY_LOCK', 1)[0]
        if permiso_denegado:
            # Fuerza EPERM sin depender de ejecutar los tests como usuario sin root.
            programa = ("from unittest.mock import Mock\nimport os\n"
                        "os.kill = Mock(side_effect=PermissionError)\n" + programa)
        return subprocess.run(['/usr/bin/python3', '-B', '-', str(self.lock), str(os.getpid())],
                              input=programa, capture_output=True, text=True,
                              env=dict(os.environ, **env), timeout=8)

    def test_lock_locale_distinto_conserva_dueno_vivo(self):
        self.lock_viejo(*self.dueno())  # identidad C / UTC0
        owner = (self.lock / 'owner').read_text()
        p = self.programa_lock({'LC_ALL': 'es_MX.UTF-8', 'TZ': 'America/Mexico_City'})
        self.assertEqual(p.returncode, 10, p.stdout + p.stderr)
        self.assertEqual((self.lock / 'owner').read_text(), owner)
        self.assertIn('más de 120 min con dueño vivo', p.stdout)

    def test_lock_registro_normaliza_locale_y_zona(self):
        p = self.programa_lock({'LC_ALL': 'es_MX.UTF-8', 'TZ': 'America/Mexico_City'})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        inicio = subprocess.check_output(['ps', '-p', str(os.getpid()), '-o', 'lstart='],
                                         env=dict(os.environ, LC_ALL='C', TZ='UTC0'), text=True)
        self.assertEqual((self.lock / 'owner').read_text().splitlines()[1], inicio.rstrip('\n'))

    def test_lock_permission_error_pid_ajeno_recupera(self):
        campos = [subprocess.check_output(['ps', '-p', '1', '-o', campo + '='],
                                         env=dict(os.environ, LC_ALL='C', TZ='UTC0'),
                                         text=True).rstrip('\n') for campo in ('lstart', 'command')]
        self.assertNotIn('limpieza-disco.sh', campos[1])
        self.lock_viejo(1, *campos)
        p = self.programa_lock({'LC_ALL': 'C', 'TZ': 'UTC0'}, permiso_denegado=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('recuperado lock', p.stdout)
        self.assertEqual((self.lock / 'owner').read_text().splitlines()[0], str(os.getpid()))

    def soltar_fixture(self, caso, guard_ocupado=False):
        self.lock.mkdir()
        Path(str(self.lock) + '.guard').touch()
        funcion = self.script.read_text().split('soltar_lock() {', 1)[1].split('\n}', 1)[0]
        # Ejecuta la función real, sin ejecutar las reglas de limpieza.
        shell = r"""
log() { echo "$*"; }
pid=$$
inicio=$(LC_ALL=C TZ=UTC0 ps -p $$ -o lstart=)
comando=$(LC_ALL=C TZ=UTC0 ps -p $$ -o command=)
case "$1" in
  pid) pid=1 ;;
  inicio) inicio='inicio de otra pasada' ;;
  comando) comando='comando de otra pasada' ;;
esac
printf '%s\n%s\n%s\n' "$pid" "$inicio" "$comando" > "$LOCK/owner"
case "$1" in
  ps-ausente) export PATH=/ruta-inexistente ;;
  ps-falla) export PATH="$PS_FALLA" ;;
esac
soltar_lock
"""
        self.executable('ps', '#!/bin/sh\nexit 1\n')
        with open(str(self.lock) + '.guard', 'r+') as guard:
            if guard_ocupado:
                fcntl.flock(guard, fcntl.LOCK_EX)
            p = subprocess.Popen(['bash', '-c', 'soltar_lock() {' + funcion + '\n}\n' + shell,
                                  'fixture', caso], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, env=dict(os.environ, LOCK=str(self.lock),
                                                     PS_FALLA=str(self.bin), LC_ALL='es_MX.UTF-8',
                                                     TZ='America/Mexico_City'))
            try:
                if guard_ocupado:
                    plazo = time.monotonic() + 5
                    while not (self.lock / 'owner').exists() and p.poll() is None:
                        self.assertLess(time.monotonic(), plazo, 'fixture no publicó owner')
                        time.sleep(0.01)
                    # El guard sigue ocupado al entrar en soltar_lock.
                    time.sleep(0.3)
                    fcntl.flock(guard, fcntl.LOCK_UN)
                out, err = p.communicate(timeout=13)
            finally:
                if p.poll() is None:
                    p.kill()
                    p.communicate()
        self.assertEqual(p.returncode, 0, out + err)
        self.assertEqual(err, '', out + err)
        return out

    def test_soltar_lock_conserva_ajeno(self):
        out = self.soltar_fixture('pid')
        self.assertTrue((self.lock / 'owner').exists(), out)
        self.assertEqual((self.lock / 'owner').read_text().splitlines()[0], '1')
        self.assertRegex(out, r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} no suelto lock:')

    def test_soltar_lock_propio_con_locale_distinto(self):
        self.soltar_fixture('propio')
        self.assertFalse(self.lock.exists())

    def test_soltar_lock_guard_ocupado_brevemente(self):
        self.soltar_fixture('propio', guard_ocupado=True)
        self.assertFalse(self.lock.exists())

    def test_soltar_lock_sin_ps(self):
        for caso in ('ps-ausente', 'ps-falla'):
            with self.subTest(caso=caso):
                try:
                    self.soltar_fixture(caso)
                    self.assertFalse(self.lock.exists())
                finally:
                    # Un fallo contra la base no debe impedir el siguiente subcaso.
                    shutil.rmtree(self.lock, ignore_errors=True)

    def test_soltar_lock_propio_no_revalida_identidad(self):
        for caso in ('inicio', 'comando'):
            with self.subTest(caso=caso):
                try:
                    self.soltar_fixture(caso)
                    self.assertFalse(self.lock.exists())
                finally:
                    # Un fallo contra la base no debe impedir el siguiente subcaso.
                    shutil.rmtree(self.lock, ignore_errors=True)

    def test_home_poda_icloud_y_orbstack(self):
        self.ejecutar()
        visited = (self.root / 'visited').read_text().splitlines()
        self.assertIn(str(self.home / 'app/hijo'), visited)
        for carpeta in ('Documents', 'Desktop', 'OrbStack'):
            self.assertFalse(any(p.startswith(str(self.home / carpeta) + '/') for p in visited), visited)
        self.assertIn('caché de Next.js', self.log)

    def test_corte_actividad_next_veta_cache(self):
        self.config['block'] = 'next'
        out = self.ejecutar()
        self.assertIn('tope 0.5 s: regla 7: actividad', out)
        self.assertNotIn('caché de Next.js', out)
        self.assertIn('fin (libre:', out)

    def test_corte_du_descarta_tamano_parcial(self):
        self.config['block'] = 'du'
        out = self.ejecutar()
        self.assertIn('tope 0.5 s: regla 7: tamaño', out)
        self.assertNotIn('caché de Next.js', out)
        self.assertIn('fin (libre:', out)

    def test_corte_pytest_veta_temporal(self):
        p = self.home / 'pytest-bt-viejo'
        p.mkdir()
        self.envejecer(p)
        self.config['block'] = 'pytest'
        out = self.ejecutar()
        self.assertIn('tope 0.5 s: regla 2: actividad', out)
        self.assertNotIn('[dry-run] borraría ' + str(p), out)
        self.assertIn('fin (libre:', out)

    def test_corte_lsof_veta_copia(self):
        self.config['block'] = 'lsof'
        out = self.ejecutar()
        self.assertIn('tope 0.5 s: reglas 1 y 3: lsof previo', out)
        self.assertNotIn('[dry-run] borraría ' + str(self.rr / 'work/copia'), out)
        self.assertIn('fin (libre:', out)

    def test_lsof_rc1_con_salida_parcial_veta_copia(self):
        self.config['block'] = 'lsof-parcial'
        out = self.ejecutar()
        self.assertNotIn('[dry-run] borraría ' + str(self.rr / 'work/copia'), out)
        self.assertIn('captura parcial', out)

    def test_corte_corrida_nueva_veta_copia(self):
        self.config['block'] = 'runs'
        out = self.ejecutar()
        self.assertIn('tope 0.5 s: reglas 1 y 3: corridas nuevas', out)
        self.assertNotIn('[dry-run] borraría ' + str(self.rr / 'work/copia'), out)
        self.assertIn('fin (libre:', out)


if __name__ == '__main__':
    unittest.main()
