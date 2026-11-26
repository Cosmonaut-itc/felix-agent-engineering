"""Volver a esta versión con un ledger escrito por SEN-7614 no rompe la lectura."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dominio import Entrada, Entrega, Estado, datos, desde_datos


class VueltaRearmadaTests(unittest.TestCase):
    def test_lee_entradas_con_la_marca_rearmada(self):
        entrada = Entrada(Entrega("example/project", 1, "1" * 40, "apto", "dirigida"), 100,
                          admitida=340)
        guardado = datos(Estado("example/project", (entrada,)))
        guardado["entradas"][0]["rearmada"] = True
        self.assertEqual(desde_datos(guardado).entradas, (entrada,))


if __name__ == "__main__":
    unittest.main()
