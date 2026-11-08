"""Pasada completa aislada; el tope corto y PATH sólo cambian en el fixture.

LIMPIEZA_TEST_SOURCE permite ejecutar estos mismos tests contra origin/main.
Nunca consulta /private/tmp ni HOME reales, y siempre usa --dry-run.
"""
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
block = ((c['block'] == 'home' and a[0] == home)
         or (c['block'] == 'copia' and a[0] == './copia')
         or (c['block'] == 'next' and a[0].endswith('.next/cache'))
         or (c['block'] == 'pytest' and a[0].endswith('pytest-bt-viejo'))
         or (c['block'] == 'runs' and a[0].endswith('/remote-run/runs')))
if block:
    (root / 'blocked.pid').write_text(str(os.getpid()))
    if c['block'] == 'home':
        print(home + '/app/hijo/.next', flush=True)  # salida parcial
    time.sleep(30)
if a[0] == home:
    # La misma expresión de poda, publicando cada directorio visitado.
    assert a[-6:] == ['-type', 'd', '-name', '.next', '-print', '-prune'], a
    visited = subprocess.run(['/usr/bin/find'] + a[:-6] + ['-type', 'd', '-print'],
                             capture_output=True, text=True, check=True)
    (root / 'visited').write_text(visited.stdout)
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
        script = (HOSTS / 'limpieza-disco/limpieza-disco.sh').read_text()
        script = script.replace(
            'export PATH="$HOME/.bun/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"',
            'export PATH=' + shlex.quote(str(self.bin)) + ':/usr/bin:/bin:/usr/sbin:/sbin')
        script = script.replace("libre_gb() { df -g /System/Volumes/Data | awk 'NR==2{print $4}'; }",
                                'libre_gb() { echo 41; }')
        script = script.replace('RECORRIDO_SEGUNDOS=600', 'RECORRIDO_SEGUNDOS=0.5')
        script = script.replace('/private/tmp', str(self.root / 'tmp'))
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

    def ejecutar(self):
        (self.root / 'config.json').write_text(json.dumps(self.config))
        start = time.monotonic()
        p = subprocess.Popen(['bash', str(self.script), '--dry-run'], start_new_session=True,
                             env=dict(os.environ, HOME=str(self.home), HARNESS=str(self.root)),
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
        inicio = subprocess.check_output(['ps', '-p', str(p.pid), '-o', 'lstart='], text=True).rstrip('\n')
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
