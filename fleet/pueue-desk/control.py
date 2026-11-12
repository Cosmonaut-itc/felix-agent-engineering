#!/usr/bin/python3 -I
"""Cuotas y guardia de Pueue 4.0.4; sólo biblioteca estándar."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import math
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
                              text=True, capture_output=True).stdout

    def status(self):
        state = json.loads(self.call("status", "--json"))
        if not isinstance(state.get("tasks"), dict) or not isinstance(state.get("groups"), dict):
            raise ValueError("Estado Pueue incompleto")
        return state

    def parallel(self, group, value):
        self.call("parallel", validate_limit(value), "-g", group)


def apply_mode(pueue, now):
    mode = mode_at(now)
    limits = {group: validate_limit(row[mode]) for group, row in GROUP_LIMITS.items()}
    state = pueue.status()
    for group, limit in limits.items():
        if group not in state["groups"]:
            pueue.call("group", "add", group, "--parallel", limit)
        pueue.parallel(group, limit)
    state = pueue.status()
    for group, limit in limits.items():
        if state["groups"][group]["parallel_tasks"] != limit:
            raise ValueError(f"Cuota incorrecta: {group}")
    print(f"Modo {mode}: " + ", ".join(f"{g}={n}" for g, n in limits.items()))


@contextmanager
def locked(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "control.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def save_owned(directory, owned):
    target = directory / "guardia-state.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(sorted(owned)) + "\n")
    temporary.replace(target)


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


def guard(pueue, proc, directory):
    readings = pressure(proc)
    log(directory, "lectura", **readings)
    high, low = PRESSURE_THRESHOLDS["pause"], PRESSURE_THRESHOLDS["resume"]
    action = "mantener"
    if readings["io"] > high["io"] or readings["cpu"] > high["cpu"] or readings["memory"] < high["memory"]:
        action = "pausar"
    elif readings["io"] < low["io"] and readings["cpu"] < low["cpu"] and readings["memory"] >= low["memory"]:
        action = "reanudar"
    log(directory, "decision", accion=action)
    target = directory / "guardia-state.json"
    owned = set(json.loads(target.read_text())) if target.exists() else set()
    if not owned <= GROUP_LIMITS.keys():
        raise ValueError("Grupos desconocidos en estado del guardia")
    groups = pueue.status()["groups"]
    for group in GROUP_LIMITS:
        status = groups[group]["status"]
        # Si alguien ya arrancó un grupo, deja de pertenecer al guardia.
        if group in owned and status != "Paused":
            owned.remove(group)
            save_owned(directory, owned)
        if action == "pausar" and status == "Running":
            pueue.call("pause", "--wait", "-g", group)
            owned.add(group)
            save_owned(directory, owned)
            log(directory, "pausado", grupo=group)
        elif action == "reanudar" and group in owned and status == "Paused":
            # Quitar propiedad antes de arrancar: un fallo nunca amplía el permiso.
            owned.remove(group)
            save_owned(directory, owned)
            pueue.call("start", "-g", group)
            log(directory, "reanudado", grupo=group)


def live_tasks(state):
    # Los enums con datos de serde son objetos, p.ej. {"Running": {...}}.
    # Sólo estados terminales conocidos permiten relevar un daemon manual.
    return [task for task in state["tasks"].values()
            if task["status"] != "Done" and not (
                isinstance(task["status"], dict) and set(task["status"]) == {"Done"})]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["aplicar", "guardia", "pausar"])
    parser.add_argument("group", nargs="?", choices=list(GROUP_LIMITS))
    args = parser.parse_args()
    pueue = Pueue()
    with locked(STATE_DIR):
        try:
            if args.command == "aplicar":
                for attempt in range(30):
                    try:
                        pueue.status()
                        break
                    except subprocess.CalledProcessError:
                        if attempt == 29:
                            raise
                        time.sleep(1)
                apply_mode(pueue, datetime.now(timezone.utc))
            elif args.command == "guardia":
                guard(pueue, Path("/proc"), STATE_DIR)
            else:
                if args.group is None:
                    parser.error("pausar necesita un grupo")
                target = STATE_DIR / "guardia-state.json"
                owned = set(json.loads(target.read_text())) if target.exists() else set()
                owned.discard(args.group)
                save_owned(STATE_DIR, owned)
                pueue.call("pause", "--wait", "-g", args.group)
                log(STATE_DIR, "pausa_manual", grupo=args.group)
        except Exception as error:
            log(STATE_DIR, "error", detalle=str(error))
            raise


if __name__ == "__main__":
    main()
