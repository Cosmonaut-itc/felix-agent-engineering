"""Injertos U15: regresiones CLI/tick y recuperación contra un origen real."""
import ast
from dataclasses import replace
import json
import sqlite3
import unittest

import test_activo as activo
import test_juicio as juicio
from dominio import (Armado, Certificado, Congelado,
                     EsperandoCI, Pausa, Promoviendo, Rechazado, Reproduccion,
                     Terminal)
from juicios import POLITICA, politica

REPO = activo.REPO


class ContextoInjerto(unittest.TestCase):
    setUp = juicio.JuicioTests.setUp
    comando = juicio.JuicioTests.comando
    commit = juicio.JuicioTests.commit
    push = juicio.JuicioTests.push
    remoto = juicio.JuicioTests.remoto
    ledger = juicio.JuicioTests.ledger
    estado = juicio.JuicioTests.estado
    hasta_fase = juicio.JuicioTests.hasta_fase
    lineas = juicio.JuicioTests.lineas
    hijo = juicio.JuicioTests.hijo
    cli = juicio.JuicioTests.cli
    hasta_pausa = juicio.JuicioTests.hasta_pausa
    rojo = juicio.JuicioTests.rojo
    aterrizar_ultimo = juicio.JuicioTests.aterrizar_ultimo
    posts = juicio.JuicioTests.posts
    decisiones = juicio.JuicioTests.decisiones
    juicio = juicio.JuicioTests.juicio


class RegresionesTests(ContextoInjerto):
    def test_expulsar_excluye_otros_heads_ya_rechazados(self):
        self.hasta_fase(Armado)
        for indice in (0, 1):
            pr, head = self.miembros[indice]
            nuevo = self.commit({"comun": "base\n", "nuevo": str(pr)}, (head,))
            self.push(nuevo, "refs/pull/{}/head".format(pr))
            self.contexto.config["miembros"][indice][1] = nuevo
        antes = self.hasta_pausa()
        self.assertEqual(antes.pausa.motivo, "head-cambiado-en-tren")
        self.assertTrue(all(isinstance(e.admision, Rechazado) for e in antes.entradas[:2]))
        self.juicio("expulsar", 1)
        self.contexto.tick(8000)
        despues = self.estado()
        self.assertEqual(despues.trenes[-1].recibos, (antes.entradas[2].entrega.recibo,))
        self.assertEqual(despues.entradas[1], antes.entradas[1])
        self.assertEqual(despues.trenes[0].fase, Terminal("DEVUELTO"))
        self.aterrizar_ultimo()
        self.assertEqual([p["payload"]["state"] for p in self.posts("/statuses/")],
                         ["pending"] * 3 + ["failure"] * 3 + ["pending", "success"])
        self.assertEqual(len(self.posts("/comments")), 3)  # dos caducidades y la expulsión
        self.assertEqual(len(self.lineas("trabajos.log")), 1)
        self.comando(self.origen, "merge-base", "--is-ancestor", self.miembros[2][1], self.remoto())
        for _, head in self.miembros[:2]:
            self.assertNotEqual(self.comando(self.origen, "merge-base", head, self.remoto()), head)

    def test_absolver_rechaza_snapshot_caducado_y_fases_inadmisibles(self):
        self.rojo()
        nuevo = self.commit({"comun": "base\n", "nuevo": "head\n"}, (self.miembros[0][1],))
        self.push(nuevo, "refs/pull/1/head")
        self.contexto.config["miembros"][0][1] = nuevo
        self.contexto.tick(7900)
        antes = self.estado()
        self.assertEqual(antes.pausa.motivo, "necesita-juicio")
        self.assertIsInstance(antes.entradas[0].admision, Rechazado)
        tren = antes.trenes[0]
        casos = [(antes, "miembro-rechazado")]
        candidato = tren.fase.candidato
        for fase in (Congelado(self.B), Armado(candidato), EsperandoCI(candidato),
                     Certificado(candidato.C, candidato.arbol, "suite", "ci", self.B),
                     Promoviendo(Certificado(candidato.C, candidato.arbol, "suite", "ci", self.B))):
            casos.append((replace(antes, trenes=(replace(tren, fase=fase),)), "fase-no-corresponde"))
        for snapshot, error in casos:
            with self.subTest(fase=type(snapshot.trenes[0].fase).__name__, error=error):
                with self.ledger() as ledger:
                    ledger.guardar_paso(snapshot, (), 7950)
                    eventos = ledger.db.execute("SELECT count(*) FROM events").fetchone()[0]
                codigo, salida = self.cli("juicio", "--tren", tren.id, "--decision", "absolver",
                                          "--motivo", "prueba")
                self.assertEqual(codigo, 2, salida)
                self.assertEqual(salida["error"], error)
                self.assertEqual(self.estado(), snapshot)
                self.assertEqual(self.decisiones(), [])
                with self.ledger() as ledger:
                    self.assertEqual(ledger.db.execute("SELECT count(*) FROM events").fetchone()[0], eventos)
                opciones = self.cli("estado", "--juicio")[1]["juicio"]["admitidas"]
                self.assertNotIn("absolver", [a["decision"] for a in opciones])

    def test_infraestructura_agotada_permite_suite_nueva_sin_ampliar_d5(self):
        estado = self.rojo()
        tren = estado.trenes[0]
        candidato = tren.fase.candidato
        fase = replace(tren.fase, reanudaciones=3, reanudar="comando guardado",
            presupuesto_s=39, pendiente=Reproduccion("reproduccion", tren.fase.fallos[0], "C", costo_s=20))
        with self.ledger() as ledger:
            ledger.guardar_paso(replace(estado, trenes=(replace(tren, fase=fase),),
                pausa=Pausa("infraestructura", detalle="tres-reanudaciones-agotadas")), (), 7000)
        antes = self.estado()
        codigo, salida = self.cli("juicio", "--tren", tren.id, "--decision", "reanudar", "--motivo", "prueba")
        self.assertEqual(codigo, 2)
        self.assertEqual(salida["error"], "presupuesto-de-diagnostico-agotado")
        self.assertEqual(self.estado(), antes)
        self.assertEqual(self.decisiones(), [])
        opciones = self.cli("estado", "--juicio")[1]["juicio"]["admitidas"]
        self.assertEqual({a["decision"] for a in opciones}, {"absolver", "abortar"})
        self.juicio("absolver")
        self.contexto.tick(8000)
        self.assertEqual(self.estado().trenes[0].fase, Terminal("ABSUELTO"))
        self.assertEqual(self.estado().trenes[-1].fase, Armado(candidato))
        self.assertEqual(self.remoto(), self.B)
        (self.raiz / "ci-pendiente").write_text("esperar CI nuevo")
        for n in range(3):
            self.contexto.tick(8100 + n)
        self.assertIsInstance(self.estado().trenes[-1].fase, EsperandoCI)
        self.assertEqual(len(self.lineas("trabajos.log")), 1)
        (self.raiz / "ci-pendiente").rename(self.raiz / "ci-restablecido")
        (self.raiz / "producto-rojo").rename(self.raiz / "rojo-retirado")
        self.aterrizar_ultimo()
        self.assertEqual(self.remoto(), candidato.C)
        self.assertEqual(len(self.lineas("trabajos.log")), 2)
        self.assertEqual(sum(p.endswith(":refs/heads/main") for p in self.lineas("pushes.log")), 1)
        self.assertFalse(any(p["payload"]["state"] == "failure" for p in self.posts("/statuses/")))
        self.assertEqual(self.posts("/comments"), [])


class RecuperacionTests(ContextoInjerto):
    def test_sombra_con_decision_pendiente_conserva_eventos_y_decision(self):
        # Primero el contrafactual sin juicio; se compara el payload completo
        # del mismo tick contra una copia con una decisión pendiente.
        args = activo.cli.parser().parse_args(["tick", "--repo", REPO, "--seco"])
        for inscripcion, tipo in (("sombra", "sombra-doble-camino"), ("activo", "sombra")):
            with self.subTest(inscripcion=inscripcion):
                self.inscritos.write_text(REPO + " " + inscripcion + "\n")
                with self.ledger() as ledger:
                    # Sólo se copian bases aisladas en el host de pruebas.
                    copia = self.raiz / ("antes-" + inscripcion + ".sqlite3")
                    conexion = sqlite3.connect(str(copia))
                    ledger.db.backup(conexion)
                    conexion.close()
                codigo, esperado = activo.cli.ejecutar(args, observador=self.contexto.observador, ahora=100)
                self.assertEqual(codigo, 0)
                with self.ledger() as ledger:
                    conexion = sqlite3.connect(str(copia))
                    conexion.backup(ledger.db)
                    conexion.close()
                self.inscritos.write_text(REPO + " activo\n")
                self.assertEqual(self.cli("pausa", "--motivo", "pendiente durante sombra")[0], 0)
                self.inscritos.write_text(REPO + " " + inscripcion + "\n")
                antes = self.estado()
                decisiones = self.decisiones()
                io = [self.lineas(n) for n in ("trabajos.log", "posts.log", "pushes.log")]
                codigo, salida = activo.cli.ejecutar(args, observador=self.contexto.observador, ahora=100)
                self.assertEqual(codigo, 0)
                self.assertEqual(salida, esperado)
                self.assertEqual(self.estado(), antes)
                self.assertEqual(self.decisiones(), decisiones)
                self.assertTrue(all(d["estado"] == "pendiente" for d in decisiones))
                self.assertEqual(io, [self.lineas(n) for n in ("trabajos.log", "posts.log", "pushes.log")])
                with self.ledger() as ledger:
                    eventos = ledger.db.execute("SELECT tipo, payload FROM events WHERE tipo=?", (tipo,)).fetchall()
                    self.assertEqual(len(eventos), 1)
                    self.assertEqual(json.loads(eventos[0]["payload"]), esperado["plan"])
                    self.assertEqual(ledger.db.execute("SELECT count(*) FROM events WHERE tipo IN "
                        "('operativo', 'juicio-resuelto')").fetchone()[0], 0)
                    # Dejar la fixture inicial para la otra variante, sin borrar.
                    conexion = sqlite3.connect(str(copia))
                    conexion.backup(ledger.db)
                    conexion.close()

    def test_muerte_reintento_promocion_con_ack_perdido_aterriza_una_vez(self):
        self.hasta_fase(Certificado)
        certificado = self.estado().trenes[0].fase
        (self.raiz / "rechazar-push").write_text("rechazar")
        self.assertEqual(self.hasta_pausa().pausa.motivo, "promocion-rechazada")
        self.juicio("reanudar")
        (self.raiz / "rechazar-push").rename(self.raiz / "rechazo-retirado")
        self.comando(self.origen, "config", "core.logAllRefUpdates", "true")
        hijo = self.hijo('''
from test_activo import Contexto
c = Contexto(sys.argv[3], "durante-promover")
c.tick(8000)
sys.exit(9)
''')
        salida, error = hijo.communicate(timeout=60)
        self.assertEqual(hijo.returncode, 77, salida + error)
        self.assertEqual(self.remoto(), certificado.C)
        self.assertIsInstance(self.estado().trenes[0].fase, Promoviendo)
        self.assertEqual(self.estado().trenes[0].fase.intentos, 4)
        with self.ledger() as ledger:
            promociones = [r for r in ledger.outbox(REPO) if json.loads(r["payload"])["tipo"] == "promover"]
            self.assertEqual(len(promociones), 1)
            self.assertEqual(promociones[0]["estado"], "ejecutando")
            self.assertIsNone(promociones[0]["resultado"])
        self.assertEqual(self.decisiones()[0]["estado"], "aplicada")
        self.aterrizar_ultimo()
        for n in range(3):
            self.contexto.tick(15000 + n)
        self.assertEqual([t.fase for t in self.estado().trenes], [Terminal("ATERRIZADO")])
        self.assertEqual(len(self.lineas("trabajos.log")), 1)
        self.assertEqual(sum(p.endswith(":refs/heads/main") for p in self.lineas("pushes.log")), 1)
        self.assertEqual(len(self.comando(self.origen, "reflog", "show", "--format=%H", "main").splitlines()), 1)
        with self.ledger() as ledger:
            self.assertEqual(ledger.outbox(REPO), [])
            self.assertEqual(ledger.db.execute("SELECT count(*) FROM events WHERE tipo='juicio-resuelto'").fetchone()[0], 1)


class TablaTests(unittest.TestCase):
    def test_cada_pausa_y_detalle_del_reductor_tiene_fila(self):
        raices = [ast.parse((activo.RAIZ / nombre).read_text())
                  for nombre in ("dominio.py", "atribucion.py")]

        def literal_o_prefijo(nodo):
            if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
                return nodo.value
            if isinstance(nodo, ast.BinOp) and isinstance(nodo.op, ast.Add):
                prefijo = literal_o_prefijo(nodo.left)
                self.assertTrue(prefijo.endswith(":"), ast.dump(nodo))
                return prefijo
            self.fail("Detalle sin cobertura explícita: " + ast.dump(nodo))

        inciertos = {literal_o_prefijo(k.value) for raiz in raices for nodo in ast.walk(raiz)
                     if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name)
                     and nodo.func.id == "Plan" for k in nodo.keywords if k.arg == "incierto"}
        esperadas = set()
        for raiz in raices:
            for nodo in ast.walk(raiz):
                if not (isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name)
                        and nodo.func.id == "Pausa"):
                    continue
                motivo = ast.literal_eval(nodo.args[0])
                detalle = next((k.value for k in nodo.keywords if k.arg == "detalle"), None)
                if detalle is None:
                    detalles = {""}
                elif isinstance(detalle, ast.Attribute) and ast.dump(detalle) == ast.dump(
                        ast.parse("plan.incierto", mode="eval").body):
                    self.assertEqual(motivo, "necesita-juicio")
                    self.assertTrue(inciertos)
                    detalles = inciertos
                else:
                    detalles = {literal_o_prefijo(detalle)}
                esperadas.update((motivo, valor) for valor in detalles)
        self.assertTrue({"etapa-1", "necesita-juicio", "infraestructura"} <= {m for m, _ in esperadas})
        self.assertEqual(set(POLITICA), esperadas | {("manual", "")})
        readme = (activo.RAIZ / "README.md").read_text()
        for motivo, detalle in sorted(esperadas):
            with self.subTest(motivo=motivo, detalle=detalle):
                self.assertIn("`" + motivo + "`", readme)
                if detalle:
                    self.assertIn("`" + detalle + ("<unidad>" if detalle.endswith(":") else "") + "`", readme)
                real = detalle + "unidad" if detalle.endswith(":") else detalle
                self.assertEqual(politica(Pausa(motivo, detalle=real)), POLITICA[(motivo, detalle)])
                self.assertTrue(POLITICA[(motivo, detalle)])
        self.assertEqual(politica(Pausa("motivo-futuro")), {})
        self.assertEqual(politica(Pausa("necesita-juicio", detalle="detalle-futuro")), {})


if __name__ == "__main__":
    unittest.main()
