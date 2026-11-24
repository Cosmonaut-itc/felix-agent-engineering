"""Contrato y CLI del panel con un ledger temporal."""
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import get_args
from unittest.mock import patch

from test_tren import Aislado, BASE, HEAD, REPO, cli, entrega

from dominio import (Admitido, Candidato, Certificado, Congelado, Corriendo,
                     Diagnosticando, Efecto, Entrada, Estado, Fallo, Fase, Motivo,
                     Pausa, Rechazado, Reproduccion, Retenido, Terminal, Tren, clave,
                     datos)
from ledger import directorio_estado
import panel

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
        comienzo = time.monotonic()
        doc = self.proyectar("--repo", REPO)
        frio = time.monotonic() - comienzo
        self.assertEqual(doc["repos"][0]["lote"]["cierre"], self.ahora - 5001)
        comienzo = time.monotonic()
        self.assertEqual(doc, self.proyectar("--repo", REPO))
        caliente = time.monotonic() - comienzo
        print(f"panel: 5000 × 50 KiB en {frio:.3f} s sin cursor y {caliente:.3f} s con cursor", flush=True)
        self.assertLess(frio, 1)
        self.assertLess(caliente, 1)
