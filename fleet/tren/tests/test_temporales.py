"""SEN-7935: las fixtures devuelven vacío el directorio temporal."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class TemporalesTests(unittest.TestCase):
    def test_fixtures_no_dejan_entradas_en_tmpdir(self):
        pruebas = str(Path(__file__).resolve().parent)
        with tempfile.TemporaryDirectory(prefix="pao-tren-limpieza-") as tmp:
            resultado = subprocess.run(
                [sys.executable, "-I", "-B", "-c",
                 "import sys, unittest; sys.path.insert(0, sys.argv.pop(1)); unittest.main(module=None)",
                 pruebas,
                 "test_aterrizaje.AterrizajeTests.test_head_distinto_no_se_arma_ni_produce_observacion_positiva",
                 "test_tren.LedgerTests.test_entrega_duplicada_y_reapertura"],
                env={**os.environ, "TMPDIR": tmp}, capture_output=True, text=True, timeout=120,
            )
            restos = sorted(ruta.name for ruta in Path(tmp).iterdir())
            self.assertEqual(restos, [], "Entradas restantes en TMPDIR")
            self.assertEqual(resultado.returncode, 0, resultado.stdout + resultado.stderr)
