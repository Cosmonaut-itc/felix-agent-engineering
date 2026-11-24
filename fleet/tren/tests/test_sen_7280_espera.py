"""SEN-7280: espera del CI del candidato dentro del tick activo."""
from contextlib import closing, contextmanager, redirect_stdout
from dataclasses import replace
import io
import itertools
import json
import os
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_sen_7280_cadena import CadenaActivaTests as ContextoCadena, REPO, cli
from dominio import Candidato, Congelado, Decision, EsperandoCI, Estado, Pausa, ResultadoCI, Tren
from observador import GitHubGit


class EncadenarEsperaTests(unittest.TestCase):
    def ejecutar(self, huellas, esperar, seguir=lambda: True, tope=cli.ENCADENADOS):
        huellas = iter(huellas)
        ticks = itertools.count(1)
        return list(cli.encadenar(lambda: (0, next(ticks)), lambda: next(huellas),
                                  seguir, tope, esperar=esperar))

    def test_huella_igual_consulta_la_espera(self):
        for listo in (False, True):
            with self.subTest(listo=listo):
                esperar = Mock(return_value=listo)
                self.assertEqual(len(self.ejecutar("aaa", esperar)), 1 + listo)
                esperar.assert_called_once_with()

    def test_tras_esperar_si_no_puede_seguir_corta(self):
        esperar, seguir = Mock(return_value=True), Mock(return_value=False)
        self.assertEqual(self.ejecutar("aa", esperar, seguir), [(0, 1)])
        esperar.assert_called_once_with()
        seguir.assert_called_once_with()

    def test_tras_avanzar_puede_esperar_de_nuevo_hasta_el_tope(self):
        esperar = Mock(return_value=True)
        self.assertEqual(len(self.ejecutar("aabb", esperar, tope=4)), 4)
        self.assertEqual(esperar.call_count, 2)


class Reloj:
    def __init__(self, ahora=0):
        self.ahora = ahora
        self.suenos = []

    def __call__(self):
        return self.ahora

    def dormir(self, segundos):
        self.suenos.append(segundos)
        self.ahora += segundos


class EsperarCITests(unittest.TestCase):
    def setUp(self):
        raiz = tempfile.mkdtemp(prefix="pao-tren-espera-")
        entorno = patch.dict(os.environ, {"XDG_STATE_HOME": raiz})
        entorno.start()
        self.addCleanup(entorno.stop)
        self.tren = Tren("lote", (), EsperandoCI(Candidato("a" * 40, (), "b" * 40, "c" * 40)))
        self.estado = Estado(REPO, trenes=(self.tren,))
        self.reloj = Reloj(55)
        self.vigente = True

    def guardar(self, estado):
        with closing(cli.Ledger()) as ledger, ledger.candado(REPO):
            ledger.guardar_paso(estado, (), self.reloj())

    def esperar(self, sondear, dormir=None):
        return cli.esperar_ci(REPO, 100, cli.estado_para_espera, sondear,
                              lambda: self.vigente, dormir or self.reloj.dormir, self.reloj)

    def test_ci_pendiente_pasa_a_verde_o_rojo(self):
        self.guardar(self.estado)
        for color in ("verde", "rojo"):
            with self.subTest(color=color):
                self.reloj = Reloj(55)
                sondear = Mock(side_effect=[(self.tren, ResultadoCI("pendiente")),
                                            (self.tren, ResultadoCI(color))])
                self.assertTrue(self.esperar(sondear))
                self.assertEqual(self.reloj.suenos, [20, 20])
                self.assertEqual(sondear.call_count, 2)

    def test_corta_si_el_carril_cambia_durante_el_sueno(self):
        fase = self.tren.fase
        cambios = (replace(self.estado, pausa=Pausa("operador")),
                   replace(self.estado, trenes=()),
                   replace(self.estado, trenes=(replace(self.tren, fase=Congelado("a" * 40)),)),
                   replace(self.estado, trenes=(replace(self.tren, id="otro"),)),
                   replace(self.estado, trenes=(replace(self.tren, fase=replace(
                       fase, candidato=replace(fase.candidato, C="d" * 40))),)),
                   replace(self.estado, trenes=(replace(self.tren, fase=replace(fase, solicitud=1)),)))
        for estado in cambios:
            with self.subTest(estado=estado):
                self.guardar(self.estado)
                self.reloj = Reloj(55)
                sondear = Mock()

                def dormir(segundos):
                    self.guardar(estado)
                    self.reloj.dormir(segundos)

                self.assertFalse(self.esperar(sondear, dormir))
                sondear.assert_not_called()

    def test_corta_si_aparece_una_decision(self):
        self.guardar(self.estado)
        sondear = Mock()

        def dormir(segundos):
            with closing(cli.Ledger()) as ledger, ledger.candado(REPO):
                ledger.registrar_decision(Decision("juicio", REPO, None, "pausar", None,
                                                   "prueba", self.reloj(), "", None))
            self.reloj.dormir(segundos)

        self.assertFalse(self.esperar(sondear, dormir))
        sondear.assert_not_called()

    def test_corta_si_cambia_el_programa(self):
        self.guardar(self.estado)
        sondear = Mock()

        def dormir(segundos):
            self.vigente = False
            self.reloj.dormir(segundos)

        self.assertFalse(self.esperar(sondear, dormir))
        sondear.assert_not_called()

    def test_plazo_absoluto_y_sondeo_fallido(self):
        self.guardar(self.estado)
        sondear = Mock(side_effect=[OSError("sin GitHub"), (self.tren, ResultadoCI("pendiente"))])
        self.reloj.ahora = 101
        self.assertFalse(self.esperar(sondear))
        self.assertEqual(self.reloj.suenos, [])
        sondear.assert_not_called()
        self.reloj.ahora = 55
        self.assertFalse(self.esperar(sondear))
        self.assertEqual(self.reloj.suenos, [20, 20])
        self.assertEqual(sondear.call_count, 2)

    def test_ledger_en_solo_lectura_y_cerrado_al_dormir(self):
        sondear = Mock(return_value=(self.tren, ResultadoCI("verde")))
        self.assertFalse(self.esperar(sondear))
        self.assertFalse(cli.directorio_estado().exists())
        self.assertEqual(self.reloj.suenos, [])
        sondear.assert_not_called()
        self.guardar(self.estado)
        lectura_original = cli.lectura_medicion
        conexiones = []

        @contextmanager
        def lectura():
            with lectura_original() as db:
                conexiones.append(db)
                with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                    db.execute("DELETE FROM trains")
                yield db

        def dormir(segundos):
            self.assertEqual(len(conexiones), 1)
            with self.assertRaises(sqlite3.ProgrammingError):
                conexiones[0].execute("SELECT 1")
            self.reloj.dormir(segundos)

        with patch.object(cli, "lectura_medicion", lectura):
            self.assertTrue(self.esperar(sondear, dormir))
        sondear.assert_called_once()

    def test_observar_conserva_el_orden_y_la_ref_de_github(self):
        for solicitud in (0, 2):
            estado = replace(self.estado, trenes=(replace(
                self.tren, fase=replace(self.tren.fase, solicitud=solicitud)),))
            urls = []
            base = "repos/" + REPO
            prefijo = base + "/commits/" + self.tren.fase.candidato.C
            endpoints = [base + "/git/ref/heads/main",
                         prefijo + "/check-runs?per_page=100" + ("&filter=all" if solicitud else "")]
            respuestas = [{"object": {"sha": "a" * 40}}, [{"check_runs": []}]]
            if solicitud:
                endpoints.append(prefijo + "/check-suites?per_page=100")
                respuestas.append([{"check_suites": []}])
            endpoints.append(prefijo + "/statuses?per_page=100")
            respuestas.append([[]])

            def gh(argv, **opciones):
                urls.append(argv[4])
                return subprocess.CompletedProcess(argv, 0, json.dumps(respuestas.pop(0)), "")

            observacion = GitHubGit({}, ejecutar=gh, activo=True).observar(estado, 1)
            self.assertEqual(urls, endpoints)
            self.assertEqual(observacion.ci_candidato, "pendiente")


class EsperaActivaTests(unittest.TestCase):
    comando = ContextoCadena.comando
    commit = ContextoCadena.commit
    push = ContextoCadena.push
    remoto = ContextoCadena.remoto
    setUp = ContextoCadena.setUp
    ledger = ContextoCadena.ledger
    estado = ContextoCadena.estado
    lineas = ContextoCadena.lineas
    retener_ci_del_candidato = ContextoCadena.retener_ci_del_candidato

    def test_main_espera_el_verde_y_lanza_la_suite_en_el_mismo_proceso(self):
        soltar_ci = self.retener_ci_del_candidato()
        config = self.raiz / "repos.json"
        config.write_text(json.dumps({"repos": {REPO: self.contexto.config}}))
        reloj, salida, stdout_por_tick = Reloj(), io.StringIO(), []
        run = subprocess.run

        def ejecutar(argv, **opciones):
            return self.contexto.gh(argv, **opciones) if argv[0] == "gh" else run(argv, **opciones)

        def dormir(segundos):
            self.assertIsInstance(self.estado().trenes[0].fase, EsperandoCI)
            self.assertEqual(self.lineas("trabajos.log"), [])
            soltar_ci()
            reloj.dormir(segundos)

        def intento(args):
            codigo, resultado = self.contexto.tick(100 + len(stdout_por_tick))
            stdout_por_tick.append(resultado)
            return codigo, resultado

        with patch("sys.argv", ["pao-tren", "tick", "--repo", REPO, "--config", str(config)]), \
                patch.object(cli, "ARRANQUE", reloj()), patch.object(cli, "cuota", return_value=True), \
                patch.object(cli, "intento", intento), patch.object(subprocess, "run", ejecutar), \
                patch.object(cli, "time", SimpleNamespace(monotonic=reloj, sleep=dormir)), \
                redirect_stdout(salida):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(reloj.suenos, [20])
        self.assertEqual(len(self.lineas("trabajos.log")), 1)
        self.assertEqual([json.loads(linea) for linea in salida.getvalue().splitlines()],
                         stdout_por_tick)


class EsperaSecaTests(unittest.TestCase):
    def test_seco_no_espera(self):
        salida = io.StringIO()
        with patch("sys.argv", ["pao-tren", "tick", "--repo", REPO, "--seco"]), \
                patch.object(cli, "intento", return_value=(0, {})), \
                patch.object(cli, "esperar_ci", side_effect=AssertionError("espera en seco")), \
                redirect_stdout(salida):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(salida.getvalue(), "{}\n")
