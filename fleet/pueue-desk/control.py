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
# Horas UTC de los eventos de Día/Noche, las mismas de pueue-desk-mode.timer.
SCHEDULE_HOURS = (5, 14)
# `default` del desk queda fuera del reparto: no entra en el tope ni en el hash.
DESK_POLICY_HASH = hashlib.sha256(json.dumps(dict(limits=GROUP_LIMITS, hours=SCHEDULE_HOURS),
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


def last_event(at):
    """Epoch del último evento de horario en o antes de `at`."""
    day = math.floor(at / 86400) * 86400
    for hour in reversed(SCHEDULE_HOURS):
        if day + hour * 3600 <= at:
            return day + hour * 3600
    return day - 86400 + SCHEDULE_HOURS[-1] * 3600


def next_event(at):
    """Epoch del primer evento de horario estrictamente después de `at`."""
    day = math.floor(at / 86400) * 86400
    for hour in SCHEDULE_HOURS:
        if day + hour * 3600 > at:
            return day + hour * 3600
    return day + 86400 + SCHEDULE_HOURS[0] * 3600


def event_mode(event):
    return mode_at(datetime.fromtimestamp(event, timezone.utc))


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
        return engine_for(host, pueue, directory, proc=proc).guard(readings)
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


def validate_host_request(request, groups=MINI_PROFILE, modes=('fijo',)):
    if type(request) is not dict or request.get('schema') != HOST_SCHEMA:
        raise ValueError('Petición inválida')
    op = request.get('op')
    keys = {'schema', 'op'}
    if op in ('slots', 'undo', 'status'):
        keys.add('action_id')
        if type(request.get('action_id')) is not str or not ID.fullmatch(request['action_id']):
            raise ValueError('ID inválido')
    if op in ('slots', 'undo'):
        keys |= {'dry_run', 'expected', 'change' if op == 'slots' else 'undo_of'}
        if type(request.get('dry_run')) is not bool:
            raise ValueError('Ensayo inválido')
        e = request.get('expected')
        if (type(e) is not dict or set(e) != {'revision', 'policy_hash', 'generation', 'mode', 'slots'}
                or type(e['revision']) is not int or e['revision'] < 1
                or type(e['generation']) is not str or not 1 <= len(e['generation']) <= 160
                or type(e['policy_hash']) is not str or type(e['mode']) is not str or e['mode'] not in modes):
            raise ValueError('Estado esperado inválido')
        validate_vector(e['slots'], groups)
        if op == 'slots':
            c = request.get('change')
            if (type(c) is not dict or set(c) != {'group', 'delta'}
                    or type(c['group']) is not str or c['group'] not in groups
                    or type(c['delta']) is not int or c['delta'] not in (-1, 1)):
                raise ValueError('Cambio inválido')
        elif type(request.get('undo_of')) is not str or not ID.fullmatch(request['undo_of']):
            raise ValueError('Recibo inválido')
    elif op not in ('capabilities', 'status'):
        raise ValueError('Operación no disponible')
    if set(request) != keys:
        raise ValueError('Claves inválidas')
    return request


def validate_vector(slots, groups=MINI_PROFILE):
    if type(slots) is not dict or set(slots) != set(groups):
        raise ValueError('Vector incompleto')
    for value in slots.values():
        validate_limit(value)
    return slots


def validate_target(before, target, cap=sum(MINI_PROFILE.values()), groups=MINI_PROFILE):
    validate_vector(before, groups)
    validate_vector(target, groups)
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


def desk_generation(pid_file, proc=Path('/proc')):
    # Linux: the configured pid file plus that PID's own start time and comm.
    pid = pid_file.read_text().strip()
    if not pid.isdigit() or int(pid) < 1:
        raise ValueError('PID inválido')
    comm = (proc / pid / 'comm').read_text().strip()
    if comm != 'pueued':
        raise ValueError('Daemon no identificado')
    started = (proc / pid / 'stat').read_text().rsplit(')', 1)[1].split()[19]
    return hashlib.sha256((pid + ':' + str(pid_file.stat().st_mtime_ns) + ':' + started + ':' + comm).encode()).hexdigest()


class HostControl:
    """Caller holds control.lock. Queries/ensayos open SQLite read-only, never recover.

    One FULL transaction accepts both desired policy and intent. RPCs run outside
    transactions; a crashed worker leaves the same durable target for guardia.
    The fixed profile below is the Mini's; DeskControl adds the Día/Noche table.
    """
    HOST, GROUPS, MODES, PLACE, CAP_TEXT = 'mini', MINI_PROFILE, ('fijo',), 'en la Mini', '7'
    POLICY_HASH = POLICY_HASH
    SCHEDULED = False

    def __init__(self, pueue, directory, clock=time.time, generation=None, proc=Path('/proc')):
        self.pueue, self.directory, self.clock = pueue, Path(directory), clock
        self.path = self.directory / 'control.sqlite3'
        self.generation_source = generation
        self.proc = Path('/proc') if proc is None else Path(proc)

    def base(self, state):
        return dict(self.GROUPS)

    def mode(self, state):
        return 'fijo'

    def cap(self, state):
        return sum(self.base(state).values())

    def expiry(self, state):
        return None

    def resolve(self, state, now):
        """Pure: the state an elapsed schedule event would produce, and whether it changed."""
        return state, False

    def check_schedule(self, value):
        pass

    def initial(self):
        return dict(MINI_PROFILE), {}

    def default_pid(self):
        return Path.home() / 'Library/Application Support/pueue/pueue.pid'

    def sample(self):
        return pressure_mini()

    def thresholds(self, group):
        return RAPIDAS_THRESHOLDS['mini'] if group == 'rapidas' else MINI_THRESHOLDS

    def extra_view(self, state):
        return {}

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

    def state(self, db):
        row = db.execute('SELECT body FROM policy WHERE id=1').fetchone()
        value = json.loads(row[0])
        if (value['policy_hash'] != self.POLICY_HASH or type(value['revision']) is not int
                or value['revision'] < 1 or type(value['reasons']) is not dict):
            raise ValueError('Autoridad inválida')
        self.check_schedule(value)
        validate_vector(value['desired'], self.GROUPS)
        overrides = value['overrides']
        if type(overrides) is not dict or not set(overrides) <= set(self.GROUPS):
            raise ValueError('Ajustes inválidos')
        calculated = self.base(value)
        for g, item in overrides.items():
            # Desk overrides expire at the first event after their acceptance.
            if (type(item) is not dict or set(item) != {'slots', 'actor', 'action_id', 'requested_at', 'expires_at'}
                    or item['actor'] != 'Felix' or type(item['expires_at']) is bool
                    or item['expires_at'] != self.expiry(value)
                    or type(item['action_id']) is not str or not ID.fullmatch(item['action_id'])
                    or type(item['requested_at']) not in (int, float) or not math.isfinite(item['requested_at'])):
                raise ValueError('Ajuste inválido')
            calculated[g] = validate_limit(item['slots'])
        if calculated != value['desired']:
            raise ValueError('Deseo inconsistente')
        # Ownership is independent of the quota profile (including foreign groups).
        if any(type(g) is not str or type(r) is not list or not set(r) <= {'pressure', 'manual', 'ambiguous'}
               for g, r in value['reasons'].items()):
            raise ValueError('Propiedad inválida')
        return value

    def store(self, db, state):
        db.execute('UPDATE policy SET body=? WHERE id=1', (canonical(state),))

    def generation(self, state):
        if self.generation_source:
            return self.generation_source()
        pid_file = Path(state['pid_file'])
        return mini_generation(pid_file) if self.HOST == 'mini' else desk_generation(pid_file, self.proc)

    def disabled(self):
        for target in (self.directory / 'slots.disabled', self.directory.parent / 'rack/actions.disabled'):
            try:
                target.lstat()
            except FileNotFoundError:
                continue
            return True
        return False

    def observed(self):
        groups = self.pueue.status()['groups']
        vector = {g: groups[g]['parallel_tasks'] for g in self.GROUPS}
        validate_vector(vector, self.GROUPS)
        if any(groups[g]['status'] not in ('Running', 'Paused') for g in self.GROUPS):
            raise ValueError('Admisión no disponible')
        return vector, groups

    def adopt(self, pid_file=None):
        if has_authority(self.directory):
            raise ValueError('La autoridad ya fue adoptada; no se reinicializa')
        observed, groups = self.observed()
        profile, schedule = self.initial()
        if observed != profile:
            raise ValueError('La adopción requiere el perfil verificado ' + '/'.join(map(str, profile.values())))
        pid_file = pid_file or self.default_pid()
        state = dict(policy_hash=self.POLICY_HASH, revision=1, desired=observed, overrides={},
                     reasons={}, generation='', pid_file=str(Path(pid_file).resolve()), actor='Felix', at=self.clock(),
                     **schedule)
        state['generation'] = self.generation(state)
        old = self.directory / 'guardia-state.json'
        owned = set(json.loads(old.read_text())) if old.exists() else set()
        if any(type(g) is not str for g in owned):
            raise ValueError('Propiedad anterior inválida')
        for g, row in groups.items():
            if row['status'] == 'Paused':
                state['reasons'][g] = ['pressure' if g in owned and g in self.GROUPS else 'manual']
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
            state = self.state(db)
            pending = [json.loads(r[0]) for r in db.execute('SELECT body FROM host_actions')
                       if json.loads(r[0])['response']['status'] in ('applying', 'partial', 'unknown')]
        # Virtual: an elapsed event shows its table now, but only real entries store it.
        state, elapsed = self.resolve(state, self.clock())
        observed, groups = self.observed()
        generation = self.generation(state)
        for group in self.GROUPS:
            if os.path.lexists(self.pause_fence(group)):
                state['reasons'][group] = sorted(set(state['reasons'].get(group, [])) | {'manual'})
        enabled = not self.disabled()
        return dict(schema='rack-host-result@1', status='ok', capabilities=dict(
            slots=enabled, undo=enabled, dry_run=enabled, mode=False, reorder=False, restart_queue=False),
            revision=state['revision'], policy_hash=self.POLICY_HASH, generation=generation,
            mode=self.mode(state), cap=self.cap(state),
            slots=observed, desired=state['desired'], drift=observed != state['desired'],
            reason=None if enabled else f'Los ajustes de lugares están apagados {self.PLACE}.',
            recovery_pending=bool(pending) or generation != state['generation'] or elapsed,
            pause_reasons=state['reasons'], pending_actions=[a['request']['action_id'] for a in pending],
            **self.extra_view(state))

    def reply(self, request, status, code=None, **fields):
        messages = {'stale_state': 'Los lugares cambiaron. Actualiza antes de decidir.',
                    'busy': 'Hay un ajuste pendiente; consulta su recibo.',
                    'lock_busy': 'El control está ocupado. Vuelve a intentarlo.',
                    'idempotency_conflict': 'Ese ID ya pertenece a otra petición.',
                    'undo_conflict': 'Los lugares cambiaron desde ese ajuste. No se puede deshacer.',
                    'undo_expired': 'El ajuste venció con el cambio de horario. No se puede deshacer.',
                    'expired': 'Venció con el cambio de horario; rige la tabla del modo nuevo.',
                    'invalid_request': f'Ajuste inválido: cada grupo admite 1..8 y el tope es {self.CAP_TEXT}.',
                    'recovery_error': 'Estado de control ilegible o no disponible. Se conserva la pausa.',
                    'disabled': f'Los ajustes de lugares están apagados {self.PLACE}.',
                    'unsupported': 'Falta actualizar el control de este host.'}
        return dict(schema='rack-host-result@1', action_id=request.get('action_id'), status=status,
                    error=dict(code=code, message=messages[code]) if code else None, **fields)

    def handle(self, request):
        validate_host_request(request, self.GROUPS, self.MODES)
        try:
            response = self._handle(request)
            if request['op'] in ('slots', 'undo') and not request['dry_run']:
                # Rejected IDs are bound too; a later refresh cannot turn a retry
                # of the same ID into a previously unaccepted effect.
                digest = hashlib.sha256(canonical(dict(host=self.HOST, request=request)).encode()).hexdigest()
                if (response.get('error') or {}).get('code') != 'idempotency_conflict':
                    response['receipt_digest'] = digest
                with self.connect(write=True) as db:
                    db.execute('INSERT OR IGNORE INTO host_actions VALUES (?,?,?)',
                               (request['action_id'], digest, canonical(dict(request=request, response=response))))
            return response
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            uncertain = request['op'] == 'status' or request['op'] in ('slots', 'undo') and not request['dry_run']
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
        digest = hashlib.sha256(canonical(dict(host=self.HOST, request=request)).encode()).hexdigest()
        with self.connect() as db:
            row = db.execute('SELECT digest,body FROM host_actions WHERE action_id=?', (request['action_id'],)).fetchone()
            if row:
                return (self.reply(request, 'conflict', 'idempotency_conflict') if row[0] != digest
                        else json.loads(row[1])['response'])
            if self.disabled():
                return self.reply(request, 'rejected', 'disabled', effects=False)
        # Only a real entry stores an elapsed event, and reconciles it before judging.
        if not request['dry_run'] and self.advance():
            self.reconcile()
        with self.connect() as db:
            state, _ = self.resolve(self.state(db), self.clock())
            before, _ = self.observed()
            expected = dict(revision=state['revision'], policy_hash=self.POLICY_HASH,
                            generation=self.generation(state), mode=self.mode(state), slots=before)
            previous = None
            if op == 'undo':
                previous = db.execute('SELECT body FROM host_actions WHERE action_id=?', (request['undo_of'],)).fetchone()
                previous = json.loads(previous[0]) if previous else None
                # Desk: an event between the receipt and its undo already restored the table.
                if previous and previous.get('event') != state.get('event'):
                    return self.reply(request, 'conflict', 'undo_expired', effects=False)
            if (request['expected'] != expected or expected['generation'] != state['generation']
                    or before != state['desired']):
                return self.reply(request, 'conflict', 'stale_state', control=self.view(), effects=False)
            if any(json.loads(r[0])['response']['status'] in ('applying', 'partial', 'unknown')
                   for r in db.execute('SELECT body FROM host_actions')):
                return self.reply(request, 'rejected', 'busy', effects=False)
            target, overrides = dict(before), dict(state['overrides'])
            if op == 'slots':
                group, delta = request['change']['group'], request['change']['delta']
                target[group] += delta
                overrides[group] = dict(slots=target[group], actor='Felix', action_id=request['action_id'],
                                        requested_at=self.clock(), expires_at=self.expiry(state))
            else:
                if (not previous or previous['response']['status'] != 'applied'
                        or previous['revision'] != state['revision'] or previous['target'] != before):
                    return self.reply(request, 'conflict', 'undo_conflict', effects=False)
                target, overrides = previous['before'], previous['before_overrides']
            try:
                validate_target(before, target, self.cap(state), self.GROUPS)
            except ValueError:
                return self.reply(request, 'rejected', 'invalid_request', effects=False)
            plan = slot_plan(before, target)
            payload = dict(before=before, desired=target, observed=before,
                           expires_at=self.expiry(state) if overrides else None,
                           applied=[], unconfirmed=[g for g, _ in plan],
                           decreases=[dict(group=g, slots=n) for g, n in plan if n < before[g]],
                           increases=[dict(group=g, slots=n) for g, n in plan if n > before[g]],
                           rpc=[['parallel', str(n), '-g', g] for g, n in plan])
            if request['dry_run']:
                return self.reply(request, 'dry_run', result=payload, effects=False)
        state['revision'] += 1
        action = dict(request=request, before=before, before_overrides=state['overrides'], target=target,
                      revision=state['revision'], event=state.get('event'), at=self.clock(), response=self.reply(request, 'applying',
                      revision=state['revision'], result=payload))
        state.update(desired=target, overrides=overrides, actor='Felix', at=self.clock())
        with self.connect(write=True) as db:
            self.store(db, state)
            db.execute('INSERT INTO host_actions VALUES (?,?,?)', (request['action_id'], digest, canonical(action)))
        self.reconcile()
        return self._handle(dict(schema=HOST_SCHEMA, op='status', action_id=request['action_id']))

    def advance(self):
        """Store an elapsed schedule event: table of the new mode, overrides retired."""
        if not self.SCHEDULED:
            return False
        with self.connect(write=True) as db:
            state, changed = self.resolve(self.state(db), self.clock())
            if changed:
                self.store(db, state)
        if changed:
            log(self.directory, 'evento', modo=state['mode'], revision=state['revision'])
        return changed

    def reconcile(self):
        self.advance()
        with self.connect() as db:
            state = self.state(db)
        observed = None
        failure = False
        try:
            generation = self.generation(state)
            observed, groups = self.observed()
            if observed != state['desired']:
                validate_target(observed, state['desired'], self.cap(state), self.GROUPS)
            if generation != state['generation']:
                # A paused group after restore has ambiguous origin. Never open it.
                for g, row in groups.items():
                    if row['status'] == 'Paused':
                        state['reasons'][g] = sorted(set(state['reasons'].get(g, [])) | {'ambiguous'})
                state['generation'] = generation
                with self.connect(write=True) as db:
                    self.store(db, state)
            for g, n in slot_plan(observed, state['desired']):
                self.pueue.parallel(g, n)
                observed, _ = self.observed()
                if observed[g] != n or self.generation(state) != generation:
                    raise ValueError('RPC no confirmada o daemon sustituido')
            observed, _ = self.observed()
            if observed != state['desired'] or self.generation(state) != generation:
                raise ValueError('Deriva de cuotas')
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            failure = True
            try:
                observed, _ = self.observed()
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                observed = None
        with self.connect(write=True) as db:
            for key, body in db.execute('SELECT action_id,body FROM host_actions').fetchall():
                action = json.loads(body)
                if action['response']['status'] not in ('applying', 'partial', 'unknown'):
                    continue
                status = 'unknown' if observed is None else 'partial' if failure else 'applied'
                # An event superseded this pending intent: its history stays, its quota does not.
                expired = action.get('event') != state.get('event')
                status = 'aborted' if expired else status
                payload = dict(action['response']['result'], observed=observed, desired=state['desired'],
                               undo_of=key if status == 'applied' else None)
                changed = [g for g in action['target'] if action['target'][g] != action['before'][g]]
                payload['applied'] = [g for g in changed if observed is not None and observed[g] == action['target'][g]]
                payload['unconfirmed'] = [g for g in changed if observed is None or observed[g] != action['target'][g]]
                action['response'] = self.reply(action['request'], status, 'expired' if expired else None,
                                                revision=state['revision'], result=payload)
                db.execute('UPDATE host_actions SET body=? WHERE action_id=?', (canonical(action), key))
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

    def guard(self, readings):
        failure, state = None, None
        errors = (OSError, sqlite3.Error, ValueError, KeyError, TypeError, subprocess.SubprocessError)
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
        normal = decide(self.HOST, readings, self.thresholds(None))
        rapid = decide(self.HOST, readings, self.thresholds('rapidas'))
        log(self.directory, 'decision', accion=normal, accion_rapidas=rapid)
        for g in self.GROUPS:
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
                    current = self.sample()
                    if decide(self.HOST, current, self.thresholds(g)) != 'reanudar':
                        continue
                    observed, _ = self.observed()
                    if observed != state['desired'] or self.generation(state) != state['generation']:
                        raise ValueError('Deseo o daemon cambiaron antes de admitir')
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
            if decide(self.HOST, self.sample(), self.thresholds(group)) != 'reanudar':
                raise ValueError('La presión no permite admitir')
            reasons = set(state['reasons'].get(group, [])) - {'manual', 'ambiguous'}
            if not reasons:
                # Keep reasons until success: a failed start retains explicit ownership.
                observed, _ = self.observed()
                if observed != state['desired'] or self.generation(state) != state['generation']:
                    raise ValueError('Deseo o daemon cambiaron antes de admitir')
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


class MiniControl(HostControl):
    """Fixed 3/1/3 profile, cap 7; a quota adjustment lasts until another manual change."""


class DeskControl(HostControl):
    """GROUP_LIMITS for the current Día/Noche event; adjustments last until the next event.

    `event` is the last 05:00/14:00 UTC boundary already applied. The first entry
    that sees a later boundary (timer, guardia or a panel action) stores the new
    mode's table and retires every override, whether or not the timer ran.
    """
    HOST, GROUPS, MODES, PLACE = 'desk', {g: row['dia'] for g, row in GROUP_LIMITS.items()}, ('dia', 'noche'), 'en el desk'
    CAP_TEXT = '{} de día y {} de noche'.format(*(sum(row[m] for row in GROUP_LIMITS.values()) for m in ('dia', 'noche')))
    POLICY_HASH = DESK_POLICY_HASH
    SCHEDULED = True

    def base(self, state):
        return {g: validate_limit(row[state['mode']]) for g, row in GROUP_LIMITS.items()}

    def mode(self, state):
        return state['mode']

    def expiry(self, state):
        return next_event(state['event'])

    def resolve(self, state, now):
        event = last_event(now)
        if event <= state['event']:
            return state, False
        # Several missed events converge to the current one, never replay history.
        mode = event_mode(event)
        return dict(state, mode=mode, event=event, overrides={}, desired=self.base(dict(mode=mode)),
                    revision=state['revision'] + 1, actor='horario', at=now), True

    def check_schedule(self, value):
        event = value.get('event')
        if (value.get('mode') not in self.MODES or type(event) not in (int, float) or not math.isfinite(event)
                or last_event(event) != event or event_mode(event) != value['mode']):
            raise ValueError('Horario inválido')

    def initial(self):
        event = last_event(self.clock())
        mode = event_mode(event)
        return self.base(dict(mode=mode)), dict(mode=mode, event=event)

    def default_pid(self):
        return Path.home() / '.local/share/pueue/pueue.pid'

    def sample(self):
        return pressure(self.proc)

    def thresholds(self, group):
        return RAPIDAS_THRESHOLDS['desk'] if group == 'rapidas' else PRESSURE_THRESHOLDS

    def extra_view(self, state):
        # Only when an adjustment is in force: when the table of the next event returns.
        return dict(expires_at=self.expiry(state) if state['overrides'] else None)

    def apply(self):
        """Timer entry: never the bare table; the durable desire for the current event."""
        if not self.reconcile():
            raise ValueError('Deseo sin reconciliar')
        with self.connect() as db:
            state = self.state(db)
        print(f"Modo {state['mode']}: " + ", ".join(f"{g}={n}" for g, n in state['desired'].items()))


def engine_for(host, pueue, directory, proc=Path('/proc')):
    return (DeskControl if host == 'desk' else MiniControl)(pueue, directory, proc=proc)


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
        engine = engine_for(args.host, pueue, directory)
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
            validate_host_request(request, engine.GROUPS, engine.MODES)
            if not has_authority(directory):
                # Not adopted yet: nothing to read or change here, and no receipt can exist.
                response = engine.reply(request, 'unknown' if request['op'] == 'status' else 'unsupported',
                                        'unsupported', effects=False)
            else:
                # The lock is installed by adoption; even an ensayo cannot create it.
                with (directory / 'control.lock').open('r') as handle:
                    if wait_rack_lock(handle):
                        response = engine.handle(request)
                    else:
                        status = ('rejected' if request['op'] in ('slots', 'undo') else
                                  'unknown' if request['op'] == 'status' else 'busy')
                        response = engine.reply(request, status, 'lock_busy', effects=False)
        except (OSError, ValueError, TypeError, sqlite3.Error):
            request = request if type(request) is dict else {}
            uncertain = request.get('op') == 'status' or request.get('op') in ('slots', 'undo') and request.get('dry_run') is False
            response = engine.reply(request, 'unknown' if uncertain else 'recovery_error', 'recovery_error')
        print(json.dumps(response, allow_nan=False))
        return
    with locked(directory):
        try:
            engine = engine_for(args.host, pueue, directory)
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
                    engine.apply()
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
