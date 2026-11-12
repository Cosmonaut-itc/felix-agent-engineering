"""Contrato de la Mini, siempre contra dobles (también al ejecutar en macOS)."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import unittest
from unittest.mock import patch

from test_pueue_desk import Fixture, ROOT, control

spec = importlib.util.spec_from_file_location("install_mini", ROOT / "install-mini.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class MiniTests(Fixture):
    def setUp(self):
        super().setUp()
        self.reset_state()
        self.load = 0.0
        self.cores = 10
        self.level = "1"
        self.reader_error = None
        original_run = subprocess.run

        def run(args, **kwargs):
            if args[0] == "/usr/sbin/sysctl":
                self.assertEqual(args, ["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"])
                self.assertTrue(kwargs["check"])
                if self.reader_error:
                    raise self.reader_error
                return subprocess.CompletedProcess(args, 0, self.level + "\n", "")
            return original_run(args, **kwargs)

        for mocked in (patch.object(control.os, "getloadavg", side_effect=lambda: (self.load, 0, 0)),
                       patch.object(control.os, "cpu_count", side_effect=lambda: self.cores),
                       patch.object(control.subprocess, "run", side_effect=run)):
            mocked.start()
            self.addCleanup(mocked.stop)

    def reset_state(self):
        self.write_state({"groups": {g: {"status": "Running", "parallel_tasks": n}
                                     for g, n in (("suites", 3), ("default", 1), ("ajeno", 7))},
                          "tasks": {"10": {"status": {"Running": {}}, "group": "suites"}}})

    def guard(self):
        with control.locked(self.directory):
            control.guard(self.pueue, None, self.directory, host="mini")

    def owned(self):
        target = self.directory / "guardia-state.json"
        return set(json.loads(target.read_text())) if target.exists() else set()

    def changes(self):
        return [c for c in self.calls() if c[1] != "status"]

    def test_load_hysteresis_and_admission_only(self):
        before = self.pueue.status()
        for load, expected, owned in ((20, "mantener", set()),
                                      (21, "pausar", {"suites", "default"}),
                                      (15, "mantener", {"suites", "default"}),
                                      (12, "mantener", {"suites", "default"}),
                                      (11.9, "reanudar", set())):
            self.load = load
            self.guard()
            records = [json.loads(r) for r in (self.directory / "guardia.log").read_text().splitlines()]
            self.assertEqual([r["accion"] for r in records if r["evento"] == "decision"][-1], expected)
            self.assertEqual(self.owned(), owned)
        self.assertEqual(self.pueue.status(), before)
        self.assertEqual(self.changes(), [["pueue", "pause", "--wait", "-g", g] for g in control.MINI_GROUPS]
                         + [["pueue", "start", "-g", g] for g in control.MINI_GROUPS])

    def test_memory_warning_and_critical_pause(self):
        for level in ("2", "4"):
            self.level = level
            self.guard()
            self.assertEqual(self.owned(), {"suites", "default"})
            self.level = "1"
            self.guard()
            self.assertEqual(self.owned(), set())
            self.assertTrue(all(self.pueue.status()["groups"][g]["status"] == "Running"
                                for g in control.MINI_GROUPS))

    def test_respects_manual_pause(self):
        self.pueue.call("pause", "--wait", "-g", "default")
        self.load = 21
        self.guard()
        self.assertEqual(self.owned(), {"suites"})
        self.load = 0
        self.guard()
        self.assertEqual(self.pueue.status()["groups"]["default"]["status"], "Paused")
        self.assertNotIn(["pueue", "start", "-g", "default"], self.changes())

    def test_manual_takeover_and_cli_profile(self):
        self.load = 21
        self.guard()
        home_state = self.root / ".local/state/pao/pueue-mini"
        home_state.mkdir(parents=True)
        control.save_owned(home_state, self.owned())
        with patch.object(control, "Pueue", return_value=self.pueue) as factory, \
                patch.object(control, "locked", wraps=control.locked) as lock:
            with patch.object(sys, "argv", ["control.py", "pausar", "suites", "--host", "mini"]):
                control.main()
            factory.assert_called_once_with("/opt/homebrew/bin/pueue")
            lock.assert_called_once_with(home_state)
        self.assertEqual(json.loads((home_state / "guardia-state.json").read_text()), ["default"])
        self.load = 0
        with patch.object(control, "Pueue", return_value=self.pueue):
            with patch.object(sys, "argv", ["control.py", "guardia", "--host", "mini"]):
                control.main()
        self.assertEqual(self.pueue.status()["groups"]["suites"]["status"], "Paused")
        self.assertEqual(self.pueue.status()["groups"]["default"]["status"], "Running")
        self.assertFalse((self.root / ".local/state/pao/pueue-desk").exists())

    def test_external_start_drops_ownership(self):
        self.load = 21
        self.guard()
        self.pueue.call("start", "-g", "suites")
        self.load = 15
        self.guard()
        self.assertEqual(self.owned(), {"default"})
        self.pueue.call("pause", "--wait", "-g", "suites")
        self.load = 0
        self.guard()
        self.assertEqual(self.pueue.status()["groups"]["suites"]["status"], "Paused")

    def test_failed_resume_keeps_ownership(self):
        self.load = 21
        self.guard()
        (self.root / "start-falla").touch()
        self.load = 0
        with self.assertRaises(subprocess.CalledProcessError):
            self.guard()
        self.assertEqual(self.owned(), {"suites", "default"})
        self.guard()
        self.assertEqual(self.owned(), set())
        self.assertEqual({self.pueue.status()["groups"][g]["status"] for g in control.MINI_GROUPS}, {"Running"})

    def test_invalid_readings_do_not_decide_or_change_ownership(self):
        # Verifica tanto el riesgo de pausar como el de reanudar.
        invalid = [(float("nan"), 10, "1"), (float("inf"), 10, "1"), (-1, 10, "1"),
                   (0, None, "1"), (0, 0, "1"), (0, -1, "1"), (0, True, "1"),
                   (0, 1.5, "1"), (0, 10, "0"), (0, 10, "3"), (0, 10, "8"),
                   (0, 10, ""), (0, 10, "nan"), (0, 10, "1.5")]
        for owned in (set(), {"suites", "default"}):
            control.save_owned(self.directory, owned)
            state = self.pueue.status()
            for g in control.MINI_GROUPS:
                state["groups"][g]["status"] = "Paused" if owned else "Running"
            self.write_state(state)
            for self.load, self.cores, self.level in invalid:
                before = self.calls()
                with self.subTest(load=self.load, cores=self.cores, level=self.level, owned=owned):
                    with self.assertRaises((ValueError, TypeError)):
                        control.pressure_mini()
                    with self.assertRaises((ValueError, TypeError)):
                        self.guard()
                    self.assertEqual(self.owned(), owned)
                    self.assertEqual(self.calls(), before)
        for error in (OSError("sin sysctl"), subprocess.CalledProcessError(1, "sysctl")):
            self.load, self.cores, self.level = 0, 10, "1"
            self.reader_error = error
            with self.assertRaises(type(error)):
                self.guard()
        with patch.object(control.os, "getloadavg", side_effect=OSError("sin carga")):
            with self.assertRaises(OSError):
                self.guard()
        self.assertFalse((self.directory / "guardia.log").exists())

    def test_cli_invalid_reading_logs_error_and_exits_nonzero(self):
        harness = '''
import importlib.util, subprocess, sys
spec = importlib.util.spec_from_file_location("control", sys.argv[1])
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
c.os.getloadavg = lambda: (float("nan"), 0, 0)
c.os.cpu_count = lambda: 10
c.subprocess.run = lambda *a, **kw: subprocess.CompletedProcess(a, 0, "1\\n", "")
sys.argv = ["control.py", "guardia", "--host", "mini"]
c.main()
'''
        result = subprocess.run(["/usr/bin/python3", "-I", "-c", harness, str(ROOT / "control.py")],
                                env=self.env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        directory = self.root / ".local/state/pao/pueue-mini"
        records = [json.loads(r) for r in (directory / "guardia.log").read_text().splitlines()]
        self.assertEqual([r["evento"] for r in records], ["error"])
        self.assertIn("inválida", records[0]["detalle"])
        self.assertEqual(self.changes(), [])

    def test_mini_cannot_apply_quotas(self):
        with patch.object(sys, "argv", ["control.py", "aplicar", "--host", "mini"]), \
                patch.object(control, "Pueue", return_value=self.pueue):
            with self.assertRaises(SystemExit) as error:
                control.main()
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(self.calls(), [])


class InstallerTests(Fixture):
    def setUp(self):
        super().setUp()
        self.service_loaded = False
        self.brew_loaded = True
        self.pueue_available = True
        self.actions = []
        self.write_state({"groups": {"suites": {}, "default": {}}, "tasks": {}})
        mocked = patch.object(installer.subprocess, "run", side_effect=self.fake_run)
        mocked.start()
        self.addCleanup(mocked.stop)

    def fake_run(self, argv, check, **kwargs):
        self.actions.append(argv)
        code, output = 0, ""
        if argv[0] == installer.LAUNCHCTL:
            verb = argv[1]
            if verb == "print":
                loaded = self.brew_loaded if argv[2].endswith("/sh.brew.pueue") else self.service_loaded
                code = 0 if loaded else 113
            elif verb == "bootstrap":
                self.assertFalse(self.service_loaded)
                self.service_loaded = True
            elif verb == "bootout":
                self.assertTrue(argv[2].endswith("/" + installer.LABEL))
                self.service_loaded = False
            else:
                self.fail(f"launchctl inesperado: {argv}")
        elif argv[:3] == [installer.PUEUE, "start", "-g"]:
            self.assertFalse(self.service_loaded)
        elif argv == [installer.PUEUE, "status", "--json"]:
            code = 0 if self.pueue_available else 1
            output = (self.root / "pueue.json").read_text()
        else:
            self.fail(f"Acción inesperada: {argv}")
        if check and code:
            raise subprocess.CalledProcessError(code, argv)
        return subprocess.CompletedProcess(argv, code, output, "")

    def install(self, *args):
        with patch.object(sys, "argv", ["install-mini.py", *args]):
            installer.main()

    def test_dry_run_has_no_actions(self):
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))
        for command in ("instalar", "desinstalar"):
            output = io.StringIO()
            with redirect_stdout(output):
                self.install(command, "--seco")
            print(f"\nSALIDA MINI {command} --seco (doble):\n{output.getvalue()}")
            self.assertIn("SECO:", output.getvalue())
            self.assertIn("bootout", output.getvalue())
            self.assertIn("bootstrap" if command == "instalar" else "conservar", output.getvalue())
            self.assertEqual(self.actions, [])
            self.assertEqual(sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")), before)

    def test_missing_brew_or_unresponsive_pueue_aborts_before_changes(self):
        for brew, pueue in ((False, True), (True, False)):
            self.brew_loaded, self.pueue_available = brew, pueue
            self.actions.clear()
            with self.assertRaises(subprocess.CalledProcessError):
                self.install("instalar")
            self.assertFalse((self.root / "Library").exists())
            self.assertFalse((self.root / ".local/lib").exists())
            self.assertTrue(all(a[1] in ("print", "status") for a in self.actions))

    def test_install_twice_plist_and_withdraw_twice_resumes_owned_groups(self):
        library = self.root / ".local/lib/pao-pueue-mini"
        state = self.root / ".local/state/pao/pueue-mini"
        agent = self.root / "Library/LaunchAgents" / (installer.LABEL + ".plist")
        for _ in range(2):
            self.install("instalar")
            self.assertTrue(self.service_loaded)
            self.assertEqual((library / "control.py").read_bytes(), (ROOT / "control.py").read_bytes())
            data = plistlib.loads(agent.read_bytes())
            self.assertEqual(data["Label"], installer.LABEL)
            self.assertEqual(data["StartInterval"], 60)
            self.assertNotIn("KeepAlive", data)
            self.assertEqual(data["ProgramArguments"], ["/usr/bin/python3", "-I", str(library / "control.py"),
                                                       "guardia", "--host", "mini"])
            for key in ("StandardOutPath", "StandardErrorPath"):
                self.assertTrue(data[key].startswith(str(state) + "/"))
        (state / "guardia-state.json").write_text('["suites"]\n')
        (state / "guardia.log").write_text("registro conservado\n")
        for _ in range(2):
            self.install("desinstalar")
            self.assertFalse(self.service_loaded)
            self.assertFalse(agent.exists())
            self.assertFalse(library.exists())
            self.assertEqual(json.loads((state / "guardia-state.json").read_text()), [])
            self.assertEqual((state / "guardia.log").read_text(), "registro conservado\n")
        self.assertEqual([a[1:] for a in self.actions if a[0] == installer.PUEUE],
                         [["status", "--json"], ["status", "--json"], ["start", "-g", "suites"]])
        self.assertEqual(len([a for a in self.actions if a[1] == "bootstrap"]), 2)
        self.assertEqual(len([a for a in self.actions if a[1] == "bootout"]), 2)

    def test_invalid_status_aborts_before_changes(self):
        for value in ('{"groups": {"suites": {}}, "tasks": {}}',
                      '{"groups": {"default": {}}, "tasks": {}}',
                      '{"groups": {"suites": {}, "default": {}}, "tasks": []}', "no es JSON"):
            (self.root / "pueue.json").write_text(value)
            with self.assertRaises(ValueError):
                self.install("instalar")
            self.assertFalse((self.root / "Library").exists())
            self.assertFalse((self.root / ".local/lib").exists())
            self.assertFalse(self.service_loaded)


if __name__ == "__main__":
    unittest.main()
