"""Pruebas del CLI con entregas y snapshots de un ledger sintético."""
from dataclasses import replace
from datetime import datetime, timezone
import json

from test_tren import Aislado, REPO, entrega

from dominio import Admitido, Congelado, Efecto, Entrada, Estado, Motivo, Pausa, Retenido, Terminal, Tren


HORA = 3600


def iso(segundos):
    return datetime.fromtimestamp(segundos, timezone.utc).isoformat()


class LatenciaTests(Aislado):
    def setUp(self):
        super().setUp()
        self.db = self.ledger()

    def admitir(self, pr, instante=0, head="a" * 40):
        nueva = entrega(pr, head)
        self.db.entregar(nueva, instante)
        return Entrada(nueva, instante, Admitido())

    def paso(self, instante, entradas=(), trenes=(), pausa=None, efectos=()):
        self.db.guardar_paso(Estado(REPO, entradas, trenes, pausa), efectos, instante)

    def lote(self, ident, *entradas):
        return Tren(ident, tuple(e.entrega.recibo for e in entradas), Congelado("base"))

    def informe(self, desde=0, hasta=HORA, prs=None):
        opciones = []
        if prs is not None:
            archivo = self.raiz / "prs.json"
            archivo.write_text(json.dumps(prs), encoding="utf-8")
            opciones = ["--prs", str(archivo)]
        salida = self.cli("latencia", "--repo", REPO,
                          "--desde", iso(desde), "--hasta", iso(hasta), *opciones)
        self.assertEqual(salida.returncode, 0, salida.stdout + salida.stderr)
        return json.loads(salida.stdout)

    def comprobar_causa(self, informe, causa, horas_pr, horas_carril):
        fila = informe["horas_pr_por_causa"][causa]
        self.assertAlmostEqual(fila["horas_pr"], horas_pr)
        self.assertAlmostEqual(fila["horas_carril"], horas_carril)

    def test_pausa_manual_cuenta_cada_pr_admitido(self):
        self.paso(0, (self.admitir(1), self.admitir(2)), pausa=Pausa("manual"))
        self.comprobar_causa(self.informe(), "pausa manual", horas_pr=2, horas_carril=1)

    def test_lote_verde_y_absuelto_tienen_causas_distintas(self):
        primero, segundo = self.admitir(1), self.admitir(2)
        verde, absuelto = self.lote("verde", primero), self.lote("absuelto", segundo)
        self.paso(0, (primero, segundo), (verde,))
        verde = replace(verde, fase=Terminal("ATERRIZADO"))
        primero = replace(primero, terminal=verde.fase)
        self.paso(HORA, (primero, segundo), (verde, absuelto))
        absuelto = replace(absuelto, fase=Terminal("ABSUELTO"))
        self.paso(2 * HORA, (primero, segundo), (verde, absuelto))
        informe = self.informe(hasta=3 * HORA)
        self.comprobar_causa(informe, "lote verde (trabajo útil)", horas_pr=2, horas_carril=1)
        self.comprobar_causa(informe, "lote fallido: ABSUELTO", horas_pr=1, horas_carril=1)
        self.assertEqual([fila["pr"] for fila in informe["por_pr"]], [1])

    def test_retencion_por_conflicto_no_ocupa_el_carril(self):
        retenida = replace(self.admitir(1), admision=Retenido(Motivo.CONFLICTO))
        self.paso(0, (retenida,))
        informe = self.informe()
        self.comprobar_causa(informe, "retención de admisión: conflicto", horas_pr=1, horas_carril=0)
        self.comprobar_causa(informe, "carril libre", horas_pr=0, horas_carril=1)

    def test_devuelto_reparte_responsabilidad_y_conserva_la_primera_entrega(self):
        primero, segundo = self.admitir(1), self.admitir(2)
        devuelto = self.lote("devuelto", primero)
        self.paso(0, (primero, segundo), (devuelto,))
        efecto = Efecto("devolucion", "devolver", (devuelto.id, primero.entrega.recibo, "{}"))
        devuelto = replace(devuelto, fase=Terminal("DEVUELTO"))
        reentrega = self.admitir(1, HORA, "b" * 40)
        verde = self.lote("verde", segundo, reentrega)
        anterior = replace(primero, terminal=devuelto.fase)
        self.paso(HORA, (anterior, segundo, reentrega), (devuelto, verde), efectos=(efecto,))
        self.paso(2 * HORA, (anterior, segundo, reentrega),
                  (devuelto, replace(verde, fase=Terminal("ATERRIZADO"))))

        informe = self.informe(desde=HORA, hasta=3 * HORA)
        self.assertEqual([fila["pr"] for fila in informe["por_pr"]], [1, 2])
        for fila, sesion, otras in zip(informe["por_pr"], (1, 0), (0, 1)):
            self.assertAlmostEqual(fila["total"], 2)
            self.assertAlmostEqual(fila["tren"], 1)
            self.assertAlmostEqual(fila["sesion"], sesion)
            self.assertAlmostEqual(fila["otras"], otras)
        self.assertEqual(informe["resumen"]["n"], 2)
        for estadistico in ("media", "p50", "p90"):
            self.assertAlmostEqual(informe["resumen"][estadistico], 1)

    def test_prs_usa_la_hora_de_github_e_incluye_merges_externos(self):
        primero, externo = self.admitir(1), self.admitir(2)
        lote = self.lote("verde", primero)
        self.paso(0, (primero, externo), (lote,))
        self.paso(2 * HORA, (primero, externo), (replace(lote, fase=Terminal("ATERRIZADO")),))
        prs = [{"number": 1, "state": "MERGED", "mergedAt": iso(HORA)},
               {"number": 2, "state": "MERGED", "mergedAt": iso(HORA + 30)}]
        informe = self.informe(hasta=3 * HORA, prs=prs)
        self.assertEqual([fila["pr"] for fila in informe["por_pr"]], [1, 2])
        self.assertEqual([fila["total"] for fila in informe["por_pr"]], [1, (HORA + 30) / HORA])
        self.assertAlmostEqual(informe["por_pr"][1]["tren"], (HORA + 30) / HORA)
        self.assertEqual(self.informe(hasta=3 * HORA, prs=[])["por_pr"], [])
        solo_externo = self.informe(hasta=3 * HORA, prs=prs[1:])
        horas_pr = sum(fila["horas_pr"] for fila in solo_externo["horas_pr_por_causa"].values())
        # El conteo va por minuto: el merge a HORA + 30 s cierra el minuto 61.
        self.assertAlmostEqual(horas_pr, 2 + 61 / 60)

    def test_latencia_decisiones_mide_pausas_levantadas_con_su_duracion_completa(self):
        infra = Pausa("infraestructura", detalle="salida-sin-recibo")
        self.paso(0, pausa=infra)
        self.paso(600)
        self.paso(900, pausa=infra)
        self.paso(2100)
        self.paso(2400, pausa=Pausa("necesita-juicio", detalle="presupuesto-agotado"))
        informe = self.informe(desde=300)
        self.assertEqual(informe["latencia_decisiones"], {
            "infra: salida sin recibo del lanzador": {"n": 2, "p50_min": 15, "max_min": 20},
        })
