"""Contrato y CLI del panel con un ledger temporal."""
from contextlib import closing, contextmanager
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Event, Thread
import time
from typing import get_args
from unittest.mock import patch

from test_tren import Aislado, BASE, HEAD, REPO, cli, entrega, observacion, vista

from dominio import (Admitido, Candidato, Certificado, Congelado, Corriendo,
                     Decision, Diagnosticando, Efecto, Entrada, Estado, Fallo, Fase, Hueco,
                     Motivo, Observacion, Pausa, Rechazado, Reproduccion, Retenido,
                     Terminal, Tren, clave, datos, paso)
from feedback import EscritorGitHub
from ledger import directorio_estado
from observador import Falso
import panel
from sombra import paso_sombra

RACK = Path(__file__).resolve().parents[2] / "rack"
spec = importlib.util.spec_from_file_location("tren", RACK / "tren.py")
contrato = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contrato)

SOMBRA = "example/Hasito"
UNIDAD = "apps/api/foo.contract.test.ts"
CANDIDATO = Candidato(BASE, (), HEAD, HEAD)
ID = clave("lote")
URL = f"https://github.com/{REPO}/pull/1#issuecomment-123"


class PanelTests(Aislado):
    def setUp(self):
        super().setUp()
        self.env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.raiz / "config")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.registro = self.raiz / "config/pao-tren/inscritos"
        self.registro.parent.mkdir(parents=True)
        self.ahora = time.time()
        self.db = self.ledger()
        self.dir = directorio_estado()
        self.entrega = entrega()
        self.entrada = Entrada(self.entrega, self.ahora - 200, Admitido(),
                               self.ahora - 190, tren=ID)
        self.tren = Tren(ID, (self.entrega.recibo,), Congelado(BASE))
        self.estado = Estado(REPO, (self.entrada,), (self.tren,))
        self.fallo = Fallo(UNIDAD, "/evidencia/firma", "solo-candidato", "contract", parte="jale")

    def paso(self, segundos, fase=None, efectos=(), consumido=None, estado=None):
        estado = self.estado if estado is None else estado
        if fase is not None:
            estado = replace(estado, trenes=(replace(self.tren, fase=fase),))
        self.db.guardar_paso(estado, efectos, self.ahora + segundos, consumido)

    def registro_jale(self, raiz, codigo=None, log="", inicio=True):
        raiz.mkdir(parents=True)
        if inicio:
            (raiz / "iniciado.json").write_text("{}")
        if codigo is not None:
            (raiz / "codigo.json").write_text(json.dumps(codigo))
        (raiz / "corrida.log").write_text(log)

    def validar(self, doc):
        self.assertEqual(doc, contrato.valid_tren(doc, (REPO, SOMBRA), self.ahora))

        def sin_rutas(valor, campo=""):
            if isinstance(valor, dict):
                for llave, dato in valor.items():
                    self.assertNotIn("/", llave)
                    sin_rutas(dato, llave)
            elif isinstance(valor, list):
                for dato in valor:
                    sin_rutas(dato, campo)
            elif isinstance(valor, str) and campo not in ("repo", "unidad", "comentario_url"):
                self.assertNotIn("/", valor)
                self.assertNotIn("\\", valor)
        sin_rutas(doc)
        self.assertNotIn("/evidencia/", json.dumps(doc))
        return doc

    def proyectar(self, *args):
        codigo, doc = cli.ejecutar(cli.parser().parse_args(["panel", *args]), ahora=self.ahora)
        self.assertEqual(codigo, 0)
        return self.validar(doc)

    def test_lote_verde_con_hitos_u1_y_lugar(self):
        self.paso(-160)
        suite = Efecto(clave("suite", ID), "iniciar-suite", (ID,))
        self.paso(-130, Corriendo(CANDIDATO, "/evidencia/suite"), (suite,))
        self.db.iniciar_efecto(REPO, suite, self.ahora - 120)
        self.paso(-90, Certificado(HEAD, HEAD, "/evidencia/suite", "verde"), consumido=suite.clave)
        raiz = self.dir / "jale" / clave("suite", ID)
        self.registro_jale(raiz / "contract/0", 0, inicio=False, log=(
            "HOST=desktop (auto: /evidencia/motivo)\nPUEUE_ID=7812\nPUEUE_REF=job-example\n"
            "RUTA=/evidencia/log\n"))
        (raiz / "contract/completo.json").write_text("0")
        (raiz / "suite.json").write_text(json.dumps([
            {"modo": "contract", "unidades": [{"resultado": "pasa", "firma": "/evidencia/secreta"}]}]))

        lote = self.proyectar("--repo", REPO)["repos"][0]["lote"]
        self.assertEqual((lote["fase"], lote["cierre"], lote["fase_desde"]),
                         ("aterrizando", self.ahora - 160, self.ahora - 90))
        self.assertEqual(lote["miembros"], [{"pr": 1, "head": HEAD[:7]}])
        corrida, = lote["suite"]
        self.assertEqual((corrida["estado"], corrida["resultado"], corrida["fallas"], corrida["inicio"]),
                         ("terminada", "verde", 0, self.ahora - 120))
        self.assertEqual(corrida["lugar"], dict(tipo="desk", vm=None, pueue_id=7812, ref="job-example"))

    def test_atribucion_roja_y_pausa_sin_detalle_privado(self):
        pendiente = Reproduccion(clave("repro"), self.fallo, "C")
        estado = replace(self.estado, pausa=Pausa("infraestructura", 1,
                         detalle="/evidencia/detalle", archivos=("/evidencia/conflicto",)))
        self.paso(-150)
        diagnostico = Diagnosticando(CANDIDATO, (self.fallo,), "/evidencia/suite", pendiente=pendiente)
        self.paso(-120, diagnostico, estado=estado)
        self.paso(-60, diagnostico, estado=estado)
        raiz = self.dir / "jale"
        suite = raiz / clave("suite", ID)
        self.registro_jale(suite / "contract/0", 1)
        (suite / "contract/completo.json").write_text("0")
        (suite / "suite.json").write_text(json.dumps([{"modo": "contract", "unidades": [
            {"resultado": "falla"}, {"resultado": "no-corrida"}]}]))
        self.registro_jale(raiz / clave("repetir", ID, pendiente.id) / "1/0",
                           log="HOST=mini\nPUEUE_ID=33\nPUEUE_REF=/evidencia/rechazar\n")

        repo = self.proyectar("--repo", REPO)["repos"][0]
        self.assertEqual(repo["pausa"], dict(motivo="infraestructura", pr=1, desde=self.ahora - 120))
        lote = repo["lote"]
        self.assertEqual((lote["fase"], lote["fase_desde"]), ("atribucion", self.ahora - 120))
        corrida, = lote["suite"]
        self.assertEqual((corrida["resultado"], corrida["fallas"]), ("rojo", 2))
        ensayo = lote["ensayo"]
        self.assertEqual((ensayo["unidad"], ensayo["prs"], ensayo["hechos"]), (UNIDAD, [1], 0))
        self.assertEqual(ensayo["lugar"], dict(tipo="mini", vm=None, pueue_id=33, ref=None))

    def test_publica_los_vencimientos_sin_motivos_privados(self):
        pausa = Pausa("manual", detalle="/evidencia/pausa")
        self.paso(-10, estado=replace(self.estado, pausa=pausa))
        hasta_pausa = self.ahora + 7195
        hasta_hueco = self.ahora + 3600
        self.paso(-5, estado=replace(self.estado, pausa=replace(pausa, hasta=hasta_pausa),
                                    hueco=Hueco(hasta_hueco, "/evidencia/hueco")))
        repo = self.proyectar("--repo", REPO)["repos"][0]
        self.assertEqual(repo["pausa"], dict(motivo="manual", pr=None, desde=self.ahora - 10,
                                            hasta=hasta_pausa))
        self.assertEqual(repo["hueco"], dict(hasta=hasta_hueco))

    def test_devuelto_relee_feedback_y_conserva_el_pr_de_su_lote(self):
        self.paso(-170)
        payload = json.dumps(dict(fallos=[
            dict(check="contract", unidad="/evidencia/privada"),
            dict(check="contract", unidad=UNIDAD, comando="/evidencia/comando", firma=HEAD)]))
        efecto = Efecto(clave("devolver", ID, self.entrega.recibo), "devolver",
                        (ID, self.entrega.recibo, payload))
        fin = Terminal("DEVUELTO")
        estado = replace(self.estado, entradas=(replace(self.entrada, terminal=fin),))
        self.paso(-130, fin, (efecto,), estado=estado)
        reciente, = self.proyectar("--repo", REPO)["repos"][0]["recientes"]
        self.assertIsNone(reciente["miembros"][0]["devolucion"])

        self.db.registrar_efecto(REPO, efecto, self.ahora - 100,
                                {"comentario_url": URL, "detalle": "/evidencia/privada"},
                                completo=True, sin_consumo=True)
        repo = self.proyectar("--repo", REPO)["repos"][0]
        self.assertIsNone(repo["lote"])
        reciente, = repo["recientes"]
        self.assertEqual((reciente["resultado"], reciente["cierre"], reciente["fin"]),
                         ("DEVUELTO", self.ahora - 170, self.ahora - 130))
        self.assertEqual(reciente["miembros"], [dict(pr=1, resultado="DEVUELTO",
            devolucion=dict(check="contract", unidad=UNIDAD, comentario_url=URL))])

    def test_todos_incluye_sombra_y_etapas_de_espera(self):
        self.registro.write_text(f"{REPO} activo # comentario\n{SOMBRA} sombra\n")
        entrada = replace(self.entrada, entrega=replace(self.entrega, repo=SOMBRA),
                          terminal=Terminal("MERGEADO-FUERA"))
        sombra = Estado(SOMBRA, (entrada,),
                        (Tren(ID, (entrada.entrega.recibo,), Terminal("SIMULADO")),))
        self.db.sombra(sombra, (), self.ahora - 200, doble_camino=True)
        espera = tuple(replace(self.entrada, entrega=entrega(pr), tren=None, admision=admision)
                       for pr, admision in enumerate((Retenido(Motivo.CUSTODIA), Admitido(),
                           Retenido(Motivo.CI), Rechazado(Motivo.HEAD)), 2))
        self.paso(-80, estado=Estado(REPO, espera))
        activo, simulado = self.proyectar("--todos")["repos"]
        self.assertEqual([p["etapa"] for p in activo["espera"]],
                         ["entregado", "admitido", "retenido", "caducado"])
        self.assertEqual(simulado["modo"], "sombra")
        self.assertIsNone(simulado["lote"])
        reciente, = simulado["recientes"]
        self.assertEqual(reciente["resultado"], "SIMULADO")
        self.assertEqual(reciente["miembros"][0]["resultado"], "MERGEADO-FUERA")

    def test_fin_nuevo_renueva_taken_con_cursor_y_sin_cursor(self):
        self.registro.write_text(f"{SOMBRA} sombra\n")
        with patch.object(self, "ahora", self.ahora - 361):
            anterior = self.proyectar("--todos")
        entrega_sombra = replace(self.entrega, repo=SOMBRA)
        self.db.entregar(entrega_sombra, self.ahora - 21)
        observacion = Observacion(self.ahora - 1, BASE, (vista(),))
        estado, efectos = paso_sombra(self.db.estado(SOMBRA, sombra=True, doble_camino=True),
                                      observacion)
        self.db.sombra(estado, efectos, observacion.ahora, doble_camino=True)

        caliente = self.proyectar("--todos")
        cursor = self.dir / "panel-cursor.json"
        cursor.rename(cursor.with_suffix(".anterior"))
        frio = self.proyectar("--todos")
        self.assertEqual(caliente, frio)
        self.assertGreater(caliente["taken"], anterior["taken"])
        lote, = caliente["repos"][0]["recientes"]
        self.assertEqual((lote["resultado"], lote["fin"]), ("SIMULADO", observacion.ahora))

    def expulsar_del_lote(self, motivo, *, caducar=False):
        entradas = (self.entrada, *(replace(self.entrada, entrega=entrega(pr)) for pr in (2, 3)))
        tren = replace(self.tren, recibos=tuple(e.entrega.recibo for e in entradas))
        estado = Estado(REPO, entradas, (tren,), Pausa(motivo, pr=1))
        self.paso(-150, estado=estado)
        if caducar:
            vistas = (vista(1), vista(2, head="c" * 40), vista(3))
            estado, efectos = paso(estado, Observacion(self.ahora - 145, BASE, vistas))
            self.paso(-145, efectos=efectos, estado=estado)
        pausa_id = self.db.pausa_id(REPO, estado)
        decision = Decision(clave("juicio"), REPO, ID, "expulsar", 1, "motivo del operador",
                            self.ahora - 130, pausa_id, estado.pausa)
        self.db.registrar_decision(decision)
        nuevo, efectos = paso(estado, Observacion(self.ahora - 120, BASE, (), decision=decision,
                                                  pausa_id=pausa_id))
        self.paso(-120, efectos=efectos, estado=nuevo)
        devolver, = (efecto for efecto in efectos if efecto.tipo == "devolver")
        return nuevo, devolver

    def test_expulsion_conserva_url_original_con_otra_grafia(self):
        nuevo, devolver = self.expulsar_del_lote("conflicto-en-cadena")
        self.assertEqual(json.loads(devolver.datos[2])["fallos"], [])
        url = URL.replace(REPO, "Example/Project")
        escritor = EscritorGitHub()
        with patch.object(escritor, "api", side_effect=[[], {"html_url": url}, [], {}]):
            resultado = escritor.ejecutar_feedback(devolver, nuevo, lambda *_: None)
        self.db.registrar_efecto(REPO, devolver, self.ahora - 110, resultado,
                                completo=True, sin_consumo=True)
        reciente, = self.proyectar("--repo", REPO)["repos"][0]["recientes"]
        self.assertEqual(reciente["miembros"][0], dict(pr=1, resultado="DEVUELTO",
            devolucion=dict(check="devolucion", unidad="Devolución", comentario_url=url)))

    def test_url_de_devolucion_solo_ignora_mayusculas_ascii_del_repo(self):
        url = "https://github.com/example/kit/pull/1#issuecomment-123"
        for anterior, otro in (("kit", "Kit"), ("kit/", "kit2/"), ("github.com", "GitHub.com"),
                               ("/pull/", "/Pull/"), ("/1#", "/01#"),
                               ("#issuecomment-", "#Issuecomment-")):
            with self.subTest(otro=otro):
                self.assertIsNone(panel.devolucion("example/kit", 1, (
                    {}, {"comentario_url": url.replace(anterior, otro)})))

    def test_lote_devuelto_distingue_caducado_de_rearme(self):
        nuevo, _ = self.expulsar_del_lote("head-cambiado-en-tren", caducar=True)
        self.assertEqual(nuevo.entradas[1].admision, Rechazado(Motivo.HEAD))
        reciente, = self.proyectar("--repo", REPO)["repos"][0]["recientes"]
        self.assertEqual({m["pr"]: m["resultado"] for m in reciente["miembros"]},
                         {1: "DEVUELTO", 2: "CADUCADO", 3: "REARMAR"})

    def test_todos_y_repo_publican_la_grafia_del_ledger(self):
        self.registro.write_text(f"{REPO.upper()} activo\n")
        self.db.entregar(self.entrega, self.ahora - 200)
        self.paso(-20, estado=Estado(REPO, (replace(self.entrada, tren=None),)))
        una, = self.proyectar("--repo", REPO)["repos"]
        codigo, doc = cli.ejecutar(cli.parser().parse_args(["panel", "--todos"]), ahora=self.ahora)
        self.assertEqual(codigo, 0)
        self.assertEqual(doc["repos"], [una])
        self.assertEqual(self.proyectar("--repo", REPO.upper())["repos"], [una])
        self.validar(doc)
        self.assertEqual(una["repo"], REPO)
        self.assertEqual([fila["pr"] for fila in una["espera"]], [1])

    def entregar_por_cli(self, repo, pr, segundos, observador=None):
        args = cli.parser().parse_args([
            "entregar", "--repo", repo, "--pr", str(pr), "--head", HEAD,
            "--apto", "apto.json", "--dirigida", "dirigida.json"])
        codigo, resultado = cli.ejecutar(args, observador, self.ahora + segundos)
        self.assertEqual(codigo, 0, resultado)

    def comprobar_pr_terminado(self, pr=1, configurado=REPO):
        for args in (("--todos",), ("--repo", REPO), ("--repo", REPO.upper())):
            with self.subTest(args=args):
                codigo, doc = cli.ejecutar(cli.parser().parse_args(["panel", *args]), ahora=self.ahora)
                self.assertEqual(codigo, 0)
                fila, = doc["repos"]
                self.assertEqual(fila["repo"], REPO)
                self.assertEqual([m["pr"] for lote in fila["recientes"] for m in lote["miembros"]], [pr])
                self.assertEqual(fila["espera"], [])
                self.assertEqual(doc["errors"], [])
                self.validar(doc)
                self.assertEqual(contrato.valid_tren(doc, (configurado,), self.ahora),
                                 dict(doc, repos=[dict(fila, repo=configurado)]))

    def test_grafia_del_tick_sombra_prevalece_sobre_otra_entrega(self):
        self.registro.write_text(f"{REPO.upper()} sombra\n")
        self.entregar_por_cli(REPO, 1, -60, Falso(observacion()))
        tick = cli.parser().parse_args(["tick", "--repo", REPO, "--seco"])
        codigo, resultado = cli.ejecutar(tick, Falso(observacion()), self.ahora - 30)
        self.assertEqual(codigo, 0, resultado)
        self.entregar_por_cli(REPO.upper(), 2, -10, Falso(replace(observacion(), prs=(vista(2),))))
        self.db.guardar_paso(Estado(REPO.upper()), (), self.ahora - 5)
        self.comprobar_pr_terminado()

    def test_espera_proyecta_admision_durable_antes_del_tick(self):
        self.registro.write_text(f"{REPO} sombra\n")
        tick = cli.parser().parse_args(["tick", "--repo", REPO, "--seco"])
        obs_vacia = Falso(observacion(n=0, ahora=self.ahora - 90))
        codigo, resultado = cli.ejecutar(tick, obs_vacia, self.ahora - 90)
        self.assertEqual(codigo, 0, resultado)
        for pr, segundos, ci in ((1, -40, "verde"), (2, -30, "pendiente")):
            obs = replace(observacion(ahora=self.ahora + segundos), prs=(vista(pr, ci=ci),))
            self.entregar_por_cli(REPO, pr, segundos, Falso(obs))

        def espera():
            filas = self.proyectar("--todos")["repos"][0]["espera"]
            return [(fila["pr"], fila["etapa"], fila["motivo"]) for fila in filas]

        admitidas = [(1, "admitido", None), (2, "retenido", "ci-no-verde")]
        self.assertEqual(espera(), admitidas)
        self.db.entregar(entrega(3), self.ahora - 20)
        self.assertEqual(espera(), admitidas + [(3, "entregado", None)])
        revisada = Entrada(entrega(1), self.ahora - 40, Rechazado(Motivo.HEAD))
        self.db.sombra(Estado(REPO, (revisada,)), (), self.ahora - 10, doble_camino=True)
        self.assertEqual(espera(), [(1, "caducado", "head-cambiado"), admitidas[1], (3, "entregado", None)])

    def test_grafia_del_tick_activo_prevalece_sobre_otra_entrega(self):
        self.registro.write_text(f"{REPO.upper()} activo\n")
        self.entregar_por_cli(REPO, 1, -60)
        self.paso(-30, Terminal("ATERRIZADO"))
        self.entregar_por_cli(REPO.upper(), 2, -10)
        self.db.sombra(Estado(REPO.upper()), (), self.ahora - 5, doble_camino=True)
        self.comprobar_pr_terminado()

    def test_grafia_del_ultimo_tick_sombra_sigue_la_secuencia(self):
        self.registro.write_text(f"{REPO} sombra\n")
        for repo, pr, instante in ((REPO.upper(), 1, -15), (REPO, 2, -60)):
            falso = Falso(replace(observacion(), prs=(vista(pr),)))
            self.entregar_por_cli(repo, pr, instante - 5, falso)
            tick = cli.parser().parse_args(["tick", "--repo", repo, "--seco"])
            codigo, resultado = cli.ejecutar(tick, falso, self.ahora + instante)
            self.assertEqual(codigo, 0, resultado)
        self.comprobar_pr_terminado(2, configurado=REPO.upper())

    def test_grafia_del_ultimo_tick_activo_sigue_la_secuencia(self):
        self.registro.write_text(f"{REPO} activo\n")
        for repo, pr, instante in ((REPO.upper(), 1, -15), (REPO, 2, -60)):
            entrada = replace(self.entrada, entrega=replace(entrega(pr), repo=repo))
            tren = replace(self.tren, recibos=(entrada.entrega.recibo,), fase=Terminal("ATERRIZADO"))
            self.paso(instante, estado=Estado(repo, (entrada,), (tren,)))
        self.comprobar_pr_terminado(2, configurado=REPO.upper())

    def test_error_de_ledger_sustituido_y_bloqueado_usa_grafia_del_registro(self):
        self.registro.write_text(f"{REPO.upper()} activo\n")
        self.entregar_por_cli(REPO.upper(), 1, -65)
        panel.publicar(None, self.ahora - 55)
        cursor = self.dir / "panel-cursor.json"
        anterior = cursor.read_bytes()
        ledger = self.dir / "ledger.sqlite3"
        inodo = ledger.stat().st_ino
        ledger.rename(ledger.with_suffix(".anterior"))
        self.registro.write_text(f"{REPO} activo\n")
        with closing(self.ledger()) as nuevo:
            self.entregar_por_cli(REPO, 2, -20)
            self.assertNotEqual(ledger.stat().st_ino, inodo)
            nuevo.db.execute("BEGIN EXCLUSIVE")
            try:
                with patch.object(panel, "TOPE_BLOQUEO", 0.05):
                    doc = panel.publicar(None, self.ahora)
            finally:
                nuevo.db.execute("ROLLBACK")
        self.assertEqual([e["stage"] for e in doc["errors"]], ["ledger"])
        self.assertEqual([fila["repo"] for fila in doc["repos"]], [REPO])
        self.assertEqual(cursor.read_bytes(), anterior)
        self.validar(doc)

    def test_ledger_sustituido_y_legible_reconstruye_hitos_con_la_misma_secuencia(self):
        self.paso(-160)
        original = self.proyectar("--repo", REPO)["repos"][0]["lote"]
        cursor = self.dir / "panel-cursor.json"
        _, anterior = json.loads(cursor.read_bytes())
        ledger = self.dir / "ledger.sqlite3"
        ledger.rename(ledger.with_suffix(".anterior"))
        with closing(self.ledger()) as nuevo:
            nuevo.guardar_paso(self.estado, (), self.ahora - 90)

        actual = self.proyectar("--repo", REPO)["repos"][0]["lote"]
        _, reemplazo = json.loads(cursor.read_bytes())
        llave = REPO + ":activo"
        self.assertEqual(anterior["repos"][llave]["seq"], reemplazo["repos"][llave]["seq"])
        self.assertNotEqual(anterior["ledger"], reemplazo["ledger"])
        self.assertEqual(original["id"], actual["id"])
        self.assertEqual((original["cierre"], actual["cierre"]), (self.ahora - 160, self.ahora - 90))

    def test_error_del_mismo_ledger_usa_grafia_de_la_seleccion(self):
        self.registro.write_text(f"{REPO.upper()} activo\n")
        self.entregar_por_cli(REPO.upper(), 2, -70)
        self.entregar_por_cli(REPO, 1, -65)
        inicial = panel.publicar(None, self.ahora - 60)
        self.assertEqual(inicial, contrato.valid_tren(inicial, (REPO.upper(),), self.ahora))
        self.assertEqual([fila["repo"] for fila in inicial["repos"]], [REPO.upper()])
        self.paso(-30)
        self.proyectar("--todos")
        cursor = self.dir / "panel-cursor.json"
        anterior = cursor.read_bytes()
        with patch.object(panel, "lectura_medicion", side_effect=sqlite3.OperationalError("database is locked")):
            for args, grafia in ((("--todos",), REPO.upper()), (("--repo", REPO), REPO),
                                 (("--repo", REPO.upper()), REPO.upper())):
                with self.subTest(args=args):
                    codigo, doc = cli.ejecutar(cli.parser().parse_args(["panel", *args]), ahora=self.ahora)
                    self.assertEqual(codigo, 0)
                    self.assertEqual([e["stage"] for e in doc["errors"]], ["ledger"])
                    self.assertEqual([fila["repo"] for fila in doc["repos"]], [grafia])
                    limpio = contrato.valid_tren(doc, (REPO,), self.ahora)
                    self.assertEqual(limpio["repos"][0]["repo"], REPO)
        self.assertEqual(cursor.read_bytes(), anterior)

    def test_error_de_ledger_sin_cursor_usa_grafia_de_la_seleccion(self):
        self.registro.write_text(f"{REPO.upper()} activo\n")
        with patch.object(panel, "lectura_medicion", side_effect=sqlite3.OperationalError("sin tabla")):
            for args, grafia in ((("--todos",), REPO.upper()), (("--repo", REPO), REPO),
                                 (("--repo", SOMBRA), SOMBRA)):
                with self.subTest(args=args):
                    codigo, doc = cli.ejecutar(cli.parser().parse_args(["panel", *args]), ahora=self.ahora)
                    self.assertEqual(codigo, 0)
                    self.assertEqual([e["stage"] for e in doc["errors"]], ["ledger"])
                    self.assertEqual([fila["repo"] for fila in doc["repos"]], [grafia])
                    limpio = contrato.valid_tren(doc, (REPO.upper(), SOMBRA.upper()), self.ahora)
                    self.assertEqual(limpio["repos"][0]["repo"], grafia.upper())

    def test_error_de_ledger_ausente_no_lee_cursor_anterior(self):
        self.registro.write_text(f"{REPO.upper()} activo\n")
        self.entregar_por_cli(REPO, 1, -60)
        self.proyectar("--todos")
        ledger = self.dir / "ledger.sqlite3"
        ledger.rename(ledger.with_suffix(".anterior"))
        with patch.object(panel, "leer_cursor", wraps=panel.leer_cursor) as leer:
            doc = panel.publicar(REPO.upper(), self.ahora)
        leer.assert_not_called()
        self.assertEqual([e["stage"] for e in doc["errors"]], ["ledger"])
        self.assertEqual([fila["repo"] for fila in doc["repos"]], [REPO.upper()])
        limpio = contrato.valid_tren(doc, (REPO,), self.ahora)
        self.assertEqual(limpio["repos"][0]["repo"], REPO)

    def test_espera_publica_recibida_en_todas_las_etapas(self):
        admisiones = (Retenido(Motivo.CUSTODIA), Admitido(), Retenido(Motivo.CI), Rechazado(Motivo.HEAD))
        entradas = tuple(replace(self.entrada, entrega=entrega(pr), tren=None, admision=admision,
                                 recibida=self.ahora - 200 - pr, caducada=self.ahora - 60)
                         for pr, admision in enumerate(admisiones, 1))
        self.paso(-20, estado=Estado(REPO, entradas))
        espera = self.proyectar("--repo", REPO)["repos"][0]["espera"]
        self.assertEqual([(fila["etapa"], fila["desde"]) for fila in espera], list(zip(
            ("entregado", "admitido", "retenido", "caducado"), (e.recibida for e in entradas))))

    def test_ledger_ocupado_devuelve_error_en_tiempo_acotado(self):
        cursor = self.dir / "panel-cursor.json"
        self.proyectar("--repo", REPO)
        anterior = cursor.read_bytes()
        self.db.db.execute("BEGIN EXCLUSIVE")
        try:
            inicio = time.monotonic()
            doc = self.proyectar("--repo", REPO)
            duracion = time.monotonic() - inicio
        finally:
            self.db.db.execute("ROLLBACK")
        self.assertEqual(doc["errors"], [dict(stage="ledger", message="Lectura fallida: OperationalError")])
        self.assertEqual(cursor.read_bytes(), anterior)
        # La prueba determinista verifica los 2 s. Aquí damos hasta 6 s extra
        # al planificador y al resto de la proyección bajo carga del host.
        print(f"panel: bloqueo exclusivo en {duracion:.3f} s", flush=True)
        self.assertLess(duracion, 8)

    def test_presupuesto_de_bloqueo_descuenta_el_tiempo_del_primer_intento_y_la_pausa(self):
        transcurrido = 0.0
        esperas = []

        def pausa(segundos):
            nonlocal transcurrido
            transcurrido += segundos + 0.375  # Simula un despertar tardío del proceso.

        def lectura_bloqueada(raiz, *, timeout=5.0):
            nonlocal transcurrido
            esperas.append(timeout)
            transcurrido += timeout
            raise sqlite3.OperationalError("database is locked")

        with patch.object(panel.time, "monotonic", side_effect=lambda: transcurrido), \
             patch.object(panel.time, "sleep", side_effect=pausa), \
             patch.object(panel, "lectura_medicion", side_effect=lectura_bloqueada):
            doc = self.proyectar("--repo", REPO)
        self.assertEqual([error["stage"] for error in doc["errors"]], ["ledger"])
        self.assertEqual(esperas, [0.1, 1.5])
        self.assertAlmostEqual(transcurrido, 2.0)

    def test_ledger_reintenta_solo_los_bloqueos(self):
        self.db.entregar(self.entrega, self.ahora - 200)
        lectura = panel.lectura_medicion
        for mensaje, intentos in (("database is locked", 2), ("no such table: events", 1)):
            with self.subTest(mensaje=mensaje):
                with patch.object(panel, "lectura_medicion",
                                  side_effect=[sqlite3.OperationalError(mensaje),
                                               lectura(self.dir)]) as leer:
                    doc = self.proyectar("--repo", REPO)
                self.assertEqual(leer.call_count, intentos)
                if intentos == 2:
                    self.assertEqual(doc["errors"], [])
                    self.assertEqual([fila["pr"] for fila in doc["repos"][0]["espera"]], [1])
                else:
                    self.assertEqual(doc["errors"], [
                        dict(stage="ledger", message="Lectura fallida: OperationalError")])

    def test_panel_y_escritor_concurrente_en_ledger_temporal(self):
        self.db.entregar(self.entrega, self.ahora - 200)
        ruta = self.dir / "ledger.sqlite3"
        listo, leyendo, siguiente = Event(), Event(), Event()
        bloqueado, intento = Event(), Event()
        confirmadas, errores = [], []

        def escribir():
            try:
                with closing(sqlite3.connect(str(ruta), isolation_level=None, timeout=2)) as db:
                    for i in range(12):
                        db.execute("BEGIN EXCLUSIVE" if i == 1 else "BEGIN IMMEDIATE")
                        db.execute("INSERT INTO events(repo,tipo,instante,payload) VALUES (?,?,?,?)",
                                   (REPO, "control-panel", self.ahora, "{}"))
                        if i == 0:
                            listo.set()
                            if not leyendo.wait(5):
                                raise RuntimeError("el panel no abrió una instantánea")
                            siguiente.set()
                        if i == 1:
                            bloqueado.set()
                            if not intento.wait(5):
                                raise RuntimeError("el panel no intentó leer durante el bloqueo")
                            time.sleep(0.25)
                        db.execute("COMMIT")
                        confirmadas.append(i)
            except Exception as exc:
                errores.append(exc)
            finally:
                siguiente.set()

        lectura = panel.lectura_medicion

        @contextmanager
        def lectura_solapada(*args, **kwargs):
            if bloqueado.is_set():
                intento.set()
            with lectura(*args, **kwargs) as db:
                leyendo.set()
                self.assertTrue(siguiente.wait(5))
                yield db

        escritor = Thread(target=escribir, daemon=True)
        escritor.start()
        try:
            self.assertTrue(listo.wait(5))
            with patch.object(panel, "lectura_medicion", lectura_solapada):
                for i in range(12):
                    if i == 1:
                        self.assertTrue(bloqueado.wait(5))
                    doc = self.proyectar("--repo", REPO)
                    self.assertEqual(doc["errors"], [])
                    self.assertEqual([fila["pr"] for fila in doc["repos"][0]["espera"]], [1])
        finally:
            leyendo.set()
            intento.set()
            escritor.join(5)
        self.assertFalse(escritor.is_alive())
        self.assertEqual(errores, [])
        self.assertEqual(confirmadas, list(range(12)))

    def test_fase_futura_y_tabla_exhaustiva(self):
        for fase in get_args(Fase):
            self.assertIn(fase.__name__, panel.ETAPAS,
                          f"añade {fase.__name__} a ETAPAS en fleet/tren/panel.py")
        estado = datos(self.estado)
        estado["trenes"][0]["fase"] = {"_tipo": "FaseNueva", "detalle": "/evidencia/detalle"}
        self.db.evento(REPO, "operativo", self.ahora - 160, {"estado": estado})
        lote = self.proyectar("--repo", REPO)["repos"][0]["lote"]
        self.assertEqual(lote["fase"], "otra")

    def test_cli_solo_lectura_y_cursor_recuperable(self):
        self.paso(-140)
        ruta = self.dir / "ledger.sqlite3"
        ruta.chmod(0o444)
        antes = ruta.stat().st_mtime_ns
        # El lector reutilizado debe abrir exactamente con mode=ro, también
        # en un host de pruebas cuyo usuario pueda saltarse el permiso del archivo.
        with patch("ledger.sqlite3.connect", wraps=sqlite3.connect) as conectar:
            self.proyectar("--repo", REPO)
        uri, = conectar.call_args.args
        self.assertTrue(uri.endswith("?mode=ro"))
        self.assertTrue(conectar.call_args.kwargs["uri"])
        primero = self.cli("panel", "--repo", REPO)
        self.assertEqual(primero.returncode, 0, primero.stderr)
        doc = self.validar(json.loads(primero.stdout))
        segundo = self.cli("panel", "--repo", REPO)
        self.assertEqual(segundo.returncode, 0, segundo.stderr)
        self.assertEqual(primero.stdout, segundo.stdout)
        self.validar(json.loads(segundo.stdout))
        cursor = self.dir / "panel-cursor.json"
        _, contenido = json.loads(cursor.read_text())
        contenido["grafias"] = {REPO: REPO.upper()}
        cursor.write_text(json.dumps([clave(contenido), contenido]))
        compatible = self.cli("panel", "--repo", REPO)
        self.assertEqual(compatible.returncode, 0, compatible.stderr)
        self.assertEqual(primero.stdout, compatible.stdout)
        self.assertNotIn("grafias", json.loads(cursor.read_text())[1])
        cursor.write_text("{corrupto")
        reconstruido = self.cli("panel", "--repo", REPO)
        self.assertEqual(reconstruido.returncode, 0, reconstruido.stderr)
        self.assertEqual(doc["repos"], self.validar(json.loads(reconstruido.stdout))["repos"])
        self.assertEqual(ruta.stat().st_mtime_ns, antes)
        self.assertEqual(sorted(p.name for p in self.dir.glob("ledger.sqlite3*")), ["ledger.sqlite3"])

    def test_presupuesto_5000_eventos_de_50_kib(self):
        estado = datos(self.estado)
        relleno = "x" * (50 * 1024 - len(json.dumps({"estado": estado, "relleno": ""})))
        evento = json.dumps({"estado": estado, "relleno": relleno})
        self.assertEqual(len(evento), 50 * 1024)
        with self.db.transaccion():
            self.db.db.executemany("INSERT INTO events(repo, tipo, instante, payload) VALUES (?, ?, ?, ?)",
                ((REPO, "operativo", self.ahora - 5001 + n, evento[:-6] + f'{n:04d}"}}')
                 for n in range(5000)))
        lectura = """
import json
import sys
import time

sys.path.insert(0, sys.argv[1])
from panel import publicar

inicio_cpu = time.process_time()
inicio_pared = time.monotonic()
doc = publicar(sys.argv[2], float(sys.argv[3]))
print(json.dumps([time.process_time() - inicio_cpu, time.monotonic() - inicio_pared, doc]))
"""

        def medir():
            # El proceso nuevo evita reutilizar cachés de Python o SQLite. El
            # tope de pared detecta cuelgues y deja margen al planificador.
            salida = subprocess.run(
                [sys.executable, "-I", "-B", "-c", lectura, str(Path(panel.__file__).parent),
                 REPO, str(self.ahora)], capture_output=True, text=True, timeout=60)
            self.assertEqual(salida.returncode, 0, salida.stderr)
            cpu, pared, doc = json.loads(salida.stdout)
            return cpu, pared, self.validar(doc)

        cursor = self.dir / "panel-cursor.json"
        frias = []
        for _ in range(3):
            cursor.unlink(missing_ok=True)
            cpu, pared, doc = medir()
            frias.append((cpu, pared))
            self.assertEqual(doc["repos"][0]["lote"]["cierre"], self.ahora - 5001)
        cpu_caliente, pared_caliente, caliente = medir()
        self.assertEqual(doc, caliente)
        muestras = ", ".join(f"{cpu:.3f} s CPU / {pared:.3f} s pared" for cpu, pared in frias)
        print(f"panel: 5000 × 50 KiB sin cursor: {muestras}; "
              f"con cursor: {cpu_caliente:.3f} s CPU / {pared_caliente:.3f} s pared", flush=True)
        self.assertLess(min(cpu for cpu, _ in frias), 1)
        self.assertLess(cpu_caliente, 1)
