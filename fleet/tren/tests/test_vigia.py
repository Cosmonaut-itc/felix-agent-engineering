"""SEN-8032: vigía con ledger y registros de Jale locales, sin red ni launchd."""
from contextlib import closing, redirect_stdout
from dataclasses import replace
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

from test_activo import REPO, cli
from adaptadores.ejecucion_jale import CANCELAR
from adaptadores.jale import EN_CURSO, Jale
from dominio import (Candidato, Certificado, Congelado, Corriendo, Decision, Diagnosticando,
                     Efecto, EsperandoCI, Estado, Fallo, Pausa, Reproduccion, ResultadoCI, Tren, clave,
                     efecto_diagnostico, efecto_reanudar_suite)
from instalar import etiqueta
from observador import GitHubGit


class VigiaTests(unittest.TestCase):
    def setUp(self):
        temporal = tempfile.TemporaryDirectory()
        self.addCleanup(temporal.cleanup)
        raiz = Path(temporal.name)
        entorno = patch.dict(os.environ, HOME=str(raiz), XDG_STATE_HOME=str(raiz / "state"),
                             XDG_CONFIG_HOME=str(raiz / "config"))
        entorno.start()
        self.addCleanup(entorno.stop)
        self.directorio = cli.directorio_estado()
        self.directorio.mkdir(parents=True)
        self.inscritos = raiz / "config/pao-tren/inscritos"
        self.inscritos.parent.mkdir(parents=True)
        self.inscritos.write_text(REPO + " activo\n")
        self.memoria = self.directorio / "vigia" / (hashlib.sha256(REPO.encode()).hexdigest() + ".json")
        self.candidato = Candidato("a" * 40, (), "b" * 40, "c" * 40)
        self.tren = Tren("lote", (), EsperandoCI(self.candidato))
        self.config = {"git": str(raiz), "sensibles": {
            "gobierno": ["docs/**"], "ci": [".github/**"], "lockfiles": ["*.lock"],
        }}
        self.configurar()
        self.servicio = "gui/" + str(os.getuid()) + "/" + etiqueta(REPO)
        self.ahora = 100
        self.monotono = 0
        self.color = "pendiente"
        self.estado_tick = "not running"
        self.error_gh = None
        self.error_launchctl = {}
        self.llamadas_gh = []
        self.llamadas_launchctl = []

    def configurar(self, **cambios):
        (self.directorio / "config.json").write_text(
            json.dumps({"repos": {REPO: dict(self.config, **cambios)}}))

    def guardar(self, *trenes, pausa=None, efectos=(), ejecutando=True, decision=False):
        with closing(cli.Ledger()) as ledger:
            ledger.guardar_paso(Estado(REPO, trenes=trenes, pausa=pausa), efectos, self.ahora)
            if ejecutando:
                for efecto in efectos:
                    ledger.iniciar_efecto(REPO, efecto, self.ahora)
            if decision:
                ledger.registrar_decision(Decision("juicio", REPO, None, "pausar", None,
                                                   "prueba", self.ahora, "", None))

    def transporte(self, argv, *, timeout, **_):
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 15 - self.monotono)
        if argv[0] == "gh":
            self.llamadas_gh.append(argv)
            if self.error_gh is not None:
                raise self.error_gh
            endpoint = argv[4]
            if "check-runs?" in endpoint:
                check = {"id": 1, "name": "test", "status": (
                    "in_progress" if self.color == "pendiente" else "completed"),
                    "conclusion": "failure" if self.color == "rojo" else "success",
                    "head_sha": self.tren.fase.candidato.C, "check_suite": {"id": 2}}
                cuerpo = {"check_runs": [check]}
            elif "check-suites?" in endpoint:
                cuerpo = {"check_suites": [{"id": 2, "head_sha": self.tren.fase.candidato.C,
                    "head_branch": "tren/" + self.tren.id + "-ci-" + str(self.tren.fase.solicitud)}]}
            else:
                self.assertIn("/statuses?", endpoint)
                cuerpo = []
            return subprocess.CompletedProcess(argv, 0, "HTTP/2 200\n\n" + json.dumps(cuerpo), "")
        self.assertEqual(argv[0], "launchctl")
        self.llamadas_launchctl.append(argv[1:])
        error = self.error_launchctl.get(argv[1])
        if isinstance(error, Exception):
            raise error
        if error is not None:
            return subprocess.CompletedProcess(argv, *error)
        salida = self.servicio + " = {\n\tstate = " + self.estado_tick + "\n}\n"
        return subprocess.CompletedProcess(argv, 0, salida if argv[1] == "print" else "", "")

    def ejecutar(self, motivo=None, senal=None):
        salida = io.StringIO()
        with redirect_stdout(salida), patch("sys.argv", ["pao-tren", "vigia", "--repo", REPO]), \
                patch("subprocess.run", side_effect=self.transporte), \
                patch("time.time", side_effect=lambda: self.ahora), \
                patch("time.monotonic", side_effect=lambda: self.monotono), \
                patch.object(cli, "ARRANQUE", 0), \
                patch.object(cli, "candado_programa", side_effect=AssertionError("candado")), \
                patch.object(cli.Ledger, "__init__", side_effect=AssertionError("ledger de escritura")), \
                patch.object(cli.Ledger, "candado", side_effect=AssertionError("candado")):
            self.assertEqual(cli.main(), 0)
        if motivo is None:
            self.assertEqual(salida.getvalue(), "")
        else:
            esperado = {"vigia": motivo, "tren": self.tren.id, "fase": type(self.tren.fase).__name__}
            if senal is not None:
                esperado["señal"] = senal
            self.assertEqual(salida.getvalue(), json.dumps(
                esperado, sort_keys=True, ensure_ascii=False) + "\n")

    def pateos(self):
        return [orden for orden in self.llamadas_launchctl if orden[0] == "kickstart"]

    def cuenta(self):
        return json.loads(self.memoria.read_text())["pateos"]

    def preparar_suite_jale(self, host="auto"):
        self.tren = replace(self.tren, fase=Corriendo(self.candidato, "", reanudaciones=1))
        self.guardar(self.tren, efectos=(efecto_reanudar_suite(self.tren, self.tren.fase),))
        lanzador = str(self.directorio / "lanzador")
        self.configurar(adaptador="jale", lanzador=lanzador, host=host)
        jale = Jale(lanzador, host=host)
        return jale, jale.estado / clave("suite", self.tren.id)

    def registrar_jale(self, registro, jale, modo, codigo=None, filtros=()):
        registro.mkdir(parents=True)
        (registro / "solicitud.json").write_text(json.dumps(
            ["bash", jale.lanzador, jale.host_de(modo), str(registro / "corrida.log"),
             modo, self.candidato.C, *filtros]))
        if codigo is not None:
            (registro / "codigo.json").write_text(json.dumps(codigo))

    def preparar_diagnostico_jale(self):
        fallo = Fallo("prueba", "firma", "solo-candidato", "contract", "contract", "jale")
        solicitud = Reproduccion("reproduccion", fallo, "C")
        origen = self.directorio / "suite.json"
        origen.write_text(json.dumps([{"sha": self.candidato.C, "modo": fallo.check,
                                       "unidades": [{"unidad": fallo.unidad}]}]))
        fase = Diagnosticando(self.candidato, (fallo,), suite=str(origen), pendiente=solicitud)
        self.tren = replace(self.tren, fase=fase)
        lanzador = str(self.directorio / "lanzador")
        self.configurar(adaptador="jale", lanzador=lanzador)
        jale = Jale(lanzador)
        raiz = jale.estado / clave("repetir", self.tren.id, solicitud.id)
        for n in ("1", "2"):
            self.registrar_jale(raiz / n / "0", jale, fallo.check, 75, filtros=(fallo.unidad,))
        return jale

    def contrastar_suite(self, jale):
        self.assertIsNot(jale.suite(self.candidato, self.tren.id, intento=1,
                                    permitir_lanzar=False), EN_CURSO)

    def contrastar_diagnostico(self, jale):
        fase = self.tren.fase
        self.assertIsNot(jale.repetir(self.candidato, fase.suite, fase.pendiente, self.candidato.C,
                                      self.tren.id, permitir_lanzar=False), EN_CURSO)

    def test_sin_lote_pausa_decision_o_inscripcion_activa(self):
        self.ejecutar()
        self.assertFalse((self.directorio / "ledger.sqlite3").exists())
        self.memoria.parent.mkdir()
        self.memoria.write_bytes(b"memoria previa")
        mtime = self.memoria.stat().st_mtime_ns
        self.guardar()
        self.ejecutar()
        self.guardar(replace(self.tren, fase=Congelado(self.candidato.B)))
        self.ejecutar()
        self.guardar(self.tren, pausa=Pausa("manual"))
        self.ejecutar()
        self.guardar(self.tren)
        for inscripcion in ("otro/repo activo\n", REPO + " sombra\n"):
            self.inscritos.write_text(inscripcion)
            self.ejecutar()
        self.inscritos.write_text(REPO + " activo\n")
        self.guardar(self.tren, decision=True)
        self.ejecutar()
        self.assertEqual((self.llamadas_gh, self.llamadas_launchctl), ([], []))
        self.assertEqual(self.memoria.read_bytes(), b"memoria previa")
        self.assertEqual(self.memoria.stat().st_mtime_ns, mtime)
        self.assertFalse((self.directorio / "github-cache.sqlite3").exists())

    def test_ci_pendiente_incluso_con_suite_terminada(self):
        efecto = Efecto(clave("suite", self.tren.id), "iniciar-suite", (self.tren.id,))
        self.configurar(adaptador="jale", lanzador=str(self.directorio / "lanzador"))
        self.guardar(self.tren, efectos=(efecto,))
        registro = self.directorio / "jale" / efecto.clave / "contract/0"
        registro.mkdir(parents=True)
        (registro / "codigo.json").write_text("75")
        self.ejecutar("sin-señal")
        self.assertTrue(self.llamadas_gh)
        self.assertEqual(self.llamadas_launchctl, [])
        self.assertFalse(self.memoria.exists())

    def test_ci_verde_y_rojo_patean_sin_matar(self):
        self.guardar(self.tren)
        for color in ("verde", "rojo"):
            self.color = color
            self.llamadas_launchctl.clear()
            self.ejecutar("pateo", "ci")
            self.assertEqual(self.llamadas_launchctl,
                             [["print", self.servicio], ["kickstart", self.servicio]])
            self.assertEqual(self.cuenta(), 1)

    def test_ci_de_otro_tren_no_da_senal(self):
        otro = Tren("otro", (), Certificado(self.candidato.C, self.candidato.arbol, "", "verde"))
        self.guardar(self.tren, otro)
        with patch.object(GitHubGit, "ci_candidato", return_value=(otro, ResultadoCI("verde"))) as ci:
            self.ejecutar("sin-señal")
        ci.assert_called_once()
        self.assertEqual(self.llamadas_launchctl, [])
        self.assertFalse(self.memoria.exists())

    def test_misma_senal_espera_y_para_despues_de_tres_pateos(self):
        self.guardar(self.tren)
        self.color = "verde"
        for hora, motivo, cuenta in ((100, "pateo", 1), (119, "espera", 1), (120, "pateo", 2),
                                     (140, "pateo", 3), (160, "tope", 3)):
            self.ahora = hora
            self.ejecutar(motivo, "ci")
            self.assertEqual(self.cuenta(), cuenta)
        self.assertEqual(self.pateos(), [["kickstart", self.servicio]] * 3)

    def test_cambiar_tren_c_o_solicitud_reinicia_la_cuenta(self):
        self.color = "verde"
        tren = self.tren
        for cambio in (tren, replace(tren, id="otro"), replace(tren, id="otro",
                fase=replace(tren.fase, candidato=replace(self.candidato, C="d" * 40))),
                replace(tren, id="otro", fase=EsperandoCI(
                    replace(self.candidato, C="d" * 40), solicitud=1))):
            self.tren = cambio
            self.guardar(cambio)
            self.ejecutar("pateo", "ci")
            self.assertEqual(self.cuenta(), 1)
        self.assertEqual(len(self.pateos()), 4)

    def test_tick_vivo_y_ausente_no_consumen_pateos(self):
        self.guardar(self.tren)
        self.color = "verde"
        self.ejecutar("pateo", "ci")
        anterior = self.memoria.read_bytes()
        self.ahora += 20
        self.estado_tick = "running"
        self.ejecutar("tick-vivo", "ci")
        self.error_launchctl["print"] = (113, "", "Could not find service")
        self.ejecutar("servicio-ausente", "ci")
        self.assertEqual(len(self.pateos()), 1)
        self.assertEqual(self.memoria.read_bytes(), anterior)

    def test_jale_espera_la_ultima_salida_y_despierta_al_tener_todas(self):
        jale, raiz = self.preparar_suite_jale()
        self.registrar_jale(raiz / "contract/1", jale, "contract", 75)
        ultimo = raiz / "e2e/1"
        self.ejecutar("sin-señal")
        self.registrar_jale(ultimo, jale, "e2e")
        with (ultimo / "candado").open("a") as candado:
            fcntl.flock(candado, fcntl.LOCK_EX | fcntl.LOCK_NB)
            antes = {p: p.read_bytes() for p in raiz.rglob("*") if p.is_file()}
            self.ejecutar("sin-señal")
            self.assertEqual({p: p.read_bytes() for p in raiz.rglob("*") if p.is_file()}, antes)
            self.assertIs(jale.suite(self.candidato, self.tren.id, intento=1,
                                     permitir_lanzar=False), EN_CURSO)
        self.ejecutar("sin-señal")
        self.assertEqual(self.llamadas_launchctl, [])
        (ultimo / "codigo.json").write_text("75")
        self.ejecutar("pateo", "suite")
        self.contrastar_suite(jale)
        self.assertEqual(self.pateos(), [["kickstart", self.servicio]])
        self.assertEqual(self.cuenta(), 1)
        self.assertEqual(self.llamadas_gh, [])

    def test_jale_ignora_75_con_pendiente_de_boat(self):
        jale, raiz = self.preparar_suite_jale(host="boat")
        self.registrar_jale(raiz / "contract/1", jale, "contract", 75)
        registro = raiz / "e2e/1"
        self.registrar_jale(registro, jale, "e2e", 75)
        (registro / "corrida.log.boat-pendiente").write_text(
            json.dumps({"status": "pending", "cause": "fixture"}))
        self.ejecutar("sin-señal")
        self.assertEqual(self.llamadas_launchctl, [])

    def test_jale_ignora_cancelacion_y_errores_de_lectura(self):
        jale, raiz = self.preparar_suite_jale()
        self.registrar_jale(raiz / "contract/1", jale, "contract", 75)
        registro = raiz / "e2e/1"
        self.registrar_jale(registro, jale, "e2e")
        (registro / "codigo.json").write_text("{")
        self.ejecutar("sin-señal")
        (registro / "codigo.json").write_text("75")
        abrir = Path.open

        def abrir_vigilado(ruta, *args, **opciones):
            if ruta == registro / "codigo.json":
                raise PermissionError("prueba")
            return abrir(ruta, *args, **opciones)

        with patch.object(Path, "open", abrir_vigilado):
            self.ejecutar("sin-señal")
        (registro / CANCELAR).write_text("{}")
        self.ejecutar("sin-señal")
        self.assertEqual(self.llamadas_launchctl, [])

    def test_jale_lee_el_intento_fijado_por_completo(self):
        jale, raiz = self.preparar_suite_jale()
        self.registrar_jale(raiz / "contract/1", jale, "contract", 75)
        anterior = raiz / "contract/0"
        self.registrar_jale(anterior, jale, "contract")
        self.registrar_jale(raiz / "e2e/1", jale, "e2e", 75)
        (anterior.parent / "completo.json").write_text("0")
        self.ejecutar("sin-señal")
        self.assertEqual(self.llamadas_launchctl, [])
        (anterior / "codigo.json").write_text("64")
        self.ejecutar("pateo", "suite")
        self.contrastar_suite(jale)
        self.assertEqual(self.pateos(), [["kickstart", self.servicio]])

    def test_jale_diagnostico_ignora_otro_lote_y_filas_pendientes(self):
        jale = self.preparar_diagnostico_jale()
        fase = self.tren.fase
        otro = efecto_diagnostico(replace(self.tren, id="otro"), fase)
        pendiente = efecto_diagnostico(self.tren, fase)
        self.guardar(self.tren, efectos=(otro,))
        self.guardar(self.tren, efectos=(pendiente,), ejecutando=False)
        self.ejecutar("sin-señal")
        self.guardar(self.tren, efectos=(pendiente,))
        self.ejecutar("pateo", "suite")
        self.contrastar_diagnostico(jale)
        self.assertEqual(self.pateos(), [["kickstart", self.servicio]])
        self.assertEqual(self.llamadas_gh, [])

    def test_vigia_no_toca_candados_y_abre_el_ledger_en_solo_lectura_en_las_tres_fases(self):
        def vigilar_apertura(abrir):
            def vigilada(ruta, *args, **opciones):
                if not isinstance(ruta, int):
                    self.assertNotEqual(Path(os.fsdecode(ruta)).name, "candado")
                return abrir(ruta, *args, **opciones)

            return vigilada

        conectar = sqlite3.connect
        aperturas_ledger = []

        def conectar_vigilado(database, *args, **opciones):
            ruta = urlsplit(os.fsdecode(database))
            if Path(unquote(ruta.path)).name == "ledger.sqlite3":
                self.assertTrue(opciones.get("uri"))
                self.assertEqual(parse_qs(ruta.query).get("mode"), ["ro"])
                aperturas_ledger.append(database)
            return conectar(database, *args, **opciones)

        def ejecutar(motivo):
            aperturas_ledger.clear()
            with patch("fcntl.flock", side_effect=AssertionError("flock")), \
                    patch("builtins.open", side_effect=vigilar_apertura(open)), \
                    patch("io.open", side_effect=vigilar_apertura(io.open)), \
                    patch("os.open", side_effect=vigilar_apertura(os.open)), \
                    patch("sqlite3.connect", side_effect=conectar_vigilado):
                self.ejecutar("pateo", motivo)
            self.assertTrue(aperturas_ledger)

        self.guardar(self.tren)
        self.color = "verde"
        ejecutar("ci")
        jale, raiz = self.preparar_suite_jale()
        for modo in jale.modos:
            self.registrar_jale(raiz / modo / "1", jale, modo, 75)
        ejecutar("suite")
        self.contrastar_suite(jale)
        jale = self.preparar_diagnostico_jale()
        self.guardar(self.tren, efectos=(efecto_diagnostico(self.tren, self.tren.fase),))
        ejecutar("suite")
        self.contrastar_diagnostico(jale)

    def test_hasito_sin_consulta_de_suite(self):
        self.tren = replace(self.tren, fase=Corriendo(self.candidato, "", reanudaciones=1))
        self.configurar(lanzador_hasito=str(self.directorio / "lanzador"))
        self.guardar(self.tren, efectos=(efecto_reanudar_suite(self.tren, self.tren.fase),))
        self.ejecutar("sin-señal")
        self.assertEqual((self.llamadas_gh, self.llamadas_launchctl), ([], []))

    def test_error_de_gh_y_limite_no_patean(self):
        self.guardar(self.tren)
        for error, motivo in ((OSError("ruta privada"), "lectura-fallida"),
                              (subprocess.CalledProcessError(1, ["gh"], output=(
                                  "HTTP/2 429\nRetry-After: 60\n\n{}")), "limite-github")):
            self.error_gh = error
            self.ejecutar(motivo)
        self.assertEqual(self.llamadas_launchctl, [])
        self.assertFalse(self.memoria.exists())

    def test_launchctl_fallido_o_lento_no_consume_la_cuenta(self):
        self.guardar(self.tren)
        self.color = "verde"
        for error in ((5, "", "error privado"), subprocess.TimeoutExpired(["launchctl"], 15)):
            self.error_launchctl["print"] = error
            self.ejecutar("lectura-fallida", "ci")
        self.assertEqual(self.pateos(), [])
        self.error_launchctl.clear()
        self.error_launchctl["kickstart"] = (5, "", "error privado")
        self.ejecutar("lectura-fallida", "ci")
        self.assertFalse(self.memoria.exists())

    def test_memoria_malformada_se_trata_como_vacia(self):
        self.guardar(self.tren)
        self.color = "verde"
        self.memoria.parent.mkdir()
        for contenido in ("{", "[]"):
            self.memoria.write_text(contenido)
            self.ejecutar("pateo", "ci")
            self.assertEqual(self.cuenta(), 1)
        memoria = json.loads(self.memoria.read_text())
        self.memoria.write_text(json.dumps(dict(memoria, pateos=True)))
        self.ejecutar("pateo", "ci")
        self.assertEqual(self.cuenta(), 1)

    def test_presupuesto_de_io_compartido(self):
        self.guardar(self.tren)
        self.color = "verde"
        transporte = self.transporte

        def lento(argv, **opciones):
            salida = transporte(argv, **opciones)
            self.monotono += 7.5
            return salida

        with patch.object(self, "transporte", side_effect=lento):
            self.ejecutar("lectura-fallida", "ci")
        self.assertEqual(len(self.llamadas_gh), 2)
        self.assertEqual(self.llamadas_launchctl, [])
