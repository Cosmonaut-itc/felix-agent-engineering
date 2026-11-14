#!/usr/bin/python3 -I
"""Cuotas y guardia de Pueue 4.0.4; sólo biblioteca estándar."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time

# Reparto medido el 2026-11-13 (README, «Reparto»). `pesadas` y `jale-verify`
# sacan de `jale` las corridas de horas y las sesiones de verify.
GROUP_LIMITS = {
    "hasito": {"dia": 1, "noche": 2},
    "jale": {"dia": 5, "noche": 7},
    "jale-verify": {"dia": 1, "noche": 1},
    "otros": {"dia": 1, "noche": 1},
    "pesadas": {"dia": 2, "noche": 3},
    "pg5434": {"dia": 1, "noche": 1},
    "rapidas": {"dia": 3, "noche": 3},
}
# Sólo el desk: fleet/rack/snapshot.py lee pause y resume con su CPU.
PRESSURE_THRESHOLDS = {
    "pause": {"io": 40, "cpu": 30, "memory": 4 * 1024**3},
    "resume": {"io": 20, "cpu": 15, "memory": 6 * 1024**3},
}
MINI_THRESHOLDS = {
    "pause": {"load": 2.0, "memory": 2},
    "resume": {"load": 1.2, "memory": 1},
}
# El carril rápido es lo último en pausarse: en la Mini aguanta carga hasta 3,0
# por núcleo y en el desk sólo lo detiene la memoria. Reanuda con los demás.
RAPIDAS_THRESHOLDS = {
    "mini": {"pause": {**MINI_THRESHOLDS["pause"], "load": 3.0},
             "resume": MINI_THRESHOLDS["resume"]},
    "desk": {"pause": {"memory": PRESSURE_THRESHOLDS["pause"]["memory"]},
             "resume": {"memory": PRESSURE_THRESHOLDS["resume"]["memory"]}},
}
MINI_GROUPS = ("suites", "default", "rapidas")
# Perfil de cuotas de la Mini (lectura instalada del 2026-11-14 y README). Es
# independiente de los grupos que vigila la guardia: un grupo puede estar en
# uno y no en el otro. El tope es la suma del perfil y lo valida el host.
MINI_PROFILE = {"suites": 3, "default": 1, "rapidas": 3}
MINI_CAP = sum(MINI_PROFILE.values())
SLOT_MIN, SLOT_MAX = 1, 8
STORE = "control.sqlite3"
HOST_SCHEMA = "rack-host-control@1"
RESULT_SCHEMA = "rack-host-result@1"
ACTION_ID = re.compile(r"[A-Za-z0-9-]{8,64}")
GROUP_NAME = re.compile(r"[A-Za-z0-9_-]{1,32}")
# Sólo el panel, autorizado por whois del dueño, llega a este canal.
ACTOR = "rack-panel"
LOCK_WAIT = 4
RACK_PUEUE_TIMEOUT = 4
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
    def __init__(self, binary=None, timeout=None):
        self.binary = str(binary or Path.home() / ".local/bin/pueue")
        self.timeout = timeout

    def call(self, *args):
        return subprocess.run([self.binary, *map(str, args)], check=True,
                              text=True, capture_output=True, timeout=self.timeout).stdout

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


def pressure_mini():
    load = os.getloadavg()[0]
    cores = os.cpu_count()
    level = int(subprocess.run(
        ["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
        check=True, text=True, capture_output=True).stdout.strip())
    if not math.isfinite(load) or load < 0 or type(cores) is not int or cores < 1 or level not in (1, 2, 4):
        raise ValueError("Presión de la Mini inválida")
    return {"load": load / cores, "memory": level}


def decide(host, readings, thresholds):
    high, low = thresholds["pause"], thresholds["resume"]
    if host == "mini":
        if readings["load"] > high["load"] or readings["memory"] >= high["memory"]:
            return "pausar"
        if readings["load"] < low["load"] and readings["memory"] == low["memory"]:
            return "reanudar"
        return "mantener"
    # Un umbral sin E/S o CPU, como el del carril rápido, decide sólo por memoria.
    if readings["memory"] < high["memory"] or any(readings[k] > high[k] for k in ("io", "cpu") if k in high):
        return "pausar"
    if readings["memory"] >= low["memory"] and all(readings[k] < low[k] for k in ("io", "cpu") if k in low):
        return "reanudar"
    return "mantener"


def guard(pueue, proc, directory, *, host="desk"):
    readings = pressure_mini() if host == "mini" else pressure(proc)
    log(directory, "lectura", **readings)
    normal = decide(host, readings, MINI_THRESHOLDS if host == "mini" else PRESSURE_THRESHOLDS)
    rapid = decide(host, readings, RAPIDAS_THRESHOLDS[host])
    log(directory, "decision", accion=normal, accion_rapidas=rapid)
    target = directory / "guardia-state.json"
    owned = set(json.loads(target.read_text())) if target.exists() else set()
    managed_groups = MINI_GROUPS if host == "mini" else GROUP_LIMITS.keys()
    if not owned <= set(managed_groups):
        raise ValueError("Grupos desconocidos en estado del guardia")
    groups = pueue.status()["groups"]
    # La Mini abre grupos sólo con el deseo durable ya aplicado y verificado.
    # Sin autoridad legible se puede pausar, pero no abrir ni aumentar.
    can_open, failure = True, None
    if host == "mini":
        try:
            can_open = reconcile(pueue, directory, groups)
        except Exception as error:
            can_open, failure = False, error
            log(directory, "sin_reconciliar", detalle=f"{type(error).__name__}: {error}")
        groups = pueue.status()["groups"]
    for group in managed_groups:
        # Un grupo que no existe en el host no se gestiona ni se conserva como propio.
        if group not in groups:
            if group in owned:
                owned.remove(group)
                save_owned(directory, owned)
            continue
        status = groups[group]["status"]
        action = rapid if group == "rapidas" else normal
        # Si alguien ya arrancó un grupo, deja de pertenecer al guardia.
        if group in owned and status != "Paused":
            owned.remove(group)
            save_owned(directory, owned)
        if action == "pausar" and status == "Running":
            # La propiedad se escribe antes de la RPC: una caída entre ambas no deja
            # una pausa sin dueño. Si la RPC falla, el siguiente ciclo la ve Running y la suelta.
            owned.add(group)
            save_owned(directory, owned)
            pueue.call("pause", "--wait", "-g", group)
            log(directory, "pausado", grupo=group)
        elif action == "reanudar" and group in owned and status == "Paused" and can_open:
            # La propiedad se suelta sólo tras reanudar: si start falla, el siguiente ciclo reintenta.
            pueue.call("start", "-g", group)
            owned.remove(group)
            save_owned(directory, owned)
            log(directory, "reanudado", grupo=group)
    if failure is not None:
        raise failure


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def policy_hash(profile=None, cap=None):
    profile = MINI_PROFILE if profile is None else profile
    body = {"host": "mini", "profile": profile, "cap": MINI_CAP if cap is None else cap,
            "min": SLOT_MIN, "max": SLOT_MAX}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class StoreError(Exception):
    """Autoridad durable ausente o ilegible: nada se abre ni aumenta con ella."""

    def __init__(self, message, missing=False):
        super().__init__(message)
        self.code = "not_adopted" if missing else "unreadable"


class Store:
    """Deseo, revisión y recibos de la Mini en un SQLite, bajo control.lock.

    Una transacción guarda a la vez intención, deseo nuevo y revisión; no hay un
    segundo archivo cuya frontera haya que recuperar.
    """

    def __init__(self, path, readonly=False, create=False):
        if not create and not path.is_file():
            raise StoreError("falta control.sqlite3; adoptar con install-mini.py", missing=True)
        mode = "rwc" if create else "ro" if readonly else "rw"
        try:
            self.db = sqlite3.connect(f"file:{path}?mode={mode}", uri=True, timeout=LOCK_WAIT,
                                      isolation_level=None)
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA fullfsync=ON")
        except sqlite3.Error as error:
            raise StoreError(f"control.sqlite3 ilegible: {error}") from error
        self.path = path

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def create(self, desired):
        self.db.executescript(
            "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "CREATE TABLE desired(grp TEXT PRIMARY KEY, slots INTEGER NOT NULL,"
            " action_id TEXT, actor TEXT, at TEXT NOT NULL);"
            "CREATE TABLE host_actions(action_id TEXT PRIMARY KEY, digest TEXT NOT NULL,"
            " op TEXT NOT NULL, phase TEXT NOT NULL, before TEXT NOT NULL, target TEXT NOT NULL,"
            " undo_of TEXT, revision INTEGER NOT NULL, response TEXT NOT NULL,"
            " created TEXT NOT NULL, updated TEXT NOT NULL);")
        now = utcnow()
        with self.transaction():
            for key, value in (("schema", "pueue-host-control@1"), ("host", "mini"), ("revision", "0"),
                               ("policy_hash", policy_hash()), ("adopted_at", now)):
                self.db.execute("INSERT INTO meta VALUES (?,?)", (key, value))
            for group, slots in sorted(desired.items()):
                self.db.execute("INSERT INTO desired VALUES (?,?,NULL,'adopcion',?)", (group, slots, now))

    def transaction(self):
        db = self.db

        class Transaction:
            def __enter__(self):
                db.execute("BEGIN IMMEDIATE")

            def __exit__(self, kind, *rest):
                db.execute("COMMIT" if kind is None else "ROLLBACK")
        return Transaction()

    def view(self):
        """Lee y valida la autoridad completa; cualquier forma inesperada es ilegible."""
        try:
            meta = dict(self.db.execute("SELECT key,value FROM meta"))
            desired = dict(self.db.execute("SELECT grp,slots FROM desired"))
            revision = int(meta["revision"])
        except (sqlite3.Error, KeyError, ValueError) as error:
            raise StoreError(f"control.sqlite3 ilegible: {error}") from error
        if meta.get("schema") != "pueue-host-control@1" or meta.get("host") != "mini" or revision < 0:
            raise StoreError("control.sqlite3 de otro esquema u host")
        if meta.get("policy_hash") != policy_hash():
            raise StoreError("el perfil de la Mini cambió; adoptar de nuevo a mano")
        if set(desired) != set(MINI_PROFILE) or any(
                type(v) is not int or not SLOT_MIN <= v <= SLOT_MAX for v in desired.values()):
            raise StoreError("deseo durable fuera de 1..8 o con grupos ajenos")
        return {"revision": revision, "policy_hash": meta["policy_hash"], "desired": desired}

    def action(self, action_id):
        row = self.db.execute("SELECT digest,op,phase,before,target,undo_of,revision,response"
                              " FROM host_actions WHERE action_id=?", (action_id,)).fetchone()
        if row is None:
            return None
        keys = ("digest", "op", "phase", "before", "target", "undo_of", "revision", "response")
        value = dict(zip(keys, row))
        for key in ("before", "target", "response"):
            value[key] = json.loads(value[key])
        return value

    def open_action(self):
        row = self.db.execute("SELECT action_id,phase FROM host_actions WHERE phase IN ('applying','partial')"
                              " ORDER BY created LIMIT 1").fetchone()
        return None if row is None else {"action_id": row[0], "phase": row[1]}

    def begin(self, request, digest, before, target, response):
        """Intención, deseo y revisión en una sola transacción, antes de cualquier RPC."""
        now = utcnow()
        with self.transaction():
            revision = int(self.db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0]) + 1
            self.db.execute("UPDATE meta SET value=? WHERE key='revision'", (str(revision),))
            for group, slots in target.items():
                if slots != before[group]:
                    self.db.execute("UPDATE desired SET slots=?,action_id=?,actor=?,at=? WHERE grp=?",
                                    (slots, request["action_id"], ACTOR, now, group))
            response = dict(response, revision=revision)
            self.db.execute("INSERT INTO host_actions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (request["action_id"], digest, request["op"], "applying", canonical(before),
                             canonical(target), request.get("undo_of"), revision, canonical(response), now, now))
        return revision

    def finish(self, action_id, phase, response):
        with self.transaction():
            self.db.execute("UPDATE host_actions SET phase=?,response=?,updated=? WHERE action_id=?",
                            (phase, canonical(response), utcnow(), action_id))

    def close_open(self, observed):
        """El recuperador confirma intenciones abiertas cuyo deseo ya se observa."""
        closed = []
        for action_id, target, response in list(self.db.execute(
                "SELECT action_id,target,response FROM host_actions WHERE phase IN ('applying','partial')")):
            if json.loads(target) == observed:
                response = json.loads(response)
                response.update(status="applied", result=dict(response.get("result") or {}, observed=observed))
                self.finish(action_id, "verified", response)
                closed.append(action_id)
        return closed


def observed_slots(status, profile=None, partial=False):
    """Vector de parallel observado para el perfil. 0 es ilimitado en Pueue, no cero lugares."""
    profile = MINI_PROFILE if profile is None else profile
    observed = {}
    for group in profile:
        row = status["groups"].get(group)
        if row is None:
            if partial:
                continue
            raise ValueError(f"Falta el grupo {group} en Pueue")
        value = row.get("parallel_tasks")
        if type(value) is not int or value < 0:
            raise ValueError(f"parallel ilegible en {group}")
        observed[group] = value
    return observed


def exceeds(observed, limit):
    return observed == 0 or observed > limit


def converge(pueue, target, partial=False):
    """Reducciones antes que aumentos, cada etapa verificada con una lectura nueva.

    Bajar parallel no detiene lo que corre: sólo deja de admitir nuevas.
    """
    observed = observed_slots(pueue.status(), target, partial)
    for group in sorted(observed):
        if exceeds(observed[group], target[group]):
            pueue.parallel(group, target[group])
    observed = observed_slots(pueue.status(), target, partial)
    if any(exceeds(observed[g], target[g]) for g in observed):
        raise ValueError(f"Reducción sin confirmar: {observed}")
    for group in sorted(observed):
        if observed[group] < target[group]:
            pueue.parallel(group, target[group])
    return observed_slots(pueue.status(), target, partial)


def adopt(directory, status):
    """Primera adopción del perfil. Nunca reemplaza ni repara una autoridad existente."""
    path = directory / STORE
    if path.exists():
        with Store(path) as store:
            store.view()
        return False
    observed = observed_slots(status)
    if observed != MINI_PROFILE:
        raise ValueError(f"Cuotas observadas {observed} distintas del perfil {MINI_PROFILE}. "
                         "Fijarlas con /opt/homebrew/bin/pueue parallel N -g GRUPO antes de instalar")
    temporary = path.with_name(STORE + ".tmp")
    if temporary.exists():
        temporary.unlink()
    with Store(temporary, create=True) as store:
        store.create(observed)
    os.replace(temporary, path)
    return True


def reconcile(pueue, directory, groups):
    """Único recuperador de la Mini: lleva parallel al deseo durable y cierra recibos.

    Un grupo del perfil que no existe en Pueue no se crea ni bloquea a los demás.
    """
    with Store(directory / STORE) as store:
        desired = store.view()["desired"]
        observed = observed_slots({"groups": groups}, desired, partial=True)
        if any(observed[g] != desired[g] for g in observed):
            before = observed
            observed = converge(pueue, desired, partial=True)
            log(directory, "reconciliado", antes=before, deseado=desired, observado=observed)
        if any(observed[g] != desired[g] for g in observed):
            return False
        if set(observed) == set(desired):
            for action_id in store.close_open(observed):
                log(directory, "recibo_confirmado", action_id=action_id)
    return True


@contextmanager
def host_lock(directory, create):
    """control.lock con espera acotada; el ensayo no crea archivos."""
    path = directory / "control.lock"
    if not directory.is_dir() or not create and not path.exists():
        raise StoreError("sin instalación de control en este host", missing=True)
    handle = os.open(str(path), os.O_RDWR | (os.O_CREAT if create else 0), 0o600)
    try:
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("control.lock ocupado")
                time.sleep(0.05)
        yield
    finally:
        os.close(handle)


def reply(request, status, code=None, revision=None, result=None):
    return {"schema": RESULT_SCHEMA, "action_id": request.get("action_id"), "op": request.get("op"),
            "status": status, "revision": revision, "result": result,
            "error": None if code is None else {"code": code}}


def is_vector(value):
    return (type(value) is dict and set(value) == set(MINI_PROFILE)
            and all(type(v) is int and 0 <= v <= 1000 for v in value.values()))


def parse_request(raw):
    """Esquemas cerrados por operación; JSON sin claves repetidas ni NaN."""
    def unique(pairs):
        data = {}
        for key, value in pairs:
            if key in data:
                raise ValueError("clave repetida")
            data[key] = value
        return data

    def constant(value):
        raise ValueError("número no finito")
    if len(raw) > 16384:
        raise ValueError("petición demasiado grande")
    request = json.loads(raw, object_pairs_hook=unique, parse_constant=constant)
    if type(request) is not dict or request.get("schema") != HOST_SCHEMA:
        raise ValueError("esquema")
    op = request.get("op")
    keys = {"capabilities": {"schema", "op"}, "status": {"schema", "op", "action_id"},
            "slots": {"schema", "op", "action_id", "dry_run", "expected", "change"},
            "undo": {"schema", "op", "action_id", "dry_run", "expected", "undo_of"}}.get(op)
    if keys is None or set(request) != keys:
        raise ValueError("operación o claves")
    if op == "capabilities":
        return request
    if type(request["action_id"]) is not str or not ACTION_ID.fullmatch(request["action_id"]):
        raise ValueError("action_id")
    if op == "status":
        return request
    expected = request["expected"]
    if (type(request["dry_run"]) is not bool or type(expected) is not dict
            or set(expected) != {"revision", "policy_hash", "slots"}
            or type(expected["revision"]) is not int or expected["revision"] < 0
            or type(expected["policy_hash"]) is not str or not is_vector(expected["slots"])):
        raise ValueError("expected")
    if op == "slots":
        change = request["change"]
        if (type(change) is not dict or set(change) != {"group", "delta"}
                or change["group"] not in MINI_PROFILE or type(change["delta"]) is not int
                or change["delta"] not in (-1, 1)):
            raise ValueError("change")
    elif (type(request["undo_of"]) is not str or not ACTION_ID.fullmatch(request["undo_of"])
          or request["undo_of"] == request["action_id"]):
        raise ValueError("undo_of")
    return request


def check_slots(before, target, cap=MINI_CAP):
    """Reglas del host: 1..8 por grupo y suma <= tope.

    Si el vector previo ya excede el tope, sólo se acepta una reducción
    monotónica: ningún grupo sube y la suma baja.
    """
    for value in target.values():
        if value < SLOT_MIN:
            return "min"
        if value > SLOT_MAX:
            return "max"
    total, previous = sum(target.values()), sum(before.values())
    if total <= cap:
        return None
    if previous > cap and total < previous and all(target[g] <= before[g] for g in target):
        return None
    return "capacity"


def plan_of(before, target):
    return {"decreases": sorted(g for g in target if target[g] < before[g]),
            "increases": sorted(g for g in target if target[g] > before[g])}


def capabilities(pueue, directory):
    caps = {"slots": False, "undo": False, "dry_run": True, "mode": False, "reorder": False,
            "restart_queue": False}
    control = {"revision": None, "policy_hash": None, "cap": MINI_CAP, "min": SLOT_MIN, "max": SLOT_MAX,
               "desired": None, "observed": None, "pending": None, "reason": None}
    try:
        with Store(directory / STORE, readonly=True) as store:
            view = store.view()
            control.update(revision=view["revision"], policy_hash=view["policy_hash"],
                           desired=view["desired"], pending=store.open_action())
    except StoreError as error:
        control["reason"] = error.code
        return {"schema": RESULT_SCHEMA, "status": "ok", "capabilities": caps, "control": control}
    try:
        control["observed"] = observed_slots(pueue.status())
    except Exception:
        control["reason"] = "unavailable"
    if control["reason"] is None:
        control["reason"] = ("busy" if control["pending"] else
                             "drift" if control["observed"] != control["desired"] else None)
    caps["slots"] = caps["undo"] = control["reason"] is None
    return {"schema": RESULT_SCHEMA, "status": "ok", "capabilities": caps, "control": control}


def stored_status(directory, action_id):
    try:
        with Store(directory / STORE, readonly=True) as store:
            row = store.action(action_id)
    except StoreError:
        row = None
    if row is None:
        return reply({"action_id": action_id, "op": "status"}, "rejected", "not_found")
    return row["response"]


def mutate(request, pueue, directory):
    """slots/undo. El ensayo se bifurca antes de cualquier escritura o recuperación."""
    dry = request["dry_run"]
    digest = hashlib.sha256(canonical(request).encode()).hexdigest()
    with host_lock(directory, create=not dry), Store(directory / STORE, readonly=dry) as store:
        # Primero la autoridad completa: un almacén ilegible no llega a ninguna consulta.
        view = store.view()
        if not dry:
            row = store.action(request["action_id"])
            if row is not None:
                # Mismo id y cuerpo: el mismo recibo. Nunca una segunda aplicación.
                return row["response"] if row["digest"] == digest else reply(
                    request, "rejected", "idempotency_conflict")
        if store.open_action():
            return reply(request, "rejected", "busy", view["revision"])
        try:
            observed = observed_slots(pueue.status())
        except Exception:
            return reply(request, "rejected", "unavailable", view["revision"])
        fresh = {"before": observed, "desired": view["desired"], "observed": observed}
        if observed != view["desired"]:
            return reply(request, "conflict", "drift", view["revision"], fresh)
        expected = request["expected"]
        if (expected["revision"] != view["revision"] or expected["policy_hash"] != view["policy_hash"]
                or expected["slots"] != observed):
            return reply(request, "conflict", "stale_state", view["revision"], fresh)
        if request["op"] == "slots":
            target = dict(observed)
            target[request["change"]["group"]] += request["change"]["delta"]
        else:
            original = store.action(request["undo_of"])
            # Deshacer exacto: sólo la última acción verificada, con su vector intacto.
            if (original is None or original["op"] != "slots" or original["phase"] != "verified"
                    or original["revision"] != view["revision"] or original["target"] != observed):
                return reply(request, "conflict", "undo_conflict", view["revision"], fresh)
            target = original["before"]
        code = check_slots(observed, target)
        if code:
            return reply(request, "rejected", code, view["revision"], fresh)
        result = {"before": observed, "desired": target, "observed": observed, "plan": plan_of(observed, target)}
        if dry:
            return reply(request, "dry_run", None, view["revision"], result)
        result.pop("plan")
        if request["op"] == "undo":
            result["undo_of"] = request["undo_of"]
        revision = store.begin(request, digest, observed, target, reply(request, "applying", None, None, result))
        try:
            final = converge(pueue, target)
        except Exception:
            try:
                final = observed_slots(pueue.status())
            except Exception:
                final = None
        status = "applied" if final == target else "partial"
        response = reply(request, status, None, revision, dict(result, observed=final))
        store.finish(request["action_id"], "verified" if status == "applied" else "partial", response)
        return response


def rack(raw, pueue, directory):
    """Una petición rack-host-control@1, una respuesta JSON. No propaga excepciones."""
    try:
        request = parse_request(raw)
    except (ValueError, TypeError, RecursionError):
        return reply({}, "rejected", "invalid_request")
    try:
        if request["op"] == "capabilities":
            return capabilities(pueue, directory)
        if request["op"] == "status":
            return stored_status(directory, request["action_id"])
        return mutate(request, pueue, directory)
    except StoreError as error:
        return reply(request, "rejected", error.code)
    except TimeoutError:
        return reply(request, "rejected", "busy")
    except Exception:
        # Tras la intención el efecto puede haber ocurrido: el recibo se consulta, no se repite.
        return reply(request, "unknown")


def live_tasks(state):
    # Los enums con datos de serde son objetos, p.ej. {"Running": {...}}.
    # Sólo estados terminales conocidos permiten relevar un daemon manual.
    return [task for task in state["tasks"].values()
            if task["status"] != "Done" and not (
                isinstance(task["status"], dict) and set(task["status"]) == {"Done"})]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["aplicar", "guardia", "pausar", "reanudar", "rack"])
    parser.add_argument("group", nargs="?")
    parser.add_argument("--host", choices=["desk", "mini"], default="desk")
    args = parser.parse_args()
    groups = MINI_GROUPS if args.host == "mini" else GROUP_LIMITS
    if args.group is not None and args.group not in groups:
        parser.error("grupo desconocido para el host")
    if args.host == "mini" and args.command == "aplicar":
        parser.error("La Mini sólo admite guardia y pausa manual")
    if args.host != "mini" and args.command in ("reanudar", "rack"):
        parser.error(f"{args.command} sólo existe en la Mini")
    pueue = Pueue("/opt/homebrew/bin/pueue") if args.host == "mini" else Pueue()
    directory = Path.home() / ".local/state/pao/pueue-mini" if args.host == "mini" else STATE_DIR
    if args.command == "rack":
        # Canal del panel: toma control.lock por su cuenta y nunca crea la instalación.
        pueue.timeout = RACK_PUEUE_TIMEOUT
        print(json.dumps(rack(sys.stdin.read(16385), pueue, directory), ensure_ascii=False, allow_nan=False))
        return
    with locked(directory):
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
                guard(pueue, Path("/proc"), directory, host=args.host)
            elif args.command == "reanudar":
                # Pausa ambigua tras un reinicio o pausa manual: la guardia la toma y la
                # abre sólo con presión baja y el deseo de cuotas ya aplicado.
                if args.group is None:
                    parser.error("reanudar necesita un grupo")
                target = directory / "guardia-state.json"
                owned = set(json.loads(target.read_text())) if target.exists() else set()
                if pueue.status()["groups"][args.group]["status"] == "Paused":
                    owned.add(args.group)
                    save_owned(directory, owned)
                    log(directory, "relevo_a_guardia", grupo=args.group)
                guard(pueue, Path("/proc"), directory, host=args.host)
            else:
                if args.group is None:
                    parser.error("pausar necesita un grupo")
                target = directory / "guardia-state.json"
                owned = set(json.loads(target.read_text())) if target.exists() else set()
                owned.discard(args.group)
                save_owned(directory, owned)
                pueue.call("pause", "--wait", "-g", args.group)
                log(directory, "pausa_manual", grupo=args.group)
        except Exception as error:
            log(directory, "error", detalle=str(error))
            raise


if __name__ == "__main__":
    main()
