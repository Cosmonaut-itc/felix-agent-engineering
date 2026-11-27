"""Orden de la fila cuando el CI de un PR vuelve a pendiente."""
from dataclasses import replace
import unittest

from test_tren import entrega, estado, observacion, vista
from test_conflicto_aplazado import ci_rojo, conflicto
from dominio import (Aterrizado, Candidato, Certificado, Completo, EsperandoCI,
                     Promoviendo, Terminal, paso, recibo_ci)


class FifoCertificadoTests(unittest.TestCase):
    def certificar(self, lote, obs):
        tren = lote.trenes[-1]
        miembros = {e.entrega.recibo: e.entrega for e in lote.entradas}
        eslabones = tuple((miembros[r].pr, miembros[r].head, "f" * 40) for r in tren.recibos)
        candidato = Candidato(obs.base, eslabones, "c" * 40, "d" * 40)
        lectura = replace(obs, tren_id=tren.id)
        armado, _ = paso(lote, replace(lectura, candidato=candidato))
        esperando, _ = paso(armado, replace(lectura, ahora=obs.ahora + 1))
        self.assertIsInstance(esperando.trenes[-1].fase, EsperandoCI)
        lectura = replace(lectura, ahora=obs.ahora + 2, ci_recibo=recibo_ci(esperando.trenes[-1]))
        certificado, _ = paso(esperando, replace(lectura, ci_candidato="verde", suite="suite",
                                                  resultado_suite=Completo("verde")))
        self.assertIsInstance(certificado.trenes[-1].fase, Certificado)
        return esperando, certificado, lectura

    def comprobar_lote(self, previo, lectura, prs):
        nuevo, _ = paso(previo, lectura)
        self.assertNotEqual(nuevo.trenes[-1].id, previo.trenes[-1].id)
        self.assertEqual(nuevo.trenes[-1].recibos, tuple(entrega(pr).recibo for pr in prs))

    def comprobar_rearme_rojo(self, previo, lectura, prs):
        rojo = replace(lectura, ahora=200, ci_candidato="rojo", ci_rojos=("unit",),
                       prs=(vista(1, ci="pendiente"), vista(2, ci="pendiente"), vista(3), vista(4)))
        rearmado, _ = paso(previo, rojo)
        self.assertEqual(rearmado.trenes[-1].fase, Terminal("REARMAR"))
        self.comprobar_lote(rearmado, observacion(4, ahora=300), prs)

    def comprobar_main_nuevo(self, certificado, lectura, ci_pendiente, prs):
        pendiente = replace(lectura, ahora=400, ci_candidato="pendiente", prs=tuple(
            vista(pr, ci="pendiente" if pr in ci_pendiente else "verde") for pr in range(1, 5)))
        retenido, _ = paso(certificado, pendiente)
        self.assertIsInstance(retenido.trenes[-1].fase, Certificado)
        nueva_base = "e" * 40
        rearmado, _ = paso(retenido, replace(pendiente, ahora=500, base=nueva_base))
        self.assertEqual(rearmado.trenes[-1].fase, Terminal("REARMAR"))
        self.comprobar_lote(rearmado, replace(observacion(4, ahora=600), base=nueva_base), prs)

    def test_rojo_en_la_misma_base(self):
        obs = observacion(2)
        lote, _ = paso(estado(4), obs)
        esperando, certificado, lectura = self.certificar(lote, replace(obs, ahora=120))
        for previo in (esperando, certificado):
            with self.subTest(fase=type(previo.trenes[-1].fase).__name__):
                self.comprobar_rearme_rojo(previo, lectura, (1, 2, 3, 4))

    def test_certificado_pendiente_antes_de_main_nuevo(self):
        obs = observacion(2)
        lote, _ = paso(estado(4), obs)
        _, certificado, lectura = self.certificar(lote, replace(obs, ahora=120))
        self.comprobar_main_nuevo(certificado, lectura, (1, 2), (1, 2, 3, 4))

    def test_rojo_en_la_misma_base_con_un_pr_aplazado(self):
        obs = observacion(3)
        lote, _ = paso(estado(4), obs)
        aplazado, _ = paso(lote, conflicto(obs, lote.trenes[-1], 3))
        esperando, certificado, lectura = self.certificar(aplazado, replace(obs, ahora=120))
        for previo in (esperando, certificado):
            with self.subTest(fase=type(previo.trenes[-1].fase).__name__):
                self.comprobar_rearme_rojo(previo, lectura, (1, 2, 4))

    def test_certificado_pendiente_con_un_pr_antes_aplazado(self):
        obs = observacion(2)
        inicial, _ = paso(estado(4), obs)
        aplazado, _ = paso(inicial, conflicto(obs, inicial.trenes[-1], 2))
        devuelto = ci_rojo(aplazado, replace(obs, ahora=200))
        self.assertEqual(devuelto.trenes[-1].fase, Terminal("DEVUELTO"))
        obs = observacion(3, ahora=300)
        lote, _ = paso(devuelto, obs)
        self.assertEqual(lote.trenes[-1].recibos, (entrega(2).recibo, entrega(3).recibo))
        _, certificado, lectura = self.certificar(lote, replace(obs, ahora=310))
        self.comprobar_main_nuevo(certificado, lectura, (2,), (2, 3, 4))

    def test_un_lote_aterrizado_no_vuelve_a_la_fila(self):
        obs = observacion(2)
        lote, _ = paso(estado(4), obs)
        _, certificado, lectura = self.certificar(lote, replace(obs, ahora=120))
        fase = certificado.trenes[-1].fase
        promoviendo, _ = paso(certificado, replace(lectura, ahora=200, ci_candidato="verde"))
        self.assertIsInstance(promoviendo.trenes[-1].fase, Promoviendo)
        aterrizado, _ = paso(promoviendo, replace(lectura, ahora=300, intento_promocion=1,
                                                 main=Aterrizado(fase.B, fase.C, fase.arbol)))
        self.assertEqual(aterrizado.trenes[-1].fase, Terminal("ATERRIZADO"))
        self.comprobar_lote(aterrizado, replace(observacion(4, ahora=400), base=fase.C), (3, 4))

    def test_un_certificado_devuelto_no_vuelve_a_la_fila(self):
        obs = observacion()
        lote, _ = paso(estado(2), obs)
        _, certificado, lectura = self.certificar(lote, replace(obs, ahora=120))
        devuelto, _ = paso(certificado, replace(lectura, ahora=200, ci_candidato="rojo",
                                               ci_rojos=("unit",), prs=(vista(ci="pendiente"),)))
        self.assertEqual(devuelto.trenes[-1].fase, Terminal("DEVUELTO"))
        self.comprobar_lote(devuelto, observacion(2, ahora=300), (2,))
