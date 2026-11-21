"""U14 R1: regresiones reproducibles sobre 49fd41dc, sólo en host remoto."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import subprocess
import unittest
from unittest.mock import patch

import test_activo as activo
import test_feedback as feedback
from dominio import Entrega, Motivo, Pausa, Retenido, Terminal, clave, desde_datos, paso


@contextmanager
def contexto():
    caso = feedback.FeedbackTests()
    try:
        caso.setUp()
        yield caso
    finally:
        caso.doCleanups()


def tick_sin_base(c, ahora):
    observar = c.contexto.observador.observar
    with patch.object(c.contexto.observador, "observar",
                      side_effect=lambda *args: replace(observar(*args), base="")):
        c.contexto.tick(ahora)


class U14R1Tests(unittest.TestCase):
    def reentregar(self, c, pr):
        anterior = dict(c.miembros)[pr]
        nuevo = c.commit({"comun": "base\n", "pr-{}".format(pr): "ticket\n",
                          "nuevo": "head\n"}, (anterior,))
        c.push(nuevo, "refs/pull/{}/head".format(pr))
        c.contexto.config["miembros"][pr - 1][1] = nuevo
        tick_sin_base(c, 100)
        self.assertEqual([p["payload"]["state"] for p in self.statuses(c, nuevo)], ["failure"])
        apto = c.raiz / ("apto-{}.json".format(pr))
        dirigida = c.raiz / ("dirigida-{}.json".format(pr))
        apto.write_text(json.dumps({"head": nuevo, "veredicto": "APTO", "hallazgos_abiertos": 0}))
        dirigida.write_text(json.dumps({"head": nuevo, "resultado": "limpio"}))
        with c.ledger() as ledger:
            ledger.entregar(Entrega(activo.REPO, pr, nuevo, str(apto), str(dirigida)), 101)
        c.contexto.tick(102)
        c.contexto.tick(4000)
        self.assertEqual([p["payload"]["state"] for p in self.statuses(c, nuevo)],
                         ["failure", "pending"])
        self.assertIn(c.estado().trenes[-1].id, self.statuses(c, nuevo)[-1]["payload"]["description"])
        return nuevo

    def statuses(self, c, head):
        return [p for p in c.posts("/statuses/") if p["endpoint"].endswith(head)]

    def test_caducidad_reentrega_h2_pending_y_success_al_aterrizar(self):
        with contexto() as c:
            nuevo = self.reentregar(c, 1)
            final = c.terminar()
            self.assertEqual(final.trenes[-1].fase, Terminal("ATERRIZADO"))
            self.assertEqual(c.comando(c.origen, "merge-base", nuevo, c.remoto()), nuevo)
            self.assertEqual([p["payload"]["state"] for p in self.statuses(c, nuevo)],
                             ["failure", "pending", "success"])
            self.assertEqual(len(c.posts("/comments")), 1)  # sólo la caducidad

    def test_caducidad_reentrega_h2_pending_y_failure_nuevo_si_rojo(self):
        with contexto() as c:
            nuevo = self.reentregar(c, 2)
            (c.raiz / "producto-rojo").write_text("segundo PR causal")
            c.contexto.config["costo_suite_sana_s"] = 10000
            for n in range(30):
                c.contexto.tick(4100 + n * 120)
                if c.estado().trenes[0].fase == Terminal("DEVUELTO"):
                    break
            self.assertEqual(c.estado().trenes[0].fase, Terminal("DEVUELTO"))
            statuses = self.statuses(c, nuevo)
            self.assertEqual([p["payload"]["state"] for p in statuses],
                             ["failure", "pending", "failure"])
            self.assertNotEqual(statuses[0]["payload"]["target_url"],
                                statuses[-1]["payload"]["target_url"])

    def muerte_despues_de_paso(self, tipo):
        with contexto() as c:
            hijo = c.hijo('''
from test_activo import Contexto, REPO
from dominio import Terminal
from ledger import Ledger
from contextlib import closing
import os
c = Contexto(sys.argv[3])
def frontera(nombre, efecto):
    if nombre != 'paso-guardado':
        return
    with closing(Ledger()) as ledger:
        estado = ledger.estado(REPO)
    if sys.argv[4] == 'avisar-en-tren' or estado.trenes[-1].fase == Terminal('ATERRIZADO'):
        os._exit(77)
for n in range(15):
    c.tick(100 + n * 120, frontera)
sys.exit(9)
''', tipo)
            salida, error = hijo.communicate(timeout=90)
            self.assertEqual(hijo.returncode, 77, salida + error)
            filas = [f for f in c.filas() if json.loads(f["payload"])["tipo"] == tipo]
            self.assertEqual(len(filas), 3)
            self.assertTrue(all(f["estado"] == "pendiente" for f in filas))
            valor = "pending" if tipo == "avisar-en-tren" else "success"
            self.assertFalse(any(p["payload"]["state"] == valor for p in c.posts("/statuses/")))
            # Drena incluso con el carril pausado y con un tren terminal o ausente.
            with c.ledger() as ledger:
                estado = ledger.estado(activo.REPO)
                if tipo == "avisar-en-tren":
                    estado = replace(estado, trenes=())
                ledger.guardar_paso(replace(estado, pausa=Pausa("necesita-juicio")), (), 1900)
            c.contexto.tick(2000)
            c.contexto.tick(2100)
            publicados = [p for p in c.posts("/statuses/") if p["payload"]["state"] == valor]
            self.assertEqual(len(publicados), 3)
            self.assertEqual(c.posts("/comments"), [])
            for f in c.filas():
                if json.loads(f["payload"])["tipo"] != tipo:
                    continue
                self.assertEqual(f["estado"], "completo")
                resultado = json.loads(f["resultado"])
                self.assertIsNone(resultado["comentario_url"])
                self.assertNotIn("observacion", resultado)
            self.assertTrue(all("target_url" not in p["payload"] for p in publicados))

    def test_muerte_tras_guardar_pending_publica_una_vez_sin_tren_y_con_pausa(self):
        self.muerte_despues_de_paso("avisar-en-tren")

    def test_muerte_tras_guardar_success_publica_una_vez_terminal_y_con_pausa(self):
        self.muerte_despues_de_paso("avisar-aterrizaje")

    def test_firma_privada_solo_digest_en_comentario_ledger_conserva_original(self):
        firma = "AssertionError: token=SYNTHETIC_ONLY path=/private/example/credentials.json"
        with contexto() as c, patch.object(activo, "F", replace(activo.F, firma=firma)):
            activo.ActivoTests.test_rojo_atribuye_repite_subconjuntos_y_aterriza_sobrevivientes(c)
            cuerpo = c.posts("/comments")[0]["payload"]["body"]
            self.assertNotIn("SYNTHETIC_ONLY", cuerpo)
            self.assertNotIn("/private/example/credentials.json", cuerpo)
            self.assertIn("sha256:" + hashlib.sha256(firma.encode("utf-8")).hexdigest()[:16], cuerpo)
            self.assertIn(activo.F.unidad, cuerpo)
            self.assertIn(activo.F.check, cuerpo)
            with c.ledger() as ledger:
                payload = ledger.db.execute("SELECT payload FROM effects WHERE payload LIKE '%devolver%'").fetchone()[0]
            self.assertEqual(json.loads(json.loads(payload)["datos"][2])["fallos"][0]["firma"], firma)

    def test_sobrevivientes_pending_con_clave_nueva_por_generacion(self):
        with contexto() as c:
            activo.ActivoTests.test_rojo_atribuye_repite_subconjuntos_y_aterriza_sobrevivientes(c)
            primero, segundo = c.estado().trenes
            for pr in (1, 3):
                head = dict(c.miembros)[pr]
                statuses = self.statuses(c, head)
                self.assertEqual([p["payload"]["state"] for p in statuses],
                                 ["pending", "pending", "success"])
                self.assertIn(primero.id, statuses[0]["payload"]["description"])
                self.assertIn(segundo.id, statuses[1]["payload"]["description"])
            for fila in c.filas():
                efecto = desde_datos(json.loads(fila["payload"]))
                if efecto.tipo in ("avisar-en-tren", "avisar-aterrizaje"):
                    self.assertEqual(efecto.clave, clave(efecto.tipo, *efecto.datos))
            # El paso puro no vuelve a emitir un status ya asentado en el estado.
            estado = c.estado()
            obs = c.contexto.observador.observar(estado, 10000)
            self.assertEqual(paso(estado, obs), (estado, ()))


class U14R1InvariantesTests(unittest.TestCase):
    def test_eventos_sombra_historicos_y_gate_pending_sin_cambios(self):
        with contexto() as c:
            args = activo.cli.parser().parse_args(["tick", "--repo", activo.REPO, "--seco"])
            _, salida = activo.cli.ejecutar(args, c.contexto.observador, 100)
            self.assertEqual([e["tipo"] for e in salida["plan"]["efectos"]], ["armar-candidato"])
            with c.ledger() as ledger:
                evento = ledger.db.execute("SELECT payload FROM events WHERE tipo='sombra'").fetchone()[0]
                self.assertEqual(json.loads(evento), salida["plan"])
                self.assertEqual(ledger.outbox(activo.REPO), [])
            c.contexto.tick(100)
            self.assertEqual(len(c.posts("/statuses/")), 3)
            c.inscritos.write_text(activo.REPO + " sombra\n")
            sombra = activo.GitHubGit(c.contexto.config, ejecutar=c.contexto.gh, sombra=True)
            _, salida = activo.cli.ejecutar(args, sombra, 101)
            simulado = desde_datos(salida["plan"]["estado"])
            self.assertFalse(simulado.trenes)
            self.assertTrue(all(e.admision == Retenido(Motivo.CI) for e in simulado.entradas))
            self.assertEqual(salida["plan"]["efectos"], [])
            self.assertEqual(len(c.posts()), 3)

    def test_pending_retrasado_no_tapa_success_y_no_bloquea_otros_pr(self):
        with contexto() as c:
            head = c.miembros[0][1]
            original = c.contexto.escritor.ejecutar

            def fallar_pending(argv, **kwargs):
                if (argv[3] == "POST" and argv[4].endswith("/statuses/" + head) and
                        json.loads(kwargs["input"])["state"] == "pending"):
                    raise subprocess.CalledProcessError(1, argv)
                return original(argv, **kwargs)

            c.contexto.escritor.ejecutar = fallar_pending
            final = c.terminar()
            self.assertEqual(final.trenes[0].fase, Terminal("ATERRIZADO"))
            self.assertEqual([p["payload"]["state"] for p in c.posts("/statuses/")],
                             ["pending"] * 2 + ["success"] * 2)
            self.assertEqual([p for p in c.posts("/statuses/") if p["endpoint"].endswith(head)], [])
            c.contexto.escritor.ejecutar = original
            c.contexto.tick(9000)
            c.contexto.tick(9100)
            self.assertEqual([p["payload"]["state"] for p in c.posts("/statuses/")
                              if p["endpoint"].endswith(head)], ["pending", "success"])
            with c.ledger() as ledger:
                self.assertEqual(ledger.outbox(activo.REPO), [])

    def test_seco_entregar_sombra_sin_posts_con_status_nuevo_pendiente(self):
        with contexto() as c:
            with c.ledger() as ledger:
                estado = ledger.estado(activo.REPO)
                obs = c.contexto.observador.observar(estado, 100)
                nuevo, efectos = paso(estado, obs)
                ledger.guardar_paso(nuevo, efectos, 100)
            args = activo.cli.parser().parse_args(["tick", "--repo", activo.REPO, "--seco"])
            for modo in ("activo", "sombra"):
                c.inscritos.write_text(activo.REPO + " " + modo + "\n")
                self.assertEqual(activo.cli.ejecutar(args, c.contexto.observador, 200,
                    escritor=c.contexto.escritor)[0], 0)
                entregar = activo.cli.parser().parse_args(["entregar", "--repo", activo.REPO,
                    "--pr", "1", "--head", c.miembros[0][1], "--apto", str(c.raiz / "apto-1.json"),
                    "--dirigida", str(c.raiz / "dirigida-1.json")])
                self.assertEqual(activo.cli.ejecutar(entregar, c.contexto.observador, 300,
                    escritor=c.contexto.escritor)[0], 0)
            self.assertEqual(c.posts(), [])
            self.assertTrue(all(f["estado"] == "pendiente" for f in c.filas()))


if __name__ == "__main__":
    unittest.main()
