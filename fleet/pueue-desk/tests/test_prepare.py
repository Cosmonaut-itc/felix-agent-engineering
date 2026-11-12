"""U17a: preparar el arranque con el daemon parado y abrir sólo con admisión."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("control", ROOT / "control.py")
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)

NOW = "2026-11-12T03:00:00.000000-06:00"
LIMITS = {"hasito": 3, "jale": 2}


def task(task_id, status, group="hasito"):
    return {"id": task_id, "created_at": "2026-11-12T01:00:00-06:00", "original_command": "true",
            "command": "true", "path": "/tmp", "envs": {}, "group": group, "dependencies": [],
            "priority": 0, "label": None, "status": status}


def queued():
    return {"Queued": {"enqueued_at": "2026-11-12T01:00:01-06:00"}}


def running(name="Running"):
    return {name: {"enqueued_at": "2026-11-12T01:00:01-06:00", "start": "2026-11-12T01:00:02-06:00"}}


def done(result="Success"):
    return {"Done": {"enqueued_at": "2026-11-12T01:00:01-06:00", "start": "2026-11-12T01:00:02-06:00",
                     "end": "2026-11-12T01:00:03-06:00", "result": result}}


def empty_state():
    return {"owned": [], "retenidos": {}, "relevo": None}


class PrepareTests(unittest.TestCase):
    def queue(self, tasks, groups=None):
        return {"tasks": {str(t["id"]): t for t in tasks},
                "groups": groups or {"default": {"status": "Running", "parallel_tasks": 1},
                                     "hasito": {"status": "Running", "parallel_tasks": 3},
                                     "jale": {"status": "Running", "parallel_tasks": 2}}}

    def test_interrupted_fail_visible_and_are_never_requeued(self):
        source = self.queue([task(0, running()), task(1, running("Paused")), task(2, queued())])
        result, state, events = control.prepare(source, set(), empty_state(), NOW, LIMITS)
        for task_id in ("0", "1"):
            status = result["tasks"][task_id]["status"]
            self.assertEqual(list(status), ["Done"])
            self.assertEqual(status["Done"]["result"], "Killed")
            self.assertEqual(status["Done"]["start"], "2026-11-12T01:00:02-06:00")
            self.assertEqual(status["Done"]["end"], NOW)
        self.assertEqual(result["tasks"]["2"]["status"], queued())
        interrupted = [e for e in events if e["evento"] == "interrumpida"]
        self.assertEqual([e["tarea"] for e in interrupted], [0, 1])
        self.assertTrue(all("causa" in e and e["created_at"] for e in interrupted))
        self.assertEqual(state["retenidos"], {})
        self.assertEqual(source["tasks"]["0"]["status"], running())

    def test_queued_with_log_fails_and_holds_its_group(self):
        source = self.queue([task(0, queued()), task(1, queued(), "jale")])
        result, state, events = control.prepare(source, {"0"}, empty_state(), NOW, LIMITS)
        self.assertEqual(result["tasks"]["0"]["status"]["Done"]["result"], "Killed")
        self.assertEqual(result["tasks"]["1"]["status"], queued())
        self.assertEqual(state["retenidos"], {"hasito": [0]})
        self.assertIn("reanudar hasito", [e for e in events if e["evento"] == "retenido"][0]["motivo"])

    def test_groups_closed_owned_and_missing_ones_created_closed(self):
        groups = {"default": {"status": "Running", "parallel_tasks": 1},
                  "manual": {"status": "Paused", "parallel_tasks": 1},
                  "borrando": {"status": "Reset", "parallel_tasks": 1},
                  "hasito": {"status": "Running", "parallel_tasks": 3}}
        result, state, _ = control.prepare(self.queue([], groups), set(), empty_state(), NOW, LIMITS)
        self.assertEqual(result["groups"]["default"]["status"], "Paused")
        self.assertEqual(result["groups"]["manual"]["status"], "Paused")
        self.assertEqual(result["groups"]["borrando"]["status"], "Reset")
        self.assertEqual(result["groups"]["jale"], {"status": "Paused", "parallel_tasks": 2})
        self.assertEqual(state["owned"], ["default", "hasito", "jale"])
        self.assertNotIn("manual", state["owned"])

    def test_first_boot_has_default_and_managed_groups_closed(self):
        result, state, _ = control.prepare(None, set(), empty_state(), NOW, LIMITS)
        self.assertEqual({g: v["status"] for g, v in result["groups"].items()},
                         {"default": "Paused", "hasito": "Paused", "jale": "Paused"})
        self.assertEqual(result["tasks"], {})
        self.assertEqual(state["owned"], ["default", "hasito", "jale"])

    def test_second_pass_changes_nothing(self):
        source = self.queue([task(0, running()), task(1, queued()), task(2, queued(), "jale")])
        first = control.prepare(source, {"1"}, empty_state(), NOW, LIMITS)
        second = control.prepare(first[0], set(), first[1], "2026-11-12T04:00:00-06:00", LIMITS)
        self.assertEqual(first[0], second[0])
        self.assertEqual(first[1], second[1])
        self.assertEqual(second[2], [])

    def test_kills_are_never_reopened(self):
        # W9: un kill manual por id, grupo o todos deja Done/Killed y preparar no lo toca.
        source = self.queue([task(0, done("Killed")), task(1, done({"Failed": 3})), task(2, done())])
        result, _, events = control.prepare(source, {"0"}, empty_state(), NOW, LIMITS)
        self.assertEqual(result["tasks"], source["tasks"])
        self.assertFalse([e for e in events if e["evento"] == "interrumpida"])


FAKE = r'''#!/usr/bin/python3
import json, os, sys
from pathlib import Path
root = Path(os.environ["FAKE_ROOT"])
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with (root / "calls").open("a") as f:
    f.write(json.dumps([name, *args]) + "\n")
if name == "pueued":
    print(os.environ.get("FAKE_VERSION", "pueued 4.0.4"))
elif name == "docker":
    sys.exit(1 if (root / "docker-caido").exists() else 0)
elif name == "pueue":
    path = root / "daemon.json"
    if not path.exists():
        sys.exit("Error: connection refused")
    state = json.loads(path.read_text())
    if args == ["status", "--json"]:
        print(json.dumps(state))
    elif args[0] in ("pause", "start"):
        state["groups"][args[-1]]["status"] = "Paused" if args[0] == "pause" else "Running"
        path.write_text(json.dumps(state))
    else:
        sys.exit("no implementado: " + str(args))
'''


class Host(unittest.TestCase):
    """HOME temporal con pueue, pueued y docker falsos; el daemon «responde» si existe daemon.json."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("pueue", "pueued", "docker"):
            (self.bin / name).write_text(FAKE)
            (self.bin / name).chmod(0o755)
        env = {k: v for k, v in os.environ.items() if not k.startswith("XDG_")}
        env.update(HOME=str(self.root), FAKE_ROOT=str(self.root))
        patcher = patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.data = self.root / ".local/share/pueue"
        self.data.mkdir(parents=True)
        self.state_dir = self.root / "state"
        self.pueue = control.Pueue(self.bin / "pueue")
        self.proc = self.root / "proc"
        (self.proc / "pressure").mkdir(parents=True)
        self.metrics()

    def metrics(self, io_value=0, cpu=0, memory=8):
        (self.proc / "pressure/io").write_text(f"full avg10=0 avg60={io_value} avg300=0 total=1\n")
        (self.proc / "pressure/cpu").write_text(f"some avg10=0 avg60={cpu} avg300=0 total=1\n")
        (self.proc / "meminfo").write_text(f"MemAvailable: {int(memory * 1024**2)} kB\n")

    def write_disk(self, value):
        (self.data / "state.json").write_text(json.dumps(value))

    def disk(self):
        return json.loads((self.data / "state.json").read_text())

    def prepare(self):
        control.cmd_prepare("desk", self.pueue, self.bin / "pueued", self.state_dir, now=NOW)

    def boot(self):
        """El daemon arranca con lo que preparar dejó en disco, como hace Pueue."""
        (self.root / "daemon.json").write_text((self.data / "state.json").read_text())

    def calls(self):
        path = self.root / "calls"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def records(self):
        path = self.state_dir / "guardia.log"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def guard(self):
        with control.locked(self.state_dir):
            return control.guard(self.pueue, self.proc, self.state_dir, self.bin / "docker")

    def full_queue(self, tasks, groups=None):
        groups = groups or {"default": {"status": "Running", "parallel_tasks": 1},
                            **{g: {"status": "Running", "parallel_tasks": 1} for g in control.GROUP_LIMITS}}
        return {"tasks": {str(t["id"]): t for t in tasks}, "groups": groups}


class CommandPrepareTests(Host):
    def test_interrupted_and_logged_tasks_end_visible(self):
        self.write_disk(self.full_queue([task(0, running()), task(1, queued()), task(2, queued(), "jale")]))
        (self.data / "task_logs").mkdir()
        (self.data / "task_logs/1.log").write_text("salida previa\n")
        self.prepare()
        disk = self.disk()
        self.assertEqual(disk["tasks"]["0"]["status"]["Done"]["result"], "Killed")
        self.assertEqual(disk["tasks"]["1"]["status"]["Done"]["result"], "Killed")
        self.assertEqual(disk["tasks"]["2"]["status"], queued())
        self.assertEqual({g["status"] for g in disk["groups"].values()}, {"Paused"})
        state = control.load_control(self.state_dir)
        self.assertEqual(state["retenidos"], {"hasito": [1]})
        note = (self.data / "task_logs/1.log").read_text()
        self.assertTrue(note.startswith("salida previa\n"))
        self.assertIn("nunca con pueue restart", note)
        causes = {r["tarea"]: r["causa"] for r in self.records() if r["evento"] == "interrumpida"}
        self.assertEqual(set(causes), {0, 1})

    def test_prepare_twice_same_bytes(self):
        self.write_disk(self.full_queue([task(0, running()), task(1, queued())]))
        self.prepare()
        first = ((self.data / "state.json").read_bytes(), (self.state_dir / "guardia-state.json").read_bytes())
        self.prepare()
        second = ((self.data / "state.json").read_bytes(), (self.state_dir / "guardia-state.json").read_bytes())
        self.assertEqual(first, second)
        self.assertEqual([r["cambios"] for r in self.records() if r["evento"] == "preparado"][-1], 0)

    def test_death_between_ownership_and_queue_is_completed_by_the_retry(self):
        self.write_disk(self.full_queue([task(0, running())]))
        original = self.disk()
        real = control.atomic_json

        def dies_on_queue(target, value):
            if target.name == "state.json":
                raise OSError("muere entre owned y state.json")
            real(target, value)

        with patch.object(control, "atomic_json", side_effect=dies_on_queue):
            with self.assertRaises(OSError):
                self.prepare()
        # La propiedad ya es durable y el disco sigue intacto: ninguna pausa quedará sin dueño.
        self.assertEqual(self.disk(), original)
        self.assertIn("hasito", control.load_control(self.state_dir)["owned"])
        self.prepare()
        self.assertEqual(self.disk()["tasks"]["0"]["status"]["Done"]["result"], "Killed")
        self.assertEqual(self.disk()["groups"]["hasito"]["status"], "Paused")

    def test_rejections_never_write(self):
        good = self.full_queue([task(0, queued())])
        cases = {
            "daemon responde": lambda: (self.root / "daemon.json").write_text(json.dumps(good)),
            "versión distinta": lambda: os.environ.__setitem__("FAKE_VERSION", "pueued 4.1.0"),
            "estado comprimido": lambda: (self.data / "state.json.gz").write_bytes(b"\x1f\x8b"),
            "json truncado": lambda: (self.data / "state.json").write_text(json.dumps(good)[:40]),
            "tarea ilegible": lambda: self.write_disk({**good, "tasks": {"0": {"id": 0}}}),
            "pueue_directory propio": lambda: (self.root / ".config/pueue").mkdir(parents=True, exist_ok=True) or
            (self.root / ".config/pueue/pueue.yml").write_text("shared:\n  pueue_directory: /otra\n"),
            "guardia ilegible": lambda: self.state_dir.mkdir(exist_ok=True) or
            (self.state_dir / "guardia-state.json").write_text("{roto"),
        }
        for name, breaker in cases.items():
            with self.subTest(name):
                for path in (self.root / "daemon.json", self.data / "state.json.gz",
                             self.root / ".config/pueue/pueue.yml", self.state_dir / "guardia-state.json"):
                    path.unlink(missing_ok=True)
                os.environ.pop("FAKE_VERSION", None)
                self.write_disk(good)
                breaker()
                before = (self.data / "state.json").read_bytes()
                with patch.object(control, "STATE_DIR", self.state_dir):
                    code = control.main(["preparar", "--pueue-bin", str(self.bin / "pueue"),
                                         "--pueued-bin", str(self.bin / "pueued")])
                self.assertEqual(code, 1)
                self.assertEqual((self.data / "state.json").read_bytes(), before)
                self.assertEqual(self.records()[-1]["evento"], "preparar_rechazado")

    def test_stale_pid_of_another_process_is_cleared(self):
        self.write_disk(self.full_queue([]))
        pid = self.data / "pueue.pid"
        pid.write_text(str(os.getpid()))  # vivo, pero es python, no pueued
        self.prepare()
        self.assertFalse(pid.exists())
        self.assertEqual([r for r in self.records() if r["evento"] == "pid_obsoleto"][0]["pid"], os.getpid())


class AdmissionTests(Host):
    def booted(self, tasks=(), logged=()):
        self.write_disk(self.full_queue(list(tasks)))
        if logged:
            (self.data / "task_logs").mkdir()
            for task_id in logged:
                (self.data / f"task_logs/{task_id}.log").write_text("")
        self.prepare()
        self.boot()

    def groups(self):
        return {g: v["status"] for g, v in self.pueue.status()["groups"].items()}

    def test_docker_down_nothing_opens(self):
        self.booted([task(0, queued())])
        (self.root / "docker-caido").touch()
        for _ in range(2):
            action, reason, opened = self.guard()
            self.assertEqual((action, reason, opened), ("pausar", "docker", []))
        self.assertEqual(set(self.groups().values()), {"Paused"})
        self.assertFalse(any(c[:2] == ["pueue", "start"] for c in self.calls()))

    def test_docker_back_with_low_pressure_reopens_only_own_pauses(self):
        disk = self.full_queue([task(0, queued())])
        disk["groups"]["jale"]["status"] = "Paused"  # pausa manual previa
        self.write_disk(disk)
        self.prepare()
        self.boot()
        (self.root / "docker-caido").touch()
        self.guard()
        (self.root / "docker-caido").unlink()
        self.metrics(io_value=30)  # entre umbrales: mantener
        self.assertEqual(self.guard()[0], "mantener")
        self.metrics()
        _, _, opened = self.guard()
        self.assertEqual(opened, ["default", "hasito", "otros", "pg5434"])
        self.assertEqual(self.groups()["jale"], "Paused")
        self.assertEqual(control.load_control(self.state_dir)["owned"], [])

    def test_retained_group_waits_for_reanudar(self):
        self.booted([task(0, queued()), task(1, queued(), "jale")], logged=[0])
        _, _, opened = self.guard()
        self.assertNotIn("hasito", opened)
        self.assertIn("jale", opened)
        self.assertEqual(self.groups()["hasito"], "Paused")
        self.assertEqual(self.guard()[2], [])

    def test_reanudar_goes_through_admission(self):
        self.booted([task(0, queued())], logged=[0])
        (self.root / "docker-caido").touch()
        with control.locked(self.state_dir):
            control.resume(self.pueue, self.proc, self.state_dir, "hasito", self.bin / "docker", "desk")
        self.assertEqual(self.groups()["hasito"], "Paused")
        self.assertFalse(any(c[:2] == ["pueue", "start"] for c in self.calls()))
        state = control.load_control(self.state_dir)
        self.assertEqual(state["retenidos"], {})
        self.assertIn("hasito", state["owned"])
        (self.root / "docker-caido").unlink()
        self.metrics(cpu=31)
        self.guard()
        self.assertEqual(self.groups()["hasito"], "Paused")
        self.metrics()
        self.assertIn("hasito", self.guard()[2])

    def test_reanudar_lifts_a_manual_pause_only_with_admission(self):
        self.booted()
        self.pueue.call("pause", "--wait", "-g", "otros")
        state = control.load_control(self.state_dir)
        state["owned"].remove("otros")
        control.save_control(self.state_dir, state)
        self.guard()
        self.assertEqual(self.groups()["otros"], "Paused")
        with control.locked(self.state_dir):
            control.resume(self.pueue, self.proc, self.state_dir, "otros", self.bin / "docker", "desk")
        self.assertEqual(self.groups()["otros"], "Running")

    def test_group_opened_by_hand_releases_retention(self):
        self.booted([task(0, queued())], logged=[0])
        self.pueue.call("start", "-g", "hasito")
        self.guard()
        self.assertEqual(control.load_control(self.state_dir)["retenidos"], {})
        self.assertIn("retencion_liberada", [r["evento"] for r in self.records()])


class LogTests(unittest.TestCase):
    def test_host_log_is_capped_while_writer_keeps_appending(self):
        with tempfile.TemporaryDirectory() as name:
            target = Path(name) / "host.log"
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, b"x" * 100)
                control.cap_log(target, limit=50)
                os.write(fd, b"nuevo\n")
            finally:
                os.close(fd)
            self.assertEqual(target.read_bytes(), b"nuevo\n")
            self.assertEqual((Path(name) / "host.log.1").stat().st_size, 100)

    def test_every_host_log_has_a_limit(self):
        # guardia.log rota en log(); host.log lo acota vigilar; pueued del desk va al journal.
        plist = (ROOT / "launchagents/io.pao.pueue.plist").read_text()
        self.assertNotIn("StandardOutPath", plist)
        self.assertNotIn("StandardErrorPath", plist)
        self.assertIn("cap_log(m.state / \"host.log\")", (ROOT / "mini.py").read_text())
        self.assertNotIn("StandardOutput=", (ROOT / "units/pueued.service").read_text())


class UnitTests(unittest.TestCase):
    def test_daemon_unit_prepares_and_restarts(self):
        unit = (ROOT / "units/pueued.service").read_text()
        self.assertIn("ExecStartPre=/usr/bin/python3 -I @HOME@/.local/lib/pao-pueue-desk/control.py preparar --host desk", unit)
        for line in ("KillMode=mixed", "Restart=always", "StartLimitIntervalSec=0", "ExecStop=-"):
            self.assertIn(line, unit)

    def test_timers_require_the_daemon(self):
        # Por esto la parada de mantenimiento detiene antes los timers (ver test de parar).
        for name in ("pueue-desk-guard.service", "pueue-desk-mode.service"):
            self.assertIn("Requires=pueued.service", (ROOT / "units" / name).read_text())


if __name__ == "__main__":
    unittest.main()
