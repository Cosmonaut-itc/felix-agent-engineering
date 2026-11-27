"""CI de C en la promoción con remoto Git y transporte GitHub de prueba."""
import sys
from unittest.mock import patch

import test_u16_r1 as fixtures
from test_u16_r1 import ContextoR1, check, status
from dominio import Certificado, EsperandoCI, Incierto, PLAZO_CI, Pausa, Promoviendo, ResultadoCI, Terminal


class PromocionCITests(fixtures.CasosCI):
    rearmar_con_rojo = fixtures.RearmeCIRefTests.rearmar_con_rojo

    def verde_solicitud_cero(self, individual=False):
        inicio = self.individual() if individual else self.hasta_fase(EsperandoCI)
        tren = inicio.trenes[0]
        self.assertEqual(tren.fase.solicitud, 0)
        c = tren.fase.candidato.C
        datos = {"head": c, "checks": [dict(check("success"), head_sha=c)]}
        self.guardar_ci(datos)
        self.contexto = ContextoR1(self.ruta_contexto)
        return datos

    def certificar_solicitud_cero(self, individual=False):
        datos = self.verde_solicitud_cero(individual)
        self.certificar()
        return datos

    def certificar(self):
        self.contexto.tick(1020)
        self.contexto.tick(1021)
        self.assertIsInstance(self.estado().trenes[-1].fase, Certificado)

    def ci_status(self, datos, resultado, ahora):
        datos.setdefault("statuses", []).insert(0, status(resultado, ahora))
        self.guardar_ci(datos)

    def configurar_gancho(self):
        self.contexto.observador.config["pre_aterrizaje"] = {
            "comando": [sys.executable], "plazo_s": 10}

    def test_status_durante_la_espera_del_gancho_impide_aterrizar(self):
        _, tren, datos = self.rearmar_con_rojo()
        self.nueva(datos, tren, "success")
        self.configurar_gancho()
        self.certificar()
        with patch("preaterrizaje.invocar", return_value="esperar") as gancho:
            self.contexto.tick(1022)
            gancho.assert_called_once()
        self.assertIsInstance(self.estado().trenes[-1].fase, Promoviendo)
        self.assertEqual(self.remoto(), self.B)
        self.ci_status(datos, "failure", 1023)
        self.assertEqual(self.contexto.observador.ci_candidato(self.estado())[1].estado,
                         "pendiente")
        self.contexto.tick(1023)
        self.contexto.tick(1024)
        self.assertIsInstance(self.estado().trenes[-1].fase, Promoviendo)
        self.assertIsNone(self.estado().pausa)
        self.assertEqual(self.remoto(), self.B)

    def test_lee_el_ci_despues_de_que_el_gancho_devuelve_seguir(self):
        _, tren, datos = self.rearmar_con_rojo()
        self.nueva(datos, tren, "success")
        self.configurar_gancho()
        self.certificar()

        def invocar(_config, _solicitud, accion):
            if accion == "cancelar":
                return "cancelado"
            self.ci_status(datos, "pending", 1022)
            return "seguir"

        with patch("preaterrizaje.invocar", side_effect=invocar):
            self.contexto.tick(1022)
        self.assertIsInstance(self.estado().trenes[-1].fase, Promoviendo)
        self.assertEqual(self.remoto(), self.B)

    def test_recuperar_ci_no_hereda_el_plazo_del_gancho(self):
        datos = self.certificar_solicitud_cero()
        self.configurar_gancho()
        reloj = [1022]
        acciones = []

        def invocar(_config, _solicitud, accion):
            acciones.append(accion)
            if accion == "cancelar":
                return "cancelado"
            if reloj[0] == 1022:
                self.ci_status(datos, "pending", reloj[0])
            return "seguir"

        with patch("preaterrizaje.time.time", side_effect=lambda: reloj[0]), \
             patch("preaterrizaje.invocar", side_effect=invocar):
            self.contexto.tick(reloj[0])
            self.assertEqual(self.remoto(), self.B)
            reloj[0] = 1023
            self.contexto.tick(reloj[0])
            self.assertEqual(acciones, ["verificar", "cancelar"])
            reloj[0] = 2823
            self.ci_status(datos, "success", reloj[0])
            self.contexto.tick(reloj[0])
            reloj[0] += 1
            self.contexto.tick(reloj[0])
        self.assertIsNone(self.estado().pausa)
        self.assertEqual(self.estado().trenes[-1].fase, Terminal("ATERRIZADO"))
        self.assertEqual(self.remoto(), datos["head"])
        self.assertEqual(acciones, ["verificar", "cancelar", "verificar"])

    def test_solicitud_cero_pending_espera_y_vence_sin_publicar(self):
        datos = self.certificar_solicitud_cero()
        self.ci_status(datos, "pending", 1022)
        self.contexto.tick(1022)
        self.assertIsInstance(self.estado().trenes[-1].fase, Certificado)
        self.contexto.tick(1022 + PLAZO_CI - 1)
        self.assertIsNone(self.estado().pausa)
        self.contexto.tick(1022 + PLAZO_CI)
        self.assertEqual(self.estado().pausa, Pausa(
            "infraestructura", detalle="pre-aterrizaje:ci-sin-resultado"))
        self.assertEqual(self.remoto(), self.B)
        self.juicio("reanudar")
        self.contexto.tick(1023 + PLAZO_CI)
        self.assertIsNone(self.estado().pausa)
        self.assertIsInstance(self.estado().trenes[-1].fase, Certificado)
        self.assertEqual(self.remoto(), self.B)

    def test_recupera_suite_de_solicitud_cero_sin_autorizar_el_push(self):
        datos = self.verde_solicitud_cero()

        class Corte(Exception):
            pass

        def perder_ack(nombre, efecto):
            if nombre == "efecto-ejecutado" and efecto.tipo == "iniciar-suite":
                raise Corte

        with self.assertRaises(Corte):
            self.contexto.tick(1020, perder_ack)
        self.ci_status(datos, "pending", 1021)
        self.contexto.tick(1021)
        self.assertIsInstance(self.estado().trenes[-1].fase, Certificado)
        self.contexto.tick(1022)
        self.assertIsInstance(self.estado().trenes[-1].fase, Certificado)
        self.assertEqual(len(self.lineas("trabajos.log")), 1)
        self.assertEqual(self.remoto(), self.B)

    def test_solicitud_cero_failure_despues_de_certificar_rearma(self):
        datos = self.certificar_solicitud_cero()
        self.ci_status(datos, "failure", 1022)
        self.contexto.tick(1022)
        self.assertEqual(self.estado().trenes[-1].fase, Terminal("REARMAR"))
        self.assertIsNone(self.estado().pausa)
        self.assertEqual(self.remoto(), self.B)

    def test_rojo_de_c_rearma_aunque_el_ci_de_un_head_este_pendiente(self):
        datos = self.certificar_solicitud_cero()
        self.ci_status(datos, "failure", 1022)
        leer = self.contexto.observador.ci

        def ci(repo, sha, rama=None):
            if sha == self.miembros[0][1]:
                return ResultadoCI("pendiente")
            return leer(repo, sha, rama)

        with patch.object(self.contexto.observador, "ci", side_effect=ci):
            self.contexto.tick(1022)
        self.assertEqual(self.estado().trenes[-1].fase, Terminal("REARMAR"))
        self.assertEqual(self.remoto(), self.B)

    def test_sin_gancho_relee_ci_si_falla_entre_el_paso_y_el_push(self):
        datos = self.certificar_solicitud_cero()

        def publicar_status(nombre, efecto):
            if nombre == "antes-ejecutar" and efecto.tipo == "promover":
                self.ci_status(datos, "failure", 1022)

        self.contexto.tick(1022, publicar_status)
        self.assertIsInstance(self.estado().trenes[-1].fase, Promoviendo)
        self.assertEqual(self.remoto(), self.B)
        self.contexto.tick(1023)
        self.assertEqual(self.estado().trenes[-1].fase, Terminal("REARMAR"))
        self.assertEqual(self.remoto(), self.B)

    def test_verde_sostenido_aterriza_en_el_tick_de_promocion(self):
        datos = self.certificar_solicitud_cero()
        self.contexto.tick(1022)
        self.assertIsInstance(self.estado().trenes[-1].fase, Promoviendo)
        self.assertEqual(self.remoto(), datos["head"])
        self.contexto.tick(1023)
        self.assertEqual(self.estado().trenes[-1].fase, Terminal("ATERRIZADO"))

    def test_rojo_posterior_al_push_no_impide_reconciliar_un_ack_perdido(self):
        datos = self.certificar_solicitud_cero()

        class Corte(Exception):
            pass

        def perder_ack(nombre, efecto):
            if nombre == "efecto-ejecutado" and efecto.tipo == "promover":
                raise Corte

        with self.assertRaises(Corte):
            self.contexto.tick(1022, perder_ack)
        self.assertEqual(self.remoto(), datos["head"])
        self.ci_status(datos, "failure", 1023)
        self.contexto.tick(1023)
        self.assertEqual(self.estado().trenes[-1].fase, Terminal("ATERRIZADO"))
        self.assertEqual(sum(p.endswith(":refs/heads/main") for p in self.lineas("pushes.log")), 1)

    def test_rojo_con_api_atrasada_y_git_incierto_conserva_la_promocion(self):
        datos = self.certificar_solicitud_cero(individual=True)
        c = datos["head"]

        class Corte(Exception):
            pass

        def perder_ack(nombre, efecto):
            if nombre == "efecto-ejecutado" and efecto.tipo == "promover":
                raise Corte

        with self.assertRaises(Corte):
            self.contexto.tick(1022, perder_ack)
        self.assertEqual(self.remoto(), c)
        self.ci_status(datos, "failure", 1023)
        api_original = self.contexto.observador.api

        def api(endpoint, *args):
            if endpoint.endswith("/git/ref/heads/main"):
                return {"object": {"sha": self.B}}
            return api_original(endpoint, *args)

        with patch.object(self.contexto.observador, "api", side_effect=api), \
             patch.object(self.contexto.git, "reconciliar",
                          return_value=Incierto(self.B, c, "sin-transporte")):
            self.contexto.tick(1023)
            self.contexto.tick(1024)
        self.assertIsInstance(self.estado().trenes[-1].fase, Promoviendo)
        self.assertEqual(self.posts("/comments"), [])
        self.contexto.tick(1025)
        self.assertEqual(self.estado().trenes[-1].fase, Terminal("ATERRIZADO"))
        self.assertEqual(self.posts("/comments"), [])
        self.assertFalse(any(p["payload"]["state"] == "failure" for p in self.posts("/statuses/")))

    def mover_main_certificado(self, resultado):
        datos = self.certificar_solicitud_cero()
        antes = self.estado()
        nuevo_main = self.commit({"comun": "base\n", "ajeno": "cambio\n"}, (self.B,))
        self.push(nuevo_main, "refs/heads/main")
        self.ci_status(datos, resultado, 1022)
        leer_ci = self.contexto.observador.ci

        def ci(repo, sha, rama=None):
            if resultado == "pending" and sha == self.miembros[0][1]:
                return ResultadoCI("pendiente")
            return leer_ci(repo, sha, rama)

        with patch.object(self.contexto.observador, "ci", side_effect=ci):
            self.contexto.tick(1022)
        return antes, nuevo_main

    def test_base_movida_rearma_certificado_con_ci_pendiente_con_fifo(self):
        self.comprobar_rearme_certificado("pending")

    def test_base_movida_rearma_certificado_con_ci_rojo_con_fifo(self):
        self.comprobar_rearme_certificado("failure")

    def comprobar_rearme_certificado(self, resultado):
        antes, nuevo_main = self.mover_main_certificado(resultado)
        despues = self.estado()
        self.assertEqual(despues.trenes[-1].fase, Terminal("REARMAR"))
        self.assertIsNone(despues.pausa)
        self.assertEqual([e.admitida for e in despues.entradas], [e.admitida for e in antes.entradas])
        self.assertEqual(self.remoto(), nuevo_main)
        self.contexto.tick(1023)
        self.assertEqual(self.estado().trenes[-1].recibos, antes.trenes[-1].recibos)

    def test_base_movida_rearma_certificado_con_ci_verde_con_fifo(self):
        self.comprobar_rearme_certificado("success")


class CITrasAbortarTests(fixtures.CasosCI):
    def abortar_y_rearmar(self, conclusion, individual=False):
        inicio = self.individual() if individual else self.hasta_fase(EsperandoCI)
        viejo = inicio.trenes[-1]
        c = viejo.fase.candidato.C
        check_viejo = dict(check("success"), head_sha=c, status="in_progress", conclusion=None)
        datos = {"head": c, "checks": [check_viejo], "suites": [
            {"id": 1, "head_sha": c, "head_branch": "tren/" + viejo.id}]}
        self.guardar_ci(datos)
        self.contexto = ContextoR1(self.ruta_contexto)
        vence = viejo.fase.desde + PLAZO_CI
        self.contexto.tick(vence)
        self.assertEqual(self.estado().pausa.detalle, "ci-sin-resultado")
        self.juicio("abortar")
        self.contexto.tick(vence + 1)
        self.assertEqual(self.estado().trenes[-1].fase, Terminal("ABORTADO"))
        entradas = {e.entrega.recibo: e for e in self.estado().entradas}
        self.assertTrue(all(entradas[r].ci_ref_nueva and entradas[r].rearme_ci is None
                            for r in viejo.recibos))
        check_viejo.update(status="completed", conclusion=conclusion)
        self.guardar_ci(datos)
        estado = self.siguiente_hasta(
            lambda e: len(e.trenes) == 2 and isinstance(e.trenes[-1].fase, EsperandoCI), vence + 2)
        nuevo = estado.trenes[-1]
        self.assertEqual(nuevo.fase.candidato.C, c)
        self.assertEqual(nuevo.fase.solicitud, 1)
        self.assertEqual(self.remoto("refs/heads/tren/" + nuevo.id + "-ci-1"), c)
        self.assertEqual(self.contexto.observador.ci_candidato(estado)[1].estado, "pendiente")
        for ahora in range(vence + 10, vence + 15):
            self.contexto.tick(ahora)
        self.assertEqual(self.estado().trenes[-1], nuevo)
        self.assertEqual(self.remoto(), self.B)
        self.assertEqual(self.posts("/comments"), [])
        return nuevo, datos, vence + 15

    def test_verde_de_ref_abortada_no_acredita_y_primer_rojo_propio_rearma(self):
        tren, datos, ahora = self.abortar_y_rearmar("success")
        self.nueva(datos, tren, "failure")
        self.contexto.tick(ahora)
        self.assertEqual(self.estado().trenes[-1].fase, Terminal("REARMAR"))

    def test_rojo_de_ref_abortada_no_devuelve_y_rojo_propio_si_devuelve(self):
        tren, datos, ahora = self.abortar_y_rearmar("failure", individual=True)
        self.assertEqual(len(tren.recibos), 1)
        self.nueva(datos, tren, "failure")
        self.contexto.tick(ahora)
        self.assertEqual(self.estado().trenes[-1].fase, Terminal("DEVUELTO"))
        self.assertFalse(self.estado().entradas[0].ci_ref_nueva)
