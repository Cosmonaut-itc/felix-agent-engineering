"""Medición de fases con filas sintéticas y un ledger aislado."""
import json
import os
from unittest.mock import patch

from test_tren import Aislado

from dominio import Congelado, Efecto, Estado, Terminal, Tren
from fases import medir_fases


REPO = "owner/project"
DESDE = "1970-01-01T00:00:00Z"
HASTA = "1970-01-01T00:20:00Z"


class FasesTests(Aislado):
    def setUp(self):
        super().setUp()
        self.events = []
        self.effects = []

    def evento(self, instante, tipo, payload):
        self.events.append({"seq": len(self.events) + 1, "repo": REPO,
                            "tipo": tipo, "instante": instante,
                            "payload": json.dumps(payload)})

    def operativo(self, instante, tren, fase, terminal=None, pausa=False, consumido=None):
        fase = {"_tipo": fase, **({"resultado": terminal} if terminal else {})}
        self.evento(instante, "operativo", {"consumido": consumido and consumido["clave"], "efectos": [],
            "estado": {"trenes": [{"id": tren, "recibos": ["a", "b", "c"], "fase": fase}],
                       "pausa": {"_tipo": "Pausa", "motivo": "manual"} if pausa else None}})

    def efecto(self, tren, tipo):
        clave = tren + "/" + tipo
        self.effects.append({"repo": REPO, "clave": clave, "payload": json.dumps(
            {"_tipo": "Efecto", "clave": clave, "tipo": tipo, "datos": [tren]})})
        return {"clave": clave}

    def hasta_certificado(self, tren, inicio):
        self.operativo(inicio, tren, "Congelado")
        ci = self.efecto(tren, "solicitar-ci")
        self.evento(inicio + 3, "efecto-iniciado", ci)
        self.evento(inicio + 5, "efecto-fallido", ci)
        self.evento(inicio + 10, "efecto-resultado", ci)
        self.evento(inicio + 11, "efecto-resultado", ci)
        suite = self.efecto(tren, "iniciar-suite")
        self.evento(inicio + 30, "efecto-iniciado", suite)
        self.evento(inicio + 32, "efecto-iniciado", suite)
        # El resultado del efecto es el recibo del lanzamiento, no el fin de la suite.
        self.evento(inicio + 33, "efecto-resultado", suite)
        self.operativo(inicio + 60, tren, "EsperandoCI", consumido=suite)
        self.operativo(inicio + 70, tren, "Certificado")
        self.operativo(inicio + 71, tren, "Promoviendo")

    def informe(self, desde=DESDE, hasta=HASTA):
        return medir_fases(self.events, self.effects, REPO, desde, hasta)

    def test_verde_directo_mide_los_seis_hitos_y_cinco_tramos(self):
        self.hasta_certificado("verde", 0)
        self.operativo(100, "verde", "Terminal", "ATERRIZADO")
        self.operativo(110, "verde", "Terminal", "ATERRIZADO", pausa=True)
        self.assertEqual(self.informe()["trenes"], [{
            "id": "verde", "miembros": 3, "terminal": "ATERRIZADO", "verde_directo": True,
            "hitos": {"cierre": 0, "ci_solicitado": 10, "suite_iniciada": 30,
                      "suite_terminada": 60, "certificado": 70, "fin": 100},
            "tramos_s": {"armado_s": 10, "ci_s": 20, "suite_s": 30,
                         "promocion_s": 40, "total_s": 100}}])

    def test_diagnostico_y_absolucion_cuentan_por_terminal(self):
        self.operativo(100, "absuelto", "Congelado")
        self.operativo(120, "absuelto", "Diagnosticando")
        self.operativo(150, "absuelto", "Terminal", "ABSUELTO")
        self.operativo(160, "aterrizado", "Diagnosticando")
        self.operativo(180, "aterrizado", "Terminal", "ATERRIZADO")
        informe = self.informe()
        self.assertTrue(all(not tren["verde_directo"] for tren in informe["trenes"]))
        self.assertEqual(informe["resumen"]["otros"], {"ABSUELTO": 1, "ATERRIZADO": 1})

    def test_pausa_durante_el_tren_impide_verde_directo(self):
        self.hasta_certificado("pausado", 100)
        self.operativo(180, "pausado", "Promoviendo", pausa=True)
        self.operativo(200, "pausado", "Terminal", "ATERRIZADO")
        informe = self.informe()
        self.assertFalse(informe["trenes"][0]["verde_directo"])
        self.assertEqual(informe["resumen"]["otros"], {"ATERRIZADO": 1})

    def test_reanudacion_usa_el_ultimo_resultado_e_impide_verde_directo(self):
        self.operativo(0, "reanudado", "Congelado")
        suite = self.efecto("reanudado", "iniciar-suite")
        self.evento(10, "efecto-iniciado", suite)
        self.evento(11, "efecto-resultado", suite)
        self.operativo(30, "reanudado", "Corriendo", consumido=suite)
        reanudacion = self.efecto("reanudado", "reanudar-suite")
        self.evento(40, "efecto-iniciado", reanudacion)
        self.evento(41, "efecto-resultado", reanudacion)
        self.operativo(70, "reanudado", "Corriendo", consumido=reanudacion)
        self.operativo(80, "reanudado", "Promoviendo")
        self.operativo(100, "reanudado", "Terminal", "ATERRIZADO")
        tren, = self.informe()["trenes"]
        self.assertFalse(tren["verde_directo"])
        self.assertEqual(tren["hitos"]["suite_terminada"], 70)
        self.assertEqual(tren["hitos"]["certificado"], 80)
        self.assertEqual(tren["tramos_s"]["suite_s"], 60)

    def test_vivo_conserva_hitos_parciales_y_queda_fuera_del_resumen(self):
        self.operativo(5, "vivo", "Congelado")
        self.evento(15, "efecto-resultado", self.efecto("vivo", "solicitar-ci"))
        informe = self.informe()
        tren, = informe["trenes"]
        self.assertIsNone(tren["terminal"])
        self.assertIsNone(tren["hitos"]["fin"])
        self.assertEqual(tren["tramos_s"], {"armado_s": 10, "ci_s": None, "suite_s": None,
                                            "promocion_s": None, "total_s": None})
        self.assertEqual(informe["resumen"]["verdes_directos"]["n"], 0)
        self.assertEqual(informe["resumen"]["verdes_directos"]["tramos_s"]["total_s"],
                         {"p50": None, "p90": None, "max": None})
        self.assertEqual(informe["resumen"]["otros"], {})

    def test_ventana_aplica_al_primer_cierre_y_no_al_fin(self):
        self.operativo(9, "anterior", "Congelado")
        self.operativo(10, "incluido", "Congelado")
        self.operativo(11, "anterior", "Terminal", "ABORTADO")
        self.operativo(20, "posterior", "Congelado")
        self.operativo(21, "incluido", "Terminal", "ATERRIZADO")
        tren, = self.informe("1970-01-01T00:00:10Z", "1970-01-01T00:00:20Z")["trenes"]
        self.assertEqual(tren["id"], "incluido")
        self.assertEqual(tren["hitos"]["fin"], 21)

    def test_percentiles_por_rango_mas_cercano(self):
        for posicion, i in enumerate((8, 1, 5, 2, 10, 9, 7, 3, 6, 4)):
            inicio = posicion * 115
            self.hasta_certificado(str(i), inicio)
            self.operativo(inicio + 100 + i, str(i), "Terminal", "ATERRIZADO")
        resumen = self.informe()["resumen"]["verdes_directos"]
        self.assertEqual(resumen["n"], 10)
        self.assertEqual(resumen["tramos_s"]["total_s"], {"p50": 105, "p90": 109, "max": 110})
        self.assertEqual(resumen["tramos_s"]["ci_s"], {"p50": 20, "p90": 20, "max": 20})

    def test_cli_rechaza_fecha_sin_zona_antes_de_abrir_el_ledger(self):
        resultado = self.cli("fases", "--repo", REPO, "--desde", "1970-01-01T00:00:00", "--hasta", HASTA)
        self.assertEqual(resultado.returncode, 2)
        self.assertIn("--desde", resultado.stderr)
        self.assertEqual(resultado.stdout, "")

    def test_cli_lee_ledger_sin_inscripcion_ni_escrituras(self):
        ledger = self.ledger()
        tren = Tren("cli", ("a",), Congelado("base-sintetica"))
        ci = Efecto("solicitud-sintetica", "solicitar-ci", (tren.id,))
        ledger.guardar_paso(Estado(REPO, trenes=(tren,)), (ci,), 10)
        ledger.registrar_efecto(REPO, ci, 20, {}, completo=True)
        final = Tren(tren.id, tren.recibos, Terminal("ATERRIZADO"))
        ledger.guardar_paso(Estado(REPO, trenes=(final,)), (), 30, consumido=ci.clave)
        ruta = ledger.directorio / "ledger.sqlite3"
        antes = ruta.read_bytes()
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.raiz / "config")}):
            # El lector debe funcionar mientras el candado del tren está ocupado.
            with ledger.candado(REPO):
                archivos = set(ledger.directorio.iterdir())
                resultado = self.cli("fases", "--repo", REPO, "--desde", DESDE, "--hasta", HASTA)
                self.assertEqual(set(ledger.directorio.iterdir()), archivos)
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        linea, = resultado.stdout.splitlines()
        informe = json.loads(linea)
        self.assertEqual((informe["repo"], informe["desde"], informe["hasta"]), (REPO, DESDE, HASTA))
        tren, = informe["trenes"]
        self.assertEqual((tren["id"], tren["terminal"], tren["miembros"]), ("cli", "ATERRIZADO", 1))
        self.assertEqual(tren["hitos"]["ci_solicitado"], 20)
        self.assertEqual(ruta.read_bytes(), antes)
