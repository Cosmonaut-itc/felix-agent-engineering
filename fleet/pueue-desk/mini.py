#!/usr/bin/python3 -I
"""Instalar/retirar el LaunchAgent en la propia Mini. No transporta por SSH."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("control", ROOT / "control.py")
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)
LABEL = "io.pao.pueue"
BREW_LABEL = "sh.brew.pueue"


def launch(*args, check=True):
    return subprocess.run(["launchctl", *args], check=check, text=True, capture_output=True)


def change(action, dry, home=None, pueue=None):
    home = home or Path.home()
    directory = home / ".local/state/pao/pueue-desk"
    library = home / ".local/lib/pao-pueue-desk"
    agents = home / "Library/LaunchAgents"
    target = agents / (LABEL + ".plist")
    brew = agents / (BREW_LABEL + ".plist")
    domain = f"gui/{os.getuid()}"
    if action == "install":
        pueue = pueue or control.Pueue("/opt/homebrew/bin/pueue")
        if control.live_tasks(pueue.status()):
            raise RuntimeError("Instalación abortada: esperar a que termine la cola antes de sustituir el LaunchAgent")
        settings, configuration = control.managed_configuration(home, "mini", library)
        queue_path = Path(settings["shared"]["pueue_directory"]) / "state.json"
        # Sólo se admite adopción de las rutas estándar, nunca una cola vacía por error.
        disk = control.read_queue(queue_path)
        if disk != pueue.status():
            raise RuntimeError("La cola no coincide con las rutas estándar; no se cambia el LaunchAgent")
        loaded = launch("print", domain + "/" + BREW_LABEL, check=False).returncode == 0
        disabled_output = launch("print-disabled", domain).stdout
        disabled = bool(re.search(r'"sh[.]brew[.]pueue"\s*=>\s*true', disabled_output))
        if dry:
            print("SECO: cola sin tareas vivas; conservar cola y configurar suites=3")
            print("SECO: copiar control.py, host.py, configuración y io.pao.pueue.plist")
            print("SECO: disable y bootout sh.brew.pueue; bootstrap io.pao.pueue (KeepAlive y RunAtLoad)")
            return
        directory.mkdir(parents=True, exist_ok=True)
        if not (directory / "launchagent-previo.json").exists():
            control.atomic_json(directory / "launchagent-previo.json", {"brew_loaded": loaded, "brew_disabled": disabled})
        library.mkdir(parents=True, exist_ok=True)
        for name in ("control.py", "host.py"):
            (library / name).write_text((ROOT / name).read_text())
        control.atomic_json(library / "pueue-paths.json", settings)
        control.atomic_text(library / "pueue.yml", configuration)
        agents.mkdir(parents=True, exist_ok=True)
        template = plistlib.loads((ROOT / "launchagents" / (LABEL + ".plist")).read_bytes())
        for key in ("StandardOutPath", "StandardErrorPath"):
            template[key] = template[key].replace("@HOME@", str(home))
        template["ProgramArguments"] = [v.replace("@HOME@", str(home)) for v in template["ProgramArguments"]]
        target.write_bytes(plistlib.dumps(template))
        launch("disable", domain + "/" + BREW_LABEL)
        if loaded:
            launch("bootout", domain + "/" + BREW_LABEL)
        # Una segunda instalación releva sólo el agente propio y con cola vacía.
        if launch("print", domain + "/" + LABEL, check=False).returncode == 0:
            launch("bootout", domain + "/" + LABEL)
        try:
            launch("enable", domain + "/" + LABEL)
            launch("bootstrap", domain, str(target))
        except subprocess.CalledProcessError:
            launch("disable" if disabled else "enable", domain + "/" + BREW_LABEL, check=False)
            if loaded and brew.exists():
                launch("bootstrap", domain, str(brew), check=False)
            print("Falló el arranque del agente propio; se intentó restaurar el LaunchAgent anterior. "
                  "Consultar launchctl print y el registro host.log antes de reintentar.", file=sys.stderr)
            raise
        print("LaunchAgent propio instalado; cola conservada, suites=3 y recuperación al arrancar")
    else:
        if dry:
            print("SECO: bootout io.pao.pueue y quitar su plist; conservar cola, biblioteca y estado")
            print("SECO: restaurar sh.brew.pueue sólo si este instalador lo había encontrado cargado")
            return
        if launch("print", domain + "/" + LABEL, check=False).returncode == 0:
            launch("bootout", domain + "/" + LABEL)
        target.unlink(missing_ok=True)
        saved = directory / "launchagent-previo.json"
        if saved.exists():
            previous = json.loads(saved.read_text())
            launch("disable" if previous["brew_disabled"] else "enable", domain + "/" + BREW_LABEL)
            if previous["brew_loaded"] and brew.exists():
                if launch("print", domain + "/" + BREW_LABEL, check=False).returncode != 0:
                    launch("bootstrap", domain, str(brew))
            saved.unlink()
        print("Retirado el agente propio; cola y estado conservados")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "withdraw"])
    parser.add_argument("--seco", action="store_true")
    args = parser.parse_args()
    change(args.action, args.seco)


if __name__ == "__main__":
    main()
