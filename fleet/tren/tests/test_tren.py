"""Sin red; sólo ejecutar en el host de pruebas. Fixtures retenidas."""
from dataclasses import replace
import fcntl
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
from dominio import (Admitido, Armado, Candidato, Congelado, Corriendo,
                     Entrada, Entrega, EsperandoCI, Estado, Motivo, Observacion,
                     Pausa, Rechazado, Retenido, Terminal, VistaPR,
                     datos, desde_datos, inicial, paso)
from ledger import Ledger, directorio_estado
from observador import Falso, GitHubGit, configuracion, es_sensible

loader = importlib.machinery.SourceFileLoader("cli_tren", str(RAIZ / "pao-tren"))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)

HEAD = "a" * 40
BASE = "b" * 40
REPO = "example/project"


def entrega(pr=1, head=HEAD):
    return Entrega(REPO, pr, head, "/evidencia/apto.json", "/evidencia/dirigida.json")


def vista(pr=1, **kwargs):
    return replace(VistaPR(pr, HEAD, apto=True, hallazgos=0, dirigida=True,
                           ci="verde", conflicto=False), **kwargs)


def estado(n=1):
    return Estado(REPO, tuple(Entrada(entrega(i), float(i)) for i in range(1, n + 1)))


def observacion(n=1, ahora=100, **kwargs):
    return Observacion(ahora, BASE, tuple(vista(i) for i in range(1, n + 1)), **kwargs)


class Aislado(unittest.TestCase):
    def setUp(self):
        self.raiz = Path(tempfile.mkdtemp(prefix="pao-tren-u1-"))
        self.home = self.raiz / "home"
        self.home.mkdir()
        self.env = patch.dict(os.environ, {"HOME": str(self.home),
                             "XDG_STATE_HOME": str(self.raiz / "state"),
                             "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def ledger(self):
        ledger = Ledger()
        self.addCleanup(ledger.close)
        return ledger

    def cli(self, *args):
        return subprocess.run([sys.executable, "-I", str(RAIZ / "pao-tren"), *args],
                              capture_output=True, text=True, timeout=20)


class DominioTests(Aislado):
    def test_custodia(self):
        self.assertEqual(estado().entradas[0].admision, Retenido(Motivo.CUSTODIA))

    def test_admision_y_pureza(self):
        original = estado()
        nuevo, efectos = paso(original, observacion())
        self.assertIsInstance(nuevo.entradas[0].admision, Admitido)
        self.assertEqual(original, estado())
        self.assertEqual(efectos, ())

    def test_head_distinto_caduca_y_no_revive(self):
        obs = replace(observacion(), prs=(vista(head="c" * 40),))
        nuevo, efectos = paso(estado(), obs)
        self.assertEqual(nuevo.entradas[0].admision, Rechazado(Motivo.HEAD))
        self.assertEqual(efectos[0].tipo, "avisar-caducidad")
        repetido, efectos2 = paso(nuevo, observacion())
        self.assertEqual(repetido, nuevo)
        self.assertEqual(efectos2, ())
        self.assertEqual(paso(estado(), obs)[1], efectos)

    def test_retenciones_y_rechazos(self):
        casos = [({"apto": False}, Retenido(Motivo.APTO)),
                 ({"hallazgos": 1}, Retenido(Motivo.HALLAZGOS)),
                 ({"dirigida": False}, Retenido(Motivo.DIRIGIDA)),
                 ({"ci": "pendiente"}, Retenido(Motivo.CI)),
                 ({"conflicto": True}, Retenido(Motivo.CONFLICTO)),
                 ({"conflicto": None}, Retenido(Motivo.INCIERTO)),
                 ({"completa": False}, Retenido(Motivo.INCIERTO)),
                 ({"base": "develop"}, Rechazado(Motivo.BASE)),
                 ({"abierto": False}, Rechazado(Motivo.CERRADO)),
                 ({"sensible": True}, Retenido(Motivo.MERGE_READY)),
                 ({"alto_riesgo": True}, Retenido(Motivo.VERIFICADO)),
                 ({"alto_riesgo": True, "verificado": True}, Retenido(Motivo.PILOTO)),
                 ({"alto_riesgo": True, "verificado": True, "autorizado": True}, Admitido())]
        for cambios, esperado in casos:
            with self.subTest(cambios=cambios):
                nuevo, _ = paso(estado(), replace(observacion(), prs=(vista(**cambios),)))
                self.assertEqual(nuevo.entradas[0].admision, esperado)

    def test_falta_observacion_retiene(self):
        nuevo, _ = paso(estado(), replace(observacion(), prs=()))
        self.assertEqual(nuevo.entradas[0].admision, Retenido(Motivo.INCIERTO))

    def test_tres_salen_fifo_hasta_tres(self):
        nuevo, efectos = paso(estado(5), observacion(5))
        self.assertEqual(nuevo.trenes[0].recibos, tuple(entrega(i).recibo for i in (1, 2, 3)))
        self.assertIsNone(nuevo.entradas[3].tren)
        self.assertIsInstance(nuevo.trenes[0].fase, Congelado)
        self.assertEqual(efectos[0].tipo, "armar-candidato")
        self.assertEqual(paso(estado(5), observacion(5)), (nuevo, efectos))

    def test_ventana_exacta_de_sesenta_minutos(self):
        nuevo, _ = paso(estado(2), observacion(2, ahora=100))
        antes, _ = paso(nuevo, observacion(2, ahora=3699))
        self.assertFalse(antes.trenes)
        justo, _ = paso(antes, observacion(2, ahora=3700))
        self.assertEqual(len(justo.trenes[0].recibos), 2)

    def test_retencion_reinicia_ventana_al_readmitir(self):
        nuevo, _ = paso(estado(), observacion())
        retenido, _ = paso(nuevo, replace(observacion(ahora=200), prs=(vista(ci="pendiente"),)))
        self.assertIsNone(retenido.entradas[0].admitida)
        nuevo, _ = paso(retenido, observacion(ahora=4000))
        self.assertFalse(nuevo.trenes)
        self.assertEqual(nuevo.entradas[0].admitida, 4000)

    def test_fifo_por_admision(self):
        nuevo, _ = paso(estado(3), replace(observacion(3),
                           prs=(vista(1, ci="pendiente"), vista(2), vista(3))))
        nuevo, _ = paso(nuevo, observacion(3, ahora=101))
        self.assertEqual(nuevo.trenes[0].recibos, tuple(entrega(i).recibo for i in (2, 3, 1)))

    def test_alto_riesgo_solo(self):
        obs = replace(observacion(3, excluir_alto_riesgo=False),
                      prs=(vista(1, alto_riesgo=True, verificado=True, autorizado=True),
                           vista(2), vista(3)))
        nuevo, _ = paso(estado(3), obs)
        self.assertEqual(nuevo.trenes[0].recibos, (entrega().recibo,))

    def test_alto_riesgo_sin_autorizar_queda_en_merge_ready_fuera_del_piloto(self):
        obs = replace(observacion(excluir_alto_riesgo=False),
                      prs=(vista(1, alto_riesgo=True, verificado=True),))
        nuevo, _ = paso(estado(), obs)
        self.assertEqual(nuevo.entradas[0].admision, Retenido(Motivo.MERGE_READY))
        self.assertFalse(nuevo.trenes)

    def test_alto_riesgo_autorizado_viaja_solo_en_el_piloto(self):
        obs = replace(observacion(3),
                      prs=(vista(1, alto_riesgo=True, verificado=True, autorizado=True),
                           vista(2), vista(3)))
        self.assertTrue(obs.excluir_alto_riesgo)
        nuevo, _ = paso(estado(3), obs)
        self.assertEqual(nuevo.trenes[0].recibos, (entrega().recibo,))

    def test_sensible_autorizado_solo(self):
        obs = replace(observacion(3), prs=(vista(1, sensible=True, autorizado=True), vista(2), vista(3)))
        nuevo, _ = paso(estado(3), obs)
        self.assertEqual(nuevo.trenes[0].recibos, (entrega().recibo,))

    def test_solo_no_adelanta_fifo(self):
        obs = replace(observacion(3), prs=(vista(1), vista(2, sensible=True, autorizado=True), vista(3)))
        nuevo, _ = paso(estado(3), obs)
        self.assertFalse(nuevo.trenes)
        nuevo, _ = paso(nuevo, replace(obs, ahora=3700))
        self.assertEqual(nuevo.trenes[0].recibos, (entrega().recibo,))

    def test_ningun_segundo_lote_activo(self):
        nuevo, _ = paso(estado(6), observacion(6))
        nuevo, efectos = paso(nuevo, observacion(6, ahora=9999))
        self.assertEqual(len(nuevo.trenes), 1)
        self.assertEqual(efectos, ())

    def test_terminal_permite_otro_lote(self):
        nuevo, _ = paso(estado(6), observacion(6))
        nuevo = replace(nuevo, trenes=(replace(nuevo.trenes[0], fase=Terminal("ATERRIZADO")),))
        nuevo, _ = paso(nuevo, observacion(6))
        self.assertEqual(len(nuevo.trenes), 2)

    def test_pausa_y_jale(self):
        self.assertEqual(inicial("example/Jale").pausa, Pausa("etapa-1"))
        nuevo, efectos = paso(replace(estado(3), pausa=Pausa("etapa-1")), observacion(3))
        self.assertFalse(nuevo.trenes)
        self.assertFalse(efectos)

    def test_caducidad_en_tren_pausa(self):
        nuevo, _ = paso(estado(3), observacion(3))
        nuevo, _ = paso(nuevo, replace(observacion(3), prs=(vista(1, head="c" * 40), vista(2), vista(3))))
        self.assertEqual(nuevo.pausa, Pausa("head-cambiado-en-tren"))

    def test_fases_hasta_corriendo_y_claves_estables(self):
        nuevo, _ = paso(estado(3), observacion(3))
        candidato = Candidato(BASE, tuple((i, HEAD, str(i) * 40) for i in (1, 2, 3)), "3" * 40, "d" * 40)
        obs = replace(observacion(3), candidato=candidato)
        nuevo, _ = paso(nuevo, obs)
        self.assertIsInstance(nuevo.trenes[0].fase, Armado)
        nuevo, efectos = paso(nuevo, observacion(3))
        self.assertIsInstance(nuevo.trenes[0].fase, EsperandoCI)
        self.assertEqual(efectos[0].tipo, "solicitar-ci")
        esperando, efectos = paso(nuevo, observacion(3))
        self.assertEqual(esperando, nuevo)
        self.assertFalse(efectos)
        obs = replace(observacion(3), ci_candidato="verde")
        _, intento1 = paso(nuevo, obs)
        corriendo, intento2 = paso(nuevo, replace(obs, suite="recibo-suite"))
        self.assertEqual(intento1, intento2)
        self.assertIsInstance(corriendo.trenes[0].fase, Corriendo)
        self.assertEqual(paso(corriendo, obs)[1], ())
        self.assertEqual(desde_datos(datos(corriendo)), corriendo)
        self.assertEqual(desde_datos(datos(intento2)), intento2)

    def test_candidato_ajeno_pausa(self):
        nuevo, _ = paso(estado(3), observacion(3))
        candidato = Candidato("e" * 40, (), HEAD, HEAD)
        nuevo, _ = paso(nuevo, replace(observacion(3), candidato=candidato))
        self.assertEqual(nuevo.pausa, Pausa("candidato-no-corresponde"))


class LedgerTests(Aislado):
    def test_directorio_xdg_y_fallback(self):
        self.assertEqual(directorio_estado(), self.raiz / "state/pao-tren")
        with patch.dict(os.environ, {"XDG_STATE_HOME": ""}):
            # Empty XDG has the same shell-default semantics as an unset value.
            self.assertEqual(directorio_estado(), self.home / ".local/state/pao-tren")

    def test_entrega_duplicada_y_reapertura(self):
        ledger = Ledger()
        self.assertEqual(ledger.entregar(entrega(), 100), ledger.entregar(entrega(), 200))
        esperado = ledger.estado(REPO)
        for tabla in ("requests", "entries", "events"):
            self.assertEqual(ledger.db.execute("SELECT count(*) FROM " + tabla).fetchone()[0], 1)
        ledger.close()
        self.assertEqual(self.ledger().estado(REPO), esperado)

    def test_misma_clave_otras_referencias_conflicto(self):
        ledger = self.ledger()
        ledger.entregar(entrega(), 100)
        with self.assertRaises(ValueError):
            ledger.entregar(replace(entrega(), apto="/otra"), 200)
        self.assertEqual(len(ledger.estado(REPO).entradas), 1)

    def test_head_nuevo_es_otra_entrega(self):
        ledger = self.ledger()
        a = ledger.entregar(entrega(), 100)
        b = ledger.entregar(entrega(head="c" * 40), 200)
        self.assertNotEqual(a, b)
        self.assertEqual(len(ledger.estado(REPO).entradas), 2)

    def test_schema_y_tablas(self):
        ledger = self.ledger()
        self.assertEqual(ledger.db.execute("PRAGMA user_version").fetchone()[0], 1)
        tablas = {r[0] for r in ledger.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"requests", "entries", "trains", "effects", "events", "outbox"}.issubset(tablas))

    def test_schema_futuro_rechazado(self):
        ledger = Ledger()
        ledger.db.execute("PRAGMA user_version=99")
        ledger.close()
        with self.assertRaises(ValueError):
            Ledger()

    def test_interrupcion_real_mitad_transaccion(self):
        ledger = Ledger()
        ledger.entregar(entrega(), 100)
        esperado = ledger.estado(REPO)
        eventos = ledger.ultimos_eventos(REPO)
        ledger.close()
        guion = """import os, sys
sys.path.insert(0, sys.argv[1])
from ledger import Ledger
l = Ledger()
with l.transaccion():
    l.evento(sys.argv[2], 'incompleto', 200, {})
    l.db.execute("UPDATE entries SET payload='corrupto'")
    os._exit(17)
"""
        proc = subprocess.run([sys.executable, "-I", "-c", guion, str(RAIZ), REPO], timeout=20)
        self.assertEqual(proc.returncode, 17)
        reabierto = self.ledger()
        self.assertEqual(reabierto.estado(REPO), esperado)
        self.assertEqual(reabierto.ultimos_eventos(REPO), eventos)
        self.assertEqual(reabierto.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_sombra_no_ejecuta_ni_modifica_custodia(self):
        ledger = self.ledger()
        for i in (1, 2, 3):
            ledger.entregar(entrega(i), i)
        custodia = ledger.estado(REPO)
        propuesto, efectos = paso(custodia, observacion(3))
        ledger.sombra(propuesto, efectos, 100)
        self.assertEqual(ledger.estado(REPO), custodia)
        self.assertEqual(ledger.estado(REPO, sombra=True), propuesto)
        self.assertEqual(ledger.ultimos_eventos(REPO)[0]["tipo"], "sombra")
        for tabla in ("trains", "effects", "outbox"):
            self.assertEqual(ledger.db.execute("SELECT count(*) FROM " + tabla).fetchone()[0], 0)
        ledger.entregar(entrega(4), 400)
        self.assertEqual(len(ledger.estado(REPO, sombra=True).entradas), 4)

    def test_flock_excluye_mismo_repo_permite_otro(self):
        ledger = self.ledger()
        guion = """import fcntl, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from dominio import clave
p = Path(sys.argv[2]) / (clave('repo', sys.argv[3]) + '.lock')
with p.open('a') as h:
    try:
        fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(23)
"""
        with ledger.candado(REPO):
            for repo, codigo in ((REPO, 23), ("example/other", 0)):
                proc = subprocess.run([sys.executable, "-I", "-c", guion,
                                       str(RAIZ), str(ledger.directorio), repo], timeout=20)
                self.assertEqual(proc.returncode, codigo)
        proc = subprocess.run([sys.executable, "-I", "-c", guion,
                               str(RAIZ), str(ledger.directorio), REPO], timeout=20)
        self.assertEqual(proc.returncode, 0)


class CLITests(Aislado):
    def test_entregar_estado_duplicado(self):
        args = ("entregar", "--repo", REPO, "--pr", "1", "--head", HEAD,
                "--apto", "apto.json", "--dirigida", "dirigida.json")
        uno, dos = self.cli(*args), self.cli(*args)
        self.assertEqual(uno.returncode, 0, uno.stderr)
        self.assertEqual(uno.stdout, dos.stdout)
        self.assertEqual(json.loads(uno.stdout)["estado"], "custodia")
        estado_cli = self.cli("estado", "--repo", REPO, "--juicio")
        self.assertEqual(estado_cli.returncode, 0, estado_cli.stderr)
        payload = json.loads(estado_cli.stdout)
        self.assertEqual(len(payload["estado"]["entradas"]), 1)

    def test_tick_sin_seco_no_abre_ledger(self):
        proc = self.cli("tick", "--repo", REPO)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout), {"error": "tick activo requiere inscripción activa",
                                                   "inscripcion": "no-inscrito"})
        self.assertFalse(directorio_estado().exists())

    def test_argumentos_invalidos(self):
        for args in (("tick", "--repo", "-mal"),
                     ("entregar", "--repo", REPO, "--pr", "0", "--head", HEAD,
                      "--apto", "a", "--dirigida", "d")):
            self.assertEqual(self.cli(*args).returncode, 2)

    def test_sombra_con_falso_continua_ventana(self):
        ledger = Ledger()
        ledger.entregar(entrega(), 10)
        ledger.close()
        args = cli.parser().parse_args(["tick", "--repo", REPO, "--seco"])
        falso = Falso(observacion())
        codigo, payload = cli.ejecutar(args, falso, 100)
        self.assertEqual(codigo, 0)
        self.assertFalse(payload["plan"]["estado"]["trenes"])
        _, payload = cli.ejecutar(args, falso, 3700)
        self.assertEqual(len(payload["plan"]["estado"]["trenes"]), 1)

    def test_error_global_no_evento_sombra(self):
        class Fallido:
            def observar(self, estado, ahora):
                raise ValueError("sin lectura")
        args = cli.parser().parse_args(["tick", "--repo", REPO, "--seco"])
        with self.assertRaises(ValueError):
            cli.ejecutar(args, Fallido(), 100)
        self.assertEqual(self.ledger().ultimos_eventos(REPO), [])


class ObservadorTests(Aislado):
    def setUp(self):
        super().setUp()
        self.config = json.loads((RAIZ / "config.example.json").read_text())["repos"]["owner/project"]
        self.apto = self.raiz / "apto.json"
        self.dirigida = self.raiz / "dirigida.json"
        self.apto.write_text(json.dumps({"veredicto": "APTO", "head": HEAD, "hallazgos_abiertos": 0}))
        self.dirigida.write_text(json.dumps({"head": HEAD, "resultado": "limpio"}))
        self.entrega = replace(entrega(), apto=str(self.apto), dirigida=str(self.dirigida))
        self.pr = {"head": {"sha": HEAD}, "base": {"sha": BASE, "ref": "main"},
                   "state": "open", "changed_files": 1, "labels": [], "mergeable": True}
        self.archivos = [[{"filename": "src/app.py"}]]
        self.checks = [{"check_runs": [{"id": 1, "name": "lint", "status": "completed", "conclusion": "success"}]}]
        self.statuses = [[]]
        self.llamadas = []

    def ejecutar(self, args, **kwargs):
        self.llamadas.append(args)
        if args[0] == "git":
            salida = "c" * 40 if args[3] == "merge-base" else ""
        else:
            self.assertEqual(args[:4], ["gh", "api", "--method", "GET"])
            ruta = args[4]
            if "/files?" in ruta:
                salida = self.archivos
            elif "/check-runs?" in ruta:
                salida = self.checks
            elif "/statuses?" in ruta:
                salida = self.statuses
            elif "/git/ref/" in ruta:
                salida = {"object": {"sha": BASE}}
            else:
                salida = self.pr
            salida = json.dumps(salida)
        return subprocess.CompletedProcess(args, 0, salida, "")

    def observador(self):
        return GitHubGit(self.config, ejecutar=self.ejecutar)

    def decision(self):
        observador = self.observador()
        e = Estado(REPO, (Entrada(self.entrega, 1),))
        obs = observador.observar(e, 100)
        return paso(e, obs)[0].entradas[0].admision

    def test_real_usa_get_sin_token_y_admite(self):
        with patch.dict(os.environ, {"GH_TOKEN": "token-de-prueba-no-exportar"}):
            self.assertIsInstance(self.decision(), Admitido)
        self.assertTrue(self.llamadas)
        self.assertFalse(any("token-de-prueba" in str(args) for args in self.llamadas))

    def test_apto_literal_y_head_exacto(self):
        for veredicto, head in (("apto", HEAD), ("NO APTO", HEAD), ("APTO", "e" * 40)):
            self.apto.write_text(json.dumps({"veredicto": veredicto, "head": head, "hallazgos_abiertos": 0}))
            self.assertEqual(self.decision(), Retenido(Motivo.APTO))

    def test_evidencia_ilegible_retiene(self):
        self.apto.write_text("APTO sin head ni estructura")
        self.assertEqual(self.decision(), Retenido(Motivo.INCIERTO))

    def test_hallazgos_debe_ser_entero_no_booleano(self):
        self.apto.write_text(json.dumps({"veredicto": "APTO", "head": HEAD, "hallazgos_abiertos": False}))
        self.assertEqual(self.decision(), Retenido(Motivo.HALLAZGOS))

    def test_sensibles_ci_gobierno_lockfile(self):
        for archivo in (".github/workflows/check.yml", "AGENTS.md", "sub/CLAUDE.md", "package-lock.json", "sub/bun.lock"):
            with self.subTest(archivo=archivo):
                self.archivos = [[{"filename": archivo}]]
                self.assertTrue(es_sensible([archivo], self.config))
                self.assertEqual(self.decision(), Retenido(Motivo.MERGE_READY))

    def test_sensible_solo_autorizacion_del_head(self):
        self.archivos = [[{"filename": "AGENTS.md"}]]
        self.config["heads_autorizados"] = ["c" * 40]
        self.assertEqual(self.decision(), Retenido(Motivo.MERGE_READY))
        self.config["heads_autorizados"] = [HEAD]
        self.assertIsInstance(self.decision(), Admitido)

    def test_pagina_dos_ci_pendiente_retiene(self):
        self.checks.append({"check_runs": [{"id": 2, "name": "tests", "status": "in_progress", "conclusion": None}]})
        self.assertEqual(self.decision(), Retenido(Motivo.CI))

    def test_status_nuevo_pendiente_no_oculto_por_verde(self):
        self.statuses = [[{"context": "build", "state": "pending"}],
                         [{"context": "build", "state": "success"}]]
        self.assertEqual(self.decision(), Retenido(Motivo.CI))

    def test_ci_ausente_retiene(self):
        self.checks = [{"check_runs": []}]
        self.assertEqual(self.decision(), Retenido(Motivo.CI))

    def test_timeout_conserva_ventana(self):
        e = Estado(REPO, (Entrada(self.entrega, 1),))
        e, _ = paso(e, self.observador().observar(e, 100))
        original = self.ejecutar

        def con_timeout(args, **kwargs):
            if args[0] == "gh" and "/files?" in args[4]:
                raise subprocess.TimeoutExpired(args, 60)
            return original(args, **kwargs)
        e, _ = paso(e, GitHubGit(self.config, ejecutar=con_timeout).observar(e, 200))
        self.assertEqual(e.entradas[0].admision, Retenido(Motivo.INCIERTO))
        self.assertEqual(e.entradas[0].admitida, 100)
        e, efectos = paso(e, self.observador().observar(e, 3700))
        self.assertEqual(len(e.trenes), 1)

    def test_rechazadas_no_se_observan(self):
        e = Estado(REPO, tuple(Entrada(replace(self.entrega, head=format(i, "040x")), i,
                                       Rechazado(Motivo.HEAD)) for i in range(1, 51)))
        obs = self.observador().observar(e, 100)
        self.assertEqual(obs.prs, ())
        self.assertEqual(sum(a[0] == "gh" for a in self.llamadas), 1)

    def test_ci_vacio_declarado_admite(self):
        self.checks = [{"check_runs": []}]
        self.config["checks_esperados"] = []
        self.assertIsInstance(self.decision(), Admitido)

    def test_check_esperado_ausente_retiene(self):
        self.config["checks_esperados"] = ["lint", "e2e"]
        self.assertEqual(self.decision(), Retenido(Motivo.CI))
        self.config["checks_esperados"] = ["lint"]
        self.assertIsInstance(self.decision(), Admitido)

    def test_rerun_supera_corrida_cancelada(self):
        self.checks = [{"check_runs": [
            {"id": 9, "name": "lint", "status": "completed", "conclusion": "success"},
            {"id": 3, "name": "lint", "status": "completed", "conclusion": "cancelled"}]}]
        self.assertIsInstance(self.decision(), Admitido)
        self.checks = [{"check_runs": [
            {"id": 3, "name": "lint", "status": "completed", "conclusion": "success"},
            {"id": 9, "name": "lint", "status": "completed", "conclusion": "failure"}]}]
        self.assertEqual(self.decision(), Retenido(Motivo.CI))

    def test_archivos_truncados_retienen(self):
        self.pr["changed_files"] = 3001
        self.assertEqual(self.decision(), Retenido(Motivo.INCIERTO))

    def test_head_cambia_durante_lectura(self):
        observador = self.observador()
        original = observador.api
        lecturas = []
        def api(ruta, paginado=False):
            valor = original(ruta, paginado)
            if ruta.endswith("/pulls/1"):
                lecturas.append(1)
                if len(lecturas) == 2:
                    valor["head"]["sha"] = "c" * 40
            return valor
        observador.api = api
        e = Estado(REPO, (Entrada(self.entrega, 1),))
        nuevo, _ = paso(e, observador.observar(e, 100))
        self.assertEqual(nuevo.entradas[0].admision, Rechazado(Motivo.HEAD))

    def test_comentario_json_y_rechazo_url_ajena(self):
        observador = self.observador()
        observador.api = lambda ruta: {"body": self.apto.read_text(),
                                       "issue_url": "https://api.github.com/repos/example/project/issues/1"}
        referencia = "https://github.com/example/project/pull/1#issuecomment-23"
        self.assertEqual(observador.evidencia(referencia, REPO, 1)["veredicto"], "APTO")
        for url in ("https://other.example/example/project/pull/1#issuecomment-23",
                    "https://github.com/example/project/pull/2#issuecomment-23"):
            with self.assertRaises(ValueError):
                observador.evidencia(url, REPO, 1)

    def test_config_categorias_obligatorias(self):
        ruta = self.raiz / "config.json"
        ruta.write_text(json.dumps({"repos": {REPO: self.config}}))
        self.assertEqual(configuracion(ruta, REPO), self.config)
        self.config["sensibles"]["gobierno"] = []
        ruta.write_text(json.dumps({"repos": {REPO: self.config}}))
        with self.assertRaises(ValueError):
            configuracion(ruta, REPO)

    def test_git_real_conflicto_con_origen_local_sin_writes(self):
        repo = self.raiz / "origen"
        repo.mkdir()
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], check=True,
                                  capture_output=True, text=True, timeout=20).stdout.strip()
        git("init", "-b", "main")
        git("config", "user.name", "Fixture")
        git("config", "user.email", "fixture@example.invalid")
        archivo = repo / "archivo"
        archivo.write_text("base\n")
        git("add", "archivo")
        git("commit", "-m", "base")
        git("checkout", "-b", "ticket")
        archivo.write_text("ticket\n")
        git("commit", "-am", "ticket")
        head = git("rev-parse", "HEAD")
        git("checkout", "main")
        archivo.write_text("main\n")
        git("commit", "-am", "main")
        base = git("rev-parse", "HEAD")
        self.config["git"] = str(repo)
        self.pr["head"]["sha"] = head
        self.pr["base"]["sha"] = base
        self.apto.write_text(json.dumps({"veredicto": "APTO", "head": head, "hallazgos_abiertos": 0}))
        self.dirigida.write_text(json.dumps({"head": head, "resultado": "limpio"}))
        def ejecutar(args, **kwargs):
            return subprocess.run(args, **kwargs) if args[0] == "git" else self.ejecutar(args, **kwargs)
        observador = GitHubGit(self.config, ejecutar=ejecutar)
        entrada = replace(self.entrega, head=head)
        objetos_antes = sorted(str(p.relative_to(repo)) for p in (repo / ".git/objects").rglob("*"))
        lectura = observador.vista(entrada)
        self.assertTrue(lectura.conflicto)
        self.assertEqual(git("rev-parse", "HEAD"), base)
        self.assertEqual(objetos_antes, sorted(str(p.relative_to(repo)) for p in (repo / ".git/objects").rglob("*")))


if __name__ == "__main__":
    unittest.main()
