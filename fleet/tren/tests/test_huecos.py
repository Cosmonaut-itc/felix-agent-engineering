"""El CLI registra las órdenes y el tick gobierna la caducidad y el push."""
from dataclasses import replace
import json
import subprocess
import sys
import unittest

import test_activo as activo
from dominio import Certificado, Corriendo, Pausa, Promoviendo, Terminal, datos

REPO, cli = activo.REPO, activo.cli


class HuecosTests(unittest.TestCase):
    setUp = activo.ActivoTests.setUp
    comando = activo.ActivoTests.comando
    commit = activo.ActivoTests.commit
    push = activo.ActivoTests.push
    remoto = activo.ActivoTests.remoto
    ledger = activo.ActivoTests.ledger
    estado = activo.ActivoTests.estado
    hasta_fase = activo.ActivoTests.hasta_fase
    lineas = activo.ActivoTests.lineas

    def orden(self, comando, ahora, *opciones):
        args = cli.parser().parse_args([comando, "--repo", REPO, *opciones])
        codigo, cuerpo = cli.ejecutar(args, ahora=ahora)
        self.assertEqual(codigo, 0, cuerpo)
        return cuerpo

    def vencimientos(self, tipo):
        with self.ledger() as ledger:
            return [(instante, json.loads(payload)) for instante, payload in ledger.db.execute(
                "SELECT instante, payload FROM events WHERE repo=? AND tipo=?", (REPO, tipo))]

    def test_suite_en_curso_certifica_y_el_hueco_retiene_hasta_el_vencimiento(self):
        (self.raiz / "suite-75").write_text("75")
        self.hasta_fase(Corriendo)
        reanudar = self.estado().trenes[0].fase.esperar_hasta
        self.orden("hueco", reanudar - 2, "--motivo", "merge externo")
        self.contexto.tick(reanudar - 1)
        self.contexto.tick(reanudar)
        self.assertIsInstance(self.estado().trenes[0].fase, Corriendo)
        self.assertEqual(len(self.lineas("trabajos.log")), 2)
        self.contexto.tick(reanudar + 1)
        cert = self.estado().trenes[0].fase
        self.assertIsInstance(cert, Certificado)
        hasta = reanudar - 2 + 3600
        self.contexto.tick(hasta - 1)
        estado = self.orden("estado", hasta - 1, "--juicio")
        self.assertEqual(estado["estado"]["hueco"]["hasta"], hasta)
        self.assertEqual(estado["juicio"]["decisiones"][0]["estado"], "aplicada")
        self.assertEqual(self.estado().trenes[0].fase, cert)
        self.assertEqual(self.remoto(), self.B)
        self.contexto.tick(hasta)
        self.assertIsNone(self.estado().hueco)
        self.assertEqual(self.remoto(), cert.C)
        self.contexto.tick(hasta + 1)
        self.assertEqual(self.estado().trenes[0].fase, Terminal("ATERRIZADO"))
        self.assertEqual(self.vencimientos("hueco-vencido"),
                         [(hasta, {"motivo": "merge externo", "hasta": hasta})])

    def test_hueco_retiene_el_push_ya_en_outbox_y_cerrar_lo_libera(self):
        self.hasta_fase(Certificado)
        cert = self.estado().trenes[0].fase

        def detener(nombre, efecto):
            if nombre == "antes-ejecutar" and efecto.tipo == "promover":
                raise RuntimeError("interrupción")

        with self.assertRaisesRegex(RuntimeError, "interrupción"):
            self.contexto.tick(2000, detener)
        self.orden("hueco", 2001, "--motivo", "preparar pool")
        self.contexto.tick(2002)
        self.contexto.tick(2003)
        self.assertIsInstance(self.estado().trenes[0].fase, Promoviendo)
        self.assertEqual(self.remoto(), self.B)
        self.orden("hueco", 2004, "--cerrar", "--motivo", "pool listo")
        self.contexto.tick(2005)
        self.assertIsNone(self.estado().hueco)
        self.assertEqual(self.remoto(), cert.C)
        self.assertEqual(self.vencimientos("hueco-vencido"), [])
        with self.ledger() as ledger:
            self.assertEqual([(d["decision"]["decision"], d["estado"])
                              for d in ledger.decisiones(REPO)],
                             [("abrir-hueco", "aplicada"), ("cerrar-hueco", "aplicada")])

    def test_pausa_de_30_minutos_vence_con_el_motivo_original(self):
        self.hasta_fase(Certificado)
        self.orden("pausa", 2000, "--minutos", "30", "--motivo", "mantenimiento")
        self.contexto.tick(2001)
        self.contexto.tick(3799)
        self.assertEqual(self.orden("estado", 3799)["estado"]["pausa"]["hasta"], 3800)
        self.assertEqual(self.remoto(), self.B)
        self.contexto.tick(3800)
        self.assertIsNone(self.estado().pausa)
        self.assertNotEqual(self.remoto(), self.B)
        self.contexto.tick(3801)
        self.assertEqual(self.vencimientos("pausa-manual-vencida"),
                         [(3800, {"motivo": "mantenimiento", "hasta": 3800})])

    def test_snapshot_viejo_recibe_dos_horas_desde_el_primer_tick(self):
        pausa = {"_tipo": "Pausa", "motivo": "manual", "detalle": "pausa anterior"}
        foto = {**datos(self.estado()), "pausa": pausa}
        self.assertNotIn("hueco", foto)
        with self.ledger() as ledger:
            ledger.evento(REPO, "operativo", 100, {"estado": foto, "efectos": [], "consumido": None})
            identidad = ledger.pausa_id(REPO)
        self.assertIsNone(self.estado().pausa.hasta)
        self.assertIsNone(self.estado().hueco)
        self.contexto.tick(200)
        self.contexto.tick(7399)
        with self.ledger() as ledger:
            self.assertEqual(ledger.pausa_id(REPO), identidad)
        self.assertEqual(self.estado().pausa.hasta, 7400)
        self.contexto.tick(7400)
        self.assertIsNone(self.estado().pausa)
        self.assertEqual(self.vencimientos("pausa-manual-vencida"),
                         [(7400, {"motivo": "pausa anterior", "hasta": 7400})])

    def test_pausas_automaticas_no_caducan(self):
        for motivo in ("infraestructura", "necesita-juicio"):
            with self.subTest(motivo=motivo):
                pausa = Pausa(motivo, detalle="ejemplo")
                with self.ledger() as ledger:
                    ledger.guardar_paso(replace(self.estado(), pausa=pausa), (), 100)
                self.contexto.tick(10000)
                self.assertEqual(self.estado().pausa, pausa)
        self.assertEqual(self.vencimientos("pausa-manual-vencida"), [])

    def test_121_minutos_se_rechaza_en_el_cli(self):
        for comando in ("pausa", "hueco"):
            with self.subTest(comando=comando):
                proc = subprocess.run([sys.executable, "-I", str(activo.RAIZ / "pao-tren"),
                                       comando, "--repo", REPO, "--motivo", "prueba",
                                       "--minutos", "121"],
                                      capture_output=True, text=True, timeout=30)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("minutos debe estar entre 1 y 120", proc.stderr)
        with self.ledger() as ledger:
            self.assertEqual(ledger.decisiones(REPO), [])
