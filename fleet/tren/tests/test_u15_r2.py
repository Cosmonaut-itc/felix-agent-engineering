"""U15 R2: caducidad real con promoción resuelta, CLI y origen git real."""
import json
import unittest

from test_u15_r1 import ContextoPausa
from dominio import Admitido, Certificado, Motivo, Promoviendo, Rechazado, Terminal


class RegresionesTests(ContextoPausa):
    def pausa_resuelta(self, head_cambiado=False):
        self.hasta_fase(Certificado)
        (self.raiz / "rechazar-push").write_text("rechazar")
        if head_cambiado:
            self.hasta_fase(Promoviendo)
            nuevo = self.commit({"comun": "base\n", "nuevo": "head\n"},
                                (self.miembros[0][1],))
            self.push(nuevo, "refs/pull/1/head")
            self.contexto.config["miembros"][0][1] = nuevo
        antes = self.hasta_pausa()
        self.assertEqual(antes.pausa.motivo,
                         "head-cambiado-en-tren" if head_cambiado else "promocion-rechazada")
        self.assertIsInstance(antes.trenes[0].fase, Promoviendo)
        self.assertTrue(antes.trenes[0].fase.reintento_pendiente)
        self.assertEqual(self.promocion_pendiente(), [])
        self.assertEqual(self.remoto(), self.B)
        return antes

    def caducidad_cerrada(self, indice):
        pr, _ = self.miembros[indice]
        comentarios = self.posts("/issues/{}/comments".format(pr))
        self.assertEqual(len(comentarios), 1, "El cierre durante la pausa debe conservar su aviso")
        texto = comentarios[0]["payload"]["body"]
        self.assertIn("caducó: pr-no-abierto", texto)
        self.assertNotIn("decisión de prueba", texto)
        self.assertNotIn("abortar", texto)
        entrada = self.estado().entradas[indice]
        self.assertEqual(entrada.admision, Rechazado(Motivo.CERRADO))
        # Sólo el pending inicial; el cierre no publica failure ni otro pending.
        self.assertEqual([p["payload"]["state"] for p in
                          self.posts("/statuses/" + entrada.entrega.head)], ["pending"])
        with self.ledger() as ledger:
            efectos = [json.loads(r[0]) for r in ledger.db.execute("SELECT payload FROM effects")]
            avisos = [e for e in efectos if e["tipo"] == "avisar-caducidad"
                      and e["datos"] == [entrada.entrega.recibo]]
            self.assertEqual(len(avisos), 1)
            self.assertFalse(any(r["clave"] == avisos[0]["clave"] for r in ledger.outbox(entrada.entrega.repo)))

    def cerrar_y_abortar(self, head_cambiado=False):
        antes = self.pausa_resuelta(head_cambiado)
        indice = 1 if head_cambiado else 0
        pr, _ = self.miembros[indice]
        (self.raiz / ("pr-cerrado-" + str(pr))).write_text("cerrado sin merge")
        io = [self.lineas(n) for n in ("pushes.log", "trabajos.log")]
        self.contexto.tick(9000)
        pausado = self.estado()
        self.assertEqual(pausado.pausa, antes.pausa)
        self.assertEqual(pausado.trenes, antes.trenes)
        self.assertEqual(io, [self.lineas(n) for n in ("pushes.log", "trabajos.log")])
        self.juicio("abortar")
        for ahora in (9200, 9300, 9400, 9500):
            self.contexto.tick(ahora)
        final = self.estado()
        self.assertEqual(final.trenes[0].fase, Terminal("ABORTADO"))
        self.assertIsNone(final.entradas[indice].tren)
        self.assertEqual(self.remoto(), self.B)
        self.caducidad_cerrada(indice)
        self.assertEqual(pausado.entradas[indice].admision, Rechazado(Motivo.CERRADO))
        self.assertEqual(len(self.posts("/comments")), 2 if head_cambiado else 1)
        with self.ledger() as ledger:
            self.assertFalse(any(json.loads(r[0])["tipo"] == "devolver"
                                 for r in ledger.db.execute("SELECT payload FROM effects")))

    def test_cierre_en_promocion_rechazada_y_abortar_conserva_caducidad(self):
        self.cerrar_y_abortar()

    def test_cierre_en_head_cambiado_y_abortar_conserva_caducidad(self):
        self.cerrar_y_abortar(head_cambiado=True)

    def test_cierre_en_head_cambiado_y_expulsar_excluye_caducado(self):
        antes = self.pausa_resuelta(head_cambiado=True)
        (self.raiz / "pr-cerrado-2").write_text("cerrado sin merge")
        for ahora in (9000, 9100):
            self.contexto.tick(ahora)
        self.juicio("expulsar", 1)
        self.contexto.tick(9200)
        final = self.estado()
        self.assertEqual(final.trenes[0].fase, Terminal("DEVUELTO"))
        self.assertEqual(final.trenes[1].recibos, (antes.entradas[2].entrega.recibo,))
        self.assertIsInstance(final.entradas[2].admision, Admitido)
        self.caducidad_cerrada(1)
        # No reintroducir ni volver a avisar al cerrado al armar la generación.
        for ahora in (9300, 9400, 9500):
            self.contexto.tick(ahora)
        self.caducidad_cerrada(1)
        self.assertEqual(len(self.posts("/comments")), 3)  # head, cierre y expulsión
        self.assertEqual(self.remoto(), self.B)


if __name__ == "__main__":
    unittest.main()
