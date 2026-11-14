import copy
import importlib.util
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'vigia.py'
spec = importlib.util.spec_from_file_location('vigia', SCRIPT)
vigia = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vigia)
REAL = json.loads((ROOT.parent/'rack/tests/mini-pueue.json').read_text())

PUEUE = '''#!/usr/bin/python3
import os, pathlib, sys
assert sys.argv[1:] == ['status', '--json']
p = pathlib.Path(os.environ['FAKE_ROOT'])
if (p/'fail').exists(): sys.exit(1)
print((p/'data.json').read_text())
'''
SSH = '''#!/usr/bin/python3
import os, pathlib, shlex, sys
p = pathlib.Path(os.environ['FAKE_ROOT'])
with (p/'ssh-pids').open('a') as f: f.write(str(os.getpid())+'\\n')
if (p/'offline').exists(): sys.exit(1)
assert 'ControlPath=none' in sys.argv
args = shlex.split(sys.argv[-1])
os.execv(args[0], args)
'''


class Huellas(unittest.TestCase):
    def test_reales_estables_y_campos(self):
        for task in REAL['tasks'].values():
            huella = vigia.huella_de(task)
            token = vigia.token_de(huella)
            self.assertEqual(token, vigia.token_de(dict(reversed(list(huella.items())))))
            other = copy.deepcopy(task)
            for k in ('envs', 'path', 'label', 'priority', 'original_command', 'created_at'):
                other[k] = 'ajeno'
            self.assertEqual(token, vigia.token_de(vigia.huella_de(other)))
            for campo in huella:
                altered = dict(huella, **{campo: 'cambio'})
                self.assertNotEqual(token, vigia.token_de(altered), campo)
        self.assertEqual(vigia.token_de(None), 'ausente')
        self.assertEqual(vigia.huella_de(REAL['tasks']['238'])['resultado'], {'Failed': 1})
        self.assertEqual(vigia.huella_de(REAL['tasks']['242'])['estado'], 'Running')


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='vigia-test-')
        self.root = Path(self.temp.name)
        self.env = dict(os.environ, FAKE_ROOT=str(self.root),
                        PAO_VIGIA_STATE_DIR=str(self.root/'local'),
                        PAO_VIGIA_STATE_FILE=str(self.root/'state.json'),
                        PAO_VIGIA_SSH=str(self.root/'ssh'),
                        PAO_VIGIA_TICK='.3', PAO_VIGIA_IDLE='2', PAO_VIGIA_CONTACT='.5',
                        PATH=str(self.root)+os.pathsep+os.environ['PATH'])
        for name, code in [('pueue', PUEUE), ('ssh', SSH)]:
            p = self.root/name
            p.write_text(code)
            p.chmod(0o755)
        self.procs = []
        self.write(REAL)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        # Sólo procesos y archivos de esta fixture, ejecutada en desk.
        for proc in self.procs:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream:
                    stream.close()
        # El corredor hereda el candado; localizar sólo el de esta fixture.
        pidfile = self.root/'ssh-pids'
        if pidfile.exists():
            for pid in pidfile.read_text().splitlines():
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
        # Esperar el idle del corredor antes de retirar la fixture.
        time.sleep(2.2)
        self.temp.cleanup()

    def write(self, data):
        (self.root/'data.json').write_text(json.dumps(data))
        (self.root/'state.json').write_text(str(time.monotonic()))

    def launch(self, *args):
        p = subprocess.Popen([sys.executable, '-I', str(SCRIPT), *args], env=self.env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True)
        self.procs.append(p)
        return p

    def timbre(self, visto='-', plazo='0'):
        return self.launch('timbre', 'desk', '238', visto, plazo)

    def resultado(self, p, timeout=16):
        out, err = p.communicate(timeout=timeout)
        self.assertEqual(p.returncode, 0, err)
        return dict(line.split('=', 1) for line in out.splitlines())

    def esperar(self, pred, seconds=4):
        end = time.monotonic()+seconds
        while time.monotonic() < end:
            if pred():
                return
            time.sleep(.05)
        self.fail('No llegó la condición')


class Flujo(Fixture):
    def setUp(self):
        super().setUp()
        self.p = self.launch('flujo')
        self.lines = queue.Queue()
        def read():
            for line in self.p.stdout:
                self.lines.put(line.strip())
        threading.Thread(target=read, daemon=True).start()

    def subscribe(self):
        self.p.stdin.write('S 238\n')
        self.p.stdin.flush()

    def hasta(self, prefix, timeout=2):
        end = time.monotonic()+timeout
        while time.monotonic() < end:
            try:
                line = self.lines.get(timeout=max(.01, end-time.monotonic()))
            except queue.Empty:
                break
            if line.startswith(prefix):
                return line
        self.fail('No llegó '+prefix)

    def test_inmediato_cambio_reescritura_latido_eof(self):
        start = time.monotonic()
        self.subscribe()
        first = self.hasta('T ')
        self.assertLess(time.monotonic()-start, 1)
        self.hasta('L')
        self.write(REAL)
        end = time.monotonic()+1.2
        while time.monotonic() < end:
            try:
                self.assertFalse(self.lines.get(timeout=.1).startswith('T '))
            except queue.Empty:
                pass
        changed = copy.deepcopy(REAL)
        changed['tasks']['238']['status']['Done']['result'] = 'Success'
        start = time.monotonic()
        self.write(changed)
        self.assertNotEqual(first, self.hasta('T '))
        self.assertLess(time.monotonic()-start, 2)
        self.p.stdin.close()
        self.p.wait(timeout=2)
        self.assertEqual(self.p.returncode, 0)

    def test_fallo_sin_latidos_y_recuperacion(self):
        self.subscribe()
        first = self.hasta('T ')
        (self.root/'fail').touch()
        self.hasta('E ', timeout=2)
        end = time.monotonic()+1.2
        while time.monotonic() < end:
            try:
                self.assertNotEqual(self.lines.get(timeout=.1), 'L')
            except queue.Empty:
                pass
        (self.root/'fail').unlink()
        self.assertEqual(self.hasta('T ', timeout=2), first)
        self.hasta('L', timeout=2)


class Corredor(Fixture):
    def test_dos_timbres_un_corredor_mismo_cambio_y_plazo(self):
        a, b = self.timbre(), self.timbre()
        ra, rb = self.resultado(a), self.resultado(b)
        self.assertEqual(ra, rb)
        self.assertEqual(ra['VIGIA'], 'cambio')
        self.assertEqual(len((self.root/'ssh-pids').read_text().splitlines()), 1)
        self.assertEqual((self.root/'local/desk.sock').stat().st_mode & 0o777, 0o600)
        token = ra['VIGIA_TOKEN']
        start = time.monotonic()
        r = self.resultado(self.timbre(token, '1'))
        self.assertEqual(r['VIGIA'], 'plazo')
        self.assertEqual(r['VIGIA_TOKEN'], token)
        self.assertGreaterEqual(time.monotonic()-start, 1)
        self.assertLess(time.monotonic()-start, 2)
        changed = copy.deepcopy(REAL)
        changed['tasks']['238']['status']['Done']['result'] = 'Success'
        a, b = self.timbre(token, '4'), self.timbre(token, '4')
        time.sleep(.2)
        self.write(changed)
        ra, rb = self.resultado(a), self.resultado(b)
        self.assertEqual(ra, rb)
        self.assertEqual(ra['VIGIA'], 'cambio')
        self.assertNotEqual(ra['VIGIA_TOKEN'], token)

    def test_caida_reconexion_sin_falso_cambio(self):
        token = self.resultado(self.timbre())['VIGIA_TOKEN']
        (self.root/'offline').touch()
        pid = int((self.root/'ssh-pids').read_text().splitlines()[0])
        p = self.timbre(token, '2')
        time.sleep(.2)
        os.kill(pid, signal.SIGTERM)
        self.assertEqual(self.resultado(p), {'VIGIA': 'sin-vigia'})
        (self.root/'offline').unlink()
        p = self.timbre(token, '5')
        self.assertEqual(self.resultado(p)['VIGIA'], 'plazo')
        self.assertGreater(len((self.root/'ssh-pids').read_text().splitlines()), 1)

    def test_idle_sale_y_reemplaza_socket_residual(self):
        self.env['PAO_VIGIA_IDLE'] = '.4'
        p = self.launch('corredor', 'desk')
        p.wait(timeout=3)
        self.assertEqual(p.returncode, 0)
        self.assertTrue((self.root/'local/desk.sock').exists())
        self.assertEqual(self.resultado(self.timbre())['VIGIA'], 'cambio')

    def test_no_reemplaza_archivo_que_no_es_socket(self):
        local = self.root/'local'
        local.mkdir()
        residual = local/'desk.sock'
        residual.write_text('conservar')
        p = self.launch('corredor', 'desk')
        p.communicate(timeout=2)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(residual.read_text(), 'conservar')

    def test_sin_vigia_no_token(self):
        (self.root/'offline').touch()
        self.assertEqual(self.resultado(self.timbre(), timeout=16), {'VIGIA': 'sin-vigia'})

    def test_argumentos_invalidos(self):
        for args in [('otro', '1', '-', '0'), ('desk', 'a', '-', '0'),
                     ('desk', '1', 'basura', '0'), ('desk', '1', '-', '901'),
                     ('desk', '1', '-', '-1'), ('desk', '1', '-', 'nan')]:
            p = self.launch('timbre', *args)
            out, err = p.communicate(timeout=2)
            self.assertEqual(p.returncode, 2)
            self.assertEqual(out, '')
            self.assertTrue(err)


class Instalador(unittest.TestCase):
    def test_respaldo_retirada_y_wrapper_con_espacios(self):
        with tempfile.TemporaryDirectory(prefix="vigia-home '") as tmp:
            env = dict(os.environ, HOME=tmp)
            script = str(ROOT/'instalar.sh')
            def run(*args):
                return subprocess.run([script, *args], env=env, check=True,
                                      capture_output=True, text=True).stdout
            self.assertIn('Copiar', run('--seco'))
            self.assertFalse((Path(tmp)/'.local').exists())
            run()
            wrapper = Path(tmp)/'.local/bin/pao-vigia'
            lib = Path(tmp)/'.local/lib/pao-vigia/vigia.py'
            before = wrapper.read_bytes()
            r = subprocess.run([str(wrapper), '--help'], env=env, capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            run()
            self.assertEqual(next(wrapper.parent.glob('pao-vigia.bak-*')).read_bytes(), before)
            run('--retirar')
            self.assertFalse(wrapper.exists())
            self.assertFalse(lib.exists())
            self.assertEqual(next(wrapper.parent.glob('pao-vigia.retirado-*')).read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
