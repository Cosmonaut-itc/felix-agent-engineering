"""Retiro remoto: conserva todo el estado y los recibos históricos."""
import os
from pathlib import Path
import pwd
import signal
import subprocess
import sys
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
            argv = tuple(part.decode(errors="replace") for part in
                         (directory / "cmdline").read_bytes().split(b"\0") if part)
            result[int(directory.name)] = (int(stat[1]), stat[19], stat[0], argv)
        except (FileNotFoundError, ProcessLookupError):
            continue
    return result


def policy_command(argv):
    """Sólo la CLI instalada, como programa Python; nunca texto en otro argv."""
    if not argv:
        return None
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    paths = {str(home / ".local/lib/pao-deskpolicy/v1/deskpolicy/cli.py"),
             str(home / ".local/bin/pao-deskpolicy")}
    args = list(argv)
    if Path(args[0]).name in ("python", "python3", "python3.10", "python3.11",
                               "python3.12", "python3.13", "python3.14"):
        args.pop(0)
        while args and args[0] in ("-I", "-B", "-u", "-E", "-s", "-S"):
            args.pop(0)
    if len(args) >= 2 and args[0] in paths:
        return args[1]
    return None


def live_command(command):
    return {pid: row for pid, row in processes().items()
            if row[2] != "Z" and policy_command(row[3]) == command}


def drain(wait):
    deadline = time.monotonic() + wait
    while True:
        running = live_command("ejecutar")
        if not running:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(f"Retiro pendiente: quedan {len(running)} corridas ejecutar vivas. "
                  "Respuesta de retiro instalada; writer y unidades conservados. "
                  "Repite el retiro cuando terminen o usa --esperar N.", file=sys.stderr)
            return False
        time.sleep(min(0.2, remaining))


def stop_processes():
    snapshot = processes()
    selected = {pid for pid, row in snapshot.items()
                if policy_command(row[3]) == "publicar" and pid != os.getpid()}
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


def stage_install(path, payload):
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
        return stage
    except BaseException:
        if os.path.exists(stage):
            os.unlink(stage)
        raise


def install(path, payload):
    stage = stage_install(path, payload)
    try:
        os.replace(stage, path)
    finally:
        if os.path.exists(stage):
            os.unlink(stage)


def retire(payload, wait=0):
    os.umask(0o077)
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    units = {scope: policy_units(scope) for scope in ("user", "system")}
    # Fallar antes de cambiar la CLI o detener cualquier servicio.
    if units["system"]:
        subprocess.run(["sudo", "-n", "true"], check=True, capture_output=True)
    destinations = [home / ".local/lib/pao-deskpolicy/v1/deskpolicy/cli.py",
                    home / ".local/bin/pao-deskpolicy"]
    staged = []
    try:
        # Preparar ambos destinos detecta permisos y enlaces antes del reemplazo.
        for path in destinations:
            staged.append((path, stage_install(path, payload)))
        for path, stage in staged:
            os.replace(stage, path)
    finally:
        for _, stage in staged:
            if os.path.exists(stage):
                os.unlink(stage)
    for path in destinations:
        for arguments in (["estado"], ["publicar", "--help"], ["ejecutar", "--", "true"]):
            result = subprocess.run(["/usr/bin/python3", "-I", str(path), *arguments],
                                    capture_output=True)
            expected = ("deskpolicy retirado el 2026-11-12: el desk toma turno con Pueue. "
                        "Actualiza tu rama a origin/main y usa el lanzador del repositorio.\n").encode()
            if result.returncode != 64 or result.stdout or result.stderr != expected:
                raise RuntimeError(f"Respuesta de retiro incorrecta: {path}")
    if not drain(wait):
        return 2
    for scope, names in units.items():
        stop_units(scope, names)
    stop_processes()
    if live_command("publicar"):
        raise RuntimeError("Se reinició un proceso de deskpolicy durante el retiro")
    print("deskpolicy retirado: unidades detenidas y deshabilitadas; CLI verificada (64). "
          "pao-wait/ y recibos conservados.")
    return 0
