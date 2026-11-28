from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack, contextmanager
import csv
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import errno
import fcntl
from hashlib import sha256
from itertools import chain
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

from boat_api import Boat, BoatError


POOL_TYPES = ('default', 'large', 'xlarge')
PRIORITY = {'preparar': 0, 'tren': 1, 'suite': 2, 'otra': 3}
READY = {'ready', 'idle', 'running'}
STOPPED = {'archived', 'stopped'}
STARTING = {'init', 'provisioning', 'provisioned', 'cloning', 'resuming'}
STOPPING = {'archiving', 'stopping'}
VM_ID = re.compile(r'bx_[a-z0-9]{8}')
TOKEN = re.compile(r'[a-f0-9]{32}')
ISO_DATETIME = re.compile(
    r'([0-9]{4})(?P<date_sep>-?)([0-9]{2})(?P=date_sep)([0-9]{2})[T ]'
    r'([0-9]{2})(?:(?P<time_sep>:?)([0-9]{2})(?:(?P=time_sep)([0-9]{2}))?)?'
    r'(?:[.,]([0-9]+))?'
    r'(Z|[+-][0-9]{2}(?::?[0-9]{2})?)'
)
GATES = {
    'reserva_arranques': ('starts', 'day', 'remaining'),
    'horas_plan': ('planIncluded', 'vmHoursRemaining'),
    'monedero_usd': ('wallet', 'balanceDollars'),
}
POWER_TIMEOUT = 90
RESUME_TIMEOUT = 420
POWER_UNCONFIRMED = 'Boat no confirmó el cambio de estado'
SUITE_MAX = 7200
RESERVATION_TTL_MAX = 3600
AUTO_STOP_CLOCK_TOLERANCE = 60
RENEW_BEFORE = 48 * 3600
RENEW_FOR = 7 * 86400
STOPPED_NOTICE = 12 * 86400
LOCK_WAIT = 10
INCIERTO = 1.0
UNREADABLE_POWER_EVENT = object()


class Unavailable(RuntimeError):
    def __init__(self, cause, detail):
        super().__init__(detail)
        self.cause = cause


class JournalError(Unavailable):
    def __init__(self, name):
        super().__init__('apagado', 'diario ilegible en ' + name)


def print_unavailable(error):
    cause = error.cause if isinstance(error, Unavailable) else 'api'
    print(f'boat-pool: {cause}: {error}', file=sys.stderr)


def launch_wake(repo, state):
    command = Path(__file__).resolve().with_name('boat-pool')
    try:
        fd = os.open(state / 'launchd.err.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'ab') as log:
            subprocess.Popen([sys.executable, '-IB', str(command), 'despertar', '--repo', repo],
                             start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=log)
    except OSError:
        print_unavailable(Unavailable('apagado', 'no se pudo lanzar despertar'))


class InvalidUse(ValueError):
    pass


class WakeSkipped(Exception):
    pass


def stamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace('+00:00', 'Z')


def iso_datetime(value):
    match = ISO_DATETIME.fullmatch(value)
    if match is None:
        raise ValueError('fecha ISO con zona inválida')
    year, _, month, day, hour, _, minute, second, fraction, zone = match.groups()
    offset = timezone.utc
    if zone != 'Z':
        digits = zone[1:].replace(':', '')
        hours, minutes = int(digits[:2]), int(digits[2:] or '0')
        if hours > 23 or minutes > 59:
            raise ValueError('zona ISO inválida')
        sign = -1 if zone[0] == '-' else 1
        offset = timezone(sign * timedelta(hours=hours, minutes=minutes))
    micros = int((fraction or '')[:6].ljust(6, '0'))
    return datetime(int(year), int(month), int(day), int(hour), int(minute or '0'),
                    int(second or '0'), micros, offset)


def epoch(value):
    return iso_datetime(value).timestamp() if isinstance(value, str) else number(value)


def resume_deadline(operation):
    if operation and operation['accion'] == 'resume':
        return epoch(operation.get('inicio', operation['desde'])) + RESUME_TIMEOUT
    return None


def resume_replaced(followed_until, until):
    # El inicio de la marca nueva cae fuera del intento que seguía este pedido.
    return followed_until is not None and until is not None and until - RESUME_TIMEOUT >= followed_until


def creation_midnight(created):
    midnight = round(created / 86400) * 86400
    if abs(created - midnight) < INCIERTO:
        return midnight
    return None


def creation_start(created):
    midnight = creation_midnight(created)
    return created if midnight is None else midnight - INCIERTO


def number(value, minimum=None):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('número inválido')
    if minimum is not None and value < minimum:
        raise ValueError('número fuera de rango')
    return value


def integer(value, minimum):
    if type(value) is not int:
        raise ValueError('entero inválido')
    return number(value, minimum)


def label(value):
    if not isinstance(value, str) or not value.strip() or not value.isprintable():
        raise ValueError('texto inválido')
    return value


def reserved_name(name):
    return name.startswith('ci-') or name == 'hasito-sen' or VM_ID.fullmatch(name)


def safe_name(name):
    if reserved_name(name):
        raise Unavailable('sin_vm', 'nombre de VM reservado')
    if name in ('.', '..') or '/' in name or not name.isprintable() or not name.strip():
        raise ValueError('nombre de VM inválido')


def check_id(identifier):
    if not isinstance(identifier, str) or not VM_ID.fullmatch(identifier):
        raise ValueError('ID de VM inválido')


def read_prohibited(entries):
    if not isinstance(entries, list):
        raise ValueError('prohibidos debe ser una lista')
    prohibited = {}
    for entry in entries:
        identifier, until = (entry, math.inf) if isinstance(entry, str) else (entry['id'], epoch(entry['hasta']))
        check_id(identifier)
        prohibited[identifier] = max(until, prohibited.get(identifier, -math.inf))
    return prohibited


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def private_dir(path):
    if not path.is_dir():
        private_dir(path.parent)
        path.mkdir(mode=0o700, exist_ok=True)
        sync_dir(path.parent)
    return path


def read_json(path):
    with path.open(encoding='utf-8') as stream:
        return json.load(stream)


def atomic_json(path, value):
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as temp:
        pending = Path(temp.name)
        try:
            json.dump(value, temp, ensure_ascii=False, allow_nan=False)
            temp.write('\n')
            temp.flush()
            os.fsync(temp.fileno())
            os.replace(pending, path)
            sync_dir(path.parent)
        finally:
            pending.unlink(missing_ok=True)


def remove(path):
    if path.exists():
        path.unlink()
        sync_dir(path.parent)


@contextmanager
def lock(path, *, wait=True):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        flags = fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(fd, flags)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(fd)


def append_line(path, line):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'r+b') as stream:
        if stream.seek(0, os.SEEK_END):
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) != b'\n':
                stream.seek(0)
                stream.truncate(stream.read().rfind(b'\n') + 1)
                stream.seek(0, os.SEEK_END)
        stream.write(line.encode() + b'\n')
        stream.flush()
        os.fsync(stream.fileno())
    sync_dir(path.parent)


def append_event(path, event):
    append_line(path, json.dumps(event, ensure_ascii=False, allow_nan=False))


def events(path):
    try:
        with path.open('rb') as stream:
            return [json.loads(line) for line in stream if line.endswith(b'\n')]
    except FileNotFoundError:
        return []


def events_from_end(stream, end):
    fragments = []
    found_newline = False
    while end:
        size = min(end, 8192)
        end -= size
        stream.seek(end)
        first, *lines = stream.read(size).split(b'\n')
        if not lines:
            if found_newline:
                fragments.append(first)
            continue
        # Como en events(), el texto después del último salto está incompleto.
        if found_newline:
            yield json.loads(b''.join(chain((lines[-1],), reversed(fragments))))
        found_newline = True
        for line in reversed(lines[:-1]):
            yield json.loads(line)
        fragments = [first]
    if found_newline:
        yield json.loads(b''.join(reversed(fragments)))


@dataclass
class VM:
    name: str
    repo: str
    tipo: str
    places: int
    classes: dict
    weights: dict
    idle: float | None
    path: Path
    prohibited: dict
    id: str = ''
    prepared: float = 0
    apt: bool = False
    recorded_ids: set = field(default_factory=set)
    _power_event: tuple | None = field(default=None, init=False, repr=False, compare=False)

    @property
    def journal(self):
        return self.path / 'eventos.jsonl'

    @property
    def auto_stop_ttl(self):
        return None if self.idle is None else SUITE_MAX + int(self.idle)

    def read_registration(self):
        record = read_json(self.path / 'registro.json')
        identifier = record['id']
        check_id(identifier)
        if (record['nombre'], record['repo'], record['tipo']) != (self.name, self.repo, self.tipo):
            raise ValueError('registro no coincide con el pool')
        if type(record['apta']) is not bool:
            raise ValueError('aptitud inválida')
        number(record['cpu_robada_pct'], 0)
        return identifier, epoch(record['preparada_en']), record['apta']

    def registration(self, now):
        current = self.read_registration()
        if now < self.prohibited.get(current[0], -math.inf):
            raise Unavailable('sin_vm', 'ID de VM reservado')
        return current

    def validate(self, now):
        try:
            current = self.registration(now)
        except (FileNotFoundError, KeyError, TypeError, ValueError, OverflowError):
            raise Unavailable('sin_vm', 'VM sin registro válido') from None
        if current != (self.id, self.prepared, self.apt):
            raise Unavailable('sin_vm', 'registro de VM cambió')

    def read_power_operation(self):
        try:
            operation = read_json(self.path / 'operacion.json')
        except FileNotFoundError:
            return None
        sandbox_id = operation['sandbox_id']
        check_id(sandbox_id)
        if sandbox_id == self.id:
            label(operation['id'])
            if operation['accion'] not in ('resume', 'stop'):
                raise ValueError('acción inválida')
            epoch(operation['desde'])
            if operation['accion'] == 'resume' and 'inicio' in operation:
                epoch(operation['inicio'])
        return operation

    def last_power_event(self):
        try:
            stream = self.journal.open('rb')
        except FileNotFoundError:
            return None
        except OSError:
            raise JournalError(self.name) from None
        with stream:
            stat = os.fstat(stream.fileno())
            version = (self.id, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            if self._power_event is None or self._power_event[0] != version:
                try:
                    kind = next((item['evento'] for item in events_from_end(stream, stat.st_size)
                                 if item['evento'] in ('arranque', 'encendida', 'apagada')
                                 and item.get('id') == self.id), None)
                except (OSError, KeyError, TypeError, ValueError, OverflowError):
                    kind = UNREADABLE_POWER_EVENT
                self._power_event = (version, kind)
            _, kind = self._power_event
            if kind is UNREADABLE_POWER_EVENT:
                raise JournalError(self.name)
            return kind

    def read_events(self):
        try:
            return events(self.journal)
        except (OSError, ValueError):
            raise JournalError(self.name) from None

    def event(self, kind, when, **fields):
        identity = {'id': self.id} if self.id else {}
        return {'evento': kind, 'en': stamp(when), 'vm': self.name, 'tipo': self.tipo, **identity, **fields}

    def lease_path(self, token):
        return private_dir(self.path / 'arriendos') / (token + '.json')

    def pending_lease_change(self):
        try:
            return read_json(self.path / 'arriendo-pendiente.json')
        except FileNotFoundError:
            return None

    def finish_lease_change(self):
        change = self.pending_lease_change()
        if change is None:
            return
        event = change['evento']
        if not any(item.get('cambio') == event['cambio'] for item in self.read_events()):
            append_event(self.journal, event)
        path = self.lease_path(event['arriendo'])
        if change['arriendo'] is None:
            remove(path)
        else:
            atomic_json(path, change['arriendo'])
        remove(self.path / 'arriendo-pendiente.json')

    def change_lease(self, kind, when, lease):
        event = self.event(kind, when, arriendo=lease['arriendo'], repo=lease['repo'],
                           corrida=lease['corrida'], cambio=uuid4().hex)
        atomic_json(self.path / 'arriendo-pendiente.json', {
            'evento': event,
            'arriendo': lease if kind == 'concedido' else None,
        })
        self.finish_lease_change()

    def lease_files(self):
        for path in (self.path / 'arriendos').glob('*.json'):
            try:
                yield path.stem, read_json(path)
            except FileNotFoundError:
                continue

    def lease_snapshot(self, now):
        # espera.lock serializa los cambios vivos. La VM puede estar recuperando un cambio pendiente.
        change = self.pending_lease_change()
        leases = dict(self.lease_files())
        if change is not None:
            token = change['evento']['arriendo']
            if change['arriendo'] is None:
                leases.pop(token, None)
            else:
                leases[token] = change['arriendo']
        return [lease for lease in leases.values() if epoch(lease['vence']) > now]

    def leases(self, now, *, expire=False):
        self.finish_lease_change()
        live = []
        for _, lease in self.lease_files():
            expiry = epoch(lease['vence'])
            if expiry > now:
                live.append(lease)
            elif expire:
                self.change_lease('vencido', expiry, lease)
        return live

    def used_places(self, leases):
        return sum(self.weights.get(lease['clase'], 1) for lease in leases)

    def fits(self, leases, kind):
        if kind == 'preparar':
            return not leases
        return (self.used_places(leases) + self.weights.get(kind, 1) <= self.places
                and not any(lease['clase'] == 'preparar' for lease in leases)
                and sum(lease['clase'] == kind for lease in leases) < self.classes.get(kind, self.places))

    def accepts(self, turn):
        if turn['repo'] != self.repo or self.name in turn.get('descartadas', ()):
            return False
        if turn['clase'] == 'preparar':
            return turn['vm'] == self.name
        if 'candidatas' in turn and self.name not in turn['candidatas']:
            return False
        return self.apt and self.fits([], turn['clase'])

    def covered_by_batch(self, reservations):
        return self.apt and any(item['repo'] == self.repo and 'vence_solicitado' not in item
                                and any(self.classes.get(kind, 0) > 0 for kind in item['clases'])
                                for item in reservations)


def fits_reservations(vm, kind, pending, capacity):
    if not pending:
        return True
    loads = [(candidate, leases + ([{'clase': kind}] if candidate is vm else []))
             for candidate, leases in capacity]
    if len(pending) > sum(max(0, vm.places - vm.used_places(leases)) for vm, leases in loads):
        return False

    def place(index):
        if index == len(pending):
            return True
        kind = pending[index]
        for vm, leases in loads:
            if vm.fits(leases, kind):
                leases.append({'clase': kind})
                matched = place(index + 1)
                leases.pop()
                if matched:
                    return True
        return False

    return place(0)


@dataclass
class UsagePeriod:
    identifier: str
    prepared: float
    retired: float | None = None
    last_event_without_retirement: float | None = None
    created: float | None = None
    creation_error: str | None = None
    retirement_invalid: bool = False


def unix_notify(message):
    try:
        subprocess.run(
            ['/usr/bin/osascript', '-e', 'on run argv',
             '-e', 'display notification (item 1 of argv) with title "Boat pool"',
             '-e', 'end run', message],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def pid_alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as error:
        return error.errno == errno.EPERM
    return True


class Pool:
    def __init__(self, config, state, *, boat_factory=Boat, notify=unix_notify,
                 now=time.time, sleep=time.sleep, wake_launcher=launch_wake):
        if integer(config['version'], 1) != 1:
            raise ValueError('versión del pool inválida')
        self.now = now
        self.sleep = sleep
        self.notify = notify
        self.wake_launcher = wake_launcher
        self.host = socket.gethostname()
        self.prohibited = read_prohibited(config['prohibidos'])
        self.state = private_dir(state)
        self.boat = boat_factory(label(config['org']), label(config['llavero']))
        default_idle = number(config['inactividad_min'], 0)
        self.thresholds = {
            'reserva_arranques': integer(config['reserva_arranques'], 0),
            'horas_plan': number(config['piso']['horas_plan'], 0),
            'monedero_usd': number(config['piso']['monedero_usd'], 0),
        }
        self.vms = {}
        for data in config['vms']:
            name = label(data['nombre'])
            safe_name(name)
            if name in self.vms or data['tipo'] not in POOL_TYPES:
                raise ValueError('VM duplicada o tipo inválido')
            places = integer(data['lugares'], 1)
            classes = {label(kind): integer(limit, 0) for kind, limit in data.get('limite_clase', {}).items()}
            weights = data.get('peso_clase', {})
            if not isinstance(weights, dict):
                raise ValueError('peso_clase debe ser un objeto')
            weights = {label(kind): integer(weight, 1) for kind, weight in weights.items()}
            if any(weight > places for weight in weights.values()):
                raise ValueError('peso_clase supera los lugares de la VM')
            idle = data.get('inactividad_min', default_idle)
            idle = None if idle is None else number(idle, 0) * 60
            self.vms[name] = VM(name, label(data['repo']), data['tipo'], places,
                                classes, weights, idle, state / 'vms' / name, self.prohibited)

    def candidates(self, *, name=None, repo=None):
        if name is not None:
            safe_name(name)
            if name not in self.vms:
                raise Unavailable('sin_vm', 'VM fuera del pool')
        selected = []
        ids = Counter()
        for vm in self.vms.values():
            wanted = (name is None or name == vm.name) and (repo is None or repo == vm.repo)
            try:
                vm.id, vm.prepared, vm.apt = vm.registration(self.now())
            except Unavailable as error:
                if name == vm.name:
                    raise
                if wanted:
                    self.reject(str(error), nombre=vm.name)
                continue
            except (FileNotFoundError, KeyError, TypeError, ValueError, OverflowError):
                continue
            ids[vm.id] += 1
            if wanted:
                selected.append(vm)
        return [vm for vm in selected if ids[vm.id] == 1]

    def reject(self, reason, vm=None, **fields):
        if vm is not None:
            append_event(vm.journal, vm.event('rechazo', self.now(), motivo=reason, **fields))
            return
        with lock(self.state / 'eventos.lock'):
            append_event(self.state / 'eventos.jsonl',
                         {'evento': 'rechazo', 'en': stamp(self.now()), 'motivo': reason, **fields})

    def gates(self):
        values = self.boat.limits()
        gates = {}
        for name, keys in GATES.items():
            current = values
            try:
                for key in keys:
                    current = current[key]
                current = number(current)
            except (KeyError, TypeError, ValueError):
                current = None
            threshold = self.thresholds[name]
            gates[name] = {'valor': current, 'minimo': threshold,
                           'abierta': current is not None and current >= threshold}
        return gates

    def check_gates(self, vm):
        closed = [name for name, gate in self.gates().items() if not gate['abierta']]
        for gate in closed:
            self.reject('puerta cerrada', vm, puerta=gate)
        if closed:
            raise Unavailable('puerta', 'puerta cerrada: ' + ', '.join(closed))

    def check_query_id(self, name, identifier, *, now=None):
        safe_name(name)
        check_id(identifier)
        if now is None:
            now = self.now()
        if now < self.prohibited.get(identifier, -math.inf):
            raise BoatError('ID de VM reservado')

    def fetch_creation(self, name, identifier, prepared, *, now=None):
        self.check_query_id(name, identifier, now=now)
        info = self.boat.sandbox(identifier)
        if not isinstance(info, dict) or (info.get('id'), info.get('name')) != (identifier, name):
            raise BoatError('VM de Boat no coincide con el registro')
        try:
            created_at = label(info.get('createdAt'))
            created = epoch(created_at)
            if created > prepared:
                raise ValueError('creación posterior a la preparación')
        except (TypeError, ValueError, OverflowError):
            raise BoatError('Boat no confirmó createdAt anterior o igual a preparada_en') from None
        return created_at, created

    def validate(self, vm):
        vm.validate(self.now())
        if vm.id in vm.recorded_ids:
            return
        if not any(item['evento'] == 'registro' and item.get('id') == vm.id
                   for item in vm.read_events()):
            event = vm.event('registro', vm.prepared)
            try:
                event['creada_en'], _ = self.fetch_creation(vm.name, vm.id, vm.prepared)
            except BoatError:
                pass
            append_event(vm.journal, event)
        vm.recorded_ids.add(vm.id)

    def check_identity(self, vm):
        self.validate(vm)
        matching = [item for item in self.boat.find(vm.name)
                    if isinstance(item, dict) and item.get('name') == vm.name]
        if len(matching) != 1 or matching[0].get('id') != vm.id:
            raise Unavailable('api', 'VM de Boat no coincide con el registro')

    def sandbox_info(self, vm):
        self.validate(vm)
        info = self.boat.sandbox(vm.id)
        if not isinstance(info, dict) or (info.get('id'), info.get('name')) != (vm.id, vm.name):
            raise Unavailable('api', 'VM de Boat no coincide con el registro')
        return info

    def observe(self, vm, info=None):
        if info is None:
            info = self.sandbox_info(vm)
        state = info.get('state')
        states = READY | STOPPED | STARTING | STOPPING | {'error', 'cancelled'}
        if not isinstance(state, str) or state not in states:
            raise BoatError('estado de VM desconocido')
        if state == 'error':
            self.warn_vm(vm, 'error', 'Boat informa un error en ' + vm.name)
        else:
            remove(vm.path / 'aviso-error.json')
        pending = self.power_operation(vm)
        if pending and pending['accion'] == 'resume' and state in READY | STARTING:
            self.record_power_event(vm, pending, 'arranque', epoch(pending['desde']))
        if pending and pending['accion'] == 'stop' and state in STOPPED:
            self.record_power_event(vm, pending, 'apagada', self.now())
            remove(vm.path / 'operacion.json')
        if state in READY and self.stop_pending(pending):
            return 'archiving'
        stop = info.get('stop')
        if state in READY and isinstance(stop, dict) and stop.get('status') in ('pending', 'failing'):
            return 'archiving'
        if state in READY and vm.last_power_event() == 'apagada':
            append_event(vm.journal, vm.event('encendida', self.now()))
        return state

    def stop_pending(self, operation):
        return bool(operation and operation['accion'] == 'stop' and (
            operation.get('credenciales') or self.now() - epoch(operation['desde']) < POWER_TIMEOUT
        ))

    def power_operation(self, vm):
        path = vm.path / 'operacion.json'
        marker = vm.path / 'aviso-operacion.json'
        try:
            operation = vm.read_power_operation()
        except (OSError, KeyError, TypeError, ValueError, OverflowError):
            message = 'Operación pendiente sin identificar en ' + vm.name
            self.warn_vm(vm, 'operacion', message)
            raise BoatError(message) from None
        remove(marker)
        if operation is None or operation['sandbox_id'] == vm.id:
            return operation
        append_event(vm.journal, vm.event('operacion_descartada', self.now(), operacion=operation.get('id')))
        remove(path)
        return None

    def record_power_event(self, vm, operation, kind, when):
        if not any(item['evento'] == kind and item.get('operacion') == operation['id']
                   for item in vm.read_events()):
            append_event(vm.journal, vm.event(kind, when, operacion=operation['id']))

    def send_power(self, vm, action, *, restart=False, before_resume=None):
        self.validate(vm)
        previous = self.power_operation(vm)
        now = self.now()
        operation = {'id': uuid4().hex, 'sandbox_id': vm.id, 'accion': action, 'desde': stamp(now)}
        if action == 'resume':
            until = resume_deadline(previous)
            operation['inicio'] = operation['desde']
            if until is not None:
                if not restart and now >= until:
                    raise BoatError(POWER_UNCONFIRMED)
                if now - epoch(previous['desde']) < POWER_TIMEOUT:
                    raise BoatError('reanudación anterior sin confirmar')
                if not restart:
                    operation['inicio'] = previous.get('inicio', previous['desde'])
            if before_resume is not None:
                before_resume(resume_deadline(operation))
        if action == 'stop' and previous and (previous['accion'] == 'resume' or previous.get('credenciales')):
            operation['credenciales'] = True
        atomic_json(vm.path / 'operacion.json', operation)
        remove(vm.path / 'auto-stop.json')
        self.boat.power(vm, action, ttl=vm.auto_stop_ttl)
        if action == 'resume':
            self.record_power_event(vm, operation, 'arranque', epoch(operation['desde']))
        return operation

    def wait_power(self, vm, target, pending, *, timeout=POWER_TIMEOUT):
        deadline = time.monotonic() + timeout
        while True:
            info = self.sandbox_info(vm)
            state = self.observe(vm, info)
            if state in target:
                return info
            if state not in pending or time.monotonic() >= deadline:
                raise BoatError(POWER_UNCONFIRMED)
            self.sleep(1)

    def check_start_credentials(self, vm, state, info, pending, *, wait):
        if pending and pending.get('credenciales'):
            self.stop_unverified(vm, self.sandbox_info(vm)['state'], wait=wait)
            raise BoatError('Boat no acreditó la ausencia de credenciales al reanudar')
        if state in READY and resume_deadline(pending) is not None:
            self.check_resumed(vm, info, pending, wait=wait)

    def resume_step(self, vm, state, pending, *, send, requested_at, deadline, before_resume=None):
        until = resume_deadline(pending)
        can_restart = False
        if until is not None:
            now = self.now()
            retry_at = epoch(pending['desde']) + POWER_TIMEOUT
            if state not in STARTING | STOPPED:
                raise BoatError(POWER_UNCONFIRMED)
            if now >= until:
                can_restart = send and state in STOPPED and requested_at > until and now >= retry_at
                if until < requested_at < retry_at:
                    remaining = deadline - time.monotonic()
                    if not can_restart and remaining > 0:
                        # El último POST aún puede confirmarse dentro de la espera del pedido.
                        return None
                    can_restart = can_restart and remaining >= 0
                if not can_restart:
                    raise BoatError(POWER_UNCONFIRMED)
            elif state in STARTING or not send or now < retry_at:
                return until
        if state in STOPPED and send:
            with lock(self.state / 'arranques.lock'):
                self.check_gates(vm)
                operation = self.send_power(vm, 'resume', restart=can_restart, before_resume=before_resume)
            return resume_deadline(operation)
        if state not in STARTING | STOPPED:
            raise BoatError('estado de VM no reanudable')
        return None

    def start(self, vm, state, info, *, before_resume=None):
        pending = self.power_operation(vm)
        self.check_start_credentials(vm, state, info, pending, wait=True)
        if state in READY:
            return info
        if state in STOPPING and resume_deadline(pending) is None:
            self.wait_power(vm, STOPPED, STOPPING)
            state = 'archived'
        requested_at = self.now()
        deadline = time.monotonic() + RESUME_TIMEOUT
        while state not in READY:
            until = self.resume_step(vm, state, pending, send=True, requested_at=requested_at,
                                     deadline=deadline, before_resume=before_resume)
            if until is not None or resume_deadline(pending) is None:
                timeout = RESUME_TIMEOUT if until is None else until - self.now()
                info = self.wait_power(vm, READY, STARTING | STOPPED, timeout=timeout)
                break
            self.sleep(min(1, max(0, deadline - time.monotonic())))
            info = self.sandbox_info(vm)
            state = self.observe(vm, info)
            pending = self.power_operation(vm)
        pending = self.power_operation(vm)
        self.check_start_credentials(vm, 'ready', info, pending, wait=True)
        return info

    def check_resumed(self, vm, info, pending, *, wait):
        if info.get('state') in READY and info.get('holdsCreatorLogins') is False:
            remove(vm.path / 'operacion.json')
            remove(vm.path / 'aviso-credenciales.json')
            return
        atomic_json(vm.path / 'operacion.json', {**pending, 'accion': 'stop', 'credenciales': True})
        self.stop_unverified(vm, info['state'], wait=wait)
        raise BoatError('Boat no acreditó la ausencia de credenciales al reanudar')

    def stop_unverified(self, vm, state, *, wait):
        self.warn_vm(vm, 'credenciales', 'Boat no acreditó la ausencia de credenciales en ' + vm.name)
        self.stop(vm, state, wait=wait)

    def stop(self, vm, state, *, wait):
        if state in STOPPED:
            return
        if state in READY:
            self.send_power(vm, 'stop')
        elif state not in STOPPING:
            raise BoatError('VM aún no se puede detener')
        if wait:
            self.wait_power(vm, STOPPED, READY | STOPPING)
        else:
            self.observe(vm)

    def flush_reservation_events(self, data, *, recover=False):
        journal = self.state / 'eventos.jsonl'
        with lock(self.state / 'eventos.lock'):
            recorded = {event.get('cambio') for event in events(journal)} if recover else set()
            for event in data['eventos_pendientes']:
                if event['cambio'] not in recorded:
                    append_event(journal, event)
        atomic_json(self.state / 'reservas.json', {'reservas': data['reservas']})

    def write_reservations(self, reservations, changes):
        data = {'reservas': reservations, 'eventos_pendientes': [
            {'evento': kind, 'en': when, 'cambio': uuid4().hex, **self.reservation_data(reservation)}
            for kind, when, reservation in changes
        ]}
        atomic_json(self.state / 'reservas.json', data)
        self.flush_reservation_events(data)

    @staticmethod
    def reservation_data(reservation):
        return {key: reservation[key] for key in ('repo', 'lote', 'clases', 'vence')}

    def reservations(self, now, *, include_expired=False):
        # El lector y las escrituras de reservas comparten espera.lock.
        try:
            data = read_json(self.state / 'reservas.json')
        except FileNotFoundError:
            return []
        if data.get('eventos_pendientes'):
            self.flush_reservation_events(data, recover=True)
        kept, expired = [], []
        for item in data['reservas']:
            end = epoch(item['vence'])
            if end > now and not item.get('vencida'):
                kept.append(item)
                continue
            if not item.get('vencida'):
                expired.append(('reserva-vencida', item['vence'], item))
            # Un reintento tardío de --vence puede traer un EPOCH posterior.
            if 'vence_solicitado' in item and end + RESERVATION_TTL_MAX > now:
                kept.append({**item, 'vencida': True})
        if expired or len(kept) != len(data['reservas']):
            self.write_reservations(kept, expired)
        return kept if include_expired else [item for item in kept if not item.get('vencida')]

    def reservation_command(self, repo, batch, classes=None, ttl=None, *, until=None):
        with lock(self.state / 'espera.lock'):
            now = self.now()
            reserve = classes is not None and (until is None or until > now)
            if reserve and not any(vm.repo == repo for vm in self.vms.values()):
                raise InvalidUse('repositorio sin VMs en el pool')
            reservations = self.reservations(now, include_expired=True)
            previous = next((item for item in reservations if (item['repo'], item['lote']) == (repo, batch)), None)
            if previous is not None and previous.get('vencida') and (not reserve or until is not None):
                return
            if previous is not None:
                reservations.remove(previous)
            if reserve:
                end = now + ttl if until is None else min(until, now + RESERVATION_TTL_MAX)
                if until is not None and previous is not None and 'vence_solicitado' in previous:
                    end = min(end, epoch(previous['vence']))
                    until = min(until, previous['vence_solicitado'])
                reservation = {'repo': repo, 'lote': batch, 'clases': classes, 'vence': stamp(end)}
                if until is not None:
                    reservation['vence_solicitado'] = until
                reservations.append(reservation)
                self.write_reservations(reservations, [('reservado', stamp(now), reservation)])
            elif previous is not None:
                if 'vence_solicitado' in previous:
                    # Un pool anterior que siga en espera decide sólo por `vence`: la marca ya venció para él.
                    reservations.append({**previous, 'vence': stamp(now), 'vencida': True})
                self.write_reservations(reservations, [('reserva-liberada', stamp(now), previous)])
        if reserve and until is None and previous is None:
            self.wake_launcher(repo, self.state)

    def reservation_loads(self, repo, now):
        pending = Counter(kind for item in self.reservations(now) if item['repo'] == repo
                          for kind in item['clases'])
        if not pending:
            return [], []
        capacity = []
        for vm in self.candidates(repo=repo):
            leases = vm.lease_snapshot(now)
            if vm.apt:
                capacity.append((vm, leases))
            for lease in leases:
                if lease.get('prioridad') == 'tren' and lease.get('repo') == repo:
                    pending[lease['clase']] -= 1
        return list(pending.elements()), capacity

    def turns(self):
        waiting = []
        now = self.now()
        for path in private_dir(self.state / 'espera').glob('*.json'):
            turn = read_json(path)
            if ('vence' not in turn
                    or max(epoch(turn['vence']), epoch(turn.get('comprobando_hasta', 0))) <= now
                    or turn['host'] != self.host or not pid_alive(turn['pid'])):
                remove(path)
            else:
                waiting.append(turn)
        return sorted(waiting, key=lambda item: (PRIORITY[item['prioridad']], item['orden'], item['turno']))

    def ask_order(self, candidates, kind):
        if len(candidates) < 2:
            return candidates
        now = self.now()
        order = {}
        for vm in candidates:
            try:
                with lock(vm.path / 'lock', wait=False) as acquired:
                    if not acquired:
                        continue
                    leases = vm.leases(now)
                    fits = vm.fits(leases, kind)
                    operation = vm.read_power_operation()
                    if operation and operation['sandbox_id'] != vm.id:
                        operation = None
                    resuming = resume_deadline(operation) is not None
                    stopped = vm.last_power_event() == 'apagada'
                    stopping = self.stop_pending(operation)
                    order[vm.name] = (not fits, (stopped or stopping) and not resuming, resuming,
                                      vm.used_places(leases) / vm.places)
            except (JournalError, OSError, KeyError, TypeError, ValueError, OverflowError):
                continue
        return sorted(candidates, key=lambda vm: order.get(vm.name, (True, True, True, math.inf)))

    def registered_resume_deadline(self, vm):
        try:
            operation = vm.read_power_operation()
            if operation and operation['sandbox_id'] == vm.id:
                return resume_deadline(operation)
        except (OSError, KeyError, TypeError, ValueError, OverflowError):
            pass  # La validación con el candado registra y avisa de las marcas ilegibles.
        return None

    def claim(self, candidates, turn, path, followed, reclaim_by, locks, blocked):
        contended = False
        eligible = False
        locked_until = -math.inf
        priority = PRIORITY[turn['prioridad']]
        with lock(self.state / 'espera.lock'):
            queue = self.turns()
            for vm in candidates:
                if not locks.enter_context(lock(vm.path / 'lock', wait=False)):
                    locks.close()
                    contended = True
                    until = self.registered_resume_deadline(vm)
                    if until is not None:
                        followed_until = followed.get(vm.id)
                        if resume_replaced(followed_until, until):
                            until = followed_until
                        elif until > self.now():
                            followed[vm.id] = until
                        locked_until = max(locked_until, until + LOCK_WAIT)
                    elif vm.id in followed:
                        until = reclaim_by.setdefault(vm.id, self.now() + LOCK_WAIT)
                        locked_until = max(locked_until, until)
                    continue
                try:
                    identifier, prepared, apt = vm.registration(self.now())
                except Unavailable as error:
                    self.reject(str(error), vm)
                    locks.close()
                    continue
                except (FileNotFoundError, KeyError, TypeError, ValueError, OverflowError):
                    locks.close()
                    continue
                if identifier != vm.id:
                    vm = next((item for item in self.candidates() if item.name == vm.name), None)
                    if vm is None:
                        locks.close()
                        continue
                else:
                    vm.prepared, vm.apt = prepared, apt
                if not vm.accepts(turn):
                    locks.close()
                    continue
                try:
                    leases = vm.leases(self.now())
                except JournalError:
                    eligible = True
                    locks.close()
                    continue
                accepting = [item for item in queue
                             if PRIORITY[item['prioridad']] <= priority and vm.accepts(item)
                             and (item['clase'] == 'preparar' or vm.fits(leases, item['clase']))]
                fits = vm.fits(leases, turn['clase'])
                other_classes = {item['clase'] for item in accepting if item['prioridad'] != 'tren'}
                if other_classes:
                    pending, capacity = self.reservation_loads(turn['repo'], self.now())
                    refused = {kind for kind in other_classes if not fits_reservations(vm, kind, pending, capacity)}
                    if turn['prioridad'] != 'tren' and fits and turn['clase'] in refused:
                        blocked.add(vm.name)
                    accepting = [item for item in accepting if item['prioridad'] == 'tren'
                                 or item['clase'] not in refused]
                first = accepting[0] if accepting else None
                # Un turno que sigue otra VM conserva su lugar para conceder cuando esté lista.
                starter = next((item for item in accepting if item.get('reanudando') in (None, vm.name)), None)
                can_grant = fits and first is not None and first['turno'] == turn['turno']
                can_resume = fits and starter is not None and starter['turno'] == turn['turno']
                if (can_grant or can_resume or vm.id in followed
                        or self.registered_resume_deadline(vm) is not None):
                    if can_resume and not turn.get('reanudando'):
                        # El turno sigue vivo mientras se comprueba si hace falta el primer POST.
                        turn['comprobando_hasta'] = stamp(self.now() + RESUME_TIMEOUT)
                        atomic_json(path, turn)
                    return vm, leases, can_grant, can_resume, contended, eligible, locked_until
                eligible = True
                locks.close()
        return None, [], False, False, contended, eligible, locked_until

    def ask_resume(self, vm, state, pending, waits, *, send, requested_at, deadline, before_resume):
        if resume_deadline(pending) is None and state in STARTING | STOPPING:
            # Boat también puede iniciar una transición sin una marca local.
            timeout = RESUME_TIMEOUT if state in STARTING else POWER_TIMEOUT
            until = waits.setdefault((vm.id, timeout), self.now() + timeout)
            if self.now() >= until:
                raise BoatError(POWER_UNCONFIRMED)
            return until
        return self.resume_step(vm, state, pending, send=send, requested_at=requested_at, deadline=deadline,
                                before_resume=before_resume)

    def extend_turn(self, turn, path, expiry, resuming):
        current = epoch(turn['vence'])
        if expiry > current or resuming != turn.get('reanudando') or 'comprobando_hasta' in turn:
            with lock(self.state / 'espera.lock'):
                turn.pop('comprobando_hasta', None)
                turn.update(vence=stamp(max(current, expiry)), reanudando=resuming)
                atomic_json(path, turn)

    def ask(self, args):
        if args.vm in self.vms and self.vms[args.vm].repo != args.repo:
            raise InvalidUse('la VM pertenece a otro repositorio')
        turn = {'turno': uuid4().hex, 'repo': args.repo, 'corrida': args.corrida,
                'clase': args.clase, 'vm': args.vm, 'prioridad': args.prioridad,
                'orden': time.time_ns(), 'host': self.host, 'pid': os.getpid(), 'descartadas': []}
        if args.candidatas is not None:
            for name in args.candidatas:
                if name not in self.vms:
                    raise InvalidUse(f'VM fuera del pool: {name}')
                if self.vms[name].repo != args.repo:
                    raise InvalidUse(f'la VM pertenece a otro repositorio: {name}')
            turn['candidatas'] = args.candidatas
        candidates = [vm for vm in self.candidates(repo=args.repo, name=args.vm) if vm.accepts(turn)]
        if not candidates:
            raise Unavailable('sin_vm', 'sin VM apta o registrada')
        path = private_dir(self.state / 'espera') / (turn['turno'] + '.json')
        with lock(self.state / 'espera.lock'):
            started = time.monotonic()
            requested_at = self.now()
            turn.update(desde=stamp(requested_at), vence=stamp(requested_at + args.esperar + LOCK_WAIT))
            atomic_json(path, turn)
        deadline = started + args.esperar
        last_error = None
        rechecked = False
        waits = {}
        followed = {}
        ready_by = {}
        reclaim_by = {}
        blocked = set()
        reservation_delayed = False
        try:
            while candidates:
                blocked.clear()
                ordered = iter(self.ask_order(candidates, turn['clase']))
                contended = eligible = False
                resume_until = turn_until = -math.inf
                wait_before_resume = -math.inf
                waiting_to_restart = False
                adopted = None
                while True:
                    error = None
                    with ExitStack() as locks:
                        vm, leases, can_grant, can_resume, busy, accepted, locked_until = self.claim(
                            ordered, turn, path, followed, reclaim_by, locks, blocked)
                        contended |= busy
                        eligible |= accepted
                        resume_until = max(resume_until, locked_until)
                        turn_until = max(turn_until, locked_until)
                        if vm is None:
                            break
                        resuming = turn.get('reanudando')
                        try:
                            self.check_identity(vm)
                            info = self.sandbox_info(vm)
                            state = self.observe(vm, info)
                            pending = self.power_operation(vm)
                            pending_until = resume_deadline(pending)
                            if resume_replaced(followed.get(vm.id), pending_until):
                                raise BoatError(POWER_UNCONFIRMED)
                            if can_grant or (can_resume and state not in READY):
                                if leases and state not in READY:
                                    raise BoatError('VM con arriendos vivos fuera de servicio')
                                self.check_start_credentials(vm, state, info, pending, wait=False)
                            if state in READY:
                                if (not can_grant and vm.name not in blocked
                                        and (pending_until is not None or vm.id in followed)
                                        and vm.fits(leases, turn['clase'])):
                                    # El primero de la fila también tiene que observar READY.
                                    until = ready_by.setdefault(vm.id, self.now() + LOCK_WAIT)
                                    resume_until = max(resume_until, until)
                                    turn_until = max(turn_until, until)
                                    wait_before_resume = max(wait_before_resume, until)
                                if resuming == vm.name:
                                    resuming = None
                            else:
                                ready_by.pop(vm.id, None)
                                send = can_resume and (self.now() >= wait_before_resume or pending_until is not None)
                                until = self.ask_resume(
                                    vm, state, pending, waits, send=send, requested_at=requested_at, deadline=deadline,
                                    before_resume=lambda expiry: self.extend_turn(
                                        turn, path, expiry + LOCK_WAIT, vm.name),
                                )
                                if until is not None:
                                    if state in STARTING | STOPPED:
                                        if pending_until is not None or state in STOPPED:
                                            followed[vm.id] = until
                                        else:
                                            followed.setdefault(vm.id, None)
                                        if can_resume:
                                            resuming = vm.name
                                        elif adopted is None:
                                            # Primero busca una VM que este turno pueda arrancar.
                                            adopted = vm.name
                                    else:
                                        wait_before_resume = max(wait_before_resume, until)
                                    resume_until = max(resume_until, until)
                                    turn_until = max(turn_until, until + LOCK_WAIT)
                                else:
                                    waiting_to_restart |= pending_until is not None
                                    if resuming == vm.name:
                                        resuming = None
                        except JournalError as rejected:
                            self.reject(str(rejected), nombre=vm.name, repo=args.repo, corrida=args.corrida)
                            error = rejected
                        except (Unavailable, BoatError) as rejected:
                            self.reject(str(rejected), vm, repo=args.repo, corrida=args.corrida)
                            error = rejected
                        else:
                            eligible = True
                            if can_grant and state in READY:
                                if vm.idle is not None:
                                    self.try_renew_vm(vm, info)
                                # Otra VM pudo conceder durante la consulta a Boat.
                                # Quien toma espera.lock primero nunca espera el candado de una VM.
                                with lock(self.state / 'espera.lock'):
                                    now = self.now()
                                    pending, capacity = (self.reservation_loads(args.repo, now)
                                                         if args.prioridad != 'tren' else ([], []))
                                    if fits_reservations(vm, args.clase, pending, capacity):
                                        grant = self.grant(vm, args, now)
                                        remove(path)
                                        return grant
                                    blocked.add(vm.name)
                    if error is not None:
                        last_error = error
                        blocked.discard(vm.name)
                        turn['descartadas'].append(vm.name)
                        rechecked = False
                        candidates.remove(vm)
                        with lock(self.state / 'espera.lock'):
                            turn.pop('comprobando_hasta', None)
                            if turn.get('reanudando') == vm.name:
                                turn.pop('reanudando')
                            atomic_json(path, turn)
                    else:
                        self.extend_turn(turn, path, turn_until, resuming)
                # Una candidata ocupada aún podría dar a este turno su propio arranque.
                resuming = turn.get('reanudando') or (adopted if not contended else None)
                self.extend_turn(turn, path, turn_until, resuming)
                reservation_delayed |= bool(blocked)
                if not candidates:
                    break
                if not (contended or eligible or rechecked):
                    candidates = [vm for vm in self.candidates(repo=args.repo, name=args.vm)
                                  if vm.accepts(turn)]
                    if not candidates:
                        raise Unavailable('sin_vm', 'sin VM apta o registrada')
                    rechecked = True
                    continue
                attempt_until = started + LOCK_WAIT if contended and args.esperar == 0 else deadline
                resume_remaining = resume_until - self.now()
                remaining = max(attempt_until - time.monotonic(), resume_remaining)
                if remaining <= 0:
                    break
                self.sleep(min(1 if resume_remaining > 0 or waiting_to_restart else 0.25, remaining))
                rechecked = False
            # Sin candidatas, el último error explica la salida. Al vencer la espera, la reserva que frenó al turno.
            if candidates and reservation_delayed:
                raise Unavailable('sin_lugar', 'sin lugar al vencer la espera (una reserva del tren frenó el turno)')
            if last_error is not None:
                raise last_error
            raise Unavailable('sin_lugar', 'sin lugar al vencer la espera')
        finally:
            with lock(self.state / 'espera.lock'):
                remove(path)

    def grant(self, vm, args, now):
        lease = {'arriendo': uuid4().hex, 'repo': args.repo, 'corrida': args.corrida,
                 'host': self.host, 'pid': os.getpid(), 'clase': args.clase,
                 'prioridad': args.prioridad,
                 'desde': stamp(now), 'latido': stamp(now), 'vence': stamp(now + args.ttl)}
        vm.change_lease('concedido', now, lease)
        return {'arriendo': lease['arriendo'], 'vm': vm.name, 'id': vm.id, 'vence': lease['vence']}

    def lease_command(self, token, ttl=None):
        if not TOKEN.fullmatch(token):
            return 0 if ttl is None else 69
        for vm in self.candidates():
            if not (vm.path / 'arriendos' / (token + '.json')).exists() and not (
                vm.path / 'arriendo-pendiente.json'
            ).exists():
                continue
            with lock(vm.path / 'lock'):
                self.validate(vm)
                vm.finish_lease_change()
                path = vm.lease_path(token)
                try:
                    lease = read_json(path)
                except FileNotFoundError:
                    continue
                expiry = epoch(lease['vence'])
                if ttl is not None and expiry > self.now():
                    try:
                        self.power_operation(vm)
                    except BoatError:
                        return 69
                with lock(self.state / 'espera.lock'):
                    now = self.now()
                    if expiry <= now:
                        vm.change_lease('vencido', expiry, lease)
                        return 0 if ttl is None else 69
                    if ttl is None:
                        vm.change_lease('soltado', now, lease)
                    else:
                        lease.update(latido=stamp(now), vence=stamp(now + ttl))
                        atomic_json(path, lease)
                if ttl is not None and vm.idle is not None:
                    self.try_renew_vm(vm)
                return 0
        return 0 if ttl is None else 69

    def power_vm(self, vm, command, *, before_resume=None):
        try:
            leases = vm.leases(self.now(), expire=True)
            if command == 'apagar' and leases:
                raise Unavailable('sin_lugar', 'VM con arriendos vivos')
            self.check_identity(vm)
            info = self.sandbox_info(vm)
            state = self.observe(vm, info)
            if command == 'apagar':
                pending = self.power_operation(vm)
                if pending and pending.get('credenciales'):
                    state = info['state']
                self.stop(vm, state, wait=True)
            elif command != 'despertar' or state in STOPPED | STOPPING:
                if leases and state not in READY:
                    raise BoatError('VM con arriendos vivos fuera de servicio')
                self.start(vm, state, info, before_resume=before_resume)
        except (Unavailable, BoatError) as error:
            self.reject(str(error), vm)
            print_unavailable(error)
            return False
        return True

    def power_command(self, args):
        selected = self.candidates(name=args.nombre)
        if not selected:
            raise Unavailable('sin_vm', 'sin VM con registro válido')
        failed = False
        for vm in selected:
            with lock(vm.path / 'lock'):
                failed |= not self.power_vm(vm, args.command)
        return 75 if failed else 0

    def can_wake(self, vm):
        with lock(self.state / 'espera.lock'):
            now = self.now()
            return (vm.covered_by_batch(self.reservations(now))
                    and not (vm.path / 'operacion.json').exists()
                    and not vm.lease_snapshot(now)
                    and not any(vm.accepts(turn) for turn in self.turns()))

    def wake(self, repo):
        def confirm(_):
            if not self.can_wake(vm):
                raise WakeSkipped

        key = sha256(repo.encode()).hexdigest()
        with lock(self.state / f'despertar-{key}.lock', wait=False) as acquired:
            if not acquired:
                return 0
            failed = False
            for vm in self.candidates(repo=repo):
                with lock(vm.path / 'lock', wait=False) as available:
                    if not available or not self.can_wake(vm):
                        continue
                    try:
                        failed |= not self.power_vm(vm, 'despertar', before_resume=confirm)
                    except WakeSkipped:
                        continue
        return 75 if failed else 0

    def status(self):
        candidates = {vm.name: vm for vm in self.candidates()}
        rows = []
        for vm in self.vms.values():
            row = {'vm': vm.name, 'repo': vm.repo, 'tipo': vm.tipo,
                   'apta': False, 'estado': 'sin registro válido',
                   'arriendos': [], 'lugares_libres': 0}
            if vm.name in candidates:
                with lock(vm.path / 'lock'):
                    row.update(apta=vm.apt, estado='desconocido', arriendos=vm.leases(self.now()))
                    try:
                        self.check_identity(vm)
                        row['estado'] = self.observe(vm)
                    except (Unavailable, BoatError):
                        pass
                    if vm.apt and all(lease['clase'] != 'preparar' for lease in row['arriendos']):
                        row['lugares_libres'] = max(0, vm.places - vm.used_places(row['arriendos']))
            rows.append(row)
        try:
            gates = self.gates()
        except BoatError:
            gates = {name: {'valor': None, 'minimo': minimum, 'abierta': False}
                     for name, minimum in self.thresholds.items()}
        with lock(self.state / 'espera.lock'):
            reservations = [self.reservation_data(item) for item in self.reservations(self.now())]
        return {'vms': rows, 'puertas': gates, 'reservas': reservations}

    def cache_auto_stop(self, vm, info):
        path = vm.path / 'auto-stop.json'
        try:
            deadline = epoch(info.get('archiveAfter'))
        except (ValueError, OverflowError):
            remove(path)
            return None
        atomic_json(path, {'id': vm.id, 'archiveAfter': deadline, 'ttlSeconds': info.get('ttlSeconds')})
        return deadline

    def cached_auto_stop(self, vm, seconds):
        try:
            cached = read_json(vm.path / 'auto-stop.json')
            if cached['id'] == vm.id and cached.get('ttlSeconds') == seconds:
                return epoch(cached['archiveAfter'])
        except (FileNotFoundError, KeyError, ValueError, TypeError, OverflowError):
            pass
        return None

    def renew_vm(self, vm, info):
        seconds = vm.auto_stop_ttl
        if seconds is None:
            seconds, minimum = RENEW_FOR, RENEW_BEFORE
            if info.get('archiveAfter') is None or epoch(info['archiveAfter']) - self.now() >= minimum:
                return
        else:
            minimum = SUITE_MAX
            if info is None:
                deadline = self.cached_auto_stop(vm, seconds)
                if deadline is None or deadline <= self.now():
                    info = self.sandbox_info(vm)
                    if self.observe(vm, info) not in READY:
                        return
            if info is not None:
                deadline = self.cache_auto_stop(vm, info)
            if deadline is not None:
                remaining = deadline - self.now()
                if (minimum <= remaining <= seconds
                        and (info is None or info.get('ttlSeconds') == seconds)):
                    return
        self.validate(vm)
        now = self.now()
        result = self.boat.renew_lifetime(vm, seconds)
        if (not isinstance(result, dict)
                or (result.get('id'), result.get('name')) != (vm.id, vm.name)
                or epoch(result.get('archiveAfter')) - now < minimum):
            raise BoatError('Boat no confirmó la renovación')
        if vm.idle is not None:
            deadline = self.cache_auto_stop(vm, result)
            if (result.get('ttlSeconds') != seconds
                    or deadline > self.now() + seconds + AUTO_STOP_CLOCK_TOLERANCE):
                raise BoatError('Boat no confirmó el auto-stop')

    def try_renew_vm(self, vm, info=None):
        try:
            self.renew_vm(vm, info)
        except (Unavailable, BoatError, KeyError, ValueError, TypeError, OverflowError):
            return False
        return True

    def watch_vm(self, vm, notices):
        with ExitStack() as locks:
            with lock(self.state / 'espera.lock'):
                queue = self.turns()
                if not locks.enter_context(lock(vm.path / 'lock', wait=False)):
                    return
                busy = bool(vm.leases(self.now(), expire=True)) or any(vm.accepts(turn) for turn in queue)
            self.check_identity(vm)
            info = self.sandbox_info(vm)
            state = self.observe(vm, info)
            pending = self.power_operation(vm)
            if pending and pending.get('credenciales'):
                self.stop_unverified(vm, info['state'], wait=False)
                return
            if pending and pending['accion'] == 'resume' and info['state'] in READY:
                self.check_resumed(vm, info, pending, wait=False)
            if state in STOPPED and info.get('stoppedAt') is not None:
                if self.now() - epoch(info['stoppedAt']) >= STOPPED_NOTICE:
                    notices['disco:' + vm.name] = 'Retención de disco cerca del límite para ' + vm.name
            with lock(self.state / 'espera.lock'):
                covered = vm.covered_by_batch(self.reservations(self.now()))
            if vm.idle is None or busy or covered:
                renewable = READY | STOPPED if vm.idle is None else READY
                if state in renewable and not self.try_renew_vm(vm, info):
                    notices['ttl:' + vm.name] = 'No se pudo renovar el plazo de Boat de ' + vm.name
                return covered and not busy and pending is None and state in STOPPED | STOPPING
            if info['state'] not in READY:
                return
            try:
                deadline = epoch(info.get('archiveAfter'))
            except (ValueError, OverflowError):
                deadline = None
            limit = SUITE_MAX + vm.idle + AUTO_STOP_CLOCK_TOLERANCE
            if deadline is None or deadline - self.now() > limit:
                notices['ttl:' + vm.name] = 'Plazo de Boat sin acotar para ' + vm.name
                if state in READY:
                    self.stop(vm, state, wait=False)
                return
            if state not in READY:
                return
            activity = (epoch(item['en']) for item in events(vm.journal)
                        if item['evento'] in ('soltado', 'vencido', 'arranque'))
            last = max(chain((vm.prepared,), activity))
            if self.now() - last >= vm.idle:
                self.stop(vm, state, wait=False)

    def watch(self):
        candidates = self.candidates()
        with lock(self.state / 'vigilar.lock', wait=False) as acquired:
            if not acquired:
                return
            with lock(self.state / 'espera.lock'):
                self.reservations(self.now())
            notices = {}
            wake_repos = set()
            for vm in candidates:
                try:
                    if self.watch_vm(vm, notices):
                        wake_repos.add(vm.repo)
                except (Unavailable, BoatError, ValueError, TypeError, OverflowError) as error:
                    detail = str(error) if isinstance(error, (Unavailable, BoatError)) else 'datos de Boat inválidos'
                    with lock(self.state / 'eventos.lock'):
                        append_event(self.state / 'eventos.jsonl',
                                     vm.event('rechazo', self.now(), motivo=detail))
            for repo in sorted(wake_repos):
                self.wake_launcher(repo, self.state)
            try:
                gates = self.gates()
                for gate in ('horas_plan', 'monedero_usd'):
                    if not gates[gate]['abierta']:
                        notices[gate] = 'Piso del pool sin acreditar: ' + gate
            except BoatError:
                notices['limites'] = 'No se pudo comprobar el piso del pool'
            try:
                raw = self.boat.key_expiration()
                if raw is not None and epoch(raw) - self.now() <= 3 * 86400:
                    notices['llave'] = 'La llave de Boat vence (UTC): ' + stamp(epoch(raw))
            except (BoatError, ValueError, TypeError, OverflowError):
                notices['llave'] = 'No se pudo comprobar el vencimiento de la llave de Boat'
            self.warn(notices)

    def warn_vm(self, vm, key, message):
        marker = vm.path / ('aviso-' + key + '.json')
        if not marker.exists():
            self.deliver_notice(message)
            atomic_json(marker, {'en': stamp(self.now())})

    def deliver_notice(self, message):
        with lock(self.state / 'avisos.lock'):
            append_line(self.state / 'avisos.log', stamp(self.now()) + ' ' + message)
        self.notify(message)

    def warn(self, notices):
        path = self.state / 'avisos.json'
        try:
            previous = read_json(path)
        except FileNotFoundError:
            previous = {}
        for key, message in notices.items():
            if previous.get(key) != message:
                self.deliver_notice(message)
        if notices != previous:
            atomic_json(path, notices)

    def monthly_org_cost(self, month, inventory, deleted):
        start = datetime.combine(month, datetime.min.time(), tzinfo=timezone.utc).timestamp()
        seconds = dollars = 0
        try:
            for cost in chain((deleted,), (sandbox.get('cost') for sandbox in inventory)):
                if epoch(cost['since']) != start:
                    raise ValueError('mes distinto')
                seconds += integer(cost['seconds'], 0)
                dollars += number(cost['dollars'], 0)
        except (KeyError, TypeError, ValueError, OverflowError):
            raise BoatError('Boat no confirmó el consumo del mes UTC en curso') from None
        return f'{seconds / 3600:.6f}', f'{dollars:.6f}'

    def report_periods(self, directory, journal, day_start, now):
        path = directory / 'eventos.jsonl'
        name = directory.name
        prepared = {}
        creations = {}
        registered = set()
        latest_event = {}
        for item in journal:
            identifier = item.get('id')
            moment = epoch(item['en'])
            if item['evento'] != 'creacion':
                latest_event[identifier] = max(moment, latest_event.get(identifier, -math.inf))
            if item['evento'] == 'registro':
                prepared[identifier] = min(moment, prepared.get(identifier, math.inf))
                registered.add(identifier)
            if item['evento'] in ('registro', 'creacion') and 'creada_en' in item:
                creations[identifier] = epoch(item['creada_en'])

        current = None
        current_type = None
        retired = {}
        invalid_retirements = set()
        for record_path in chain((directory / 'registro.json',), directory.glob('registro.retirado-*.json')):
            try:
                record = read_json(record_path)
                if record['nombre'] != name:
                    raise ValueError('registro no coincide con el directorio')
                identifier = record['id']
                check_id(identifier)
                moment = epoch(record['preparada_en'])
            except FileNotFoundError:
                continue
            prepared[identifier] = min(moment, prepared.get(identifier, math.inf))
            if record_path.name == 'registro.json':
                current = identifier
                current_type = record.get('tipo')
            else:
                try:
                    closure = int(record_path.stem.removeprefix('registro.retirado-')) / 1e9
                except (ValueError, OverflowError):
                    invalid_retirements.add(identifier)
                    continue
                if closure < max(moment, creations.get(identifier, moment)) + INCIERTO:
                    invalid_retirements.add(identifier)
                    continue
                retired.setdefault(identifier, []).append(closure)

        periods = []
        for identifier, moment in prepared.items():
            closure = max(retired.get(identifier, ()), default=None)
            invalid = identifier in invalid_retirements or (closure is not None and closure > now)
            period = UsagePeriod(identifier, moment, created=creations.get(identifier),
                                 retirement_invalid=invalid)
            if identifier != current and not period.retirement_invalid:
                period.retired = closure
                if closure is None:
                    last_event = max(moment, latest_event.get(identifier, -math.inf))
                    if len(prepared) == 1:
                        last_event = max(last_event, latest_event.get(None, -math.inf))
                    period.last_event_without_retirement = last_event
            periods.append(period)
            if period.created is not None:
                continue
            if (day_start is not None and period.retired is not None
                    and period.retired <= day_start - INCIERTO):
                continue
            try:
                created_at, period.created = self.fetch_creation(name, identifier, moment, now=now)
            except (Unavailable, BoatError) as error:
                period.creation_error = str(error)
            except (TypeError, ValueError):
                period.creation_error = 'ID inválido'
            new_registration = identifier == current and identifier not in registered
            if new_registration or period.created is not None:
                kind = 'registro' if new_registration else 'creacion'
                event = {'evento': kind, 'en': stamp(moment), 'vm': name, 'id': identifier}
                if new_registration:
                    event['tipo'] = current_type
                if period.created is not None:
                    event['creada_en'] = created_at
                append_event(path, event)
        return periods

    def daily_pool_cost(self, registrations, unregistered, start, end, now):
        until = min(end, math.floor(now * 1000) / 1000)
        since_stamp, until_stamp = stamp(start), stamp(until)
        hours = Counter()
        dollars = 0
        queried = set()
        errors = [reason for created, reason in unregistered
                  if (created is None and start <= now)
                  or (created is not None and creation_start(created) < end
                      and (start <= now or creation_midnight(created) == start))]
        for name, first_event, periods in registrations:
            first_registration = min((p.created if p.created is not None else p.prepared
                                      for p in periods), default=math.inf)
            if start < until and first_event < min(first_registration, until) and start < first_registration:
                errors.append(f'{name}: diario histórico sin ID de Boat')
            for period in periods:
                created = period.created
                uncertain_creation = (created is not None and created <= now
                                      and creation_midnight(created) in (start, end))
                if uncertain_creation:
                    errors.append(f'{name}: creación cerca de medianoche UTC')
                if period.retirement_invalid and start <= now and (created is None or created < end):
                    errors.append(f'{name}: cierre de retiro inválido')
                    continue
                if uncertain_creation or start >= until:
                    continue
                if period.retired is not None and period.retired <= start - INCIERTO:
                    continue
                if created is not None and (created >= end or created > now):
                    continue
                if (period.last_event_without_retirement is not None
                        and period.last_event_without_retirement < start):
                    errors.append(f'{name}: falta registro de retiro después del último evento')
                    continue
                if created is None:
                    errors.append(f'{name}: creación sin acreditar: {period.creation_error}')
                    continue
                identifier = period.identifier
                try:
                    self.check_query_id(name, identifier, now=now)
                    if identifier in queried:
                        continue
                    queried.add(identifier)
                    usage = self.boat.usage(identifier, since_stamp, until_stamp)
                    tipo = usage['sandboxType']
                    if (usage.get('ok') is not True or usage.get('type') != 'sandbox.usage'
                            or usage['sandboxId'] != identifier or tipo not in POOL_TYPES
                            or epoch(usage['since']) != start or epoch(usage['until']) != until
                            or type(usage['running']) is not bool):
                        raise ValueError('respuesta inválida')
                    integer(usage['secondsPerDollar'], 1)
                    multiplier = number(usage['billingMultiplier'])
                    if multiplier <= 0:
                        raise ValueError('multiplicador inválido')
                    hours[tipo] += integer(usage['seconds'], 0) / multiplier / 3600
                    dollars += number(usage['dollars'], 0)
                    number(hours[tipo])
                    number(dollars)
                except (Unavailable, BoatError) as error:
                    errors.append(f'{name}: {error}')
                except (KeyError, TypeError, ValueError, OverflowError):
                    errors.append(f'{name}: respuesta de uso inválida o ID inválido')
        if errors:
            raise BoatError(', '.join(errors))
        return (*(f'{hours[tipo]:.6f}' for tipo in POOL_TYPES), f'{dollars:.6f}')

    def report(self, day):
        now = self.now()
        today = datetime.fromtimestamp(now, timezone.utc).date()
        month = today.replace(day=1)
        known_ids = {name: set() for name in self.vms}
        include_month = day is None or day.replace(day=1) == month
        inventory, deleted = (), None
        inventory_error = None
        # Un retiro durante la consulta puede aparecer sólo en el estado local.
        if include_month or known_ids:
            try:
                inventory, deleted = self.boat.org_inventory()
            except BoatError as error:
                inventory_error = f'inventario de Boat sin acreditar: {error}'
            else:
                if deleted is None:
                    inventory_error = 'inventario de Boat sin acreditar: falta deletedCost'

        history = []
        registrations = []
        day_start = epoch(day.isoformat() + 'T00:00:00Z') if day else None
        for directory in (self.state / 'vms').glob('*'):
            if not directory.is_dir():
                continue
            vm = self.vms.get(directory.name)
            with lock(directory / 'lock'):
                if vm is not None:
                    vm.finish_lease_change()
                journal = events(directory / 'eventos.jsonl')
                periods = self.report_periods(directory, journal, day_start, now)
                if vm is not None:
                    known_ids[vm.name].update(period.identifier for period in periods)
                    known_ids[vm.name].update(item['id'] for item in journal if item.get('id') is not None)
                first_event = min((epoch(item['en']) for item in journal), default=math.inf)
                registrations.append((directory.name, first_event, periods))
                history.extend((item, directory.name, periods) for item in journal)
        monthly_cost = ('NA', 'NA')
        monthly_error = None
        unregistered = []
        for sandbox in inventory:
            name = sandbox.get('name')
            identifier = sandbox['id']
            if (not isinstance(name, str) or name not in known_ids
                    or now < self.prohibited.get(identifier, -math.inf) or identifier in known_ids[name]):
                continue
            reason = f'{name}: VM de Boat sin registro ({identifier})'
            try:
                created = epoch(label(sandbox.get('createdAt')))
                if created > now:
                    raise ValueError('fecha futura')
            except (ValueError, OverflowError):
                created = None
                reason += ', creación sin acreditar'
            unregistered.append((created, reason))
        if include_month and inventory_error is None:
            try:
                monthly_cost = self.monthly_org_cost(month, inventory, deleted)
            except BoatError as error:
                monthly_error = str(error)

        first_time = min(chain(
            (first_event for _, first_event, _ in registrations),
            (creation_start(period.created) if period.created is not None else period.prepared
             for _, _, periods in registrations for period in periods),
            (creation_start(created) for created, _ in unregistered if created is not None),
            (now,),
        ))
        first = datetime.fromtimestamp(first_time, timezone.utc).date()
        days = [day] if day else [first + timedelta(days=i) for i in range((today - first).days + 1)]
        writer = csv.writer(sys.stdout, delimiter='\t', lineterminator='\n')
        for day in days:
            start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp()
            end = start + 86400
            try:
                pool_cost = self.daily_pool_cost(registrations, unregistered, start, end, now)
            except BoatError as error:
                pool_cost = ('NA',) * 4
                print(f'boat-pool: reporte {day}: {error}', file=sys.stderr)
            if inventory_error is not None:
                pool_cost = ('NA',) * 4
                print(f'boat-pool: reporte {day}: {inventory_error}', file=sys.stderr)
            daily = []
            unlocated = set()
            for item, name, periods in history:
                moment = epoch(item['en'])
                if not start <= moment < end:
                    continue
                if moment <= now:
                    daily.append(item)
                if item.get('id') is not None or item['evento'] not in (
                        'arranque', 'encendida', 'apagada', 'concedido', 'soltado', 'vencido'):
                    continue
                if not any(p.created is not None and p.created <= moment <= now
                           and not p.retirement_invalid
                           and (p.retired is None or moment <= p.retired)
                           and (p.last_event_without_retirement is None
                                or moment <= p.last_event_without_retirement)
                           for p in periods):
                    unlocated.add(name)
            if unlocated:
                pool_cost = ('NA',) * 4
                for name in sorted(unlocated):
                    print(f'boat-pool: reporte {day}: {name}: actividad sin ID fuera de todo periodo',
                          file=sys.stderr)
            starts = sum(item['evento'] == 'arranque' for item in daily)
            runs = {(item['repo'], item['corrida']) for item in daily if item['evento'] == 'concedido'}
            org_cost, error = monthly_cost, monthly_error
            if day.replace(day=1) != month:
                org_cost, error = ('NA', 'NA'), 'Boat sólo informa el mes UTC en curso'
            if error is not None:
                print(f'boat-pool: reporte {day}: {error}', file=sys.stderr)
            writer.writerow([day.isoformat(), *pool_cost, starts, len(runs), *org_cost])


class Usage(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(64, f'{self.prog}: uso inválido: {message}\n')


def seconds(value, *, minimum, maximum):
    try:
        result = number(float(value), minimum)
        if result <= maximum:
            return result
    except (ValueError, OverflowError):
        pass
    raise argparse.ArgumentTypeError(f'usa entre {minimum} y {maximum} segundos')


def ttl(value):
    return seconds(value, minimum=300, maximum=3600)


def deadline(value):
    try:
        return number(float(value))
    except ValueError:
        raise argparse.ArgumentTypeError('usa un instante Unix en segundos') from None


def wait_seconds(value):
    return seconds(value, minimum=0, maximum=7200)


def iso_day(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() == value:
            return parsed
    except ValueError:
        pass
    raise argparse.ArgumentTypeError('usa AAAA-MM-DD')


def parser():
    cli = Usage(prog='boat-pool')
    commands = cli.add_subparsers(dest='command', required=True)
    cmd = commands.add_parser('pedir')
    for field in ('repo', 'corrida', 'clase'):
        cmd.add_argument('--' + field, required=True)
    cmd.add_argument('--prioridad', choices=PRIORITY)
    cmd.add_argument('--vm')
    cmd.add_argument('--candidatas')
    cmd.add_argument('--esperar', type=wait_seconds, default=0)
    cmd.add_argument('--ttl', type=ttl, default=900)
    cmd = commands.add_parser('latido')
    cmd.add_argument('arriendo')
    cmd.add_argument('--ttl', type=ttl, default=900)
    commands.add_parser('soltar').add_argument('arriendo')
    for name in ('reservar', 'liberar-reserva'):
        cmd = commands.add_parser(name)
        cmd.add_argument('--repo', required=True)
        cmd.add_argument('--lote', required=True)
        if name == 'reservar':
            cmd.add_argument('--clases', required=True)
            expiration = cmd.add_mutually_exclusive_group(required=True)
            expiration.add_argument('--ttl', type=ttl)
            expiration.add_argument('--vence', type=deadline, metavar='EPOCH')
    commands.add_parser('estado').add_argument('--json', action='store_true')
    for name in ('encender', 'apagar'):
        cmd = commands.add_parser(name)
        target = cmd.add_mutually_exclusive_group(required=True)
        target.add_argument('nombre', nargs='?')
        target.add_argument('--todas', action='store_true')
    commands.add_parser('despertar').add_argument('--repo', required=True)
    commands.add_parser('vigilar')
    commands.add_parser('reporte').add_argument('--dia', type=iso_day)
    return cli


def main(argv=None, **pool_options):
    args = parser().parse_args(argv)
    try:
        if args.command == 'pedir':
            for value in (args.repo, args.corrida, args.clase):
                label(value)
            preparing = args.clase == 'preparar'
            if preparing != (args.vm is not None) or (args.prioridad == 'preparar' and not preparing):
                raise InvalidUse('--vm y la prioridad preparar corresponden a la clase preparar')
            if args.candidatas is not None:
                if preparing:
                    raise InvalidUse('--candidatas no se admite con la clase preparar')
                args.candidatas = [label(name) for name in args.candidatas.split(',')]
            args.prioridad = 'preparar' if preparing else (args.prioridad or 'otra')
        elif args.command in ('reservar', 'liberar-reserva'):
            label(args.repo)
            label(args.lote)
            if args.command == 'reservar':
                args.clases = [label(kind) for kind in args.clases.split(',')]
                if 'preparar' in args.clases or len(set(args.clases)) != len(args.clases):
                    raise InvalidUse('--clases no admite preparar ni clases repetidas')
        elif args.command == 'despertar':
            label(args.repo)
        config = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'boat-pool' / 'pool.json'
        state = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 'boat-pool'
        try:
            declaration = read_json(config)
        except FileNotFoundError:
            if args.command == 'pedir':
                raise Unavailable('apagado', 'falta pool.json') from None
            raise
        pool = Pool(declaration, state, **pool_options)
    except InvalidUse as error:
        print(str(error), file=sys.stderr)
        return 64
    except Unavailable as error:
        print_unavailable(error)
        return 75
    except (OSError, KeyError, TypeError, ValueError, OverflowError):
        print('configuración o argumentos del pool inválidos', file=sys.stderr)
        return 64
    try:
        if args.command == 'pedir':
            print(json.dumps(pool.ask(args), ensure_ascii=False))
        elif args.command == 'latido':
            return pool.lease_command(args.arriendo, args.ttl)
        elif args.command == 'soltar':
            return pool.lease_command(args.arriendo)
        elif args.command == 'reservar':
            pool.reservation_command(args.repo, args.lote, args.clases, args.ttl, until=args.vence)
        elif args.command == 'liberar-reserva':
            pool.reservation_command(args.repo, args.lote)
        elif args.command in ('encender', 'apagar'):
            return pool.power_command(args)
        elif args.command == 'despertar':
            return pool.wake(args.repo)
        elif args.command == 'estado':
            status = pool.status()
            if args.json:
                print(json.dumps(status, ensure_ascii=False))
            else:
                for vm in status['vms']:
                    print(f"{vm['vm']}\t{vm['estado']}\t{vm['lugares_libres']}")
                    for lease in vm['arriendos']:
                        fields = ('arriendo', 'corrida', 'clase', 'vence')
                        print('\t' + '\t'.join(str(lease.get(field, '')) for field in fields))
                for name, gate in status['puertas'].items():
                    print(f"{name}\t{'abierta' if gate['abierta'] else 'cerrada'}")
                for item in status['reservas']:
                    classes = ','.join(item['clases'])
                    print(f"reserva\t{item['repo']}\t{item['lote']}\t{classes}\t{item['vence']}")
        elif args.command == 'vigilar':
            pool.watch()
        else:
            pool.report(args.dia)
    except InvalidUse as error:
        print(str(error), file=sys.stderr)
        return 64
    except (Unavailable, BoatError) as error:
        print_unavailable(error)
        pool.reject(str(error))
        return 75
    except (OSError, KeyError, TypeError, ValueError, OverflowError):
        print_unavailable(Unavailable('apagado', 'estado del pool inválido o no disponible'))
        return 75
    return 0
