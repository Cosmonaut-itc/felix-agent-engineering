"""Relanzamientos por lote a través del reductor y del ledger."""
from dataclasses import replace
import json

from test_tren import Aislado, estado, observacion
from dominio import (Completo, Corriendo, Decision, Diagnosticando, EsperandoCI,
                     Pausa, RechazoLanzador, Tren, intento_suite, paso)
from test_hasito import CANDIDATO
from test_jale import FALLO
from test_sen_7120 import evidencia


class RelanzamientoAutomaticoTests(Aislado):
    def setUp(self):
        super().setUp()
        inicial = estado()
        self.tren = Tren("lote", (inicial.entradas[0].entrega.recibo,), EsperandoCI(CANDIDATO))
        self.inicial = replace(inicial, trenes=(self.tren,))
        self.obs = observacion(tren_id=self.tren.id)
        self.suite = replace(self.obs, ci_candidato="verde", suite="suite.json")
        self.sin_recibo = replace(self.suite, resultado_suite=RechazoLanzador(
            "coordinador.log", "salida-sin-recibo"))

    def test_suite_sin_recibo_relanzada_con_otro_intento(self):
        nuevo, (registro, efecto) = paso(self.inicial, self.sin_recibo)
        tren, = nuevo.trenes
        self.assertIsNone(nuevo.pausa)
        self.assertIsInstance(tren.fase, Corriendo)
        self.assertEqual(intento_suite(tren.fase), 1)
        self.assertEqual(tren.relanzamientos_automaticos, 1)
        self.assertEqual(efecto.tipo, "reanudar-suite")
        self.assertEqual(efecto.datos, (tren.id, "1"))
        self.assertEqual(registro.tipo, "registrar-relanzamiento-automatico")
        self.assertEqual(paso(nuevo, self.sin_recibo), (nuevo, (efecto,)))

    def test_presupuesto_del_lote_sobrevive_a_fases_y_evidencias(self):
        en_suite, _ = paso(self.inicial, self.sin_recibo)
        en_diagnostico, _ = paso(en_suite, replace(self.suite, suite_intento=1,
            resultado_suite=Completo("rojo", (FALLO,)), costo_suite_sana_s=1000))
        fase = en_diagnostico.trenes[0].fase
        self.assertIsInstance(fase, Diagnosticando)
        pedido = fase.pendiente
        interrumpida = replace(self.obs, reproduccion_id=pedido.id,
            repeticion=RechazoLanzador("repeticion.log", "corrida-interrumpida"))
        relanzado, (_, efecto) = paso(en_diagnostico, interrumpida)
        tren, = relanzado.trenes
        self.assertIsNone(relanzado.pausa)
        self.assertEqual(tren.fase.pendiente, pedido)
        self.assertEqual(intento_suite(tren.fase), 1)
        self.assertEqual(tren.relanzamientos_automaticos, 2)
        self.assertEqual(efecto.tipo, "repetir")
        self.assertEqual(efecto.datos, (tren.id, pedido.id, "1"))

        con_evidencia, _ = paso(relanzado, replace(interrumpida, suite_intento=1,
            repeticion=evidencia(pedido, falla=True).resultado))
        tren, = con_evidencia.trenes
        self.assertEqual(intento_suite(tren.fase), 0)
        self.assertEqual(tren.relanzamientos_automaticos, 2)
        pausa = Pausa("infraestructura", detalle="salida-sin-recibo", archivos=("otra.log",))
        pausado, efectos = paso(con_evidencia, replace(self.obs,
            reproduccion_id=tren.fase.pendiente.id,
            repeticion=RechazoLanzador("otra.log", "salida-sin-recibo")))
        self.assertEqual((pausado.pausa, efectos), (pausa, ()))
        for accion, intento, reenganches in (("reanudar", 0, 1), ("absolver", 1, 0)):
            with self.subTest(accion=accion):
                decision = Decision(accion, pausado.repo, tren.id, accion, None,
                                    "revisé el log", self.obs.ahora, "pausa-1", pausa)
                resuelto, (juicio, efecto) = paso(pausado, replace(
                    self.obs, decision=decision, pausa_id="pausa-1"))
                tren_resuelto, = resuelto.trenes
                self.assertIsNone(resuelto.pausa)
                self.assertEqual(juicio.datos[1], "aplicada")
                self.assertEqual(efecto.tipo, "repetir")
                self.assertEqual(intento_suite(tren_resuelto.fase), intento)
                self.assertEqual(tren_resuelto.fase.reenganches, reenganches)
                self.assertEqual(tren_resuelto.relanzamientos_automaticos, 2)

    def test_rechazo_del_lanzador_pausa_en_el_primer_intento(self):
        pausado, efectos = paso(self.inicial, replace(self.suite,
            resultado_suite=RechazoLanzador("coordinador.log")))
        self.assertEqual(pausado.pausa, Pausa("infraestructura", detalle="lanzador-rechazo",
                                            archivos=("coordinador.log",)))
        self.assertEqual(efectos, ())
        self.assertEqual(pausado.trenes[0].relanzamientos_automaticos, 0)

    def test_snapshot_anterior_y_rastro_del_relanzamiento(self):
        ledger = self.ledger()
        repo = self.inicial.repo
        ledger.guardar_paso(self.inicial, (), 98)
        snapshot = json.loads(ledger.ultimos_eventos(repo)[0]["payload"])
        self.assertNotIn("relanzamientos_automaticos", snapshot["estado"]["trenes"][0])
        anterior = ledger.estado(repo)
        self.assertEqual(anterior.trenes[0].relanzamientos_automaticos, 0)
        nuevo, efectos = paso(anterior, self.sin_recibo)
        ledger.guardar_paso(nuevo, efectos, 100)
        self.assertEqual(ledger.estado(repo), nuevo)
        evento, = [evento for evento in ledger.ultimos_eventos(repo)
                   if evento["tipo"] == "relanzamiento-automatico"]
        self.assertEqual(json.loads(evento["payload"]), {
            "tren": self.tren.id, "numero": 1, "limite": 2,
            "detalle": "salida-sin-recibo", "evidencia": "coordinador.log",
        })
        fila, = ledger.outbox(repo)
        self.assertEqual(fila["clave"], efectos[-1].clave)

    def test_relanzamiento_espera_readmision(self):
        retenido, (registro,) = paso(self.inicial, replace(self.sin_recibo, prs=()))
        self.assertIsNone(retenido.pausa)
        self.assertEqual(registro.tipo, "registrar-relanzamiento-automatico")
        nuevo, (efecto,) = paso(retenido, self.obs)
        self.assertEqual(nuevo.trenes[0].relanzamientos_automaticos, 1)
        self.assertEqual(efecto.tipo, "reanudar-suite")
        self.assertEqual(efecto.datos, (self.tren.id, "1"))
