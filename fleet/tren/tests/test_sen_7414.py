"""SEN-7414: juicio, pausa y tick en seco no esperan en silencio el candado del repositorio."""
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_activo as activo
from dominio import clave
from ledger import CandadoOcupado, Ledger

REPO = activo.REPO
LINEA_FIJA = "pao-tren: tick en curso; no se registró nada"


class CandadoConPlazoTests(unittest.TestCase):
    def setUp(self):
        self.raiz = Path(tempfile.mkdtemp(prefix="pao-tren-7414-"))
        self.ledger = Ledger(self.raiz)
        self.addCleanup(self.ledger.close)

    def tomar(self):
        handle = (self.raiz / (clave("repo", REPO) + ".lock")).open("a")
        fcntl.flock(handle, fcntl.LOCK_EX)
        self.addCleanup(handle.close)
        return handle

    def test_libre_se_toma_sin_avisar(self):
        avisos = []
        with self.ledger.candado(REPO, 0, lambda: avisos.append(1)):
            pass
        self.assertEqual(avisos, [])

    def test_ocupado_avisa_una_vez_y_vence(self):
        self.tomar()
        avisos = []
        inicio = time.monotonic()
        with self.assertRaises(CandadoOcupado):
            with self.ledger.candado(REPO, 1.2, lambda: avisos.append(1)):
                self.fail("tomó un candado ocupado")
        self.assertEqual(avisos, [1])
        self.assertGreaterEqual(time.monotonic() - inicio, 1.2)

    def test_plazo_cero_no_espera(self):
        self.tomar()
        inicio = time.monotonic()
        with self.assertRaises(CandadoOcupado):
            with self.ledger.candado(REPO, 0):
                pass
        self.assertLess(time.monotonic() - inicio, 0.5)


class ComandosConPlazoTests(unittest.TestCase):
    setUp = activo.ActivoTests.setUp
    comando = activo.ActivoTests.comando
    commit = activo.ActivoTests.commit
    push = activo.ActivoTests.push
    remoto = activo.ActivoTests.remoto
    ledger = activo.ActivoTests.ledger
    estado = activo.ActivoTests.estado

    def cli(self, *argv, entorno=None):
        env = dict(os.environ, **(entorno or {}))
        return subprocess.Popen([sys.executable, "-I", str(activo.RAIZ / "pao-tren"),
                                 argv[0], "--repo", REPO, *argv[1:]],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)

    def terminar(self, proc):
        salida, errores = proc.communicate(timeout=30)
        return proc.returncode, json.loads(salida.strip().splitlines()[-1]), errores

    def ocupar(self):
        with self.ledger() as ledger:
            ruta = ledger.directorio / (clave("repo", REPO) + ".lock")
        handle = ruta.open("a")
        fcntl.flock(handle, fcntl.LOCK_EX)
        self.addCleanup(handle.close)
        return handle

    def decisiones(self):
        with self.ledger() as ledger:
            return ledger.siguiente_decision(REPO)

    def test_tick_en_seco_sale_con_75_sin_esperar(self):
        self.ocupar()
        inicio = time.monotonic()
        codigo, resultado, errores = self.terminar(self.cli("tick", "--seco"))
        self.assertEqual(codigo, 75, errores)
        self.assertEqual(resultado, {"candado": "ocupado", "registrado": False})
        self.assertIn(LINEA_FIJA, errores)
        self.assertIn("pao-tren estado --repo " + REPO, errores)
        self.assertLess(time.monotonic() - inicio, 10)

    def test_juicio_avisa_y_sale_con_75_sin_registrar(self):
        self.ocupar()
        codigo, resultado, errores = self.terminar(self.cli(
            "juicio", "--decision", "reanudar", "--motivo", "prueba de plazo",
            entorno={"PAO_TREN_PLAZO_CANDADO_S": "1"}))
        self.assertEqual(codigo, 75, errores)
        self.assertEqual(resultado, {"candado": "ocupado", "registrado": False})
        self.assertIn("pao-tren: tick en curso: espero el candado hasta 1 s", errores)
        self.assertIn(LINEA_FIJA, errores)
        self.assertIsNone(self.decisiones())

    def test_pausa_sale_con_75_sin_registrar(self):
        self.ocupar()
        codigo, resultado, errores = self.terminar(self.cli(
            "pausa", "--motivo", "prueba de plazo", entorno={"PAO_TREN_PLAZO_CANDADO_S": "0"}))
        self.assertEqual(codigo, 75, errores)
        self.assertIn(LINEA_FIJA, errores)
        self.assertIsNone(self.decisiones())
        self.assertIsNone(self.estado().pausa)

    def test_pausa_toma_el_candado_cuando_se_suelta_dentro_del_plazo(self):
        handle = self.ocupar()
        proc = self.cli("pausa", "--motivo", "prueba de plazo",
                        entorno={"PAO_TREN_PLAZO_CANDADO_S": "20"})
        time.sleep(1)
        fcntl.flock(handle, fcntl.LOCK_UN)
        codigo, resultado, errores = self.terminar(proc)
        self.assertEqual(codigo, 0, errores)
        self.assertEqual(resultado["estado"], "pendiente")
        self.assertIn("espero el candado", errores)
        self.assertNotIn(LINEA_FIJA, errores)

    def test_plazo_invalido_sale_con_2_sin_tocar_el_candado(self):
        self.ocupar()
        for valor in ("", "-1", "nan", "inf", "diez"):
            with self.subTest(valor=valor):
                codigo, resultado, errores = self.terminar(self.cli(
                    "pausa", "--motivo", "prueba de plazo",
                    entorno={"PAO_TREN_PLAZO_CANDADO_S": valor}))
                self.assertEqual(codigo, 2, errores)
                self.assertIn("PAO_TREN_PLAZO_CANDADO_S", resultado["error"])
                self.assertNotIn(LINEA_FIJA, errores)

    def test_plazo_invalido_no_afecta_al_tick_en_seco(self):
        self.ocupar()
        codigo, resultado, errores = self.terminar(self.cli(
            "tick", "--seco", entorno={"PAO_TREN_PLAZO_CANDADO_S": "diez"}))
        self.assertEqual(codigo, 75, errores)
        self.assertEqual(resultado, {"candado": "ocupado", "registrado": False})
        self.assertIn(LINEA_FIJA, errores)


if __name__ == "__main__":
    unittest.main()
