"""U15 R1: pausa con promoción aterrizada y ACK pendiente o perdido, git real."""
import json
import unittest

import test_activo as activo
import test_juicio as juicio
from dominio import Admitido, Certificado, Pausa, Promoviendo, Rechazado, Terminal

REPO = activo.REPO


class ContextoPausa(unittest.TestCase):
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
    posts = juicio.JuicioTests.posts
    hasta_pausa = juicio.JuicioTests.hasta_pausa
    aterrizar_ultimo = juicio.JuicioTests.aterrizar_ultimo
    juicio = juicio.JuicioTests.juicio

    def promocion_pendiente(self):
        with self.ledger() as ledger:
            return [r for r in ledger.outbox(REPO)
                    if json.loads(r["payload"])["tipo"] == "promover"]


class RegresionesTests(ContextoPausa):
    def pausar_y_aterrizar(self, estado_outbox):
        antes = self.estado()
        cert = antes.trenes[0].fase.certificado
        self.assertEqual(self.remoto(), cert.C)
        self.assertTrue(all(isinstance(e.admision, Admitido) for e in antes.entradas))
        pendiente = self.promocion_pendiente()
        self.assertEqual(len(pendiente), 1)
        self.assertEqual(pendiente[0]["estado"], estado_outbox)
        self.assertEqual(pendiente[0]["resultado"] is None, estado_outbox == "ejecutando")
        io = [self.lineas(n) for n in ("pushes.log", "trabajos.log")]
        self.assertEqual(self.cli("pausa", "--motivo", "mantenimiento")[0], 0)
        for ahora in (3000, 3100, 3200):
            self.contexto.tick(ahora)
            pausado = self.estado()
            self.assertEqual(pausado.pausa, Pausa("manual", detalle="mantenimiento"))
            self.assertEqual(pausado.trenes, antes.trenes)
            self.assertEqual(pausado.entradas, antes.entradas)
            self.assertEqual(self.posts("/comments"), [])
            self.assertEqual(self.promocion_pendiente(), pendiente)
            self.assertEqual(io, [self.lineas(n) for n in ("pushes.log", "trabajos.log")])
        self.assertEqual(self.cli("pausa", "--levantar", "--motivo", "seguir")[0], 0)
        self.contexto.tick(3300)
        final = self.aterrizar_ultimo()
        self.assertEqual([t.fase for t in final.trenes], [Terminal("ATERRIZADO")])
        self.assertEqual(final.entradas, antes.entradas)
        for ahora in (15000, 15100, 15200):
            self.contexto.tick(ahora)
        self.assertEqual(self.estado(), final)
        self.assertEqual(self.remoto(), cert.C)
        self.assertEqual(self.posts("/comments"), [])
        self.assertEqual([p["payload"]["state"] for p in self.posts("/statuses/")],
                         ["pending"] * 3 + ["success"] * 3)
        self.assertEqual(io, [self.lineas(n) for n in ("pushes.log", "trabajos.log")])
        self.assertEqual(sum(p.endswith(":refs/heads/main") for p in self.lineas("pushes.log")), 1)
        self.assertEqual(len(self.lineas("trabajos.log")), 1)
        self.assertEqual(len(self.comando(self.origen, "reflog", "show", "--format=%H", "main").splitlines()), 1)
        with self.ledger() as ledger:
            self.assertEqual(ledger.outbox(REPO), [])
            efectos = [json.loads(r[0]) for r in ledger.db.execute("SELECT payload FROM effects")]
            self.assertFalse(any(e["tipo"] == "avisar-caducidad" for e in efectos))

    def test_pausa_entre_push_y_consumo_del_ack_guardado(self):
        self.comando(self.origen, "config", "core.logAllRefUpdates", "true")
        self.hasta_fase(Promoviendo)
        self.pausar_y_aterrizar("resultado")

    def test_pausa_tras_muerte_despues_del_push_sin_registrar_ack(self):
        self.comando(self.origen, "config", "core.logAllRefUpdates", "true")
        self.hasta_fase(Certificado)
        hijo = self.hijo('''
from test_activo import Contexto
c = Contexto(sys.argv[3], "durante-promover")
c.tick(2000)
sys.exit(9)
''')
        salida, error = hijo.communicate(timeout=60)
        self.assertEqual(hijo.returncode, 77, salida + error)
        self.assertIsInstance(self.estado().trenes[0].fase, Promoviendo)
        self.pausar_y_aterrizar("ejecutando")


class LimitesTests(ContextoPausa):
    def caducidad_real(self, cerrado):
        self.hasta_fase(Certificado)
        (self.raiz / "rechazar-push").write_text("rechazar")
        antes = self.hasta_pausa()
        self.assertEqual(antes.pausa.motivo, "promocion-rechazada")
        self.assertIsInstance(antes.trenes[0].fase, Promoviendo)
        # El reductor consumió EnBase antes de pausar: no queda ACK pendiente.
        self.assertEqual(self.promocion_pendiente(), [])
        if cerrado:
            (self.raiz / "pr-cerrado-1").write_text("cerrado sin merge")
        else:
            nuevo = self.commit({"comun": "base\n", "nuevo": "head\n"},
                                (self.miembros[0][1],))
            self.push(nuevo, "refs/pull/1/head")
            self.contexto.config["miembros"][0][1] = nuevo
        for ahora in (9000, 9100):
            self.contexto.tick(ahora)
            self.assertEqual(self.estado().trenes, antes.trenes)
            self.assertEqual(self.estado().pausa, antes.pausa)
            self.assertIsInstance(self.estado().entradas[0].admision, Rechazado)
            self.assertEqual(len(self.posts("/comments")), 1)
        self.juicio("reanudar")
        self.contexto.tick(9200)
        self.contexto.tick(9300)
        despues = self.estado()
        self.assertEqual(despues.pausa.motivo, "head-cambiado-en-tren")
        self.assertIsInstance(despues.entradas[0].admision, Rechazado)
        self.assertTrue(all(isinstance(e.admision, Admitido) for e in despues.entradas[1:]))
        self.assertEqual(len(self.posts("/comments")), 1)
        self.assertEqual([p["payload"]["state"] for p in self.posts("/statuses/")],
                         ["pending"] * 3 + ([] if cerrado else ["failure"]))
        if not cerrado:
            self.assertTrue(self.posts("/statuses/")[-1]["endpoint"].endswith(nuevo))
        # Un PR cerrado recibe comentario, sin status: ése es el contrato U14.
        with self.ledger() as ledger:
            self.assertEqual(ledger.outbox(REPO), [])
        self.assertEqual(self.remoto(), self.B)
        self.assertEqual(sum(p.endswith(":refs/heads/main") for p in self.lineas("pushes.log")), 0)
        self.assertEqual(len(self.lineas("trabajos.log")), 1)

    def test_pausa_con_enbase_consumido_publica_caducidad_real_sin_esperar_juicio(self):
        self.caducidad_real(cerrado=False)

    def test_pr_cerrado_caduca_con_comentario_sin_status(self):
        self.caducidad_real(cerrado=True)


if __name__ == "__main__":
    unittest.main()
