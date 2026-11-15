"""U16: salida del CI sobre C. Origen git local, GitHub y lanzador falsos.

Sólo en el host de pruebas. Las regresiones importan nada más que nombres
que ya existían antes de U16: `remoto-u16.sh` las corre también sobre main.
"""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import test_activo as activo
import test_juicio as tj
from dominio import (Admitido, Candidato, Entrada, Entrega, EsperandoCI, Estado,
                     Observacion, Pausa, Terminal, Tren, VistaPR, datos, desde_datos, paso)
from ledger import Ledger
from observador import GitHubGit

REPO = activo.REPO
VERDE = {"id": 1, "name": "check", "status": "completed", "conclusion": "success"}
ROJO = [VERDE, {"id": 2, "name": "lint", "status": "completed", "conclusion": "failure"}]
PENDIENTE = [{"id": 1, "name": "check", "status": "in_progress", "conclusion": None}]


def envolver(contexto):
    """CI de C según `ci-c.json`; los heads de PR conservan el GitHub falso U13.

    También hace ilegible el PR de `pr-ilegible` y da al lanzador falso la B
    de cada suite (los trenes de uno aterrizan sobre un main que avanza).
    """
    original = contexto.observador.ejecutar

    def ejecutar(argv, **kwargs):
        endpoint = argv[4] if argv[:2] == ["gh", "api"] else ""
        regla = contexto.raiz / "ci-c.json"
        heads = {head for _, head in contexto.config["miembros"]}
        if "/check-runs?" in endpoint and regla.exists():
            commit = endpoint.split("/")[4]
            regla = json.loads(regla.read_text())
            if commit not in heads and (regla["modo"] == "pendiente" or (
                    regla["modo"] == "rojo-si-contiene" and contexto.git.git(
                        "merge-base", "--is-ancestor", regla["head"], commit).returncode == 0)):
                contexto.log("github.log", endpoint)
                checks = PENDIENTE if regla["modo"] == "pendiente" else ROJO
                return subprocess.CompletedProcess(argv, 0, json.dumps([{"check_runs": checks}]), "")
        ilegible = contexto.raiz / "pr-ilegible"
        if ilegible.exists() and endpoint == "repos/{}/pulls/{}".format(REPO, ilegible.read_text()):
            raise subprocess.CalledProcessError(1, argv, stderr="dato privado")
        return original(argv, **kwargs)

    contexto.observador.ejecutar = ejecutar
    lanzador = contexto.hasito.runner

    def runner(argv, **kwargs):
        if "--base" in argv:
            contexto.config["base"] = argv[argv.index("--base") + 1]
        return lanzador(argv, **kwargs)

    contexto.hasito.runner = runner
    return contexto


class ContextoU16(unittest.TestCase):
    comando = tj.JuicioTests.comando
    commit = tj.JuicioTests.commit
    push = tj.JuicioTests.push
    remoto = tj.JuicioTests.remoto
    ledger = tj.JuicioTests.ledger
    estado = tj.JuicioTests.estado
    lineas = tj.JuicioTests.lineas
    hijo = tj.JuicioTests.hijo
    cli = tj.JuicioTests.cli
    juicio = tj.JuicioTests.juicio
    posts = tj.JuicioTests.posts

    def setUp(self):
        tj.JuicioTests.setUp(self)
        envolver(self.contexto)

    def ci_de_c(self, modo, head=None):
        ruta = self.raiz / "ci-c.json"
        if modo is None:
            if ruta.exists():
                ruta.rename(self.raiz / ("ci-c-retirado-" + str(len(self.lineas("github.log")))))
            return
        ruta.write_text(json.dumps({"modo": modo, "head": head}))

    def contiene(self, head):
        return self.comando(self.origen, "merge-base", head, self.remoto()) == head

    def recibo(self, indice):
        pr, head = self.miembros[indice]
        return Entrega(REPO, pr, head, str(self.raiz / "apto-{}.json".format(pr)),
                       str(self.raiz / "dirigida-{}.json".format(pr))).recibo

    def fallos(self):
        return [p for p in self.posts("/statuses/") if p["payload"]["state"] == "failure"]

    def hasta(self, condicion, desde, ticks=80):
        for n in range(ticks):
            self.contexto.tick(desde + n * 120)
            estado = self.estado()
            if condicion(estado):
                return estado, desde + n * 120
        self.fail("no converge: " + json.dumps(datos(self.estado()))[:2000])

    def todos_terminales(self, cuantos):
        return lambda e: (len(e.trenes) == cuantos and
                          all(isinstance(t.fase, Terminal) for t in e.trenes))

    def asertar_tres_trenes_de_uno(self, final):
        self.assertIsNone(final.pausa)
        self.assertEqual([t.fase for t in final.trenes], [
            Terminal("PARTIDO"), Terminal("ATERRIZADO"), Terminal("DEVUELTO"), Terminal("ATERRIZADO")])
        self.assertEqual([t.recibos for t in final.trenes[1:]],
                         [(self.recibo(0),), (self.recibo(1),), (self.recibo(2),)])
        # Cada tren publicó su propio C y leyó su propio CI.
        pushes = self.lineas("pushes.log")
        self.assertEqual(sum(":refs/heads/tren/" in p for p in pushes), 4, pushes)
        self.assertEqual(sum(p.endswith(":refs/heads/main") for p in pushes), 2, pushes)
        self.assertTrue(self.contiene(self.miembros[0][1]))
        self.assertTrue(self.contiene(self.miembros[2][1]))
        self.assertFalse(self.contiene(self.miembros[1][1]))
        self.assertEqual(len(self.lineas("trabajos.log")), 2)
        self.assertEqual(final.entradas[1].terminal, Terminal("DEVUELTO"))
        comentarios = self.posts("/comments")
        self.assertEqual(len(comentarios), 1)
        self.assertIn("/issues/2/comments", comentarios[0]["endpoint"])
        self.assertIn("lint", comentarios[0]["payload"]["body"])
        fallos = self.fallos()
        self.assertEqual(len(fallos), 1)
        self.assertTrue(fallos[0]["endpoint"].endswith("/statuses/" + self.miembros[1][1]))
        with self.ledger() as ledger:
            self.assertEqual(ledger.outbox(REPO), [])


class RegresionesTests(ContextoU16):
    """Fallan sobre 85138706: allí un CI rojo o mudo ocupa el carril."""

    def test_tren_de_tres_con_ci_rojo_se_parte_en_trenes_de_uno(self):
        self.ci_de_c("rojo-si-contiene", self.miembros[1][1])
        partido, _ = self.hasta(lambda e: e.trenes and isinstance(e.trenes[0].fase, Terminal), 100, 15)
        self.assertEqual(partido.trenes[0].fase, Terminal("PARTIDO"))
        self.assertEqual(len(partido.trenes), 1)
        # Sin culpa, sin rojo y sin escritura en main; todos vuelven a la fila
        # con su custodia y su orden de llegada.
        self.assertEqual(self.remoto(), self.B)
        self.assertFalse(any(p.endswith(":refs/heads/main") for p in self.lineas("pushes.log")))
        self.assertEqual(self.posts("/comments"), [])
        self.assertEqual(self.fallos(), [])
        for entrada in partido.entradas:
            self.assertIsNone(entrada.tren)
            self.assertIsNone(entrada.terminal)
            self.assertIsInstance(entrada.admision, Admitido)
            self.assertEqual(entrada.admitida, 100)
        final, _ = self.hasta(self.todos_terminales(4), 3000)
        self.asertar_tres_trenes_de_uno(final)

    def test_tren_de_uno_con_ci_rojo_devuelve_una_vez_y_libera_el_carril(self):
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.raiz / "state-uno")}):
            with self.ledger() as ledger:
                for indice in (0, 2):
                    pr, head = self.miembros[indice]
                    ledger.entregar(Entrega(REPO, pr, head, str(self.raiz / "apto-{}.json".format(pr)),
                                            str(self.raiz / "dirigida-{}.json".format(pr))), pr)
            self.contexto.tick(100)
            self.assertEqual(self.estado().trenes, ())
            # El 3 queda incierto (conserva su admisión); el 1 viaja solo al vencer la ventana.
            (self.raiz / "pr-ilegible").write_text("3")
            self.contexto.tick(3700)
            self.assertEqual(self.estado().trenes[0].recibos, (self.recibo(0),))
            (self.raiz / "pr-ilegible").rename(self.raiz / "pr-ilegible-retirado")
            self.ci_de_c("rojo-si-contiene", self.miembros[0][1])
            devuelto, instante = self.hasta(
                lambda e: isinstance(e.trenes[0].fase, Terminal), 3820, 15)
            self.assertEqual(devuelto.trenes[0].fase, Terminal("DEVUELTO"))
            self.assertEqual(devuelto.entradas[0].terminal, Terminal("DEVUELTO"))
            self.assertEqual(len(devuelto.trenes), 1)
            comentarios = self.posts("/comments")
            self.assertEqual(len(comentarios), 1)
            self.assertIn("/issues/1/comments", comentarios[0]["endpoint"])
            self.assertIn("lint", comentarios[0]["payload"]["body"])
            self.assertEqual(len(self.fallos()), 1)
            self.assertTrue(self.fallos()[0]["endpoint"].endswith(self.miembros[0][1]))
            self.assertEqual(self.remoto(), self.B)
            self.assertFalse(any(p.endswith(":refs/heads/main") for p in self.lineas("pushes.log")))
            self.assertEqual(self.lineas("trabajos.log"), [])
            # Carril libre en el acto: la otra entrega forma tren en el tick siguiente.
            self.contexto.tick(instante + 1)
            siguiente = self.estado()
            self.assertEqual(len(siguiente.trenes), 2)
            self.assertEqual(siguiente.trenes[1].recibos, (self.recibo(2),))
            self.assertNotIsInstance(siguiente.trenes[1].fase, Terminal)
            final, _ = self.hasta(self.todos_terminales(2), instante + 2)
            self.assertEqual(final.trenes[1].fase, Terminal("ATERRIZADO"))
            self.assertTrue(self.contiene(self.miembros[2][1]))
            self.assertEqual(len(self.posts("/comments")), 1)
            self.assertEqual(len(self.fallos()), 1)
            with self.ledger() as ledger:
                self.assertEqual(ledger.outbox(REPO), [])

    def test_ci_sin_resultado_pausa_a_los_60_min_y_reanudar_vuelve_a_pedirlo(self):
        self.ci_de_c("pendiente")
        _, inicio = self.hasta(lambda e: e.trenes and isinstance(e.trenes[0].fase, EsperandoCI), 100, 15)
        self.contexto.tick(inicio + 3599)
        self.assertIsNone(self.estado().pausa)
        self.assertIsInstance(self.estado().trenes[0].fase, EsperandoCI)
        self.contexto.tick(inicio + 3600)
        pausado = self.estado()
        self.assertEqual(pausado.pausa, Pausa("infraestructura", detalle="ci-sin-resultado"))
        self.assertIsInstance(pausado.trenes[0].fase, EsperandoCI)
        self.assertEqual(self.lineas("trabajos.log"), [])
        self.assertEqual(self.fallos(), [])
        self.assertEqual(self.posts("/comments"), [])
        opciones = self.cli("estado", "--juicio")[1]["juicio"]["admitidas"]
        self.assertEqual({a["decision"] for a in opciones}, {"reanudar", "abortar"})
        self.juicio("reanudar")
        reanudado = inicio + 4000
        self.contexto.tick(reanudado)
        fase = self.estado().trenes[0].fase
        self.assertIsNone(self.estado().pausa)
        self.assertEqual(fase.candidato, pausado.trenes[0].fase.candidato)
        self.assertEqual(fase.desde, reanudado)
        with self.ledger() as ledger:
            tipos = [json.loads(r[0])["tipo"] for r in ledger.db.execute("SELECT payload FROM effects")]
            self.assertEqual(tipos.count("solicitar-ci"), 2)
            self.assertEqual([r for r in ledger.outbox(REPO)
                              if json.loads(r["payload"])["tipo"] == "solicitar-ci"], [])
        # El plazo empieza de nuevo: 59 min después del juicio todavía espera.
        self.contexto.tick(reanudado + 3599)
        self.assertIsNone(self.estado().pausa)
        self.ci_de_c(None)
        final, _ = self.hasta(lambda e: e.trenes[0].fase == Terminal("ATERRIZADO"), reanudado + 3600)
        self.assertIsNone(final.pausa)
        self.assertEqual(len(self.lineas("trabajos.log")), 1)
        self.assertEqual(sum(p.endswith(":refs/heads/main") for p in self.lineas("pushes.log")), 1)
        for _, head in self.miembros:
            self.assertTrue(self.contiene(head))


class RecuperacionTests(ContextoU16):
    def morir_al_guardar(self, resultado):
        codigo = """
from test_activo import Contexto, REPO
from test_u16 import envolver
from dominio import Terminal
from ledger import Ledger
import os
c = envolver(Contexto(sys.argv[3]))
def frontera(nombre, efecto):
    if nombre != 'paso-guardado':
        return
    l = Ledger()
    estado = l.estado(REPO)
    l.close()
    if any(t.fase == Terminal(sys.argv[4]) for t in estado.trenes):
        os._exit(77)
for n in range(80):
    c.tick(100 + n * 120, frontera)
sys.exit(9)
"""
        hijo = self.hijo(codigo, resultado)
        salida, error = hijo.communicate(timeout=300)
        self.assertEqual(hijo.returncode, 77, salida + error)

    def test_muerte_tras_partir_conserva_la_fila(self):
        self.ci_de_c("rojo-si-contiene", self.miembros[1][1])
        self.morir_al_guardar("PARTIDO")
        muerto = self.estado()
        self.assertEqual([t.fase for t in muerto.trenes], [Terminal("PARTIDO")])
        self.assertTrue(all(e.tren is None and e.partida == muerto.trenes[0].id
                            for e in muerto.entradas))
        final, _ = self.hasta(self.todos_terminales(4), 100000)
        self.asertar_tres_trenes_de_uno(final)

    def test_muerte_antes_de_drenar_devolver_publica_una_vez(self):
        self.ci_de_c("rojo-si-contiene", self.miembros[1][1])
        self.morir_al_guardar("DEVUELTO")
        muerto = self.estado()
        self.assertEqual(muerto.trenes[-1].fase, Terminal("DEVUELTO"))
        self.assertEqual(self.posts("/comments"), [])
        with self.ledger() as ledger:
            pendientes = [json.loads(r["payload"])["tipo"] for r in ledger.outbox(REPO)]
        self.assertIn("devolver", pendientes)
        self.assertEqual(muerto.entradas[2].tren, None)
        final, _ = self.hasta(self.todos_terminales(4), 100000)
        self.asertar_tres_trenes_de_uno(final)


class LecturaTests(unittest.TestCase):
    def observador(self, checks=(), statuses=(), activo=True, **config):
        def ejecutar(argv, **kwargs):
            valor = [{"check_runs": list(checks)}] if "/check-runs?" in argv[4] else [list(statuses)]
            return subprocess.CompletedProcess(argv, 0, json.dumps(valor), "")
        return GitHubGit(dict(config, git="/no-usado"), ejecutar=ejecutar, activo=activo)

    def run_(self, nombre, conclusion, id=1, status="completed"):
        return {"id": id, "name": nombre, "status": status, "conclusion": conclusion}

    def test_cada_conclusion_y_status_tiene_su_lectura(self):
        tabla = {"success": "verde", "neutral": "verde", "skipped": "verde",
                 "failure": "rojo", "timed_out": "rojo", "cancelled": "pendiente",
                 "action_required": "pendiente", "startup_failure": "pendiente",
                 "stale": "pendiente", None: "pendiente", "futura": "pendiente"}
        for conclusion, esperado in tabla.items():
            with self.subTest(conclusion=conclusion):
                lectura = self.observador([self.run_("c", conclusion)]).lectura_ci(REPO, "h")
                self.assertEqual(lectura.valor, esperado)
                self.assertEqual(lectura.rojos, ("c",) if esperado == "rojo" else ())
        for estado, esperado in {"success": "verde", "failure": "rojo", "error": "pendiente",
                                 "pending": "pendiente", "futuro": "pendiente"}.items():
            with self.subTest(status=estado):
                lectura = self.observador(statuses=[{"context": "ext", "state": estado}]).lectura_ci(REPO, "h")
                self.assertEqual((lectura.valor, lectura.rojos),
                                 (esperado, ("ext",) if esperado == "rojo" else ()))
        for status in ("queued", "in_progress", "waiting", "requested", "pending"):
            with self.subTest(run=status):
                self.assertEqual(self.observador([self.run_("c", None, status=status)]).ci(REPO, "h"),
                                 "pendiente")

    def test_corrida_mas_reciente_por_nombre_y_reglas_previas(self):
        casos = [
            ([self.run_("c", "failure", 1), self.run_("c", "success", 2)], {}, "verde", ()),
            ([self.run_("c", "success", 2), self.run_("c", "failure", 3)], {}, "rojo", ("c",)),
            ([self.run_("c", "cancelled", 1), self.run_("c", "success", 2)], {}, "verde", ()),
            ([self.run_("a", "failure", 1), self.run_("b", "timed_out", 2),
              self.run_("c", "success", 3)], {}, "rojo", ("a", "b")),
            # Un rojo decide aunque falte un esperado o haya pendientes.
            ([self.run_("a", "failure", 1), self.run_("b", None, 2, "in_progress")],
             {"checks_esperados": ["a", "z"]}, "rojo", ("a",)),
            ([self.run_("a", "success", 1)], {"checks_esperados": ["a", "z"]}, "pendiente", ()),
            # Un esperado debe correr: skipped/neutral no lo acreditan.
            ([self.run_("a", "skipped", 1), self.run_("b", "skipped", 2)],
             {"checks_esperados": ["a"]}, "pendiente", ()),
            ([self.run_("a", "neutral", 1), self.run_("b", "skipped", 2)],
             {"checks_esperados": ["c"]}, "pendiente", ()),
            ([], {}, "pendiente", ()),
            ([], {"checks_esperados": []}, "verde", ()),
        ]
        for checks, config, valor, rojos in casos:
            with self.subTest(checks=checks, config=config):
                lectura = self.observador(checks, **config).lectura_ci(REPO, "h")
                self.assertEqual((lectura.valor, lectura.rojos), (valor, rojos))
        # El status más reciente por contexto decide (la API los da primero).
        statuses = [{"context": "ext", "state": "success"}, {"context": "ext", "state": "failure"}]
        self.assertEqual(self.observador([VERDE], statuses).ci(REPO, "h"), "verde")

    def test_feedback_no_es_ci_salvo_en_sombra_y_la_sombra_conserva_dos_valores(self):
        aviso = [{"context": "pao-tren/resultado", "state": "failure"}]
        self.assertEqual(self.observador([VERDE], aviso).ci(REPO, "h"), "verde")
        sombra = self.observador([VERDE], aviso, activo=False)
        sombra.sombra = True
        self.assertEqual(sombra.ci(REPO, "h"), "pendiente")
        for conclusion in ("skipped", "neutral", "failure"):
            with self.subTest(conclusion=conclusion):
                checks = [VERDE, self.run_("otro", conclusion, 2)]
                self.assertEqual(self.observador(checks, activo=False).ci(REPO, "h"), "pendiente")
        self.assertEqual(self.observador([VERDE], activo=False).ci(REPO, "h"), "verde")


class ReductorTests(unittest.TestCase):
    B, C = "b" * 40, "c" * 40

    def tren(self, n, **fase):
        entradas = tuple(Entrada(Entrega(REPO, pr, str(pr) * 40, "a", "d"), pr, Admitido(), pr,
                                 tren="t") for pr in range(1, n + 1))
        candidato = Candidato(self.B, tuple((e.entrega.pr, e.entrega.head, self.C)
                                            for e in entradas), self.C, "d" * 40)
        tren = Tren("t", tuple(e.entrega.recibo for e in entradas), EsperandoCI(candidato, **fase))
        vistas = tuple(VistaPR(e.entrega.pr, e.entrega.head, apto=True, hallazgos=0, dirigida=True,
                               ci="verde", conflicto=False) for e in entradas)
        return Estado(REPO, entradas, (tren,)), Observacion(1000, self.B, vistas, tren_id="t")

    def test_rojo_ajeno_o_sin_nombres_no_se_consume(self):
        for n in (1, 3):
            estado, obs = self.tren(n, desde=900)
            rojo = replace(obs, ci_candidato="rojo", ci_sha=self.C, ci_rojos=("lint",))
            for ajena in (replace(rojo, ci_sha="e" * 40), replace(rojo, ci_rojos=()),
                          replace(rojo, tren_id="otro")):
                with self.subTest(n=n, ajena=ajena):
                    self.assertEqual(paso(estado, ajena)[0].trenes, estado.trenes)
            nuevo, _ = paso(estado, rojo)
            self.assertEqual(nuevo.trenes[0].fase, Terminal("DEVUELTO" if n == 1 else "PARTIDO"))

    def test_plazo_desde_entrada_snapshot_anterior_y_verde_visto(self):
        estado, obs = self.tren(3, desde=1000)
        self.assertIsNone(paso(estado, replace(obs, ahora=4599))[0].pausa)
        self.assertEqual(paso(estado, replace(obs, ahora=4600))[0].pausa,
                         Pausa("infraestructura", detalle="ci-sin-resultado"))
        # Un snapshot sin `desde` empieza el plazo en su primera lectura.
        antiguo, obs = self.tren(3)
        leido, _ = paso(antiguo, replace(obs, ahora=5000))
        self.assertEqual(leido.trenes[0].fase.desde, 5000)
        self.assertIsNone(paso(leido, replace(obs, ahora=8599))[0].pausa)
        # Con verde visto la suite pudo lanzarse: una lectura pendiente no pausa.
        verde, efectos = paso(estado, replace(obs, ahora=1100, ci_candidato="verde"))
        self.assertEqual([e.tipo for e in efectos], ["iniciar-suite"])
        self.assertEqual(verde.trenes[0].fase.verde, 1100)
        self.assertIsNone(paso(verde, replace(obs, ahora=99999))[0].pausa)

    def test_partida_sobrevive_readmision_y_no_afecta_otras_entregas(self):
        estado, obs = self.tren(3, desde=900)
        rojo = replace(obs, ci_candidato="rojo", ci_sha=self.C, ci_rojos=("lint",))
        partido, efectos = paso(estado, rojo)
        self.assertEqual(efectos, ())
        # Llega después: los partidos conservan su lugar en la fila.
        nueva = Entrada(Entrega(REPO, 4, "4" * 40, "a", "d"), 4, Admitido(), 1100)
        partido = replace(partido, entradas=partido.entradas + (nueva,))
        vistas = obs.prs + (VistaPR(4, "4" * 40, apto=True, hallazgos=0, dirigida=True,
                                    ci="verde", conflicto=False),)
        siguiente = replace(obs, tren_id="", prs=vistas, ahora=1200)
        for esperado in ((1,), (2,), (3,)):
            partido, efectos = paso(partido, siguiente)
            tren = partido.trenes[-1]
            self.assertEqual(tuple(e.entrega.pr for e in partido.entradas
                                   if e.entrega.recibo in tren.recibos), esperado)
            self.assertTrue(all(e.partida == "t" for e in partido.entradas[:3]))
            self.assertIsNone(partido.entradas[3].partida)
            self.assertEqual(desde_datos(datos(partido)), partido)
            partido = replace(partido, trenes=partido.trenes[:-1] + (
                replace(tren, fase=Terminal("ATERRIZADO")),))
        # Sin miembros partidos en la fila, la entrega nueva espera su ventana.
        self.assertEqual(paso(partido, siguiente)[0].trenes, partido.trenes)
        # El campo aditivo no se escribe en su default: snapshots y sombra intactos.
        self.assertNotIn("partida", datos(nueva))
        self.assertEqual(datos(partido.entradas[0])["partida"], "t")


class SombraTests(unittest.TestCase):
    """Eventos de sombra con heads rojo, skipped y verde; `remoto-u16.sh` los
    compara byte a byte con los de 85138706."""
    comando = tj.JuicioTests.comando
    commit = tj.JuicioTests.commit
    push = tj.JuicioTests.push
    remoto = tj.JuicioTests.remoto
    ledger = tj.JuicioTests.ledger

    def setUp(self):
        # Fechas fijas: mismos SHA, recibos e ids de tren en ambos árboles.
        fechas = patch.dict(os.environ, {"GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
                                         "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"})
        fechas.start()
        self.addCleanup(fechas.stop)
        tj.JuicioTests.setUp(self)

    def test_eventos_de_sombra_no_cambian(self):
        pr1, pr2 = self.miembros[0][1], self.miembros[1][1]
        posts = self.raiz / "posts.log"
        posts.write_text(json.dumps({"endpoint": "repos/{}/statuses/{}".format(REPO, pr1),
                                     "payload": {"context": "otro-check", "state": "failure"}}) + "\n")
        original = self.contexto.gh

        def gh(argv, **kwargs):
            if argv[:2] == ["gh", "api"] and argv[4].startswith(
                    "repos/{}/commits/{}/check-runs?".format(REPO, pr2)):
                valor = [{"check_runs": [VERDE, {"id": 2, "name": "opcional",
                                                 "status": "completed", "conclusion": "skipped"}]}]
                return subprocess.CompletedProcess(argv, 0, json.dumps(valor), "")
            return original(argv, **kwargs)

        args = activo.cli.parser().parse_args(["tick", "--repo", REPO, "--seco"])
        for inscripcion, doble in (("sombra", True), ("activo", False)):
            self.inscritos.write_text(REPO + " " + inscripcion + "\n")
            observador = GitHubGit(self.contexto.config, ejecutar=gh, sombra=doble)
            for ahora in (100, 3700, 7300):
                self.assertEqual(activo.cli.ejecutar(args, observador=observador, ahora=ahora)[0], 0)
        with self.ledger() as ledger:
            eventos = [(r["tipo"], r["instante"], json.loads(r["payload"])) for r in ledger.db.execute(
                "SELECT tipo, instante, payload FROM events WHERE tipo IN "
                "('sombra', 'sombra-doble-camino', 'admision-entrega') ORDER BY seq")]
        self.assertEqual(sum(t in ("sombra", "sombra-doble-camino") for t, _, _ in eventos), 6)
        for tipo, _, plan in eventos:
            if tipo == "admision-entrega":
                continue
            admisiones = [e["admision"] for e in plan["estado"]["entradas"]]
            # Rojo y skipped en el head siguen siendo «no verde» para la sombra.
            self.assertEqual([a["_tipo"] for a in admisiones[:2]], ["Retenido", "Retenido"])
            self.assertEqual({a["motivo"] for a in admisiones[:2]}, {"ci-no-verde"})
            self.assertEqual(admisiones[2]["_tipo"], "Admitido")
        texto = json.dumps(eventos, sort_keys=True).replace(str(self.raiz), "<raiz>")
        destino = os.environ.get("PAO_TREN_U16_SOMBRA")
        if destino:
            Path(destino).write_text(texto)


if __name__ == "__main__":
    unittest.main()
