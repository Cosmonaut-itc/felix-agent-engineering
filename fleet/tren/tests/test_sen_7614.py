"""SEN-7614: un tren cuya B quedó atrás de main se rearma antes de CI y suite."""
from dataclasses import replace
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dominio import (Armado, Candidato, Completo, Congelado, Corriendo, Entrada, Entrega,
                     EsperandoCI, Estado, Observacion, Pausa, Terminal, VistaPR, paso)

REPO = "example/project"
B = "b" * 40
MAIN = "d" * 40
MIEMBROS = ((1, "1" * 40), (2, "2" * 40))


class RearmarTempranoTests(unittest.TestCase):
    def setUp(self):
        entradas = tuple(Entrada(Entrega(REPO, pr, head, "apto", "dirigida"), pr)
                         for pr, head in MIEMBROS)
        vistas = tuple(VistaPR(pr, head, apto=True, hallazgos=0, dirigida=True,
                               ci="verde", conflicto=False) for pr, head in MIEMBROS)
        self.obs = Observacion(100, B, vistas)
        estado, efectos = paso(Estado(REPO, entradas), self.obs)
        if not efectos:
            self.obs = replace(self.obs, ahora=3700)
            estado, efectos = paso(estado, self.obs)
        self.assertEqual(estado.trenes[0].fase, Congelado(B))
        self.assertEqual(efectos[0].tipo, "armar-candidato")
        self.congelado = estado
        self.tren = estado.trenes[0]
        self.candidato = Candidato(B, tuple((pr, head, "e" * 39 + str(pr))
                                            for pr, head in MIEMBROS), "c" * 40, "a" * 40)

    def en_fase(self, fase):
        return replace(self.congelado, trenes=(replace(self.tren, fase=fase),))

    def assert_rearmado(self, antes, final, efectos):
        self.assertEqual(final.trenes[0].fase, Terminal("REARMAR"))
        self.assertIsNone(final.pausa)
        self.assertEqual([e.tipo for e in efectos], [])
        self.assertTrue(all(e.tren is None for e in final.entradas))
        self.assertEqual(tuple((e.recibida, e.admitida) for e in final.entradas),
                         tuple((e.recibida, e.admitida) for e in antes.entradas))

    def test_cada_fase_previa_a_la_suite_se_rearma(self):
        fases = {"congelado": Congelado(B), "armado": Armado(self.candidato),
                 "esperando-ci": EsperandoCI(self.candidato, 100)}
        for nombre, fase in fases.items():
            with self.subTest(fase=nombre):
                antes = self.en_fase(fase)
                obs = replace(self.obs, base=MAIN, tren_id=self.tren.id, ci_candidato="verde")
                final, efectos = paso(antes, obs)
                self.assert_rearmado(antes, final, efectos)

    def test_candidato_recien_armado_sobre_b_vieja_no_pasa_a_armado(self):
        obs = replace(self.obs, base=MAIN, tren_id=self.tren.id, candidato=self.candidato)
        final, efectos = paso(self.congelado, obs)
        self.assert_rearmado(self.congelado, final, efectos)

    def test_el_lote_se_congela_sobre_main_en_el_paso_siguiente(self):
        antes = self.en_fase(Armado(self.candidato))
        obs = replace(self.obs, base=MAIN)
        final, efectos = paso(antes, obs)
        self.assert_rearmado(antes, final, efectos)
        nuevo, efectos = paso(final, obs)
        self.assertEqual(nuevo.trenes[-1].fase, Congelado(MAIN))
        self.assertEqual(nuevo.trenes[-1].recibos, self.tren.recibos)
        self.assertNotEqual(nuevo.trenes[-1].id, self.tren.id)
        self.assertEqual(efectos[0].tipo, "armar-candidato")

    def test_miembros_retenidos_tras_rearmar_conservan_su_orden(self):
        # En la base, EsperandoCI conservaba admitida hasta el lote siguiente.
        marcas = {1: 340, 2: 220}
        antes = replace(self.en_fase(EsperandoCI(self.candidato, 100)), entradas=tuple(
            replace(e, admitida=marcas[e.entrega.pr]) for e in self.congelado.entradas))
        pendiente = replace(self.obs, ahora=4000, base=MAIN, prs=tuple(
            replace(v, ci="pendiente") for v in self.obs.prs))
        estado, _ = paso(antes, pendiente)
        self.assertEqual(estado.trenes[0].fase, Terminal("REARMAR"))
        for ahora in (4120, 4240):
            estado, _ = paso(estado, replace(pendiente, ahora=ahora))
            self.assertEqual({e.entrega.pr: e.admitida for e in estado.entradas}, marcas)
        estado, _ = paso(estado, replace(self.obs, ahora=4360, base=MAIN))
        nuevo = estado.trenes[-1]
        self.assertEqual(nuevo.fase, Congelado(MAIN))
        recibos = {e.entrega.pr: e.entrega.recibo for e in antes.entradas}
        self.assertEqual(nuevo.recibos, (recibos[2], recibos[1]))
        self.assertFalse(any(e.rearmada for e in estado.entradas))

    def test_suite_iniciada_no_se_rearma(self):
        # Sus coordinadores pueden seguir vivos con la fase aún en EsperandoCI.
        antes = self.en_fase(EsperandoCI(self.candidato, 100))
        obs = replace(self.obs, base=MAIN, tren_id=self.tren.id, ci_candidato="verde",
                      suite_iniciada=True)
        final, efectos = paso(antes, obs)
        self.assertIsInstance(final.trenes[0].fase, EsperandoCI)
        self.assertEqual([e.tipo for e in efectos], ["iniciar-suite"])
        final, _ = paso(antes, replace(obs, suite="/estado/suite.json",
                                       resultado_suite=Completo("verde", (), 10.0)))
        self.assertNotIsInstance(final.trenes[0].fase, Terminal)
        self.assertIsNone(final.pausa)

    def test_con_main_en_b_sigue_igual(self):
        final, efectos = paso(self.en_fase(Armado(self.candidato)), self.obs)
        self.assertIsInstance(final.trenes[0].fase, EsperandoCI)
        self.assertEqual([e.tipo for e in efectos], ["solicitar-ci"])
        self.assertIsNone(final.pausa)

    def test_suite_en_curso_no_se_rearma_por_este_camino(self):
        antes = self.en_fase(Corriendo(self.candidato, "/estado/suite"))
        final, _ = paso(antes, replace(self.obs, base=MAIN))
        self.assertIsInstance(final.trenes[0].fase, Corriendo)
        self.assertIsNone(final.pausa)

    def test_pausa_vigente_o_base_sin_leer_no_rearman(self):
        armado = self.en_fase(Armado(self.candidato))
        pausado = replace(armado, pausa=Pausa("manual"))
        final, _ = paso(pausado, replace(self.obs, base=MAIN))
        self.assertIsInstance(final.trenes[0].fase, Armado)
        self.assertEqual(final.pausa, Pausa("manual"))
        final, _ = paso(armado, replace(self.obs, base=""))
        self.assertNotIsInstance(final.trenes[0].fase, Terminal)


if __name__ == "__main__":
    unittest.main()
