#!/usr/bin/python3 -I
"""Cuotas y guardia de Pueue 4.0.4; sólo biblioteca estándar."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import math
import os
import pwd
import re
import sys
from pathlib import Path
import subprocess
import time

GROUP_LIMITS = {
    "hasito": {"dia": 3, "noche": 4},
    "jale": {"dia": 2, "noche": 3},
    "otros": {"dia": 1, "noche": 1},
    "pg5434": {"dia": 1, "noche": 1},
}
PRESSURE_THRESHOLDS = {
    "pause": {"io": 40, "cpu": 30, "memory": 4 * 1024**3},
    "resume": {"io": 20, "cpu": 15, "memory": 6 * 1024**3},
    "docker_timeout": 3,
}
LOG_MAX_BYTES = 1024 * 1024
STATE_DIR = Path.home() / ".local/state/pao/pueue-desk"


def mode_at(now):
    if now.tzinfo is None:
        raise ValueError("La hora debe incluir zona horaria")
    return "noche" if 5 <= now.astimezone(timezone.utc).hour < 14 else "dia"


def validate_limit(value):
    if type(value) is not int or not 1 <= value <= 8:
        raise ValueError(f"parallel fuera de 1..8: {value!r}")
    return value


class Pueue:
    def __init__(self, binary=None, config=None):
        self.binary = str(binary or Path.home() / ".local/bin/pueue")
        self.config = ["--config", str(config)] if config else []

    def call(self, *args):
        return subprocess.run([self.binary, *self.config, *map(str, args)], check=True,
                              text=True, capture_output=True, timeout=10).stdout

    def status(self):
        state = json.loads(self.call("status", "--json"))
        if not isinstance(state.get("tasks"), dict) or not isinstance(state.get("groups"), dict):
            raise ValueError("Estado Pueue incompleto")
        return state

    def parallel(self, group, value):
        self.call("parallel", validate_limit(value), "-g", group)


def apply_mode(pueue, now, host="desk"):
    mode = mode_at(now)
    limits = {group: validate_limit(row[mode]) for group, row in limits_for(host).items()}
    state = pueue.status()
    for group, limit in limits.items():
        if group not in state["groups"]:
            pueue.call("group", "add", group, "--parallel", limit)
        pueue.parallel(group, limit)
    # default está reservado, cerrado por preparar; pausa sin matar tareas.
    pueue.call("pause", "--wait", "-g", "default")
    state = pueue.status()
    for group, limit in limits.items():
        if state["groups"][group]["parallel_tasks"] != limit:
            raise ValueError(f"Cuota incorrecta: {group}")
    print(f"Modo {mode}: " + ", ".join(f"{g}={n}" for g, n in limits.items()))


@contextmanager
def locked(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "control.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def limits_for(host):
    return GROUP_LIMITS if host == "desk" else {"suites": {"dia": 3, "noche": 3}}


def atomic_text(target, content):
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(target)
    sync_directory(target.parent)


def atomic_json(target, value):
    atomic_text(target, json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")


def sync_directory(directory):
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def load_control(directory):
    target = directory / "guardia-state.json"
    value = json.loads(target.read_text()) if target.exists() else []
    # Migración de U11, conservando la propiedad de las pausas.
    if isinstance(value, list):
        value = {"version": 2, "owned": value, "recovery": {}, "pending": {}}
    if value.get("version") != 2 or not isinstance(value.get("recovery"), dict):
        raise ValueError("Estado del control incompatible")
    return value


def save_control(directory, state):
    atomic_json(directory / "guardia-state.json", state)


def save_owned(directory, owned):
    state = load_control(directory)
    state["owned"] = sorted(owned)
    save_control(directory, state)


def log(directory, event, **fields):
    target = directory / "guardia.log"
    if target.exists() and target.stat().st_size >= LOG_MAX_BYTES:
        target.replace(directory / "guardia.log.1")
    record = {"utc": datetime.now(timezone.utc).isoformat(), "evento": event, **fields}
    with target.open("a") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")


def pressure(proc):
    def psi(name, kind):
        for line in (proc / "pressure" / name).read_text().splitlines():
            parts = line.split()
            if parts[0] == kind:
                value = float(dict(part.split("=") for part in parts[1:])["avg60"])
                if not math.isfinite(value) or not 0 <= value <= 100:
                    raise ValueError("PSI fuera de rango")
                return value
        raise ValueError(f"Falta {kind} en PSI {name}")
    memory = dict(line.split(":", 1) for line in (proc / "meminfo").read_text().splitlines())
    amount, unit = memory["MemAvailable"].split()
    if unit != "kB" or int(amount) < 0:
        raise ValueError("MemAvailable inválido")
    return {"io": psi("io", "full"), "cpu": psi("cpu", "some"), "memory": int(amount) * 1024}


def docker_ready(binary):
    try:
        result = subprocess.run([str(binary), "info"], timeout=PRESSURE_THRESHOLDS["docker_timeout"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def guard(pueue, proc, directory, docker="/usr/bin/docker", host="desk"):
    state = load_control(directory)
    docker_ok = host == "mini" or docker_ready(docker)
    readings = None
    reason = "docker" if not docker_ok else "presion"
    action = "pausar" if not docker_ok else "mantener"
    if docker_ok:
        if host == "mini":
            action, reason = "reanudar", "arranque"
        else:
            readings = pressure(proc)
            log(directory, "lectura", **readings, docker=True)
            high, low = PRESSURE_THRESHOLDS["pause"], PRESSURE_THRESHOLDS["resume"]
            if readings["io"] > high["io"] or readings["cpu"] > high["cpu"] or readings["memory"] < high["memory"]:
                action = "pausar"
            elif readings["io"] < low["io"] and readings["cpu"] < low["cpu"] and readings["memory"] >= low["memory"]:
                action = "reanudar"
    if state.get("starting"):
        action, reason = "pausar", "recuperacion_pendiente"
    log(directory, "decision", accion=action, motivo=reason, docker=docker_ok)
    owned = set(state["owned"])
    limits = limits_for(host)
    if not owned <= limits.keys():
        raise ValueError("Grupos desconocidos en estado del guardia")
    groups = pueue.status()["groups"]
    for group in limits:
        status = groups[group]["status"]
        if group in owned and status != "Paused":
            owned.remove(group)
            save_owned(directory, owned)
        if action == "pausar" and status == "Running":
            # Intención durable antes del efecto; una caída no pierde la propiedad.
            owned.add(group)
            save_owned(directory, owned)
            pueue.call("pause", "--wait", "-g", group)
            log(directory, "pausado", grupo=group, motivo=reason)
        elif action == "reanudar" and group in owned and status == "Paused":
            pueue.call("start", "-g", group)
            owned.remove(group)
            save_owned(directory, owned)
            log(directory, "reanudado", grupo=group, motivo=reason)


def validate_task(task_id, task):
    """Rechazar antes de que Pueue sustituya una cola ilegible por una vacía."""
    def timestamp(value):
        if not isinstance(value, str) or datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("Fecha Pueue incompatible")

    def status(value):
        if not isinstance(value, dict) or len(value) != 1:
            raise ValueError("Estado de tarea incompatible")
        name, detail = next(iter(value.items()))
        required = {"Queued": ("enqueued_at",), "Running": ("enqueued_at", "start"),
                    "Paused": ("enqueued_at", "start"), "Done": ("enqueued_at", "start", "end")}
        if name in required:
            for field in required[name]:
                timestamp(detail[field])
            if name == "Done":
                result = detail["result"]
                if isinstance(result, dict):
                    if len(result) != 1 or not (
                        ("Failed" in result and type(result["Failed"]) is int)
                        or ("FailedToSpawn" in result and isinstance(result["FailedToSpawn"], str))
                    ):
                        raise ValueError("Resultado Pueue incompatible")
                elif result not in ("Success", "Killed", "Errored", "DependencyFailed"):
                    raise ValueError("Resultado Pueue incompatible")
        elif name == "Stashed":
            if detail["enqueue_at"] is not None:
                timestamp(detail["enqueue_at"])
        elif name == "Locked":
            status(detail["previous_status"])
        else:
            raise ValueError("Estado de tarea incompatible")

    if type(task["id"]) is not int or str(task["id"]) != task_id or task["id"] < 0:
        raise ValueError("Identidad Pueue incompatible")
    timestamp(task["created_at"])
    for field in ("command", "original_command", "path", "group"):
        if not isinstance(task[field], str):
            raise ValueError("Tarea Pueue incompatible")
    if not isinstance(task["envs"], dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in task["envs"].items()):
        raise ValueError("Entorno Pueue incompatible")
    if not isinstance(task["dependencies"], list) or not all(type(v) is int and v >= 0 for v in task["dependencies"]):
        raise ValueError("Dependencias Pueue incompatibles")
    if type(task["priority"]) is not int or not -(2**31) <= task["priority"] < 2**31:
        raise ValueError("Prioridad Pueue incompatible")
    if task["label"] is not None and not isinstance(task["label"], str):
        raise ValueError("Etiqueta Pueue incompatible")
    status(task["status"])


def read_queue(path):
    if path.with_suffix(".json.gz").exists():
        raise ValueError("Se requiere compress_state_file: false; no se altera el estado comprimido")
    value = json.loads(path.read_text()) if path.exists() else {"tasks": {}, "groups": {}}
    if not isinstance(value.get("tasks"), dict) or not isinstance(value.get("groups"), dict):
        raise ValueError("Estado Pueue incompleto; arranque abortado sin reemplazar la cola")
    try:
        for group in value["groups"].values():
            if group.get("status") not in ("Running", "Paused") or type(group.get("parallel_tasks")) is not int or group["parallel_tasks"] < 0:
                raise ValueError("Grupo Pueue incompatible; arranque abortado")
        for task_id, task in value["tasks"].items():
            validate_task(task_id, task)
            if task["group"] not in value["groups"]:
                raise ValueError("Grupo de tarea ausente; no se remapea ni se pierde la cola")
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError("Cola Pueue incompatible; arranque abortado sin sustituirla") from error
    return value


def task_key(task):
    # Pueue reutiliza ids después de clean/reset: el id por sí solo no es identidad.
    return str(task["id"]) + ":" + task["created_at"]


def active_attempts(queue):
    result = {}
    for task_id, task in queue["tasks"].items():
        status = task["status"]
        if isinstance(status, dict) and ("Running" in status or "Paused" in status):
            detail = status.get("Running", status.get("Paused"))
            result[task_id] = {"key": task_key(task), "start": detail["start"]}
    return result


def prepare_start(queue_path, directory, host, boot):
    """Sólo con pueued detenido, bajo el candado de ciclo de vida del servicio."""
    queue = read_queue(queue_path)
    state = load_control(directory)
    # La intención de una parada ordenada ya se guardó antes de apagar el daemon.
    pending = dict(state.get("pending", {}))
    pending.update(active_attempts(queue))
    same_boot = state.get("boot") == boot
    safe = host == "desk" or (state.get("boot") is not None and not same_boot)
    if state.pop("clean_stop", False):
        safe = True
    for item in pending.values():
        item["safe"] = safe
    owned = set(state["owned"])
    for group, row in limits_for(host).items():
        if group not in queue["groups"]:
            queue["groups"][group] = {"status": "Running", "parallel_tasks": row["dia"]}
        if queue["groups"][group]["status"] == "Running":
            owned.add(group)
    # Cierra también grupos vacíos y tareas diferidas: no basta el auto-pause de Pueue.
    for value in queue["groups"].values():
        value["status"] = "Paused"
    queue["groups"].setdefault("default", {"status": "Paused", "parallel_tasks": 1})
    state.update(owned=sorted(owned), pending=pending, starting=True, boot=boot)
    save_control(directory, state)
    atomic_json(queue_path, queue)
    log(directory, "arranque_cerrado", pendientes=sorted(pending), sin_huerfanos=safe, host=host)


def capture_stop(pueue, directory, host):
    # Pausar primero impide que una tarea Queued empiece después de la captura.
    state = load_control(directory)
    groups = pueue.status()["groups"]
    owned = set(state["owned"])
    owned.update(group for group in limits_for(host) if groups[group]["status"] == "Running")
    state["owned"] = sorted(owned)
    state["starting"] = True  # El timer no debe reabrir entre captura y SIGTERM.
    save_control(directory, state)
    for group in groups:
        pueue.call("pause", "--wait", "-g", group)
    queue = pueue.status()
    state["pending"] = active_attempts(queue)
    state["clean_stop"] = False
    save_control(directory, state)
    log(directory, "parada_capturada", pendientes=sorted(state["pending"]), host=host)


def recover(pueue, directory, queue_path):
    state = load_control(directory)
    queue = pueue.status()
    for task_id, item in state["pending"].items():
        task = queue["tasks"].get(task_id)
        if task is None or task_key(task) != item["key"]:
            continue
        status = task["status"]
        detail = status.get("Done", {}) if isinstance(status, dict) else {}
        if detail.get("result") != "Killed" or detail.get("start") != item["start"]:
            continue  # Un fallo propio, una muerte manual o una ejecución distinta.
        record = state["recovery"].get(item["key"])
        if record and record.get("handled_start") == item["start"]:
            if record["phase"] == "intentando":
                record.update(phase="fallida", motivo="Recuperación interrumpida; no se repite un restart de resultado incierto")
                save_control(directory, state)
                log(directory, "recuperacion_rechazada", tarea=task_id, motivo=record["motivo"])
            continue
        if record or not item["safe"]:
            reason = ("Segundo reinicio: esta tarea ya consumió su única recuperación"
                      if record else "No se descartan hijos vivos tras la muerte del daemon en macOS")
            state["recovery"][item["key"]] = {"handled_start": item["start"], "phase": "fallida", "motivo": reason}
            save_control(directory, state)
            log(directory, "recuperacion_rechazada", tarea=task_id, motivo=reason)
            continue
        if queue["groups"][task["group"]]["status"] != "Paused":
            raise RuntimeError("recuperar requiere la admisión cerrada por el supervisor")
        # El intento se consume ANTES de enviar restart. Una caída en esta ventana
        # deja fallo conservador; jamás envía restart dos veces para la misma tarea.
        state["recovery"][item["key"]] = {"handled_start": item["start"], "phase": "intentando"}
        save_control(directory, state)
        pueue.call("restart", "--in-place", task_id)
        # 4.0.4 no persiste restart ni parallel. pause --wait sí guarda todo el estado.
        pueue.call("pause", "--wait", "-g", task["group"])
        disk = read_queue(queue_path)
        restored = disk["tasks"].get(task_id, {})
        if not isinstance(restored.get("status"), dict) or "Queued" not in restored["status"]:
            raise RuntimeError(f"No se confirmó en disco la recuperación de {task_id}")
        with queue_path.open("rb") as handle:
            os.fsync(handle.fileno())
        sync_directory(queue_path.parent)
        state["recovery"][item["key"]]["phase"] = "encolada"
        save_control(directory, state)
        log(directory, "recuperada", tarea=task_id, motivo="reinicio", mismo_id=True)
    state["starting"] = False
    save_control(directory, state)


def live_tasks(state):
    # Los enums con datos de serde son objetos, p.ej. {"Running": {...}}.
    # Sólo estados terminales conocidos permiten relevar un daemon manual.
    return [task for task in state["tasks"].values()
            if task["status"] != "Done" and not (
                isinstance(task["status"], dict) and set(task["status"]) == {"Done"})]


def default_data():
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/pueue"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "pueue"


def managed_settings(home, host):
    """Rutas estándar de Pueue; el cliente normal comparte socket, secreto y cola."""
    if host == "mini":
        data = home / "Library/Application Support/pueue"
        config_dir = home / "Library/Application Support/pueue"
    else:
        data = Path(os.environ.get("XDG_DATA_HOME", str(home / ".local/share"))) / "pueue"
        config_dir = Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config"))) / "pueue"
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", str(data)))
    username = pwd.getpwuid(os.getuid()).pw_name
    return {"shared": {"pueue_directory": str(data), "runtime_directory": str(runtime),
                       "unix_socket_path": str(runtime / f"pueue_{username}.socket"),
                       "pid_path": str(runtime / "pueue.pid"),
                       "alias_file": str(config_dir / "pueue_aliases.yml")},
            "daemon": {"compress_state_file": False}}


def managed_configuration(home, host, library):
    """Conservar opciones de daemon/cliente; fijar explícitamente las rutas adoptadas.

    El YAML emitido por Pueue usa bloques de primer nivel. No se interpreta YAML:
    se conserva literalmente todo salvo shared. JSON también es YAML válido.
    """
    settings = managed_settings(home, host)
    default_config = (home / "Library/Application Support/pueue/pueue.yml" if host == "mini"
                      else Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config"))) / "pueue/pueue.yml")
    source = library / "pueue.yml"
    if not source.exists():
        source = Path(os.environ.get("PUEUE_CONFIG_PATH", str(default_config)))
    if not source.exists():
        return settings, json.dumps(settings, ensure_ascii=False) + "\n"
    text = source.read_text()
    try:
        original = json.loads(text)
    except json.JSONDecodeError:
        # Configuraciones YAML complejas se rechazan antes de reemplazar el servicio.
        lines = text.splitlines(keepends=True)
        starts = [i for i, line in enumerate(lines) if re.match(r"^shared:", line)]
        if len(starts) != 1:
            raise ValueError("La configuración debe tener un único bloque shared de primer nivel o ser JSON")
        daemon_lines = [line for line in lines if re.match(r"^daemon:", line)]
        if any(not re.fullmatch(r"daemon:\s*(?:#.*)?\n?", line) for line in daemon_lines):
            raise ValueError("El bloque daemon debe ser YAML expandido o usar configuración JSON")
        for line in lines:
            match = re.match(r"^\s+compress_state_file:\s*([^#]*)(?:#.*)?$", line)
            if match and match.group(1).strip() != "false":
                raise ValueError("Se requiere compress_state_file: false antes de adoptar la cola")
        first = starts[0]
        end = first + 1
        while end < len(lines) and (not lines[end].strip() or lines[end][0].isspace() or lines[end].startswith("#")):
            end += 1
        lines[first:end] = ["shared: " + json.dumps(settings["shared"], ensure_ascii=False) + "\n"]
        return settings, "".join(lines)
    if original.get("daemon", {}).get("compress_state_file", False):
        raise ValueError("Se requiere compress_state_file: false antes de adoptar la cola")
    original["shared"] = settings["shared"]
    return settings, json.dumps(original, ensure_ascii=False) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["aplicar", "guardia", "recuperar", "pausar", "reanudar"])
    parser.add_argument("group", nargs="?")
    parser.add_argument("--host", choices=["desk", "mini"], default="desk")
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    parser.add_argument("--pueue-bin")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--data-dir", type=Path, default=default_data())
    parser.add_argument("--docker-bin", default=os.environ.get("PAO_DOCKER_BIN", "/usr/bin/docker"))
    args = parser.parse_args()
    pueue = Pueue(args.pueue_bin, args.config)
    directory = args.state_dir
    with locked(directory):
        try:
            if args.command == "aplicar":
                apply_mode(pueue, datetime.now(timezone.utc), args.host)
            elif args.command == "guardia":
                guard(pueue, Path("/proc"), directory, args.docker_bin, args.host)
            elif args.command == "recuperar":
                recover(pueue, directory, args.data_dir / "state.json")
            else:
                if args.group not in limits_for(args.host):
                    parser.error("Se necesita un grupo administrado por este host")
                state = load_control(directory)
                owned = set(state["owned"])
                owned.discard(args.group)
                save_owned(directory, owned)
                if args.command == "pausar":
                    pueue.call("pause", "--wait", "-g", args.group)
                else:
                    pueue.call("start", "-g", args.group)
                log(directory, "pausa_manual" if args.command == "pausar" else "reanudacion_forzada", grupo=args.group)
        except Exception as error:
            log(directory, "error", detalle=str(error))
            raise


if __name__ == "__main__":
    main()
