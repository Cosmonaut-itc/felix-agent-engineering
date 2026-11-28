"""U13: procesos reales, origen local, gh/lanzador falsos; sólo en host de pruebas.

Los fallos de muerte usan os._exit, no excepciones
que pudieran limpiar o confirmar accidentalmente una transacción.
"""
from dataclasses import replace
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import selectors
import shlex
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_aterrizaje as fixtures
from test_hasito import F, repeticion_fixture, suite_fixture
from dominio import (Armado, Candidato, Certificado, Congelado,
                     Corriendo, Efecto, Entrada, EsperandoCI, Promoviendo, Retenido,
                     Terminal, clave, datos, desde_datos)
from adaptadores.hasito import Hasito
from coordinadores import ejecutar_tick
from git_ops import GitTren
from feedback import EscritorGitHub
from ledger import Ledger
from observador import GitHubGit

RAIZ = Path(__file__).resolve().parents[1]
REPO = "example/project"
loader = importlib.machinery.SourceFileLoader("cli_activo", str(RAIZ / "pao-tren"))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)


class Contexto:
    """Transporte git real; GitHub de sólo lectura y coordinador idempotente falsos."""
    def __init__(self, ruta, muerte=""):
        self.ruta = Path(ruta)
        self.config = json.loads(self.ruta.read_text())
        self.raiz = self.ruta.parent
        self.muerte = muerte
        self.git = GitTren(self.config["remoto"], ejecutar=self.transporte)
        # Como pao-tren: la admisión activa lee el bare del ejecutor.
        self.observador = GitHubGit(self.config, ejecutar=self.gh, activo=True,
                                    almacen=self.git)
        self.hasito = Hasito(self.raiz / "run.sh", self.git, self.lanzador)
        self.escritor = EscritorGitHub(self.feedback_gh)

    def log(self, nombre, texto):
        with (self.raiz / nombre).open("a") as archivo:
            archivo.write(texto + "\n")

    def git_texto(self, ruta, *args):
        return subprocess.run(["git", "-C", str(ruta), *args], check=True,
                              capture_output=True, text=True, timeout=30).stdout.strip()

    def gh(self, argv, **kwargs):
        if argv[0] == "git":
            return subprocess.run(argv, **kwargs)
        if argv[:4] != ["gh", "api", "--method", "GET"]:
            raise AssertionError("GitHub falso sólo admite lecturas")
        endpoint = argv[4]
        self.log("github.log", endpoint)
        main = self.git_texto(self.config["remoto"], "rev-parse", "main")
        if "/git/ref/heads/main" in endpoint:
            valor = {"object": {"sha": main}}
        elif "/check-runs?" in endpoint:
            fallo = self.raiz / "fallo-ci"
            if fallo.exists():
                raise subprocess.CalledProcessError(1, argv, stderr="dato privado")
            pendiente = (self.raiz / "ci-pendiente").exists()
            valor = [{"check_runs": [{"id": 1, "name": "check", "status":
                "in_progress" if pendiente else "completed", "conclusion":
                None if pendiente else "success"}]}]
        elif "/statuses?" in endpoint:
            head = endpoint.split("/")[4]
            posts = self.raiz / "posts.log"
            publicados = [json.loads(s) for s in posts.read_text().splitlines()] if posts.exists() else []
            valor = [[p["payload"] for p in reversed(publicados)
                      if p["endpoint"].endswith("/statuses/" + head)]]
        elif "/files?" in endpoint:
            valor = [[{"filename": "src/producto.py"}]]
        else:
            pr = int(endpoint.rsplit("/", 1)[-1])
            head = dict(self.config["miembros"])[str(pr)]
            cambio = self.raiz / "head-cambiado"
            if cambio.exists() and pr == 1:
                head = "a" * 40
            merged = self.git_texto(self.config["remoto"], "merge-base", head, main) == head
            valor = {"head": {"sha": head}, "base": {"ref": "main", "sha": main},
                     "state": "closed" if merged else "open", "merged": merged,
                     "changed_files": 1, "labels": [], "mergeable": True}
            if (self.raiz / ("pr-cerrado-" + str(pr))).exists():
                valor["state"] = "closed"
        if "-i" in argv:
            pagina = valor[0] if isinstance(valor, list) else valor
            return subprocess.CompletedProcess(argv, 0, "HTTP/2.0 200 OK\n\n" + json.dumps(pagina), "")
        return subprocess.CompletedProcess(argv, 0, json.dumps(valor), "")

    def feedback_gh(self, argv, **kwargs):
        """Escritor falso independiente; persiste el POST antes de retornar."""
        metodo, endpoint = argv[3:5]
        ruta = self.raiz / "comentarios.json"
        comentarios = json.loads(ruta.read_text()) if ruta.exists() else {}
        if metodo == "GET":
            if "/comments?" in endpoint:
                pr = endpoint.split("/")[4]
                todos = comentarios.get(pr, [])
                valor = [todos[n:n + 100] for n in range(0, len(todos), 100)] or [[]]
                self.log("feedback-lecturas.log", json.dumps(argv))
                return subprocess.CompletedProcess(argv, 0, json.dumps(valor), "")
            return self.gh(argv, **kwargs)
        if metodo != "POST" or argv[5:] != ["--input", "-"]:
            raise AssertionError("escritura inesperada")
        payload = json.loads(kwargs["input"])
        self.log("posts.log", json.dumps({"endpoint": endpoint, "payload": payload}))
        fallo = self.raiz / "fallo-post"
        if fallo.exists() and fallo.read_text() in endpoint:
            raise subprocess.CalledProcessError(1, argv, stderr="dato privado")
        if "/comments" in endpoint:
            pr = endpoint.split("/")[4]
            lista = comentarios.setdefault(pr, [])
            valor = {"body": payload["body"], "html_url":
                     "https://github.com/" + REPO + "/pull/" + pr +
                     "#issuecomment-" + str(1 + len(lista))}
            lista.append(valor)
            ruta.write_text(json.dumps(comentarios))
        else:
            valor = {"state": payload["state"]}
        return subprocess.CompletedProcess(argv, 0, json.dumps(valor), "")

    def transporte(self, argv, **kwargs):
        main_push = "push" in argv and argv[-1].endswith(":refs/heads/main")
        if main_push and self.muerte == "antes-push":
            os._exit(77)
        if main_push and (self.raiz / "escritor").exists():
            self.git_texto(self.config["remoto"], "update-ref", "refs/heads/main",
                           (self.raiz / "escritor").read_text())
        if main_push and (self.raiz / "rechazar-push").exists():
            return subprocess.CompletedProcess(argv, 1, "", "rechazado")
        resultado = subprocess.run(argv, **kwargs)
        if "push" in argv:
            self.log("pushes.log", argv[-1])
        if main_push and self.muerte == "durante-promover":
            os._exit(77)
        return resultado

    def lanzador(self, argv, **kwargs):
        # El coordinador del lanzador se identifica por el mismo nombre y
        # evidencia; un reenganche repite el argv pero no crea otro trabajo.
        evidencia = Path(argv[argv.index("--evidencia") + 1])
        evidencia.mkdir(parents=True, exist_ok=True)
        rep = "--repetir" in argv
        reanudando = "--reanudar" in argv
        recibo = evidencia / ("resultado-reanudado.json" if reanudando else "resultado.json")
        if not recibo.exists():
            commit = argv[argv.index("--sha" if rep else "--suite") + 1]
            # Una segunda generación puede correr sobre otro main.
            base = argv[argv.index("--base") + 1] if "--base" in argv else self.config["base"]
            head_causal = dict(self.config["miembros"])["2"]
            contiene = self.git.git("merge-base", "--is-ancestor", head_causal, commit).returncode == 0
            rojo = (self.raiz / "producto-rojo").exists() and contiene
            if rep:
                origen = Path(argv[argv.index("--repetir") + 1])
                fuente = json.loads(origen.read_text())
                candidato = Candidato(base, (), fuente["sha"], "d" * 40)
                valores = ("falla", "falla") if rojo else ("pasa", "pasa")
                resultado = repeticion_fixture(origen, commit, candidato, F, valores)
            else:
                candidato = Candidato(base, (), commit, "d" * 40)
                codigo = 75 if (self.raiz / "suite-75").exists() and not reanudando else 1 if rojo else 0
                resultado = suite_fixture(candidato, codigo, (F,) if rojo else ())
                if codigo == 75:
                    resultado["reanudar"] = shlex.join(argv + ["--reanudar"])
            recibo.write_text(json.dumps(resultado))
            self.log("trabajos.log", argv[argv.index("--nombre") + 1])
        if self.muerte == "tras-lanzar":
            os._exit(77)
        codigo = json.loads(recibo.read_text())["veredicto"]
        etiqueta, campo = ("REPETICION", "repeticion") if rep else ("SUITE", "resultado")
        return subprocess.CompletedProcess(argv, codigo,
            "{} veredicto={} {}={}\n".format(etiqueta, codigo, campo, recibo), "")

    def tick(self, ahora=100, frontera=None):
        args = cli.parser().parse_args(["tick", "--repo", REPO])
        return ejecutar_tick(cli.ejecutar, args, self.observador, ahora, self.git,
                             adaptador=self.hasito, frontera=frontera, escritor=self.escritor)


class ActivoTests(unittest.TestCase):
    # Reutilizar únicamente la construcción del origen, no volver a descubrir
    # todas las pruebas U2 por herencia.
    comando = fixtures.AterrizajeTests.comando
    commit = fixtures.AterrizajeTests.commit
    push = fixtures.AterrizajeTests.push
    remoto = fixtures.AterrizajeTests.remoto

    def setUp(self, preparar_git=None):
        fixtures.AterrizajeTests.setUp(self)
        if preparar_git is not None:
            preparar_git()
        self.env_extra = patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(self.raiz / "config"),
            "XDG_CACHE_HOME": str(self.raiz / "cache"),
            "XDG_DATA_HOME": str(self.raiz / "data")})
        self.env_extra.start()
        self.addCleanup(self.env_extra.stop)
        self.inscritos = self.raiz / "config/pao-tren/inscritos"
        self.inscritos.parent.mkdir(parents=True)
        self.inscritos.write_text(REPO + " activo\n")
        self.ruta_contexto = self.raiz / "contexto.json"
        config = {"git": str(self.fuente), "remoto": str(self.origen), "base": self.B,
                  "miembros": [[str(pr), head] for pr, head in self.miembros],
                  "sensibles": {"ci": [".github/*"], "gobierno": ["AGENTS.md"],
                                "lockfiles": ["*lock*"]}}
        self.ruta_contexto.write_text(json.dumps(config))
        self.contexto = Contexto(self.ruta_contexto)
        with self.ledger() as ledger:
            for pr, head in self.miembros:
                apto = self.raiz / ("apto-{}.json".format(pr))
                dirigida = self.raiz / ("dirigida-{}.json".format(pr))
                apto.write_text(json.dumps({"head": head, "veredicto": "APTO", "hallazgos_abiertos": 0}))
                dirigida.write_text(json.dumps({"head": head, "resultado": "limpio"}))
                from dominio import Entrega
                ledger.entregar(Entrega(REPO, pr, head, str(apto), str(dirigida)), pr)

    def ledger(self):
        from contextlib import closing
        return closing(Ledger())

    def estado(self):
        with self.ledger() as ledger:
            return ledger.estado(REPO)

    def terminar(self):
        for n in range(15):
            self.contexto.tick(1000 + n * 120)
            estado = self.estado()
            if estado.pausa or estado.trenes and isinstance(estado.trenes[0].fase, Terminal):
                return estado
        self.fail("el tren no converge: " + str(datos(estado)))

    def hasta_fase(self, clase):
        for n in range(15):
            self.contexto.tick(100 + n * 120)
            estado = self.estado()
            if estado.trenes and isinstance(estado.trenes[0].fase, clase):
                return estado
        self.fail("fase no alcanzada: " + clase.__name__)

    def lineas(self, nombre):
        ruta = self.raiz / nombre
        return ruta.read_text().splitlines() if ruta.exists() else []

    def assert_aterrizado_una_vez(self, verificar_pushes=True):
        final = self.terminar()
        self.assertEqual(final.trenes[0].fase, Terminal("ATERRIZADO"))
        self.assertIsNone(final.pausa)
        with self.ledger() as ledger:
            filas = ledger.db.execute("SELECT payload FROM effects ORDER BY rowid").fetchall()
            self.assertEqual(len(filas), 10)  # cuatro de trabajo y seis de status
            self.assertEqual(ledger.outbox(REPO), [])
            self.assertEqual(ledger.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(ledger.db.execute("SELECT count(*) FROM trains").fetchone()[0], 1)
            for row in ledger.db.execute("SELECT payload FROM entries"):
                custodia = desde_datos(json.loads(row[0]))
                self.assertEqual(custodia, Entrada(custodia.entrega, custodia.recibida))
        main = self.remoto()
        self.assertNotEqual(main, self.B)
        self.assertEqual(self.comando(self.origen, "rev-parse", main + "^{tree}"),
                         self.contexto.git.texto("rev-parse", main + "^{tree}"))
        for _, head in self.miembros:
            self.comando(self.origen, "merge-base", "--is-ancestor", head, main)
        if verificar_pushes:
            pushes = self.lineas("pushes.log")
            self.assertEqual(sum(p.endswith(":refs/heads/main") for p in pushes), 1, pushes)
            self.assertEqual(sum(":refs/heads/tren/" in p for p in pushes), 1, pushes)
        self.assertEqual(len(self.lineas("trabajos.log")), 1)

    def hijo(self, codigo, *args, **kwargs):
        guion = "import sys\nsys.path[:0] = sys.argv[1:3]\n" + codigo
        return subprocess.Popen([sys.executable, "-I", "-c", guion,
                                 str(RAIZ), str(RAIZ / "tests"), str(self.ruta_contexto), *args],
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)

    def test_lote_de_punta_a_punta(self):
        self.assert_aterrizado_una_vez()
        # Cerrar los PR por nuestro push no produce caducidades posteriores.
        self.contexto.tick(5000)
        self.assertEqual(self.estado().trenes[0].fase, Terminal("ATERRIZADO"))
        self.assertEqual(len(self.lineas("pushes.log")), 2)

    def test_cli_real_carga_config_y_adaptador_sin_inyeccion(self):
        # El CLI usa transporte real, sin nuestro logger: verificar escrituras
        # por el reflog del origen, activado sólo después de crear la fixture.
        self.comando(self.origen, "config", "core.logAllRefUpdates", "true")
        ejecutables = self.raiz / "bin"
        ejecutables.mkdir()
        lanzador = self.raiz / "run.sh"
        config = self.contexto.config
        config["lanzador_hasito"] = str(lanzador)
        self.ruta_contexto.write_text(json.dumps(config))
        helper = self.raiz / "frontera.py"
        helper.write_text("import sys\nsys.path[:0] = " + repr([str(RAIZ), str(RAIZ / "tests")]) + "\n"
            "from test_activo import Contexto\nc = Contexto(" + repr(str(self.ruta_contexto)) + ")\n"
            "if sys.argv[1] == 'gh':\n    kwargs = {'input': sys.stdin.read()} if '--input' in sys.argv else {}\n    r = c.feedback_gh(['gh'] + sys.argv[2:], **kwargs)\n"
            "else:\n    r = c.lanzador(['bash', c.config['lanzador_hasito']] + sys.argv[2:])\n"
            "print(r.stdout, end='')\nsys.exit(r.returncode)\n")
        for ruta, modo in ((ejecutables / "gh", "gh"), (lanzador, "suite")):
            ruta.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, "-I", str(helper), modo]) + ' "$@"\n')
            ruta.chmod(0o700)
        privada = self.raiz / "config.json"
        privada.write_text(json.dumps({"repos": {REPO: config}}))
        env = dict(os.environ, PATH=str(ejecutables) + os.pathsep + os.environ["PATH"])
        for n in range(10):
            proc = subprocess.run([sys.executable, "-I", str(RAIZ / "pao-tren"),
                "tick", "--repo", REPO, "--config", str(privada)],
                env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["modo"], "activo")
            trenes = payload["estado"]["trenes"]
            if trenes and trenes[0]["fase"] == datos(Terminal("ATERRIZADO")):
                break
        else:
            self.fail("el CLI real no aterrizó")
        self.assert_aterrizado_una_vez(verificar_pushes=False)
        self.assertEqual(len(self.comando(self.origen, "reflog", "show", "--format=%H", "main").splitlines()), 1)
        rama = self.comando(self.origen, "for-each-ref", "--format=%(refname)", "refs/heads/tren/")
        self.assertEqual(len(rama.splitlines()), 1)
        self.assertEqual(len(self.comando(self.origen, "reflog", "show", "--format=%H", rama).splitlines()), 1)

    def test_puerta_no_inscrito_sombra_activo_y_seco(self):
        args = cli.parser().parse_args(["tick", "--repo", REPO])
        for texto, modo in (("", "no-inscrito"), (REPO + " sombra\n", "sombra")):
            self.inscritos.write_text(texto)
            codigo, payload = cli.ejecutar(args)
            self.assertEqual(codigo, 2)
            self.assertEqual(payload["inscripcion"], modo)
        self.inscritos.write_text(REPO + " activo\n")
        self.assertEqual(self.contexto.tick()[0], 0)
        self.inscritos.write_text(REPO + " sombra\n")
        antes = self.estado()
        args.seco = True
        self.assertEqual(cli.ejecutar(args, self.contexto.observador, 200)[0], 0)
        self.assertEqual(self.estado(), antes)

    def test_activar_conserva_muestras_de_admision_sin_importar_trenes_simulados(self):
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.raiz / "state-nuevo")}):
            self.inscritos.write_text(REPO + " sombra\n")
            pr, head = self.miembros[0]
            args = cli.parser().parse_args(["entregar", "--repo", REPO, "--pr", str(pr),
                "--head", head, "--apto", str(self.raiz / "apto-1.json"),
                "--dirigida", str(self.raiz / "dirigida-1.json")])
            self.assertEqual(cli.ejecutar(args, self.contexto.observador, 100)[0], 0)
            seco = cli.parser().parse_args(["tick", "--repo", REPO, "--seco"])
            sombra = cli.ejecutar(seco, self.contexto.observador, 3700)[1]["plan"]["estado"]
            self.assertEqual(sombra["trenes"][0]["fase"], datos(Terminal("SIMULADO")))
            self.inscritos.write_text(REPO + " activo\n")
            self.contexto.tick(3800)
            estado = self.estado()
            self.assertEqual(estado.entradas[0].admitida, 100)
            self.assertEqual(len(estado.trenes), 1)
            self.assertIsInstance(estado.trenes[0].fase, Congelado)
            self.assertEqual(self.terminar().trenes[0].fase, Terminal("ATERRIZADO"))

    def test_75_persistido_espera_y_reanuda_suite(self):
        (self.raiz / "suite-75").write_text("75")
        self.hasta_fase(Corriendo)
        fase = self.estado().trenes[0].fase
        self.assertIsNotNone(fase.esperar_hasta)
        self.assertEqual(len(self.lineas("trabajos.log")), 1)
        self.contexto.tick(fase.esperar_hasta - 1)
        self.assertEqual(len(self.lineas("trabajos.log")), 1)
        self.contexto.tick(fase.esperar_hasta)
        self.assertEqual(len(self.lineas("trabajos.log")), 2)
        final = self.terminar()
        self.assertEqual(final.trenes[0].fase, Terminal("ATERRIZADO"))
        with self.ledger() as ledger:
            self.assertEqual(ledger.outbox(REPO), [])
            efectos = [json.loads(r[0])["tipo"] for r in ledger.db.execute("SELECT payload FROM effects")]
        self.assertEqual(efectos.count("reanudar-suite"), 1)

    def test_rojo_atribuye_repite_subconjuntos_y_aterriza_sobrevivientes(self):
        (self.raiz / "producto-rojo").write_text("causal: segundo PR")
        self.contexto.config["costo_suite_sana_s"] = 10000
        for n in range(30):
            self.contexto.tick(1000 + 120 * n)
            final = self.estado()
            self.assertIsNone(final.pausa, datos(final.pausa))
            if len(final.trenes) == 2 and final.trenes[-1].fase == Terminal("ATERRIZADO"):
                break
        else:
            self.fail("el diagnóstico no convergió: " + str(datos(final)))
        self.assertEqual(final.trenes[0].fase, Terminal("DEVUELTO"))
        self.assertEqual(final.entradas[1].terminal, Terminal("DEVUELTO"))
        with self.ledger() as ledger:
            self.assertEqual(ledger.outbox(REPO), [])
            fila = ledger.db.execute("SELECT e.payload, o.estado FROM effects e JOIN outbox o "
                "ON e.clave=o.clave WHERE e.payload LIKE '%devolver%'").fetchone()
            efecto = desde_datos(json.loads(fila["payload"]))
            self.assertEqual((efecto.tipo, fila["estado"]), ("devolver", "completo"))
            payload = json.loads(efecto.datos[2])
            self.assertEqual(payload["pr"], 2)
            self.assertEqual(payload["fallos"][0]["conjunto_causal"], [2])
            self.assertIn("--worktree", payload["fallos"][0]["comando"])
            tipos = [json.loads(r[0])["tipo"] for r in ledger.db.execute("SELECT payload FROM effects")]
        self.assertEqual(tipos.count("repetir"), 5)
        self.assertEqual(tipos.count("armar-subconjunto"), 3)
        main = self.remoto()
        self.assertEqual(sum(p.endswith(":refs/heads/main") for p in self.lineas("pushes.log")), 1)
        for indice in (0, 2):
            self.comando(self.origen, "merge-base", "--is-ancestor", self.miembros[indice][1], main)
        self.assertNotEqual(self.comando(self.origen, "merge-base", self.miembros[1][1], main),
                            self.miembros[1][1])

    def test_estado_muestra_fila_fase_pausa_y_outbox(self):
        self.hasta_fase(Congelado)
        args = cli.parser().parse_args(["estado", "--repo", REPO, "--juicio"])
        codigo, payload = cli.ejecutar(args)
        self.assertEqual(codigo, 0)
        self.assertEqual(len(payload["estado"]["entradas"]), 3)
        self.assertEqual(payload["estado"]["trenes"][0]["fase"]["_tipo"], "Congelado")
        self.assertIsNone(payload["estado"]["pausa"])
        self.assertEqual(payload["outbox"][0]["estado"], "resultado")

    def test_feedback_pendiente_no_bloquea_otro_efecto(self):
        with self.ledger() as ledger:
            estado = ledger.estado(REPO)
            feedback = tuple(Efecto(clave(tipo), tipo, ("recibo",))
                             for tipo in ("caduca", "feedback-futuro"))
            ledger.guardar_paso(estado, feedback, 10)
            ledger.guardar_paso(estado, feedback, 11)
        final = self.terminar()
        self.assertEqual(final.trenes[0].fase, Terminal("ATERRIZADO"))
        with self.ledger() as ledger:
            pendientes = ledger.outbox(REPO)
            self.assertEqual(len(pendientes), 2)
            self.assertTrue(all(f["estado"] == "pendiente" for f in pendientes))
        args = cli.parser().parse_args(["estado", "--repo", REPO])
        self.assertTrue(all(not f["ejecutor"] for f in cli.ejecutar(args)[1]["outbox"]))

    def test_resultado_durable_no_reutiliza_admision_vieja(self):
        self.hasta_fase(Congelado)  # El IO terminó, falta consumir el candidato.
        # No puede leer evidencia de uno: conserva el candidato completado,
        # retiene la admisión y no solicita CI hasta recuperar evidencia.
        (self.raiz / "apto-1.json").write_text("{}")
        self.contexto.tick(300)
        estado = self.estado()
        self.assertIsInstance(estado.trenes[0].fase, Armado)
        self.assertIsInstance(estado.entradas[0].admision, Retenido)
        self.assertEqual(self.lineas("pushes.log"), [])
        head = self.miembros[0][1]
        (self.raiz / "apto-1.json").write_text(json.dumps({"head": head, "veredicto": "APTO", "hallazgos_abiertos": 0}))
        self.assert_aterrizado_una_vez()

    def test_ci_se_lee_sobre_c_y_no_se_inventa(self):
        self.hasta_fase(EsperandoCI)
        candidato = self.estado().trenes[0].fase.candidato
        # Retener sólo el CI de C, sin invalidar los heads de admisión.
        original = self.contexto.observador.ci
        self.contexto.observador.ci = lambda repo, sha: type(original(repo, sha))("pendiente") if sha == candidato.C else original(repo, sha)
        for n in range(3):
            self.contexto.tick(1000 + n)
        self.assertEqual(self.lineas("trabajos.log"), [])
        self.assertIsInstance(self.estado().trenes[0].fase, EsperandoCI)
        self.contexto.observador.ci = original
        self.assert_aterrizado_una_vez()
        self.assertTrue(any("/commits/" + candidato.C + "/check-runs" in p
                            for p in self.lineas("github.log")))

    def test_publicacion_pendiente_o_interrumpida_readmite_antes_de_publicar(self):
        self.hasta_fase(Armado)
        class Muerte(Exception):
            pass
        def detener(nombre, efecto):
            if nombre == "paso-guardado":
                raise Muerte()
        with self.assertRaises(Muerte):
            self.contexto.tick(500, detener)
        (self.raiz / "apto-1.json").write_text("{}")
        for interrumpido in (False, True):
            if interrumpido:
                with self.ledger() as ledger:
                    fila = ledger.outbox(REPO)[0]
                    efecto = desde_datos(json.loads(fila["payload"]))
                    self.assertEqual(efecto.tipo, "solicitar-ci")
                    ledger.iniciar_efecto(REPO, efecto, 600)
            self.contexto.tick(700)
            self.assertEqual(self.lineas("pushes.log"), [])
            self.assertIsInstance(self.estado().entradas[0].admision, Retenido)
        (self.raiz / "apto-1.json").write_text(json.dumps({"head": self.miembros[0][1],
            "veredicto": "APTO", "hallazgos_abiertos": 0}))
        self.assert_aterrizado_una_vez()

    def test_error_github_en_ci_es_pendiente_y_recupera(self):
        self.hasta_fase(EsperandoCI)
        (self.raiz / "fallo-ci").write_text("fallar")
        for n in range(3):
            self.assertEqual(self.contexto.tick(500 + n)[0], 0)
        # Sin CI legible no hay verde inventado ni suite lanzada.
        self.assertIsInstance(self.estado().trenes[0].fase, EsperandoCI)
        self.assertEqual(self.lineas("trabajos.log"), [])
        (self.raiz / "fallo-ci").rename(self.raiz / "fallo-ci-retirado")
        self.assert_aterrizado_una_vez()

    def test_error_git_queda_fallido_y_reintenta(self):
        original = self.contexto.git.ejecutar
        def sin_transporte(argv, **kwargs):
            if "fetch" in argv:
                raise subprocess.TimeoutExpired(argv, 1)
            return original(argv, **kwargs)
        self.contexto.git.ejecutar = sin_transporte
        # Falla sólo el armado: la admisión trae los objetos con el transporte
        # sano al mismo bare (U17 cubre la falla al observar).
        self.contexto.observador.almacen = GitTren(self.contexto.config["remoto"],
                                                   ejecutar=original)
        self.contexto.tick()
        with self.ledger() as ledger:
            fila = ledger.outbox(REPO)[0]
            self.assertEqual(fila["estado"], "fallido")
            self.assertEqual(json.loads(fila["resultado"])["error"]["codigo"], "git-no-disponible")
        self.assertIsInstance(self.estado().trenes[0].fase, Congelado)
        self.contexto.git.ejecutar = original
        self.assert_aterrizado_una_vez()

    def test_main_movido_durante_cas_pausa(self):
        self.hasta_fase(Certificado)
        ajeno = self.commit({"comun": "base\n", "externo": "cambio\n"}, (self.B,))
        self.push(ajeno, "refs/heads/ajeno")
        (self.raiz / "escritor").write_text(ajeno)
        final = self.terminar()
        self.assertEqual(final.pausa.motivo, "escritor-externo")
        self.assertEqual(final.pausa.main, ajeno)
        self.assertEqual(final.trenes[0].fase, Terminal("REARMAR"))
        self.assertEqual(self.remoto(), ajeno)
        self.assertTrue(all(e.tren is None for e in final.entradas))
        args = cli.parser().parse_args(["estado", "--repo", REPO, "--juicio"])
        self.assertEqual(cli.ejecutar(args)[1]["estado"]["pausa"]["motivo"], "escritor-externo")

    def test_main_en_b_reintenta_con_claves_por_intento_y_pausa_al_limite(self):
        self.hasta_fase(Certificado)
        (self.raiz / "rechazar-push").write_text("rechazar")
        final = self.terminar()
        self.assertEqual(final.pausa.motivo, "promocion-rechazada")
        self.assertEqual(final.trenes[0].fase.intentos, 3)
        self.assertEqual(self.remoto(), self.B)
        with self.ledger() as ledger:
            efectos = [json.loads(r[0]) for r in ledger.db.execute("SELECT payload FROM effects")]
        self.assertEqual(len([e for e in efectos if e["tipo"] == "promover"]), 3)

    def test_main_se_relee_antes_de_consumir_ack_guardado(self):
        self.hasta_fase(Promoviendo)  # Push y resultado guardados, aún sin ACK.
        aterrizado = self.remoto()
        self.assertNotEqual(aterrizado, self.B)
        ajeno = self.commit({"comun": "base\n", "externo": "otro main\n"}, (self.B,))
        self.push(ajeno, "refs/heads/ajeno")
        self.comando(self.origen, "update-ref", "refs/heads/main", ajeno)
        self.contexto.tick(3000)
        final = self.estado()
        self.assertEqual(final.pausa.motivo, "escritor-externo")
        self.assertEqual(final.pausa.main, ajeno)
        self.assertEqual(final.trenes[0].fase, Terminal("REARMAR"))
        self.assertEqual(len(self.lineas("pushes.log")), 2)

    def test_ack_guardado_con_lectura_main_incierta_no_se_confirma(self):
        self.hasta_fase(Promoviendo)
        original = self.contexto.git.ejecutar
        def sin_lectura(argv, **kwargs):
            if "ls-remote" in argv:
                raise subprocess.TimeoutExpired(argv, 1)
            return original(argv, **kwargs)
        self.contexto.git.ejecutar = sin_lectura
        self.contexto.tick(3000)
        self.assertIsInstance(self.estado().trenes[0].fase, Promoviendo)
        with self.ledger() as ledger:
            self.assertEqual(ledger.outbox(REPO)[0]["estado"], "fallido")
        self.contexto.git.ejecutar = original
        self.assert_aterrizado_una_vez()

    def test_paso_y_ack_hacen_rollback_juntos(self):
        self.hasta_fase(Congelado)
        antes = self.estado()
        with self.ledger() as ledger:
            previo = ledger.outbox(REPO)
            ledger.db.execute("CREATE TRIGGER fallar BEFORE INSERT ON events "
                              "WHEN NEW.tipo='operativo' BEGIN SELECT RAISE(ABORT, 'inyectado'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.contexto.tick(300)
        self.assertEqual(self.estado(), antes)
        with self.ledger() as ledger:
            self.assertEqual(ledger.outbox(REPO), previo)
            self.assertEqual(ledger.db.execute("SELECT payload FROM trains").fetchone()[0],
                             json.dumps(datos(antes.trenes[0]), sort_keys=True, ensure_ascii=True))

    def test_dos_ticks_serializados_con_io_bajo_candado(self):
        codigo = """
from test_activo import Contexto
c = Contexto(sys.argv[3])
def frontera(nombre, efecto):
    if nombre == 'antes-ejecutar':
        print('candado', flush=True)
        sys.stdin.readline()
_, resultado = c.tick(frontera=frontera)
print(resultado['estado']['trenes'][0]['fase']['_tipo'], flush=True)
"""
        uno = self.hijo(codigo, stdin=subprocess.PIPE)
        self.addCleanup(lambda: uno.poll() is None and uno.kill())
        with selectors.DefaultSelector() as selector:
            selector.register(uno.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(30), "el primer tick no tomó el candado")
        self.assertEqual(uno.stdout.readline().strip(), "candado")
        estado_cli = subprocess.run([sys.executable, "-I", str(RAIZ / "pao-tren"),
            "estado", "--repo", REPO], capture_output=True, text=True, timeout=10)
        self.assertEqual(estado_cli.returncode, 0, estado_cli.stderr)
        self.assertEqual(json.loads(estado_cli.stdout)["outbox"][0]["estado"], "ejecutando")
        dos = self.hijo("from test_activo import Contexto\nc=Contexto(sys.argv[3])\n"
                        "print(c.tick()[1]['estado']['trenes'][0]['fase']['_tipo'], flush=True)\n")
        self.addCleanup(lambda: dos.poll() is None and dos.kill())
        with self.assertRaises(subprocess.TimeoutExpired):
            dos.communicate(timeout=0.3)
        salida_uno, error_uno = uno.communicate("seguir\n", timeout=30)
        salida_dos, error_dos = dos.communicate(timeout=30)
        self.assertEqual((uno.returncode, dos.returncode), (0, 0), error_uno + error_dos)
        self.assertIn("Congelado", salida_uno)
        self.assertIn("Armado", salida_dos)
        self.assert_aterrizado_una_vez()

    def muerte_y_convergencia(self, punto, tipo):
        codigo = """
from test_activo import Contexto, REPO
from ledger import Ledger
import os
c = Contexto(sys.argv[3], sys.argv[4])
punto, tipo = sys.argv[4:6]
if punto == 'publicacion-recibo':
    reemplazar = os.replace
    def morir(origen, destino):
        if str(destino).endswith('/recibo.json'):
            os._exit(77)
        return reemplazar(origen, destino)
    os.replace = morir
def frontera(nombre, efecto):
    if nombre != punto:
        return
    if efecto is not None and efecto.tipo == tipo:
        os._exit(77)
    if nombre == 'paso-guardado':
        l = Ledger()
        pendientes = l.outbox(REPO)
        l.close()
        import json
        if any(json.loads(f['payload'])['tipo'] == tipo for f in pendientes):
            os._exit(77)
for n in range(20):
    c.tick(100 + n * 120, frontera)
sys.exit(9)
"""
        hijo = self.hijo(codigo, punto, tipo)
        salida, error = hijo.communicate(timeout=90)
        self.assertEqual(hijo.returncode, 77, salida + error)
        self.assert_aterrizado_una_vez()


def prueba_muerte(punto, tipo):
    def probar(self):
        self.muerte_y_convergencia(punto, tipo)
    return probar


for punto in ("paso-guardado", "antes-ejecutar", "efecto-ejecutado", "antes-registrar", "resultado-guardado"):
    for tipo in ("armar-candidato", "solicitar-ci", "iniciar-suite", "promover"):
        setattr(ActivoTests, "test_muerte_" + punto.replace("-", "_") + "_" + tipo.replace("-", "_"),
                prueba_muerte(punto, tipo))
for punto, tipo in (("antes-push", "promover"), ("durante-promover", "promover"),
                    ("tras-lanzar", "iniciar-suite"), ("publicacion-recibo", "iniciar-suite")):
    setattr(ActivoTests, "test_muerte_" + punto.replace("-", "_"), prueba_muerte(punto, tipo))


if __name__ == "__main__":
    unittest.main()
