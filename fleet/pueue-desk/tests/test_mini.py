import copy
import importlib.util
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("mini", ROOT / "mini.py")
mini = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mini)


class MiniDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.pueue = Mock()
        self.queue = {"groups": {"suites": {"parallel_tasks": 3, "status": "Running"}}, "tasks": {}}
        self.pueue.status.side_effect = lambda: copy.deepcopy(self.queue)
        self.pueue.call.side_effect = self.pueue_call
        self.data = self.home / "Library/Application Support/pueue"
        self.data.mkdir(parents=True)
        (self.data / "state.json").write_text(json.dumps(self.queue))
        self.calls = []
        self.disabled = False
        self.loaded = {mini.BREW_LABEL}
        self.domain = f"gui/{mini.os.getuid()}"
        self.launcher = patch.object(mini, "launch", side_effect=self.launch)
        self.launcher.start()
        self.addCleanup(self.launcher.stop)

    def pueue_call(self, *args):
        if args[0] != "pause":
            raise AssertionError(args)
        self.queue["groups"][args[-1]]["status"] = "Paused"
        (self.data / "state.json").write_text(json.dumps(self.queue))

    def launch(self, *args, check=True):
        self.calls.append(args)
        code, output = 0, ""
        if args[0] == "print":
            code = 0 if args[-1].split("/")[-1] in self.loaded else 113
        elif args[0] == "print-disabled":
            output = '"sh.brew.pueue" => ' + str(self.disabled).lower()
        elif args[0] == "bootout":
            self.loaded.discard(args[-1].split("/")[-1])
        elif args[0] == "bootstrap":
            self.loaded.add(Path(args[-1]).stem)
        elif args[0] in ("enable", "disable"):
            pass
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, code, output, "")

    def test_dry_install_and_withdraw_do_not_write_or_mutate_launchctl(self):
        before = {str(p.relative_to(self.home)): p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        mini.change("install", True, self.home, self.pueue)
        mini.change("withdraw", True, self.home, self.pueue)
        after = {str(p.relative_to(self.home)): p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertTrue(all(c[0] in ("print", "print-disabled") for c in self.calls))

    def test_live_queue_aborts_before_any_launchctl(self):
        self.queue["tasks"] = {"0": {"status": {"Queued": {"enqueued_at": "ahora"}}}}
        with self.assertRaisesRegex(RuntimeError, "termine la cola"):
            mini.change("install", True, self.home, self.pueue)
        self.assertEqual(self.calls, [])

    def test_wrong_data_directory_aborts_without_replacing_agent(self):
        (self.data / "state.json").unlink()
        with self.assertRaisesRegex(RuntimeError, "rutas estándar"):
            mini.change("install", False, self.home, self.pueue)
        self.assertEqual(self.calls, [])

    def test_install_twice_and_withdraw_preserves_queue_and_restores_brew(self):
        agents = self.home / "Library/LaunchAgents"
        agents.mkdir()
        (agents / (mini.BREW_LABEL + ".plist")).write_text("original")
        for _ in range(2):
            mini.change("install", False, self.home, self.pueue)
        own = agents / (mini.LABEL + ".plist")
        rendered = plistlib.loads(own.read_bytes())
        self.assertTrue(rendered["KeepAlive"])
        self.assertTrue(rendered["RunAtLoad"])
        self.assertIn(str(self.home / ".local/lib/pao-pueue-desk/host.py"), rendered["ProgramArguments"])
        self.assertNotIn("@HOME@", own.read_text())
        before = (self.data / "state.json").read_bytes()
        mini.change("withdraw", False, self.home, self.pueue)
        mini.change("withdraw", False, self.home, self.pueue)
        self.assertFalse(own.exists())
        self.assertEqual((self.data / "state.json").read_bytes(), before)
        self.assertIn(mini.BREW_LABEL, self.loaded)
        self.assertNotIn(mini.LABEL, self.loaded)
        self.assertEqual((agents / (mini.BREW_LABEL + ".plist")).read_text(), "original")

    def test_withdraw_restores_prior_disabled_brew(self):
        self.disabled = True
        self.loaded.clear()
        mini.change("install", False, self.home, self.pueue)
        mini.change("withdraw", False, self.home, self.pueue)
        self.assertIn(("disable", self.domain + "/" + mini.BREW_LABEL), self.calls)
        self.assertNotIn(("enable", self.domain + "/" + mini.BREW_LABEL), self.calls)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.source = self.home / "Library/Application Support/pueue/pueue.yml"
        self.source.parent.mkdir(parents=True)
        self.library = self.home / "biblioteca-administrada"

    def test_preserves_yaml_daemon_options_and_client(self):
        self.source.write_text('shared:\n  pueue_directory: null\n  use_unix_socket: true\ndaemon:\n  compress_state_file: false\n  callback: "echo terminado"\n  env_vars:\n    PRUEBA: original\nclient:\n  dark_mode: true\n')
        settings, text = mini.control.managed_configuration(self.home, "mini", self.library)
        self.assertIn('callback: "echo terminado"', text)
        self.assertIn('PRUEBA: original', text)
        self.assertIn('dark_mode: true', text)
        shared = json.loads(text.splitlines()[0].split(":", 1)[1])
        self.assertEqual(shared, settings["shared"])
        self.assertEqual(shared["pueue_directory"], str(self.source.parent))

    def test_preserves_json_daemon_settings(self):
        self.source.write_text(json.dumps({"shared": {}, "daemon": {"env_vars": {"PRUEBA": "original"}}, "client": {"dark_mode": True}}))
        _, text = mini.control.managed_configuration(self.home, "mini", self.library)
        actual = json.loads(text)
        self.assertEqual(actual["daemon"]["env_vars"], {"PRUEBA": "original"})
        self.assertTrue(actual["client"]["dark_mode"])

    def test_rejects_compression_before_install(self):
        for source in ('shared:\n  port: "6924"\ndaemon:\n  compress_state_file: true\n',
                       '{"daemon":{"compress_state_file":true}}'):
            self.source.write_text(source)
            with self.assertRaisesRegex(ValueError, "compress_state_file"):
                mini.control.managed_configuration(self.home, "mini", self.library)
        self.assertFalse(self.library.exists())
