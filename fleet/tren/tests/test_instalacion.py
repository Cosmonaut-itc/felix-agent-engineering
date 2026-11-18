"""U7: instalación aislada, sin red ni launchctl real. Fixtures conservadas."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

RAIZ = Path(__file__).resolve().parents[1]
PROYECTO = RAIZ.parents[1]
spec = importlib.util.spec_from_file_location("instalar_tren_u7", RAIZ / "instalar.py")
instalador = importlib.util.module_from_spec(spec)
spec.loader.exec_module(instalador)
REPO = "example/project"


class InstalacionTests(unittest.TestCase):
    def setUp(self):
        self.raiz = Path(tempfile.mkdtemp(prefix="pao-tren-u7-"))
        self.home = self.raiz / "home con espacios & acentos á"
        self.bin = self.home / ".local/bin"
        self.bin.mkdir(parents=True)
        self.cli = self.bin / "pao-tren"
        self.cli.symlink_to(RAIZ / "pao-tren")
        self.falsos = self.raiz / "herramientas con espacios"
        self.falsos.mkdir()
        self.calls = self.raiz / "llamadas.jsonl"
        self.carga = self.raiz / "cargado"
        self.carga.write_text("no")
        for nombre in ("gh", "git"):
            ruta = self.falsos / nombre
            ruta.write_text("#!/bin/sh\nexit 0\n")
            ruta.chmod(0o755)
        launchctl = self.falsos / "launchctl"
        launchctl.write_text("#!" + sys.executable + "\n" + '''import json, os, sys
from pathlib import Path
with Path(os.environ["LAUNCHCTL_CALLS"]).open("a") as salida:
    salida.write(json.dumps(sys.argv[1:]) + "\\n")
estado = Path(os.environ["LAUNCHCTL_STATE"])
accion = sys.argv[1]
if accion == "print":
    sys.exit(0 if estado.read_text() == "sí" else 3)
if accion == "bootout":
    if os.environ.get("LAUNCHCTL_FAIL_BOOTOUT"):
        sys.exit(5)
    estado.write_text("no")
elif accion == "bootstrap":
    estado.write_text("sí")
else:
    sys.exit(2)
''')
        launchctl.chmod(0o755)
        self.variables = {
            "HOME": str(self.home),
            "XDG_STATE_HOME": str(self.raiz / "state"),
            "XDG_CONFIG_HOME": str(self.raiz / "config"),
            "XDG_DATA_HOME": str(self.raiz / "data"),
            "XDG_CACHE_HOME": str(self.raiz / "cache"),
            "PATH": str(self.falsos) + os.pathsep + os.environ["PATH"],
            "LAUNCHCTL_CALLS": str(self.calls), "LAUNCHCTL_STATE": str(self.carga),
        }
        self.env = patch.dict(os.environ, self.variables)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.plist = self.home / "Library/LaunchAgents" / (instalador.etiqueta(REPO) + ".plist")

    def ejecutar(self, *opciones):
        salida = io.StringIO()
        with redirect_stdout(salida):
            self.assertEqual(instalador.main(["--repo", REPO, *opciones]), 0)
        return salida.getvalue()

    def llamadas(self):
        return [json.loads(s) for s in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def test_plist_sombra_logs_y_path_sin_carga_implicita(self):
        salida = self.ejecutar()
        unidad = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(unidad["Label"], self.plist.stem)
        self.assertEqual(unidad["StartInterval"], 120)
        self.assertIs(unidad["RunAtLoad"], True)
        self.assertEqual(unidad["ProgramArguments"],
                         [str(self.cli), "tick", "--repo", REPO, "--seco"])
        self.assertNotIn("KeepAlive", unidad)
        variables = unidad["EnvironmentVariables"]
        for nombre, valor in self.variables.items():
            if nombre == "HOME" or nombre.startswith("XDG_"):
                self.assertEqual(variables[nombre], valor)
        for nombre in ("python3", "gh", "git"):
            self.assertIsNotNone(shutil.which(nombre, path=variables["PATH"]))
        for nombre in ("gh", "git"):
            self.assertEqual(shutil.which(nombre, path=variables["PATH"]), str(self.falsos / nombre))
        self.assertIn("/usr/bin", variables["PATH"].split(os.pathsep))
        logs = Path(self.variables["XDG_STATE_HOME"]) / "pao-tren/launchd" / self.plist.stem
        self.assertEqual(unidad["StandardOutPath"], str(logs / "stdout.log"))
        self.assertEqual(unidad["StandardErrorPath"], str(logs / "stderr.log"))
        self.assertTrue(logs.is_dir())
        self.assertIn("launchctl bootstrap gui/" + str(os.getuid()), salida)
        self.assertIn(str(self.plist), salida)
        self.assertEqual(self.llamadas(), [])

    def test_activo_quita_seco_respalda_y_vuelve_a_sombra(self):
        self.ejecutar("--cargar")
        sombra = self.plist.read_bytes()
        self.ejecutar("--activo", "--cargar")
        unidad = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(unidad["ProgramArguments"], [str(self.cli), "tick", "--repo", REPO])
        self.assertEqual([p.read_bytes() for p in self.plist.parent.glob("*.bak-*")], [sombra])
        self.assertEqual([c[0] for c in self.llamadas() if c[0] != "print"],
                         ["bootstrap", "bootout", "bootstrap"])
        self.ejecutar("--cargar")
        self.assertEqual(self.plist.read_bytes(), sombra)
        with redirect_stdout(io.StringIO()), patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as salida:
                instalador.main(["--repo", REPO, "--activo", "--retirar"])
        self.assertEqual(salida.exception.code, 2)
        self.assertTrue(self.plist.is_file())

    def test_identica_no_reescribe_y_distinta_respalda_antes(self):
        self.ejecutar()
        anterior = self.plist.read_bytes()
        estado = self.plist.stat()
        self.ejecutar()
        self.assertEqual(self.plist.read_bytes(), anterior)
        self.assertEqual(self.plist.stat().st_ino, estado.st_ino)
        self.assertEqual(self.plist.stat().st_mtime_ns, estado.st_mtime_ns)
        self.assertEqual(list(self.plist.parent.glob("*.bak-*")), [])
        unidad = plistlib.loads(anterior)
        unidad["StartInterval"] = 999
        distinto = plistlib.dumps(unidad)
        self.plist.write_bytes(distinto)
        atomico = instalador.atomico

        def comprobar_respaldo(ruta, contenido):
            self.assertEqual(ruta.read_bytes(), distinto)
            respaldos = list(ruta.parent.glob(ruta.name + ".bak-*"))
            self.assertEqual([p.read_bytes() for p in respaldos], [distinto])
            return atomico(ruta, contenido)

        with patch.object(instalador, "atomico", side_effect=comprobar_respaldo):
            self.ejecutar()
        self.assertEqual(self.plist.read_bytes(), anterior)
        self.assertEqual(list(self.plist.parent.glob("*.tmp")), [])

    def test_fallo_de_reemplazo_conserva_anterior_y_respaldo(self):
        self.ejecutar()
        anterior = self.plist.read_bytes() + b"\n"
        self.plist.write_bytes(anterior)
        with patch.object(instalador, "atomico", side_effect=OSError("fallo simulado")):
            with self.assertRaises(OSError):
                instalador.instalar(REPO)
        self.assertEqual(self.plist.read_bytes(), anterior)
        self.assertEqual([p.read_bytes() for p in self.plist.parent.glob("*.bak-*")], [anterior])

    def test_cargar_es_explicito_y_aplica_cambios_preparados(self):
        self.ejecutar("--cargar")
        bootstrap = [c for c in self.llamadas() if c[0] == "bootstrap"]
        self.assertEqual(bootstrap, [["bootstrap", "gui/" + str(os.getuid()), str(self.plist)]])
        self.plist.write_bytes(self.plist.read_bytes() + b"\n")
        self.ejecutar()
        self.ejecutar("--cargar")
        self.assertEqual([c[0] for c in self.llamadas() if c[0] != "print"],
                         ["bootstrap", "bootout", "bootstrap"])

    def test_retirar_no_borra_plist_logs_ledger_ni_respaldos(self):
        self.ejecutar("--cargar")
        contenido = self.plist.read_bytes()
        unidad = plistlib.loads(contenido)
        log = Path(unidad["StandardOutPath"])
        log.write_text("salida previa\n")
        ledger = Path(self.variables["XDG_STATE_HOME"]) / "pao-tren/ledger.sqlite3"
        ledger.write_bytes(b"ledger previo")
        respaldo = self.plist.with_name(self.plist.name + ".bak-previo")
        respaldo.write_bytes(b"plist previo")
        # Retirar no necesita python3 en PATH ni consultar las dependencias del tick.
        with patch.dict(os.environ, PATH=str(self.falsos)):
            self.ejecutar("--retirar")
        self.assertFalse(self.plist.exists())
        retirados = list(self.plist.parent.glob(self.plist.name + ".retirado-*"))
        self.assertEqual([p.read_bytes() for p in retirados], [contenido])
        self.assertEqual(log.read_text(), "salida previa\n")
        self.assertEqual(ledger.read_bytes(), b"ledger previo")
        self.assertEqual(respaldo.read_bytes(), b"plist previo")
        self.assertTrue(self.cli.is_symlink())
        self.ejecutar("--retirar")
        self.assertEqual(list(self.plist.parent.glob(self.plist.name + ".retirado-*")), retirados)
        self.assertEqual(len([c for c in self.llamadas() if c[0] == "bootout"]), 1)

    def test_bootout_fallido_conserva_plist(self):
        self.ejecutar("--cargar")
        contenido = self.plist.read_bytes()
        with patch.dict(os.environ, LAUNCHCTL_FAIL_BOOTOUT="1"):
            with self.assertRaises(subprocess.CalledProcessError):
                instalador.instalar(REPO, retirar=True)
        self.assertEqual(self.plist.read_bytes(), contenido)
        self.assertEqual(list(self.plist.parent.glob("*.retirado-*")), [])

    def test_repos_aislados(self):
        self.ejecutar()
        with redirect_stdout(io.StringIO()):
            instalador.main(["--repo", "other/project"])
        self.assertEqual(len(list(self.plist.parent.glob("*.plist"))), 2)
        self.assertNotEqual(instalador.etiqueta("a.b/c"), instalador.etiqueta("a/b.c"))
        self.ejecutar("--retirar")
        otro = self.plist.with_name(instalador.etiqueta("other/project") + ".plist")
        self.assertTrue(otro.is_file())

    def test_symlink_del_manifiesto_resuelve_modulos_con_python_aislado(self):
        fila = "file fleet/tren/pao-tren ~/.local/bin/pao-tren"
        self.assertIn(fila, (PROYECTO / "manifest").read_text().splitlines())
        # Un HOME nuevo permite probar la instalación file real sin tocar otras entradas.
        nuevo_home = self.raiz / "otro-home"
        nuevo_home.mkdir()
        manifiesto = self.raiz / "manifest"
        manifiesto.write_text(fila + "\n")
        env = dict(os.environ, HOME=str(nuevo_home), PAO_MANIFEST=str(manifiesto),
                   PATH=self.variables["PATH"].split(os.pathsep, 1)[1])
        resultado = subprocess.run([str(PROYECTO / "bin/pao"), "sync", "--no-plugins", "--no-schedule"],
                                   env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(resultado.returncode, 0, resultado.stdout + resultado.stderr)
        enlace = nuevo_home / ".local/bin/pao-tren"
        self.assertTrue(enlace.is_symlink())
        self.assertEqual(enlace.resolve(), RAIZ / "pao-tren")
        for flags in ([], ["-I"]):
            resultado = subprocess.run([sys.executable, *flags, str(enlace), "estado", "--repo", REPO],
                                       cwd=self.raiz, env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(resultado.returncode, 0, resultado.stderr)
            self.assertIn("estado", json.loads(resultado.stdout))

    def test_defaults_xdg_y_dependencias_ausentes(self):
        with patch.dict(os.environ, {k: "" for k in self.variables if k.startswith("XDG_")}):
            unidad = instalador.renderizar(REPO, self.home)
        self.assertEqual(unidad["EnvironmentVariables"]["XDG_STATE_HOME"],
                         str(self.home / ".local/state"))
        with patch.dict(os.environ, PATH=""):
            with self.assertRaisesRegex(ValueError, "falta en PATH"):
                instalador.renderizar(REPO, self.home)
        with patch.dict(os.environ, XDG_STATE_HOME="relativo"):
            with self.assertRaisesRegex(ValueError, "debe ser absoluto"):
                instalador.renderizar(REPO, self.home)


if __name__ == "__main__":
    unittest.main()
