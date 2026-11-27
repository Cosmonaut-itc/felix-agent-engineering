"""CI de C en la promoción con remoto Git y transporte GitHub de prueba."""
import sys
from unittest.mock import patch

import test_u16_r1 as fixtures
from test_u16_r1 import ContextoR1, check, status
from dominio import Certificado, EsperandoCI, PLAZO_CI, Pausa, Promoviendo, ResultadoCI, Terminal


class PromocionCITests(fixtures.CasosCI):
    rearmar_con_rojo = fixtures.RearmeCIRefTests.rearmar_con_rojo

    def certificar_solicitud_cero(self):
        tren = self.hasta_fase(EsperandoCI).trenes[0]
        self.assertEqual(tren.fase.solicitud, 0)
        c = tren.fase.candidato.C
        datos = {"head": c, "checks": [dict(check("success"), head_sha=c)]}
        self.guardar_ci(datos)
        self.contexto = ContextoR1(self.ruta_contexto)
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
