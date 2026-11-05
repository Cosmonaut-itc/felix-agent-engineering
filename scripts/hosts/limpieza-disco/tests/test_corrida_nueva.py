"""Ejecuta sólo el bloque Python de corrida_nueva, sin iniciar la limpieza."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


DIRECTORIO = Path(__file__).resolve().parents[1]
INVENTARIO = DIRECTORIO.parent / 'inventario' / 'inventario.py'


class CorridaNuevaTest(unittest.TestCase):
    def ejecutar(self, cmd, copia='copia'):
        funcion = (DIRECTORIO / 'limpieza-disco.sh').read_text().split(
            'corrida_nueva() {', 1)[1].split('\nborrar_en() {', 1)[0]
        bloque = funcion.split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / 'nueva'
            ruta.mkdir()
            (ruta / 'meta').write_bytes(
                b'src=/src\nhost=mini\nmirror=remote-run/work/copia\nsub=.\ncmd=' + cmd)
            listado = Path(tmp) / 'listado'
            listado.write_bytes(os.fsencode(ruta) + b'\0')
            return subprocess.run(
                [sys.executable, '-B', '-', str(INVENTARIO), str(listado), copia],
                input=bloque, text=True, capture_output=True)

    def comprobar_veto(self, cmd):
        resultado = self.ejecutar(cmd)
        self.assertEqual(resultado.returncode, 1, resultado.stderr)
        self.assertNotIn('Traceback', resultado.stderr)
        self.assertIn('corrida nueva asociada a la copia en ', resultado.stderr)

    def test_meta_normal_veta_su_copia(self):
        self.comprobar_veto(b'true\n')

    def test_cmd_no_utf8_veta_su_copia_sin_traceback(self):
        self.comprobar_veto(b"echo '\xff\xc3'\notra linea\n")

    def test_meta_de_otra_copia_no_veta(self):
        resultado = self.ejecutar(b'true\n', copia='otra')
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertEqual(resultado.stderr, '')
