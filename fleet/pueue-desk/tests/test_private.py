"""Instancia privada de Pueue 4.0.4 (sólo en la Mini): preparar, pueued y guardia reales.

HOME, configuración, estado y socket viven en un directorio temporal de la corrida; el socket
es relativo («s») y todos los procesos corren con cwd en ese HOME. Nunca toca el daemon del usuario.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
PUEUE = Path("/opt/homebrew/bin/pueue")
PUEUED = Path("/opt/homebrew/bin/pueued")


def available():
    if not PUEUED.exists():
        return False
    return subprocess.run([str(PUEUED), "--version"], text=True, capture_output=True).stdout.strip() == "pueued 4.0.4"


@unittest.skipUnless(available(), "requiere /opt/homebrew/bin/pueued 4.0.4 (Mini)")
class PrivateInstance(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="u17a-")
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name) / "h"
        self.data = self.home / "Library/Application Support/pueue"
        self.data.mkdir(parents=True)
        (self.data / "pueue.yml").write_text(json.dumps({"shared": {"unix_socket_path": "s"},
                                                         "daemon": {"compress_state_file": False}}))
        self.state = self.home / ".local/state/pao/pueue-desk"
        self.env = {"HOME": str(self.home), "PATH": "/opt/homebrew/bin:/usr/bin:/bin"}
        self.daemon = None
        self.addCleanup(self.cleanup)

    def cleanup(self):
        if self.daemon and self.daemon.poll() is None:
            self.daemon.kill()
            self.daemon.wait()
        # Sólo las tareas largas anotan su pid; se mata una vez y se borra el archivo, para no
        # mandar nunca una señal a un pid ya reutilizado por otro proceso de la Mini.
        for pidfile in self.home.glob("*.pid-tarea"):
            try:
                os.killpg(int(pidfile.read_text().split()[0]), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, ValueError):
                pass
            pidfile.unlink()

    def pueue(self, *args, check=True):
        result = subprocess.run([str(PUEUE), *args], cwd=self.home, env=self.env, text=True,
                                capture_output=True, timeout=10)
        if check and result.returncode:
            raise AssertionError(result.stderr or result.stdout)
        return result.stdout.strip()

    def status(self):
        return json.loads(self.pueue("status", "--json"))

    def control(self, *args):
        return subprocess.run(["/usr/bin/python3", "-I", str(ROOT / "control.py"), *args, "--host", "mini"],
                              cwd=self.home, env=self.env, text=True, capture_output=True, timeout=30)

    def boot(self):
        """Lo que hace io.pao.pueue: preparar y exec de pueued con el mismo PID."""
        self.daemon = subprocess.Popen(["/usr/bin/python3", "-I", str(ROOT / "control.py"), "preparar",
                                        "--host", "mini", "--exec"], cwd=self.home, env=self.env)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.pueue("status", "--json", check=False) and self.daemon.poll() is None:
                return self.status()
            if self.daemon.poll() is not None:
                break
            time.sleep(0.05)
        raise AssertionError("pueued no arrancó: " + self.log_text())

    def stop(self, sig):
        self.daemon.send_signal(sig)
        self.daemon.wait(timeout=10)
        self.daemon = None

    def guard(self):
        result = self.control("guardia")
        self.assertEqual(result.returncode, 0, result.stderr + self.log_text())

    def log_text(self):
        path = self.state / "guardia.log"
        return path.read_text() if path.exists() else ""

    def events(self, name):
        return [json.loads(line) for line in self.log_text().splitlines() if json.loads(line)["evento"] == name]

    def add(self, group, sentinel, sleep=None):
        marker = self.home / f"{sentinel}.pid-tarea"
        command = f"echo una >> {self.home / sentinel}"
        if sleep:
            command = f"echo $$ > {marker}; {command}; exec sleep {sleep}"
        return self.pueue("add", "--print-task-id", "-g", group, "--", command)

    def wait_status(self, task_id, name, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            status = self.status()["tasks"][task_id]["status"]
            if name in status:
                return status
            time.sleep(0.05)
        raise AssertionError(f"{task_id} no llegó a {name}: {status}")

    def lines(self, sentinel):
        path = self.home / sentinel
        return path.read_text().count("una") if path.exists() else 0

    def group_status(self, group):
        return self.status()["groups"][group]["status"]

    def opened_with_work_group(self):
        first = self.boot()
        self.assertEqual({g: v["status"] for g, v in first["groups"].items()}, {"default": "Paused", "suites": "Paused"})
        self.guard()
        self.assertEqual(self.group_status("suites"), "Running")
        self.pueue("group", "add", "work", "--parallel", "1")

    def test_interrupted_fails_once_and_queued_runs_once(self):
        for sig in (signal.SIGKILL, signal.SIGTERM):
            with self.subTest(signal=sig.name):
                self.setUp_again()
                self.opened_with_work_group()
                interrupted = self.add("work", "a", sleep=600)
                self.wait_status(interrupted, "Running")
                pending = self.add("work", "b")
                self.stop(sig)
                self.boot()
                self.assertEqual(self.status()["tasks"][interrupted]["status"]["Done"]["result"], "Killed")
                self.assertIn("Queued", self.status()["tasks"][pending]["status"])
                self.assertEqual(self.group_status("work"), "Paused")
                time.sleep(1)
                self.assertEqual(self.lines("b"), 0, "corrió antes de que el guardia abriera")
                self.guard()
                self.wait_status(pending, "Done")
                time.sleep(0.5)
                self.assertEqual((self.lines("a"), self.lines("b")), (1, 1))
                self.assertEqual(self.status()["tasks"][interrupted]["status"]["Done"]["result"], "Killed")
                if sig == signal.SIGKILL:
                    self.assertEqual([e["tarea"] for e in self.events("interrumpida")], [int(interrupted)])
                self.stop(signal.SIGTERM)
                self.cleanup()

    def setUp_again(self):
        if self.daemon:
            self.stop(signal.SIGKILL)
        self.cleanup()
        for path in sorted(self.home.rglob("*"), reverse=True):
            if path.is_file() and path.name != "pueue.yml":
                path.unlink()

    def test_queued_with_log_fails_and_holds_group_until_reanudar(self):
        self.opened_with_work_group()
        first = self.add("work", "a", sleep=600)
        self.wait_status(first, "Running")
        launched = self.add("work", "b")
        self.stop(signal.SIGKILL)
        # Lo que deja Pueue si muere entre crear el log y guardar Running (spawn.rs:134→:246).
        (self.data / f"task_logs/{launched}.log").write_text("")
        self.boot()
        status = self.status()["tasks"][launched]["status"]
        self.assertEqual(status["Done"]["result"], "Killed")
        self.assertIn("pudo haber empezado", (self.data / f"task_logs/{launched}.log").read_text())
        self.guard()
        self.assertEqual(self.group_status("work"), "Paused")
        self.assertEqual(self.group_status("suites"), "Running")
        later = self.add("work", "c")
        time.sleep(1)
        self.assertEqual(self.lines("c"), 0)
        result = self.control("reanudar", "work")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.wait_status(later, "Done")
        self.assertEqual(self.lines("b"), 0, "la tarea con log no se reejecutó")

    def test_empty_open_group_admits_nothing_before_guard(self):
        self.opened_with_work_group()
        self.stop(signal.SIGKILL)
        self.boot()
        probe = self.add("work", "d")
        time.sleep(1)
        self.assertIn("Queued", self.status()["tasks"][probe]["status"])
        self.guard()
        self.wait_status(probe, "Done")

    def test_manual_pause_survives_restarts(self):
        self.opened_with_work_group()
        self.assertEqual(self.control("pausar", "work").returncode, 0)
        probe = self.add("work", "e")
        for sig in (signal.SIGTERM, signal.SIGKILL):
            self.stop(sig)
            self.boot()
            self.guard()
            self.assertEqual(self.group_status("work"), "Paused")
            self.assertIn("Queued", self.status()["tasks"][probe]["status"])
        self.assertEqual(self.lines("e"), 0)

    def test_unreadable_state_does_not_start_or_overwrite(self):
        self.opened_with_work_group()
        self.add("work", "f")
        self.stop(signal.SIGTERM)
        path = self.data / "state.json"
        broken = path.read_text()[:60]
        path.write_text(broken)
        result = subprocess.run(["/usr/bin/python3", "-I", str(ROOT / "control.py"), "preparar", "--host", "mini",
                                 "--exec"], cwd=self.home, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(path.read_text(), broken)
        self.assertNotEqual(subprocess.run([str(PUEUE), "status"], cwd=self.home, env=self.env,
                                           capture_output=True, timeout=10).returncode, 0)
        self.assertIn("ilegible", self.events("preparar_rechazado")[-1]["detalle"])

    def test_prepare_twice_is_identical(self):
        self.opened_with_work_group()
        task = self.add("work", "g", sleep=600)
        self.wait_status(task, "Running")
        self.add("work", "h")
        self.stop(signal.SIGKILL)
        snapshots = []
        for _ in range(2):
            self.assertEqual(self.control("preparar").returncode, 0, self.log_text())
            snapshots.append(((self.data / "state.json").read_bytes(), (self.state / "guardia-state.json").read_bytes()))
        self.assertEqual(snapshots[0], snapshots[1])


if __name__ == "__main__":
    unittest.main()
