"""Regresiones del aplazamiento por conflicto en cadena."""
from dataclasses import replace
import unittest

from test_tren import BASE, entrega, estado, observacion, vista
from dominio import (Admitido, Candidato, ConflictoCadena, Congelado, EsperandoCI,
                     Motivo, Rechazado, Terminal, datos, desde_datos, paso, recibo_ci)
from panel import espera


def conflicto(obs, tren, pr):
    return replace(obs, ahora=obs.ahora + 1, tren_id=tren.id,
                   conflicto_cadena=ConflictoCadena(pr, ("comun",)))


def ci_rojo(nuevo, obs):
    tren = nuevo.trenes[-1]
    recibos = set(tren.recibos)
    eslabones = tuple((e.entrega.pr, e.entrega.head, "f" * 40)
                      for e in nuevo.entradas if e.entrega.recibo in recibos)
    candidato = Candidato(BASE, eslabones, "c" * 40, "d" * 40)
    lectura = replace(obs, tren_id=tren.id)
    nuevo, _ = paso(nuevo, replace(lectura, candidato=candidato))
    nuevo, _ = paso(nuevo, replace(lectura, ahora=lectura.ahora + 1))
    tren = nuevo.trenes[-1]
    assert isinstance(tren.fase, EsperandoCI), tren.fase
    return paso(nuevo, replace(lectura, ahora=lectura.ahora + 2, ci_candidato="rojo",
                              ci_rojos=("unit",), ci_recibo=recibo_ci(tren)))[0]


class ConflictoAplazadoTests(unittest.TestCase):
    def test_al_salir_el_prefijo_recupera_fifo_sobre_la_misma_base(self):
        obs = observacion(2)
        original, _ = paso(estado(3), obs)
        aplazado, _ = paso(original, conflicto(obs, original.trenes[-1], 2))
        self.assertEqual(aplazado.trenes[-1].recibos, (entrega(1).recibo,))
        self.assertEqual(aplazado.entradas[1].admision, Admitido())
        self.assertIsNone(aplazado.entradas[1].tren)
        aplazado = desde_datos(datos(aplazado))

        obs = observacion(3, ahora=200)
        devuelto = ci_rojo(aplazado, obs)
        self.assertEqual(devuelto.trenes[-1].fase, Terminal("DEVUELTO"))
        siguiente, _ = paso(devuelto, replace(obs, ahora=300))
        self.assertEqual(siguiente.trenes[-1].fase, Congelado(BASE))
        self.assertEqual(siguiente.trenes[-1].recibos, (entrega(2).recibo, entrega(3).recibo))
        self.assertEqual(siguiente.entradas[1].admitida, original.entradas[1].admitida)

        # Si el prefijo se rearma tras su primer rojo, no se repite el lote conflictivo.
        obs = observacion(3)
        original, _ = paso(estado(3), obs)
        aplazado, _ = paso(original, conflicto(obs, original.trenes[-1], 3))
        rearmado = ci_rojo(aplazado, replace(obs, ahora=200))
        siguiente, _ = paso(rearmado, replace(obs, ahora=300))
        self.assertNotEqual(siguiente.trenes[-1].id, aplazado.trenes[-1].id)
        self.assertEqual(siguiente.trenes[-1].recibos, aplazado.trenes[-1].recibos)
        self.assertIsNone(siguiente.entradas[2].tren)

    def test_aplazar_tras_dos_rojos_conserva_la_particion(self):
        obs = observacion(3)
        inicial, _ = paso(estado(3), obs)
        rearmado = ci_rojo(inicial, replace(obs, ahora=200))
        segundo, _ = paso(rearmado, replace(obs, ahora=300))
        partido = ci_rojo(segundo, replace(obs, ahora=400))
        particion = partido.trenes[-1].id
        self.assertEqual(partido.trenes[-1].fase, Terminal("PARTIDO"))
        individual, _ = paso(partido, replace(obs, ahora=500))
        aplazado, _ = paso(individual, conflicto(replace(obs, ahora=500), individual.trenes[-1], 1))
        self.assertEqual([(e.partida, e.particion) for e in aplazado.entradas],
                         [(True, particion)] * 3)
        self.assertIsNone(aplazado.entradas[0].tren)
        siguiente, _ = paso(aplazado, replace(obs, ahora=600))
        self.assertEqual(siguiente.trenes[-1].recibos, (entrega(2).recibo,))

    def test_prefijo_cerrado_se_desliga_y_aparece_caducado_en_el_panel(self):
        obs = observacion(3)
        inicial, _ = paso(estado(3), obs)
        lectura = conflicto(replace(obs, prs=(vista(1, abierto=False), *obs.prs[1:])),
                            inicial.trenes[-1], 2)
        aplazado, efectos = paso(inicial, lectura)
        cerrada = aplazado.entradas[0]
        self.assertEqual(cerrada.admision, Rechazado(Motivo.CERRADO))
        self.assertIsNone(cerrada.tren)
        self.assertIsNone(cerrada.terminal)
        self.assertEqual([e.tipo for e in efectos].count("avisar-caducidad"), 1)
        filas = espera(datos(aplazado)["entradas"], lectura.ahora)
        self.assertIn((1, "caducado"), [(f["pr"], f["etapa"]) for f in filas])
        siguiente, _ = paso(aplazado, replace(obs, ahora=200))
        self.assertEqual(siguiente.trenes[-1].recibos, (entrega(2).recibo, entrega(3).recibo))


def abortar_todo(estado_, cerradas=()):
    return replace(estado_,
        trenes=tuple(replace(t, fase=Terminal("ABORTADO")) for t in estado_.trenes),
        entradas=tuple(replace(e, tren=None, admision=Rechazado(Motivo.CERRADO))
                       if e.entrega.pr in cerradas else replace(e, tren=None)
                       for e in estado_.entradas))


class ConflictoAplazadoRonda2Tests(unittest.TestCase):
    def test_con_el_prefijo_otra_vez_en_fila_viajan_los_que_no_chocan(self):
        obs = observacion(4)
        original, _ = paso(estado(4), obs)
        aplazado, _ = paso(original, conflicto(obs, original.trenes[-1], 3))
        siguiente, _ = paso(abortar_todo(aplazado), replace(obs, ahora=200))
        self.assertEqual(siguiente.trenes[-1].recibos,
                         tuple(entrega(n).recibo for n in (1, 2, 4)))
        self.assertIsNone(siguiente.entradas[2].tren)

    def test_un_segundo_conflicto_en_la_misma_base_conserva_el_primero(self):
        obs = observacion(3)
        original, _ = paso(estado(3), obs)
        r1, r2, r3 = (entrega(n).recibo for n in (1, 2, 3))
        tren = replace(original.trenes[-1], recibos=(r2, r3))
        previo = replace(original, trenes=(tren,), entradas=tuple(
            replace(e, tren=None) if e.entrega.recibo == r1 else
            replace(e, aplazada_en_base=BASE, prefijo_en_conflicto=(r1,))
            if e.entrega.recibo == r3 else e for e in original.entradas))
        aplazado, _ = paso(previo, conflicto(obs, tren, 3))
        aplazado = desde_datos(datos(aplazado))
        lectura = replace(obs, ahora=200, prs=(obs.prs[0], vista(2, abierto=False), *obs.prs[2:]))
        siguiente, _ = paso(abortar_todo(aplazado, cerradas=(2,)), lectura)
        # Sin el primer conflicto guardado, el lote repetiría [1, 3] sobre la misma base.
        self.assertEqual(siguiente.trenes[-1].recibos, (r1,))
