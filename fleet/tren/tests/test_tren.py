"""Sin red; sólo ejecutar en el host de pruebas. Fixtures retenidas."""
from dataclasses import replace
import fcntl
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
from dominio import (Admitido, Armado, Aterrizado, Candidato, Completo, Congelado, Corriendo,
                     Decision, Diagnosticando, Entrada, Entrega, EscritorExterno, EsperandoCI, Estado,
                     Evidencia, Fallo, Intento, LIMITE_LOTE, Motivo, Observacion,
                     Pausa, Rechazado, Repeticion, Reproduccion, Retenido, Terminal,
                     UnidadRepetida, VistaPR, clave, datos, desde_datos, inicial, paso,
                     recibo_ci)
from ledger import Ledger, directorio_estado
from observador import Falso, GitHubGit, categorias_sensibles, configuracion
from sombra import paso_sombra

loader = importlib.machinery.SourceFileLoader("cli_tren", str(RAIZ / "pao-tren"))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)

HEAD = "a" * 40
BASE = "b" * 40
REPO = "example/project"


def entrega(pr=1, head=HEAD):
    return Entrega(REPO, pr, head, "/evidencia/apto.json", "/evidencia/dirigida.json")


def vista(pr=1, **kwargs):
    return replace(VistaPR(pr, HEAD, apto=True, hallazgos=0, dirigida=True,
                           ci="verde", conflicto=False), **kwargs)


def estado(n=1):
    return Estado(REPO, tuple(Entrada(entrega(i), float(i)) for i in range(1, n + 1)))


def observacion(n=1, ahora=100, **kwargs):
    return Observacion(ahora, BASE, tuple(vista(i) for i in range(1, n + 1)), **kwargs)


class Aislado(unittest.TestCase):
    def setUp(self):
        self.raiz = Path(tempfile.mkdtemp(prefix="pao-tren-u1-"))
        self.home = self.raiz / "home"
        self.home.mkdir()
        self.env = patch.dict(os.environ, {"HOME": str(self.home),
                             "XDG_STATE_HOME": str(self.raiz / "state"),
                             "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def ledger(self):
        ledger = Ledger()
        self.addCleanup(ledger.close)
        return ledger

    def cli(self, *args):
        return subprocess.run([sys.executable, "-I", str(RAIZ / "pao-tren"), *args],
                              capture_output=True, text=True, timeout=20)


class DominioTests(Aislado):
    def test_custodia(self):
        self.assertEqual(estado().entradas[0].admision, Retenido(Motivo.CUSTODIA))

    def test_admision_y_pureza(self):
        original = estado()
        nuevo, efectos = paso(original, replace(observacion(), base=""))
        self.assertIsInstance(nuevo.entradas[0].admision, Admitido)
        self.assertEqual(original, estado())
        self.assertEqual(efectos, ())

    def test_head_distinto_caduca_y_no_revive(self):
        obs = replace(observacion(), prs=(vista(head="c" * 40),))
        nuevo, efectos = paso(estado(), obs)
        self.assertEqual(nuevo.entradas[0].admision, Rechazado(Motivo.HEAD))
        self.assertEqual(efectos[0].tipo, "avisar-caducidad")
        repetido, efectos2 = paso(nuevo, observacion())
        self.assertEqual(repetido, nuevo)
        self.assertEqual(efectos2, ())
        self.assertEqual(paso(estado(), obs)[1], efectos)

    def test_retenciones_y_rechazos(self):
        casos = [({"apto": False}, Retenido(Motivo.APTO)),
                 ({"hallazgos": 1}, Retenido(Motivo.HALLAZGOS)),
                 ({"dirigida": False}, Retenido(Motivo.DIRIGIDA)),
                 ({"ci": "pendiente"}, Retenido(Motivo.CI)),
                 ({"conflicto": True}, Retenido(Motivo.CONFLICTO)),
                 ({"conflicto": None}, Retenido(Motivo.INCIERTO)),
                 ({"completa": False}, Retenido(Motivo.INCIERTO)),
                 ({"base": "develop"}, Rechazado(Motivo.BASE)),
                 ({"abierto": False}, Rechazado(Motivo.CERRADO)),
                 ({"sensible": True}, Retenido(Motivo.MERGE_READY)),
                 ({"alto_riesgo": True}, Retenido(Motivo.VERIFICADO)),
                 ({"alto_riesgo": True, "verificado": True}, Retenido(Motivo.PILOTO)),
                 ({"alto_riesgo": True, "verificado": True, "autorizado": True}, Admitido())]
        for cambios, esperado in casos:
            with self.subTest(cambios=cambios):
                nuevo, _ = paso(estado(), replace(observacion(), prs=(vista(**cambios),)))
                self.assertEqual(nuevo.entradas[0].admision, esperado)

    def test_falta_observacion_retiene(self):
        nuevo, _ = paso(estado(), replace(observacion(), prs=()))
        self.assertEqual(nuevo.entradas[0].admision, Retenido(Motivo.INCIERTO))

    def test_un_admitido_sale_en_el_mismo_tick(self):
        nuevo, efectos = paso(estado(), observacion())
        self.assertIsInstance(nuevo.entradas[0].admision, Admitido)
        self.assertEqual(nuevo.trenes[0].recibos, (entrega().recibo,))
        self.assertIsInstance(nuevo.trenes[0].fase, Congelado)
        self.assertEqual(efectos[0].tipo, "armar-candidato")
        self.assertEqual(paso(estado(), observacion()), (nuevo, efectos))

    def test_readmision_reinicia_fifo(self):
        obs = replace(observacion(), base="")
        nuevo, _ = paso(estado(), obs)
        retenido, _ = paso(nuevo, replace(obs, ahora=200, prs=(vista(ci="pendiente"),)))
        self.assertIsNone(retenido.entradas[0].admitida)
        nuevo, _ = paso(retenido, observacion(ahora=4000))
        self.assertEqual(nuevo.trenes[0].recibos, (entrega().recibo,))
        self.assertEqual(nuevo.entradas[0].admitida, 4000)

    def test_fifo_por_admision(self):
        nuevo, _ = paso(estado(3), replace(observacion(3), base="",
                           prs=(vista(1, ci="pendiente"), vista(2), vista(3))))
        nuevo, _ = paso(nuevo, observacion(3, ahora=101))
        self.assertEqual(nuevo.trenes[0].recibos, tuple(entrega(i).recibo for i in (2, 3, 1)))

    def test_alto_riesgo_solo(self):
        obs = replace(observacion(3, excluir_alto_riesgo=False),
                      prs=(vista(1, alto_riesgo=True, verificado=True, autorizado=True),
                           vista(2), vista(3)))
        nuevo, _ = paso(estado(3), obs)
        self.assertEqual(nuevo.trenes[0].recibos, (entrega().recibo,))

    def test_alto_riesgo_sin_autorizar_queda_en_merge_ready_fuera_del_piloto(self):
        obs = replace(observacion(excluir_alto_riesgo=False),
                      prs=(vista(1, alto_riesgo=True, verificado=True),))
        nuevo, _ = paso(estado(), obs)
        self.assertEqual(nuevo.entradas[0].admision, Retenido(Motivo.MERGE_READY))
        self.assertFalse(nuevo.trenes)

    def test_alto_riesgo_autorizado_viaja_solo_en_el_piloto(self):
        obs = replace(observacion(3),
                      prs=(vista(1, alto_riesgo=True, verificado=True, autorizado=True),
                           vista(2), vista(3)))
        self.assertTrue(obs.excluir_alto_riesgo)
        nuevo, _ = paso(estado(3), obs)
        self.assertEqual(nuevo.trenes[0].recibos, (entrega().recibo,))

    def test_sensible_autorizado_solo(self):
        obs = replace(observacion(3), prs=(vista(1, sensible=True, autorizado=True), vista(2), vista(3)))
        nuevo, _ = paso(estado(3), obs)
        self.assertEqual(nuevo.trenes[0].recibos, (entrega().recibo,))

    def test_solo_no_adelanta_fifo(self):
        obs = replace(observacion(3), prs=(vista(1), vista(2, sensible=True, autorizado=True), vista(3)))
        nuevo, _ = paso(estado(3), obs)
        self.assertEqual(nuevo.trenes[0].recibos, (entrega().recibo,))
        terminal = replace(nuevo.trenes[0], fase=Terminal("ATERRIZADO"))
        nuevo, _ = paso(replace(nuevo, trenes=(terminal,)), replace(obs, ahora=101))
        self.assertEqual(nuevo.trenes[1].recibos, (entrega(2).recibo,))
        self.assertIsNone(nuevo.entradas[2].tren)

    def test_seis_admitidos_con_tren_activo_salen_en_lotes_de_cinco_y_uno(self):
        nuevo, _ = paso(estado(), observacion())
        nuevo = replace(nuevo, entradas=nuevo.entradas + estado(7).entradas[1:])
        for ahora, n in ((101, 4), (102, 7)):
            nuevo, efectos = paso(nuevo, observacion(n, ahora=ahora))
            self.assertEqual(len(nuevo.trenes), 1)
            self.assertFalse(efectos)
        self.assertTrue(all(isinstance(e.admision, Admitido) and e.tren is None
                            for e in nuevo.entradas[1:]))
        for ahora, prs in ((103, range(2, 7)), (104, (7,))):
            terminal = replace(nuevo.trenes[-1], fase=Terminal("ATERRIZADO"))
            nuevo, efectos = paso(replace(nuevo, trenes=nuevo.trenes[:-1] + (terminal,)),
                                  observacion(7, ahora=ahora))
            self.assertEqual(nuevo.trenes[-1].recibos, tuple(entrega(i).recibo for i in prs))
            self.assertEqual(efectos[0].tipo, "armar-candidato")

    def test_pausa_y_jale(self):
        self.assertEqual(inicial("example/Jale").pausa, Pausa("etapa-1"))
        nuevo, efectos = paso(replace(estado(3), pausa=Pausa("etapa-1")), observacion(3))
        self.assertFalse(nuevo.trenes)
        self.assertFalse(efectos)

    def test_caducidad_en_tren_pausa(self):
        nuevo, _ = paso(estado(3), observacion(3))
        nuevo, _ = paso(nuevo, replace(observacion(3), prs=(vista(1, head="c" * 40), vista(2), vista(3))))
        self.assertEqual(nuevo.pausa, Pausa("head-cambiado-en-tren"))

    def test_fases_hasta_corriendo_y_claves_estables(self):
        nuevo, _ = paso(estado(3), observacion(3))
        candidato = Candidato(BASE, tuple((i, HEAD, str(i) * 40) for i in (1, 2, 3)), "3" * 40, "d" * 40)
        obs = replace(observacion(3), candidato=candidato)
        nuevo, _ = paso(nuevo, obs)
        self.assertIsInstance(nuevo.trenes[0].fase, Armado)
        nuevo, efectos = paso(nuevo, observacion(3))
        self.assertIsInstance(nuevo.trenes[0].fase, EsperandoCI)
        self.assertEqual(efectos[0].tipo, "solicitar-ci")
        esperando, efectos = paso(nuevo, observacion(3))
        self.assertEqual(esperando, nuevo)
        self.assertFalse(efectos)
        obs = replace(observacion(3), ci_candidato="verde")
        _, intento1 = paso(nuevo, obs)
        corriendo, intento2 = paso(nuevo, replace(obs, suite="recibo-suite"))
        self.assertEqual(intento1, intento2)
        self.assertIsInstance(corriendo.trenes[0].fase, Corriendo)
        self.assertEqual(paso(corriendo, obs)[1], ())
        self.assertEqual(desde_datos(datos(corriendo)), corriendo)
        self.assertEqual(desde_datos(datos(intento2)), intento2)

    def test_candidato_ajeno_pausa(self):
        nuevo, _ = paso(estado(3), observacion(3))
        candidato = Candidato("e" * 40, (), HEAD, HEAD)
        nuevo, _ = paso(nuevo, replace(observacion(3), candidato=candidato))
        self.assertEqual(nuevo.pausa, Pausa("candidato-no-corresponde"))


class RearmeCITests(unittest.TestCase):
    def armar(self, nuevo, obs):
        tren = nuevo.trenes[-1]
        prs = {e.entrega.recibo: e.entrega.pr for e in nuevo.entradas}
        eslabones = tuple((prs[r], HEAD, str(prs[r]) * 40) for r in tren.recibos)
        candidato = Candidato(obs.base, eslabones, eslabones[-1][2], "d" * 40)
        armado, _ = paso(nuevo, replace(obs, candidato=candidato, tren_id=tren.id))
        return paso(armado, obs)

    def ci(self, nuevo, obs, conclusion="rojo"):
        tren = nuevo.trenes[-1]
        return paso(nuevo, replace(obs, tren_id=tren.id, ci_recibo=recibo_ci(tren),
                                   ci_candidato=conclusion, ci_rojos=("gate",)))

    def rearme(self, n=3):
        obs = observacion(n)
        lote, _ = paso(estado(n), obs)
        espera, _ = self.armar(lote, obs)
        rearmado, _ = self.ci(espera, obs)
        lote_nuevo, _ = paso(rearmado, obs)
        return lote_nuevo, obs

    def test_primer_rojo_rearma_y_conserva_fifo(self):
        lote, _ = paso(estado(3), observacion(3))
        espera, _ = self.armar(lote, observacion(3))
        original = espera.trenes[0]
        obs = replace(observacion(3, ahora=101),
                      prs=(vista(1, apto=False), vista(2), vista(3)))
        rearmado, efectos = self.ci(espera, obs)
        self.assertEqual(rearmado.trenes[0].fase, Terminal("REARMAR"))
        self.assertIsNone(rearmado.pausa)
        self.assertEqual(efectos, ())
        self.assertEqual([e.admitida for e in rearmado.entradas], [100] * 3)
        self.assertTrue(all(e.rearme_ci == original.id and not e.solo and e.tren is None
                            for e in rearmado.entradas))
        self.assertEqual(desde_datos(datos(rearmado)), rearmado)
        nuevo, _ = paso(rearmado, observacion(3, ahora=102))
        self.assertNotEqual(nuevo.trenes[-1].id, original.id)
        self.assertEqual(nuevo.trenes[-1].recibos, original.recibos)

    def test_rearme_verde_pide_suite_con_su_recibo_y_conserva_marca(self):
        lote, obs = self.rearme()
        espera, efectos = self.armar(lote, obs)
        original, nuevo = espera.trenes
        self.assertEqual(nuevo.fase.solicitud, 1)
        self.assertEqual(efectos[0].clave, clave("ci", nuevo.id, 1))
        self.assertEqual(efectos[0].tipo, "solicitar-ci")
        anterior = replace(obs, tren_id=nuevo.id, ci_candidato="rojo", ci_rojos=("gate",),
                           ci_recibo="github:" + nuevo.fase.candidato.C)
        self.assertEqual(paso(espera, anterior), (espera, ()))
        siguiente, efectos = self.ci(espera, obs, "verde")
        self.assertEqual([e.tipo for e in efectos], ["iniciar-suite"])
        self.assertTrue(all(e.rearme_ci == original.id for e in siguiente.entradas))

    def test_rearme_rojo_parte_incluso_si_se_sumo_otra_entrega(self):
        lote, _ = paso(estado(2), observacion(2))
        espera, _ = self.armar(lote, observacion(2))
        rearmado, _ = self.ci(espera, observacion(2))
        rearmado = replace(rearmado, entradas=rearmado.entradas + estado(3).entradas[2:])
        obs = observacion(3, ahora=101)
        lote, _ = paso(rearmado, obs)
        espera, _ = self.armar(lote, obs)
        partido, efectos = self.ci(espera, obs)
        tren = partido.trenes[-1]
        self.assertEqual(tren.fase, Terminal("PARTIDO"))
        self.assertEqual(efectos, ())
        self.assertTrue(all(e.tren is None and e.solo and e.partida and e.particion == tren.id
                            for e in partido.entradas))

    def test_rearme_conserva_fifo_si_main_avanza_antes_de_pedir_ci(self):
        lote, obs = self.rearme(2)
        base_nueva = "f" * 40
        pendiente = replace(obs, ahora=104, base=base_nueva,
                           prs=(vista(1, ci="pendiente"), vista(2)))
        fila, _ = paso(lote, pendiente)
        self.assertEqual(fila.trenes[-1].fase, Terminal("REARMAR"))
        self.assertEqual([e.admitida for e in fila.entradas], [100, 100])
        nuevo, _ = paso(fila, replace(obs, ahora=105, base=base_nueva))
        self.assertEqual(nuevo.trenes[-1].recibos, (entrega(1).recibo, entrega(2).recibo))

    def test_individual_aterriza_y_los_otros_cuatro_aterrizan_en_lote(self):
        lote, obs = self.rearme(LIMITE_LOTE)
        espera, _ = self.armar(lote, obs)
        partido, _ = self.ci(espera, obs)
        self.assertEqual(partido.trenes[-1].fase, Terminal("PARTIDO"))
        solo, _ = paso(partido, obs)
        self.assertEqual(solo.trenes[-1].recibos, (entrega(1).recibo,))
        espera, _ = self.armar(solo, obs)
        def aterrizar(estado, obs):
            tren = estado.trenes[-1]
            c = tren.fase.candidato
            cert, _ = self.ci(estado, replace(obs, suite="suite",
                                               resultado_suite=Completo("verde")), "verde")
            prom, _ = paso(cert, obs)
            fin, _ = paso(prom, replace(obs, tren_id=tren.id, intento_promocion=1,
                                        main=Aterrizado(c.B, c.C, c.arbol)))
            self.assertEqual(fin.trenes[-1].fase, Terminal("ATERRIZADO"))
            return fin, c.C

        fin, c = aterrizar(espera, obs)
        self.assertTrue(all(not e.partida and e.rearme_ci for e in fin.entradas[1:]))
        self.assertEqual([e.admitida for e in fin.entradas[1:]], [100] * 4)
        obs = replace(obs, base=c)
        juntos, _ = paso(fin, obs)
        self.assertEqual(juntos.trenes[-1].recibos, tuple(entrega(i).recibo for i in range(2, 6)))
        espera, _ = self.armar(juntos, obs)
        final, _ = aterrizar(espera, obs)
        self.assertTrue(all(e.tren is not None and e.rearme_ci is None for e in final.entradas))

    def test_expulsion_individual_por_juicio_libera_la_particion(self):
        lote, obs = self.rearme()
        espera, _ = self.armar(lote, obs)
        partido, _ = self.ci(espera, obs)
        solo, _ = paso(partido, obs)
        tren = solo.trenes[-1]
        pausa = Pausa("head-cambiado-en-tren", pr=1)
        decision = Decision("juicio", REPO, tren.id, "expulsar", 1, "motivo", 101, "pausa", pausa)
        devuelto, _ = paso(replace(solo, pausa=pausa),
                           replace(obs, decision=decision, pausa_id="pausa"))
        self.assertEqual(devuelto.trenes[-1].fase, Terminal("DEVUELTO"))
        juntos, _ = paso(devuelto, obs)
        self.assertEqual(juntos.trenes[-1].recibos, (entrega(2).recibo, entrega(3).recibo))

    def test_rechazo_en_cola_libera_la_particion_en_el_mismo_tick(self):
        lote, obs = self.rearme()
        espera, _ = self.armar(lote, obs)
        partido, _ = self.ci(espera, obs)
        juntos, _ = paso(partido, replace(obs, prs=(vista(1, abierto=False), vista(2), vista(3))))
        self.assertEqual(juntos.entradas[0].admision, Rechazado(Motivo.CERRADO))
        self.assertEqual(juntos.trenes[-1].recibos, (entrega(2).recibo, entrega(3).recibo))

    def test_rechazo_libera_el_individual_que_rearma_en_el_mismo_tick(self):
        lote, obs = self.rearme()
        espera, _ = self.armar(lote, obs)
        partido, _ = self.ci(espera, obs)
        individual, _ = paso(partido, obs)
        cierre = replace(obs, ahora=101, prs=(vista(1), vista(2, abierto=False), vista(3)))
        viajando, _ = paso(individual, cierre)
        self.assertEqual(viajando.trenes[-1].recibos, (entrega(1).recibo,))
        self.assertIsNotNone(viajando.entradas[0].tren)
        base_nueva = replace(cierre, base="f" * 40)
        fila, _ = paso(individual, base_nueva)
        self.assertEqual(fila.trenes[-1].fase, Terminal("REARMAR"))
        for pos in (0, 2):
            self.assertFalse(fila.entradas[pos].partida)
            self.assertIsNone(fila.entradas[pos].particion)
            self.assertEqual(fila.entradas[pos].admitida, 100)
        siguiente, _ = paso(fila, replace(base_nueva, ahora=102))
        self.assertEqual(siguiente.trenes[-1].recibos, (entrega(1).recibo, entrega(3).recibo))

    def test_individual_liberado_conserva_fifo_tras_rearmar_en_promocion(self):
        lote, obs = self.rearme()
        espera, _ = self.armar(lote, obs)
        partido, _ = self.ci(espera, obs)
        individual, _ = paso(partido, obs)
        espera, _ = self.armar(individual, obs)
        tren = espera.trenes[-1]
        c = tren.fase.candidato
        certificado, _ = self.ci(espera, replace(
            obs, ahora=101, suite="suite", resultado_suite=Completo("verde")), "verde")
        cierre = replace(obs, ahora=102, prs=(vista(1), vista(2, abierto=False), vista(3)))
        promoviendo, _ = paso(certificado, cierre)
        base_nueva = replace(cierre, ahora=103, base="f" * 40)
        fila, _ = paso(promoviendo, replace(
            base_nueva, tren_id=tren.id, intento_promocion=1,
            main=EscritorExterno(c.B, c.C, base_nueva.base)))
        self.assertEqual(fila.pausa.motivo, "escritor-externo")
        pausada, _ = paso(fila, replace(
            base_nueva, ahora=104, prs=(vista(1, ci="pendiente"), *cierre.prs[1:])))
        decision = Decision("juicio", fila.repo, tren.id, "reanudar", None,
                            "escritor externo revisado", 105, "pausa", pausada.pausa)
        libre, _ = paso(pausada, replace(
            base_nueva, ahora=105, decision=decision, pausa_id="pausa"))
        siguiente, _ = paso(libre, replace(base_nueva, ahora=106))
        self.assertEqual(siguiente.entradas[0].admitida, 100)
        self.assertEqual(siguiente.trenes[-1].recibos, (entrega(1).recibo, entrega(3).recibo))

    def test_devuelto_individual_libera_su_particion_y_reagrupa_hasta_el_limite(self):
        lote, obs = self.rearme(LIMITE_LOTE)
        espera, _ = self.armar(lote, obs)
        partido, _ = self.ci(espera, obs)
        n = LIMITE_LOTE + 3
        partido = replace(partido, entradas=partido.entradas + estado(n).entradas[LIMITE_LOTE:])
        obs = observacion(n, ahora=200)
        solo, _ = paso(partido, obs)
        espera, _ = self.armar(solo, obs)
        devuelto, efectos = self.ci(espera, obs)
        self.assertEqual(devuelto.trenes[-1].fase, Terminal("DEVUELTO"))
        self.assertEqual([e.tipo for e in efectos], ["devolver"])
        self.assertIsNone(devuelto.entradas[0].rearme_ci)
        liberadas = devuelto.entradas[1:LIMITE_LOTE]
        self.assertTrue(all(not e.partida and e.particion is None for e in liberadas))
        self.assertEqual([e.admitida for e in liberadas], [100] * (LIMITE_LOTE - 1))
        lote, _ = paso(devuelto, obs)
        self.assertTrue(all(not e.solo for e in lote.entradas[1:LIMITE_LOTE]))
        self.assertEqual(lote.trenes[-1].recibos, tuple(entrega(i).recibo for i in range(2, LIMITE_LOTE + 2)))
        self.assertIsNone(lote.entradas[LIMITE_LOTE + 1].tren)

    def test_suite_roja_individual_atribuida_libera_a_su_particion(self):
        lote, obs = self.rearme()
        espera, _ = self.armar(lote, obs)
        partido, _ = self.ci(espera, obs)
        solo, _ = paso(partido, obs)
        espera, _ = self.armar(solo, obs)
        tren = espera.trenes[-1]
        c = tren.fase.candidato
        fallo = Fallo("test", "firma", "solo-candidato", duracion_s=1)
        evidencias = []
        for objetivo, sha, veredicto in (("C", c.C, "falla"), ("B", c.B, "pasa")):
            unidad = UnidadRepetida("", fallo.unidad, "", "definición", tuple(
                Intento(i, veredicto, fallo.firma, 1) for i in (1, 2)))
            evidencias.append(Evidencia(Reproduccion(objetivo, fallo, objetivo),
                Repeticion(sha, c.C, c.B, "evidencia", (unidad,))))
        fase = Diagnosticando(c, (fallo,), "suite", presupuesto_s=10, evidencias=tuple(evidencias))
        diagnosticando = replace(espera, trenes=espera.trenes[:-1] + (replace(tren, fase=fase),))
        final, efectos = paso(diagnosticando, replace(obs, tren_id=tren.id))
        self.assertEqual(final.trenes[-1].fase, Terminal("DEVUELTO"))
        self.assertEqual([e.tipo for e in efectos], ["devolver"])
        self.assertTrue(all(not e.partida for e in final.entradas[1:]))

    def test_snapshot_partido_sin_id_conserva_el_aislamiento(self):
        viejo = estado(3)
        viejo = replace(viejo, entradas=tuple(replace(e, partida=True, solo=True) for e in viejo.entradas))
        snapshot = datos(viejo)
        self.assertTrue(all("particion" not in e and "rearme_ci" not in e for e in snapshot["entradas"]))
        obs = observacion(3)
        lote, _ = paso(desde_datos(snapshot), obs)
        espera, _ = self.armar(lote, obs)
        devuelto, efectos = self.ci(espera, obs)
        self.assertEqual(devuelto.trenes[-1].fase, Terminal("DEVUELTO"))
        self.assertEqual([e.tipo for e in efectos], ["devolver"])
        siguiente, _ = paso(devuelto, obs)
        self.assertEqual(siguiente.trenes[-1].recibos, (entrega(2).recibo,))
        self.assertTrue(all(e.partida and e.solo for e in siguiente.entradas[1:]))


class LedgerTests(Aislado):
    def test_directorio_xdg_y_fallback(self):
        self.assertEqual(directorio_estado(), self.raiz / "state/pao-tren")
        with patch.dict(os.environ, {"XDG_STATE_HOME": ""}):
            # Empty XDG has the same shell-default semantics as an unset value.
            self.assertEqual(directorio_estado(), self.home / ".local/state/pao-tren")

    def test_entrega_duplicada_y_reapertura(self):
        ledger = Ledger()
        self.assertEqual(ledger.entregar(entrega(), 100), ledger.entregar(entrega(), 200))
        esperado = ledger.estado(REPO)
        for tabla in ("requests", "entries", "events"):
            self.assertEqual(ledger.db.execute("SELECT count(*) FROM " + tabla).fetchone()[0], 1)
        ledger.close()
        self.assertEqual(self.ledger().estado(REPO), esperado)

    def test_misma_clave_otras_referencias_conflicto(self):
        ledger = self.ledger()
        ledger.entregar(entrega(), 100)
        with self.assertRaises(ValueError):
            ledger.entregar(replace(entrega(), apto="/otra"), 200)
        self.assertEqual(len(ledger.estado(REPO).entradas), 1)

    def test_head_nuevo_es_otra_entrega(self):
        ledger = self.ledger()
        a = ledger.entregar(entrega(), 100)
        b = ledger.entregar(entrega(head="c" * 40), 200)
        self.assertNotEqual(a, b)
        self.assertEqual(len(ledger.estado(REPO).entradas), 2)

    def test_schema_y_tablas(self):
        ledger = self.ledger()
        self.assertEqual(ledger.db.execute("PRAGMA user_version").fetchone()[0], 1)
        tablas = {r[0] for r in ledger.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"requests", "entries", "trains", "effects", "events", "outbox"}.issubset(tablas))

    def test_schema_futuro_rechazado(self):
        ledger = Ledger()
        ledger.db.execute("PRAGMA user_version=99")
        ledger.close()
        with self.assertRaises(ValueError):
            Ledger()

    def test_interrupcion_real_mitad_transaccion(self):
        ledger = Ledger()
        ledger.entregar(entrega(), 100)
        esperado = ledger.estado(REPO)
        eventos = ledger.ultimos_eventos(REPO)
        ledger.close()
        guion = """import os, sys
sys.path.insert(0, sys.argv[1])
from ledger import Ledger
l = Ledger()
with l.transaccion():
    l.evento(sys.argv[2], 'incompleto', 200, {})
    l.db.execute("UPDATE entries SET payload='corrupto'")
    os._exit(17)
"""
        proc = subprocess.run([sys.executable, "-I", "-c", guion, str(RAIZ), REPO], timeout=20)
        self.assertEqual(proc.returncode, 17)
        reabierto = self.ledger()
        self.assertEqual(reabierto.estado(REPO), esperado)
        self.assertEqual(reabierto.ultimos_eventos(REPO), eventos)
        self.assertEqual(reabierto.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_sombra_no_ejecuta_ni_modifica_custodia(self):
        ledger = self.ledger()
        for i in (1, 2, 3):
            ledger.entregar(entrega(i), i)
        custodia = ledger.estado(REPO)
        propuesto, efectos = paso(custodia, observacion(3))
        ledger.sombra(propuesto, efectos, 100)
        self.assertEqual(ledger.estado(REPO), custodia)
        self.assertEqual(ledger.estado(REPO, sombra=True), propuesto)
        self.assertEqual(ledger.ultimos_eventos(REPO)[0]["tipo"], "sombra")
        for tabla in ("trains", "effects", "outbox"):
            self.assertEqual(ledger.db.execute("SELECT count(*) FROM " + tabla).fetchone()[0], 0)
        ledger.entregar(entrega(4), 400)
        self.assertEqual(len(ledger.estado(REPO, sombra=True).entradas), 4)

    def test_flock_excluye_mismo_repo_permite_otro(self):
        ledger = self.ledger()
        guion = """import fcntl, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from dominio import clave
p = Path(sys.argv[2]) / (clave('repo', sys.argv[3]) + '.lock')
with p.open('a') as h:
    try:
        fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(23)
"""
        with ledger.candado(REPO):
            for repo, codigo in ((REPO, 23), ("example/other", 0)):
                proc = subprocess.run([sys.executable, "-I", "-c", guion,
                                       str(RAIZ), str(ledger.directorio), repo], timeout=20)
                self.assertEqual(proc.returncode, codigo)
        proc = subprocess.run([sys.executable, "-I", "-c", guion,
                               str(RAIZ), str(ledger.directorio), REPO], timeout=20)
        self.assertEqual(proc.returncode, 0)


class CLITests(Aislado):
    def test_entregar_estado_duplicado(self):
        args = ("entregar", "--repo", REPO, "--pr", "1", "--head", HEAD,
                "--apto", "apto.json", "--dirigida", "dirigida.json")
        uno, dos = self.cli(*args), self.cli(*args)
        self.assertEqual(uno.returncode, 0, uno.stderr)
        self.assertEqual(uno.stdout, dos.stdout)
        self.assertEqual(json.loads(uno.stdout)["estado"], "custodia")
        estado_cli = self.cli("estado", "--repo", REPO, "--juicio")
        self.assertEqual(estado_cli.returncode, 0, estado_cli.stderr)
        payload = json.loads(estado_cli.stdout)
        self.assertEqual(len(payload["estado"]["entradas"]), 1)

    def test_tick_sin_seco_no_abre_ledger(self):
        proc = self.cli("tick", "--repo", REPO)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout)["error"], "tick activo requiere inscripción activa")
        self.assertEqual(json.loads(proc.stdout)["inscripcion"], "no-inscrito")
        self.assertFalse(directorio_estado().exists())

    def test_argumentos_invalidos(self):
        for args in (("tick", "--repo", "-mal"),
                     ("entregar", "--repo", REPO, "--pr", "0", "--head", HEAD,
                      "--apto", "a", "--dirigida", "d")):
            self.assertEqual(self.cli(*args).returncode, 2)

    def test_sombra_con_falso_forma_lote_en_el_primer_tick(self):
        ledger = Ledger()
        ledger.entregar(entrega(), 10)
        ledger.close()
        args = cli.parser().parse_args(["tick", "--repo", REPO, "--seco"])
        falso = Falso(observacion())
        codigo, payload = cli.ejecutar(args, falso, 100)
        self.assertEqual(codigo, 0)
        self.assertEqual(len(payload["plan"]["estado"]["trenes"]), 1)

    def test_error_global_no_evento_sombra(self):
        class Fallido:
            def observar(self, estado, ahora):
                raise ValueError("sin lectura")
        args = cli.parser().parse_args(["tick", "--repo", REPO, "--seco"])
        with self.assertRaises(ValueError):
            cli.ejecutar(args, Fallido(), 100)
        self.assertEqual(self.ledger().ultimos_eventos(REPO), [])


class ObservadorTests(Aislado):
    def setUp(self):
        super().setUp()
        self.config = json.loads((RAIZ / "config.example.json").read_text())["repos"]["owner/project"]
        self.apto = self.raiz / "apto.json"
        self.dirigida = self.raiz / "dirigida.json"
        self.apto.write_text(json.dumps({"veredicto": "APTO", "head": HEAD, "hallazgos_abiertos": 0}))
        self.dirigida.write_text(json.dumps({"head": HEAD, "resultado": "limpio"}))
        self.entrega = replace(entrega(), apto=str(self.apto), dirigida=str(self.dirigida))
        self.pr = {"head": {"sha": HEAD}, "base": {"sha": BASE, "ref": "main"},
                   "state": "open", "changed_files": 1, "labels": [], "mergeable": True}
        self.archivos = [[{"filename": "src/app.py"}]]
        self.checks = [{"check_runs": [{"id": 1, "name": "lint", "status": "completed", "conclusion": "success"}]}]
        self.statuses = [[]]
        self.llamadas = []

    def ejecutar(self, args, **kwargs):
        self.llamadas.append(args)
        if args[0] == "git":
            salida = "c" * 40 if args[3] == "merge-base" else ""
        else:
            self.assertEqual(args[:4], ["gh", "api", "--method", "GET"])
            ruta = args[4]
            if "/files?" in ruta:
                salida = self.archivos
            elif "/check-runs?" in ruta:
                salida = self.checks
            elif "/statuses?" in ruta:
                salida = self.statuses
            elif "/git/ref/" in ruta:
                salida = {"object": {"sha": BASE}}
            else:
                salida = self.pr
            salida = json.dumps(salida)
        return subprocess.CompletedProcess(args, 0, salida, "")

    def observador(self):
        return GitHubGit(self.config, ejecutar=self.ejecutar)

    def decision(self):
        observador = self.observador()
        e = Estado(REPO, (Entrada(self.entrega, 1),))
        obs = observador.observar(e, 100)
        return paso(e, obs)[0].entradas[0].admision

    def test_real_usa_get_sin_token_y_admite(self):
        with patch.dict(os.environ, {"GH_TOKEN": "token-de-prueba-no-exportar"}):
            self.assertIsInstance(self.decision(), Admitido)
        self.assertTrue(self.llamadas)
        self.assertFalse(any("token-de-prueba" in str(args) for args in self.llamadas))

    def test_apto_literal_y_head_exacto(self):
        for veredicto, head in (("apto", HEAD), ("NO APTO", HEAD), ("APTO", "e" * 40)):
            self.apto.write_text(json.dumps({"veredicto": veredicto, "head": head, "hallazgos_abiertos": 0}))
            self.assertEqual(self.decision(), Retenido(Motivo.APTO))

    def test_evidencia_ilegible_retiene(self):
        self.apto.write_text("APTO sin head ni estructura")
        self.assertEqual(self.decision(), Retenido(Motivo.INCIERTO))

    def test_hallazgos_debe_ser_entero_no_booleano(self):
        self.apto.write_text(json.dumps({"veredicto": "APTO", "head": HEAD, "hallazgos_abiertos": False}))
        self.assertEqual(self.decision(), Retenido(Motivo.HALLAZGOS))

    def test_sensibles_ci_gobierno_lockfile(self):
        for archivo in (".github/workflows/check.yml", "AGENTS.md", "sub/CLAUDE.md", "package-lock.json", "sub/bun.lock"):
            with self.subTest(archivo=archivo):
                self.archivos = [[{"filename": archivo}]]
                self.assertTrue(categorias_sensibles([archivo], self.config))
                self.assertEqual(self.decision(), Retenido(Motivo.MERGE_READY))

    def test_sensible_solo_autorizacion_del_head(self):
        self.archivos = [[{"filename": "AGENTS.md"}]]
        self.config["heads_autorizados"] = ["c" * 40]
        self.assertEqual(self.decision(), Retenido(Motivo.MERGE_READY))
        self.config["heads_autorizados"] = [HEAD]
        self.assertIsInstance(self.decision(), Admitido)

    def lote_con_normal(self, archivos, *, activo):
        self.archivos = [[{"filename": archivo} for archivo in archivos]]
        self.pr["changed_files"] = len(archivos)
        original = Estado(REPO, (Entrada(self.entrega, 1), Entrada(entrega(2), 2)))
        observador = GitHubGit(self.config, ejecutar=self.ejecutar, activo=activo,
                               sombra=not activo)
        obs = Observacion(100, BASE, (observador.vista(self.entrega), vista(2)),
                          self.config["excluir_alto_riesgo"])
        reductor = paso if activo else paso_sombra
        return reductor(original, obs)[0]

    def prueba_sen_7114_lote(self, archivos, *, solo):
        for activo in (True, False):
            with self.subTest(activo=activo):
                nuevo = self.lote_con_normal(archivos, activo=activo)
                self.assertEqual(nuevo.entradas[0].admision, Admitido())
                recibos = (self.entrega.recibo,) if solo else (self.entrega.recibo, entrega(2).recibo)
                self.assertEqual(nuevo.trenes[0].recibos, recibos)

    def test_sen_7114_gobierno_autorizado_viaja_en_lote(self):
        self.config.update(sensibles_en_lote=["gobierno"], heads_autorizados=[HEAD])
        self.prueba_sen_7114_lote(["AGENTS.md"], solo=False)

    def test_sen_7114_gobierno_sin_autorizacion_sigue_retenido(self):
        self.config.update(sensibles_en_lote=["gobierno"], heads_autorizados=["c" * 40])
        for activo in (True, False):
            with self.subTest(activo=activo):
                nuevo = self.lote_con_normal(["AGENTS.md"], activo=activo)
                self.assertEqual(nuevo.entradas[0].admision, Retenido(Motivo.MERGE_READY))
                self.assertEqual(tuple(t.recibos for t in nuevo.trenes), ((entrega(2).recibo,),))

    def test_sen_7114_categoria_fuera_de_lista_fuerza_solo(self):
        self.config.update(sensibles_en_lote=["gobierno"], heads_autorizados=[HEAD])
        self.prueba_sen_7114_lote(["AGENTS.md", ".github/check.yml"], solo=True)
        self.config["sensibles"]["ci"].append("AGENTS.md")
        self.prueba_sen_7114_lote(["AGENTS.md"], solo=True)

    def marcar_alto_riesgo(self, *, verificado=True):
        self.pr["labels"] = [{"name": "alto-riesgo"}]
        apto = json.loads(self.apto.read_text())
        if verificado:
            apto["verificado"] = "Verificado"
        self.apto.write_text(json.dumps(apto))

    def test_sen_7114_alto_riesgo_fuerza_solo(self):
        self.config.update(sensibles_en_lote=["gobierno"], heads_autorizados=[HEAD])
        self.marcar_alto_riesgo()
        self.prueba_sen_7114_lote(["AGENTS.md"], solo=True)

    def test_sen_7114_r2_alto_riesgo_autorizado_viaja_en_lote(self):
        self.config.update(alto_riesgo_en_lote=True, heads_autorizados=[HEAD])
        self.marcar_alto_riesgo()
        self.prueba_sen_7114_lote(["src/app.py"], solo=False)

    def test_sen_7114_r2_alto_riesgo_sin_autorizacion_sigue_retenido(self):
        self.config.update(alto_riesgo_en_lote=True, heads_autorizados=[])
        self.marcar_alto_riesgo()
        for piloto, motivo in ((True, Motivo.PILOTO), (False, Motivo.MERGE_READY)):
            self.config["excluir_alto_riesgo"] = piloto
            for activo in (True, False):
                with self.subTest(piloto=piloto, activo=activo):
                    nuevo = self.lote_con_normal(["src/app.py"], activo=activo)
                    self.assertEqual(nuevo.entradas[0].admision, Retenido(motivo))
                    self.assertEqual(tuple(t.recibos for t in nuevo.trenes),
                                     ((entrega(2).recibo,),))

    def test_sen_7114_r2_alto_riesgo_sin_verificar_sigue_retenido(self):
        self.config.update(alto_riesgo_en_lote=True, heads_autorizados=[HEAD])
        self.marcar_alto_riesgo(verificado=False)
        for activo in (True, False):
            with self.subTest(activo=activo):
                nuevo = self.lote_con_normal(["src/app.py"], activo=activo)
                self.assertEqual(nuevo.entradas[0].admision, Retenido(Motivo.VERIFICADO))
                self.assertEqual(tuple(t.recibos for t in nuevo.trenes),
                                 ((entrega(2).recibo,),))

    def test_sen_7114_r2_alto_riesgo_sin_excepcion_va_solo(self):
        self.config["heads_autorizados"] = [HEAD]
        self.marcar_alto_riesgo()
        self.prueba_sen_7114_lote(["src/app.py"], solo=True)
        self.config["alto_riesgo_en_lote"] = False
        self.prueba_sen_7114_lote(["src/app.py"], solo=True)

    def test_sen_7114_r2_alto_riesgo_con_categoria_fuera_de_lista_va_solo(self):
        self.config.update(alto_riesgo_en_lote=True, sensibles_en_lote=["gobierno"],
                           heads_autorizados=[HEAD])
        self.marcar_alto_riesgo()
        self.prueba_sen_7114_lote(["AGENTS.md", ".github/check.yml"], solo=True)

    def test_sen_7114_r2_alto_riesgo_con_ventana_va_solo_en_activo(self):
        self.config.update(alto_riesgo_en_lote=True, heads_autorizados=[{
            "head": HEAD, "ventana": {"inicio": "2026-01-01T00:00:00Z",
                                      "fin": "2026-01-01T01:00:00Z"}
        }])
        self.marcar_alto_riesgo()
        nuevo = self.lote_con_normal(["src/app.py"], activo=True)
        self.assertEqual(nuevo.entradas[0].admision, Admitido())
        self.assertEqual(tuple(t.recibos for t in nuevo.trenes), ((self.entrega.recibo,),))

    def test_sen_7114_r2_config_alto_riesgo_en_lote(self):
        ruta = self.raiz / "config.json"
        for valor in (True, False):
            self.config["alto_riesgo_en_lote"] = valor
            ruta.write_text(json.dumps({"repos": {REPO: self.config}}))
            self.assertEqual(configuracion(ruta, REPO), self.config)
        for valor in ("true", 1, None):
            with self.subTest(valor=valor):
                self.config["alto_riesgo_en_lote"] = valor
                ruta.write_text(json.dumps({"repos": {REPO: self.config}}))
                with self.assertRaisesRegex(ValueError, "alto_riesgo_en_lote"):
                    configuracion(ruta, REPO)

    def test_sen_7114_ventana_fuerza_solo_en_activo(self):
        self.config.update(sensibles_en_lote=["gobierno"], heads_autorizados=[{
            "head": HEAD, "ventana": {"inicio": "2026-01-01T00:00:00Z",
                                      "fin": "2026-01-01T01:00:00Z"}
        }])
        nuevo = self.lote_con_normal(["AGENTS.md"], activo=True)
        self.assertEqual(nuevo.entradas[0].admision, Admitido())
        self.assertEqual(tuple(t.recibos for t in nuevo.trenes), ((self.entrega.recibo,),))

    def test_sen_7114_sin_excepciones_fuerza_solo(self):
        self.config["heads_autorizados"] = [HEAD]
        self.prueba_sen_7114_lote(["AGENTS.md"], solo=True)
        self.config["sensibles_en_lote"] = []
        self.prueba_sen_7114_lote(["AGENTS.md"], solo=True)

    def test_sen_7114_config_sensibles_en_lote(self):
        ruta = self.raiz / "config.json"
        for valor in ([], ["gobierno"]):
            self.config["sensibles_en_lote"] = valor
            ruta.write_text(json.dumps({"repos": {REPO: self.config}}))
            self.assertEqual(configuracion(ruta, REPO), self.config)
        for valor in (["desconocida"], "gobierno", [None], None):
            with self.subTest(valor=valor):
                self.config["sensibles_en_lote"] = valor
                ruta.write_text(json.dumps({"repos": {REPO: self.config}}))
                with self.assertRaisesRegex(ValueError, "sensibles_en_lote"):
                    configuracion(ruta, REPO)

    def test_sen_7114_renombre_desde_ci_fuerza_solo(self):
        self.config.update(sensibles_en_lote=["gobierno"], heads_autorizados=[HEAD])
        for activo in (True, False):
            with self.subTest(activo=activo):
                self.archivos = [[{"filename": "AGENTS.md", "status": "modified"},
                                  {"filename": "docs/check.yml", "status": "renamed",
                                   "previous_filename": ".github/workflows/check.yml"}]]
                self.pr["changed_files"] = 2
                observador = GitHubGit(self.config, ejecutar=self.ejecutar, activo=activo,
                                       sombra=not activo)
                v = observador.vista(self.entrega)
                self.assertTrue(v.sensible)
                self.assertTrue(v.requiere_solo)

    def test_sen_7114_renombre_desde_ci_exige_autorizacion(self):
        self.archivos = [[{"filename": "docs/check.yml", "status": "renamed",
                           "previous_filename": ".github/workflows/check.yml"}]]
        self.config["heads_autorizados"] = ["c" * 40]
        self.assertEqual(self.decision(), Retenido(Motivo.MERGE_READY))

    def test_sen_7114_categoria_adicional_invalida_falla_al_leer(self):
        ruta = self.raiz / "config.json"
        for valor in (None, [], [""], "scripts/*"):
            with self.subTest(valor=valor):
                self.config["sensibles"]["despliegue"] = valor
                ruta.write_text(json.dumps({"repos": {REPO: self.config}}))
                with self.assertRaisesRegex(ValueError, "patrones sensibles"):
                    configuracion(ruta, REPO)
        self.config["sensibles"]["despliegue"] = ["scripts/deploy/*"]
        ruta.write_text(json.dumps({"repos": {REPO: self.config}}))
        self.assertEqual(configuracion(ruta, REPO), self.config)

    def test_sen_7114_entrega_en_sombra_conserva_opcion_de_lote(self):
        self.config.update(sensibles_en_lote=["gobierno"], heads_autorizados=[HEAD])
        self.archivos = [[{"filename": "AGENTS.md"}]]
        self.comprobar_entrega_en_lote_sombra()

    def test_sen_7114_r2_entrega_alto_riesgo_en_sombra_conserva_opcion_de_lote(self):
        self.config.update(alto_riesgo_en_lote=True, heads_autorizados=[HEAD])
        self.marcar_alto_riesgo()
        self.comprobar_entrega_en_lote_sombra()

    def comprobar_entrega_en_lote_sombra(self):
        observador = GitHubGit(self.config, ejecutar=self.ejecutar, sombra=True)
        args = cli.parser().parse_args(["entregar", "--repo", REPO, "--pr", "1",
            "--head", HEAD, "--apto", str(self.apto), "--dirigida", str(self.dirigida)])
        with patch.object(cli, "modo", return_value="sombra"):
            codigo, resultado = cli.ejecutar(args, observador, 100)
        self.assertEqual(codigo, 0)
        self.assertEqual(resultado["admision"]["estado"], "admitido")
        self.ledger().entregar(entrega(2), 100, Entrada(entrega(2), 100, Admitido(), 100))
        original = self.ledger().estado(REPO, sombra=True, doble_camino=True)
        nuevo, _ = paso_sombra(original, Observacion(100, BASE, (vista(2),)))
        self.assertEqual(tuple(t.recibos for t in nuevo.trenes),
                         (tuple(sorted((self.entrega.recibo, entrega(2).recibo))),))

    def test_pagina_dos_ci_pendiente_retiene(self):
        self.checks.append({"check_runs": [{"id": 2, "name": "tests", "status": "in_progress", "conclusion": None}]})
        self.assertEqual(self.decision(), Retenido(Motivo.CI))

    def test_status_nuevo_pendiente_no_oculto_por_verde(self):
        self.statuses = [[{"context": "build", "state": "pending"}],
                         [{"context": "build", "state": "success"}]]
        self.assertEqual(self.decision(), Retenido(Motivo.CI))

    def test_ci_ausente_retiene(self):
        self.checks = [{"check_runs": []}]
        self.assertEqual(self.decision(), Retenido(Motivo.CI))

    def test_timeout_conserva_ventana(self):
        e = Estado(REPO, (Entrada(self.entrega, 1),))
        e, _ = paso(e, self.observador().observar(e, 100))
        original = self.ejecutar

        def con_timeout(args, **kwargs):
            if args[0] == "gh" and "/files?" in args[4]:
                raise subprocess.TimeoutExpired(args, 60)
            return original(args, **kwargs)
        e, _ = paso(e, GitHubGit(self.config, ejecutar=con_timeout).observar(e, 200))
        self.assertEqual(e.entradas[0].admision, Retenido(Motivo.INCIERTO))
        self.assertEqual(e.entradas[0].admitida, 100)
        e, efectos = paso(e, self.observador().observar(e, 3700))
        self.assertEqual(len(e.trenes), 1)

    def test_rechazadas_no_se_observan(self):
        e = Estado(REPO, tuple(Entrada(replace(self.entrega, head=format(i, "040x")), i,
                                       Rechazado(Motivo.HEAD)) for i in range(1, 51)))
        obs = self.observador().observar(e, 100)
        self.assertEqual(obs.prs, ())
        self.assertEqual(sum(a[0] == "gh" for a in self.llamadas), 1)

    def test_ci_vacio_declarado_admite(self):
        self.checks = [{"check_runs": []}]
        self.config["checks_esperados"] = []
        self.assertIsInstance(self.decision(), Admitido)

    def test_check_esperado_ausente_retiene(self):
        self.config["checks_esperados"] = ["lint", "e2e"]
        self.assertEqual(self.decision(), Retenido(Motivo.CI))
        self.config["checks_esperados"] = ["lint"]
        self.assertIsInstance(self.decision(), Admitido)

    def test_rerun_supera_corrida_cancelada(self):
        self.checks = [{"check_runs": [
            {"id": 9, "name": "lint", "status": "completed", "conclusion": "success"},
            {"id": 3, "name": "lint", "status": "completed", "conclusion": "cancelled"}]}]
        self.assertIsInstance(self.decision(), Admitido)
        self.checks = [{"check_runs": [
            {"id": 3, "name": "lint", "status": "completed", "conclusion": "success"},
            {"id": 9, "name": "lint", "status": "completed", "conclusion": "failure"}]}]
        self.assertEqual(self.decision(), Retenido(Motivo.CI))

    def test_archivos_truncados_retienen(self):
        self.pr["changed_files"] = 3001
        self.assertEqual(self.decision(), Retenido(Motivo.INCIERTO))

    def test_head_cambia_durante_lectura(self):
        observador = self.observador()
        original = observador.api
        lecturas = []
        def api(ruta, paginado=False):
            valor = original(ruta, paginado)
            if ruta.endswith("/pulls/1"):
                lecturas.append(1)
                if len(lecturas) == 2:
                    valor["head"]["sha"] = "c" * 40
            return valor
        observador.api = api
        e = Estado(REPO, (Entrada(self.entrega, 1),))
        nuevo, _ = paso(e, observador.observar(e, 100))
        self.assertEqual(nuevo.entradas[0].admision, Rechazado(Motivo.HEAD))

    def test_comentario_json_y_rechazo_url_ajena(self):
        observador = self.observador()
        observador.api = lambda ruta: {"body": self.apto.read_text(),
                                       "issue_url": "https://api.github.com/repos/example/project/issues/1"}
        referencia = "https://github.com/example/project/pull/1#issuecomment-23"
        self.assertEqual(observador.evidencia(referencia, REPO, 1)["veredicto"], "APTO")
        for url in ("https://other.example/example/project/pull/1#issuecomment-23",
                    "https://github.com/example/project/pull/2#issuecomment-23"):
            with self.assertRaises(ValueError):
                observador.evidencia(url, REPO, 1)

    def test_config_categorias_obligatorias(self):
        ruta = self.raiz / "config.json"
        ruta.write_text(json.dumps({"repos": {REPO: self.config}}))
        self.assertEqual(configuracion(ruta, REPO), self.config)
        self.config["sensibles"]["gobierno"] = []
        ruta.write_text(json.dumps({"repos": {REPO: self.config}}))
        with self.assertRaises(ValueError):
            configuracion(ruta, REPO)

    def test_config_identidad_commits_invalida_falla_al_leer(self):
        ruta = self.raiz / "config.json"
        self.config["identidad_commits"] = {"nombre": "Felix", "email": "1+felix@users.noreply.github.com"}
        ruta.write_text(json.dumps({"repos": {REPO: self.config}}))
        self.assertEqual(configuracion(ruta, REPO), self.config)
        for valor in ("Felix", {"nombre": "Felix"}, {"nombre": "", "email": "a@b"},
                      {"nombre": " Felix", "email": "a@b"}, {"nombre": "Felix", "email": "sin-arroba"},
                      {"nombre": "Felix", "email": "a<b@c"}, {"nombre": "Fe\nlix", "email": "a@b"},
                      {"nombre": "Felix", "email": "a@b", "extra": "x"},
                      {"nombre": "Felix Jr.", "email": "a@b"}, {"nombre": "Felix", "email": "'a@b"},
                      {"nombre": "...", "email": "a@b"}, {"nombre": "Felix", "email": "@"},
                      {"nombre": "Felix", "email": "felix@"}, {"nombre": "Felix", "email": "@example.org"}):
            self.config["identidad_commits"] = valor
            ruta.write_text(json.dumps({"repos": {REPO: self.config}}))
            with self.assertRaises(ValueError, msg=repr(valor)):
                configuracion(ruta, REPO)

    def test_git_real_conflicto_con_origen_local_sin_writes(self):
        repo = self.raiz / "origen"
        repo.mkdir()
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], check=True,
                                  capture_output=True, text=True, timeout=20).stdout.strip()
        git("init", "-b", "main")
        git("config", "user.name", "Fixture")
        git("config", "user.email", "fixture@example.invalid")
        archivo = repo / "archivo"
        archivo.write_text("base\n")
        git("add", "archivo")
        git("commit", "-m", "base")
        git("checkout", "-b", "ticket")
        archivo.write_text("ticket\n")
        git("commit", "-am", "ticket")
        head = git("rev-parse", "HEAD")
        git("checkout", "main")
        archivo.write_text("main\n")
        git("commit", "-am", "main")
        base = git("rev-parse", "HEAD")
        self.config["git"] = str(repo)
        self.pr["head"]["sha"] = head
        self.pr["base"]["sha"] = base
        self.apto.write_text(json.dumps({"veredicto": "APTO", "head": head, "hallazgos_abiertos": 0}))
        self.dirigida.write_text(json.dumps({"head": head, "resultado": "limpio"}))
        def ejecutar(args, **kwargs):
            return subprocess.run(args, **kwargs) if args[0] == "git" else self.ejecutar(args, **kwargs)
        observador = GitHubGit(self.config, ejecutar=ejecutar)
        entrada = replace(self.entrega, head=head)
        objetos_antes = sorted(str(p.relative_to(repo)) for p in (repo / ".git/objects").rglob("*"))
        lectura = observador.vista(entrada)
        self.assertTrue(lectura.conflicto)
        self.assertEqual(git("rev-parse", "HEAD"), base)
        self.assertEqual(objetos_antes, sorted(str(p.relative_to(repo)) for p in (repo / ".git/objects").rglob("*")))


if __name__ == "__main__":
    unittest.main()
