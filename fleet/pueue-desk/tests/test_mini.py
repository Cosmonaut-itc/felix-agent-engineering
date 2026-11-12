"""W6: instalar y retirar en la Mini sobreviven a SIGKILL en cada frontera.

launchctl, pueue, pueued y brew son dobles que comparten world.json. Cada llamada externa
del instalador cuenta; FAKE_KILL=before:N|after:N manda SIGKILL real al instalador antes o
después del efecto de la llamada N. Después corre una sola pasada de `mini.py vigilar`, lo
que hace io.pao.pueue-guardia cada 30 s, y el host debe quedar sano.
"""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("control", ROOT / "control.py")
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)
OWN, GUARD, BREW = "io.pao.pueue", "io.pao.pueue-guardia", "sh.brew.pueue"
# /usr/bin/python3 es un intermediario de xcrun; el intérprete real con -IS arranca varias veces más rápido.
PYTHON = sys.executable
# Holgados a propósito: el camino sano sale en cuanto el host queda listo, y un host cargado
# no debe convertir una espera en un falso fallo.
WAIT_S, CALL_TIMEOUT_S, WORKERS = "30", 300, 4

FAKE = r'''#!@PYTHON@ -IS
import json, os, signal, sys
from pathlib import Path
OWN, BREW = "io.pao.pueue", "sh.brew.pueue"
name = Path(sys.argv[0]).name
args = sys.argv[1:]
path = Path(os.environ["FAKE_WORLD"])
world = json.loads(path.read_text())
world["calls"] += 1
kill = os.environ.get("FAKE_KILL", "")
when, _, number = kill.partition(":")
hit = number and int(number) == world["calls"]
def save():
    path.write_text(json.dumps(world))
def die():
    save()
    os.kill(os.getppid(), signal.SIGKILL)
    sys.exit(1)
if hit and when == "before":
    die()
# KeepAlive: launchd reintenta el preparar de io.pao.pueue, que arranca si no responde otro daemon.
if OWN in world["loaded"] and world["alive"] is None:
    world["alive"] = OWN
code, out = 0, ""
if name == "launchctl":
    verb = args[0]
    if verb == "bootstrap":
        import plistlib
        label = plistlib.loads(Path(args[2]).read_bytes())["Label"]
    else:
        label = args[1].rsplit("/", 1)[-1]
    if verb == "print":
        code = 0 if label in world["loaded"] else 113
    elif verb == "bootstrap":
        if label in world["loaded"]:
            code = 5
        elif label in world["disabled"]:
            code = 119
        else:
            world["loaded"].append(label)
            # Si ya responde otro daemon, preparar rechaza (io.pao.pueue) o el pid de Pueue lo impide (brew).
            if label in (OWN, BREW) and world["alive"] is None:
                world["alive"] = label
    elif verb == "bootout":
        if label not in world["loaded"]:
            code = 3
        else:
            world["loaded"].remove(label)
            if world["alive"] == label:
                world["alive"] = None
    elif verb in ("enable", "disable"):
        world["disabled"] = sorted(set(world["disabled"]) - {label} | ({label} if verb == "disable" else set()))
    elif verb == "kickstart":
        if label in world["loaded"] and label in (OWN, BREW) and world["alive"] is None:
            world["alive"] = label
    else:
        code = 64
elif name == "pueue":
    if world["alive"] is None:
        code, out = 2, "Error: connection refused"
    elif args == ["status", "--json"]:
        out = json.dumps({"tasks": world["tasks"], "groups": world["groups"]})
    elif args[0] in ("pause", "start"):
        world["groups"][args[-1]]["status"] = "Paused" if args[0] == "pause" else "Running"
    else:
        code = 64
elif name == "pueued":
    out = "pueued 4.0.4"
elif name == "brew":
    out = "git\npueue"
save()
if out:
    print(out, file=sys.stderr if code else sys.stdout)
if hit and when == "after":
    die()
sys.exit(code)
'''


def queued_task():
    return {"id": 1, "created_at": "2026-11-12T01:00:00-06:00", "original_command": "true", "command": "true",
            "path": "/tmp", "envs": {}, "group": "suites", "dependencies": [], "priority": 0, "label": None,
            "status": {"Queued": {"enqueued_at": "2026-11-12T01:00:01-06:00"}}}


class World(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        for name in ("launchctl", "pueue", "pueued", "brew"):
            (self.bin / name).write_text(FAKE.replace("@PYTHON@", PYTHON))
            (self.bin / name).chmod(0o755)

    def fresh(self, name, installed=False):
        home = self.base / name
        agents = home / "Library/LaunchAgents"
        agents.mkdir(parents=True)
        (agents / f"{BREW}.plist").write_bytes(plistlib.dumps({"Label": BREW}))
        data = home / "Library/Application Support/pueue"
        data.mkdir(parents=True)
        tasks = {"1": queued_task()}
        groups = {"default": {"status": "Running", "parallel_tasks": 1},
                  "suites": {"status": "Running", "parallel_tasks": 3}}
        (data / "state.json").write_text(json.dumps({"tasks": tasks, "groups": groups}))
        world = {"calls": 0, "loaded": [BREW], "disabled": [], "alive": BREW, "tasks": tasks, "groups": groups}
        (home / "world.json").write_text(json.dumps(world))
        if installed:
            result = self.mini(home, "install")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_healthy(home, "propio")
        return home

    def env(self, home, kill=None):
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "FAKE_WORLD": str(home / "world.json"),
               "PAO_LAUNCHCTL": str(self.bin / "launchctl"), "PAO_PUEUE_BIN_DIR": str(self.bin),
               "PAO_BREW_BIN": str(self.bin / "brew"), "PAO_MINI_ESPERA_S": WAIT_S}
        if kill:
            env["FAKE_KILL"] = kill
        return env

    def mini(self, home, action, kill=None):
        args = [action] if action == "vigilar" else [action, "--esperar", "0"]
        return subprocess.run([PYTHON, "-I", str(ROOT / "mini.py"), *args],
                              env=self.env(home, kill), text=True, capture_output=True, timeout=CALL_TIMEOUT_S)

    def world(self, home):
        return json.loads((home / "world.json").read_text())

    def assert_healthy(self, home, expected=None):
        world = self.world(home)
        agents = home / "Library/LaunchAgents"
        self.assertIn(world["alive"], (OWN, BREW), "ningún daemon responde")
        owner = "propio" if world["alive"] == OWN else "brew"
        if expected:
            self.assertEqual(owner, expected)
        if owner == "propio":
            self.assertNotIn(BREW, world["loaded"])
            self.assertIn(BREW, world["disabled"])
            self.assertTrue((agents / f"{OWN}.plist").exists())
            self.assertIn(GUARD, world["loaded"])
        else:
            self.assertNotIn(OWN, world["loaded"])
            # Sin plist en LaunchAgents, el próximo login no carga un segundo daemon.
            self.assertFalse((agents / f"{OWN}.plist").exists())
            self.assertNotIn(BREW, world["disabled"])
        self.assertEqual({g: v["status"] for g, v in world["groups"].items()},
                         {"default": "Running", "suites": "Running"})
        state = control.load_control(home / ".local/state/pao/pueue-desk")
        self.assertEqual((state["owned"], state["relevo"]), ([], None))
        self.assertEqual(list(world["tasks"]), ["1"])
        return owner

    def boundaries(self, action):
        home = self.fresh("referencia-" + action, installed=action == "withdraw")
        start = self.world(home)["calls"]
        result = self.mini(home, action)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_healthy(home, "propio" if action == "install" else "brew")
        return self.world(home)["calls"] - start

    def boundary(self, action, when, number):
        """Una frontera completa; corre en un hilo y sus aserciones se informan en el principal."""
        home = self.fresh(f"{action}-{when}-{number}", installed=action == "withdraw")
        offset = self.world(home)["calls"]
        result = self.mini(home, action, kill=f"{when}:{offset + number}")
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
        # Una sola pasada del vigilante, si launchd lo tiene cargado.
        if GUARD in self.world(home)["loaded"]:
            tick = self.mini(home, "vigilar")
            self.assertEqual(tick.returncode, 0, (home / ".local/state/pao/pueue-desk/guardia.log").read_text())
        owner = self.assert_healthy(home)
        if action == "withdraw" and owner == "brew":
            self.assertNotIn(GUARD, self.world(home)["loaded"])
            self.assertFalse((home / f"Library/LaunchAgents/{GUARD}.plist").exists())
        return owner

    def check_every_boundary(self, action):
        total = self.boundaries(action)
        self.assertGreater(total, 10)
        cases = [(when, number) for number in range(1, total + 1) for when in ("before", "after")]

        def run(case):
            try:
                return self.boundary(action, *case), None
            except Exception as error:  # se vuelve a lanzar dentro de su subTest
                return None, error

        with ThreadPoolExecutor(WORKERS) as pool:
            results = list(pool.map(run, cases))
        outcomes = set()
        for (when, number), (owner, error) in zip(cases, results):
            with self.subTest(action=action, kill=f"{when}:{number}"):
                if error:
                    raise error
                outcomes.add(owner)
        print(f"\nW6 {action}: SIGKILL en {total} llamadas × antes/después; destino "
              f"{sorted(outcomes)}; sano tras una pasada de vigilar")

    def test_install_survives_sigkill_at_every_boundary(self):
        self.check_every_boundary("install")

    def test_withdraw_survives_sigkill_at_every_boundary(self):
        self.check_every_boundary("withdraw")

    def test_guard_interval_keeps_repair_under_a_minute(self):
        guard = plistlib.loads((ROOT / f"launchagents/{GUARD}.plist").read_bytes())
        own = plistlib.loads((ROOT / f"launchagents/{OWN}.plist").read_bytes())
        # Una pasada basta (pruebas anteriores): StartInterval + PAO_MINI_ESPERA_S por defecto < 60 s.
        self.assertLessEqual(guard["StartInterval"] + 20, 60)
        self.assertTrue(guard["RunAtLoad"])
        self.assertTrue(own["KeepAlive"] and own["RunAtLoad"])
        self.assertEqual(own["ProgramArguments"][-4:], ["preparar", "--host", "mini", "--exec"])

    def test_running_task_aborts_install_without_touching_the_daemon(self):
        home = self.fresh("vivo")
        world = self.world(home)
        world["tasks"]["2"] = {**queued_task(), "id": 2, "status": {"Running": {
            "enqueued_at": "2026-11-12T01:00:01-06:00", "start": "2026-11-12T01:00:02-06:00"}}}
        (home / "world.json").write_text(json.dumps(world))
        # El estado en disco coincide con el daemon, para que el aborto venga de la tarea viva.
        (home / "Library/Application Support/pueue/state.json").write_text(
            json.dumps({"tasks": world["tasks"], "groups": world["groups"]}))
        result = self.mini(home, "install")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Abortado sin tocar el daemon", result.stderr)
        world = self.world(home)
        self.assertEqual((world["alive"], world["disabled"]), (BREW, []))
        self.assertEqual(world["groups"]["suites"]["status"], "Running")

    def test_maintenance_stop_unloads_watcher_first(self):
        home = self.fresh("parada", installed=True)
        start = self.world(home)["calls"]
        trace = home / "trace"
        env = self.env(home)
        wrapper = self.base / "bin-trace"
        wrapper.mkdir(exist_ok=True)
        (wrapper / "launchctl").write_text(f"#!/bin/sh\necho \"$@\" >> {trace}\nexec {self.bin / 'launchctl'} \"$@\"\n")
        (wrapper / "launchctl").chmod(0o755)
        env["PAO_LAUNCHCTL"] = str(wrapper / "launchctl")
        result = subprocess.run([PYTHON, "-I", str(ROOT / "mini.py"), "parar"],
                                env=env, text=True, capture_output=True, timeout=CALL_TIMEOUT_S)
        self.assertEqual(result.returncode, 0, result.stderr)
        bootouts = [line.split()[1].rsplit("/", 1)[-1] for line in trace.read_text().splitlines()
                    if line.startswith("bootout")]
        self.assertEqual(bootouts, [GUARD, OWN])
        world = self.world(home)
        self.assertEqual((world["alive"], world["loaded"]), (None, []))
        self.assertGreater(world["calls"], start)


if __name__ == "__main__":
    unittest.main()
