#!/usr/bin/python3 -I
"""Cuotas y guardia de Pueue 4.0.4; sólo biblioteca estándar."""
import argparse
from copy import deepcopy
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
import shlex
import sys
import time
import importlib.util
import types

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


def simple_yaml_value(raw):
    raw = raw.split(' #', 1)[0].strip()
    if raw in ('', '~', 'null'):
        return None
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in '\'"':
        inner = raw[1:-1]
        if chr(92) in inner or raw[0] in inner:
            raise ValueError('Escape YAML no acreditado')
        return inner
    if re.fullmatch(r'[A-Za-z0-9_/. ~@+-]+', raw):
        return raw
    raise ValueError('Valor YAML no reconocido')


def pueue_settings(path):
    """Only client.edit_mode and shared.alias_file. Anything not proven fails closed."""
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return dict(edit_mode=None, alias_file=None)
    try:
        value = json.loads(text)
    except ValueError:
        value, section = {}, None
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith('#') or line.strip() == '---':
                continue
            top = re.fullmatch(r'([A-Za-z_]+):(.*)', line)
            if top:
                section = top.group(1)
                if section in ('client', 'shared') and simple_yaml_value(top.group(2)) is not None:
                    raise ValueError('Bloque YAML en línea no reconocido')
                if section in value:
                    raise ValueError('Bloque duplicado')
                value[section] = {}
                continue
            if section in ('client', 'shared'):
                inner = re.fullmatch(r'  ([A-Za-z_]+):(.*)', line)
                if not inner:
                    raise ValueError('Configuración de Pueue no reconocida')
                if inner.group(1) in value[section]:
                    raise ValueError('Clave duplicada')
                value[section][inner.group(1)] = simple_yaml_value(inner.group(2)) if inner.group(1) in ('edit_mode', 'alias_file') else None
    if type(value) is not dict or any(type(value.get(k, {})) is not dict for k in ('client', 'shared')):
        raise ValueError('Configuración de Pueue no reconocida')
    edit_mode, alias_file = value.get('client', {}).get('edit_mode'), value.get('shared', {}).get('alias_file')
    if any(v is not None and type(v) is not str for v in (edit_mode, alias_file)):
        raise ValueError('Configuración de Pueue no reconocida')
    return dict(edit_mode=edit_mode, alias_file=alias_file)


def read_aliases(path):
    """Flat `name: value` map as serde_yaml reads it; absent file means no aliases."""
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return {}
    if text.strip() == '{}':
        return {}
    aliases = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith('#') or line.strip() == '---':
            continue
        match = re.fullmatch(r'([^\s:#\'"][^:#]*?|"[^"]*"|\'[^\']*\'):\s+(.*)', line)
        if not match:
            raise ValueError('Alias no reconocido')
        key = simple_yaml_value(match.group(1))
        if key in aliases:
            raise ValueError('Alias repetido')
        aliases[key] = simple_yaml_value(match.group(2))
    return aliases



class Pueue:
    def __init__(self, binary=None, env=None, cwd=None):
        self.binary = str(binary or Path.home() / ".local/bin/pueue")
        self.timeout = None
        self.deadline = None
        self.context = dict(env=env, cwd=cwd) if env is not None else {}

    def call(self, *args):
        timeout = self.timeout
        if self.deadline is not None:
            remaining = self.deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Presupuesto de pausa agotado')
            timeout = min(timeout or remaining,remaining)
        return subprocess.run([self.binary, *map(str, args)], check=True,
                              text=True, capture_output=True, timeout=timeout, **self.context).stdout

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
def locked(directory, create=True):
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    with (directory / "control.lock").open("a" if create else "rb") as handle:
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


def pressure_mini(timeout=None):
    load = os.getloadavg()[0]
    cores = os.cpu_count()
    level = int(subprocess.run(
        ["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
        check=True, text=True, capture_output=True, **(dict(timeout=timeout) if timeout is not None else {})).stdout.strip())
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


def recover_mini(pueue, directory):
    import importlib.util
    spec = importlib.util.spec_from_file_location('mini_recovery', Path(__file__).with_name('mini.py'))
    recovery = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recovery)
    return recovery.Manager(pueue=pueue, directory=directory)._recover()


def guard(pueue, proc, directory, *, host="desk"):
    if host == 'mini' and has_authority(directory):
        # The existing 60 s job also owns handoff recovery. No second timer.
        try:
            if not recover_mini(pueue, directory):
                return
        except ADMISSION_ERRORS as error:
            # Recovery cannot bypass the old protective pressure close on corrupt DBs.
            # Carry its failure into the guard so even valid quotas cannot reopen.
            return MiniControl(pueue, directory)._legacy_guard(None, proc=proc, recovery_error=error)
    if has_authority(directory):
        engine = MiniControl(pueue, directory) if host == 'mini' else MiniControl(pueue, directory, host='desk')
        return engine.guard(None, proc=proc)
    readings = pressure_mini() if host == 'mini' else pressure(proc)
    log(directory, 'lectura', **readings)
    normal = decide(host, readings, MINI_THRESHOLDS if host == "mini" else PRESSURE_THRESHOLDS)
    rapid = decide(host, readings, RAPIDAS_THRESHOLDS[host])
    log(directory, "decision", accion=normal, accion_rapidas=rapid)
    target = directory / "guardia-state.json"
    owned = set(json.loads(target.read_text())) if target.exists() else set()
    managed_groups = MINI_GROUPS if host == "mini" else GROUP_LIMITS.keys()
    groups = pueue.status()["groups"]
    if any(type(g) is not str for g in owned):
        raise ValueError('Propiedad inválida')
    absent = owned - set(groups)
    if absent:
        owned -= absent
        save_owned(directory, owned)
    for group in managed_groups:
        # Un grupo que no existe en el host no se gestiona; la poda anterior ya lo soltó.
        if group not in groups:
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


_PRIORITY_HELPER = None


def priority_helper():
    # -I ignores PYTHONDONTWRITEBYTECODE. A read/ensayo must not create pyc.
    global _PRIORITY_HELPER
    if _PRIORITY_HELPER is None:
        path = Path(__file__).with_name('editor_prioridad.py')
        module = types.ModuleType('priority_editor')
        module.__file__ = str(path)
        exec(compile(path.read_bytes(),str(path),'exec'),module.__dict__)
        _PRIORITY_HELPER = module
    return _PRIORITY_HELPER


def validate_reorder_request(request, host, allow_short=False):
    e = request.get('expected')
    fields = {'revision','admission_revision','policy_hash','generation','group','queue_etag','queue'}
    if (type(e) is not dict or set(e) != fields or type(e['revision']) is not int or e['revision'] < 1
            or type(e['admission_revision']) is not int or e['admission_revision'] < 1
            or type(e['group']) is not str or e['group'] not in (MINI_PROFILE if host=='mini' else GROUP_LIMITS)
            or type(e['generation']) is not str or not 1 <= len(e['generation']) <= 160
            or any(type(e[k]) is not str or not re.fullmatch('[0-9a-f]{64}',e[k]) for k in ('policy_hash','queue_etag'))
            or type(e['queue']) is not list or not (0 if allow_short or request['op']=='undo' else 2) <= len(e['queue']) <= 64):
        raise ValueError('Fila inválida')
    ids = []
    for row in e['queue']:
        if (type(row) is not dict or set(row) != {'id','identity','priority'}
                or type(row['id']) is not int or not 0 <= row['id'] < 2**64
                or type(row['priority']) is not int or not -(2**31) <= row['priority'] < 2**31
                or type(row['identity']) is not str or not re.fullmatch('[0-9a-f]{64}',row['identity'])):
            raise ValueError('Identidad inválida')
        ids.append(row['id'])
    if len(set(ids)) != len(ids):
        raise ValueError('Fila duplicada')
    if request['op'] == 'reorder':
        c = request.get('change')
        if (type(c) is not dict or set(c) != {'order'} or type(c['order']) is not list
                or len(c['order']) != len(ids) or any(type(n) is not int for n in c['order'])
                or set(c['order']) != set(ids)):
            raise ValueError('Permutación inválida')
    elif type(request.get('undo_of')) is not str or not ID.fullmatch(request['undo_of']):
        raise ValueError('Recibo inválido')
    if len(canonical(request).encode()) > 16384:
        raise ValueError('Fila demasiado larga para reordenar de una vez')


def validate_host_request(request, host='mini'):
    if type(request) is not dict or request.get('schema') != HOST_SCHEMA:
        raise ValueError('Petición inválida')
    op = request.get('op')
    if op == 'reorder' or op == 'undo' and type(request.get('expected')) is dict and 'queue' in request['expected']:
        keys = {'schema','op','action_id','dry_run','expected', 'change' if op=='reorder' else 'undo_of'}
        if set(request) != keys or type(request.get('action_id')) is not str or not ID.fullmatch(request['action_id']) or type(request.get('dry_run')) is not bool:
            raise ValueError('Petición inválida')
        validate_reorder_request(request,host)
        return request
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


PROC = Path('/proc')
DESK_GENERATION = 'linux-proc-v1:'


def generation_kind(value):
    """Desk authority accepts only the legacy digest or the versioned Linux digest."""
    if type(value) is str:
        if re.fullmatch(r'[0-9a-f]{64}', value):
            return 'legacy'
        if re.fullmatch(r'linux-proc-v1:[0-9a-f]{64}', value):
            return 'linux-proc-v1'
    raise ValueError('Generación desk inválida')


def _pid_signature(info):
    return [info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size]


def linux_identity(pid_file, *, proc=None):
    """Two coherent Linux snapshots, with the configured pid file pinned each time.

    starttime is in boot ticks: no ps, btime, wall time or conversion to a date.
    This fences reads; it cannot make a later Pueue RPC atomic with the snapshot.
    """
    if proc is None and sys.platform != 'linux':
        raise ValueError('La identidad del desk requiere Linux')
    proc = PROC if proc is None else Path(proc)
    pid_file = Path(pid_file)

    def snapshot():
        with pid_file.open('r', encoding='ascii') as handle:
            signature = _pid_signature(os.fstat(handle.fileno()))
            if _pid_signature(pid_file.stat()) != signature:
                raise ValueError('La ruta del pid file cambió al abrirlo')
            text = handle.read()
            pid_text = text.strip()
            if not re.fullmatch(r'[0-9]+', pid_text) or int(pid_text) < 1:
                raise ValueError('PID inválido')
            pid = int(pid_text)
            boot = (proc / 'sys/kernel/random/boot_id').read_text(encoding='ascii').strip()
            if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', boot):
                raise ValueError('boot_id inválido')
            record = (proc / str(pid) / 'stat').read_text(encoding='ascii')
            head, closing, tail = record.rpartition(')')
            fields = tail.split()  # field 3 begins the suffix; field 22 is index 19
            if (not closing or head != str(pid) + ' (pueued' or len(fields) < 20
                    or fields[0] not in ('R', 'S', 'D', 'T', 't', 'W', 'K', 'P', 'I')
                    or not re.fullmatch(r'[0-9]+', fields[19]) or int(fields[19]) >= 2**64):
                raise ValueError('Daemon no identificado en proc')
            handle.seek(0)
            if (handle.read() != text or _pid_signature(os.fstat(handle.fileno())) != signature
                    or _pid_signature(pid_file.stat()) != signature):
                raise ValueError('El pid file cambió durante la lectura')
            return dict(boot=boot, pid=pid, ticks=int(fields[19]), file=signature)

    first, second = snapshot(), snapshot()
    if first != second:
        raise ValueError('La identidad del daemon cambió durante la lectura')
    return first


def desk_generation(pid_file, *, proc=None):
    identity = linux_identity(pid_file, proc=proc)
    seed = ['linux-proc-v1', identity['boot'], identity['pid'], identity['ticks'], identity['file']]
    return DESK_GENERATION + hashlib.sha256(canonical(seed).encode()).hexdigest()


INTERRUPTED = 'Interrumpida al reiniciar Pueue; relanzar manualmente'


def validate_timestamp(value):
    # fromisoformat normalizes +00:99 on Python 3.9. Chrono rejects it.
    pattern = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.[0-9]{1,9})?(?:Z|[+-]([0-9]{2}):([0-9]{2}))"
    match = re.fullmatch(pattern, value) if isinstance(value, str) else None
    if not match or any(int(n or 0) > limit for n, limit in zip(match.groups(), (23, 59, 59, 23, 59))):
        raise ValueError('Fecha Pueue incompatible')
    datetime.fromisoformat(value[:19])  # calendar; preserve nanoseconds on Python 3.9


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
        validate_timestamp(value)

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
    if os.path.lexists(path.with_suffix(".json.gz")):
        raise ValueError("Se requiere compress_state_file: false; no se altera el estado comprimido")
    value = json.loads(path.read_text(), object_pairs_hook=unique_object)
    return validate_queue(value)


def validate_queue(value):
    if not isinstance(value, dict) or not isinstance(value.get("tasks"), dict) or not isinstance(value.get("groups"), dict):
        raise ValueError("Estado Pueue incompleto; arranque abortado sin reemplazar la cola")
    try:
        for name, group in value["groups"].items():
            if not isinstance(name, str) or not name or not isinstance(group, dict):
                raise ValueError("Grupo incompatible")
            if group.get("status") not in ("Running", "Paused") or type(group.get("parallel_tasks")) is not int or not 0 <= group["parallel_tasks"] < 2**64:
                raise ValueError("Grupo Pueue incompatible; arranque abortado")
        for task_id, task in value["tasks"].items():
            validate_task(task_id, task)
            if task["group"] not in value["groups"]:
                raise ValueError("Grupo de tarea ausente; no se remapea ni se pierde la cola")
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError("Cola Pueue incompatible; arranque abortado sin sustituirla") from error
    return value


def default_data(home=None):
    home = home or Path.home()
    if sys.platform == "darwin":
        return home / "Library/Application Support/pueue"
    return Path(os.environ.get("XDG_DATA_HOME", str(home / ".local/share"))) / "pueue"


MANAGER_KEYS = ('XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_RUNTIME_DIR', 'PUEUE_CONFIG_PATH')


def mini_environment(home):
    env = dict(os.environ, HOME=str(home))
    for key in MANAGER_KEYS:
        env.pop(key, None)
    return env


def manager_environment(text):
    """Only path settings from systemctl show-environment; never log its contents."""
    env = {}
    for line in text.splitlines():
        key = line.split('=', 1)[0]
        if key not in MANAGER_KEYS:
            continue
        words = shlex.split(line)
        if len(words) != 1 or words[0].startswith("$'") or '=' not in words[0]:
            raise ValueError('Entorno de systemd ambiguo para ' + key)
        name, value = words[0].split('=', 1)
        env[name] = value
    return env


def validate_configuration(home=None, config_path=None, data=None, pid_file=None, pueue=None, daemon=None,
                           env=None, cwd=None):
    """Sólo valida; no escribe ni genera un pueue.yml administrado.

    Acepta JSON y el YAML expandido simple que emite Pueue. Ante sintaxis
    ambigua falla cerrado: no intenta adivinar dónde vive la cola.
    """
    home = home or Path.home()
    env = os.environ if env is None else env
    cwd = Path.cwd() if cwd is None else Path(cwd)
    data = data or (default_data(home) if sys.platform == 'darwin' else
                    Path(env.get('XDG_DATA_HOME', str(home / '.local/share'))) / 'pueue')
    runtime = env.get('XDG_RUNTIME_DIR') if sys.platform != 'darwin' else None
    pid_file = pid_file or (Path(runtime) if runtime else data) / 'pueue.pid'
    pueue = pueue or home / '.local/bin/pueue'
    daemon = daemon or home / '.local/bin/pueued'
    for binary, expected in ((pueue, 'pueue 4.0.4'), (daemon, 'pueued 4.0.4')):
        if subprocess.check_output([str(binary), '--version'], text=True, timeout=5).strip() != expected:
            raise ValueError('Se requiere Pueue estándar 4.0.4')
    for key in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR"):
        if key in env and not Path(env[key]).is_absolute():
            raise ValueError(f"{key} debe ser absoluto para compartir rutas con Pueue")
    config_dir = (default_data(home) if sys.platform == "darwin" else
                  Path(env.get("XDG_CONFIG_HOME", str(home / ".config"))) / "pueue")
    standard = config_dir / "pueue.yml"
    if config_path is None and env.get("PUEUE_CONFIG_PATH"):
        raise ValueError("PUEUE_CONFIG_PATH personalizado no se admite en el host")
    source = config_path or (standard if standard.exists() else cwd / "pueue.yml")
    if not source.exists():
        return dict(data=data, pid_file=pid_file)
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
        allowed = {'pueue_directory': str(data)}
        if config_path is not None:
            allowed.update(runtime_directory=str(data), unix_socket_path=str(data / 'socket'), pid_path=str(pid_file))
        if value is not None and value != allowed.get(key):
            raise ValueError(f"shared.{key} personalizado; no se adopta la cola")
    if shared.get("use_unix_socket", True) is not True:
        raise ValueError("El host requiere socket Unix")
    if config.get("daemon", {}).get("compress_state_file", False) is not False:
        raise ValueError("Se requiere compress_state_file: false")


    daemon_config = config.get('daemon', {})
    # Pueue 4.0.4 writes `shell_command: null` for its default shell.
    if daemon_config.get('env_vars', {}) != {} or daemon_config.get('shell_command') is not None:
        raise ValueError('daemon.env_vars o shell_command complejo no soportado; conservar configuración')
    return dict(data=data, pid_file=pid_file)


def sync_directory(path):
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sync_file(stream):
    stream.flush()
    os.fsync(stream.fileno())
    if sys.platform == 'darwin':
        fcntl.fcntl(stream.fileno(), 51)  # F_FULLFSYNC, propagate a failed durability barrier


def atomic_json(path, value):
    temporary = path.with_name(path.name + '.startup-tmp')
    with temporary.open('w') as stream:
        os.chmod(temporary, 0o600)
        stream.write(canonical(value) + '\n')
        sync_file(stream)
    os.replace(temporary, path)
    sync_directory(path.parent)


def queue_hash(queue):
    return hashlib.sha256(canonical(queue).encode()).hexdigest()


def assert_offline(pueue, pid_file):
    try:
        pueue.status()
    except subprocess.CalledProcessError:
        pass
    else:
        raise ValueError('Daemon responde; preparar exige daemon detenido')
    if os.path.lexists(pid_file):
        pid = pid_file.read_text().strip()
        if not pid.isdigit() or int(pid) < 1:
            raise ValueError('PID ilegible; no se prepara')
        # Any live or unidentifiable PID blocks. No name search, no killing.
        result = subprocess.run(['/bin/ps', '-p', pid, '-o', 'comm='],
                                text=True, capture_output=True, timeout=5)
        if result.returncode != 1:
            raise ValueError('PID anterior vivo o ambiguo; no se prepara')


def startup_plan(queue, state, now, data, profile):
    image = deepcopy(queue)
    state = deepcopy(state)
    previous = state.get('startup', {})
    holds = dict(previous.get('holds', {}))
    interruptions = dict(previous.get('interruptions', {}))
    changes = {}
    image['groups'].setdefault('default', dict(status='Running', parallel_tasks=1))
    for g, n in state['desired'].items():
        image['groups'].setdefault(g, dict(status='Running', parallel_tasks=n))
        image['groups'][g]['parallel_tasks'] = n
    for g, row in image['groups'].items():
        reasons = set(state['reasons'].get(g, []))
        if g not in profile:
            reasons.add('ambiguous'); holds[g] = 'grupo fuera del reparto'
        elif row['status'] == 'Paused' and not reasons:
            reasons.add('manual'); holds[g] = 'pausa de origen desconocido'
        elif 'manual' in reasons or 'ambiguous' in reasons:
            holds.setdefault(g, 'pausa manual' if 'manual' in reasons else 'pausa de origen desconocido')
        reasons.add('startup')
        state['reasons'][g] = sorted(reasons)
        row['status'] = 'Paused'
    stamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
    for ident, task in image['tasks'].items():
        variant, detail = next(iter(task['status'].items()))
        if variant in ('Running', 'Paused') or (variant == 'Queued' and os.path.lexists(data / 'task_logs' / (ident + '.log'))):
            task['status'] = {'Done': dict(enqueued_at=detail['enqueued_at'], start=detail.get('start', stamp), end=stamp, result='Killed')}
            changes[ident] = task['status']
            interruptions[ident] = dict(created_at=task['created_at'], message=INTERRUPTED)
            if variant == 'Queued':
                holds[task['group']] = 'tarea en fila con log previo; lanzamiento ambiguo'
                state['reasons'][task['group']] = sorted(set(state['reasons'][task['group']]) | {'ambiguous'})
        elif (variant == 'Done' and detail['result'] == 'Killed'
              and state.get('stop_tasks', {}).get(ident) == task['created_at']):
            interruptions[ident] = dict(created_at=task['created_at'], message=INTERRUPTED)
        elif variant == 'Locked':
            holds[task['group']] = 'tarea Locked desconocida; revisar antes de reanudar'
            state['reasons'][task['group']] = sorted(set(state['reasons'][task['group']]) | {'ambiguous'})
    # Identity is checked again by snapshot; cancelled/terminal tasks get no new reason.
    state.pop('stop_tasks', None)
    state['startup'] = dict(phase='intent', source_hash=queue_hash(queue), target_hash=queue_hash(image),
                            statuses=changes, groups=image['groups'], holds=holds,
                            interruptions=interruptions, previous_generation=state['generation'])
    return state, image


def start_inputs(engine, config, queue_path, policy_migration=False):
    """Read-only gates shared by the installer and the offline preparer."""
    queue = read_queue(queue_path)  # adopted host: absent is never a fresh queue
    with engine.connect() as db:
        stored = engine.state(db, policy_migration=policy_migration)
    if os.path.lexists(engine.directory / 'maintenance'):
        raise ValueError('Host en mantenimiento')
    if config['pid_file'].resolve() != Path(stored['pid_file']).resolve():
        raise ValueError('PID configurado difiere de la autoridad')
    return queue, stored


def start_projection(engine, config, queue_path=None, allow_pending=True, policy_migration=False):
    """Validate the exact preparation image without changing files or the daemon."""
    queue, stored = start_inputs(engine, config, queue_path or config['data'] / 'state.json', policy_migration)
    state = engine.resolve(stored)
    state['startup_version'] = 1
    prior = state.get('startup', {})
    if prior.get('phase') == 'intent' and not allow_pending:
        raise ValueError('Preparación incompleta; recuperar antes de instalar')
    disk_hash = queue_hash(queue)
    if prior.get('phase') == 'intent':
        digest = queue_hash(queue)
        if digest == prior['source_hash']:
            queue = deepcopy(queue)
            queue['groups'] = prior['groups']
            for ident, status in prior['statuses'].items():
                queue['tasks'][ident]['status'] = status
            for ident, priority in prior.get('priorities', {}).items():
                queue['tasks'][ident]['priority'] = priority
            if queue_hash(queue) != prior['target_hash']:
                raise ValueError('Intención de arranque inconsistente')
        elif digest != prior['target_hash']:
            raise ValueError('Cola cambió durante preparación; conservar archivos')
    validate_queue(queue)  # an intent reconstructed from authority is validated too
    # Re-project even after a crash: an event may have expired overrides meanwhile.
    recovered = engine.reorder_offline(queue, state, config['data'])
    state, image = startup_plan(recovered, state, engine.clock(), config['data'], engine.profile)
    state['startup']['statuses'].update({ident:t['status'] for ident,t in image['tasks'].items() if t['status'] != queue['tasks'][ident]['status']})
    state['startup']['priorities'] = {ident:t['priority'] for ident,t in image['tasks'].items() if t['priority'] != queue['tasks'][ident]['priority']}
    state['startup']['target_hash'] = queue_hash(image)
    validate_queue(image)  # validate the exact bytes to be published, before intent
    state['startup']['source_hash'] = disk_hash
    # Carry recovered statuses forward if the disk still contains the old image.
    if prior.get('phase') == 'intent':
        state['startup']['statuses'] = dict(prior['statuses'], **state['startup']['statuses'])
        state['startup']['priorities'] = dict(prior.get('priorities',{}), **state['startup']['priorities'])
    return state, image


def prepare_start(engine, queue_path=None, configuration=None, dry_run=False, boundary=lambda phase: None):
    """Caller holds control.lock. Intention -> queue rename -> repeatable completion."""
    config = configuration if configuration is not None else (
        validate_configuration(pueue='/opt/homebrew/bin/pueue', daemon='/opt/homebrew/bin/pueued',
                               env={}, cwd=Path.home()) if engine.host == 'mini' else validate_configuration())
    queue_path = queue_path or config['data'] / 'state.json'
    state, image = start_projection(engine, config, queue_path)
    assert_offline(engine.pueue, config['pid_file'])
    if dry_run:
        return image
    with engine.connect(write=True) as db:
        engine.store(db, state)
    boundary('intent')
    atomic_json(queue_path, image)
    boundary('queue')
    state['startup']['phase'] = 'prepared'
    with engine.connect(write=True) as db:
        engine.store(db, state)
    boundary('complete')
    if engine.resolve(state) != state:
        return prepare_start(engine, queue_path, config, dry_run=False, boundary=boundary)
    # Stock refuses even a dead pid file. Remove only after all data are durable.
    if os.path.lexists(config['pid_file']):
        config['pid_file'].unlink(); sync_directory(config['pid_file'].parent)
    return image


ADMISSION_ERRORS = (OSError, sqlite3.Error, ValueError, KeyError, TypeError, ArithmeticError, subprocess.SubprocessError)
ADMISSION_GAP_NS = 60_000_000_000
GATE_SECONDS = 10
ADMISSION_CAUSES = {'fresh_sample', 'docker', 'generation', 'desired', 'event', 'admission',
                    'startup', 'authority', 'maintenance', 'reason', 'pressure_high', 'dependency',
                    'quota', 'task_changed', 'cooldown', 'admission_unknown', 'clock', 'log', 'rpc'}


def utc_at(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def timestamp_key(value):
    validate_timestamp(value)
    # Preserve Chrono's nanoseconds; datetime truncates after microseconds.
    base = re.sub(r'\.[0-9]+', '', value).replace('Z', '+00:00')
    fraction = re.search(r'\.([0-9]+)', value)
    return (int(datetime.fromisoformat(base).timestamp()),
            int(fraction[1].ljust(9, '0')) if fraction else 0)


def admission_clock():
    boot = Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip()
    ns = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', boot) or type(ns) is not int or ns < 0:
        raise ValueError('Reloj de admisión inválido')
    return boot, ns


def validate_admission(value):
    if type(value) is not dict or set(value) != {'version', 'last_attempt', 'last_confirmed'} or type(value['version']) is not int or value['version'] != 1:
        raise ValueError('Permiso inválido')
    attempt, confirmed = value['last_attempt'], value['last_confirmed']
    def identity(row):
        if (type(row['id']) is not int or not 0 <= row['id'] < 2**64 or type(row['grupo']) is not str
                or row['grupo'] not in GROUP_LIMITS or row['grupo'] == 'rapidas'):
            raise ValueError('Identidad de permiso inválida')
        validate_timestamp(row['at'])
        if not row['at'].endswith(('+00:00','Z')):
            raise ValueError('UTC de permiso inválido')
    if confirmed is not None:
        if type(confirmed) is not dict or set(confirmed) != {'at','grupo','id'}:
            raise ValueError('Confirmación inválida')
        identity(confirmed)
    if attempt is not None:
        fields = {'boot_id','reserved_ns','completed_ns','at','generation','grupo','id','created_at','enqueued_at','outcome'}
        if type(attempt) is not dict or set(attempt) != fields:
            raise ValueError('Intento inválido')
        identity(attempt)
        if (type(attempt['boot_id']) is not str or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',attempt['boot_id'])
                or type(attempt['reserved_ns']) is not int or attempt['reserved_ns'] < 0
                or attempt['outcome'] not in ('reserved','confirmed','not_sent','unknown')):
            raise ValueError('Reserva inválida')
        generation_kind(attempt['generation'])
        validate_timestamp(attempt['created_at']); validate_timestamp(attempt['enqueued_at'])
        end = attempt['completed_ns']
        if end is not None and (type(end) is not int or end < attempt['reserved_ns']):
            raise ValueError('Finalización inválida')
        if (attempt['outcome'] in ('confirmed','not_sent')) != (end is not None):
            raise ValueError('Resultado incoherente')
        if attempt['outcome'] == 'confirmed' and (confirmed is None or any(confirmed[k] != attempt[k] for k in ('grupo','id'))):
            raise ValueError('Confirmación incoherente')
    elif confirmed is not None:
        raise ValueError('Confirmación sin intento')
    return value


def admission_state(db, migrate=False):
    exists = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='guard_admission'").fetchone()
    if not exists:
        if not migrate:
            return None  # compatible read-only preflight; never a runtime permission
        db.execute('CREATE TABLE guard_admission (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)')
        value = dict(version=1,last_attempt=None,last_confirmed=None)
        db.execute('INSERT INTO guard_admission VALUES (1,?)',(canonical(value),))
    rows = db.execute('SELECT id,body FROM guard_admission').fetchall()
    if len(rows) != 1 or rows[0][0] != 1:
        raise ValueError('Fila de permiso inválida')
    return validate_admission(json.loads(rows[0][1],object_pairs_hook=unique_object))


def validate_mini_handoff(value):
    if (type(value) is not dict or type(value.get('version')) is not int or value.get('version') not in (1, 2)
            or value.get('target') not in ('install', 'withdraw')
            or value.get('phase') not in ('stage', 'drain', 'switch', 'verify', 'maintenance', 'done', 'withdrawn', 'aborted')
            or type(value.get('original_disabled')) is not dict or len(value['original_disabled']) != 3
            or any(type(k) is not str or type(v) is not bool for k, v in value['original_disabled'].items())
            or type(value.get('effect')) not in (str, type(None))
            or 'deadline' not in value
            or value['deadline'] is not None and (type(value['deadline']) not in (int, float) or not math.isfinite(value['deadline']))
            or 'maintenance_owned' in value and type(value['maintenance_owned']) is not bool
            or 'retained_owner' in value and value['retained_owner'] not in ('homebrew', 'prepared')
            or value['version'] == 1 and 'source' in value
            or value['version'] == 2 and (
                type(value.get('source')) is not dict
                or set(value['source']) != {'owner', 'pid', 'generation'}
                or value['source']['owner'] not in ('homebrew', 'prepared')
                or type(value['source']['pid']) is not int or value['source']['pid'] < 1
                or type(value['source']['generation']) is not str or not value['source']['generation']
                or value.get('phase') == 'aborted' and 'retained_owner' in value
                and value['retained_owner'] != value['source']['owner'])):
        raise ValueError('Relevo ilegible; conservar cierre y archivos')


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
        self.boot_clock = admission_clock

    def mode(self, state):
        return 'fijo' if self.host == 'mini' else (state['mode_override']['mode'] if state['mode_override'] else
                mode_at(datetime.fromtimestamp(state['schedule_event'], timezone.utc)))

    def cap(self, state):
        return 7 if self.host == 'mini' else sum(mode_profile(self.mode(state)).values())

    def notes(self, state):
        try:
            return mode_notes(state, self.mode(state))
        except (ImportError, KeyError, OSError, ValueError):
            # UTC policy and ± remain available without the display timezone.
            return dict(mode_note=None, mode_detail=None, slots_note=None)

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
        migrating = policy_migration and value['policy_hash'] != self.policy_hash
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
            generation_kind(value['generation'])
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
        if any(type(g) is not str or type(r) is not list or not set(r) <= {'pressure', 'manual', 'ambiguous', 'startup', 'handoff', 'reorder'}
               for g, r in value['reasons'].items()):
            raise ValueError('Propiedad inválida')
        if self.host == 'mini' and 'mini_handoff' in value:
            validate_mini_handoff(value['mini_handoff'])
        startup = value.get('startup')
        if ('startup_version' in value and (type(value['startup_version']) is not int or value['startup_version'] != 1)
                or any('startup' in r for r in value['reasons'].values()) and startup is None):
            raise ValueError('Versión o propiedad de arranque inválida')
        if startup is not None:
            if (value.get('startup_version') != 1 or type(startup) is not dict or startup.get('phase') not in ('intent', 'prepared', 'ready')
                    or any(type(startup.get(k)) is not dict for k in ('statuses', 'groups', 'holds', 'interruptions'))
                    or any(type(g) is not str or type(reason) is not str for g, reason in startup['holds'].items())
                    or any(not re.fullmatch('[0-9a-f]{64}', startup.get(k, '')) for k in ('source_hash', 'target_hash'))):
                raise ValueError('Arranque durable inválido')
            if self.host == 'desk' and 'previous_generation' in startup:
                generation_kind(startup['previous_generation'])
            for ident, item in startup['interruptions'].items():
                if not ident.isdigit() or type(item) is not dict or item.get('message') != INTERRUPTED:
                    raise ValueError('Interrupción inválida')
                validate_timestamp(item['created_at'])
        return value

    def store(self, db, state):
        db.execute('UPDATE policy SET body=? WHERE id=1', (canonical(state),))

    def generation(self, state):
        if self.generation_source:
            return self.generation_source()
        reader = mini_generation if self.host == 'mini' else desk_generation
        return reader(Path(state['pid_file']))

    def disabled(self):
        if self.host == 'mini':
            with self.connect() as db:
                if self.state(db).get('mini_handoff', {}).get('phase') not in (None, 'done', 'withdrawn', 'aborted'):
                    return True
        for target in (self.directory / 'slots.disabled', self.directory / 'maintenance', self.directory.parent / 'rack/actions.disabled'):
            try:
                target.lstat()
            except FileNotFoundError:
                continue
            return True
        return False

    def observed(self):
        snapshot = self.pueue.status()
        self._status_snapshot = snapshot  # private; never published or logged
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
                if self.host == 'desk':
                    admission_state(db, migrate=True)
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
                     tables={m: mode_profile(m) for m in ('dia', 'noche')}, **self.notes(state)) if self.host == 'desk' else dict(
                         slots_note='Lugares ajustados · hasta otro cambio manual' if state['overrides'] else None)
        reorder_ready = (self.host=='mini' and enabled and not pending and observed==state['desired']
                         and generation==state['generation'] and self.reorder_host_ready(state))
        reorder_groups = self.reorder_view(state, self._status_snapshot, reorder_ready) if self.host=='mini' else {}
        return dict(schema='rack-host-result@1', status='ok', reorder_groups=reorder_groups, capabilities=dict(
            slots=enabled, undo=enabled, dry_run=enabled, mode=enabled and self.host == 'desk' and extra.get('mode_note') is not None, reorder=any(g['enabled'] for g in reorder_groups.values()), restart_queue=False,
            reason_reorder='La coordinación de pausas de los clientes no está acreditada.' if self.host=='mini' and not self.coordinated_reorder else None),
            revision=state['revision'], policy_hash=self.policy_hash, generation=generation, mode=self.mode(state), cap=self.cap(state),
            slots=observed, desired=state['desired'], drift=observed != state['desired'],
            **(dict(foreign_work=self.foreign_work) if self.host == 'desk' else {}),
            reason=None if enabled else 'Los ajustes de lugares están apagados en este host.',
            recovery_pending=bool(pending) or generation != state['generation'],
            startup_holds={g:state.get('startup', {}).get('holds', {}).get(g, 'pausa manual' if 'manual' in r else 'pausa de origen desconocido')
                           for g,r in state['reasons'].items() if 'startup' in r and set(r) & {'manual', 'ambiguous'}},
            startup_waiting={g:'cuotas_sensores' for g,r in state['reasons'].items()
                             if 'startup' in r and not set(r) & {'manual', 'ambiguous'}},
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
            if request['op'] in ('slots', 'mode', 'undo', 'reorder') and not request['dry_run']:
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
            uncertain = request['op'] == 'status' or request['op'] in ('slots', 'mode', 'undo', 'reorder') and not request['dry_run']
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
        if op == 'reorder' or op == 'undo' and 'queue' in request['expected']:
            return self.reorder_handle(request)
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
                               **self.notes(projected))
            if self.resolve(state) != state:  # acceptance fence after potentially blocking notes
                return self.reply(request, 'conflict', 'undo_conflict' if op == 'undo' else 'stale_state', effects=False)
            if op == 'mode' and payload.get('mode_note') is None:
                return self.reply(request, 'rejected', 'unsupported', effects=False)
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

    # Reorder uses this controller's existing lock, durable index and guard job.
    # Única compuerta durable: CLIENTES COORDINADOS. La unidad de clientes la
    # encenderá en este fuente después de acreditar TODOS sus escritores Mini.
    # No existe habilitación por entorno, JSON, instalador ni CLI de producción.
    coordinated_reorder = False
    reorder_budget = 3.0
    reorder_rpc = 1.0
    # Calibración provisional, sustituida sólo tras la corrida privada final.
    reorder_overhead = 1.0
    reorder_cost = .35
    reorder_boundary = staticmethod(lambda phase: None)
    reorder_measure = staticmethod(lambda event, seconds: None)

    def reorder_host_ready(self, state):
        handoff=state.get('mini_handoff',{})
        return (self.host=='mini' and handoff.get('phase')=='done' and handoff.get('target')=='install'
                and state.get('startup_version')==1 and state.get('startup',{}).get('phase')=='ready')

    def reorder_rows(self, snapshot, group, generation):
        rows = []
        for ident, task in snapshot['tasks'].items():
            if task['group'] != group or task['status'] == 'Done':
                continue
            name = next(iter(task['status'])) if type(task['status']) is dict else task['status']
            if name != 'Queued':
                continue
            validate_task(str(ident), task)
            rows.append(dict(id=task['id'], identity=self.task_identity(task), priority=task['priority']))
        return sorted(rows, key=lambda t: (-t['priority'], t['id']))

    @staticmethod
    def task_identity(task):
        fields = ('id','created_at','group','command','original_command','path','envs','dependencies','label')
        return hashlib.sha256(canonical({k:task[k] for k in fields}).encode()).hexdigest()

    @staticmethod
    def reorder_etag(rows, generation):
        return hashlib.sha256(canonical(dict(generation=generation, state='Queued', queue=rows)).encode()).hexdigest()

    def reorder_actions(self):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT body FROM host_actions')
                    if json.loads(r[0]).get('kind') == 'reorder']

    def reorder_view(self, state, snapshot, enabled):
        generation = self.generation(state)
        pending = [a for a in self.reorder_actions() if a['phase'] != 'terminal']
        result = {}
        for g in self.profile:
            queue = self.reorder_rows(snapshot, g, generation)
            reasons = list(state['reasons'].get(g, []))
            paused = snapshot['groups'][g]['status'] != 'Running' or bool(reasons)
            labels=dict(pressure='presión',manual='pausa manual',ambiguous='origen desconocido',startup='retenido al reiniciar',handoff='relevo',reorder='recuperación del orden')
            recovery = next((a['request']['action_id'] for a in pending if a['group'] == g), None)
            unsafe = any(t['group']==g and isinstance(t['status'],dict) and set(t['status']) & {'Paused','Locked','Stashed'} for t in snapshot['tasks'].values())
            if self.coordinated_reorder and not unsafe:
                try:
                    aliases = self.reorder_aliases(state)
                    for row in queue:
                        self.reorder_preflight(snapshot['tasks'][str(row['id'])], aliases)
                except (ValueError,OSError):
                    unsafe = True
            reason = ('En pausa recuperando… · ' + recovery if recovery else
                      'No se puede reordenar mientras el grupo está pausado · ' + ', '.join(labels[r] for r in (reasons or ['manual'])) if paused else
                      'La coordinación de pausas de los clientes no está acreditada.' if not self.coordinated_reorder else
                      'Fila demasiado larga para reordenar de una vez' if len(queue) > 64 else
                      'Se necesitan al menos dos pendientes.' if len(queue)<2 else
                      'Esta fila no se puede editar con seguridad.' if unsafe else
                      'La fila supera el presupuesto de pausa medido.' if self.reorder_overhead + len(queue)*self.reorder_cost > self.reorder_budget else
                      'Hay un ajuste pendiente o el host no está preparado.' if not enabled else None)
            result[g] = dict(enabled=reason is None, reason=reason, owner=recovery,
                             expected=dict(revision=state['revision'], admission_revision=state['revision'],
                                 policy_hash=self.policy_hash, generation=generation, group=g,
                                 queue_etag=self.reorder_etag(queue, generation), queue=queue if len(queue)<=64 else []))
        return result

    def reorder_save(self, action, phase=None, state=None):
        if phase:
            action['phase'] = phase
        with self.connect(write=True) as db:
            if state is not None:
                self.store(db, state)
            db.execute('UPDATE host_actions SET body=? WHERE action_id=?',
                       (canonical(action), action['request']['action_id']))
        self.reorder_boundary(phase or action['phase'])

    def reorder_reply(self, request, status, code=None, **fields):
        messages = dict(queue_conflict='La fila cambió. Actualiza antes de reordenar.',
                        undo_conflict='La fila cambió; no se puede deshacer',
                        paused='No se puede reordenar mientras el grupo está pausado',
                        uncoordinated='La coordinación de pausas de los clientes no está acreditada.',
                        edit_unsafe='Esta fila no se puede editar con seguridad.',
                        queue_too_long='Fila demasiado larga para reordenar de una vez',
                        pause_budget='La fila supera el presupuesto de pausa medido.',
                        recovery_error='En pausa recuperando…')
        if code not in messages:
            return self.reply(request, status, code, **fields)
        return dict(schema='rack-host-result@1', action_id=request.get('action_id'), status=status,
                    error=dict(code=code, message=messages[code]), **fields)

    @contextmanager
    def reorder_deadline(self):
        old_timeout = getattr(self.pueue, 'timeout', None)
        old_deadline = getattr(self.pueue, 'deadline', None)
        self.pueue.timeout = min(old_timeout or self.reorder_rpc, self.reorder_rpc)
        self.pueue.deadline = min(old_deadline or float('inf'), time.monotonic()+self.reorder_budget)
        try:
            yield
        finally:
            self.pueue.timeout, self.pueue.deadline = old_timeout, old_deadline

    def reorder_aliases(self, state):
        # U3.5 execs stock with no argv overrides, HOME/cwd fixed to the owner.
        # A changed config cannot attest the running daemon's cached settings.
        # The private seam names the actual daemon config, NEVER edit_settings.
        source = getattr(self.pueue, 'daemon_config', None)
        home = Path.home()
        if source is None:
            standard = home/'Library/Application Support/pueue/pueue.yml'
            source = standard if standard.is_file() else home/'pueue.yml'
        source = Path(source)
        daemon_pid = int(Path(state['pid_file']).read_text().strip())
        born = priority_helper().birth(daemon_pid)
        if not born or not born.startswith('darwin:'):
            raise ValueError('edit_unsafe')
        _, sec, usec = born.split(':')
        if source.exists() and source.stat().st_mtime_ns > int(sec)*10**9+int(usec)*1000:
            raise ValueError('edit_unsafe')
        configured = pueue_settings(source)['alias_file']
        alias = Path(configured).expanduser() if configured else home/'Library/Application Support/pueue/pueue_aliases.yml'
        if not alias.is_absolute():
            raise ValueError('edit_unsafe')
        return read_aliases(alias)

    def reorder_preflight(self, task, aliases):
        priority_helper().preflight(task)
        words = task['original_command'].split()
        if words and words[0] in aliases:
            raise ValueError('edit_unsafe')

    def reorder_handle(self, request):
        with self.reorder_deadline():
            return self._reorder_handle(request)

    def _reorder_handle(self, request):
        digest = hashlib.sha256(canonical(dict(host=self.host, request=request)).encode()).hexdigest()
        with self.connect() as db:
            row = db.execute('SELECT digest,body FROM host_actions WHERE action_id=?', (request['action_id'],)).fetchone()
            state = self.state(db)
            previous = db.execute('SELECT body FROM host_actions WHERE action_id=?', (request.get('undo_of',''),)).fetchone()
            previous = json.loads(previous[0]) if previous else None
            pending = any(json.loads(r[0])['response']['status'] in ('applying','partial','unknown') or
                          json.loads(r[0]).get('kind') == 'reorder' and json.loads(r[0])['phase'] != 'terminal'
                          for r in db.execute('SELECT body FROM host_actions'))
        if row:
            return (self.reply(request, 'conflict', 'idempotency_conflict') if digest != row[0] else json.loads(row[1])['response'])
        def reject(code, status='rejected', **extra):
            return self.reorder_reply(request, status, code, effects=False, **extra)
        if self.host != 'mini':
            return reject('unsupported')
        if self.disabled():
            return reject('disabled')
        if not self.coordinated_reorder:
            return reject('uncoordinated')
        if pending:
            return reject('busy')
        if not self.reorder_host_ready(state):
            return reject('unsupported')
        g = request['expected']['group']
        before, groups = self.observed()
        snapshot = self._status_snapshot
        gen = self.generation(state)
        rows = self.reorder_rows(snapshot, g, gen)
        expected = dict(revision=state['revision'], admission_revision=state['revision'], policy_hash=self.policy_hash,
                        generation=gen, group=g, queue_etag=self.reorder_etag(rows, gen), queue=rows)
        undo = request['op'] == 'undo'
        conflict = 'undo_conflict' if undo else 'queue_conflict'
        if expected != request['expected'] or gen != state['generation'] or state['desired'] != before:
            return reject(conflict, 'conflict')
        reasons = state['reasons'].get(g, [])
        if groups[g]['status'] != 'Running' or reasons or os.path.lexists(self.pause_fence(g)):
            return reject('paused', pause_reasons=reasons or ['manual'])
        if (state.get('startup', {}).get('phase') not in (None, 'ready') or
                any(t['group'] == g and (next(iter(t['status'])) if isinstance(t['status'],dict) else t['status'])
                    in ('Paused','Locked','Stashed') for t in snapshot['tasks'].values())):
            return reject('edit_unsafe')
        if not 2 <= len(rows) <= 64:
            return reject('undo_conflict' if undo else 'queue_too_long','conflict' if undo else 'rejected')
        if undo:
            if (not previous or previous.get('kind') != 'reorder' or previous['response']['status'] != 'applied'
                    or state['revision'] != previous['revision'] or previous['generation'] != gen
                    or self.clock() > previous.get('undo_until', 0) or previous['target'] != rows
                    or previous['group'] != g):
                return reject('undo_conflict', 'conflict')
            target = deepcopy(previous['before'])
        else:
            by_id = {t['id']:t for t in rows}
            target = [dict(by_id[ident], priority=len(rows)-i) for i, ident in enumerate(request['change']['order'])]
        changes = [t for t in target if next(r['priority'] for r in rows if r['id'] == t['id']) != t['priority']]
        if self.reorder_overhead + len(changes)*self.reorder_cost > self.reorder_budget:
            return reject('pause_budget')
        try:
            aliases = self.reorder_aliases(state)
            for row in rows:
                self.reorder_preflight(snapshot['tasks'][str(row['id'])], aliases)
        except (ValueError, OSError):
            return reject('edit_unsafe')
        payload = dict(group=g, before=rows, desired=target, observed=rows,
                       queue_etag=self.reorder_etag(target, gen), undo_of=request['action_id'])
        if request.get('dry_run') is True:
            return self.reorder_reply(request, 'dry_run', effects=False, result=payload)
        state['revision'] += 1
        state['reasons'][g] = ['reorder']
        action = dict(kind='reorder', request=request, generation=gen, group=g, before=rows, target=target,
                      enqueued={str(t['id']):snapshot['tasks'][str(t['id'])]['status']['Queued']['enqueued_at'] for t in rows},
                      revision=state['revision'], phase='prepared', at=self.clock(), process=None, pause_owner=request['action_id'],
                      response=self.reorder_reply(request, 'applying', result=payload, revision=state['revision']))
        with self.connect(write=True) as db:
            self.store(db, state)

            db.execute('INSERT INTO host_actions VALUES (?,?,?)', (request['action_id'], digest, canonical(action)))
        self.reorder_boundary('prepared')
        deadline = time.monotonic() + self.reorder_budget
        old_timeout = getattr(self.pueue, 'timeout', None)
        old_deadline = getattr(self.pueue, 'deadline', None)
        self.pueue.deadline = deadline
        self.pueue.timeout = self.reorder_rpc
        try:
            self.reorder_boundary('pause_begin')
            self.pueue.call('pause','--wait','-g',g)
            self.reorder_boundary('pause_effect')
            snap = self.pueue.status()
            if snap['groups'][g]['status'] != 'Paused':
                raise ValueError('Pausa sin confirmar')
            self.reorder_save(action, 'paused')
            if (snap['groups'][g]['status'] != 'Paused' or self.generation(state) != gen or
                    self.reorder_rows(snap,g,gen) != rows):
                action['outcome'] = 'aborted'
                self.reorder_save(action, 'releasing')
            else:
                for row in changes:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('pause_budget')
                    self._reorder_edit_snapshot = snap
                    try:
                        self.edit_priority(action, row['id'], row['priority'], deadline)
                    finally:
                        self._reorder_edit_snapshot = None
                    snap['tasks'][str(row['id'])]['priority'] = row['priority']
                self.reorder_boundary('verification_begin')
                snap = self.pueue.status()
                self.verify_reorder(action, snap, target)
                self.reorder_boundary('verification_end')
                self.reorder_save(action, 'edited')
            self.reconcile()
        except (OSError, ValueError, TimeoutError, subprocess.SubprocessError) as error:
            # Safe diagnostics never serialize argv, stderr, TOML or task fields.
            latest = next(a for a in self.reorder_actions() if a['request']['action_id']==request['action_id'])
            latest['failure'] = dict(kind=type(error).__name__, phase=latest['phase'],
                                    exit_code=getattr(error, 'returncode', None))
            self.reorder_save(latest)
            # Durable intent stays owned. The existing guard repairs it even if
            # slots.disabled/actions.disabled is set after acceptance.
            return self.reorder_reply(request, 'unknown', 'recovery_error')
        finally:
            self.pueue.timeout = old_timeout
            self.pueue.deadline = old_deadline
        return self._handle(dict(schema=HOST_SCHEMA, op='status', action_id=request['action_id']))

    def verify_reorder(self, action, snapshot, target, allow_missing=False):
        for row in target:
            task = snapshot['tasks'].get(str(row['id']))
            if task is None and allow_missing:
                continue
            if task is None or self.task_identity(task) != row['identity']:
                raise ValueError('recovery_error')
            if allow_missing and set(task['status']) & {'Done','Running'}:
                continue
            if set(task['status']) != {'Queued'} or task['priority'] != row['priority']:
                raise ValueError('recovery_error')

    def edit_priority(self, action, ident, priority, deadline):
        begun = time.monotonic()
        import tempfile
        helper = priority_helper()
        snapshot = getattr(self, '_reorder_edit_snapshot', None) or self.pueue.status()
        self.reorder_measure('parent_status',time.monotonic()-begun)
        with self.connect() as db:
            authority = self.state(db)
        row = next(r for r in action['before'] if r['id']==ident)
        task = snapshot['tasks'].get(str(ident))
        if (task is None or self.task_identity(task)!=row['identity'] or set(task['status'])!={'Queued'}
                or snapshot['groups'][action['group']]['status']!='Paused'
                or self.generation(authority)!=action.get('repair_generation',action['generation'])
                or task['priority'] not in (row['priority'],next(r['priority'] for r in action['target'] if r['id']==ident))):
            raise ValueError('recovery_error')
        self.reorder_preflight(task, self.reorder_aliases(authority))
        root = Path(tempfile.mkdtemp(prefix='edit-', dir=str(self.directory)))
        root.chmod(0o700)
        # A new config changes only this private client's edit mode / aliases.
        # Connection paths originate in authority, never in a JSON request.
        with self.connect() as db:
            data = Path(self.state(db)['pid_file']).parent
        settings = dict(shared=dict(pueue_directory=str(data), runtime_directory=str(data),
                         pid_path=str(data/'pueue.pid'), unix_socket_path=str(data/('pueue_'+__import__('pwd').getpwuid(os.getuid()).pw_name+'.socket')),
                         shared_secret_path=str(data/'shared_secret'), alias_file=str(root/'aliases.yml')),
                        client=dict(edit_mode='toml'))
        if hasattr(self.pueue, 'edit_settings'):
            settings = deepcopy(self.pueue.edit_settings)  # private harness seam, no wire input
            settings.setdefault('client', {})['edit_mode'] = 'toml'
            settings['shared']['alias_file'] = str(root/'aliases.yml')
        atomic_json(root/'config.json', settings)
        (root/'aliases.yml').write_text('{}\n'); (root/'aliases.yml').chmod(0o600)
        daemon_pid=int(Path(authority['pid_file']).read_text().strip())
        daemon_birth=helper.birth(daemon_pid)
        if not daemon_birth:
            raise ValueError('Daemon no verificable')
        plan = dict(id=ident, priority=priority, binary=self.pueue.binary,identity=row['identity'],
                    expected_priority=task['priority'],daemon=dict(pid=daemon_pid,birth=daemon_birth),
                    fingerprint=helper.fingerprint(task['original_command'],task['path'],task['label']),
                    deadline=min(deadline,time.monotonic()+self.reorder_rpc), launched=time.monotonic())
        atomic_json(root/'plan.json', plan)
        action['editing_id'] = ident
        action['process'] = dict(root=str(root), client=None)
        self.reorder_save(action, 'edit_intent')
        env = dict(os.environ, TMPDIR=str(root), EDITOR=shlex.join(['/usr/bin/python3','-I',
                   str(Path(__file__).with_name('editor_prioridad.py')), str(root/'plan.json')]))
        self.reorder_measure('prepare_edit',time.monotonic()-begun)
        launch_begun=time.monotonic()
        helper.remaining(plan)
        client = subprocess.Popen(['/usr/bin/python3','-I',str(Path(__file__).with_name('editor_prioridad.py')),
                                   '--client',str(root/'plan.json')], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, env=env, cwd=str(root))
        self.reorder_measure('launch_client',time.monotonic()-launch_begun)
        signature = helper.birth(client.pid)
        if not signature:
            client.stdin.close()
            client.wait(timeout=1)
            raise ValueError('No se acreditó el cliente propio')
        plan['client'] = dict(pid=client.pid, birth=signature)
        atomic_json(root/'plan.json', plan)
        action['process']['client'] = plan['client']
        self.reorder_save(action, 'client_ready')
        try:
            helper.remaining(plan)
            cli_begun=time.monotonic()
            client.stdin.write(b'1'); client.stdin.close()
            self.reorder_boundary('client_sent')
            client.wait(timeout=max(.001, plan['deadline']-time.monotonic()))
            self.reorder_measure('cli',time.monotonic()-cli_begun)
            if client.returncode != 0:
                raise subprocess.CalledProcessError(client.returncode, ['private-priority-client'])
            metrics = root/'metrics.json'
            if metrics.is_file():
                for key,value in json.loads(metrics.read_text()).items():
                    self.reorder_measure(key,value)
        except Exception:
            self.stop_editor(action)
            # Keep durable ownership until all identified processes have exited.
            raise
        self.reorder_boundary('edit_effect')
        action['process'] = None
        self.reorder_save(action, 'editing')
        self.reorder_measure('edit_total',time.monotonic()-begun)

    def stop_editor(self, action):
        info = action.get('process')
        if not info:
            return True
        helper = priority_helper()
        root = Path(info['root'])
        if root.parent != self.directory or root.is_symlink():
            raise ValueError('recovery_error')
        atomic_json(root/'revoked', dict(revoked=True))
        identities = [info.get('client')]
        editor = root/'editor.json'
        if editor.exists():
            identities.append(json.loads(editor.read_text()))
        signaled = False
        for identity in identities:
            if identity is None:
                # Spawn was not released through stdin before client_ready commit.
                continue
            if helper.birth(identity['pid']) == identity['birth']:
                os.kill(identity['pid'], 15)
                signaled = True
        # No second edit until every recorded process has exited.
        if signaled:
            return False
        action['process'] = None
        self.reorder_save(action)
        return True

    def recover_reorders(self, release=False):
        old_timeout = getattr(self.pueue,'timeout',None)
        old_deadline = getattr(self.pueue,'deadline',None)
        self.pueue.timeout = self.reorder_rpc
        self.pueue.deadline = min(old_deadline or float('inf'),time.monotonic()+self.reorder_budget)
        try:
            return self._recover_reorders(release)
        finally:
            self.pueue.timeout = old_timeout
            self.pueue.deadline = old_deadline

    def _recover_reorders(self, release=False):
        if self.host != 'mini':
            return True
        for action in self.reorder_actions():
            if action['phase'] == 'terminal':
                continue
            if not self.stop_editor(action):
                return False
            with self.connect() as db:
                state = self.state(db)
            g = action['group']
            snap = self.pueue.status()
            if action['phase'] not in ('edited','releasing'):
                # The old process is dead; only attributable Locked/Stashed may
                # be enqueued. Never restore a missing or terminal cancellation.
                for row in action['before']:
                    task = snap['tasks'].get(str(row['id']))
                    if task is None:
                        continue
                    if self.task_identity(task) != row['identity']:
                        raise ValueError('recovery_error')
                    name = next(iter(task['status'])) if isinstance(task['status'],dict) else task['status']
                    owned_lock = name == 'Locked' and row['id']==action.get('editing_id') and set(task['status']['Locked']['previous_status']) == {'Queued'}
                    # A stock-converted Stashed cannot prove it was not an
                    # external cancellation. The preparer resolves our Locked
                    # image first; a bypassed preparer remains held for inspection.
                    if owned_lock:
                        data = Path(state['pid_file']).parent
                        if os.path.lexists(data/'task_logs'/(str(row['id'])+'.log')):
                            raise ValueError('recovery_error')
                        self.pueue.call('enqueue',str(row['id']))
                snap = self.pueue.status()
                for row in action['before']:
                    ident = str(row['id'])
                    task = snap['tasks'].get(ident)
                    if task and set(task['status'])=={'Queued'} and self.task_identity(task)==row['identity']:
                        after = task['status']['Queued']['enqueued_at']
                        if after != action['enqueued'][ident]:
                            action.setdefault('recovered_enqueued',{})[ident] = dict(identity=row['identity'], before=action['enqueued'][ident], after=after)
                self.reorder_save(action)
                action['repair_generation'] = self.generation(state)
                try:
                    self.verify_reorder(action,snap,action['target'])
                    action['outcome'] = 'applied'
                except ValueError:
                    action['outcome'] = 'aborted'
                    for row in action['before']:
                        task = snap['tasks'].get(str(row['id']))
                        if task is None or set(task['status']) != {'Queued'}:
                            continue
                        if self.task_identity(task) != row['identity']:
                            raise ValueError('recovery_error')
                        if task['priority'] not in (row['priority'], next(t['priority'] for t in action['target'] if t['id']==row['id'])):
                            raise ValueError('recovery_error')
                        if task['priority'] != row['priority']:
                            if snap['groups'][g]['status'] != 'Paused':
                                raise ValueError('recovery_error')
                            self.edit_priority(action,row['id'],row['priority'],self.pueue.deadline)
                    self.verify_reorder(action,self.pueue.status(),action['before'],allow_missing=True)
                self.reorder_save(action,'releasing')
            if not release:
                continue
            # Quotas/generation have been reconciled by reconcile() first.
            self.reorder_boundary('admission_begin')
            snap = self.pueue.status()
            applied = action.get('outcome', 'applied') == 'applied'
            if applied:
                # An execution after an already-sent start is a historic success.
                if action.get('start_intent') and snap['groups'][g]['status'] == 'Running':
                    pass
                else:
                    self.verify_reorder(action,snap,action['target'],allow_missing=True)
            if any(t['group']==g and isinstance(t['status'],dict) and set(t['status']) & {'Locked','Stashed','Paused'} for t in snap['tasks'].values()):
                raise ValueError('recovery_error')
            reasons = set(state['reasons'].get(g,[])) - {'reorder'}
            if os.path.lexists(self.pause_fence(action['group'])):
                reasons.add('manual')
            # Transfer ownership before opening, so a crash here remains recoverable.
            reasons.add('pressure')
            state['reasons'][g] = sorted(reasons)
            self.reorder_save(action,'releasing',state)
            fresh = pressure_mini(timeout=min(self.reorder_rpc,max(.001,self.pueue.deadline-time.monotonic())))
            threshold = RAPIDAS_THRESHOLDS['mini'] if g == 'rapidas' else MINI_THRESHOLDS
            before, _ = self.observed()
            can_open = (reasons <= {'pressure'} and decide('mini',fresh,threshold) == 'reanudar'
                        and before == state['desired'] and self.generation(state) == state['generation']
                        and not os.path.lexists(self.directory/'maintenance')
                        and state.get('startup',{}).get('phase') in (None,'ready'))
            if can_open and snap['groups'][g]['status'] == 'Paused':
                # No individual Paused tasks may be resumed by start -g.
                if any(t['group']==g and isinstance(t['status'],dict) and set(t['status'])=={'Paused'} for t in self._status_snapshot['tasks'].values()):
                    raise ValueError('recovery_error')
                action['start_intent'] = True
                self.reorder_save(action,'releasing')
                sampled = time.monotonic()
                fresh = pressure_mini(timeout=min(self.reorder_rpc,max(.001,self.pueue.deadline-time.monotonic())))
                if decide('mini',fresh,threshold) != 'reanudar' or time.monotonic()-sampled > self.reorder_rpc:
                    self.pueue.call('pause','--wait','-g',g)
                else:
                    self.pueue.call('start','-g',g)
                    self.reorder_boundary('start_effect')
                    state['reasons'][g] = []
            elif snap['groups'][g]['status'] == 'Paused':
                # enqueue doesn't persist; save under our existing pause only.
                self.pueue.call('pause','--wait','-g',g)
            elif snap['groups'][g]['status'] == 'Running':
                state['reasons'][g] = sorted(reasons - {'pressure'})
            else:
                raise ValueError('recovery_error')
            self.reorder_boundary('admission_end')
            self.reorder_boundary('receipt_begin')
            final = self.pueue.status()
            action['undo_until'] = self.clock()+60
            observed = self.reorder_rows(final,g,self.generation(state))
            action['response'] = self.reorder_reply(action['request'],'applied' if applied else 'aborted',
                None if applied else 'queue_conflict', revision=action['revision'],
                result=dict(group=g,before=action['before'],desired=action['target'],observed=observed[:64],queue_complete=len(observed)<=64,
                            group_status=final['groups'][g]['status'],pause_reasons=state['reasons'].get(g,[]),
                            queue_etag=self.reorder_etag(observed,self.generation(state)),
                            undo_of=action['request']['action_id'] if applied else None))
            self.reorder_save(action,'terminal',state)
            self.reorder_boundary('receipt_end')
        return True

    def reorder_offline(self, queue, state, data):
        image = deepcopy(queue)
        for action in self.reorder_actions():
            if action['phase'] == 'terminal':
                continue
            info = action.get('process')
            if info:
                identities = [info.get('client')]
                editor = Path(info['root'])/'editor.json'
                if editor.exists():
                    identities.append(json.loads(editor.read_text()))
                if any(p and priority_helper().birth(p['pid']) == p['birth'] for p in identities):
                    raise ValueError('Editor propio aún vivo; conservar cierre')
            target = action['target'] if action['phase'] in ('edited','releasing') and action.get('outcome','applied')=='applied' else action['before']
            for row in target:
                ident = str(row['id'])
                task = image['tasks'].get(ident)
                if task is None:
                    continue
                if self.task_identity(task) != row['identity']:
                    raise ValueError('Identidad de reorder cambió; conservar cierre')
                name, detail = next(iter(task['status'].items()))
                if name == 'Locked' and row['id'] == action.get('editing_id'):
                    if set(detail['previous_status']) != {'Queued'} or os.path.lexists(data/'task_logs'/(ident+'.log')):
                        raise ValueError('Locked de reorder sin atribución segura')
                    task['status'] = deepcopy(detail['previous_status'])
                    name = 'Queued'
                if name == 'Queued' and not os.path.lexists(data/'task_logs'/(ident+'.log')):
                    task['priority'] = row['priority']
            # Action remains pending until online reconciliation and fresh admission.
        return image

    def record_stop(self):
        with self.connect() as db:
            state = self.state(db)
        snapshot = self.pueue.status()
        state['stop_tasks'] = {ident:task['created_at'] for ident,task in snapshot['tasks'].items()
                               if isinstance(task['status'], dict) and set(task['status']) & {'Running', 'Paused'}}
        with self.connect(write=True) as db:
            self.store(db, state)

    def retire_empty(self, group):
        # Explicit only. Caller holds control.lock; never retire on deployment.
        if self.host != 'desk' or group not in ('suites', 'pg5434'):
            raise ValueError('Retiro no autorizado para este grupo')
        with self.connect() as db:
            state = self.state(db)
        snapshot = self.pueue.status()
        if any(t['group'] == group for t in snapshot['tasks'].values()):
            raise ValueError('Grupo con tareas; no se retira')
        state['retire_intent'] = group
        with self.connect(write=True) as db:
            self.store(db, state)
        self.finish_retirement(state)

    def finish_retirement(self, state):
        group = state.get('retire_intent')
        if group is None:
            return
        if group not in ('suites', 'pg5434'):
            raise ValueError('Intención de retiro inválida')
        snapshot = self.pueue.status()
        if any(t['group'] == group for t in snapshot['tasks'].values()):
            state.pop('retire_intent', None)
            state['reasons'][group] = sorted(set(state['reasons'].get(group, [])) | {'manual'})
            with self.connect(write=True) as db:
                self.store(db, state)
            return False
        if group in snapshot['groups']:
            self.pueue.call('group', 'remove', group)
        if group in self.pueue.status()['groups']:
            raise ValueError('Retiro sin confirmar')
        state['reasons'].pop(group, None)
        state.get('startup', {}).get('holds', {}).pop(group, None)
        state.pop('retire_intent', None)
        with self.connect(write=True) as db:
            self.store(db, state)

    def reconcile(self):
        if self.host == 'mini' and any(a['phase'] != 'terminal' for a in self.reorder_actions()):
            with self.reorder_deadline():
                return self._reconcile()
        return self._reconcile()

    def _reconcile(self):
        if os.path.lexists(self.directory / 'maintenance'):
            return False
        with self.connect() as db:
            stored = self.state(db)
            state = self.resolve(stored)
        if not self.recover_reorders():
            return False
        with self.connect() as db:
            state = self.resolve(self.state(db))
        if state.get('startup', {}).get('phase') == 'intent':
            raise ValueError('Preparación incompleta; no se admite trabajo')
        if state != stored:
            with self.connect(write=True) as db:
                self.store(db, state)  # durable expiration precedes any RPC
        if state.get('retire_intent'):
            self.finish_retirement(state)
        observed = None
        failure = False
        try:
            generation = self.generation(state)
            observed, groups = self.observed()
            absent = set(state['reasons']) - set(groups)
            if absent:
                for g in absent:
                    state['reasons'].pop(g, None)
                    state.get('startup', {}).get('holds', {}).pop(g, None)
                with self.connect(write=True) as db:
                    self.store(db, state)
            if observed != state['desired']:
                validate_target(observed, state['desired'], self.host, self.cap(state))
            if generation != state['generation']:
                # A paused group after restore has ambiguous origin. Never open it.
                startup = state.get('startup', {})
                prepared = (startup.get('phase') == 'prepared' and
                            startup.get('previous_generation') == state['generation'])
                for g, row in groups.items():
                    if row['status'] == 'Paused' and not (prepared and 'startup' in state['reasons'].get(g, [])):
                        state['reasons'][g] = sorted(set(state['reasons'].get(g, [])) | {'ambiguous'})
                if prepared:
                    startup['phase'] = 'ready'
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
                if action.get('kind') == 'reorder':
                    continue
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
                                   **self.notes(state))
                else:
                    payload['slots_note'] = 'Lugares ajustados · hasta otro cambio manual' if state['overrides'] else None
                action['response'] = self.reply(action['request'], status, 'expired' if expired else None,
                                               revision=state['revision'], result=payload)
                if self.resolve(state) != state:  # immediately before each durable receipt
                    db.rollback()
                    crossed_event = True
                    break
                db.execute('UPDATE host_actions SET body=? WHERE action_id=?', (canonical(action), key))
        if crossed_event:
            return self.reconcile()  # release the receipt transaction before recovering
        if not failure and not self.recover_reorders(release=True):
            return False
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

    def _admission_gate(self, state, group=None, proc=Path('/proc'), operation='resume'):
        """One bounded sequence, repeated once after reservation; no retry loop.

        The caller owns control.lock. External Pueue writers remain outside it.
        No SQLite transaction spans Docker/Pueue. Returned status is private.
        """
        started = time.monotonic()
        stage = 'authority'
        try:
            if os.path.lexists(self.directory / 'maintenance'):
                return 'maintenance', None, None, started
            if state.get('startup') and state['startup']['phase'] != 'ready':
                return 'startup', None, None, started
            with self.connect() as db:
                current = self.state(db)
                if current != state:
                    return 'authority', None, None, started
                receipts = [json.loads(r[0]) for r in db.execute('SELECT body FROM host_actions')]
            if any(a['response']['status'] in ('applying','partial','unknown') for a in receipts):
                return 'admission', None, None, started
            if self.host == 'desk' and state.get('startup_version') == 1:
                stage = 'docker'
                subprocess.run(['/usr/bin/docker','info'],check=True,capture_output=True,timeout=5)
            stage = 'desired'
            vector, _ = self.observed()
            snapshot = self._status_snapshot
            if vector != state['desired'] or any(snapshot['groups'][g]['status'] not in ('Running','Paused') for g in self.profile):
                return 'desired', None, None, started
            stage = 'generation'
            if self.generation(state) != state['generation']:
                return 'generation', None, None, started
            stage = 'fresh_sample'
            sample = pressure_mini() if self.host == 'mini' else pressure(proc)
            threshold = RAPIDAS_THRESHOLDS[self.host] if group == 'rapidas' else (MINI_THRESHOLDS if self.host == 'mini' else PRESSURE_THRESHOLDS)
            expected = 'mantener' if operation == 'one' else 'reanudar'
            if decide(self.host,sample,threshold) != expected or time.monotonic()-started > GATE_SECONDS:
                return 'fresh_sample', None, None, started
            stage = 'event'
            if self._event_stale(state):
                return 'event', None, None, started
            return None, snapshot, sample, started
        except ADMISSION_ERRORS:
            return stage, None, None, started

    def _event_stale(self, state):
        try:
            return self.resolve(state) != state
        except ADMISSION_ERRORS:
            return True

    def admission_ready(self, state, group=None, proc=Path('/proc')):
        if self.host == 'mini':
            return self._legacy_admission_ready(state,group,proc)
        # group=None historically checks Docker/vector/event, without a sensor.
        if group is None:
            return self._legacy_admission_ready(state,group,proc)
        return self._admission_gate(state,group,proc)[0] is None

    def admission_plan(self, db):
        return admission_state(db)

    def _save_admission(self, value):
        validate_admission(value)
        with self.connect(write=True) as db:
            # Never create/reset at runtime. Only installation/adoption migrates.
            if admission_state(db) is None:
                raise ValueError('Permiso sin migrar')
            db.execute('UPDATE guard_admission SET body=? WHERE id=1',(canonical(value),))

    def _candidate(self, snapshot, state, block):
        candidates = []
        tasks = snapshot['tasks']
        if type(tasks) is not dict:
            raise ValueError('Cola inválida')
        for key, task in tasks.items():
            if (type(task) is not dict or type(task.get('group')) is not str
                    or type(task.get('id')) is not int or str(task['id']) != key
                    or not 0 <= task['id'] < 2**64):
                raise ValueError('Identidad de cola inválida')
        for group in self.profile:
            if group == 'rapidas':
                continue
            row = snapshot['groups'][group]
            waiting = [t for t in tasks.values() if t.get('group') == group and type(t.get('status')) is dict and 'Queued' in t['status']]
            if not waiting:
                continue
            reasons = set(state['reasons'].get(group,[]))
            if os.path.lexists(self.pause_fence(group)):
                reasons.add('manual')
            if row['status'] != 'Paused':
                continue  # open group: waits on its own Pueue quota, not on the guard
            if reasons != {'pressure'}:
                block('reason',group)
                continue
            occupied = 0
            for task in tasks.values():
                if task.get('group') != group:
                    continue
                status = task.get('status')
                if type(status) is not dict or len(status) != 1 or next(iter(status)) not in ('Queued','Stashed','Locked','Running','Paused','Done'):
                    raise ValueError('Estado desconocido')
                occupied += bool(set(status) & {'Running','Paused','Locked'})
            if occupied >= row['parallel_tasks']:
                block('quota',group)
                continue
            for task in waiting:
                try:
                    ident = task['id']
                    if type(ident) is not int or not 0 <= ident < 2**64 or tasks.get(str(ident)) != task or len(task['status']) != 1:
                        raise ValueError('Identidad inválida')
                    timestamp_key(task['created_at'])
                    enqueue = task['status']['Queued']['enqueued_at']
                    order = timestamp_key(enqueue)
                    dependencies = task['dependencies']
                    if type(dependencies) is not list or any(type(d) is not int or not 0 <= d < 2**64 for d in dependencies):
                        raise ValueError('Dependencia inválida')
                    if any(tasks.get(str(d),{}).get('status',{}).get('Done',{}).get('result') != 'Success' for d in dependencies):
                        block('dependency',group)
                        continue
                    candidates.append((order,ident,group,task['created_at'],enqueue,occupied,row['parallel_tasks']))
                except ADMISSION_ERRORS:
                    block('task_changed',group)
        return min(candidates) if candidates else None

    def _execution_confirmed(self, attempt, state):
        if self.generation(state) != attempt['generation']:
            return False
        snapshot = self.pueue.status()
        if snapshot['groups'][attempt['grupo']]['status'] != 'Paused':
            return False
        task = snapshot['tasks'].get(str(attempt['id']))
        if not task or any(task.get(k) != attempt[v] for k,v in (('id','id'),('group','grupo'),('created_at','created_at'))):
            return False
        status = task['status']
        if type(status) is not dict or len(status) != 1 or not set(status) <= {'Running','Done'}:
            return False
        detail = next(iter(status.values()))
        return (detail.get('enqueued_at') == attempt['enqueued_at'] and
                timestamp_key(attempt['at']) <= timestamp_key(detail['start']) <= timestamp_key(utc_at(self.clock())))

    def _boot_sample(self):
        boot, ns = self.boot_clock()
        if (type(boot) is not str or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',boot)
                or type(ns) is not int or ns < 0):
            raise ValueError('Reloj de admisión inválido')
        return boot, ns

    def _finish_attempt(self, value, outcome, minimum=None):
        attempt = value['last_attempt']
        boot, ns = self._boot_sample()
        if (boot != attempt['boot_id'] or ns < attempt['reserved_ns']
                or minimum is not None and (boot != minimum[0] or ns < minimum[1])):
            outcome = 'unknown'
        attempt['outcome'] = outcome
        attempt['completed_ns'] = ns if outcome in ('confirmed','not_sent') else None
        if outcome == 'confirmed':
            value['last_confirmed'] = dict(at=utc_at(self.clock()),grupo=attempt['grupo'],id=attempt['id'])
        self._save_admission(value)
        return outcome

    def resolve_admission(self, no_effect=False):
        """Caller holds control.lock; --sin-efecto certifies absence of any late effect.

        Maintenance, a new boot or a Queued observation alone never grant permission.
        Keep the durable identity/history and start a full minute at resolution.
        """
        if self.host != 'desk' or not os.path.lexists(self.directory/'maintenance'):
            raise ValueError('Resolver la admisión exige el desk en mantenimiento')
        with self.connect() as db:
            value = admission_state(db)
            state = self.state(db)
        if value is None or value['last_attempt'] is None or value['last_attempt']['outcome'] not in ('reserved','unknown'):
            raise ValueError('No hay admisión incierta que resolver')
        attempt = value['last_attempt']
        boot, ns = self._boot_sample()
        if boot == attempt['boot_id'] and ns < attempt['reserved_ns']:
            raise ValueError('Reloj anterior a la reserva')
        confirmed = self._execution_confirmed(attempt,state)
        if not confirmed and not no_effect:
            raise ValueError('Sin evidencia; certificar ausencia de efecto tardío o mantener retenida')
        outcome = 'confirmed' if confirmed else 'not_sent'
        log(self.directory,'admision_resolucion_intento',grupo=attempt['grupo'],id=attempt['id'],resultado=outcome)
        if boot != attempt['boot_id']:
            attempt.update(boot_id=boot,reserved_ns=ns)
        if self._finish_attempt(value,outcome,minimum=(boot,ns)) != outcome:
            raise ValueError('Reloj no fiable al finalizar resolución')
        log(self.directory,'admision_resuelta',grupo=attempt['grupo'],id=attempt['id'],resultado=outcome)
        return outcome

    def _one(self, state, proc, block, tick, result):
        cause,snapshot,sample,_ = self._admission_gate(state,proc=proc,operation='one')
        if cause:
            block(cause)
            return
        candidate = self._candidate(snapshot,state,block)
        if candidate is None:
            return
        with self.connect() as db:
            value = admission_state(db)
        if value is None:
            block('authority')
            return
        attempt = value['last_attempt']
        try:
            boot,ns = self._boot_sample()
        except (AttributeError,) + ADMISSION_ERRORS:
            block('clock')
            return
        if attempt and attempt['outcome'] in ('reserved','unknown'):
            # A crash cannot grant a new permit. Observe only the durable identity.
            if attempt['outcome'] == 'reserved':
                attempt['outcome'] = 'unknown'
                self._save_admission(value)
            try:
                if (boot != attempt['boot_id'] or ns >= attempt['reserved_ns']) and self._execution_confirmed(attempt,state):
                    # After reboot, rebase only with actual execution evidence.
                    attempt.update(boot_id=boot,reserved_ns=ns)
                    self._finish_attempt(value,'confirmed')
            except ADMISSION_ERRORS:
                pass
            block('admission_unknown')
            return
        if attempt:
            elapsed = ns-attempt['completed_ns'] if boot == attempt['boot_id'] else ns
            if elapsed < ADMISSION_GAP_NS:
                block('clock' if elapsed < 0 else 'cooldown')
                result['evento'] = 'tope_alcanzado'
                return
        _,ident,group,created,enqueue,occupied,quota = candidate
        attempt = dict(boot_id=boot,reserved_ns=ns,completed_ns=None,at=utc_at(self.clock()),
                       generation=state['generation'],grupo=group,id=ident,created_at=created,
                       enqueued_at=enqueue,outcome='reserved')
        value['last_attempt'] = attempt
        self._save_admission(value)  # FULL commit, no transaction held across RPC
        cause,final,sample,started = self._admission_gate(state,group,proc,operation='one')
        if not cause:
            try:
                final_candidate = self._candidate(final,state,lambda *args:None)
                # Never switch to a replacement candidate after reserving.
                if final_candidate != candidate:
                    cause = 'task_changed'
            except ADMISSION_ERRORS:
                cause = 'task_changed'
        if cause:
            self._finish_attempt(value,'not_sent');block(cause,group)
            return
        try:
            log(self.directory,'admision_intento',tick_id=tick,grupo=group,id=ident)
            if time.monotonic()-started > GATE_SECONDS:
                self._finish_attempt(value,'not_sent');block('fresh_sample',group)
                return
        except ADMISSION_ERRORS:
            self._finish_attempt(value,'not_sent');block('log',group)
            return
        # The schedule event is checked last, after the intent write, right before the effect.
        if self._event_stale(state):
            self._finish_attempt(value,'not_sent');block('event',group)
            return
        try:
            self.pueue.call('start',ident)
        except ADMISSION_ERRORS:
            pass  # RPC failure never proves no effect
        confirmed = False
        try:
            confirmed = self._execution_confirmed(attempt,state)
        except ADMISSION_ERRORS:
            pass
        outcome = self._finish_attempt(value,'confirmed' if confirmed else 'unknown')
        if outcome == 'confirmed':
            result.update(evento='admision_una',grupo=group,id=ident,resultado='confirmed',
                          presion=sample,cuota=quota,ocupados_antes=occupied)
        else:
            block('admission_unknown',group)

    def guard(self, readings, proc=Path('/proc')):
        if self.host == 'mini':
            return self._legacy_guard(readings,proc)
        tick = os.urandom(12).hex()
        blocks = []
        result = dict(evento='guard_fin')
        def block(cause, group=None, operation=None):
            item = dict(grupo=group,operacion=operation or ('unknown' if normal is None else 'one' if normal == 'mantener' else 'resume'),causa=cause)
            if item not in blocks and len(blocks) < 16:
                blocks.append(item)
        state, normal, can_open, failure = None, None, True, None
        stage = 'authority'
        try:
            try:
                if os.path.lexists(self.directory/'maintenance'):
                    block('maintenance');can_open=False;failure=ValueError('Mantenimiento')
                elif not self.reconcile():
                    block('admission');can_open=False;failure=ValueError('Admisión sin reconciliar')
            except ADMISSION_ERRORS as error:
                block('authority');can_open=False;failure=error
            try:
                with self.connect() as db:state=self.state(db)
            except ADMISSION_ERRORS as error:
                block('authority');can_open=False;failure=error
            if state and state.get('startup') and state['startup']['phase'] != 'ready':
                block('startup');can_open=False
            stage = 'fresh_sample'
            readings = readings if readings is not None else pressure(proc)
            normal = decide('desk',readings,PRESSURE_THRESHOLDS)
            rapid = decide('desk',readings,RAPIDAS_THRESHOLDS['desk'])
            stage = 'authority'
            for item in blocks:item['operacion']='one' if normal=='mantener' else 'resume'
            try:
                log(self.directory,'lectura',tick_id=tick,**readings)
                log(self.directory,'decision',tick_id=tick,accion=normal,accion_rapidas=rapid)
            except ADMISSION_ERRORS:
                block('log');can_open=False
            groups = self.pueue.status()['groups']
            for group in self.profile:
                if group not in groups:
                    continue
                reasons = set(state['reasons'].get(group,[])) if state else set()
                if os.path.lexists(self.pause_fence(group)):
                    reasons.add('manual')
                status = groups[group]['status']
                decision = rapid if group == 'rapidas' else normal
                if status == 'Running' and reasons & {'pressure','startup'}:
                    reasons -= {'pressure','startup'}
                    state.get('startup',{}).get('holds',{}).pop(group,None)
                    state['reasons'][group] = sorted(reasons)
                    try:
                        with self.connect(write=True) as db:self.store(db,state)
                    except ADMISSION_ERRORS as error:
                        can_open=False;failure=failure or error;block('authority')
                        try:self.fence_pause(group)
                        except ADMISSION_ERRORS:pass
                if decision == 'pausar':
                    if group != 'rapidas':block('pressure_high')
                    if status == 'Running' or 'pressure' in reasons:
                        try:
                            if can_open:
                                state['reasons'][group] = sorted(reasons | {'pressure'})
                                with self.connect(write=True) as db:self.store(db,state)
                            else:self.fence_pause(group)
                        except ADMISSION_ERRORS as error:
                            can_open=False;failure=failure or error;block('authority')
                            try:self.fence_pause(group)
                            except ADMISSION_ERRORS:pass
                        try:self.pueue.call('pause','--wait','-g',group)
                        except ADMISSION_ERRORS as error:
                            can_open=False;failure=failure or error;block('rpc',group)
                    continue
                if decision == 'reanudar' and reasons & {'pressure','startup'}:
                    if not can_open:continue
                    if not reasons <= {'pressure','startup'}:
                        block('reason',group,'resume');continue
                    if status != 'Paused':continue
                    cause,_,_,started = self._admission_gate(state,group,proc)
                    if cause:
                        block(cause,group,'resume')
                        if cause in ('event','generation','desired'):
                            can_open=False;failure=failure or ValueError('Gate de reapertura: '+cause)
                        continue
                    try:
                        log(self.directory,'apertura_intento',tick_id=tick,grupo=group)
                    except ADMISSION_ERRORS:
                        block('log',group,'resume');continue
                    if time.monotonic()-started > GATE_SECONDS:
                        block('fresh_sample',group,'resume');continue
                    if self._event_stale(state):
                        can_open=False;failure=failure or ValueError('Gate de reapertura: event')
                        block('event',group,'resume');continue
                    try:
                        self.pueue.call('start','-g',group)
                    except ADMISSION_ERRORS as error:
                        can_open=False;failure=failure or error;block('rpc',group,'resume');continue
                    state['reasons'][group] = sorted(reasons-{'pressure','startup'})
                    state.get('startup',{}).get('holds',{}).pop(group,None)
                    with self.connect(write=True) as db:self.store(db,state)
                elif can_open and status == 'Paused' and not reasons:
                    state['reasons'][group]=['manual']
                    with self.connect(write=True) as db:self.store(db,state)
            if normal == 'mantener' and can_open:
                self._one(state,proc,block,tick,result)
        except ADMISSION_ERRORS as error:
            failure=error
            block(stage)
        finally:
            event = result.pop('evento')
            if blocks and event == 'guard_fin':event='retenido'
            # Fixed whitelist, six managed groups, bounded to <4 KiB per summary.
            log(self.directory,event,tick_id=tick,bloqueos=blocks,**result)
        if failure is not None:
            raise failure

    def _legacy_guard(self, readings, proc=Path('/proc'), recovery_error=None):
        failure, state = recovery_error, None
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
        can_open = failure is None and not os.path.lexists(self.directory / 'maintenance')
        readings = readings if readings is not None else (pressure_mini() if self.host == 'mini' else pressure(proc))
        log(self.directory, 'lectura', **readings)
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
            if status == 'Running' and reasons & {'pressure', 'startup'}:
                # An external start ends our ownership, even in hysteresis.
                reasons.discard('pressure')
                reasons.discard('startup')
                state.get('startup', {}).get('holds', {}).pop(g, None)
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
            elif can_open and decision == 'reanudar' and reasons & {'pressure', 'startup'}:
                if reasons <= {'pressure', 'startup'} and status == 'Paused':
                    # A fresh sample, after reconciliation and immediately before admission.
                    current = pressure_mini() if self.host == 'mini' else pressure(proc)
                    if decide(self.host, current, RAPIDAS_THRESHOLDS[self.host] if g == 'rapidas' else thresholds) != 'reanudar':
                        continue
                    observed, _ = self.observed()
                    if observed != state['desired'] or self.generation(state) != state['generation']:
                        raise ValueError('Deseo o daemon cambiaron antes de admitir')
                    if self.resolve(state) != state:  # guard: after status and daemon identity
                        raise ValueError('Evento antes de abrir por presión')
                    if not self.admission_ready(state, group=g, proc=proc):
                        continue
                    self.pueue.call('start', '-g', g)
                    reasons.discard('startup')
                    state.get('startup', {}).get('holds', {}).pop(g, None)
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

    def _legacy_admission_ready(self, state, group=None, proc=Path('/proc')):
        if os.path.lexists(self.directory / 'maintenance'):
            return False
        if state.get('startup') and state['startup']['phase'] != 'ready':
            return False
        if self.host == 'mini' and state.get('startup_version') == 1:
            try:
                observed, _ = self.observed()
                if observed != state['desired'] or self.generation(state) != state['generation']:
                    return False
                current = pressure_mini()
                threshold = RAPIDAS_THRESHOLDS['mini'] if group == 'rapidas' else MINI_THRESHOLDS
                if decide('mini', current, threshold) != 'reanudar':
                    return False
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                return False
        if self.host == 'desk' and state.get('startup_version') == 1:
            try:
                subprocess.run(['/usr/bin/docker', 'info'], check=True, capture_output=True, timeout=5)
                observed, _ = self.observed()
                if observed != state['desired'] or self.generation(state) != state['generation']:
                    return False
                if group is not None:
                    current = pressure(proc)  # Docker/identity reads can take time too
                    threshold = RAPIDAS_THRESHOLDS['desk'] if group == 'rapidas' else PRESSURE_THRESHOLDS
                    if decide('desk', current, threshold) != 'reanudar':
                        return False
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                return False
        return self.resolve(state) == state

    def manual(self, group, resume=False):
        # Existing foreign groups may be resolved without joining the quota profile.
        if group not in self.pueue.status()['groups']:
            raise ValueError('Grupo inexistente; no se crea al reanudar')
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
            if not self.admission_ready(state, group=group):
                raise ValueError('Mantenimiento o Docker no preparado; admisión retenida')
            if state.get('startup', {}).get('phase') not in (None, 'ready'):
                raise ValueError('Arranque sin verificar')
            reasons = set(state['reasons'].get(group, [])) - {'manual', 'ambiguous', 'startup'}
            if not reasons:
                # Keep reasons until success: a failed start retains explicit ownership.
                observed, _ = self.observed()
                if observed != state['desired'] or self.generation(state) != state['generation']:
                    raise ValueError('Deseo o daemon cambiaron antes de admitir')
                if self.resolve(state) != state:  # manual: after status and daemon identity
                    raise ValueError('Evento antes de reanudar manualmente')
                self.pueue.call('start', '-g', group)
            state['reasons'][group] = sorted(reasons)
            state.get('startup', {}).get('holds', {}).pop(group, None)
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
    parser.add_argument("command", choices=["aplicar", "guardia", "pausar", "reanudar", "adoptar", "rack", "preparar", "retirar", "parada", "resolver-admision"])
    parser.add_argument("group", nargs="?")
    parser.add_argument("--host", choices=["desk", "mini"], default="desk")
    parser.add_argument('--pid-file', type=Path)
    parser.add_argument('--seco', action='store_true')
    parser.add_argument('--sin-efecto', action='store_true', help='Certifica que no existe efecto pendiente ni tardío de la admisión')
    args = parser.parse_args()
    if args.sin_efecto and args.command != 'resolver-admision':
        parser.error('--sin-efecto sólo para resolver-admision')
    if args.command == 'resolver-admision' and (args.host != 'desk' or args.group is not None):
        parser.error('resolver-admision sólo para desk, sin grupo')
    if args.seco and args.command != 'preparar':
        parser.error('--seco sólo para preparar; rack usa dry_run')
    groups = MINI_GROUPS if args.host == "mini" else GROUP_LIMITS
    foreign_resume = args.command == 'reanudar'
    if args.group is not None and args.group not in groups and not foreign_resume and not (args.command == 'retirar' and args.group in ('suites', 'pg5434')):
        parser.error("grupo desconocido para el host")
    if args.host == "mini" and args.command == "aplicar":
        parser.error("La Mini sólo admite guardia y pausa manual")
    pueue = Pueue("/opt/homebrew/bin/pueue") if args.host == "mini" else Pueue()
    if args.host == 'mini':
        pueue.context = dict(env=mini_environment(Path.home()), cwd=Path.home())
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
                        status = ('rejected' if request['op'] in ('slots', 'mode', 'undo', 'reorder') else
                                  'unknown' if request['op'] == 'status' else 'busy')
                        response = engine.reply(request, status, 'lock_busy', effects=False)
        except (OSError, ValueError, TypeError, sqlite3.Error):
            request = request if type(request) is dict else {}
            uncertain = request.get('op') == 'status' or request.get('op') in ('slots', 'mode', 'undo', 'reorder') and request.get('dry_run') is False
            response = engine.reply(request, 'unknown' if uncertain else 'recovery_error', 'recovery_error')
        print(json.dumps(response, allow_nan=False))
        return
    with locked(directory, create=args.command != 'preparar'):
        try:
            engine = MiniControl(pueue, directory, host=args.host)
            if args.command == 'resolver-admision':
                print(json.dumps(dict(resultado=engine.resolve_admission(no_effect=args.sin_efecto))))
            elif args.command == 'preparar':
                prepare_start(engine, dry_run=args.seco)
            elif args.command == 'parada':
                engine.record_stop()
            elif args.command == 'retirar':
                engine.retire_empty(args.group)
            elif args.command == 'adoptar':
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
            if args.command != 'preparar':
                detalle = type(error).__name__ if args.host == 'desk' else str(error)
                log(directory, "error", detalle=detalle)
            raise


if __name__ == "__main__":
    main()
