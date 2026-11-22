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
    def ejecutar(self, cmd, copia='copia', inventario_json=None, mtime_nuevo=True):
        funcion = (DIRECTORIO / 'limpieza-disco.sh').read_text().split(
            'corrida_nueva() {', 1)[1].split('\nborrar_en() {', 1)[0]
        bloque = funcion.split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / 'runs'
            ruta = runs / 'nueva'
            ruta.mkdir(parents=True)
            (ruta / 'meta').write_bytes(
                b'src=/src\nhost=mini\nmirror=remote-run/work/copia\nsub=.\ncmd=' + cmd)
            listado = Path(tmp) / 'listado'
            listado.write_bytes(os.fsencode(ruta) + b'\0' if mtime_nuevo else b'')
            captura = Path(tmp) / 'inventario.json'
            if inventario_json is not None:
                captura.write_text(inventario_json)
            return subprocess.run(
                [sys.executable, '-B', '-', str(INVENTARIO), str(listado), copia,
                 str(captura), str(runs)],
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

    def test_inventario_ausente_o_ilegible_conserva_el_filtro_mtime(self):
        for captura in (None, 'no es JSON', '{"corridas": {}}'):
            with self.subTest(captura=captura):
                vieja = self.ejecutar(b'true\n', inventario_json=captura, mtime_nuevo=False)
                nueva = self.ejecutar(b'true\n', inventario_json=captura)
                self.assertEqual(vieja.returncode, 0, vieja.stderr)
                self.assertEqual(nueva.returncode, 1, nueva.stderr)
                self.assertIn('runs/nueva (mtime)', nueva.stderr)
