"""U15: juicio y pausa por CLI, consumidos por el tick; sólo en el host de pruebas.

Origen git local, GitHub y lanzador falsos de U13/U14, HOME y XDG temporales.
No se retiran fixtures.
"""
import json
from pathlib import Path
import re
import shlex
import unittest

import test_activo as activo
from dominio import (Admitido, Congelado, Corriendo, Decision, Diagnosticando, Entrada,
                     Entrega, EsperandoCI, Estado, Motivo, Pausa, Rechazado, Terminal,
                     Tren, Candidato, datos, desde_datos)
import juicio

RAIZ = Path(__file__).resolve().parents[1]
REPO = activo.REPO


class JuicioTests(unittest.TestCase):
    setUp_activo = activo.ActivoTests.setUp
    comando = activo.ActivoTests.comando
    commit = activo.ActivoTests.commit
    push = activo.ActivoTests.push
    remoto = activo.ActivoTests.remoto
    ledger = activo.ActivoTests.ledger
    estado = activo.ActivoTests.estado
    terminar = activo.ActivoTests.terminar
    hasta_fase = activo.ActivoTests.hasta_fase
    lineas = activo.ActivoTests.lineas
    hijo = activo.ActivoTests.hijo

    def setUp(self):
        self.setUp_activo()
        self.reloj = 10000

    def cli(self, *argv):
        args = activo.cli.parser().parse_args(list(argv))
        return activo.cli.ejecutar(args)

    def juicio(self, *argv):
        return self.cli("juicio", "--repo", REPO, *argv)

    def pausa(self, *argv):
        return self.cli("pausa", "--repo", REPO, *argv)

    def tick(self):
        self.reloj += 120
        return self.contexto.tick(self.reloj)

    def ticks(self, hasta, n=25):
        for _ in range(n):
            self.tick()
            estado = self.estado()
            if hasta(estado):
                return estado
        estado = self.estado()
        self.fail("no converge: " + json.dumps({
            "pausa": datos(estado.pausa), "fases": [datos(t.fase) for t in estado.trenes],
            "admision": [datos(e.admision) for e in estado.entradas]})[:4000])

    def posts(self, tipo=None):
        todos = [json.loads(s) for s in self.lineas("posts.log")]
        return [p for p in todos if tipo is None or tipo in p["endpoint"]]

    def estados_status(self):
        return [p["payload"]["state"] for p in self.posts("/statuses/")]

    def decisiones(self):
        with self.ledger() as ledger:
            return ledger.decisiones(REPO)

    def pushes_main(self):
        return [p for p in self.lineas("pushes.log") if p.endswith(":refs/heads/main")]

    def rojo_sin_atribucion(self):
        """Sin costo de suite sana no hay presupuesto: el rojo pasa a juicio."""
        (self.raiz / "producto-rojo").write_text("causal: segundo PR")
        estado = self.ticks(lambda e: e.pausa is not None)
        self.assertEqual(estado.pausa, Pausa("necesita-juicio", detalle="sin-costo-de-suite-sana"))
        self.assertIsInstance(estado.trenes[-1].fase, Diagnosticando)
        return estado

    def retirar(self, nombre):
        ruta = self.raiz / nombre
        ruta.rename(ruta.with_name(nombre + "-retirado-" + str(self.reloj)))

    def test_absolver_relanza_suite_sobre_el_mismo_c_y_aterriza_sólo_con_verde(self):
        pausado = self.rojo_sin_atribucion()
        primero = pausado.trenes[0]
        C = primero.fase.candidato.C
        codigo, salida = self.juicio("--tren", primero.id, "--decision", "absolver",
                                     "--motivo", "rojo intermitente conocido")
        self.assertEqual((codigo, salida["estado"]), (0, "pendiente"), salida)
        # Otro rojo sobre el mismo C vuelve a pausar en la generación nueva.
        repausado = self.ticks(lambda e: e.pausa is not None)
        self.assertEqual(repausado.trenes[0].fase, Terminal("ABSUELTO"))
        segundo = repausado.trenes[1]
        self.assertEqual(segundo.recibos, primero.recibos)
        self.assertIsInstance(segundo.fase, Diagnosticando)
        self.assertEqual(segundo.fase.candidato, primero.fase.candidato)
        self.assertEqual(repausado.pausa.motivo, "necesita-juicio")
        self.assertEqual(len(self.lineas("trabajos.log")), 2)
        # El tren absuelto es terminal: ya no admite juicio.
        codigo, salida = self.juicio("--tren", primero.id, "--decision", "absolver",
                                     "--motivo", "tarde")
        self.assertEqual((codigo, salida["error"]), (2, "tren-no-corresponde"))
        self.retirar("producto-rojo")
        self.assertEqual(self.juicio("--tren", segundo.id, "--decision", "absolver",
                                     "--motivo", "corregido el entorno")[0], 0)
        final = self.ticks(lambda e: e.pausa is not None or
                           isinstance(e.trenes[-1].fase, Terminal))
        self.assertIsNone(final.pausa)
        self.assertEqual([t.fase for t in final.trenes],
                         [Terminal("ABSUELTO"), Terminal("ABSUELTO"), Terminal("ATERRIZADO")])
        self.assertEqual(self.remoto(), C)
        self.assertEqual(len(self.pushes_main()), 1)
        self.assertEqual(len(self.lineas("trabajos.log")), 3)
        # Absolver no publica rojo; cada generación avisa su pending.
        self.assertEqual(self.posts("/comments"), [])
        self.assertEqual(self.estados_status(), ["pending"] * 9 + ["success"] * 3)
        self.assertEqual([d["estado"] for d in self.decisiones()], ["aplicada", "aplicada"])

    def test_expulsar_devuelve_al_pr_y_los_demas_aterrizan_en_otra_generacion(self):
        pausado = self.rojo_sin_atribucion()
        tren = pausado.trenes[0]
        codigo, salida = self.juicio("--tren", tren.id, "--decision", "expulsar", "--pr", "2",
                                     "--motivo", "el rojo es del segundo PR")
        self.assertEqual(codigo, 0, salida)
        final = self.ticks(lambda e: e.pausa is not None or
                           isinstance(e.trenes[-1].fase, Terminal) and len(e.trenes) > 1)
        self.assertIsNone(final.pausa)
        self.assertEqual(final.trenes[0].fase, Terminal("EXPULSADO"))
        self.assertEqual(final.trenes[1].fase, Terminal("ATERRIZADO"))
        self.assertEqual(final.entradas[1].terminal, Terminal("DEVUELTO"))
        recibos = [e.entrega.recibo for e in final.entradas]
        self.assertEqual(final.trenes[1].recibos, (recibos[0], recibos[2]))
        comentarios = self.posts("/comments")
        self.assertEqual(len(comentarios), 1)
        self.assertIn("/issues/2/comments", comentarios[0]["endpoint"])
        self.assertIn("conjunto_causal", comentarios[0]["payload"]["body"])
        self.assertNotIn(str(self.raiz), comentarios[0]["payload"]["body"])
        fallo = [p for p in self.posts("/statuses/") if p["payload"]["state"] == "failure"]
        self.assertEqual(len(fallo), 1)
        self.assertTrue(fallo[0]["endpoint"].endswith(self.miembros[1][1]))
        self.assertEqual(fallo[0]["payload"]["context"], "pao-tren/resultado")
        self.assertEqual(fallo[0]["payload"]["target_url"],
                         "https://github.com/" + REPO + "/pull/2#issuecomment-1")
        self.assertEqual(self.estados_status(),
                         ["pending"] * 3 + ["failure"] + ["pending"] * 2 + ["success"] * 2)
        main = self.remoto()
        for indice in (0, 2):
            self.comando(self.origen, "merge-base", "--is-ancestor", self.miembros[indice][1], main)
        self.assertNotEqual(self.comando(self.origen, "merge-base", self.miembros[1][1], main),
                            self.miembros[1][1])
        self.assertEqual(len(self.pushes_main()), 1)
        with self.ledger() as ledger:
            fila = ledger.db.execute("SELECT payload FROM effects WHERE payload LIKE '%\"devolver\"%'"
                                     ).fetchone()
        payload = json.loads(desde_datos(json.loads(fila[0])).datos[2])
        self.assertEqual((payload["pr"], payload["C"]), (2, tren.fase.candidato.C))
        self.assertEqual(payload["fallos"][0]["conjunto_causal"], [2])
        self.assertIn("--sha " + tren.fase.candidato.C, payload["fallos"][0]["comando"])

    def test_abortar_sin_culpa_ni_main_y_las_entregas_vuelven_a_formar_tren(self):
        pausado = self.rojo_sin_atribucion()
        tren = pausado.trenes[0]
        antes = {e.entrega.recibo: e.admitida for e in pausado.entradas}
        self.assertEqual(self.juicio("--tren", tren.id, "--decision", "abortar",
                                     "--motivo", "lote equivocado")[0], 0)
        self.tick()
        abortado = self.estado()
        self.assertIsNone(abortado.pausa)
        self.assertEqual(abortado.trenes[0].fase, Terminal("ABORTADO"))
        self.assertTrue(all(e.tren is None and e.terminal is None for e in abortado.entradas))
        self.assertEqual({e.entrega.recibo: e.admitida for e in abortado.entradas}, antes)
        self.tick()
        rearmado = self.estado()
        self.assertEqual(len(rearmado.trenes), 2)
        self.assertNotEqual(rearmado.trenes[1].id, tren.id)
        self.assertEqual(set(rearmado.trenes[1].recibos), set(tren.recibos))
        self.assertIsInstance(rearmado.trenes[1].fase, Congelado)
        self.assertEqual(self.remoto(), self.B)
        self.assertEqual(self.pushes_main(), [])
        self.assertEqual(self.posts("/comments"), [])
        self.assertNotIn("failure", self.estados_status())
        self.assertEqual(self.estados_status(), ["pending"] * 6)
        with self.ledger() as ledger:
            for row in ledger.db.execute("SELECT payload FROM entries"):
                custodia = desde_datos(json.loads(row[0]))
                self.assertEqual(custodia, Entrada(custodia.entrega, custodia.recibida))

    def test_escritor_externo_reanudar_conserva_main_ajeno_y_rearma_sobre_el(self):
        self.hasta_fase(activo.Certificado)
        ajeno = self.commit({"comun": "base\n", "externo": "cambio\n"}, (self.B,))
        self.push(ajeno, "refs/heads/ajeno")
        (self.raiz / "escritor").write_text(ajeno)
        pausado = self.terminar()
        self.assertEqual(pausado.pausa, Pausa("escritor-externo", main=ajeno))
        self.retirar("escritor")
        self.assertEqual(self.remoto(), ajeno)
        # El lanzador falso fecha su resultado contra la B del tren nuevo.
        self.contexto.config["base"] = ajeno
        codigo, salida = self.juicio("--tren", pausado.trenes[0].id, "--decision", "reanudar",
                                     "--motivo", "merge manual de otra sesión")
        self.assertEqual((codigo, salida["error"]), (2, "tren-no-corresponde"))
        codigo, salida = self.juicio("--decision", "abortar", "--motivo", "no aplica")
        self.assertEqual((codigo, salida["error"]), (2, "decision-no-admitida"))
        self.assertEqual(self.juicio("--decision", "reanudar",
                                     "--motivo", "merge manual de otra sesión")[0], 0)
        final = self.ticks(lambda e: e.pausa is not None or
                           len(e.trenes) > 1 and isinstance(e.trenes[-1].fase, Terminal))
        self.assertIsNone(final.pausa)
        self.assertEqual(final.trenes[0].fase, Terminal("REARMAR"))
        self.assertEqual(final.trenes[1].fase, Terminal("ATERRIZADO"))
        main = self.remoto()
        self.assertNotEqual(main, ajeno)
        self.comando(self.origen, "merge-base", "--is-ancestor", ajeno, main)
        for _, head in self.miembros:
            self.comando(self.origen, "merge-base", "--is-ancestor", head, main)

    def test_head_cambiado_expulsar_al_rechazado_aterriza_el_resto(self):
        self.hasta_fase(EsperandoCI)
        # Head real nuevo del PR 1 en GitHub; el ledger conserva el entregado.
        nuevo = self.commit({"comun": "base\n", "pr-1": "ticket\n", "nuevo": "head\n"},
                            (self.miembros[0][1],))
        self.push(nuevo, "refs/pull/1/head")
        self.contexto.config["miembros"][0][1] = nuevo
        pausado = self.ticks(lambda e: e.pausa is not None)
        self.assertEqual(pausado.pausa, Pausa("head-cambiado-en-tren"))
        self.assertEqual(pausado.entradas[0].admision, Rechazado(Motivo.HEAD))
        tren = pausado.trenes[0]
        codigo, salida = self.juicio("--tren", tren.id, "--decision", "absolver", "--motivo", "x")
        self.assertEqual((codigo, salida["error"]), (2, "decision-no-admitida"))
        self.assertEqual(self.juicio("--tren", tren.id, "--decision", "expulsar", "--pr", "1",
                                     "--motivo", "cambió su head dentro del tren")[0], 0)
        final = self.ticks(lambda e: e.pausa is not None or
                           len(e.trenes) > 1 and isinstance(e.trenes[-1].fase, Terminal))
        self.assertIsNone(final.pausa)
        self.assertEqual(final.trenes[1].fase, Terminal("ATERRIZADO"))
        self.assertEqual(final.trenes[1].recibos, tren.recibos[1:])
        self.assertEqual(final.entradas[0].terminal, Terminal("DEVUELTO"))

    def test_decisiones_invalidas_no_cambian_estado_y_sombra_rechaza(self):
        codigo, salida = self.juicio("--decision", "reanudar", "--motivo", "sin pausa")
        self.assertEqual((codigo, salida["error"]), (2, "sin-pausa"))
        pausado = self.rojo_sin_atribucion()
        tren = pausado.trenes[0]
        casos = (
            (("--decision", "reanudar"), "decision-no-admitida"),
            (("--decision", "abortar"), "tren-no-corresponde"),
            (("--tren", "0" * 64, "--decision", "abortar"), "tren-no-corresponde"),
            (("--tren", tren.id, "--decision", "expulsar", "--pr", "9"), "pr-no-expulsable"),
            (("--tren", tren.id, "--decision", "expulsar"), "falta-pr"),
            (("--tren", tren.id, "--decision", "abortar", "--pr", "1"), "pr-no-corresponde"),
        )
        for argv, error in casos:
            codigo, salida = self.juicio(*argv, "--motivo", "prueba")
            self.assertEqual((codigo, salida["error"]), (2, error), argv)
        self.assertEqual(self.pausa("--levantar", "--motivo", "x")[1]["error"], "decision-no-admitida")
        self.assertEqual(self.pausa("--motivo", "x")[1]["error"], "carril-pausado")
        with self.assertRaises(SystemExit):
            activo.cli.parser().parse_args(["juicio", "--repo", REPO, "--decision", "abortar",
                                            "--motivo", "  "])
        self.assertEqual(self.decisiones(), [])
        self.assertEqual(self.estado(), pausado)
        # Si sólo el reductor puede saberlo, queda rechazada y sin efecto.
        with self.ledger() as ledger:
            ledger.registrar_decision(Decision("d" * 64, REPO, "abortar", "pausa vieja", 1,
                                               Pausa("conflicto-en-cadena", pr=1), tren.id))
        _, salida = self.tick()
        self.assertEqual(salida["decision"], {"id": "d" * 64, "rechazo": "pausa-distinta"})
        self.assertEqual(self.estado(), pausado)
        self.assertEqual([(d["estado"], d["resultado"]) for d in self.decisiones()],
                         [("rechazada", "pausa-distinta")])
        # Una sola pendiente por repo.
        self.assertEqual(self.juicio("--tren", tren.id, "--decision", "abortar",
                                     "--motivo", "válida")[0], 0)
        codigo, salida = self.juicio("--tren", tren.id, "--decision", "absolver", "--motivo", "otra")
        self.assertEqual((codigo, salida["error"]), (2, "decision-pendiente"))
        for texto, modo in ((REPO + " sombra\n", "sombra"), ("", "no-inscrito")):
            self.inscritos.write_text(texto)
            for argv in (("juicio", "--repo", REPO, "--tren", tren.id, "--decision", "absolver",
                          "--motivo", "x"), ("pausa", "--repo", REPO, "--motivo", "x")):
                codigo, salida = self.cli(*argv)
                self.assertEqual((codigo, salida["inscripcion"]), (2, modo))
        self.assertEqual(len(self.decisiones()), 2)

    def morir_en(self, punto):
        hijo = self.hijo("""
from test_activo import Contexto
import os
c = Contexto(sys.argv[3])
def frontera(nombre, efecto):
    if nombre == sys.argv[4]:
        os._exit(77)
c.tick(int(sys.argv[5]), frontera)
sys.exit(9)
""", punto, str(self.reloj + 60))
        salida, error = hijo.communicate(timeout=90)
        self.assertEqual(hijo.returncode, 77, salida + error)

    def test_muerte_antes_y_despues_de_consumir_la_decision_la_aplica_una_vez(self):
        pausado = self.rojo_sin_atribucion()
        tren = pausado.trenes[0]
        self.assertEqual(self.juicio("--tren", tren.id, "--decision", "absolver",
                                     "--motivo", "reintentar")[0], 0)
        self.morir_en("antes-guardar-paso")
        self.assertEqual(self.estado(), pausado)
        self.assertEqual([d["estado"] for d in self.decisiones()], ["pendiente"])
        self.morir_en("paso-guardado")
        aplicado = self.estado()
        self.assertEqual([t.fase for t in aplicado.trenes[:1]], [Terminal("ABSUELTO")])
        self.assertEqual(len(aplicado.trenes), 2)
        self.assertEqual([d["estado"] for d in self.decisiones()], ["aplicada"])
        self.retirar("producto-rojo")
        final = self.ticks(lambda e: e.pausa is not None or isinstance(e.trenes[-1].fase, Terminal))
        self.assertEqual([t.fase for t in final.trenes],
                         [Terminal("ABSUELTO"), Terminal("ATERRIZADO")])
        self.assertEqual(len(self.lineas("trabajos.log")), 2)
        self.assertEqual(len(self.pushes_main()), 1)
        with self.ledger() as ledger:
            consumos = [json.loads(r[0]).get("decision") for r in ledger.db.execute(
                "SELECT payload FROM events WHERE tipo='operativo'")]
        self.assertEqual(len([c for c in consumos if c]), 1)

    def test_pausa_manual_detiene_git_y_suite_pero_no_feedback(self):
        (self.raiz / "fallo-post").write_text("/statuses/")
        self.tick()
        self.assertIsInstance(self.estado().trenes[0].fase, Congelado)
        codigo, salida = self.pausa("--motivo", "mantenimiento del lanzador")
        self.assertEqual(codigo, 0, salida)
        self.tick()
        self.assertEqual(self.estado().pausa, Pausa("manual", detalle="mantenimiento del lanzador"))
        self.retirar("fallo-post")
        for _ in range(3):
            self.tick()
        pausado = self.estado()
        self.assertIsInstance(pausado.trenes[0].fase, Congelado)
        self.assertEqual(self.lineas("pushes.log"), [])
        self.assertEqual(self.lineas("trabajos.log"), [])
        with self.ledger() as ledger:
            # Feedback publicado bajo la pausa; sólo queda el candidato sin consumir.
            pendientes = ledger.outbox(REPO)
        self.assertEqual([(json.loads(f["payload"])["tipo"], f["estado"]) for f in pendientes],
                         [("armar-candidato", "resultado")])
        _, salida = self.cli("estado", "--repo", REPO, "--juicio")
        self.assertEqual(salida["juicio"]["admitidas"], [{"decision": "levantar", "comando": shlex.join(
            ["pao-tren", "pausa", "--repo", REPO, "--levantar", "--motivo", "<motivo>"])}])
        self.assertEqual(self.juicio("--decision", "reanudar", "--motivo", "x")[1]["error"],
                         "decision-no-admitida")
        self.assertEqual(self.pausa("--levantar", "--motivo", "lanzador listo")[0], 0)
        final = self.ticks(lambda e: e.pausa is not None or isinstance(e.trenes[0].fase, Terminal))
        self.assertEqual(final.trenes[0].fase, Terminal("ATERRIZADO"))
        self.assertEqual(len(self.pushes_main()), 1)

    def test_estado_juicio_expone_expediente_y_comandos_exactos(self):
        pausado = self.rojo_sin_atribucion()
        tren = pausado.trenes[0]
        codigo, salida = self.cli("estado", "--repo", REPO, "--juicio")
        self.assertEqual(codigo, 0)
        exp = salida["juicio"]
        self.assertEqual(exp["pausa"]["motivo"], "necesita-juicio")
        self.assertEqual(exp["pausa"]["detalle"], "sin-costo-de-suite-sana")
        self.assertEqual((exp["tren"], exp["fase"]), (tren.id, "Diagnosticando"))
        self.assertEqual((exp["B"], exp["C"]), (self.B, tren.fase.candidato.C))
        self.assertEqual([(m["pr"], m["head"]) for m in exp["miembros"]],
                         [(pr, head) for pr, head in self.miembros])
        self.assertTrue(exp["causa"]["suite"])
        self.assertEqual(exp["causa"]["fallos"][0]["unidad"], activo.F.unidad)
        base = ["pao-tren", "juicio", "--repo", REPO, "--tren", tren.id, "--decision"]
        esperados = ([shlex.join(base + ["absolver", "--motivo", "<motivo>"])] +
                     [shlex.join(base + ["expulsar", "--pr", str(pr), "--motivo", "<motivo>"])
                      for pr in (1, 2, 3)] +
                     [shlex.join(base + ["abortar", "--motivo", "<motivo>"])])
        self.assertEqual([a["comando"] for a in exp["admitidas"]], esperados)
        # Cada comando del expediente se acepta tal cual, con su motivo.
        argv = shlex.split(esperados[-1].replace("<motivo>", "desde el expediente"))
        self.assertEqual(self.cli(*argv[1:])[0], 0)
        self.tick()
        exp = self.cli("estado", "--repo", REPO, "--juicio")[1]["juicio"]
        self.assertIsNone(exp["pausa"])
        self.assertEqual([(d["decision"]["decision"], d["estado"]) for d in exp["decisiones"]],
                         [("abortar", "aplicada")])
        self.assertEqual(exp["admitidas"][0]["decision"], "pausar")

    def test_sombra_no_consume_decisiones_ni_cambia_sus_eventos(self):
        with self.ledger() as ledger:
            ledger.registrar_decision(Decision("e" * 64, REPO, "pausar", "x", 1))
        self.inscritos.write_text(REPO + " sombra\n")
        seco = activo.cli.parser().parse_args(["tick", "--repo", REPO, "--seco"])
        codigo, salida = activo.cli.ejecutar(seco, self.contexto.observador, 3700)
        self.assertEqual(codigo, 0)
        self.assertEqual(set(salida["plan"]), {"estado", "efectos"})
        self.assertNotIn("decision", json.dumps(salida["plan"]))
        self.assertIsNone(self.estado().pausa)
        self.assertEqual([d["estado"] for d in self.decisiones()], ["pendiente"])
        with self.ledger() as ledger:
            tipos = [r[0] for r in ledger.db.execute("SELECT tipo FROM events ORDER BY seq")]
        self.assertEqual(tipos.count("sombra-doble-camino"), 1)
        self.assertNotIn("operativo", tipos)


class TablaTests(unittest.TestCase):
    """La tabla cubre cada pausa que asigna el reductor; transiciones puras."""

    def literales(self, patron):
        texto = (RAIZ / "dominio.py").read_text() + (RAIZ / "atribucion.py").read_text()
        return set(re.findall(patron, texto))

    def test_cada_pausa_del_reductor_tiene_fila(self):
        motivos = self.literales(r'Pausa\(\s*"([a-z0-9-]+)"')
        self.assertTrue(motivos >= {"etapa-1", "necesita-juicio", "escritor-externo"})
        for motivo in motivos:
            self.assertIn(motivo, juicio.TABLA)
        detalles = (self.literales(r'detalle="([a-z0-9-]+)"') |
                    self.literales(r'incierto="([a-z0-9-]+)[:"]'))
        detalles.discard("tres-reanudaciones-agotadas")  # infraestructura
        self.assertEqual(detalles, set(juicio.DETALLES_JUICIO))
        decisiones = set(juicio.TRANSICIONES)
        for fila in juicio.TABLA.values():
            for admite in (fila.values() if isinstance(fila, dict) else (fila,)):
                self.assertTrue(set(admite.decisiones) <= decisiones)
                self.assertEqual(bool(admite.expulsar), "expulsar" in admite.decisiones)
                # reanudar/levantar no tocan el tren; nunca conviven con decisiones de tren.
                if {"reanudar", "levantar"} & set(admite.decisiones):
                    self.assertFalse(juicio.SOBRE_TREN & set(admite.decisiones))
        self.assertEqual(juicio.admitidas(Pausa("motivo-futuro")).decisiones, ("abortar",))

    def estado_pausado(self, pausa, fase=None, rechazado=False):
        entradas = tuple(Entrada(Entrega(REPO, pr, str(pr) * 40, "a", "d"), pr,
                                 Rechazado(Motivo.HEAD) if rechazado and pr == 1 else Admitido(),
                                 pr, tren="t") for pr in (1, 2, 3))
        candidato = Candidato("b" * 40, tuple((pr, str(pr) * 40, "c" * 40) for pr in (1, 2, 3)),
                              "c" * 40, "e" * 40)
        fase = fase or Corriendo(candidato, "suite")
        return Estado(REPO, entradas, (Tren("t", tuple(e.entrega.recibo for e in entradas), fase),),
                      pausa)

    def decision(self, estado, nombre, pr=None, tren="t"):
        return Decision("x", REPO, nombre, "m", 1, estado.pausa, tren, pr)

    def test_conflicto_sólo_expulsa_al_pr_en_conflicto(self):
        estado = self.estado_pausado(Pausa("conflicto-en-cadena", pr=2, archivos=("a",)),
                                     Congelado("b" * 40))
        self.assertEqual(juicio.rechazo(estado, self.decision(estado, "expulsar", 1)),
                         "pr-no-expulsable")
        nuevo, efectos = juicio.aplicar(estado, self.decision(estado, "expulsar", 2))
        self.assertIsNone(nuevo.pausa)
        self.assertEqual([e.tipo for e in efectos],
                         ["devolver", "armar-candidato", "avisar-en-tren", "avisar-en-tren"])
        payload = json.loads(efectos[0].datos[2])
        self.assertEqual((payload["pr"], payload["C"], payload["fallos"]), (2, "", []))
        self.assertEqual(nuevo.trenes[1].fase, Congelado("b" * 40))

    def test_absolver_exige_miembros_vigentes_y_candidato(self):
        estado = self.estado_pausado(Pausa("necesita-juicio", detalle="verde-incoherente"),
                                     rechazado=True)
        self.assertEqual(juicio.rechazo(estado, self.decision(estado, "absolver")),
                         "miembro-rechazado")
        self.assertEqual(juicio.aplicar(estado, self.decision(estado, "absolver")), (estado, ()))
        estado = self.estado_pausado(Pausa("necesita-juicio", detalle="verde-incoherente"))
        nuevo, efectos = juicio.aplicar(estado, self.decision(estado, "absolver"))
        self.assertEqual(nuevo.trenes[1].fase, EsperandoCI(estado.trenes[0].fase.candidato))
        self.assertEqual({e.tipo for e in efectos}, {"avisar-en-tren"})

    def test_ninguna_decision_certifica_ni_promueve(self):
        pausas = [Pausa(m) for m, f in juicio.TABLA.items() if not isinstance(f, dict)]
        pausas += [Pausa("necesita-juicio", detalle=d) for d in juicio.DETALLES_JUICIO]
        for pausa in pausas:
            estado = self.estado_pausado(pausa)
            for nombre in juicio.TRANSICIONES:
                for pr in ((1, 2, 3) if nombre == "expulsar" else (None,)):
                    tren = "t" if nombre in juicio.SOBRE_TREN else ""
                    nuevo, efectos = juicio.aplicar(estado, self.decision(estado, nombre, pr, tren))
                    self.assertFalse({"promover", "iniciar-suite"} & {e.tipo for e in efectos})
                    for t in nuevo.trenes:
                        self.assertNotIn(type(t.fase).__name__, ("Certificado", "Promoviendo"))


if __name__ == "__main__":
    unittest.main()
