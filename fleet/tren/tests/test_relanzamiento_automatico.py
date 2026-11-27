"""Relanzamientos por lote a través del reductor y del ledger."""
from dataclasses import replace
import json

from test_tren import Aislado, estado, observacion
from dominio import (Completo, Corriendo, Decision, Diagnosticando, Efecto, EsperandoCI,
                     Pausa, RechazoLanzador, Tren, clave, intento_suite, paso)
from test_hasito import CANDIDATO
from test_jale import FALLO
from test_sen_7120 import evidencia


class RelanzamientoAutomaticoTests(Aislado):
    def setUp(self):
        super().setUp()
        inicial = estado()
        self.tren = Tren("lote", (inicial.entradas[0].entrega.recibo,), EsperandoCI(CANDIDATO, desde=99))
        self.inicial = replace(inicial, trenes=(self.tren,))
        self.obs = observacion(tren_id=self.tren.id)
        self.suite = replace(self.obs, ci_candidato="verde", suite="suite.json")
        self.sin_recibo = replace(self.suite, resultado_suite=RechazoLanzador(
            "coordinador.log", "salida-sin-recibo"))

    def test_suite_sin_recibo_reengancha_el_mismo_intento(self):
        nuevo, (registro, efecto) = paso(self.inicial, self.sin_recibo)
        tren, = nuevo.trenes
        self.assertIsNone(nuevo.pausa)
        self.assertIsInstance(tren.fase, Corriendo)
        self.assertEqual(intento_suite(tren.fase), 0)
        self.assertEqual(tren.fase.reenganches, 1)
        self.assertEqual(tren.fase.registro_reenganche, "coordinador.log")
        self.assertEqual(tren.relanzamientos_automaticos, 1)
        self.assertEqual(efecto.tipo, "reanudar-suite")
        self.assertEqual(efecto.datos, (tren.id, "0", "1", "coordinador.log"))
        self.assertEqual(registro.tipo, "registrar-relanzamiento-automatico")
        self.assertEqual(paso(nuevo, self.obs), (nuevo, (efecto,)))

    def test_presupuesto_del_lote_sobrevive_a_fases_y_evidencias(self):
        en_suite, _ = paso(self.inicial, self.sin_recibo)
        en_diagnostico, _ = paso(en_suite, replace(self.suite,
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

    def test_relanzamiento_diferido_conserva_resultado_en_el_buzon(self):
        from observador import Falso
        from operativo import resultado_observacion, tick

        ledger = self.ledger()
        repo = self.inicial.repo
        iniciar = Efecto(clave("suite", self.tren.id), "iniciar-suite", (self.tren.id,))
        for entrada in self.inicial.entradas:
            ledger.entregar(entrada.entrega, entrada.recibida)
        ledger.guardar_paso(self.inicial, (iniciar,), 98)
        ledger.registrar_efecto(repo, iniciar, 99,
            {"observacion": resultado_observacion(iniciar, self.sin_recibo)}, True)
        tick(ledger, repo, Falso(replace(self.obs, prs=())), 100, object())
        retenido = ledger.estado(repo, operativo=True)
        self.assertIsNone(retenido.pausa)
        self.assertEqual(retenido.trenes, (self.tren,))
        self.assertFalse(any(e["tipo"] == "relanzamiento-automatico"
                             for e in ledger.ultimos_eventos(repo)))
        fila, = ledger.outbox(repo)
        self.assertEqual((fila["clave"], fila["estado"]), (iniciar.clave, "resultado"))

        class PasoGuardado(Exception):
            pass

        def detener(nombre, efecto):
            if nombre == "paso-guardado":
                raise PasoGuardado

        with self.assertRaises(PasoGuardado):
            tick(ledger, repo, Falso(self.obs), 101, object(), frontera=detener)
        nuevo = ledger.estado(repo, operativo=True)
        self.assertEqual(nuevo.trenes[0].relanzamientos_automaticos, 1)
        self.assertEqual(nuevo.trenes[0].fase.registro_reenganche, "coordinador.log")
        evento, = [e for e in ledger.ultimos_eventos(repo)
                   if e["tipo"] == "relanzamiento-automatico"]
        self.assertEqual(json.loads(evento["payload"])["numero"], 1)
        fila, = ledger.outbox(repo)
        self.assertEqual((fila["estado"], json.loads(fila["payload"])["tipo"]),
                         ("pendiente", "reanudar-suite"))
