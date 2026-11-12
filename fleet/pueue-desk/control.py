#!/usr/bin/python3 -I
"""Cuotas y guardia de Pueue 4.0.4; sólo biblioteca estándar."""
import argparse
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import math
import os
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
    def __init__(self, binary=None):
        self.binary = str(binary or Path.home() / ".local/bin/pueue")

    def call(self, *args):
        return subprocess.run([self.binary, *map(str, args)], check=True,
                              text=True, capture_output=True, timeout=10).stdout

    def status(self):
        state = json.loads(self.call("status", "--json"))
        if not isinstance(state.get("tasks"), dict) or not isinstance(state.get("groups"), dict):
            raise ValueError("Estado Pueue incompleto")
        return state

    def parallel(self, group, value):
        self.call("parallel", validate_limit(value), "-g", group)


def wait_ready(pueue, seconds=30):
    deadline = time.monotonic() + seconds
    while True:
        try:
            return pueue.status()
        except subprocess.CalledProcessError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


def limits_for(host):
    return GROUP_LIMITS if host == "desk" else {"suites": {"dia": 3, "noche": 3}}


def apply_mode(pueue, now, host="desk"):
    mode = mode_at(now)
    limits = {group: validate_limit(row[mode]) for group, row in limits_for(host).items()}
    state = pueue.status()
    if not limits.keys() <= state["groups"].keys():
        raise ValueError("Faltan grupos: preparar offline antes de arrancar el daemon")
    for group, limit in limits.items():
        pueue.parallel(group, limit)
    state = pueue.status()
    for group, limit in limits.items():
        if state["groups"][group]["parallel_tasks"] != limit:
            raise ValueError(f"Cuota incorrecta: {group}")


@contextmanager
def locked(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "control.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


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


def load_owned(directory):
    target = directory / "guardia-state.json"
    value = json.loads(target.read_text()) if target.exists() else []
    if not isinstance(value, list) or not all(isinstance(group, str) for group in value):
        raise ValueError("Estado del guardia incompatible; se conserva sin sobrescribir")
    return set(value)


def save_owned(directory, owned):
    atomic_json(directory / "guardia-state.json", sorted(owned))


def log(directory, event, **fields):
    # Una entrada enorme también tiene tope. Nunca registrar comandos ni entornos.
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / "guardia.log"
    record = {"utc": datetime.now(timezone.utc).isoformat(), "evento": event, **fields}
    encoded = (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode()
    if len(encoded) > LOG_MAX_BYTES:
        encoded = (json.dumps({"utc": record["utc"], "evento": event,
                              "detalle": "Registro truncado por límite de tamaño"}) + "\n").encode()
    if target.exists() and target.stat().st_size + len(encoded) > LOG_MAX_BYTES:
        # También acota un archivo heredado que ya superara el límite.
        if target.stat().st_size > LOG_MAX_BYTES:
            with target.open("r+b") as handle:
                handle.truncate(LOG_MAX_BYTES)
        target.replace(directory / "guardia.log.1")
    with target.open("ab") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    sync_directory(directory)


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
    owned = load_owned(directory)
    action, reason = "mantener", "presion"
    if host == "mini":
        action, reason = "reanudar", "arranque"
    elif not docker_ready(docker):
        action, reason = "pausar", "docker"
    else:
        readings = pressure(proc)
        log(directory, "lectura", **readings)
        high, low = PRESSURE_THRESHOLDS["pause"], PRESSURE_THRESHOLDS["resume"]
        if readings["io"] > high["io"] or readings["cpu"] > high["cpu"] or readings["memory"] < high["memory"]:
            action = "pausar"
        elif readings["io"] < low["io"] and readings["cpu"] < low["cpu"] and readings["memory"] >= low["memory"]:
            action = "reanudar"
    log(directory, "decision", accion=action, motivo=reason)
    groups = pueue.status()["groups"]
    if action == "reanudar":
        # Admisión incluye cuotas, también si el timer de modo aún no corrió.
        apply_mode(pueue, datetime.now(timezone.utc), host)
    candidates = set(groups) if reason == "docker" else set(limits_for(host))
    for group in sorted(candidates | owned):
        if group not in groups:
            raise ValueError(f"Grupo {group} ausente: requiere preparar offline")
        status = groups[group]["status"]
        if group in owned and status == "Running":
            owned.remove(group)
            save_owned(directory, owned)
        if action == "pausar" and status == "Running":
            owned.add(group)
            save_owned(directory, owned)  # intención durable antes del efecto
            pueue.call("pause", "--wait", "-g", group)
            log(directory, "pausado", grupo=group, motivo=reason)
        elif action == "reanudar" and group in owned and status == "Paused":
            pueue.call("start", "-g", group)
            owned.remove(group)  # tras el efecto: si falla, se reintenta
            save_owned(directory, owned)
            log(directory, "reanudado", grupo=group, motivo=reason)


def request_resume(pueue, group, directory, proc, docker="/usr/bin/docker", host="desk"):
    groups = pueue.status()["groups"]
    if group not in groups or groups[group]["status"] == "Reset":
        raise ValueError("El grupo no existe o está en Reset")
    owned = load_owned(directory)
    owned.add(group)
    save_owned(directory, owned)
    log(directory, "admision_solicitada", grupo=group)
    guard(pueue, proc, directory, docker, host)


def live_tasks(state):
    # Los enums con datos de serde son objetos, p.ej. {"Running": {...}}.
    # Sólo estados terminales conocidos permiten relevar un daemon manual.
    return [task for task in state["tasks"].values()
            if task["status"] != "Done" and not (
                isinstance(task["status"], dict) and set(task["status"]) == {"Done"})]


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Clave JSON duplicada; no se sustituye la cola")
        value[key] = item
    return value


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
                        ("Failed" in result and type(result["Failed"]) is int and -(2**31) <= result["Failed"] < 2**31)
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

    if type(task["id"]) is not int or str(task["id"]) != task_id or not 0 <= task["id"] < 2**64:
        raise ValueError("Identidad Pueue incompatible")
    timestamp(task["created_at"])
    for field in ("command", "original_command", "path", "group"):
        if not isinstance(task[field], str):
            raise ValueError("Tarea Pueue incompatible")
    if not isinstance(task["envs"], dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in task["envs"].items()):
        raise ValueError("Entorno Pueue incompatible")
    if not isinstance(task["dependencies"], list) or not all(type(v) is int and 0 <= v < 2**64 for v in task["dependencies"]):
        raise ValueError("Dependencias Pueue incompatibles")
    if type(task["priority"]) is not int or not -(2**31) <= task["priority"] < 2**31:
        raise ValueError("Prioridad Pueue incompatible")
    if task["label"] is not None and not isinstance(task["label"], str):
        raise ValueError("Etiqueta Pueue incompatible")
    status(task["status"])


def read_queue(path):
    if path.with_suffix(".json.gz").exists():
        raise ValueError("Se requiere compress_state_file: false; no se altera el estado comprimido")
    value = json.loads(path.read_text(), object_pairs_hook=unique_object) if path.exists() else {"tasks": {}, "groups": {}}
    if not isinstance(value, dict) or not isinstance(value.get("tasks"), dict) or not isinstance(value.get("groups"), dict):
        raise ValueError("Estado Pueue incompleto; arranque abortado sin reemplazar la cola")
    try:
        for group in value["groups"].values():
            if group.get("status") not in ("Running", "Paused", "Reset") or type(group.get("parallel_tasks")) is not int or not 0 <= group["parallel_tasks"] < 2**64:
                raise ValueError("Grupo Pueue incompatible; arranque abortado")
        for task_id, task in value["tasks"].items():
            validate_task(task_id, task)
            if task["group"] not in value["groups"]:
                raise ValueError("Grupo de tarea ausente; no se remapea ni se pierde la cola")
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError("Cola Pueue incompatible; arranque abortado sin sustituirla") from error
    return value


def prepare_queue(queue, logged_ids, owned, now, host):
    """Transformación pura. El reloj sólo fecha el fallo, nunca clasifica logs."""
    queue = deepcopy(queue)
    owned = set(owned)
    events = []
    mode = mode_at(datetime.fromisoformat(now.replace("Z", "+00:00")))
    managed = limits_for(host)
    queue["groups"].setdefault("default", {"status": "Running", "parallel_tasks": 1})
    for name, row in managed.items():
        if name not in queue["groups"]:
            queue["groups"][name] = {"status": "Paused", "parallel_tasks": row[mode]}
            owned.add(name)
        else:
            queue["groups"][name]["parallel_tasks"] = row[mode]
    for name, group in queue["groups"].items():
        if group["status"] == "Running":
            owned.add(name)
            group["status"] = "Paused"
            events.append({"evento": "cerrado", "grupo": name, "motivo": "arranque"})
    for task_id, task in queue["tasks"].items():
        variant, detail = next(iter(task["status"].items()))
        cause = None
        if variant in ("Running", "Paused"):
            cause = "sin_parada"
        elif variant == "Queued" and task_id in logged_ids:
            cause = "log_existente"
            # Una ambigüedad pide intervención: no basta que Docker vuelva.
            owned.discard(task["group"])
        if cause:
            start = detail.get("start", now)
            task["status"] = {"Done": {"enqueued_at": detail["enqueued_at"],
                                       "start": start, "end": now, "result": "Killed"}}
            events.append({"evento": "interrumpida", "tarea": task_id, "created_at": task["created_at"],
                           "start": start, "grupo": task["group"], "causa": cause})
    return queue, owned, events


def prepare_start(pueue, directory, queue_path, host, daemon):
    """Con control.lock tomado y el daemon detenido; jamás llama restart."""
    try:
        pueue.status()
    except subprocess.CalledProcessError:
        pass
    else:
        raise RuntimeError("Otro daemon responde; preparar exige daemon detenido")
    version = subprocess.check_output([str(daemon), "--version"], text=True, timeout=10).strip()
    if version != "pueued 4.0.4":
        raise RuntimeError("Se requiere pueued 4.0.4; arranque bloqueado")
    queue = read_queue(queue_path)
    if any(group["status"] == "Reset" for group in queue["groups"].values()):
        raise ValueError("Reset interrumpido: Pueue abriría el grupo sin admisión; revisar la cola antes de arrancar")
    # lexists: incluso un enlace roto implica evidencia previa, nunca ignorarla.
    logs = {task_id for task_id in queue["tasks"]
            if os.path.lexists(queue_path.parent / "task_logs" / (task_id + ".log"))}
    queue, owned, events = prepare_queue(queue, logs, load_owned(directory),
                                         datetime.now(timezone.utc).isoformat(), host)
    for event in events:
        event = dict(event)
        log(directory, event.pop("evento"), **event)
    save_owned(directory, owned)
    atomic_json(queue_path, queue)
    return queue


def default_data():
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/pueue"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "pueue"


def validate_configuration():
    """Sólo valida; no escribe ni genera un pueue.yml administrado.

    Acepta JSON y el YAML expandido simple que emite Pueue. Ante sintaxis
    ambigua falla cerrado: no intenta adivinar dónde vive la cola.
    """
    for key in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR"):
        if key in os.environ and not Path(os.environ[key]).is_absolute():
            raise ValueError(f"{key} debe ser absoluto para compartir rutas con Pueue")
    config_dir = (default_data() if sys.platform == "darwin" else
                  Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "pueue")
    standard = config_dir / "pueue.yml"
    if os.environ.get("PUEUE_CONFIG_PATH"):
        raise ValueError("PUEUE_CONFIG_PATH personalizado no se admite en el host")
    source = standard if standard.exists() else Path.cwd() / "pueue.yml"
    if not source.exists():
        return
    text = source.read_text()
    try:
        config = json.loads(text)
    except ValueError:
        config = {}
        section = None
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith("#") or line.strip() == "---":
                continue
            if not line.startswith(" "):
                match = re.fullmatch(r"([a-z_]+):\s*(.*?)\s*", line)
                if not match:
                    raise ValueError("Configuración YAML compleja; revisar antes de adoptar")
                section, raw = match.groups()
                if section in config:
                    raise ValueError("Bloque de configuración duplicado")
                if not raw or raw.startswith("#"):
                    config[section] = {}
                else:
                    try:
                        config[section] = json.loads(raw.split(" #", 1)[0])
                    except ValueError:
                        raise ValueError("Bloque YAML inline complejo; revisar antes de adoptar") from None
            elif section in ("shared", "daemon"):
                match = re.fullmatch(r"  ([a-z_]+):\s*(.*?)\s*", line)
                if not match:
                    raise ValueError("Configuración shared/daemon compleja")
                key, raw = match.groups()
                if key in config[section]:
                    raise ValueError("Clave de configuración duplicada")
                raw = raw.split(" #", 1)[0]
                try:
                    value = json.loads(raw)
                except ValueError:
                    if raw.startswith("'") and raw.endswith("'"):
                        value = raw[1:-1].replace("''", "'")
                    elif re.fullmatch(r"[a-zA-Z0-9_/. :~-]+", raw):
                        value = raw
                    else:
                        raise ValueError("Valor de configuración complejo") from None
                config[section][key] = value
    if not isinstance(config, dict) or not all(isinstance(config.get(key, {}), dict) for key in ("shared", "daemon")):
        raise ValueError("Configuración incompatible")
    shared = config.get("shared", {})
    # El cliente y el daemon sin --config deben mirar las mismas rutas estándar.
    for key in ("pueue_directory", "runtime_directory", "unix_socket_path", "pid_path"):
        value = shared.get(key)
        if value is not None and not (key == "pueue_directory" and value == str(default_data())):
            raise ValueError(f"shared.{key} personalizado; no se adopta la cola")
    if shared.get("use_unix_socket", True) is not True:
        raise ValueError("El host requiere socket Unix")
    if config.get("daemon", {}).get("compress_state_file", False) is not False:
        raise ValueError("Se requiere compress_state_file: false")


def record_stop(pueue, directory):
    for task_id, task in pueue.status()["tasks"].items():
        if any(name in task["status"] for name in ("Running", "Paused")):
            log(directory, "interrumpida", tarea=task_id, causa="parada")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["aplicar", "guardia", "pausar", "reanudar", "preparar", "parada"])
    parser.add_argument("group", nargs="?")
    parser.add_argument("--host", choices=["desk", "mini"], default="desk")
    parser.add_argument("--docker-bin", default=os.environ.get("PAO_DOCKER_BIN", "/usr/bin/docker"))
    parser.add_argument("--exec", dest="executable")
    args = parser.parse_args()
    binary = "/opt/homebrew/bin/pueue" if args.host == "mini" else None
    daemon = "/opt/homebrew/bin/pueued" if args.host == "mini" else Path.home() / ".local/bin/pueued"
    pueue = Pueue(binary)
    try:
        with locked(STATE_DIR):
            if args.command == "preparar":
                validate_configuration()
                prepare_start(pueue, STATE_DIR, default_data() / "state.json", args.host, daemon)
            elif args.command == "aplicar":
                wait_ready(pueue)
                apply_mode(pueue, datetime.now(timezone.utc), args.host)
            elif args.command == "guardia":
                guard(pueue, Path("/proc"), STATE_DIR, args.docker_bin, args.host)
            elif args.command == "parada":
                record_stop(pueue, STATE_DIR)
            else:
                if args.group is None:
                    parser.error(f"{args.command} necesita un grupo")
                if args.command == "reanudar":
                    request_resume(pueue, args.group, STATE_DIR, Path("/proc"), args.docker_bin, args.host)
                else:
                    owned = load_owned(STATE_DIR)
                    owned.discard(args.group)
                    save_owned(STATE_DIR, owned)
                    pueue.call("pause", "--wait", "-g", args.group)
                    log(STATE_DIR, "pausa_manual", grupo=args.group)
        if args.executable:
            if args.command != "preparar" or Path(args.executable) != Path(daemon):
                raise ValueError("--exec sólo admite el pueued estándar tras preparar")
            # El daemon conserva el PID de launchd; no hay supervisor.
            os.execv(str(daemon), [str(daemon)])
        return 0
    except Exception as error:
        with locked(STATE_DIR):
            log(STATE_DIR, "error", detalle=str(error))
        return 1


if __name__ == "__main__":
    sys.exit(main())
