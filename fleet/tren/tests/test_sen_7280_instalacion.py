"""SEN-7280: instalar con el candado del programa sin que un tick mezcle versiones."""
from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_sen_7280_cadena import REPO, cli

PROBAR_CANDADO = ("import fcntl, sys\n"
                  "with open(sys.argv[1], 'a') as candado:\n"
                  "    try:\n"
                  "        fcntl.flock(candado, fcntl.LOCK_SH | fcntl.LOCK_NB)\n"
                  "    except BlockingIOError:\n"
                  "        sys.exit(7)\n")


def instalando():
    with cli.candado_programa(exclusivo=False) as libre:
        return not libre


class CandadoTests(unittest.TestCase):
    def setUp(self):
        self.raiz = Path(tempfile.mkdtemp(prefix="pao-tren-candado-"))
        self.addCleanup(shutil.rmtree, self.raiz)
        for parche in (patch.dict(os.environ, {"XDG_STATE_HOME": str(self.raiz)}),
                       patch.object(cli, "GRACIA_INSTALACION_S", 0)):
            parche.start()
            self.addCleanup(parche.stop)

    def crear_ledger(self):
        cli.Ledger().close()
        return cli.directorio_estado() / "programa.lock"

    def candado(self, *orden):
        return cli.ejecutar(cli.parser().parse_args(["candado", "--", *orden]))

    def test_sin_ledger_sale_con_2_y_no_lo_crea(self):
        marca = self.raiz / "corrio"
        self.assertEqual(self.candado("touch", str(marca)), (2, {"error": "ledger no disponible"}))
        self.assertFalse(marca.exists())
        self.assertFalse(cli.directorio_estado().exists())

    def test_el_comando_corre_con_el_candado_tomado_y_da_su_codigo(self):
        ruta = self.crear_ledger()
        self.assertEqual(self.candado(sys.executable, "-I", "-c", PROBAR_CANDADO, str(ruta)),
                         (7, {"candado": "tomado", "codigo": 7}))
        self.assertFalse(instalando())
        self.assertEqual(self.candado("true"), (0, {"candado": "tomado", "codigo": 0}))

    def test_conserva_el_candado_durante_la_gracia(self):
        self.crear_ledger()
        gracias = []

        def dormir(segundos):
            gracias.append((segundos, instalando()))

        with patch.object(cli, "GRACIA_INSTALACION_S", 5), patch.object(cli.time, "sleep", dormir):
            self.assertEqual(self.candado("true")[0], 0)
        self.assertEqual(gracias, [(5, True)])
        self.assertFalse(instalando())

    def test_con_un_tick_activo_sale_con_75_sin_correr_el_comando(self):
        self.crear_ledger()
        marca = self.raiz / "corrio"
        with cli.candado_programa(exclusivo=False) as tick, \
                cli.candado_programa(exclusivo=False) as otro_carril:
            self.assertTrue(tick and otro_carril)
            self.assertEqual(self.candado("touch", str(marca)), (75, {"candado": "ocupado"}))
        self.assertFalse(marca.exists())


def instalable():
    with cli.candado_programa(exclusivo=True) as libre:
        return libre


class ProcesoTests(unittest.TestCase):
    """`main` con el candado real: lo que hace el proceso de un tick."""
    def setUp(self):
        self.raiz = Path(tempfile.mkdtemp(prefix="pao-tren-proceso-"))
        self.addCleanup(shutil.rmtree, self.raiz)
        (self.raiz / "pao-tren").mkdir()
        entorno = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.raiz)})
        entorno.start()
        self.addCleanup(entorno.stop)

    def correr(self, *opciones, **parches):
        salida = io.StringIO()
        parches = {"activacion": lambda repo: "activo", "huella": lambda repo: None,
                   "cuota": lambda: False, **parches}
        with ExitStack() as pila:
            pila.enter_context(patch("sys.argv", ["pao-tren", "tick", "--repo", REPO, *opciones]))
            for nombre, valor in parches.items():
                pila.enter_context(patch.object(cli, nombre, valor))
            pila.enter_context(redirect_stdout(salida))
            codigo = cli.main()
        return codigo, [json.loads(linea) for linea in salida.getvalue().splitlines()]

    def test_con_una_instalacion_en_curso_responde_una_linea_y_no_toca_nada(self):
        prohibido = Mock(side_effect=AssertionError("el proceso omitido siguió"))
        for opciones in ((), ("--seco",)):
            with self.subTest(opciones=opciones), \
                    cli.candado_programa(exclusivo=True) as instalador:
                self.assertTrue(instalador)
                self.assertEqual(
                    self.correr(*opciones, Ledger=prohibido, huella=prohibido, cuota=prohibido,
                                programa=prohibido, esperar_ci=prohibido),
                    (0, [{"omitido": "instalacion-en-curso"}]))
        self.assertFalse((self.raiz / "pao-tren/ledger.sqlite3").exists())

    def test_sin_inscripcion_activa_el_tick_activo_responde_su_error(self):
        error = {"error": "tick activo requiere inscripción activa", "inscripcion": "sombra"}
        for instalar in (False, True):
            with self.subTest(instalar=instalar), \
                    cli.candado_programa(exclusivo=instalar) as tomado:
                self.assertTrue(tomado)
                self.assertEqual(self.correr(activacion=lambda repo: "sombra"), (2, [error]))

    def test_un_registro_de_inscritos_ilegible_responde_el_error_de_siempre(self):
        error = {"clase": "OSError", "error": "entrada u observación inválida"}
        for instalar in (False, True):
            with self.subTest(instalar=instalar), \
                    cli.candado_programa(exclusivo=instalar) as tomado:
                self.assertTrue(tomado)
                self.assertEqual(
                    self.correr(activacion=Mock(side_effect=OSError("registro ilegible"))),
                    (2, [error]))

    def test_sin_directorio_de_estado_el_tick_corre_y_no_lo_crea(self):
        (self.raiz / "pao-tren").rmdir()
        for opciones in ((), ("--seco",)):
            with self.subTest(opciones=opciones):
                self.assertEqual(self.correr(*opciones, intento=Mock(return_value=(0, {}))),
                                 (0, [{}]))
                self.assertFalse((self.raiz / "pao-tren").exists())

    def test_sin_poder_tomar_el_candado_no_corre_ningun_tick(self):
        (self.raiz / "pao-tren/programa.lock").mkdir()
        intento = Mock(side_effect=AssertionError("corrió un tick"))
        for opciones in ((), ("--seco",)):
            with self.subTest(opciones=opciones):
                codigo, lineas = self.correr(*opciones, intento=intento)
                self.assertEqual((codigo, [linea["error"] for linea in lineas]),
                                 (2, ["entrada u observación inválida"]))

    def test_el_proceso_conserva_el_candado_entre_ticks_y_lo_suelta_al_salir(self):
        for opciones in ((), ("--seco",)):
            with self.subTest(opciones=opciones):
                vistos, huellas = [], iter("abb")

                def espiar(valor):
                    def llamada(*args, **kwargs):
                        vistos.append(instalable())
                        return valor() if callable(valor) else valor
                    return llamada

                self.assertEqual(
                    self.correr(*opciones, intento=espiar((0, {})), cuota=espiar(True),
                                huella=espiar(lambda: next(huellas)), programa=lambda: "igual",
                                ARRANQUE=float("-inf")),
                    (0, [{}] * (1 if opciones else 2)))
                # Activo: huella, tick, huella, cuota, tick, huella. Seco: un tick.
                self.assertEqual(vistos, [False] * (1 if opciones else 6))
                with cli.candado_programa(exclusivo=True) as instalador:
                    self.assertTrue(instalador)


if __name__ == "__main__":
    unittest.main()
