#!/usr/bin/python3 -I
"""Cuotas, guardia y preparación del arranque de Pueue 4.0.4; sólo biblioteca estándar."""
import argparse
from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

GROUP_LIMITS = {
    "hasito": {"dia": 3, "noche": 4},
    "jale": {"dia": 2, "noche": 3},
    "otros": {"dia": 1, "noche": 1},
    "pg5434": {"dia": 1, "noche": 1},
}
MINI_LIMITS = {"suites": {"dia": 3, "noche": 3}}
PRESSURE_THRESHOLDS = {
    "pause": {"io": 40, "cpu": 30, "memory": 4 * 1024**3},
    "resume": {"io": 20, "cpu": 15, "memory": 6 * 1024**3},
    "docker_timeout": 3,
}
LOG_MAX_BYTES = 1024 * 1024
PUEUED_VERSION = "pueued 4.0.4"
STATE_DIR = Path.home() / ".local/state/pao/pueue-desk"
BIN_DIRS = {"desk": Path.home() / ".local/bin", "mini": Path("/opt/homebrew/bin")}
INTERRUPTED_NOTE = ("\n[pao-pueue] Tarea interrumpida: {causa}. El host no la reejecuta; "
                    "relanzarla con una corrida nueva del cliente, nunca con pueue restart.\n")


class Rejected(Exception):
    """preparar no puede garantizar la cola; el daemon no debe arrancar."""


def mode_at(now):
    if now.tzinfo is None:
        raise ValueError("La hora debe incluir zona horaria")
    return "noche" if 5 <= now.astimezone(timezone.utc).hour < 14 else "dia"


def validate_limit(value):
    if type(value) is not int or not 1 <= value <= 8:
        raise ValueError(f"parallel fuera de 1..8: {value!r}")
    return value


def limits_for(host):
    return GROUP_LIMITS if host == "desk" else MINI_LIMITS


class Pueue:
    def __init__(self, binary=None):
        self.binary = str(binary or BIN_DIRS["desk"] / "pueue")

    def call(self, *args):
        return subprocess.run([self.binary, *map(str, args)], check=True,
                              text=True, capture_output=True, timeout=10).stdout

    def status(self):
        state = json.loads(self.call("status", "--json"))
        if not isinstance(state.get("tasks"), dict) or not isinstance(state.get("groups"), dict):
            raise ValueError("Estado Pueue incompleto")
        return state

    def responds(self):
        try:
            self.status()
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
            return False

    def parallel(self, group, value):
        self.call("parallel", validate_limit(value), "-g", group)


def apply_mode(pueue, now, host="desk"):
    mode = mode_at(now)
    limits = {group: validate_limit(row[mode]) for group, row in limits_for(host).items()}
    state = pueue.status()
    for group in limits:
        # group add crea el grupo abierto; sólo preparar lo crea, cerrado y sin daemon.
        if group not in state["groups"]:
            raise ValueError(f"Falta el grupo {group}; lo crea preparar con el daemon parado")
    for group, limit in limits.items():
        pueue.parallel(group, limit)
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


def flush(fd):
    # En macOS fsync no vacía la caché del disco; F_FULLFSYNC sí.
    if hasattr(fcntl, "F_FULLFSYNC"):
        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except OSError:
            pass
    os.fsync(fd)


def sync_directory(directory):
    fd = os.open(directory, os.O_RDONLY)
    try:
        flush(fd)
    finally:
        os.close(fd)


def atomic_text(target, content):
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.with_name(target.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(content)
        handle.flush()
        flush(handle.fileno())
    temporary.replace(target)
    sync_directory(target.parent)


def atomic_json(target, value):
    atomic_text(target, json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n")


def load_control(directory):
    """owned: pausas propias que el guardia reabre. retenidos: grupos que sólo reabre `reanudar`.

    relevo: cambio de servicio en curso en la Mini (lo completa mini.py vigilar).
    Una lista sola es el formato de U11 y equivale a owned.
    """
    target = directory / "guardia-state.json"
    value = json.loads(target.read_text()) if target.exists() else []
    if isinstance(value, list):
        value = {"owned": value}
    value = {"owned": [], "retenidos": {}, "relevo": None, **value}
    if set(value) != {"owned", "retenidos", "relevo"} or not all(isinstance(g, str) for g in value["owned"]) \
            or not isinstance(value["retenidos"], dict) \
            or not all(isinstance(ids, list) and all(type(i) is int for i in ids) for ids in value["retenidos"].values()) \
            or value["relevo"] is not None and not isinstance(value["relevo"], dict):
        raise ValueError("Estado del guardia ilegible; no se sobrescribe")
    return value


def save_control(directory, value):
    atomic_json(directory / "guardia-state.json", {
        "owned": sorted(set(value["owned"])),
        "retenidos": {g: sorted(set(ids)) for g, ids in sorted(value["retenidos"].items()) if ids},
        "relevo": value["relevo"]})


def cap_log(target, limit=LOG_MAX_BYTES):
    """Copia y trunca: el escritor abre con O_APPEND y sigue al final del archivo vacío."""
    if target.exists() and target.stat().st_size >= limit:
        shutil.copyfile(target, target.with_name(target.name + ".1"))
        os.truncate(target, 0)


def log(directory, evento, **fields):
    target = directory / "guardia.log"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.exists() and target.stat().st_size >= LOG_MAX_BYTES:
        target.replace(directory / "guardia.log.1")
    record = {"utc": datetime.now(timezone.utc).isoformat(), "evento": evento, **fields}
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


def decide(proc, directory, docker, host):
    """Admisión del host: (acción, motivo). Docker caído nunca reabre."""
    if host == "mini":
        return "reanudar", "arranque"
    if not docker_ready(docker):
        return "pausar", "docker"
    readings = pressure(proc)
    log(directory, "lectura", **readings, docker=True)
    high, low = PRESSURE_THRESHOLDS["pause"], PRESSURE_THRESHOLDS["resume"]
    if readings["io"] > high["io"] or readings["cpu"] > high["cpu"] or readings["memory"] < high["memory"]:
        return "pausar", "presion"
    if readings["io"] < low["io"] and readings["cpu"] < low["cpu"] and readings["memory"] >= low["memory"]:
        return "reanudar", "presion"
    return "mantener", "presion"


def guard(pueue, proc, directory, docker="/usr/bin/docker", host="desk"):
    """Un ciclo del guardia, con el candado tomado. Devuelve los grupos que reabrió."""
    state = load_control(directory)
    action, reason = decide(proc, directory, docker, host)
    if state["relevo"] is not None:
        # Un relevo de servicios sin terminar lo completa mini.py vigilar; mientras, no se abre nada.
        action, reason = ("pausar" if action == "pausar" else "mantener"), "relevo"
    log(directory, "decision", accion=action, motivo=reason)
    owned, held = set(state["owned"]), {g: set(ids) for g, ids in state["retenidos"].items()}
    groups = pueue.status()["groups"]

    def save():
        save_control(directory, {**state, "owned": owned, "retenidos": held})

    # Si alguien ya abrió o borró un grupo, deja de pertenecer al guardia.
    stale = {g for g in owned | held.keys() if groups.get(g, {}).get("status") != "Paused"}
    if stale:
        owned -= stale
        for group in stale & held.keys():
            log(directory, "retencion_liberada", grupo=group, tareas=sorted(held.pop(group)),
                motivo="grupo abierto o borrado fuera del control")
        save()
    opened = []
    for group in limits_for(host):
        if action == "pausar" and groups.get(group, {}).get("status") == "Running":
            # Intención durable antes del efecto: una caída no deja una pausa sin dueño.
            owned.add(group)
            save()
            pueue.call("pause", "--wait", "-g", group)
            log(directory, "pausado", grupo=group, motivo=reason)
    if action == "reanudar":
        for group in sorted(owned - held.keys()):
            # La propiedad se suelta sólo tras reanudar: si start falla, el siguiente ciclo reintenta.
            pueue.call("start", "-g", group)
            owned.remove(group)
            save()
            opened.append(group)
            log(directory, "reanudado", grupo=group, motivo=reason)
    return action, reason, opened


def live_tasks(state):
    # Los enums con datos de serde son objetos, p.ej. {"Running": {...}}.
    # Sólo estados terminales conocidos permiten relevar un daemon manual.
    return [task for task in state["tasks"].values()
            if task["status"] != "Done" and not (
                isinstance(task["status"], dict) and set(task["status"]) == {"Done"})]


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
    """Cola en disco, o None en un primer arranque. Nunca devuelve una cola parcial."""
    if path.with_name("state.json.gz").exists():
        raise ValueError("Se requiere compress_state_file: false; no se altera el estado comprimido")
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text())
        if not isinstance(value.get("tasks"), dict) or not isinstance(value.get("groups"), dict):
            raise ValueError("Estado Pueue incompleto")
        for group in value["groups"].values():
            if group.get("status") not in ("Running", "Paused", "Reset") \
                    or type(group.get("parallel_tasks")) is not int or group["parallel_tasks"] < 0:
                raise ValueError("Grupo Pueue incompatible")
        for task_id, task in value["tasks"].items():
            validate_task(task_id, task)
            if task["group"] not in value["groups"]:
                raise ValueError("Grupo de tarea ausente")
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise ValueError(f"Cola Pueue ilegible ({error}); arranque abortado sin sustituirla") from error
    return value


def status_name(task):
    return next(iter(task["status"]))


def prepare(queue, logged, state, now, limits):
    """Transformación pura del arranque: (cola, estado del guardia, eventos).

    queue es state.json (None si no existe), logged los ids Queued con task_logs/<id>.log,
    state lo de load_control y limits {grupo: parallel} de los grupos administrados.
    Ninguna tarea empezada sigue en una forma que Pueue pueda lanzar; ningún grupo queda abierto.
    """
    queue = copy.deepcopy(queue) if queue is not None else {
        "tasks": {}, "groups": {"default": {"status": "Running", "parallel_tasks": 1}}}
    owned = set(state["owned"])
    held = {g: set(ids) for g, ids in state["retenidos"].items()}
    events = []
    for group, limit in limits.items():
        if group not in queue["groups"]:
            queue["groups"][group] = {"status": "Paused", "parallel_tasks": validate_limit(limit)}
            owned.add(group)
            events.append({"evento": "grupo_creado", "grupo": group, "parallel": limit})
    for task_id, task in sorted(queue["tasks"].items(), key=lambda item: int(item[0])):
        name = status_name(task)
        detail = task["status"][name]
        if name in ("Running", "Paused"):
            cause = "el daemon murió sin parada ordenada mientras corría"
            task["status"] = {"Done": {"enqueued_at": detail["enqueued_at"], "start": detail["start"],
                                       "end": now, "result": "Killed"}}
        elif name == "Queued" and task_id in logged:
            # Pueue crea el log justo antes de lanzar: la tarea pudo haber corrido sin que se guardara.
            cause = "seguía Queued pero ya tenía log; pudo haber empezado"
            task["status"] = {"Done": {"enqueued_at": detail["enqueued_at"], "start": now,
                                       "end": now, "result": "Killed"}}
            held.setdefault(task["group"], set()).add(task["id"])
        else:
            continue
        events.append({"evento": "interrumpida", "tarea": task["id"], "grupo": task["group"],
                       "created_at": task["created_at"], "estado_previo": name, "causa": cause})
    for group, value in sorted(queue["groups"].items()):
        if value["status"] == "Running":
            value["status"] = "Paused"
            owned.add(group)
            events.append({"evento": "cerrado", "grupo": group, "motivo": "arranque"})
    for group, ids in sorted(held.items()):
        if group in state["retenidos"] and set(state["retenidos"][group]) == ids:
            continue
        events.append({"evento": "retenido", "grupo": group, "tareas": sorted(ids),
                       "motivo": "tarea Queued con log; abrir con control.py reanudar " + group})
    return queue, {**state, "owned": sorted(owned), "retenidos": {g: sorted(i) for g, i in held.items()}}, events


def pueue_paths(host):
    home = Path.home()
    if host == "mini":
        config = home / "Library/Application Support/pueue"
        data = config
    else:
        config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "pueue"
        data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share") / "pueue"
    runtime = Path(os.environ["XDG_RUNTIME_DIR"]) if host == "desk" and os.environ.get("XDG_RUNTIME_DIR") else data
    return {"config": config / "pueue.yml", "data": data, "pid": runtime / "pueue.pid"}


def check_config(path):
    if not path.exists():
        return
    text = path.read_text()
    if re.search(r"^\s*pueue_directory\s*:\s*(?!null\s*$|~\s*$)\S", text, re.M):
        raise Rejected(f"{path} fija pueue_directory; preparar sólo conoce la ruta por defecto")
    if re.search(r"^\s*compress_state_file\s*:\s*true\b", text, re.M):
        raise Rejected(f"{path} comprime el estado; se requiere compress_state_file: false")


def clear_stale_pid(path, directory):
    """Tras reiniciar la máquina el pid viejo puede ser de otro proceso y pueued no arrancaría."""
    if not path.exists():
        return
    try:
        pid = int(path.read_text().strip())
    except ValueError:
        return
    command = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "comm="], text=True, capture_output=True)
    if command.returncode != 0:
        return
    if Path(command.stdout.strip()).name == "pueued":
        raise Rejected(f"pueued {pid} sigue vivo sin responder; no se prepara un estado en uso")
    path.unlink()
    log(directory, "pid_obsoleto", pid=pid, proceso=command.stdout.strip())


def cmd_prepare(host, pueue, pueued, directory, now=None):
    paths = pueue_paths(host)
    state_path = paths["data"] / "state.json"
    with locked(directory):
        if pueue.responds():
            raise Rejected("pueued ya responde; no se prepara un estado en uso")
        version = subprocess.run([str(pueued), "--version"], text=True, capture_output=True).stdout.strip()
        if version != PUEUED_VERSION:
            raise Rejected(f"Se esperaba {PUEUED_VERSION} y hay {version or 'nada'}; la cola queda intacta")
        check_config(paths["config"])
        try:
            queue = read_queue(state_path)
            state = load_control(directory)
        except ValueError as error:
            raise Rejected(str(error)) from error
        clear_stale_pid(paths["pid"], directory)
        logs = paths["data"] / "task_logs"
        logged = {task_id for task_id, task in (queue or {"tasks": {}})["tasks"].items()
                  if status_name(task) == "Queued" and (logs / f"{task_id}.log").exists()}
        limits = {g: row[mode_at(datetime.now(timezone.utc))] for g, row in limits_for(host).items()}
        stamp = now or datetime.now().astimezone().isoformat()
        new_queue, new_state, events = prepare(queue, logged, state, stamp, limits)
        # Se registra antes del efecto: una muerte a medias repite la línea, nunca la pierde.
        for event in events:
            log(directory, **event)
        # Primero la propiedad, después la cola: una muerte entre ambas deja pausas con dueño.
        if new_state != state:
            save_control(directory, new_state)
        if new_queue != queue:
            atomic_json(state_path, new_queue)
        logs.mkdir(parents=True, exist_ok=True)
        for event in events:
            if event["evento"] == "interrumpida":
                with (logs / f"{event['tarea']}.log").open("a") as handle:
                    handle.write(INTERRUPTED_NOTE.format(causa=event["causa"]))
        log(directory, "preparado", host=host, cambios=len(events))


def exec_daemon(pueued, directory):
    """Mini: launchd vigila el mismo PID; la salida va a host.log, que vigilar mantiene acotado."""
    target = directory / "host.log"
    cap_log(target)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.close(fd)
    os.execv(str(pueued), [str(pueued)])


def stop_note(pueue, directory):
    """ExecStop del desk: sólo deja constancia de qué tareas cortará la parada ordenada."""
    try:
        tasks = pueue.status()["tasks"]
    except Exception as error:
        log(directory, "parada", detalle=f"pueued no respondió: {error}")
        return
    running = sorted(t["id"] for t in tasks.values() if status_name(t) in ("Running", "Paused"))
    for task_id in running:
        log(directory, "interrumpida", tarea=task_id, grupo=tasks[str(task_id)]["group"], causa="parada ordenada")
    log(directory, "parada", tareas=running)


def resume(pueue, proc, directory, group, docker, host):
    """Retira la pausa manual o la retención y pide admisión; nunca abre sin ella."""
    groups = pueue.status()["groups"]
    if groups.get(group, {}).get("status") != "Paused":
        raise ValueError(f"{group} no está pausado")
    state = load_control(directory)
    state["retenidos"].pop(group, None)
    state["owned"] = sorted(set(state["owned"]) | {group})
    save_control(directory, state)
    log(directory, "reanudar_solicitado", grupo=group)
    action, reason, opened = guard(pueue, proc, directory, docker, host)
    if group in opened:
        print(f"{group} reabierto")
    else:
        print(f"{group} queda cerrado hasta que el host lo admita ({action}, {reason}); el guardia lo reabrirá")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["aplicar", "guardia", "pausar", "reanudar", "preparar", "parada"])
    parser.add_argument("group", nargs="?")
    parser.add_argument("--host", choices=["desk", "mini"], default="desk")
    parser.add_argument("--exec", dest="exec_daemon", action="store_true",
                        help="preparar: sustituir el proceso por pueued (Mini)")
    parser.add_argument("--pueue-bin")
    parser.add_argument("--pueued-bin")
    parser.add_argument("--docker-bin", default=os.environ.get("PAO_DOCKER_BIN", "/usr/bin/docker"))
    args = parser.parse_args(argv)
    bins = BIN_DIRS[args.host]
    pueue = Pueue(args.pueue_bin or bins / "pueue")
    pueued = Path(args.pueued_bin or bins / "pueued")
    if args.command in ("pausar", "reanudar") and args.group is None:
        parser.error(f"{args.command} necesita un grupo")
    if args.command == "preparar":
        try:
            cmd_prepare(args.host, pueue, pueued, STATE_DIR)
        except Exception as error:
            # En la Mini launchd reintenta cada 30 s: el motivo va sólo a guardia.log, que rota.
            log(STATE_DIR, "preparar_rechazado", detalle=str(error))
            if args.host == "desk":
                print(f"preparar rechazado: {error}", file=sys.stderr)
            return 1
        if args.exec_daemon:
            exec_daemon(pueued, STATE_DIR)
        return 0
    if args.command == "parada":
        stop_note(pueue, STATE_DIR)
        return 0
    with locked(STATE_DIR):
        try:
            if args.command == "aplicar":
                for attempt in range(30):
                    if pueue.responds():
                        break
                    if attempt == 29:
                        raise RuntimeError("pueued no responde")
                    time.sleep(1)
                apply_mode(pueue, datetime.now(timezone.utc), args.host)
            elif args.command == "guardia":
                guard(pueue, Path("/proc"), STATE_DIR, args.docker_bin, args.host)
            elif args.command == "reanudar":
                resume(pueue, Path("/proc"), STATE_DIR, args.group, args.docker_bin, args.host)
            else:
                state = load_control(STATE_DIR)
                state["owned"] = sorted(set(state["owned"]) - {args.group})
                save_control(STATE_DIR, state)
                pueue.call("pause", "--wait", "-g", args.group)
                log(STATE_DIR, "pausa_manual", grupo=args.group)
        except Exception as error:
            log(STATE_DIR, "error", detalle=str(error))
            raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
