"""U13: tick activo y outbox. Sin red; sólo en el host de pruebas. Fixtures conservadas.

GitHub y el lanzador de Hasito son falsos; git es real contra un origen local.
Una muerte se simula con una BaseException que ningún except del tick captura:
el candado se suelta y la transacción abierta se revierte, como al morir.
"""
from contextlib import contextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import test_aterrizaje as fixtures
from test_hasito import suite_fixture
from test_tren import cli
import activo
from adaptadores.hasito import Hasito
from dominio import (Armado, Candidato, Congelado, Efecto, Entrega, EsperandoCI, Motivo,
                     Pausa, Rechazado, Terminal, clave)
import efectos
from git_ops import GitTren
from ledger import Ledger, directorio_estado
from observador import GitHubGit

RAIZ = Path(__file__).resolve().parents[1]
REPO = "example/project"
CERO = "0" * 40
EJECUTADOS = ("armar-candidato", "solicitar-ci", "iniciar-suite", "promover")


class Muerte(BaseException):
    """El proceso muere aquí."""


class Lanzador:
    """Lanzador falso: cada llamada sería una suite real en un host."""
    def __init__(self, raiz):
        self.raiz = raiz
        self.llamadas = []

    def __call__(self, argv, **kwargs):
        self.llamadas.append(tuple(argv))
        C, B = argv[argv.index("--suite") + 1], argv[argv.index("--base") + 1]
        carpeta = Path(argv[argv.index("--evidencia") + 1])
        carpeta.mkdir(parents=True, exist_ok=True)
        archivo = carpeta / "resultado.json"
        archivo.write_text(json.dumps(suite_fixture(Candidato(B, (), C, ""), 0, ())),
                           encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "SUITE veredicto=0 resultado={}\n".format(archivo), "")


class Base(unittest.TestCase):
    commit = fixtures.AterrizajeTests.commit
    comando = fixtures.AterrizajeTests.comando
    push = fixtures.AterrizajeTests.push
    remoto = fixtures.AterrizajeTests.remoto

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="pao-tren-u13-"))
        self.env = patch.dict(os.environ, {"GIT_CONFIG_NOSYSTEM": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        version = subprocess.run(["git", "version"], capture_output=True, text=True, check=True).stdout
        numeros = re.search(r"git version (\d+)\.(\d+)", version)
        self.assertGreaterEqual(tuple(map(int, numeros.groups())), (2, 38), version)
        self.escenario()

    def escenario(self, nombre="escenario"):
        """Origen, PR, evidencias, inscripción y estado nuevos; nada se reutiliza."""
        self.raiz = Path(tempfile.mkdtemp(prefix=nombre + "-", dir=str(self.base)))
        self.home = self.raiz / "home"
        self.home.mkdir()
        os.environ.update(HOME=str(self.home), XDG_STATE_HOME=str(self.raiz / "state"),
                          XDG_CONFIG_HOME=str(self.raiz / "config"),
                          GIT_CONFIG_GLOBAL=str(self.home / ".gitconfig"))
        self.origen, self.fuente = self.raiz / "origen.git", self.raiz / "fuente.git"
        for ruta in (self.origen, self.fuente):
            subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(ruta)],
                           capture_output=True, check=True, text=True)
        self.B = self.commit({"comun": "base\n"}, mensaje="base")
        self.push(self.B, "refs/heads/main")
        self.heads = {}
        for pr in (1, 2, 3, 4):
            self.heads[pr] = self.commit({"comun": "base\n", "pr-{}".format(pr): "ticket\n"},
                                         padres=(self.B,), mensaje="ticket {}".format(pr))
            self.push(self.heads[pr], "refs/pull/{}/head".format(pr))
        # Cada actualización real de una ref del origen queda en este registro.
        self.registro = self.raiz / "actualizaciones.log"
        hook = self.origen / "hooks/post-receive"
        hook.write_text("#!/bin/sh\ncat >> '{}'\n".format(self.registro))
        hook.chmod(0o755)
        self.inscribir(REPO)
        config = json.loads((RAIZ / "config.example.json").read_text())["repos"]["owner/project"]
        self.config = dict(config, git=str(self.origen), checks_esperados=["lint"],
                           remoto=str(self.origen), lanzador=str(self.raiz / "run.sh"))
        self.heads_gh = {}
        self.gh_falla = False
        self.git = GitTren(str(self.origen))
        self.lanzador = Lanzador(self.raiz)
        self.hasito = Hasito(self.raiz / "run.sh", self.git, self.lanzador)
        self.observador = GitHubGit(self.config, ejecutar=self.gh, activo=True)

    def inscribir(self, texto):
        registro = self.raiz / "config/pao-tren/inscritos"
        registro.parent.mkdir(parents=True, exist_ok=True)
        registro.write_text(texto + "\n")

    def gh(self, args, **kwargs):
        if args[0] == "git":
            return subprocess.run(args, **kwargs)
        self.assertEqual(args[:4], ["gh", "api", "--method", "GET"])
        if self.gh_falla:
            raise subprocess.CalledProcessError(1, args)
        ruta, main = args[4], self.remoto()
        if ruta.endswith("/git/ref/heads/main"):
            salida = {"object": {"sha": main}}
        elif "/check-runs?" in ruta:
            salida = [{"check_runs": [{"id": 1, "name": "lint", "status": "completed",
                                       "conclusion": "success"}]}]
        elif "/statuses?" in ruta:
            salida = [[]]
        else:
            m = re.fullmatch(r"repos/" + REPO + r"/pulls/(\d+)(/files\?per_page=100)?", ruta)
            pr = int(m[1])
            head = self.heads_gh.get(pr, self.heads[pr])
            if m[2]:
                salida = [[{"filename": "pr-{}".format(pr)}]]
            else:
                mergeado = subprocess.run(["git", "-C", str(self.origen), "merge-base",
                    "--is-ancestor", head, "refs/heads/main"]).returncode == 0
                salida = {"head": {"sha": head}, "base": {"sha": main, "ref": "main"},
                          "state": "closed" if mergeado else "open", "merged": mergeado,
                          "changed_files": 1, "labels": [], "mergeable": True}
        return subprocess.CompletedProcess(args, 0, json.dumps(salida), "")

    def entregar(self, prs=(1, 2, 3)):
        ledger = Ledger()
        try:
            for pr in prs:
                apto, dirigida = self.raiz / "apto-{}.json".format(pr), self.raiz / "dirigida-{}.json".format(pr)
                apto.write_text(json.dumps({"veredicto": "APTO", "head": self.heads[pr],
                                            "hallazgos_abiertos": 0}))
                dirigida.write_text(json.dumps({"head": self.heads[pr], "resultado": "limpio"}))
                ledger.entregar(Entrega(REPO, pr, self.heads[pr], str(apto), str(dirigida)), 10 + pr)
        finally:
            ledger.close()

    def tick(self, ahora, observador=None):
        ledger = Ledger()
        try:
            return activo.tick(ledger, REPO, observador or self.observador, self.git, self.hasito, ahora)
        finally:
            ledger.close()

    def leer(self, funcion):
        ledger = Ledger()
        try:
            return funcion(ledger)
        finally:
            ledger.close()

    def estado(self):
        return self.leer(lambda l: l.operativo(REPO))

    def filas(self, sql, *args):
        return self.leer(lambda l: [tuple(r) for r in l.db.execute(sql, args)])

    def actualizaciones(self):
        if not self.registro.exists():
            return []
        return [tuple(l.split()) for l in self.registro.read_text().splitlines()]

    def hasta_terminal(self, maximo=12):
        """Ticks de 100 s hasta que el primer tren termine; una muerte gasta un tick."""
        muertes = 0
        for i in range(maximo):
            try:
                self.tick(100 + 100 * i)
            except Muerte:
                muertes += 1
            trenes = self.estado().trenes
            if trenes and isinstance(trenes[0].fase, Terminal):
                return muertes
        self.fail("no terminó: {}".format(self.estado().trenes))

    def assert_aterrizo_una_vez(self, suites=None, pendientes=()):
        """main == C una vez, una rama de tren, una suite y cada resultado aplicado una vez."""
        estado = self.estado()
        tren = estado.trenes[0]
        self.assertEqual(tren.fase, Terminal("ATERRIZADO"))
        resultados = [json.loads(p) for (p,) in self.filas(
            "SELECT payload FROM events WHERE tipo='efecto-resultado'")]
        candidato = next(r["observacion"]["candidato"] for r in resultados
                         if r["tipo"] == "armar-candidato")
        C, arbol = candidato["C"], candidato["arbol"]
        self.assertEqual(self.remoto(), C)
        self.assertEqual(self.comando(self.origen, "rev-parse", "main^{tree}"), arbol)
        for pr in (1, 2, 3):
            self.comando(self.origen, "merge-base", "--is-ancestor", self.heads[pr], "main")
        cambios = self.actualizaciones()
        self.assertEqual([c for c in cambios if c[2] == "refs/heads/main"],
                         [(self.B, C, "refs/heads/main")])
        self.assertEqual([c for c in cambios if c[2].startswith("refs/heads/tren/")],
                         [(CERO, C, "refs/heads/tren/" + tren.id)])
        self.assertEqual(len(self.lanzador.llamadas) if suites is None else suites, 1)
        filas = self.filas("SELECT e.payload, o.estado FROM effects e JOIN outbox o ON o.clave=e.clave")
        self.assertEqual(sorted((json.loads(p)["tipo"], e) for p, e in filas),
                         sorted([(t, "hecho") for t in EJECUTADOS] +
                                [(t, "pendiente") for t in pendientes]))
        claves = [r["clave"] for r in resultados]
        self.assertEqual(len(claves), len(set(claves)))
        self.assertEqual(len(claves), len(EJECUTADOS))
        return C


class PuertaTests(Base):
    def cli(self, *args):
        return subprocess.run([sys.executable, "-I", str(RAIZ / "pao-tren"), *args],
                              capture_output=True, text=True, timeout=60)

    def test_no_inscrito_sombra_y_ambiguo_no_abren_ledger(self):
        casos = [("# vacío", "no-inscrito"), (REPO + " sombra", "sombra"),
                 (REPO + " desconocido", "ambiguo"), (REPO + " sombra\n" + REPO, "ambiguo"),
                 (REPO + "\v", "ambiguo")]
        for texto, esperado in casos:
            with self.subTest(texto=texto):
                self.inscribir(texto)
                proc = self.cli("tick", "--repo", REPO)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertEqual(json.loads(proc.stdout),
                                 {"error": "tick activo requiere inscripción activa",
                                  "inscripcion": esperado})
                self.assertFalse(directorio_estado().exists())

    def test_activo_sin_remoto_ni_lanzador_no_abre_ledger(self):
        config = self.raiz / "config.json"
        sin = {k: v for k, v in self.config.items() if k not in ("remoto", "lanzador")}
        config.write_text(json.dumps({"repos": {REPO: sin}}))
        proc = self.cli("tick", "--repo", REPO, "--config", str(config))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout)["error"], "config activa requiere remoto y lanzador")
        self.assertFalse(directorio_estado().exists())

    def test_inscripcion_releida_bajo_el_candado(self):
        self.entregar()
        self.inscribir(REPO + " sombra")
        with self.assertRaises(activo.PuertaCerrada) as error:
            self.tick(100)
        self.assertEqual(error.exception.inscripcion, "sombra")
        self.assertEqual(self.filas("SELECT tipo FROM events WHERE tipo NOT IN ('custodia')"), [])
        self.assertEqual(self.filas("SELECT * FROM outbox"), [])

    def test_activo_cli_real_aterriza_con_gh_y_lanzador_falsos(self):
        """Cableado del CLI: observador real con gh en PATH, GitTren y Hasito reales."""
        self.entregar()
        bin_falso = self.raiz / "bin"
        bin_falso.mkdir()
        datos = self.raiz / "gh.json"
        datos.write_text(json.dumps({"repo": REPO, "origen": str(self.origen),
                                     "heads": {str(k): v for k, v in self.heads.items()}}))
        gh = bin_falso / "gh"
        gh.write_text("#!{} -I\n".format(sys.executable) + GH_FALSO.format(datos=str(datos)))
        gh.chmod(0o755)
        llamadas = self.raiz / "lanzador.log"
        guion = self.raiz / "lanzador.py"
        guion.write_text(LANZADOR_FALSO.format(tests=str(Path(__file__).resolve().parent),
                                               log=str(llamadas)))
        (self.raiz / "run.sh").write_text('#!/usr/bin/env bash\nexec "{}" -I "{}" "$@"\n'.format(
            sys.executable, guion))
        config = self.raiz / "config.json"
        config.write_text(json.dumps({"repos": {REPO: self.config}}))
        env = dict(os.environ, PATH=str(bin_falso) + os.pathsep + os.environ.get("PATH", ""))
        salidas = []
        for _ in range(6):
            proc = subprocess.run([sys.executable, "-I", str(RAIZ / "pao-tren"), "tick", "--repo",
                                   REPO, "--config", str(config)], capture_output=True, text=True,
                                  timeout=120, env=env)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            salidas.append(json.loads(proc.stdout))
            if salidas[-1]["terminales"]:
                break
        self.assertEqual(salidas[-1]["terminales"][0]["resultado"], "ATERRIZADO", salidas)
        self.assertEqual([[e["tipo"] for e in s["ejecutados"]] for s in salidas],
                         [[t] for t in EJECUTADOS])
        self.assert_aterrizo_una_vez(suites=len(llamadas.read_text().splitlines()))
        estado = subprocess.run([sys.executable, "-I", str(RAIZ / "pao-tren"), "estado",
                                 "--repo", REPO], capture_output=True, text=True, timeout=60)
        operativo = json.loads(estado.stdout)["operativo"]
        self.assertIsNone(operativo["tren"])
        self.assertEqual(operativo["efectos"], [])


class TickTests(Base):
    def test_lote_de_punta_a_punta_aterriza_con_main_igual_a_C(self):
        self.entregar()
        informes = [self.tick(100 + 100 * i) for i in range(4)]
        self.assertEqual([[e["tipo"] for e in i["ejecutados"]] for i in informes],
                         [[t] for t in EJECUTADOS])
        self.assertEqual([i["tren"]["fase"] if i["tren"] else None for i in informes],
                         ["Armado", "EsperandoCI", "Certificado", None])
        C = self.assert_aterrizo_una_vez()
        eventos = len(self.filas("SELECT seq FROM events"))
        # Ya aterrizado: los PR cerrados no caducan ni se reobservan; nada cambia.
        llamadas = []
        observador = GitHubGit(self.config, ejecutar=lambda a, **k: llamadas.append(a) or self.gh(a, **k),
                               activo=True)
        final = self.tick(900, observador)
        self.assertEqual((final["encolados"], final["ejecutados"]), ([], []))
        self.assertEqual(len(self.filas("SELECT seq FROM events")), eventos)
        self.assertFalse(any("/pulls/" in " ".join(a) for a in llamadas))
        self.assertEqual(self.remoto(), C)

    def test_ci_del_candidato_pendiente_no_inicia_suite(self):
        self.entregar()
        self.tick(100)
        self.tick(200)
        original = self.gh

        def sin_ci(args, **kwargs):
            if (args[0] == "gh" and "/check-runs?" in args[4] and
                    "/commits/" + self.estado().trenes[0].fase.candidato.C in args[4]):
                return subprocess.CompletedProcess(args, 0, json.dumps([{"check_runs": []}]), "")
            return original(args, **kwargs)
        self.observador.ejecutar = sin_ci
        informe = self.tick(300)
        self.assertEqual((informe["encolados"], informe["ejecutados"]), ([], []))
        self.assertIsInstance(self.estado().trenes[0].fase, EsperandoCI)
        self.assertEqual(self.lanzador.llamadas, [])

    def test_clave_guardada_no_se_duplica_y_otra_carga_se_rechaza(self):
        ledger = Ledger()
        self.addCleanup(ledger.close)
        estado = ledger.operativo(REPO)
        efecto = Efecto(clave("x"), "armar-candidato", ("t",))
        self.assertEqual(ledger.avanzar(estado, estado, (efecto, efecto), 1, "prueba"), (efecto.clave,))
        self.assertEqual(ledger.avanzar(estado, estado, (efecto,), 2, "prueba"), ())
        ledger.marcar(REPO, efecto.clave, "hecho", "efecto-resultado", {}, 3)
        self.assertEqual(ledger.avanzar(estado, estado, (efecto,), 4, "prueba"), ())
        self.assertEqual(ledger.outbox(REPO), [])
        with self.assertRaises(ValueError):
            ledger.avanzar(estado, estado, (replace(efecto, datos=("u",)),), 5, "prueba")
        self.assertEqual(len(ledger.db.execute("SELECT * FROM events WHERE tipo='operativo'").fetchall()), 1)

    def test_observacion_fallida_no_guarda_nada(self):
        self.entregar()
        self.tick(100)
        antes = self.filas("SELECT seq FROM events"), self.filas("SELECT * FROM outbox")
        self.gh_falla = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.tick(200)
        self.assertEqual((self.filas("SELECT seq FROM events"), self.filas("SELECT * FROM outbox")), antes)
        self.gh_falla = False
        self.hasta_terminal()
        self.assert_aterrizo_una_vez()

    def test_fallo_de_git_queda_fallido_legible_y_converge(self):
        self.entregar()
        real = subprocess.run
        fallas = []

        def sin_fetch(args, **kwargs):
            if "fetch" in args and not fallas:
                fallas.append(args)
                return subprocess.CompletedProcess(args, 128, "", "")
            return real(args, **kwargs)
        self.git.ejecutar = sin_fetch
        informe = self.tick(100)
        self.assertEqual(informe["ejecutados"][0]["resultado"], "fallido")
        self.assertIsInstance(self.estado().trenes[0].fase, Congelado)
        abiertos = self.leer(lambda l: activo.efectos_abiertos(l, REPO))
        self.assertEqual([(e["tipo"], e["estado"], e["intentos"], e["ultimo"]["registro"])
                          for e in abiertos], [("armar-candidato", "fallido", 1, "efecto-fallido")])
        self.assertEqual(abiertos[0]["ultimo"]["resultado"]["_tipo"], "ErrorGit")
        informe = self.tick(200)
        self.assertEqual(informe["ejecutados"][0]["resultado"], "hecho")
        self.assertIsInstance(self.estado().trenes[0].fase, Armado)
        self.hasta_terminal()
        self.assert_aterrizo_una_vez()

    def test_efecto_sin_ejecutor_queda_pendiente_y_no_bloquea(self):
        self.entregar((1, 2, 3, 4))
        otro = self.commit({"comun": "base\n", "pr-4": "otro head\n"}, (self.B,), "ticket 4 bis")
        self.heads_gh[4] = otro
        self.hasta_terminal()
        self.assert_aterrizo_una_vez(pendientes=("avisar-caducidad",))
        estado = self.estado()
        self.assertEqual(estado.entradas[3].admision, Rechazado(Motivo.HEAD))
        args = cli.parser().parse_args(["estado", "--repo", REPO])
        codigo, salida = cli.ejecutar(args)
        self.assertEqual(codigo, 0)
        efectos_abiertos = salida["operativo"]["efectos"]
        self.assertEqual([(e["tipo"], e["estado"], e["ejecutor"], e["intentos"])
                          for e in efectos_abiertos], [("avisar-caducidad", "pendiente", False, 0)])
        self.assertIsNone(salida["operativo"]["pausa"])
        self.assertEqual(salida["operativo"]["terminales"][0]["resultado"], "ATERRIZADO")

    def test_main_movido_por_fuera_durante_promover_pausa(self):
        self.entregar()
        ajeno = self.commit({"comun": "base\n", "ajeno": "externo\n"}, (self.B,), "externo")
        real = subprocess.run

        def carrera(args, **kwargs):
            if "push" in args and args[-1].endswith(":refs/heads/main"):
                self.push(ajeno, "refs/heads/main")
            return real(args, **kwargs)
        self.git.ejecutar = carrera
        self.hasta_terminal()
        estado = self.estado()
        self.assertEqual(estado.trenes[0].fase, Terminal("REARMAR"))
        self.assertEqual(estado.pausa, Pausa("escritor-externo", main=ajeno))
        self.assertTrue(all(e.tren is None for e in estado.entradas))
        self.assertEqual(self.remoto(), ajeno)
        self.assertEqual([c for c in self.actualizaciones() if c[2] == "refs/heads/main"],
                         [(self.B, ajeno, "refs/heads/main")])
        # Pausado: el tick siguiente no congela otro lote ni escribe.
        informe = self.tick(5000)
        self.assertEqual((informe["encolados"], informe["ejecutados"]), ([], []))
        self.assertEqual(informe["pausa"]["motivo"], "escritor-externo")
        self.assertEqual(self.remoto(), ajeno)

    def test_dos_ticks_a_la_vez_el_candado_serializa(self):
        self.entregar()
        dentro, soltar = threading.Event(), threading.Event()
        orden, errores = [], []
        real = self.observador

        class Primero:
            def observar(self, estado, ahora):
                orden.append(("A", len(estado.trenes)))
                dentro.set()
                soltar.wait(30)
                return real.observar(estado, ahora)

        class Segundo:
            def observar(self, estado, ahora):
                orden.append(("B", len(estado.trenes)))
                return real.observar(estado, ahora)

        def correr(observador, ahora):
            try:
                self.tick(ahora, observador)
            except BaseException as exc:  # registrar para el hilo principal
                errores.append(exc)
        a = threading.Thread(target=correr, args=(Primero(), 100))
        b = threading.Thread(target=correr, args=(Segundo(), 101))
        a.start()
        self.assertTrue(dentro.wait(30))
        b.start()
        time.sleep(0.5)
        self.assertEqual(orden, [("A", 0)])
        soltar.set()
        a.join(60)
        b.join(60)
        self.assertEqual(errores, [])
        # B vio el tren que A congeló y armó: un solo armar, ya confirmado.
        self.assertEqual(orden, [("A", 0), ("B", 1)])
        tipos = [json.loads(p)["tipo"] for (p,) in self.filas("SELECT payload FROM effects ORDER BY rowid")]
        self.assertEqual(tipos, ["armar-candidato", "solicitar-ci"])
        self.assertEqual([c[2] for c in self.actualizaciones()], ["refs/heads/tren/" + self.estado().trenes[0].id])


class MuerteTests(Base):
    """Una muerte en cada frontera; el tick siguiente converge sin repetir IO externo."""

    def converge(self, inyeccion, tipos=EJECUTADOS):
        for tipo in tipos:
            with self.subTest(tipo=tipo):
                self.escenario(tipo)
                self.entregar()
                with inyeccion(tipo) as muertes:
                    self.assertGreaterEqual(self.hasta_terminal(), 1)
                self.assertEqual(muertes, [tipo])
                self.assert_aterrizo_una_vez()

    @contextmanager
    def una_vez(self, objetivo, nombre, envoltura):
        muertes = []
        real = getattr(objetivo, nombre)
        with patch.object(objetivo, nombre, envoltura(real, muertes)):
            yield muertes

    def test_tras_guardar_el_paso(self):
        def envoltura(tipo):
            def hacer(real, muertes):
                def drenar(ledger, repo, *args):
                    if not muertes and any(e.tipo == tipo for _, _, e in ledger.outbox(repo)):
                        muertes.append(tipo)
                        raise Muerte()
                    return real(ledger, repo, *args)
                return drenar
            return self.una_vez(activo, "drenar", hacer)
        self.converge(envoltura)

    def test_en_curso_antes_de_ejecutar(self):
        def envoltura(tipo):
            def hacer(real, muertes):
                def ejecutar(efecto, *args):
                    if efecto.tipo == tipo and not muertes:
                        muertes.append(tipo)
                        raise Muerte()
                    return real(efecto, *args)
                return ejecutar
            return self.una_vez(efectos, "ejecutar", hacer)
        self.converge(envoltura)

    def test_tras_ejecutar_antes_de_registrar(self):
        def envoltura(tipo):
            def hacer(real, muertes):
                def ejecutar(efecto, *args):
                    resultado = real(efecto, *args)
                    if efecto.tipo == tipo and not muertes:
                        muertes.append(tipo)
                        raise Muerte()
                    return resultado
                return ejecutar
            return self.una_vez(efectos, "ejecutar", hacer)
        self.converge(envoltura)

    def test_dentro_de_la_transaccion_que_registra(self):
        def envoltura(tipo):
            def hacer(real, muertes):
                def cerrar(ledger, repo, ahora, clave_efecto, estado_outbox, tipo_evento, payload):
                    real(ledger, repo, ahora, clave_efecto, estado_outbox, tipo_evento, payload)
                    if estado_outbox == "hecho" and payload["tipo"] == tipo and not muertes:
                        muertes.append(tipo)
                        raise Muerte()
                return cerrar
            return self.una_vez(Ledger, "_cerrar", hacer)
        self.converge(envoltura)

    def test_durante_promover(self):
        """Tras el push (main == C al recuperar) y antes del push (main == B)."""
        for despues in (True, False):
            def envoltura(tipo, despues=despues):
                @contextmanager
                def inyectar():
                    muertes = []
                    real = subprocess.run

                    def morir(args, **kwargs):
                        a_main = "push" in args and args[-1].endswith(":refs/heads/main")
                        if a_main and not muertes and not despues:
                            muertes.append(tipo)
                            raise Muerte()
                        salida = real(args, **kwargs)
                        if a_main and not muertes:
                            muertes.append(tipo)
                            raise Muerte()
                        return salida
                    self.git.ejecutar = morir
                    try:
                        yield muertes
                    finally:
                        self.git.ejecutar = real
                return inyectar()
            with self.subTest(despues=despues):
                self.converge(envoltura, ("promover",))
                inicios = [json.loads(p) for (p,) in self.filas(
                    "SELECT payload FROM events WHERE tipo='efecto-inicio'")]
                self.assertEqual([i["recuperacion"] for i in inicios if i["tipo"] == "promover"],
                                 [False, True])


GH_FALSO = r'''
import json, re, subprocess, sys
datos = json.load(open({datos!r}))
args = sys.argv[1:]
assert args[:3] == ["api", "--method", "GET"], args
ruta = args[3]
origen = datos["origen"]
main = subprocess.run(["git", "--git-dir", origen, "rev-parse", "refs/heads/main"],
                      capture_output=True, text=True, check=True).stdout.strip()
if ruta.endswith("/git/ref/heads/main"):
    salida = {{"object": {{"sha": main}}}}
elif "/check-runs?" in ruta:
    salida = [{{"check_runs": [{{"id": 1, "name": "lint", "status": "completed", "conclusion": "success"}}]}}]
elif "/statuses?" in ruta:
    salida = [[]]
else:
    m = re.fullmatch(r"repos/" + datos["repo"] + r"/pulls/(\d+)(/files\?per_page=100)?", ruta)
    head = datos["heads"][m[1]]
    if m[2]:
        salida = [[{{"filename": "pr-" + m[1]}}]]
    else:
        mergeado = subprocess.run(["git", "--git-dir", origen, "merge-base", "--is-ancestor",
                                   head, "refs/heads/main"]).returncode == 0
        salida = {{"head": {{"sha": head}}, "base": {{"sha": main, "ref": "main"}},
                  "state": "closed" if mergeado else "open", "merged": mergeado,
                  "changed_files": 1, "labels": [], "mergeable": True}}
print(json.dumps(salida))
'''

LANZADOR_FALSO = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, {tests!r})
from test_hasito import suite_fixture
from dominio import Candidato
argv = sys.argv[1:]
with open({log!r}, "a") as log:
    log.write(json.dumps(argv) + "\n")
C, B = argv[argv.index("--suite") + 1], argv[argv.index("--base") + 1]
carpeta = Path(argv[argv.index("--evidencia") + 1])
carpeta.mkdir(parents=True, exist_ok=True)
archivo = carpeta / "resultado.json"
archivo.write_text(json.dumps(suite_fixture(Candidato(B, (), C, ""), 0, ())), encoding="utf-8")
print("SUITE veredicto=0 resultado={{}}".format(archivo))
'''


if __name__ == "__main__":
    unittest.main()
