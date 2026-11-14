#!/usr/bin/python3 -I
"""Cuotas y guardia de Pueue 4.0.4; sólo biblioteca estándar."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sqlite3
import re
import sys
import time

# Reparto medido el 2026-11-13 (README, «Reparto»). `pesadas` y `jale-verify`
# sacan de `jale` las corridas de horas y las sesiones de verify.
GROUP_LIMITS = {
    "hasito": {"dia": 1, "noche": 2},
    "jale": {"dia": 5, "noche": 7},
    "jale-verify": {"dia": 1, "noche": 1},
    "otros": {"dia": 2, "noche": 2},
    "pesadas": {"dia": 2, "noche": 3},
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
MINI_PROFILE = {"suites": 3, "default": 1, "rapidas": 3}
POLICY_HASH = hashlib.sha256(json.dumps(MINI_PROFILE, sort_keys=True).encode()).hexdigest()
DESK_POLICY_HASH = hashlib.sha256(json.dumps(dict(groups=GROUP_LIMITS, events_utc=[5, 14]),
                                           sort_keys=True).encode()).hexdigest()
HOST_SCHEMA = 'rack-host-control@1'
ID = re.compile(r'[A-Za-z0-9-]{8,64}')
LOG_MAX_BYTES = 1024 * 1024
STATE_DIR = Path.home() / ".local/state/pao/pueue-desk"
RACK_LOCK_SECONDS = 1.0


def mode_at(now):
    if now.tzinfo is None:
        raise ValueError("La hora debe incluir zona horaria")
    return "noche" if 5 <= now.astimezone(timezone.utc).hour < 14 else "dia"


def schedule_at(timestamp):
    """Last/next event identities, even when a timer missed an entire day."""
    now = datetime.fromtimestamp(timestamp, timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    events = sorted((midnight + timedelta(days=d, hours=h)).timestamp()
                    for d in (-1, 0, 1) for h in (5, 14))
    return max(t for t in events if t <= timestamp), min(t for t in events if t > timestamp)


def mode_profile(mode):
    return {g: row[mode] for g, row in GROUP_LIMITS.items()}


def desk_profile(timestamp):
    return mode_profile(mode_at(datetime.fromtimestamp(timestamp, timezone.utc)))


def mexico_time(timestamp):
    return datetime.fromtimestamp(timestamp, ZoneInfo('America/Mexico_City')).strftime('%H:%M')


def mode_notes(state, mode):
    # The approved return time describes the next visible change, not expiry.
    event = schedule_at(state['schedule_event'])[1]
    while mode_at(datetime.fromtimestamp(event, timezone.utc)) == mode:
        event = schedule_at(event)[1]
    label = 'día' if mode == 'noche' else 'noche'
    advanced = state['mode_override'] is not None
    note = ('Adelantado · vuelve a ' if advanced else 'Automático · cambia a ') + label + ' a las ' + mexico_time(event)
    expires = schedule_at(state['schedule_event'])[1]
    return dict(mode_note=note,
                mode_detail='Ajustes manuales hasta las ' + mexico_time(expires) if advanced else None,
                slots_note='Lugares ajustados · hasta las ' + mexico_time(expires) if state['overrides'] else None,
                mode_pending={m:'Aplicando horario de ' + ('día' if m == 'dia' else 'noche') + '…' for m in ('dia','noche')},
                mode_partial='Horario aplicado parcialmente · revisando…',
                mode_unknown='Cambio sin confirmar · consultando…')


def validate_limit(value):
    if type(value) is not int or not 1 <= value <= 8:
        raise ValueError(f"parallel fuera de 1..8: {value!r}")
    return value


class Pueue:
    def __init__(self, binary=None):
        self.binary = str(binary or Path.home() / ".local/bin/pueue")
        self.timeout = None

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


def wait_rack_lock(handle):
    """A short tick may finish; expiry before entry certifies no effects."""
    deadline = time.monotonic() + RACK_LOCK_SECONDS
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(.02, remaining))


def save_owned(directory, owned):
    target = directory / "guardia-state.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(sorted(owned)) + "\n")
    temporary.replace(target)


def has_authority(directory):
    # Broken symlinks are unreadable authority, never permission to fall back.
    return any(os.path.lexists(directory / name) for name in ('control.adopted', 'control.sqlite3'))


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
    if has_authority(directory):
        engine = MiniControl(pueue, directory) if host == 'mini' else MiniControl(pueue, directory, host='desk')
        return engine.guard(readings, proc=proc)
    normal = decide(host, readings, MINI_THRESHOLDS if host == "mini" else PRESSURE_THRESHOLDS)
    rapid = decide(host, readings, RAPIDAS_THRESHOLDS[host])
    log(directory, "decision", accion=normal, accion_rapidas=rapid)
    target = directory / "guardia-state.json"
    owned = set(json.loads(target.read_text())) if target.exists() else set()
    managed_groups = MINI_GROUPS if host == "mini" else GROUP_LIMITS.keys()
    if not owned <= set(managed_groups):
        raise ValueError("Grupos desconocidos en estado del guardia")
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
            pueue.call("pause", "--wait", "-g", group)
            owned.add(group)
            save_owned(directory, owned)
            log(directory, "pausado", grupo=group)
        elif action == "reanudar" and group in owned and status == "Paused":
            # La propiedad se suelta sólo tras reanudar: si start falla, el siguiente ciclo reintenta.
            pueue.call("start", "-g", group)
            owned.remove(group)
            save_owned(directory, owned)
            log(directory, "reanudado", grupo=group)


def live_tasks(state):
    # Los enums con datos de serde son objetos, p.ej. {"Running": {...}}.
    # Sólo estados terminales conocidos permiten relevar un daemon manual.
    return [task for task in state["tasks"].values()
            if task["status"] != "Done" and not (
                isinstance(task["status"], dict) and set(task["status"]) == {"Done"})]


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def validate_host_request(request, host='mini'):
    if type(request) is not dict or request.get('schema') != HOST_SCHEMA:
        raise ValueError('Petición inválida')
    op = request.get('op')
    keys = {'schema', 'op'}
    if op in ('slots', 'mode', 'undo', 'status'):
        keys.add('action_id')
        if type(request.get('action_id')) is not str or not ID.fullmatch(request['action_id']):
            raise ValueError('ID inválido')
    if op in ('slots', 'mode', 'undo'):
        keys |= {'dry_run', 'expected', 'change' if op in ('slots', 'mode') else 'undo_of'}
        if type(request.get('dry_run')) is not bool:
            raise ValueError('Ensayo inválido')
        e = request.get('expected')
        if (type(e) is not dict or set(e) != {'revision', 'policy_hash', 'generation', 'mode', 'slots'}
                or type(e['revision']) is not int or e['revision'] < 1
                or type(e['generation']) is not str or not 1 <= len(e['generation']) <= 160
                or type(e['policy_hash']) is not str or e['mode'] not in (('fijo',) if host == 'mini' else ('dia', 'noche'))):
            raise ValueError('Estado esperado inválido')
        validate_vector(e['slots'], host)
        if op == 'slots':
            c = request.get('change')
            if (type(c) is not dict or set(c) != {'group', 'delta'}
                    or type(c['group']) is not str or c['group'] not in (MINI_PROFILE if host == 'mini' else GROUP_LIMITS)
                    or type(c['delta']) is not int or c['delta'] not in (-1, 1)):
                raise ValueError('Cambio inválido')
        elif op == 'mode':
            c = request.get('change')
            if type(c) is not dict or set(c) != {'mode'} or c['mode'] not in ('dia', 'noche'):
                raise ValueError('Modo inválido')
        elif type(request.get('undo_of')) is not str or not ID.fullmatch(request['undo_of']):
            raise ValueError('Recibo inválido')
    elif op not in ('capabilities', 'status'):
        raise ValueError('Operación no disponible')
    if set(request) != keys:
        raise ValueError('Claves inválidas')
    return request


def validate_vector(slots, host='mini'):
    if type(slots) is not dict or set(slots) != set(MINI_PROFILE if host == 'mini' else GROUP_LIMITS):
        raise ValueError('Vector incompleto')
    for value in slots.values():
        validate_limit(value)
    return slots


def validate_target(before, target, host='mini', cap=7):
    validate_vector(before, host)
    validate_vector(target, host)
    monotonic = sum(target.values()) < sum(before.values()) and all(target[g] <= before[g] for g in before)
    if sum(target.values()) > cap and not monotonic:
        raise ValueError(f'Tope de {cap} lugares. Quita un lugar a otro grupo para añadirlo aquí.')


def slot_plan(before, target):
    # Reductions must be verified before any increase, including undo/recovery.
    return [(g, target[g]) for g in sorted(target) if target[g] < before[g]] + [
        (g, target[g]) for g in sorted(target) if target[g] > before[g]]


def mini_generation(pid_file):
    # Adoption pins the daemon's configured pid file; no process-name search.
    pid = pid_file.read_text().strip()
    if not pid.isdigit() or int(pid) < 1:
        raise ValueError('PID inválido')
    identity = subprocess.run(['/bin/ps', '-p', pid, '-o', 'lstart=', '-o', 'comm='],
                              check=True, text=True, capture_output=True, timeout=2,
                              env=dict(os.environ, LC_ALL='C')).stdout.strip()
    if not identity or Path(identity.split()[-1]).name != 'pueued':
        raise ValueError('Daemon no identificado')
    return hashlib.sha256((pid + ':' + str(pid_file.stat().st_mtime_ns) + ':' + identity).encode()).hexdigest()


class MiniControl:
    """Caller holds control.lock. Queries/ensayos open SQLite read-only, never recover.

    One FULL transaction accepts both desired policy and intent. RPCs run outside
    transactions; a crashed worker leaves the same durable target for guardia.
    """
    def __init__(self, pueue, directory, clock=time.time, generation=None, host='mini'):
        if host not in ('mini', 'desk'):
            raise ValueError('Host inválido')
        self.host = host
        self.profile = MINI_PROFILE if host == 'mini' else GROUP_LIMITS
        self.policy_hash = POLICY_HASH if host == 'mini' else DESK_POLICY_HASH
        self.pueue, self.directory, self.clock = pueue, Path(directory), clock
        self.path = self.directory / 'control.sqlite3'
        self.generation_source = generation

    def mode(self, state):
        return 'fijo' if self.host == 'mini' else (state['mode_override']['mode'] if state['mode_override'] else
                mode_at(datetime.fromtimestamp(state['schedule_event'], timezone.utc)))

    def cap(self, state):
        return 7 if self.host == 'mini' else sum(mode_profile(self.mode(state)).values())

    def resolve(self, state, now=None):
        # Pure projection: capabilities and ensayo must never recover or write.
        if self.host == 'desk':
            event, _ = schedule_at(self.clock() if now is None else now)
            if event < state['schedule_event']:
                raise ValueError('Reloj anterior al evento durable')
            if event != state['schedule_event']:
                state = dict(state, schedule_event=event, mode_override=None, overrides={}, desired=desk_profile(event),
                             revision=state['revision'] + 1)
        return state

    @contextmanager
    def connect(self, write=False):
        # mode=rw is deliberate: a missing authority can never become a fresh DB.
        db = sqlite3.connect(self.path.as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=.2)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA fullfsync=ON')
            with db:
                yield db
        finally:
            db.close()

    def state(self, db, policy_migration=False):
        row = db.execute('SELECT body FROM policy WHERE id=1').fetchone()
        value = json.loads(row[0])
        migrating = policy_migration and self.host == 'desk' and value['policy_hash'] != self.policy_hash
        if ((value['policy_hash'] != self.policy_hash and not migrating) or type(value['revision']) is not int
                or value['revision'] < 1 or type(value['reasons']) is not dict):
            raise ValueError('Autoridad inválida')
        if (type(value['policy_hash']) is not str or not re.fullmatch(r'[0-9a-f]{64}', value['policy_hash'])
                or type(value['generation']) is not str or not value['generation']
                or type(value['pid_file']) is not str or not Path(value['pid_file']).is_absolute()
                or value['actor'] != 'Felix' or type(value['at']) not in (int, float)
                or not math.isfinite(value['at'])):
            raise ValueError('Autoridad inválida')
        validate_vector(value['desired'], self.host)
        overrides = value['overrides']
        if type(overrides) is not dict or not set(overrides) <= set(self.profile):
            raise ValueError('Ajustes inválidos')
        calculated = dict(MINI_PROFILE)
        if self.host == 'desk':
            event = value.get('schedule_event')
            if type(event) not in (int, float) or not math.isfinite(event) or schedule_at(event)[0] != event:
                raise ValueError('Evento inválido')
            if policy_migration and 'mode_override' not in value:
                value['mode_override'] = None
            advance = value['mode_override']
            if advance is not None:
                if (type(advance) is not dict or set(advance) != {'mode', 'actor', 'action_id', 'requested_at', 'expires_at'}
                        or advance['mode'] not in ('dia', 'noche') or advance['actor'] != 'Felix'
                        or type(advance['action_id']) is not str or not ID.fullmatch(advance['action_id'])
                        or type(advance['requested_at']) not in (int, float) or not math.isfinite(advance['requested_at'])
                        or advance['expires_at'] != schedule_at(event)[1]
                        or not event <= advance['requested_at'] < advance['expires_at']
                        or advance['mode'] == mode_at(datetime.fromtimestamp(event, timezone.utc))):
                    raise ValueError('Adelanto inválido')
            calculated = mode_profile(self.mode(value))
        for g, item in overrides.items():
            if (type(item) is not dict or set(item) != {'slots', 'actor', 'action_id', 'requested_at', 'expires_at'}
                    or item['actor'] != 'Felix'
                    or (item['expires_at'] is not None if self.host == 'mini' else
                        item['expires_at'] != schedule_at(value['schedule_event'])[1])
                    or type(item['action_id']) is not str or not ID.fullmatch(item['action_id'])
                    or type(item['requested_at']) not in (int, float) or not math.isfinite(item['requested_at'])):
                raise ValueError('Ajuste inválido')
            if self.host == 'desk' and not value['schedule_event'] <= item['requested_at'] < item['expires_at']:
                raise ValueError('Ajuste fuera de su evento')
            calculated[g] = validate_limit(item['slots'])
        if calculated != value['desired'] and not migrating:
            raise ValueError('Deseo inconsistente')
        # Ownership is independent of the quota profile (including foreign groups).
        if any(type(g) is not str or type(r) is not list or not set(r) <= {'pressure', 'manual', 'ambiguous'}
               for g, r in value['reasons'].items()):
            raise ValueError('Propiedad inválida')
        return value

    def store(self, db, state):
        db.execute('UPDATE policy SET body=? WHERE id=1', (canonical(state),))

    def generation(self, state):
        return self.generation_source() if self.generation_source else mini_generation(Path(state['pid_file']))

    def disabled(self):
        for target in (self.directory / 'slots.disabled', self.directory / 'maintenance', self.directory.parent / 'rack/actions.disabled'):
            try:
                target.lstat()
            except FileNotFoundError:
                continue
            return True
        return False

    def observed(self):
        snapshot = self.pueue.status()
        groups = snapshot['groups']
        # Publish only a count, never task identities, commands or environments.
        self.foreign_work = (sum(task['group'] not in self.profile for task in live_tasks(snapshot))
                             if self.host == 'desk' else 0)
        vector = {g: groups[g]['parallel_tasks'] for g in self.profile}
        validate_vector(vector, self.host)
        if any(groups[g]['status'] not in ('Running', 'Paused') for g in self.profile):
            raise ValueError('Admisión no disponible')
        return vector, groups

    def adopt(self, pid_file=None):
        if has_authority(self.directory):
            raise ValueError('La autoridad ya fue adoptada; no se reinicializa')
        observed, groups = self.observed()
        now = self.clock()
        profile = MINI_PROFILE if self.host == 'mini' else desk_profile(now)
        if observed != profile:
            raise ValueError('La adopción requiere la tabla verificada del host y horario vigente')
        pid_file = pid_file or (Path.home() / 'Library/Application Support/pueue/pueue.pid' if self.host == 'mini'
                               else Path.home() / '.local/share/pueue/pueue.pid')
        state = dict(policy_hash=self.policy_hash, revision=1, desired=observed, overrides={},
                     reasons={}, generation='', pid_file=str(Path(pid_file).resolve()), actor='Felix', at=self.clock())
        if self.host == 'desk':
            state['schedule_event'] = schedule_at(now)[0]
            state['mode_override'] = None
        state['generation'] = self.generation(state)
        old = self.directory / 'guardia-state.json'
        owned = set(json.loads(old.read_text())) if old.exists() else set()
        if any(type(g) is not str for g in owned):
            raise ValueError('Propiedad anterior inválida')
        for g, row in groups.items():
            if row['status'] == 'Paused':
                state['reasons'][g] = ['pressure' if g in owned and g in self.profile else 'manual']
        for g in owned - groups.keys():
            state['reasons'][g] = ['manual']
        # Record intent to adopt before creating DB; missing/corrupt DB stays closed.
        marker = self.directory / 'control.adopted'
        with marker.open('x') as stream:
            stream.write('pueue-host-control@1\n'); stream.flush(); os.fsync(stream.fileno())
        folder = os.open(str(self.directory), os.O_RDONLY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
        db = sqlite3.connect(str(self.path))
        try:
            db.execute('PRAGMA synchronous=FULL'); db.execute('PRAGMA fullfsync=ON')
            with db:
                db.execute('CREATE TABLE policy (id INTEGER PRIMARY KEY, body TEXT NOT NULL)')
                db.execute('CREATE TABLE host_actions (action_id TEXT PRIMARY KEY, digest TEXT NOT NULL, body TEXT NOT NULL)')
                db.execute('INSERT INTO policy VALUES (1,?)', (canonical(state),))
        finally:
            db.close()
        self.path.chmod(0o600)
        folder = os.open(str(self.directory), os.O_RDONLY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
        if old.exists():
            old.replace(self.directory / 'guardia-state.imported.json')
        return self.view()

    def view(self):
        with self.connect() as db:
            state = self.resolve(self.state(db))
            pending = [json.loads(r[0]) for r in db.execute('SELECT body FROM host_actions')
                       if json.loads(r[0])['response']['status'] in ('applying', 'partial', 'unknown')]
        observed, groups = self.observed()
        generation = self.generation(state)
        for group in self.profile:
            if os.path.lexists(self.pause_fence(group)):
                state['reasons'][group] = sorted(set(state['reasons'].get(group, [])) | {'manual'})
        enabled = not self.disabled()
        extra = dict(expires_at=schedule_at(state['schedule_event'])[1] if state['overrides'] or state['mode_override'] else None,
                     next_event=schedule_at(state['schedule_event'])[1], schedule_event=state['schedule_event'],
                     **mode_notes(state, self.mode(state))) if self.host == 'desk' else {}
        return dict(schema='rack-host-result@1', status='ok', capabilities=dict(
            slots=enabled, undo=enabled, dry_run=enabled, mode=enabled and self.host == 'desk', reorder=False, restart_queue=False),
            revision=state['revision'], policy_hash=self.policy_hash, generation=generation, mode=self.mode(state), cap=self.cap(state),
            slots=observed, desired=state['desired'], drift=observed != state['desired'],
            **(dict(foreign_work=self.foreign_work) if self.host == 'desk' else {}),
            reason=None if enabled else 'Los ajustes de lugares están apagados en este host.',
            recovery_pending=bool(pending) or generation != state['generation'],
            pause_reasons=state['reasons'], pending_actions=[a['request']['action_id'] for a in pending], **extra)

    def reply(self, request, status, code=None, **fields):
        messages = {'stale_state': 'Los lugares cambiaron. Actualiza antes de decidir.',
                    'busy': 'Hay un ajuste pendiente; consulta su recibo.',
                    'lock_busy': 'El control está ocupado. Vuelve a intentarlo.',
                    'idempotency_conflict': 'Ese ID ya pertenece a otra petición.',
                    'undo_conflict': 'El horario o los lugares cambiaron; no se puede deshacer este ajuste',
                    'expired': 'El ajuste venció en el cambio de horario.',
                    'same_mode': 'Ese horario ya está vigente.',
                    'invalid_request': 'Ajuste inválido: cada grupo admite 1..8 y se respeta el tope del modo.',
                    'recovery_error': 'Estado de control ilegible o no disponible. Se conserva la pausa.',
                    'disabled': 'Los ajustes de lugares están apagados en este host.',
                    'foreign_work': 'Hay trabajo fuera del reparto. Resuélvelo antes de añadir lugares.',
                    'unsupported': 'Falta actualizar el control de este host.'}
        return dict(schema='rack-host-result@1', action_id=request.get('action_id'), status=status,
                    error=dict(code=code, message=messages[code]) if code else None, **fields)

    def handle(self, request):
        validate_host_request(request, self.host)
        if self.host == 'desk' and not has_authority(self.directory):
            return self.reply(request, 'unknown' if request['op'] == 'status' else 'unsupported',
                              'unsupported', effects=False)
        try:
            response = self._handle(request)
            if request['op'] in ('slots', 'mode', 'undo') and not request['dry_run']:
                # Rejected IDs are bound too; a later refresh cannot turn a retry
                # of the same ID into a previously unaccepted effect.
                digest = hashlib.sha256(canonical(dict(host=self.host, request=request)).encode()).hexdigest()
                if (response.get('error') or {}).get('code') != 'idempotency_conflict':
                    response['receipt_digest'] = digest
                with self.connect(write=True) as db:
                    db.execute('INSERT OR IGNORE INTO host_actions VALUES (?,?,?)',
                               (request['action_id'], digest, canonical(dict(request=request, response=response))))
            return response
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, ArithmeticError, subprocess.SubprocessError):
            uncertain = request['op'] == 'status' or request['op'] in ('slots', 'mode', 'undo') and not request['dry_run']
            return self.reply(request, 'unknown' if uncertain else 'recovery_error', 'recovery_error')

    def _handle(self, request):
        op = request['op']
        # Bifurcate BEFORE recovery, DB mutation, pruning or generation adoption.
        if op == 'capabilities':
            return self.view()
        if op == 'status':
            with self.connect() as db:
                row = db.execute('SELECT digest,body FROM host_actions WHERE action_id=?', (request['action_id'],)).fetchone()
            return dict(json.loads(row[1])['response'], receipt_digest=row[0]) if row else self.reply(request, 'unknown')
        digest = hashlib.sha256(canonical(dict(host=self.host, request=request)).encode()).hexdigest()
        with self.connect() as db:
            row = db.execute('SELECT digest,body FROM host_actions WHERE action_id=?', (request['action_id'],)).fetchone()
            if row:
                return (self.reply(request, 'conflict', 'idempotency_conflict') if row[0] != digest
                        else json.loads(row[1])['response'])
            if op == 'mode' and self.host != 'desk':
                return self.reply(request, 'unsupported', 'unsupported', effects=False)
            if self.disabled():
                return self.reply(request, 'rejected', 'disabled', effects=False)
            if op == 'mode' and request['change']['mode'] == self.mode(self.resolve(self.state(db))):
                return self.reply(request, 'rejected', 'same_mode', effects=False)
            if self.host == 'desk' and op in ('slots', 'mode') and not request['dry_run']:
                stored = self.state(db)
                if self.resolve(stored) != stored:
                    # A missed timer is recovered by a real ordinary click too.
                    # Undo and all pure entries deliberately bypass this path.
                    self.reconcile()
                    view = self.view()
                    return self.reply(request, 'conflict', 'stale_state', control=view, effects=True,
                                      schedule_recovery=view['schedule_event'])
            state = self.resolve(self.state(db))
            if op == 'undo':
                prior = db.execute('SELECT body FROM host_actions WHERE action_id=?', (request['undo_of'],)).fetchone()
                prior = json.loads(prior[0]) if prior else None
                if (self.host == 'desk' and prior and
                        prior.get('schedule_event') != state['schedule_event']):
                    return self.reply(request, 'conflict', 'undo_conflict', effects=False)
            before, _ = self.observed()
            expected = dict(revision=state['revision'], policy_hash=self.policy_hash,
                            generation=self.generation(state), mode=self.mode(state), slots=before)
            if (request['expected'] != expected or expected['generation'] != state['generation']
                    or before != state['desired']):
                return self.reply(request, 'conflict', 'undo_conflict' if op == 'undo' else 'stale_state', control=self.view(), effects=False)
            if any(json.loads(r[0])['response']['status'] in ('applying', 'partial', 'unknown')
                   for r in db.execute('SELECT body FROM host_actions')):
                return self.reply(request, 'rejected', 'busy', effects=False)
            target, overrides = dict(before), dict(state['overrides'])
            advance = state.get('mode_override')
            if op == 'slots':
                group, delta = request['change']['group'], request['change']['delta']
                target[group] += delta
                overrides[group] = dict(slots=target[group], actor='Felix', action_id=request['action_id'],
                                        requested_at=self.clock(), expires_at=(schedule_at(state['schedule_event'])[1]
                                        if self.host == 'desk' else None))
            elif op == 'mode':
                mode = request['change']['mode']
                target, overrides = mode_profile(mode), {}
                advance = (None if mode == mode_at(datetime.fromtimestamp(state['schedule_event'], timezone.utc)) else
                           dict(mode=mode, actor='Felix', action_id=request['action_id'],
                                requested_at=self.clock(), expires_at=schedule_at(state['schedule_event'])[1]))
            else:
                previous = db.execute('SELECT body FROM host_actions WHERE action_id=?', (request['undo_of'],)).fetchone()
                previous = json.loads(previous[0]) if previous else None
                if (not previous or previous['response']['status'] != 'applied'
                        or previous['revision'] != state['revision'] or previous['target'] != before):
                    return self.reply(request, 'conflict', 'undo_conflict', effects=False)
                target, overrides = previous['before'], previous['before_overrides']
                advance = previous.get('before_mode_override')
            try:
                validate_target(before, target, self.host, self.cap(dict(state, mode_override=advance)))
            except ValueError:
                return self.reply(request, 'rejected', 'invalid_request', effects=False)
            if self.foreign_work and any(target[g] > before[g] for g in target):
                return self.reply(request, 'rejected', 'foreign_work', control=self.view(), effects=False)
            plan = slot_plan(before, target)
            expires = schedule_at(state['schedule_event'])[1] if self.host == 'desk' and (overrides or advance) else None
            if self.resolve(state) != state:
                return self.reply(request, 'conflict', 'undo_conflict' if op == 'undo' else 'stale_state', effects=False)
            payload = dict(before=before, desired=target, observed=before, expires_at=expires,
                           applied=[], unconfirmed=[g for g, _ in plan],
                           decreases=[dict(group=g, slots=n) for g, n in plan if n < before[g]],
                           increases=[dict(group=g, slots=n) for g, n in plan if n > before[g]],
                           rpc=[['parallel', str(n), '-g', g] for g, n in plan])
            if self.host == 'desk':
                projected = dict(state, mode_override=advance, overrides=overrides)
                payload.update(mode=self.mode(projected), cap=self.cap(projected),
                               schedule_event=state['schedule_event'], next_event=schedule_at(state['schedule_event'])[1],
                               **mode_notes(projected, self.mode(projected)))
            if request['dry_run']:
                return self.reply(request, 'dry_run', result=payload, effects=False)
        state['revision'] += 1
        action = dict(request=request, before=before, before_overrides=state['overrides'],
                      before_mode_override=state.get('mode_override'), before_mode=self.mode(state), target=target,
                      revision=state['revision'], at=self.clock(), response=self.reply(request, 'applying',
                      revision=state['revision'], result=payload))
        if self.host == 'desk':
            action['schedule_event'] = state['schedule_event']
        state.update(desired=target, overrides=overrides, actor='Felix', at=self.clock())
        if self.host == 'desk':
            state['mode_override'] = advance
        with self.connect(write=True) as db:
            self.store(db, state)
            db.execute('INSERT INTO host_actions VALUES (?,?,?)', (request['action_id'], digest, canonical(action)))
        self.reconcile()
        return self._handle(dict(schema=HOST_SCHEMA, op='status', action_id=request['action_id']))

    def reconcile(self):
        if self.host == 'desk' and os.path.lexists(self.directory / 'maintenance'):
            return False
        with self.connect() as db:
            stored = self.state(db)
            state = self.resolve(stored)
        if state != stored:
            with self.connect(write=True) as db:
                self.store(db, state)  # durable expiration precedes any RPC
        observed = None
        failure = False
        try:
            generation = self.generation(state)
            observed, groups = self.observed()
            if observed != state['desired']:
                validate_target(observed, state['desired'], self.host, self.cap(state))
            if generation != state['generation']:
                # A paused group after restore has ambiguous origin. Never open it.
                for g, row in groups.items():
                    if row['status'] == 'Paused':
                        state['reasons'][g] = sorted(set(state['reasons'].get(g, [])) | {'ambiguous'})
                state['generation'] = generation
                with self.connect(write=True) as db:
                    self.store(db, state)
            for g, n in slot_plan(observed, state['desired']):
                if self.resolve(state) != state:
                    return self.reconcile()
                self.pueue.parallel(g, n)
                observed, _ = self.observed()
                if observed[g] != n or self.generation(state) != generation:
                    raise ValueError('RPC no confirmada o daemon sustituido')
            observed, _ = self.observed()
            if observed != state['desired'] or self.generation(state) != generation:
                raise ValueError('Deriva de cuotas')
            # The final daemon identification can cross the schedule boundary too.
            if self.resolve(state) != state:
                return self.reconcile()
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            failure = True
            try:
                observed, _ = self.observed()
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                observed = None
        crossed_event = False
        with self.connect(write=True) as db:
            for key, body in db.execute('SELECT action_id,body FROM host_actions').fetchall():
                if self.resolve(state) != state:  # revalidate each receipt after DB reads
                    db.rollback()
                    crossed_event = True
                    break
                action = json.loads(body)
                if action['response']['status'] not in ('applying', 'partial', 'unknown'):
                    continue
                status = 'unknown' if observed is None else 'partial' if failure else 'applied'
                expired = self.host == 'desk' and action.get('schedule_event') != state['schedule_event']
                if expired and not failure:
                    status = 'aborted'
                payload = dict(action['response']['result'], observed=observed, desired=state['desired'],
                               undo_of=key if status == 'applied' else None, expired=expired)
                changed = [g for g in action['target'] if action['target'][g] != action['before'][g]]
                payload['applied'] = [g for g in changed if observed is not None and observed[g] == action['target'][g]]
                payload['unconfirmed'] = [g for g in changed if observed is None or observed[g] != action['target'][g]]
                if self.host == 'desk':
                    payload.update(mode=self.mode(state), cap=self.cap(state),
                                   schedule_event=state['schedule_event'], next_event=schedule_at(state['schedule_event'])[1],
                                   expires_at=(schedule_at(state['schedule_event'])[1]
                                               if state['overrides'] or state['mode_override'] else None),
                                   **mode_notes(state, self.mode(state)))
                action['response'] = self.reply(action['request'], status, 'expired' if expired else None,
                                               revision=state['revision'], result=payload)
                if self.resolve(state) != state:  # immediately before each durable receipt
                    db.rollback()
                    crossed_event = True
                    break
                db.execute('UPDATE host_actions SET body=? WHERE action_id=?', (canonical(action), key))
        if crossed_event:
            return self.reconcile()  # release the receipt transaction before recovering
        return not failure

    def pause_fence(self, group):
        return self.directory / ('pause-' + group + '.manual')

    def fence_pause(self, group):
        # A tombstone, not another ownership authority. Only explicit CLI admission
        # removes it; repairing SQLite must never claim an emergency pause.
        with self.pause_fence(group).open('w') as stream:
            stream.write('manual\n'); stream.flush(); os.fsync(stream.fileno())
        folder = os.open(str(self.directory), os.O_RDONLY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)

    def guard(self, readings, proc=Path('/proc')):
        failure, state = None, None
        # Any unreadable authority, including numeric overflow, keeps protective closing.
        errors = (OSError, sqlite3.Error, ValueError, KeyError, TypeError, ArithmeticError, subprocess.SubprocessError)
        try:
            if not self.reconcile():
                raise ValueError('Deseo sin reconciliar; admisión retenida')
        except errors as error:
            failure = error
        try:
            with self.connect() as db:
                state = self.state(db)
        except errors as error:
            failure = error
        can_open = failure is None
        # Protective closing needs existing group status, not a valid quota vector.
        groups = self.pueue.status()['groups']
        thresholds = MINI_THRESHOLDS if self.host == 'mini' else PRESSURE_THRESHOLDS
        normal = decide(self.host, readings, thresholds)
        rapid = decide(self.host, readings, RAPIDAS_THRESHOLDS[self.host])
        log(self.directory, 'decision', accion=normal, accion_rapidas=rapid)
        for g in self.profile:
            if g not in groups:
                continue
            reasons = set(state['reasons'].get(g, [])) if state else set()
            if os.path.lexists(self.pause_fence(g)):
                reasons.add('manual')
            status = groups[g]['status']
            if status == 'Running' and 'pressure' in reasons:
                # An external start ends our ownership, even in hysteresis.
                reasons.discard('pressure')
                state['reasons'][g] = sorted(reasons)
                try:
                    with self.connect(write=True) as db:
                        self.store(db, state)
                except errors as error:
                    can_open, failure = False, error
                    try:
                        self.fence_pause(g)
                    except errors:
                        pass
            decision = rapid if g == 'rapidas' else normal
            if decision == 'pausar' and (status == 'Running' or 'pressure' in reasons):
                try:
                    if not can_open:
                        self.fence_pause(g)
                    else:
                        reasons.add('pressure')
                        state['reasons'][g] = sorted(reasons)
                        with self.connect(write=True) as db:
                            self.store(db, state)  # ownership/intention precedes pause RPC
                except errors as error:
                    can_open, failure = False, error
                    try:
                        self.fence_pause(g)
                    except errors:
                        pass  # Even a failed disk must not suppress protective pause.
                try:
                    self.pueue.call('pause', '--wait', '-g', g)
                except errors as error:
                    can_open, failure = False, error
                continue
            elif can_open and decision == 'reanudar' and 'pressure' in reasons:
                if reasons == {'pressure'} and status == 'Paused':
                    # A fresh sample, after reconciliation and immediately before admission.
                    current = pressure_mini() if self.host == 'mini' else pressure(proc)
                    if decide(self.host, current, RAPIDAS_THRESHOLDS[self.host] if g == 'rapidas' else thresholds) != 'reanudar':
                        continue
                    observed, _ = self.observed()
                    if observed != state['desired'] or self.generation(state) != state['generation']:
                        raise ValueError('Deseo o daemon cambiaron antes de admitir')
                    if self.resolve(state) != state:  # guard: after status and daemon identity
                        raise ValueError('Evento antes de abrir por presión')
                    self.pueue.call('start', '-g', g)
                reasons.discard('pressure')
                state['reasons'][g] = sorted(reasons)
                with self.connect(write=True) as db:
                    self.store(db, state)
            elif can_open and status == 'Paused' and not reasons:
                state['reasons'][g] = ['manual']
                with self.connect(write=True) as db:
                    self.store(db, state)
        if failure is not None:
            log(self.directory, 'sin_reconciliar', detalle=type(failure).__name__)
            raise failure

    def manual(self, group, resume=False):
        with self.connect() as db:
            state = self.state(db)
        if resume:
            if not self.reconcile():
                raise ValueError('Deseo sin reconciliar')
            with self.connect() as db:
                state = self.state(db)
            threshold = RAPIDAS_THRESHOLDS[self.host] if group == 'rapidas' else (MINI_THRESHOLDS if self.host == 'mini' else PRESSURE_THRESHOLDS)
            current = pressure_mini() if self.host == 'mini' else pressure(Path('/proc'))
            if decide(self.host, current, threshold) != 'reanudar' or self.resolve(state) != state:
                raise ValueError('La presión no permite admitir')
            reasons = set(state['reasons'].get(group, [])) - {'manual', 'ambiguous'}
            if not reasons:
                # Keep reasons until success: a failed start retains explicit ownership.
                observed, _ = self.observed()
                if observed != state['desired'] or self.generation(state) != state['generation']:
                    raise ValueError('Deseo o daemon cambiaron antes de admitir')
                if self.resolve(state) != state:  # manual: after status and daemon identity
                    raise ValueError('Evento antes de reanudar manualmente')
                self.pueue.call('start', '-g', group)
            state['reasons'][group] = sorted(reasons)
        else:
            state['reasons'][group] = sorted(set(state['reasons'].get(group, [])) | {'manual'})
            with self.connect(write=True) as db:
                self.store(db, state)
            self.pueue.call('pause', '--wait', '-g', group)
        with self.connect(write=True) as db:
            self.store(db, state)
        if resume and os.path.lexists(self.pause_fence(group)):
            self.pause_fence(group).unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["aplicar", "guardia", "pausar", "reanudar", "adoptar", "rack"])
    parser.add_argument("group", nargs="?")
    parser.add_argument("--host", choices=["desk", "mini"], default="desk")
    parser.add_argument('--pid-file', type=Path)
    args = parser.parse_args()
    groups = MINI_GROUPS if args.host == "mini" else GROUP_LIMITS
    if args.group is not None and args.group not in groups:
        parser.error("grupo desconocido para el host")
    if args.host == "mini" and args.command == "aplicar":
        parser.error("La Mini sólo admite guardia y pausa manual")
    pueue = Pueue("/opt/homebrew/bin/pueue") if args.host == "mini" else Pueue()
    directory = Path.home() / ".local/state/pao/pueue-mini" if args.host == "mini" else STATE_DIR
    if args.command == 'rack':
        # Only panel RPCs have a deadline. Guards and aplicar wait for Pueue.
        pueue.timeout = 3
        request = {}
        engine = MiniControl(pueue, directory, host=args.host)
        try:
            def unique(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError('Clave repetida')
                    value[key] = item
                return value
            raw = sys.stdin.buffer.read(16385)
            if len(raw) > 16384:
                raise ValueError('Petición excesiva')
            request = json.loads(raw, object_pairs_hook=unique)
            validate_host_request(request, args.host)
            # The lock is installed by adoption; even an ensayo cannot create it.
            if args.host == 'desk' and not has_authority(directory):
                response = engine.handle(request)
            else:
                with (directory / 'control.lock').open('r') as handle:
                    if wait_rack_lock(handle):
                        response = engine.handle(request)
                    else:
                        status = ('rejected' if request['op'] in ('slots', 'mode', 'undo') else
                                  'unknown' if request['op'] == 'status' else 'busy')
                        response = engine.reply(request, status, 'lock_busy', effects=False)
        except (OSError, ValueError, TypeError, sqlite3.Error):
            request = request if type(request) is dict else {}
            uncertain = request.get('op') == 'status' or request.get('op') in ('slots', 'mode', 'undo') and request.get('dry_run') is False
            response = engine.reply(request, 'unknown' if uncertain else 'recovery_error', 'recovery_error')
        print(json.dumps(response, allow_nan=False))
        return
    with locked(directory):
        try:
            engine = MiniControl(pueue, directory, host=args.host)
            if args.command == 'adoptar':
                print(json.dumps(engine.adopt(args.pid_file)))
            elif args.command == 'reanudar':
                if args.group is None:
                    parser.error('reanudar necesita un grupo')
                engine.manual(args.group, resume=True)
            elif args.command == "aplicar":
                for attempt in range(30):
                    try:
                        pueue.status()
                        break
                    except subprocess.CalledProcessError:
                        if attempt == 29:
                            raise
                        time.sleep(1)
                if has_authority(directory):
                    if not engine.reconcile():
                        raise ValueError('Horario pendiente de reconciliación')
                else:
                    apply_mode(pueue, datetime.now(timezone.utc))
            elif args.command == "guardia":
                guard(pueue, Path("/proc"), directory, host=args.host)
            else:
                if args.group is None:
                    parser.error("pausar necesita un grupo")
                if has_authority(directory):
                    engine.manual(args.group)
                    return
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
