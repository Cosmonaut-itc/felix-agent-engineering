from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("control", ROOT / "control.py")
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)

FAKE = r'''#!/usr/bin/python3
import json
import os
from pathlib import Path
import sys
name = Path(sys.argv[0]).name
root = Path(os.environ["FAKE_ROOT"])
args = sys.argv[1:]
if name == "ssh":
    import subprocess
    sys.exit(subprocess.run(["/usr/bin/python3", "-I", "-"], input=sys.stdin.read(), text=True).returncode)
with (root / "calls").open("a") as f:
    f.write(json.dumps([name, *args]) + "\n")
state_file = root / "pueue.json"
state = json.loads(state_file.read_text())
if name == "pueue":
    if args == ["status", "--json"]:
        print(json.dumps(state))
    elif args[:2] == ["group", "add"]:
        assert args[3] == "--parallel" and 1 <= int(args[4]) <= 8
        state["groups"][args[2]] = {"status": "Running", "parallel_tasks": int(args[4])}
    elif args[:2] == ["group", "remove"]:
        del state["groups"][args[2]]
    elif args[0] == "parallel":
        assert 1 <= int(args[1]) <= 8
        state["groups"][args[3]]["parallel_tasks"] = int(args[1])
    elif args[0] == "start" and (root / "start-falla").exists():
        (root / "start-falla").unlink()
        sys.exit("start falló")
    elif args[0] in ("pause", "start"):
        state["groups"][args[-1]]["status"] = "Paused" if args[0] == "pause" else "Running"
    elif args == ["shutdown"]:
        (root / "manual").unlink()
    else:
        sys.exit("Comando falso no implementado: " + str(args))
    state_file.write_text(json.dumps(state))
elif name == "pgrep":
    sys.exit(0 if (root / "manual").exists() else 1)
elif name == "systemctl":
    if "is-active" in args:
        sys.exit(0 if (root / "managed").exists() else 3)
elif name == "loginctl" and (root / "linger-falla").exists():
    sys.exit("Access denied")
elif name not in ("loginctl", "pueued"):
    sys.exit("Ejecutable falso desconocido: " + name)
'''


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / ".local/bin"
        self.bin.mkdir(parents=True)
        for name in ("pueue", "pueued", "ssh", "systemctl", "pgrep", "loginctl"):
            target = self.bin / name
            target.write_text(FAKE)
            target.chmod(0o755)
        self.env = {**os.environ, "HOME": str(self.root), "FAKE_ROOT": str(self.root),
                    "PATH": str(self.bin) + os.pathsep + os.environ["PATH"]}
        env = patch.dict(os.environ, self.env)
        env.start()
        self.addCleanup(env.stop)
        self.pueue = control.Pueue(self.bin / "pueue")
        self.write_state({"groups": {g: {"status": "Running", "parallel_tasks": 1}
                                     for g in control.GROUP_LIMITS}, "tasks": {}})
        self.proc = self.root / "proc"
        (self.proc / "pressure").mkdir(parents=True)
        self.directory = self.root / "guard"
        self.directory.mkdir()

    def write_state(self, state):
        (self.root / "pueue.json").write_text(json.dumps(state))

    def calls(self):
        target = self.root / "calls"
        return [json.loads(line) for line in target.read_text().splitlines()] if target.exists() else []

    def metrics(self, io_value=0, cpu=0, memory=8):
        (self.proc / "pressure/io").write_text(f"some avg60=99\nfull avg10=99 avg60={io_value} avg300=99 total=1\n")
        (self.proc / "pressure/cpu").write_text(f"some avg10=99 avg60={cpu} avg300=99 total=1\n")
        (self.proc / "meminfo").write_text(f"MemAvailable: {int(memory * 1024**2)} kB\n")

    def guard(self):
        with control.locked(self.directory):
            control.guard(self.pueue, self.proc, self.directory)

    def deploy(self, *args):
        return subprocess.run(["bash", str(ROOT / "deploy-desk.sh"), *args],
                              env=self.env, text=True, capture_output=True)


class ControlTests(Fixture):
    def test_utc_boundaries(self):
        for hour, mode in [("04:59:59", "dia"), ("05:00:00", "noche"),
                           ("13:59:59", "noche"), ("14:00:00", "dia")]:
            self.assertEqual(control.mode_at(datetime.fromisoformat("2026-11-12T" + hour + "+00:00")), mode)
        self.assertEqual(control.mode_at(datetime.fromisoformat("2026-11-12T00:00:00-06:00")), "noche")

    def test_reject_limits_before_any_pueue_call(self):
        for invalid in (0, -1, 9, 1.5, True, "3"):
            with self.assertRaises(ValueError):
                self.pueue.parallel("hasito", invalid)
        with patch.dict(control.GROUP_LIMITS, {"hasito": {"dia": 0}}):
            with self.assertRaises(ValueError):
                control.apply_mode(self.pueue, datetime.fromisoformat("2026-11-12T15:00:00+00:00"))
        self.assertEqual(self.calls(), [])

    def test_apply_idempotent_and_creates_groups(self):
        self.write_state({"groups": {}, "tasks": {}})
        for hour in (5, 14):
            now = datetime.fromisoformat(f"2026-11-12T{hour:02}:00:00+00:00")
            control.apply_mode(self.pueue, now)
            first = self.pueue.status()
            control.apply_mode(self.pueue, now)
            self.assertEqual(first, self.pueue.status())
            for group, row in control.GROUP_LIMITS.items():
                self.assertEqual(first["groups"][group]["parallel_tasks"], row[control.mode_at(now)])

    def test_hysteresis_manual_pause_and_log(self):
        self.pueue.call("pause", "--wait", "-g", "jale")
        self.metrics(io_value=41)
        self.guard()
        owned = json.loads((self.directory / "guardia-state.json").read_text())
        self.assertEqual(set(owned), {"hasito", "otros", "pg5434"})
        self.metrics(io_value=30, cpu=20, memory=5)
        before = self.calls()
        self.guard()
        self.assertFalse(any(c[1] in ("pause", "start") for c in self.calls()[len(before):]))
        self.metrics(io_value=19, cpu=14, memory=6)
        self.guard()
        self.assertEqual(self.pueue.status()["groups"]["jale"]["status"], "Paused")
        self.assertEqual(json.loads((self.directory / "guardia-state.json").read_text()), [])
        records = [json.loads(line) for line in (self.directory / "guardia.log").read_text().splitlines()]
        self.assertEqual(len([r for r in records if r["evento"] == "lectura"]), 3)
        self.assertEqual([r["accion"] for r in records if r["evento"] == "decision"], ["pausar", "mantener", "reanudar"])
        for record in records:
            self.assertIsNotNone(datetime.fromisoformat(record["utc"]).tzinfo)

    def test_thresholds(self):
        for values, expected in [({"io_value": 40, "cpu": 30, "memory": 4}, "mantener"),
                                 ({"io_value": 20}, "mantener"), ({"cpu": 15}, "mantener"),
                                 ({"cpu": 31}, "pausar"), ({"memory": 3}, "pausar")]:
            with self.subTest(values=values):
                self.metrics(**values)
                self.guard()
                records = [json.loads(line) for line in (self.directory / "guardia.log").read_text().splitlines()]
                self.assertEqual([r["accion"] for r in records if r["evento"] == "decision"][-1], expected)

    def test_quotas_per_mode(self):
        expected = {"dia": {"hasito": 3, "jale": 5, "otros": 1, "pg5434": 1, "rapidas": 2},
                    "noche": {"hasito": 4, "jale": 7, "otros": 1, "pg5434": 1, "rapidas": 2}}
        self.write_state({"groups": {}, "tasks": {}})
        for hour, mode in ((15, "dia"), (6, "noche")):
            control.apply_mode(self.pueue, datetime.fromisoformat(f"2026-11-12T{hour:02}:00:00+00:00"))
            self.assertEqual({g: s["parallel_tasks"] for g, s in self.pueue.status()["groups"].items()},
                             expected[mode])

    def test_rapidas_pauses_and_resumes_only_by_memory(self):
        def step(memory, normal, rapid, owned, **metrics):
            self.metrics(memory=memory, **metrics)
            self.guard()
            records = [json.loads(line) for line in (self.directory / "guardia.log").read_text().splitlines()]
            decision = [r for r in records if r["evento"] == "decision"][-1]
            self.assertEqual((decision["accion"], decision["accion_rapidas"]), (normal, rapid))
            self.assertEqual(set(json.loads((self.directory / "guardia-state.json").read_text())), owned)
        busy = {"hasito", "jale", "otros", "pg5434"}
        step(8, "pausar", "mantener", busy, io_value=41, cpu=31)
        self.assertEqual(self.pueue.status()["groups"]["rapidas"]["status"], "Running")
        step(3, "pausar", "pausar", busy | {"rapidas"}, io_value=41)
        step(5, "pausar", "mantener", busy | {"rapidas"}, cpu=31)
        step(6, "pausar", "reanudar", busy, io_value=41)
        self.assertEqual(self.pueue.status()["groups"]["rapidas"]["status"], "Running")
        self.assertEqual(self.pueue.status()["groups"]["hasito"]["status"], "Paused")

    def test_missing_group_is_skipped_and_released(self):
        control.save_owned(self.directory, {"rapidas"})
        state = self.pueue.status()
        del state["groups"]["rapidas"]
        self.write_state(state)
        self.metrics(memory=3)
        self.guard()
        self.assertEqual(set(json.loads((self.directory / "guardia-state.json").read_text())),
                         {"hasito", "jale", "otros", "pg5434"})
        self.assertNotIn("rapidas", self.pueue.status()["groups"])

    def test_manual_takeover_of_guard_pause(self):
        self.metrics(cpu=31)
        self.guard()
        with patch.object(control, "STATE_DIR", self.directory), patch.object(control, "Pueue", return_value=self.pueue):
            with patch("sys.argv", ["control.py", "pausar", "hasito"]):
                control.main()
        self.metrics()
        self.guard()
        self.assertEqual(self.pueue.status()["groups"]["hasito"]["status"], "Paused")

    def test_failed_resume_keeps_ownership_and_retries(self):
        self.metrics(io_value=41)
        self.guard()
        (self.root / "start-falla").touch()
        self.metrics()
        with self.assertRaises(subprocess.CalledProcessError):
            self.guard()
        self.assertIn("hasito", json.loads((self.directory / "guardia-state.json").read_text()))
        self.guard()
        self.assertEqual({g["status"] for g in self.pueue.status()["groups"].values()}, {"Running"})
        self.assertEqual(json.loads((self.directory / "guardia-state.json").read_text()), [])

    def test_failed_apply_does_not_restart_daemon(self):
        units = ROOT / "units"
        daemon = (units / "pueued.service").read_text()
        self.assertNotIn("ExecStartPost", daemon)
        self.assertIn("Wants=pueue-desk-mode.service", daemon)
        mode = (units / "pueue-desk-mode.service").read_text()
        self.assertIn("Restart=on-failure", mode)
        self.assertIn("After=pueued.service", mode)

    def test_invalid_readings_never_resume(self):
        self.metrics(io_value=41)
        self.guard()
        for invalid in ("nan", "inf", "-1", "101"):
            self.metrics(io_value=invalid)
            with self.assertRaises(ValueError):
                self.guard()
        self.assertFalse(any(c[1] == "start" for c in self.calls()))

    def test_rotation(self):
        (self.directory / "guardia.log").write_text("x" * control.LOG_MAX_BYTES)
        control.log(self.directory, "decision", accion="mantener")
        self.assertEqual((self.directory / "guardia.log.1").stat().st_size, control.LOG_MAX_BYTES)
        self.assertEqual(json.loads((self.directory / "guardia.log").read_text())["evento"], "decision")


class DeploymentTests(Fixture):
    def test_dry_run(self):
        (self.root / "manual").touch()
        result = self.deploy("--seco")
        self.assertEqual(result.returncode, 0, result.stderr)
        print("\nSALIDA DEPLOY --seco (ssh falso):\n" + result.stdout)
        self.assertIn("SECO: pueue shutdown", result.stdout)
        self.assertFalse((self.root / ".config").exists())
        self.assertTrue((self.root / "manual").exists())
        self.assertEqual([c[0] for c in self.calls()], ["systemctl", "pgrep", "pueue"])

    def test_live_manual_aborts_without_changes(self):
        for status in ("Queued", "Paused", {"Queued": {"enqueued_at": "now"}},
                       {"Paused": {"start": "now"}}, {"Running": {"start": "now"}}, "Stashed", "Locked"):
            for args in ((), ("--seco",)):
                with self.subTest(status=status, args=args):
                    (self.root / "manual").touch()
                    state = self.pueue.status()
                    state["tasks"] = {"0": {"status": status, "group": "hasito"}}
                    self.write_state(state)
                    before = len(self.calls())
                    result = self.deploy(*args)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("abortada sin cambios", result.stderr)
                    self.assertTrue((self.root / "manual").exists())
                    self.assertFalse((self.root / ".config").exists())
                    self.assertEqual([c[0] for c in self.calls()[before:]], ["systemctl", "pgrep", "pueue"])

    def test_rejected_linger_keeps_manual_daemon(self):
        (self.root / "manual").touch()
        (self.root / "linger-falla").touch()
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Access denied", result.stderr)
        self.assertTrue((self.root / "manual").exists())
        self.assertFalse(any(c == ["pueue", "shutdown"] for c in self.calls()))
        self.assertFalse((self.root / ".config").exists())

    def test_failure_after_shutdown_is_announced(self):
        (self.root / "manual").touch()
        (self.root / ".config").write_text("no es un directorio")
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("el pueued manual quedó detenido", result.stderr)

    def test_install_twice_and_withdraw_preserves_queue(self):
        (self.root / "manual").touch()
        state = self.pueue.status()
        state["groups"]["suites"] = {"status": "Running", "parallel_tasks": 2}
        state["tasks"] = {"1": {"status": {"Done": "Success"}, "group": "hasito"}}
        self.write_state(state)
        for _ in range(2):
            result = self.deploy()
            self.assertEqual(result.returncode, 0, result.stderr)
        units = self.root / ".config/systemd/user"
        self.assertEqual(len(list(units.iterdir())), 5)
        self.assertNotIn("@HOME@", (units / "pueued.service").read_text())
        self.assertNotIn("suites", self.pueue.status()["groups"])
        before = self.pueue.status()
        result = subprocess.run(["bash", str(ROOT / "withdraw-desk.sh")], env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(units.iterdir()), [])
        self.assertEqual(before, self.pueue.status())

    def test_managed_daemon_with_live_tasks_is_not_stopped(self):
        (self.root / "managed").touch()
        (self.root / "manual").touch()  # pgrep también ve al daemon administrado.
        state = self.pueue.status()
        state["tasks"] = {"1": {"status": {"Running": {}}, "group": "hasito"}}
        self.write_state(state)
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(c == ["pueue", "shutdown"] for c in self.calls()))
        self.assertEqual(self.pueue.status()["tasks"], state["tasks"])

    def test_nonempty_suites_preserved(self):
        state = self.pueue.status()
        state["groups"]["suites"] = {"status": "Running", "parallel_tasks": 2}
        state["tasks"] = {"1": {"status": {"Done": "Success"}, "group": "suites"}}
        self.write_state(state)
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("suites tiene tareas; se conserva", result.stdout)
        self.assertIn("suites", self.pueue.status()["groups"])


if __name__ == "__main__":
    unittest.main()
