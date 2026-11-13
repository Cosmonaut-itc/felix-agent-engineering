#!/usr/bin/python3 -I
"""Instala o retira sólo la guardia local de la Mini; nunca administra pueued."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess

LABEL = "local.pao.pueue-mini.guard"
SOURCE = Path(__file__).resolve().parent
PUEUE = "/opt/homebrew/bin/pueue"
LAUNCHCTL = "/bin/launchctl"


def run(*args, check=True):
    return subprocess.run(list(args), check=check, text=True, capture_output=True)


def resume_owned(state):
    """Reanuda las pausas de la guardia: sin guardia cargada, nadie lo haría."""
    owned_file = state / "guardia-state.json"
    if not owned_file.exists():
        return
    with (state / "control.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        owned = json.loads(owned_file.read_text())
        if not set(owned) <= {"suites", "default", "rapidas"}:
            raise ValueError("Grupos desconocidos en estado del guardia")
        for group in sorted(owned):
            run(PUEUE, "start", "-g", group)
            remaining = sorted(set(json.loads(owned_file.read_text())) - {group})
            temporary = owned_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(remaining) + "\n")
            temporary.replace(owned_file)


def keep_owned_guarded(state, domain, agent, error):
    """Tras un fallo sin guardia cargada: reanuda sus grupos o la recarga; si no, lo dice."""
    try:
        resume_owned(state)
        return
    except Exception:
        pass
    try:
        # Recargada, la guardia conserva la propiedad y reanuda cuando baje la presión.
        run(LAUNCHCTL, "bootstrap", domain, str(agent))
        return
    except Exception:
        pass
    owned = json.loads((state / "guardia-state.json").read_text())
    commands = "; ".join(f"{PUEUE} start -g {group}" for group in owned)
    raise RuntimeError(f"sin guardia cargada y en pausa por ella: {', '.join(owned)}. "
                       f"Reanudar a mano: {commands}") from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["instalar", "desinstalar"])
    parser.add_argument("--seco", action="store_true")
    args = parser.parse_args()
    home = Path.home()
    library = home / ".local/lib/pao-pueue-mini"
    state = home / ".local/state/pao/pueue-mini"
    agent = home / "Library/LaunchAgents" / (LABEL + ".plist")
    # Equivalente a id -u, sin ejecutar comandos tampoco en --seco.
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{LABEL}"
    if args.seco:
        if args.command == "instalar":
            print(f"SECO: comprobar {LAUNCHCTL} print {domain}/sh.brew.pueue")
            print(f"SECO: comprobar {PUEUE} status --json (grupos suites y default)")
        print(f"SECO: si está cargado, {LAUNCHCTL} bootout {service}")
        if args.command == "instalar":
            print(f"SECO: copiar {SOURCE / 'control.py'} -> {library / 'control.py'}")
            print(f"SECO: crear {state} y renderizar plist -> {agent}")
            print("SECO: " + shlex.join([LAUNCHCTL, "bootstrap", domain, str(agent)]))
        else:
            print(f"SECO: {PUEUE} start -g <grupo> para cada grupo que pausó la guardia, y vaciar su propiedad")
            print(f"SECO: retirar {agent} y {library / 'control.py'}; conservar el log en {state}")
        return

    if args.command == "instalar":
        run(LAUNCHCTL, "print", f"{domain}/sh.brew.pueue")
        # Valida respuesta y grupos antes de escribir o descargar la guardia anterior.
        status = json.loads(run(PUEUE, "status", "--json").stdout)
        if not isinstance(status.get("tasks"), dict) or not {"suites", "default"} <= status["groups"].keys():
            raise ValueError("Pueue incompleto: se necesitan suites y default")
        template = plistlib.loads((SOURCE / "launchagents" / (LABEL + ".plist")).read_bytes())
        for key in ("ProgramArguments", "StandardOutPath", "StandardErrorPath"):
            value = template[key]
            template[key] = ([part.replace("@HOME@", str(home)) for part in value]
                             if isinstance(value, list) else value.replace("@HOME@", str(home)))
    if run(LAUNCHCTL, "print", service, check=False).returncode == 0:
        run(LAUNCHCTL, "bootout", service)
    if args.command == "instalar":
        try:
            library.mkdir(parents=True, exist_ok=True)
            state.mkdir(parents=True, exist_ok=True)
            agent.parent.mkdir(parents=True, exist_ok=True)
            # Copia y reemplazo atómico: un fallo a medias deja intacto el archivo anterior.
            for target, write in ((library / "control.py", lambda path: shutil.copy2(SOURCE / "control.py", path)),
                                  (agent, lambda path: path.write_bytes(plistlib.dumps(template)))):
                temporary = target.with_name(target.name + ".tmp")
                write(temporary)
                os.replace(temporary, target)
            run(LAUNCHCTL, "bootstrap", domain, str(agent))
        except Exception as error:
            keep_owned_guarded(state, domain, agent, error)
            raise
    else:
        try:
            resume_owned(state)
        except Exception as error:
            keep_owned_guarded(state, domain, agent, error)
            raise
        for target in (agent, library / "control.py"):
            target.unlink(missing_ok=True)
            target.with_name(target.name + ".tmp").unlink(missing_ok=True)
        if library.exists():
            library.rmdir()


if __name__ == "__main__":
    main()
