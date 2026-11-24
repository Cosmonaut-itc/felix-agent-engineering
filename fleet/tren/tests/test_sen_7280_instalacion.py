"""SEN-7280: instalar con el candado del carril sin que un tick mezcle versiones."""
from contextlib import closing, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_sen_7280_cadena import CadenaActivaTests as ContextoCadena, REPO, cli

PROBAR_CANDADO = ("import fcntl, sys\n"
                  "with open(sys.argv[1], 'a') as candado:\n"
                  "    try:\n"
                  "        fcntl.flock(candado, fcntl.LOCK_SH | fcntl.LOCK_NB)\n"
                  "    except BlockingIOError:\n"
                  "        sys.exit(7)\n")


class CandadoTests(unittest.TestCase):
    def setUp(self):
        self.raiz = Path(tempfile.mkdtemp(prefix="pao-tren-candado-"))
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
        self.assertFalse(cli.instalando())
        self.assertFalse(cli.directorio_estado().exists())

    def test_el_comando_corre_con_el_candado_tomado_y_da_su_codigo(self):
        ruta = self.crear_ledger()
        self.assertEqual(self.candado(sys.executable, "-I", "-c", PROBAR_CANDADO, str(ruta)),
                         (7, {"candado": "tomado", "codigo": 7}))
        self.assertFalse(cli.instalando())
        self.assertEqual(self.candado("true"), (0, {"candado": "tomado", "codigo": 0}))

    def test_conserva_el_candado_durante_la_gracia(self):
        self.crear_ledger()
        gracias = []

        def dormir(segundos):
            gracias.append((segundos, cli.instalando()))

        with patch.object(cli, "GRACIA_INSTALACION_S", 5), patch.object(cli.time, "sleep", dormir):
            self.assertEqual(self.candado("true")[0], 0)
        self.assertEqual(gracias, [(5, True)])
        self.assertFalse(cli.instalando())

    def test_con_un_tick_activo_sale_con_75_sin_correr_el_comando(self):
        self.crear_ledger()
        marca = self.raiz / "corrio"
        with cli.candado_programa(exclusivo=False) as tick, \
                cli.candado_programa(exclusivo=False) as otro_carril:
            self.assertTrue(tick and otro_carril)
            self.assertFalse(cli.instalando())
            self.assertEqual(self.candado("touch", str(marca)), (75, {"candado": "ocupado"}))
        self.assertFalse(marca.exists())


class TickBajoCandadoTests(unittest.TestCase):
    comando = ContextoCadena.comando
    commit = ContextoCadena.commit
    push = ContextoCadena.push
    remoto = ContextoCadena.remoto
    setUp = ContextoCadena.setUp
    ledger = ContextoCadena.ledger
    estado = ContextoCadena.estado
    lineas = ContextoCadena.lineas

    def tick(self, vigente):
        c = self.contexto
        args = cli.parser().parse_args(["tick", "--repo", REPO])
        return cli.ejecutar(args, c.observador, 100, c.git, c.hasito, None, c.escritor,
                            vigente=vigente)

    def eventos(self):
        with self.ledger() as ledger:
            return ledger.ultimos_eventos(REPO)

    def test_programa_cambiado_o_instalacion_en_curso_no_observa_ni_escribe(self):
        antes, eventos = self.estado(), self.eventos()
        observador = self.contexto.observador
        with patch.object(observador, "observar", wraps=observador.observar) as observar:
            self.assertEqual(self.tick(lambda: False), (0, {"omitido": "programa-cambiado"}))
            with cli.candado_programa(exclusivo=True) as instalador:
                self.assertTrue(instalador)
                self.assertEqual(self.tick(lambda: True), (0, {"omitido": "instalacion-en-curso"}))
            observar.assert_not_called()
            self.assertEqual((self.estado(), self.eventos()), (antes, eventos))
            codigo, resultado = self.tick(lambda: True)
            self.assertEqual((codigo, resultado["modo"]), (0, "activo"))
            observar.assert_called_once()

    def test_el_tick_tiene_el_candado_compartido_mientras_corre(self):
        observador, vistos = self.contexto.observador, []
        observar = observador.observar

        def espiar(estado, ahora):
            with cli.candado_programa(exclusivo=True) as instalador:
                vistos.append(instalador)
            return observar(estado, ahora)

        with patch.object(observador, "observar", espiar):
            self.assertEqual(self.tick(lambda: True)[0], 0)
        self.assertEqual(vistos, [False])
        with cli.candado_programa(exclusivo=True) as instalador:
            self.assertTrue(instalador)


class MainTests(unittest.TestCase):
    def correr(self, intento, instalando=False, programa=lambda: "igual"):
        salida = io.StringIO()
        with patch("sys.argv", ["pao-tren", "tick", "--repo", REPO]), \
                patch.object(cli, "intento", intento), \
                patch.object(cli, "instalando", lambda: instalando), \
                patch.object(cli, "huella", lambda repo: None), \
                patch.object(cli, "programa", programa), redirect_stdout(salida):
            codigo = cli.main()
        return codigo, [json.loads(linea) for linea in salida.getvalue().splitlines()]

    def test_una_instalacion_en_curso_al_arrancar_omite_el_tick(self):
        intento = Mock(side_effect=AssertionError("corrió un tick"))
        self.assertEqual(self.correr(intento, instalando=True),
                         (0, [{"omitido": "instalacion-en-curso"}]))

    def test_cada_tick_recibe_la_comparacion_con_el_programa_del_arranque(self):
        instalado, vistos = ["igual"], []

        def intento(args, vigente=None):
            vistos.append(vigente())
            instalado[0] = "nuevo"
            vistos.append(vigente())
            return 0, {}

        self.assertEqual(self.correr(intento, programa=lambda: instalado[0]), (0, [{}]))
        self.assertEqual(vistos, [True, False])

    def test_sin_huella_del_arranque_corre_un_tick_sin_comparar(self):
        intento = Mock(return_value=(0, {}))
        self.assertEqual(self.correr(intento, programa=lambda: None), (0, [{}]))
        self.assertIsNone(intento.call_args.kwargs["vigente"])


if __name__ == "__main__":
    unittest.main()
