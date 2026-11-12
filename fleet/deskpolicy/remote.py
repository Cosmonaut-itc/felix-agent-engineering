"""Retiro remoto: conserva todo el estado y los recibos históricos."""
import os
from pathlib import Path
import pwd
import signal
import subprocess
import tempfile
import time


def systemctl(scope, *args, check=True):
    prefix = ["systemctl", "--user"] if scope == "user" else ["systemctl"]
    if scope == "system" and args[0] in ("stop", "disable"):
        prefix = ["sudo", "-n", *prefix]
    return subprocess.run([*prefix, *args], text=True, capture_output=True, check=check)


def policy_units(scope):
    names = set()
    for command in ("list-unit-files", "list-units"):
        listing = systemctl(scope, command, "--all", "--no-legend", "--no-pager", "--plain")
        for row in listing.stdout.splitlines():
            name = row.split()[0]
            if name.endswith((".service", ".timer", ".path", ".socket", ".target")):
                names.add(name)
    selected = set()
    for name in names:
        content = systemctl(scope, "cat", name, check=False).stdout
        if not content:
            # Las unidades transitorias pueden no tener archivo que leer.
            content = systemctl(scope, "show", name, "--property=ExecStart,Description").stdout
        if "deskpolicy" in (name + "\n" + content).lower():
            selected.add(name)
    # Un timer con nombre genérico puede activar un writer identificado por su
    # ExecStart. Incluye todos sus activadores, también por dependencia implícita.
    inspected = set()
    while selected - inspected:
        name = sorted(selected - inspected)[0]
        inspected.add(name)
        links = systemctl(scope, "show", name, "--property=Triggers,TriggeredBy").stdout
        for row in links.splitlines():
            selected.update(row.partition("=")[2].split())
    return sorted(selected, key=lambda name: (name.endswith(".service"), name))


def stop_units(scope, units):
    # Primero los activadores, después el writer y los guardias.
    for name in units:
        systemctl(scope, "stop", name)
        enabled = systemctl(scope, "is-enabled", name, check=False).stdout.strip()
        if enabled in ("enabled", "enabled-runtime", "linked", "linked-runtime", "alias"):
            flags = ["--runtime"] if enabled.endswith("-runtime") else []
            systemctl(scope, "disable", name, *flags)
    for name in units:
        active = systemctl(scope, "is-active", name, check=False).stdout.strip()
        enabled = systemctl(scope, "is-enabled", name, check=False).stdout.strip()
        if active not in ("inactive", "failed", "unknown") or enabled not in (
            "disabled", "static", "masked", "masked-runtime", "indirect", "not-found"
        ):
            raise RuntimeError(f"No se acreditó el retiro de {scope}/{name}: {active}, {enabled}")


def processes(proc=Path("/proc")):
    result = {}
    for directory in proc.iterdir():
        if not directory.name.isdigit():
            continue
        try:
            stat = (directory / "stat").read_text().rsplit(")", 1)[1].split()
            argv = (directory / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            result[int(directory.name)] = (int(stat[1]), stat[19], stat[0], argv)
        except (FileNotFoundError, ProcessLookupError):
            continue
    return result


def stop_processes():
    snapshot = processes()
    selected = {pid for pid, row in snapshot.items()
                if "deskpolicy" in row[3].lower() and pid != os.getpid()}
    # Incluye hijos del writer aunque ya no tengan el nombre de la CLI en argv.
    while True:
        children = {pid for pid, row in snapshot.items() if row[0] in selected}
        if children <= selected:
            break
        selected |= children
    for sig in (signal.SIGTERM, signal.SIGKILL):
        current = processes()
        for pid in selected:
            if pid in current and current[pid][1] == snapshot[pid][1] and current[pid][2] != "Z":
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
        if selected:
            time.sleep(0.2)
    current = processes()
    if any(pid in current and current[pid][1] == snapshot[pid][1] and current[pid][2] != "Z"
           for pid in selected):
        raise RuntimeError("Quedan procesos de deskpolicy vivos")


def install(path, payload):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.resolve() != path.parent:
        raise RuntimeError(f"Directorio de instalación enlazado: {path.parent}")
    fd, stage = tempfile.mkstemp(prefix=".deskpolicy-retirado-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fchmod(stream.fileno(), 0o700)
            os.fsync(stream.fileno())
        os.replace(stage, path)
    finally:
        if os.path.exists(stage):
            os.unlink(stage)


def retire(payload):
    os.umask(0o077)
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    units = {scope: policy_units(scope) for scope in ("user", "system")}
    for scope, names in units.items():
        stop_units(scope, names)
    stop_processes()
    destinations = [home / ".local/lib/pao-deskpolicy/v1/deskpolicy/cli.py",
                    home / ".local/bin/pao-deskpolicy"]
    for path in destinations:
        install(path, payload)
        for arguments in (["estado"], ["publicar", "--help"], ["ejecutar", "--", "true"]):
            result = subprocess.run(["/usr/bin/python3", "-I", str(path), *arguments],
                                    capture_output=True)
            expected = ("deskpolicy retirado el 2026-11-12: el desk toma turno con Pueue. "
                        "Actualiza tu rama a origin/main y usa el lanzador del repositorio.\n").encode()
            if result.returncode != 64 or result.stdout or result.stderr != expected:
                raise RuntimeError(f"Respuesta de retiro incorrecta: {path}")
    for scope, names in units.items():
        stop_units(scope, names)
    if any("deskpolicy" in row[3].lower() and row[2] != "Z" and pid != os.getpid()
           for pid, row in processes().items()):
        raise RuntimeError("Se reinició un proceso de deskpolicy durante el retiro")
    print("deskpolicy retirado: unidades detenidas y deshabilitadas; CLI verificada (64). "
          "pao-wait/ y recibos conservados.")
