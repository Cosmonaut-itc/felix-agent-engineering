"""U13: procesos reales, origen local, gh/lanzador falsos; sólo en desk.

No se retiran fixtures. Los fallos de muerte usan os._exit, no excepciones
que pudieran limpiar o confirmar accidentalmente una transacción.
"""
from dataclasses import replace
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import selectors
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_aterrizaje as fixtures
from test_hasito import suite_fixture
from dominio import (Admitido, Armado, Candidato, Certificado, Congelado,
                     Efecto, EsperandoCI, Estado, Promoviendo, Retenido,
                     Terminal, clave, datos, desde_datos)
from adaptadores.hasito import Hasito
from git_ops import GitTren
from ledger import Ledger, directorio_estado
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
        self.observador = GitHubGit(self.config, ejecutar=self.gh, activo=True)
        self.hasito = Hasito(self.raiz / "run.sh", self.git, self.lanzador)

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
            valor = [[]]
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
        recibo = evidencia / "resultado.json"
        if not recibo.exists():
            commit = argv[argv.index("--suite") + 1]
            base = argv[argv.index("--base") + 1]
            candidato = Candidato(base, (), commit, "d" * 40)
            recibo.write_text(json.dumps(suite_fixture(candidato, 0, ())))
            self.log("trabajos.log", argv[argv.index("--nombre") + 1])
        if self.muerte == "tras-lanzar":
            os._exit(77)
        return subprocess.CompletedProcess(argv, 0, "SUITE veredicto=0 resultado=" + str(recibo) + "\n", "")

    def tick(self, ahora=100, frontera=None):
        args = cli.parser().parse_args(["tick", "--repo", REPO])
        return cli.ejecutar(args, self.observador, ahora, self.git, self.hasito, frontera)


class ActivoTests(unittest.TestCase):
    # Reutilizar únicamente la construcción del origen, no volver a descubrir
    # todas las pruebas U2 por herencia.
    comando = fixtures.AterrizajeTests.comando
    commit = fixtures.AterrizajeTests.commit
    push = fixtures.AterrizajeTests.push
    remoto = fixtures.AterrizajeTests.remoto

    def setUp(self):
        fixtures.AterrizajeTests.setUp(self)
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
        config = {"git": str(self.fuente), "remoto": str(self.origen),
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

    def assert_aterrizado_una_vez(self):
        final = self.terminar()
        self.assertEqual(final.trenes[0].fase, Terminal("ATERRIZADO"))
        self.assertIsNone(final.pausa)
        with self.ledger() as ledger:
            filas = ledger.db.execute("SELECT payload FROM effects ORDER BY rowid").fetchall()
            self.assertEqual(len(filas), 4)
            self.assertEqual(ledger.outbox(REPO), [])
            self.assertEqual(ledger.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(ledger.db.execute("SELECT count(*) FROM trains").fetchone()[0], 1)
            for row in ledger.db.execute("SELECT payload FROM entries"):
                self.assertIn(desde_datos(json.loads(row[0])), final.entradas)
        main = self.remoto()
        self.assertNotEqual(main, self.B)
        self.assertEqual(self.comando(self.origen, "rev-parse", main + "^{tree}"),
                         self.contexto.git.texto("rev-parse", main + "^{tree}"))
        for _, head in self.miembros:
            self.comando(self.origen, "merge-base", "--is-ancestor", head, main)
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
                             for tipo in ("devolver", "caduca", "avisar-caducidad", "feedback-futuro"))
            ledger.guardar_paso(estado, feedback, 10)
            ledger.guardar_paso(estado, feedback, 11)
        final = self.terminar()
        self.assertEqual(final.trenes[0].fase, Terminal("ATERRIZADO"))
        with self.ledger() as ledger:
            pendientes = ledger.outbox(REPO)
            self.assertEqual(len(pendientes), 4)
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
        self.contexto.observador.ci = lambda repo, sha: "pendiente" if sha == candidato.C else original(repo, sha)
        for n in range(3):
            self.contexto.tick(1000 + n)
        self.assertEqual(self.lineas("trabajos.log"), [])
        self.assertIsInstance(self.estado().trenes[0].fase, EsperandoCI)
        self.contexto.observador.ci = original
        self.assert_aterrizado_una_vez()
        self.assertTrue(any("/commits/" + candidato.C + "/check-runs" in p
                            for p in self.lineas("github.log")))

    def test_error_github_no_guarda_paso_parcial_y_recupera(self):
        self.hasta_fase(EsperandoCI)
        with self.ledger() as ledger:
            antes = ledger.ultimos_eventos(REPO)
        (self.raiz / "fallo-ci").write_text("fallar")
        with self.assertRaises(subprocess.CalledProcessError):
            self.contexto.tick(500)
        with self.ledger() as ledger:
            self.assertEqual(ledger.ultimos_eventos(REPO), antes)
        (self.raiz / "fallo-ci").rename(self.raiz / "fallo-ci-retirado")
        self.assert_aterrizado_una_vez()

    def test_error_git_queda_fallido_y_reintenta(self):
        original = self.contexto.git.ejecutar
        def sin_transporte(argv, **kwargs):
            if "fetch" in argv:
                raise subprocess.TimeoutExpired(argv, 1)
            return original(argv, **kwargs)
        self.contexto.git.ejecutar = sin_transporte
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
