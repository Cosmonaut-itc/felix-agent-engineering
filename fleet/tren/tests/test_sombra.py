"""U12: doble camino y medición sin red. Ejecutar sólo en el host de pruebas."""
from copy import deepcopy
from dataclasses import replace
import json
import os
import subprocess
from unittest.mock import patch

from test_tren import (Aislado, BASE, HEAD, REPO, cli, entrega, estado,
                       observacion, vista)
from comparar_eventos_sombra import comparar
from dominio import (Admitido, Candidato, Congelado, Corriendo, Entrada, Estado, Motivo,
                     Rechazado, Retenido, Terminal, datos, desde_datos, paso)
from inscripcion import modo
from ledger import Ledger, directorio_estado, filas_medicion
from medicion import instante_iso, medir
from observador import Falso, GitHubGit
from sombra import DURACION_SUITE_ESTIMADA, lotes_fifo, paso_sombra


class SombraTests(Aislado):
    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.raiz / "config")})
        env.start()
        self.addCleanup(env.stop)
        self.registry = self.raiz / "config/pao-tren/inscritos"
        self.registry.parent.mkdir(parents=True)
        self.registry.write_text(REPO.upper() + " SoMbRa # piloto\n")

    def entregar(self, pr=1, ahora=100, **cambios):
        args = cli.parser().parse_args(["entregar", "--repo", REPO, "--pr", str(pr),
            "--head", HEAD, "--apto", "apto.json", "--dirigida", "dirigida.json"])
        return cli.ejecutar(args, Falso(replace(observacion(), prs=(vista(pr, **cambios),))), ahora)

    def tick(self, ahora, prs, base=BASE):
        args = cli.parser().parse_args(["tick", "--repo", REPO, "--seco"])
        return cli.ejecutar(args, Falso(replace(observacion(), prs=tuple(prs), base=base)), ahora)

    def test_inscripcion_restrictiva(self):
        self.assertEqual(modo(REPO), "sombra")
        for texto in (REPO, REPO + " desconocido", REPO + " sombra extra",
                      REPO + " sombra\n" + REPO, REPO + " otro\n" + REPO + " sombra"):
            self.registry.write_text(texto)
            self.assertEqual(modo(REPO), "activo", texto)
        self.registry.write_text("# vacío\n")
        self.assertIsNone(modo(REPO))
        self.registry.chmod(0)
        with self.assertRaises(OSError):
            modo(REPO)
        self.registry.chmod(0o600)
        self.registry.write_bytes(b"\xff")
        with self.assertRaises(UnicodeError):
            modo(REPO)

    def test_admision_al_entregar_durable_e_idempotente(self):
        self.assertEqual(self.entregar()[1]["admision"]["estado"], "admitido")
        self.entregar(2, 200, ci="pendiente")
        self.entregar(3, 300, base="otra")
        filas = filas_medicion(REPO)
        informe = medir(*filas, REPO, 0, 4000)
        self.assertEqual([d["admision"] for d in informe["entregas"]], [
            {"estado": "admitido", "motivo": "admision-d7"},
            {"estado": "retenido", "motivo": "ci-no-verde"},
            {"estado": "rechazado", "motivo": "base-no-main"}])
        self.entregar(1, 500, ci="pendiente")
        self.assertEqual(filas_medicion(REPO), filas)
        ledger = self.ledger()
        self.assertIsInstance(ledger.estado(REPO).entradas[0].admision, Retenido)
        self.assertIsInstance(ledger.estado(REPO, sombra=True).entradas[0].admision, Admitido)

    def test_entrega_sin_observacion_conserva_custodia(self):
        args = cli.parser().parse_args(["entregar", "--repo", REPO, "--pr", "1",
            "--head", HEAD, "--apto", "apto.json", "--dirigida", "dirigida.json"])
        codigo, resultado = cli.ejecutar(args, ahora=100)
        self.assertEqual(codigo, 0)
        self.assertEqual(resultado["admision"]["motivo"], "observacion-incompleta")
        self.assertEqual(len(self.ledger().estado(REPO).entradas), 1)

    def test_siete_dias_main_avanza_y_merges_externos_no_pausan(self):
        for dia in range(7):
            t = 100 + dia * 86400
            n = 3 if dia % 2 else 1
            prs = list(range(dia * 3 + 1, dia * 3 + n + 1))
            for i, pr in enumerate(prs):
                self.entregar(pr, t + i * 30)
            _, plan = self.tick(t + 90, [vista(pr) for pr in prs], format(dia + 1, "040x"))
            self.assertIsNone(plan["plan"]["estado"]["pausa"])
            _, plan = self.tick(t + 300, [vista(pr, abierto=False, mergeado=True)
                                         for pr in prs], format(dia + 2, "040x"))
            estado_sombra = desde_datos(plan["plan"]["estado"])
            self.assertTrue(all(e.terminal == Terminal("MERGEADO-FUERA")
                                for e in estado_sombra.entradas))
            _, plan = self.tick(t + 3600, [], format(dia + 2, "040x"))
            self.assertEqual(plan["plan"]["efectos"], [])
            self.assertIsNone(plan["plan"]["estado"]["pausa"])
            self.assertEqual(len(plan["plan"]["estado"]["trenes"]), sum(
                2 if anterior % 2 else 1 for anterior in range(dia)) + 1)
        ledger = self.ledger()
        for tabla in ("trains", "effects", "outbox"):
            self.assertEqual(ledger.db.execute("SELECT count(*) FROM " + tabla).fetchone()[0], 0)
        informe = medir(*filas_medicion(REPO), REPO, 0, 7 * 86400)
        self.assertEqual(informe["distribucion"], {"1": 7, "2": 3, "3": 0, "4": 0, "5": 0})
        self.assertEqual(informe["suites_ahorradas_estimadas"], 3)

    def test_cerrado_sin_merge_y_head_distinto_conservan_rechazo(self):
        for cambios, motivo in (({"abierto": False}, Motivo.CERRADO),
                                ({"head": "c" * 40, "abierto": False,
                                  "mergeado": True}, Motivo.HEAD)):
            nuevo, efectos = paso_sombra(estado(), replace(observacion(), prs=(vista(**cambios),)))
            self.assertEqual(nuevo.entradas[0].admision, Rechazado(motivo))
            self.assertIsNone(nuevo.entradas[0].terminal)
            self.assertFalse(efectos)
            self.assertIsNone(nuevo.pausa)

    def test_retenido_rechazado_mergeado_fuera_tambien_termina(self):
        for decision in (Retenido(Motivo.MERGE_READY), Rechazado(Motivo.CERRADO)):
            entrada = Entrada(entrega(), 0, decision)
            nuevo, _ = paso_sombra(Estado(REPO, (entrada,)), replace(
                observacion(), prs=(vista(abierto=False, mergeado=True),)))
            self.assertEqual(nuevo.entradas[0].terminal, Terminal("MERGEADO-FUERA"))

    def test_evento_u1_y_camino_activo_siguen_iguales(self):
        ledger = self.ledger()
        for pr in (1, 2, 3):
            ledger.entregar(entrega(pr), pr)
        viejo, efectos = paso(ledger.estado(REPO), observacion(3))
        ledger.sombra(viejo, efectos, 100)
        self.assertEqual(ledger.estado(REPO, sombra=True), viejo)
        self.assertIsInstance(viejo.trenes[0].fase, Congelado)
        nuevo, efectos = paso_sombra(viejo, replace(observacion(3), base="c" * 40))
        self.assertTrue(all(t.fase == Terminal("SIMULADO") for t in nuevo.trenes))
        self.assertFalse(efectos)
        self.registry.write_text(REPO + " modo-desconocido")
        _, plan = self.tick(200, [vista(1), vista(2), vista(3)])
        self.assertEqual(desde_datos(plan["plan"]["estado"]), viejo)
        informe = medir(*filas_medicion(REPO), REPO, 0, 4000)
        self.assertTrue(all(e["fuente"] == "sin-observacion-al-entregar"
                            for e in informe["entregas"]))

    def test_observador_merge_confirmado_y_terminal_no_releido(self):
        llamadas = []
        def ejecutar(args, **kwargs):
            llamadas.append(args)
            payload = ({"object": {"sha": BASE}} if "/git/ref/" in args[4] else
                       {"head": {"sha": HEAD}, "base": {"ref": "main"},
                        "state": "closed", "merged": True})
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")
        observador = GitHubGit({}, ejecutar=ejecutar, sombra=True)
        original = Estado(REPO, (Entrada(entrega(), 0, Rechazado(Motivo.CERRADO)),))
        obs = observador.observar(original, 100)
        self.assertTrue(obs.prs[0].mergeado)
        nuevo, _ = paso_sombra(original, obs)
        llamadas[:] = []
        self.assertFalse(observador.observar(nuevo, 200).prs)
        self.assertEqual(len(llamadas), 1)

    def test_medir_cli_solo_lectura_y_determinista(self):
        self.entregar()
        ruta = directorio_estado() / "ledger.sqlite3"
        contenido, stat = ruta.read_bytes(), ruta.stat()
        args = ("medir", "--repo", REPO, "--desde", "1970-01-01T00:00:00Z",
                "--hasta", "1970-01-01T02:00:00Z")
        uno, dos = self.cli(*args), self.cli(*args)
        self.assertEqual(uno.returncode, 0, uno.stderr)
        self.assertEqual(uno.stdout, dos.stdout)
        self.assertEqual(len(uno.stdout.splitlines()), 1)
        self.assertEqual(ruta.read_bytes(), contenido)
        self.assertEqual(ruta.stat().st_mtime_ns, stat.st_mtime_ns)
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.raiz / "ausente")}):
            self.assertEqual(self.cli(*args).returncode, 2)
            self.assertFalse(directorio_estado().exists())

    def test_admision_tardia_no_usa_hora_de_entrega(self):
        self.entregar(1, 10, ci="pendiente")
        self.tick(500, [vista()])
        informe = medir(*filas_medicion(REPO), REPO, 0, 4200)
        self.assertEqual(informe["entregas"][0]["admision"]["estado"], "retenido")
        self.assertEqual(informe["entregas"][0]["admitida"], 500)
        self.assertEqual(informe["lotes"][0]["instante"], 500)
        antes = medir(*filas_medicion(REPO), REPO, 0, 500)
        self.assertEqual(antes["admitidas"], 0)


class MedicionTests(Aislado):
    def entradas(self, tiempos, solos=()):
        return tuple(Entrada(entrega(i), t, Admitido(), t, i in solos)
                     for i, t in enumerate(tiempos, 1))

    def test_lote_inmediato_y_admisiones_en_espera(self):
        entradas = tuple(replace(e, recibida=i) for i, e in enumerate(
            self.entradas([0, 10, 10, 20, 20, 20, 30])))
        lotes, pendientes = lotes_fifo(entradas, 20)
        self.assertEqual([(t, len(es)) for t, _, es in lotes], [(0, 1)])
        self.assertEqual(pendientes, entradas[1:6])
        lotes, pendientes = lotes_fifo(entradas, DURACION_SUITE_ESTIMADA)
        self.assertEqual([(t, len(es)) for t, _, es in lotes],
                         [(0, 1), (DURACION_SUITE_ESTIMADA, 5)])
        self.assertEqual(pendientes, entradas[6:])

    def test_sensible_solo_sin_adelantar_fifo(self):
        entradas = tuple(replace(e, recibida=i, admitida=10)
                         for i, e in enumerate(self.entradas([0] * 4, (2,))))
        lotes, pendientes = lotes_fifo(entradas, 10 + 2 * DURACION_SUITE_ESTIMADA)
        self.assertEqual([[e.entrega.pr for e in es] for _, _, es in lotes], [[1], [2], [3, 4]])
        self.assertEqual([t for t, _, _ in lotes], [10 + n * DURACION_SUITE_ESTIMADA
                                                  for n in range(3)])
        self.assertFalse(pendientes)

    def test_empates_deterministas_y_lote_parcial_inmediato(self):
        ultima = DURACION_SUITE_ESTIMADA + 100
        entradas = self.entradas([0, 0, 0, ultima])
        lotes, pendientes = lotes_fifo(entradas, ultima)
        self.assertEqual([len(es) for _, _, es in lotes], [3, 1])
        self.assertEqual(lotes[-1][0], ultima)
        self.assertFalse(pendientes)
        self.assertEqual((lotes, pendientes), lotes_fifo(reversed(entradas), ultima))

    def test_sombra_y_dominio_forman_los_mismos_lotes(self):
        original = estado(9)
        obs = observacion(9)
        obs = replace(obs, prs=obs.prs[:6] + (vista(7, sensible=True, autorizado=True),)
                      + obs.prs[7:])
        simulado, efectos = paso_sombra(original, obs)
        self.assertFalse(efectos)
        activo = original
        for n, prs in enumerate((range(1, 6), (6,), (7,), (8, 9))):
            tick = replace(obs, ahora=obs.ahora + n * DURACION_SUITE_ESTIMADA)
            activo, _ = paso(activo, tick)
            simulado, _ = paso_sombra(simulado, tick)
            tren = activo.trenes[-1]
            self.assertEqual(tren.recibos, tuple(entrega(pr).recibo for pr in prs))
            activo = replace(activo, trenes=activo.trenes[:-1] +
                             (replace(tren, fase=Terminal("ATERRIZADO")),))
        self.assertEqual([t.recibos for t in simulado.trenes],
                         [t.recibos for t in activo.trenes])

    def test_admisiones_escalonadas_y_caducidad_durante_suite(self):
        entradas = self.entradas([90, 210, 330, 450])
        vistas = tuple(vista(pr) for pr in range(1, 5))
        candidato = Candidato(BASE, ((1, HEAD, "c" * 40),), "c" * 40, "d" * 40)
        for caduca, esperado in ((False, [[1], [2, 3, 4]]), (True, [[1], [2, 4]])):
            with self.subTest(caduca=caduca), patch.dict(os.environ, {
                    "XDG_STATE_HOME": str(self.raiz / ("caduca-%s" % caduca))}):
                ledger = self.ledger()
                activo = simulado = Estado(REPO)
                for n, ahora in enumerate((100, 220, 340, 460)):
                    entrada = replace(entradas[n], admitida=None)
                    ledger.entregar(entrada.entrega, entrada.recibida)
                    observadas = vistas[:n + 1]
                    if n == 3 and caduca:
                        observadas = vistas[:2] + (vista(3, head="e" * 40), vistas[3])
                    obs = replace(observacion(), ahora=ahora, prs=observadas)
                    activo, _ = paso(replace(activo, entradas=activo.entradas + (entrada,)), obs)
                    simulado, _ = paso_sombra(
                        replace(simulado, entradas=simulado.entradas + (entrada,)), obs)
                    ledger.guardar_paso(activo, (), ahora)
                    self.assertEqual(len(activo.trenes), 1)
                    self.assertEqual(len(simulado.trenes), 1)
                    if n == 0:
                        activo = replace(activo, trenes=(replace(
                            activo.trenes[0], fase=Corriendo(candidato, "suite-1")),))

                activo = replace(activo, trenes=(replace(
                    activo.trenes[0], fase=Terminal("ATERRIZADO")),))
                activo, _ = paso(activo, replace(obs, ahora=580))
                ledger.guardar_paso(activo, (), 580)
                simulado, _ = paso_sombra(simulado, replace(obs, ahora=100 + DURACION_SUITE_ESTIMADA))
                prs = {e.entrega.recibo: e.entrega.pr for e in entradas}
                for resultado in (activo, simulado):
                    self.assertEqual([[prs[r] for r in t.recibos] for t in resultado.trenes],
                                     esperado)
                filas = filas_medicion(REPO)
                informe = medir(*filas, REPO, 0, 600)
                self.assertEqual([lote["prs"] for lote in informe["lotes"]], esperado)
                self.assertEqual([lote["instante"] for lote in informe["lotes"]], [100, 580])
                self.assertEqual(informe["suites_ahorradas_estimadas"], len(esperado[1]) - 1)
                antes = medir(*filas, REPO, 200, 580)
                self.assertEqual(antes["lotes"], [])
                self.assertEqual([prs[r] for r in antes["pendientes"]], esperado[1])

    def test_comparador_detecta_fase_distinta_en_lote_identico(self):
        congelado, efectos = paso(estado(), observacion())
        evento = [["sombra", 100, {"estado": datos(congelado), "efectos": datos(efectos)}]]
        comparar(deepcopy(evento), deepcopy(evento))
        mutado = deepcopy(evento)
        mutado[0][2]["estado"]["trenes"][0]["fase"]["B"] = "f" * 40
        with self.assertRaises(AssertionError):
            comparar(evento, mutado)

    def test_iso_con_zona_y_ventana_invalida(self):
        self.assertEqual(instante_iso("1970-01-01T01:00:00+01:00"), 0)
        for texto in ("ayer", "2026-01-01T00:00:00"):
            with self.assertRaises(ValueError):
                instante_iso(texto)
        for desde, hasta in ((1, 1), (2, 1), (0, float("inf"))):
            with self.assertRaises(ValueError):
                medir([], [], REPO, desde, hasta)

    def test_medir_desde_filas_lotes_y_sensible(self):
        ledger = self.ledger()
        entradas = self.entradas([0, 3600, 3600, 3600, 3600, 3600, 9000], (7,))
        for entrada in entradas:
            ledger.entregar(entrada.entrega, entrada.recibida, entrada)
        requests, events = filas_medicion(REPO)
        informe = medir(requests, events, REPO, 0, 10000)
        self.assertEqual(informe["distribucion"], {"1": 2, "2": 0, "3": 0, "4": 0, "5": 1})
        self.assertEqual(informe["lotes"][-1]["prs"], [7])
        self.assertEqual(informe["suites_ahorradas_estimadas"], 4)
        self.assertEqual(informe, medir(list(reversed(requests)), list(reversed(events)),
                                       REPO, 0, 10000))
        self.assertEqual(len(medir(requests, events, REPO, 3600, 9000)["entregas"]), 5)
