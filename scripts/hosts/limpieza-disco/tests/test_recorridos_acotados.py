"""Pasada completa aislada; el tope corto y PATH sólo cambian en el fixture.

LIMPIEZA_TEST_SOURCE permite ejecutar estos mismos tests contra otra revisión.
Nunca consulta /private/tmp ni HOME reales. Sólo los tests de mktemp
ejecutan la pasada sin --dry-run. Los tests r5 habilitan el borrado de las
cachés aisladas del fixture sin quitar --dry-run.
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
ADELANTO_MKTEMP_DIAS = 8
SEGUNDOS_DIA = 86400


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
import json, os, subprocess, sys, time
from pathlib import Path
root = Path(os.environ['HARNESS'])
c = json.loads((root / 'config.json').read_text())
if '+D' in sys.argv:
    if c['block'] == 'lsof': time.sleep(30)
    if c['block'] == 'lsof-parcial': print('captura parcial')
    ruta = Path(sys.argv[-1])
    if ruta.name == c.get('mktemp_modificar'):
        dato = ruta / 'uno/dos/tres/cuatro/dato'
        dato.write_text('escritura durante lsof previo')
        instante = c['mktemp_fecha_carrera']
        os.utime(dato, (instante, instante))
    if ruta.name.startswith('borrando-mktemp-'):
        if c.get('mktemp_origen_ocupado'):
            origen = Path(c['mktemp_origen_ocupado'])
            origen.mkdir()
            (origen / 'nuevo').write_text('conservar el nuevo origen')
            print('captura de cuarentena no disponible', file=sys.stderr)
            sys.exit(1)
        if c.get('mktemp_pid_abierto'):
            captura = subprocess.run(
                ['/usr/sbin/lsof', '-a', '-p', str(c['mktemp_pid_abierto']),
                 '-nP', '-Ffi', '+D', str(ruta)], capture_output=True, text=True)
            (root / 'lsof-cuarentena').write_text(captura.stdout)
            sys.stdout.write(captura.stdout)
            sys.stderr.write(captura.stderr)
            sys.exit(captura.returncode)
    sys.exit(1)
if c.get('lsof_error'):
    print('captura no disponible', file=sys.stderr)
    sys.exit(1)
captura = 'lsof_cwd' if '-d' in sys.argv else 'lsof_abiertos'
print(c.get(captura, 'p123\\nn' + os.environ['HARNESS']))
''')
        self.executable('ps', '''#!/usr/bin/python3 -B
import os, sys
if sys.argv[1:3] == ['-axww', '-o'] and sys.argv[3:] in (['command='], ['uid=,command=']):
    sys.exit(0)
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
                                'libre_gb() { echo "${LIBRE_FIXTURE:-41}"; }')
        script = script.replace('RECORRIDO_SEGUNDOS=600', 'RECORRIDO_SEGUNDOS=0.5')
        script = script.replace('/private/tmp', str(self.root / 'tmp'))
        # El simctl falso también aísla las pasadas de mktemp sin --dry-run.
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
        script = script.replace('listxattr = libc.listxattr\n', r'''import errno
import json
from pathlib import Path

config = json.loads((Path(os.environ['HARNESS']) / 'config.json').read_text())
listxattr_real = libc.listxattr


def listxattr(ruta, buffer, tamano, opciones):
    assert opciones == 0x0001  # XATTR_NOFOLLOW de macOS
    nombre = Path(os.fsdecode(ruta)).name
    if nombre == config.get('listxattr_error'):
        ctypes.set_errno(errno.EACCES)
        return -1
    if nombre == config.get('listxattr_block'):
        time.sleep(30)
    atributo = config.get('listxattrs', {}).get(nombre)
    if atributo is None:
        return listxattr_real(ruta, buffer, tamano, opciones)
    datos = atributo.encode() + b'\0'
    if buffer is not None:
        assert tamano == len(datos)
        ctypes.memmove(buffer, datos, tamano)
    return len(datos)


listxattr_real.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
listxattr_real.restype = ctypes.c_ssize_t
''')
        self.script = self.root / 'limpieza-disco.sh'
        self.plantilla_script = script
        self.script.write_text(script)
        shutil.copyfile(HOSTS / 'inventario/inventario.py', self.root / 'pao-inventario.py')

    def executable(self, name, text):
        p = self.bin / name
        p.write_text(text)
        p.chmod(0o755)

    def envejecer(self, p, dias=9):
        instante = time.time() - dias * SEGUNDOS_DIA
        for ruta in sorted(p.rglob('*'), reverse=True) + [p]:
            os.utime(ruta, (instante, instante))

    def ejecutar(self, poda_fixture=False, *, dry_run=True, libre=41, tope_pasada=8,
                 adelanto_mktemp_dias=ADELANTO_MKTEMP_DIAS):
        # Sólo el reloj del recorrido de mktemp. utime no envejece el ctime.
        adelanto = adelanto_mktemp_dias * SEGUNDOS_DIA
        self.script.write_text(self.plantilla_script.replace(
            'ahora = time.time()\n', f'ahora = time.time() + {adelanto}\n'))
        (self.root / 'config.json').write_text(json.dumps(self.config))
        start = time.monotonic()
        comando = ['bash', str(self.script)]
        if dry_run:
            comando.append('--dry-run')
        p = subprocess.Popen(comando, start_new_session=True,
                             env=dict(os.environ, HOME=str(self.home), HARNESS=str(self.root),
                                      LIBRE_FIXTURE=str(libre),
                                      PODA_FIXTURE=str(int(poda_fixture))),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            out, err = p.communicate(timeout=tope_pasada)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            out, err = p.communicate()
            self.fail(f'pasada no llegó a fin en {tope_pasada} s:\n' + out + err)
        self.log = out
        self.assertEqual(p.returncode, 0, out + err)
        self.assertEqual(err, '', out + err)
        self.assertLess(time.monotonic() - start, tope_pasada)
        return out

    def ejecutar_mktemp(self, **opciones):
        # Dos recorridos y dos lsof por candidata se suman a la pasada.
        return self.ejecutar(tope_pasada=15, **opciones)

    def cache_r5(self, nombre):
        cache = self.home / nombre / 'proyecto/.next/cache'
        cache.mkdir(parents=True)
        (cache / 'dato').write_text('fixture borrable')
        self.envejecer(self.home / nombre)
        return cache

    def temporal_mktemp(self, nombre='x.AbCd1234'):
        ruta = self.home / nombre
        ruta.mkdir(mode=0o700)
        dato = ruta / 'uno/dos/tres/cuatro/dato'
        dato.parent.mkdir(parents=True)
        dato.write_text('fixture mktemp')
        self.envejecer(ruta, 8)
        return ruta, dato

    def test_mktemp_viejo_renombra_y_borra(self):
        ruta, _ = self.temporal_mktemp()
        out = self.ejecutar_mktemp(dry_run=False)
        self.assertFalse(ruta.exists(), 'el temporal mktemp viejo sigue en el home\n' + out)
        self.assertIn(f'renombrada {ruta} a {self.state}/borrando-', out)
        self.assertIn(f'borrado {ruta} (regla 9:', out)
        self.assertFalse(list(self.state.glob('borrando-*')), out)

    def test_mktemp_archivo_profundo_reciente_conserva_el_arbol(self):
        ruta, dato = self.temporal_mktemp()
        ayer_del_recorrido = time.time() + (ADELANTO_MKTEMP_DIAS - 1) * SEGUNDOS_DIA
        os.utime(dato, (ayer_del_recorrido, ayer_del_recorrido))
        out = self.ejecutar_mktemp(dry_run=False)
        self.assertTrue(dato.exists(), out)
        self.assertIn(f'no borro {ruta}: algo cambió en los últimos 7 días', out)

    def test_mktemp_emergencia_general_usa_dos_dias(self):
        vieja, _ = self.temporal_mktemp()
        reciente, _ = self.temporal_mktemp('y.DeFg5678')
        self.envejecer(vieja, 3 - ADELANTO_MKTEMP_DIAS)
        self.envejecer(reciente, 1 - ADELANTO_MKTEMP_DIAS)
        out = self.ejecutar_mktemp(dry_run=False, libre=15)
        self.assertTrue(vieja.exists(), out)
        out = self.ejecutar_mktemp(dry_run=False, libre=14)
        self.assertFalse(vieja.exists(), out)
        self.assertTrue(reciente.exists(), out)
        self.assertIn(f'no borro {reciente}: algo cambió en los últimos 2 días', out)

    def test_mktemp_cwd_y_archivo_abierto_conservan_sus_arboles(self):
        cwd, dato_cwd = self.temporal_mktemp()
        abiertos, dato_abierto = self.temporal_mktemp('y.DeFg5678')
        proceso = subprocess.Popen(['/bin/sleep', '120'], cwd=dato_cwd.parent)
        self.addCleanup(proceso.wait)
        self.addCleanup(proceso.terminate)
        self.config['lsof_cwd'] = subprocess.run(
            ['/usr/sbin/lsof', '-a', '-p', str(proceso.pid), '-d', 'cwd', '-Fn'],
            capture_output=True, text=True, check=True).stdout
        self.assertIn(str(dato_cwd.parent), self.config['lsof_cwd'])
        with dato_abierto.open():
            self.config['lsof_abiertos'] = subprocess.run(
                ['/usr/sbin/lsof', '-p', str(os.getpid()), '-Fn'],
                capture_output=True, text=True, check=True).stdout
            self.assertIn(str(dato_abierto), self.config['lsof_abiertos'])
            out = self.ejecutar_mktemp(dry_run=False)
        for ruta in (cwd, abiertos):
            self.assertTrue(ruta.exists(), out)
            self.assertIn(f'no borro {ruta}: aparece en EN_USO', out)

    def test_mktemp_nombres_invalidos_enlace_y_archivo_se_conservan(self):
        nombres = ('x.backup12', 'x.ABCDEF12',
                   '.AbCd1234', '.x.AbCd1234', 'x.y.AbCd1234', 'x+y.AbCd1234')
        carpetas = [self.temporal_mktemp(nombre)[0] for nombre in nombres]
        # Un árbol que cumpliría bajo el enlace, fuera del primer nivel del home.
        destino = self.root / 'destino'
        destino.mkdir()
        self.envejecer(destino)
        enlace = self.home / 'enlace.AbCd1234'
        enlace.symlink_to(destino, target_is_directory=True)
        archivo = self.home / 'archivo.AbCd1234'
        archivo.touch()
        self.envejecer(archivo)
        out = self.ejecutar_mktemp(dry_run=False)
        for ruta in carpetas + [destino, enlace, archivo]:
            self.assertTrue(ruta.exists(), str(ruta) + '\n' + out)
        for ruta in (enlace, archivo):
            self.assertIn(f'no borro {ruta}: no es un directorio propio sin enlace', out)

    def test_mktemp_ctime_reciente_con_mtime_viejo_conserva_el_arbol(self):
        ruta, dato = self.temporal_mktemp()
        out = self.ejecutar_mktemp(dry_run=False, adelanto_mktemp_dias=0)
        self.assertTrue(dato.exists(), 'se borró un árbol con ctime reciente\n' + out)
        self.assertIn(f'no borro {ruta}: algo cambió en los últimos 7 días', out)

    def test_mktemp_cuarentena_devuelve_arbol_con_archivo_modificado(self):
        ruta, dato = self.temporal_mktemp()
        self.config.update(mktemp_modificar=ruta.name,
                           mktemp_fecha_carrera=time.time() + ADELANTO_MKTEMP_DIAS * SEGUNDOS_DIA)
        out = self.ejecutar_mktemp(dry_run=False)
        self.assertTrue(dato.exists(), 'se borró un árbol modificado antes del rename\n' + out)
        self.assertEqual(dato.read_text(), 'escritura durante lsof previo')
        self.assertIn(f'renombrada {ruta} a {self.state}/borrando-mktemp-', out)
        self.assertIn('algo cambió en los últimos 7 días:', out)
        self.assertIn(f'devuelta {ruta} a su ruta original', out)
        self.assertFalse(list(self.state.glob('borrando-mktemp-*')), out)

    def test_mktemp_cuarentena_devuelve_arbol_con_descriptor_abierto(self):
        ruta, dato = self.temporal_mktemp()
        self.config['mktemp_pid_abierto'] = os.getpid()
        # El doble sólo consulta este descriptor con lsof real en cuarentena.
        with dato.open() as abierto:
            out = self.ejecutar_mktemp(dry_run=False)
            self.assertTrue(dato.exists(), 'se borró un árbol con descriptor abierto\n' + out)
            self.assertEqual(abierto.read(), 'fixture mktemp')
            captura = (self.root / 'lsof-cuarentena').read_text()
            self.assertIn(f'f{abierto.fileno()}\ni{dato.stat().st_ino}\n', captura)
        self.assertIn(f'renombrada {ruta} a {self.state}/borrando-mktemp-', out)
        self.assertIn('uso no descartado en cuarentena', out)
        self.assertIn(f'devuelta {ruta} a su ruta original', out)
        self.assertFalse(list(self.state.glob('borrando-mktemp-*')), out)

    def test_mktemp_cuarentena_conserva_arbol_si_el_origen_esta_ocupado(self):
        ruta, dato = self.temporal_mktemp()
        self.config['mktemp_origen_ocupado'] = str(ruta)
        out = self.ejecutar_mktemp(dry_run=False)
        cuarentenas = list(self.state.glob('borrando-mktemp-*'))
        self.assertEqual(len(cuarentenas), 1, out)
        self.assertEqual((cuarentenas[0] / dato.relative_to(ruta)).read_text(), 'fixture mktemp')
        self.assertEqual((ruta / 'nuevo').read_text(), 'conservar el nuevo origen')
        self.assertIn('captura de cuarentena no disponible', out)
        self.assertIn('queda en STATE; requiere autorización explícita', out)

    def test_mktemp_modo_0755_conserva_el_arbol(self):
        ruta, dato = self.temporal_mktemp()
        ruta.chmod(0o755)
        out = self.ejecutar_mktemp(dry_run=False)
        self.assertTrue(dato.exists(), 'se borró un árbol con modo 0755\n' + out)
        self.assertIn(f'no borro {ruta}: modo distinto de 0700', out)

    def test_mktemp_sufijos_de_seis_a_ocho_sin_forma_de_palabra(self):
        conservadas = [self.temporal_mktemp(nombre)[0] for nombre in
                       ('x.Backup12', 'x.Frontend', 'x.AbCdE', 'x.AbCdEfGh1')]
        borradas = [self.temporal_mktemp(nombre)[0] for nombre in ('x.AbC123', 'x.AbCd123')]
        out = self.ejecutar_mktemp(dry_run=False)
        for ruta in conservadas:
            self.assertTrue(ruta.is_dir(), str(ruta) + '\n' + out)
        for ruta in borradas:
            self.assertFalse(ruta.exists(), str(ruta) + '\n' + out)
            self.assertIn(f'borrado {ruta} (regla 9:', out)

    def test_mktemp_dry_run_solo_registra(self):
        ruta, dato = self.temporal_mktemp()
        out = self.ejecutar_mktemp()
        self.assertTrue(dato.exists(), out)
        self.assertIn(f'[dry-run] borraría {ruta} (regla 9:', out)
        self.assertNotIn('renombrada ', out)
        self.assertFalse(list(self.state.glob('borrando-*')), out)

    def test_mktemp_atributos_y_tope_conservan_el_arbol(self):
        for nombre, hijo in (('macl', 'interior'), ('proveedor', 'dominio'), ('tope', 'atascada')):
            ruta = self.home / f'{nombre}.AbCd1234'
            ruta.mkdir(mode=0o700)
            (ruta / hijo).mkdir()
            self.envejecer(ruta, 8)
        self.config.update(
            listxattrs={'macl.AbCd1234': 'com.apple.macl#S',
                        'dominio': 'com.apple.file-provider-domain-id#B'},
            listxattr_block='atascada')
        out = self.ejecutar_mktemp(dry_run=False)
        for nombre, motivo in (('macl', 'com.apple.macl'),
                               ('proveedor', 'com.apple.file-provider-domain-id'),
                               ('tope', 'actividad no descartada')):
            ruta = self.home / f'{nombre}.AbCd1234'
            self.assertTrue(ruta.exists(), out)
            self.assertIn(f'no borro {ruta}: {motivo}', out)
        self.assertIn('tope 0.5 s: regla 9: actividad ruta ./tope.AbCd1234', out)

    def test_mktemp_error_listxattr_conserva_el_arbol(self):
        ruta, dato = self.temporal_mktemp()
        self.config['listxattr_error'] = dato.parent.name
        out = self.ejecutar_mktemp(dry_run=False)
        self.assertTrue(dato.exists(), out)
        self.assertIn(f'no borro {ruta}: falló listxattr', out)

    def test_mktemp_lsof_fallido_o_parcial_conserva_el_arbol(self):
        ruta, _ = self.temporal_mktemp()
        self.config['lsof_error'] = True
        out = self.ejecutar_mktemp(dry_run=False)
        self.assertTrue(ruta.exists(), out)
        self.assertIn(f'no borro {ruta}: aparece en EN_USO o no pudo consultarse', out)
        self.config.update(lsof_error=False, block='lsof-parcial')
        out = self.ejecutar_mktemp(dry_run=False)
        self.assertTrue(ruta.exists(), out)
        self.assertIn(f'no borro {ruta}: lsof salió 1: captura parcial', out)

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
