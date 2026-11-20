from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack, contextmanager
import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import errno
import fcntl
import hashlib
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


RATES = {'default': 0.036, 'large': 0.072, 'xlarge': 0.20}
PRIORITY = {'preparar': 0, 'tren': 1, 'suite': 2, 'otra': 3}
READY = {'ready', 'idle', 'running'}
STOPPED = {'archived', 'stopped'}
STARTING = {'init', 'provisioning', 'provisioned', 'cloning', 'resuming'}
STOPPING = {'archiving', 'stopping'}
VM_ID = re.compile(r'bx_[a-z0-9]{8}')
TOKEN = re.compile(r'[a-f0-9]{32}')
FORBIDDEN_IDS = {
    '6db1388f2c8702233092e9984155c756efa91805fbb2a1161aae02ae72ab2d11',
    '694be487037963332cd9dfd7b72a0b599982a60bfaacda73a460d18714cc8803',
}
TEMPORARY_ID = 'cb3aa675ef28f3f40c7def89e1e6a22a23a894c12bfa583de4842aff1515f3ce'
TEMPORARY_UNTIL = datetime(2026, 11, 21, 12, tzinfo=timezone.utc).timestamp()
GATES = {
    'reserva_arranques': ('starts', 'day', 'remaining'),
    'horas_plan': ('planIncluded', 'vmHoursRemaining'),
    'monedero_usd': ('wallet', 'balanceDollars'),
}
POWER_TIMEOUT = 90
RENEW_BEFORE = 48 * 3600
RENEW_FOR = 7 * 86400
STOPPED_RETENTION = 14 * 86400


class Unavailable(RuntimeError):
    pass


class InvalidUse(ValueError):
    pass


def stamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace('+00:00', 'Z')


def epoch(value):
    if isinstance(value, str):
        if value.endswith('Z'):
            value = value[:-1] + '+00:00'
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            raise ValueError('fecha sin zona')
        return moment.timestamp()
    return number(value)


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


def safe_name(name):
    if name.startswith('ci-') or name == 'hasito-sen' or VM_ID.fullmatch(name):
        raise Unavailable('nombre de VM reservado')
    if name in ('.', '..') or '/' in name or not name.isprintable() or not name.strip():
        raise ValueError('nombre de VM inválido')


def safe_id(identifier, now):
    if not isinstance(identifier, str) or not VM_ID.fullmatch(identifier):
        raise ValueError('ID de VM inválido')
    digest = hashlib.sha256(identifier.encode('ascii')).hexdigest()
    if digest in FORBIDDEN_IDS or (digest == TEMPORARY_ID and now < TEMPORARY_UNTIL):
        raise Unavailable('ID de VM reservado')


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


@dataclass
class VM:
    name: str
    repo: str
    tipo: str
    places: int
    classes: dict
    idle: float | None
    path: Path
    id: str = ''
    prepared: float = 0
    apt: bool = False

    @property
    def journal(self):
        return self.path / 'eventos.jsonl'

    def registration(self, now):
        record = read_json(self.path / 'registro.json')
        safe_id(record['id'], now)
        if (record['nombre'], record['repo'], record['tipo']) != (self.name, self.repo, self.tipo):
            raise ValueError('registro no coincide con el pool')
        if type(record['apta']) is not bool:
            raise ValueError('aptitud inválida')
        number(record['cpu_robada_pct'], 0)
        return record['id'], epoch(record['preparada_en']), record['apta']

    def validate(self, now):
        try:
            current = self.registration(now)
        except (FileNotFoundError, KeyError, TypeError, ValueError, OverflowError):
            raise Unavailable('VM sin registro válido') from None
        if current != (self.id, self.prepared, self.apt):
            raise Unavailable('registro de VM cambió')

    def event(self, kind, when, **fields):
        return {'evento': kind, 'en': stamp(when), 'vm': self.name, 'tipo': self.tipo, **fields}

    def lease_path(self, token):
        return private_dir(self.path / 'arriendos') / (token + '.json')

    def finish_lease_change(self):
        pending = self.path / 'arriendo-pendiente.json'
        try:
            change = read_json(pending)
        except FileNotFoundError:
            return
        event = change['evento']
        if not any(item.get('cambio') == event['cambio'] for item in events(self.journal)):
            append_event(self.journal, event)
        path = self.lease_path(event['arriendo'])
        if change['arriendo'] is None:
            remove(path)
        else:
            atomic_json(path, change['arriendo'])
        remove(pending)

    def change_lease(self, kind, when, lease):
        event = self.event(kind, when, arriendo=lease['arriendo'], repo=lease['repo'],
                           corrida=lease['corrida'], cambio=uuid4().hex)
        atomic_json(self.path / 'arriendo-pendiente.json', {
            'evento': event,
            'arriendo': lease if kind == 'concedido' else None,
        })
        self.finish_lease_change()

    def leases(self, now, *, expire=False):
        self.finish_lease_change()
        live = []
        for path in (self.path / 'arriendos').glob('*.json'):
            lease = read_json(path)
            expiry = epoch(lease['vence'])
            if expiry > now:
                live.append(lease)
            elif expire:
                self.change_lease('vencido', expiry, lease)
        return live

    def fits(self, leases, kind):
        if kind == 'preparar':
            return not leases
        return (len(leases) < self.places
                and not any(lease['clase'] == 'preparar' for lease in leases)
                and sum(lease['clase'] == kind for lease in leases) < self.classes.get(kind, self.places))

    def accepts(self, turn):
        if turn['repo'] != self.repo:
            return False
        if turn['clase'] == 'preparar':
            return turn['vm'] == self.name
        return self.apt and self.fits([], turn['clase'])


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
                 now=time.time, sleep=time.sleep):
        if integer(config['version'], 1) != 1:
            raise ValueError('versión del pool inválida')
        self.now = now
        self.sleep = sleep
        self.notify = notify
        self.host = socket.gethostname()
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
            if name in self.vms or data['tipo'] not in RATES:
                raise ValueError('VM duplicada o tipo inválido')
            places = integer(data['lugares'], 1)
            classes = {label(kind): integer(limit, 0) for kind, limit in data.get('limite_clase', {}).items()}
            idle = data.get('inactividad_min', default_idle)
            idle = None if idle is None else number(idle, 0) * 60
            self.vms[name] = VM(name, label(data['repo']), data['tipo'], places,
                                classes, idle, state / 'vms' / name)

    def candidates(self, *, name=None, repo=None):
        if name is not None:
            safe_name(name)
            if name not in self.vms:
                raise Unavailable('VM fuera del pool')
        selected = []
        ids = Counter()
        for vm in self.vms.values():
            wanted = (name is None or name == vm.name) and (repo is None or repo == vm.repo)
            try:
                vm.id, vm.prepared, vm.apt = vm.registration(self.now())
            except Unavailable:
                if wanted:
                    raise
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
            raise Unavailable('puerta cerrada: ' + ', '.join(closed))

    def check_identity(self, vm):
        vm.validate(self.now())
        matching = [item for item in self.boat.find(vm.name)
                    if isinstance(item, dict) and item.get('name') == vm.name]
        if len(matching) != 1 or matching[0].get('id') != vm.id:
            raise Unavailable('VM de Boat no coincide con el registro')

    def sandbox_info(self, vm):
        vm.validate(self.now())
        info = self.boat.sandbox(vm)
        if not isinstance(info, dict) or (info.get('id'), info.get('name')) != (vm.id, vm.name):
            raise Unavailable('VM de Boat no coincide con el registro')
        return info

    def observe(self, vm, info=None):
        if info is None:
            info = self.sandbox_info(vm)
        state = info.get('state')
        states = READY | STOPPED | STARTING | STOPPING | {'error', 'cancelled'}
        if not isinstance(state, str) or state not in states:
            raise BoatError('estado de VM desconocido')
        stop = info.get('stop')
        if state in READY and isinstance(stop, dict) and stop.get('status') in ('pending', 'failing'):
            state = 'archiving'
        return self.record_power(vm, state, info.get('stoppedAt'))

    def record_power(self, vm, state, stopped_at):
        path = vm.path / 'operacion.json'
        try:
            pending = read_json(path)
        except FileNotFoundError:
            pending = None
        history = events(vm.journal)
        last = next((item for item in reversed(history)
                     if item['evento'] in ('encendida', 'apagada')), None)
        now = self.now()
        if (pending and pending['accion'] == 'stop' and state in READY
                and now - epoch(pending['desde']) < POWER_TIMEOUT):
            state = 'archiving'
        if pending and pending['accion'] == 'resume' and state in READY | STARTING:
            self.record_start(vm, pending, history)
        if state in READY and (last is None or last['evento'] == 'apagada'):
            since = min(now, vm.prepared) if last is None and pending is None else now
            append_event(vm.journal, vm.event('encendida', since))
        if last and last['evento'] == 'encendida' and state in STOPPED | {'error', 'cancelled'}:
            try:
                stopped = max(epoch(last['en']), min(now, epoch(stopped_at)))
            except (TypeError, ValueError, OverflowError):
                stopped = now
            append_event(vm.journal, vm.event('apagada', stopped))
        if pending and (
            (pending['accion'] == 'resume' and state in READY)
            or (pending['accion'] == 'stop' and state in STOPPED)
        ):
            remove(path)
        return state

    def record_start(self, vm, operation, history):
        if not any(item.get('operacion') == operation['id'] for item in history):
            append_event(vm.journal, vm.event('arranque', epoch(operation['desde']), operacion=operation['id']))

    def send_power(self, vm, action):
        path = vm.path / 'operacion.json'
        try:
            previous = read_json(path)
        except FileNotFoundError:
            previous = None
        if previous and previous['accion'] == 'resume' and self.now() - epoch(previous['desde']) < POWER_TIMEOUT:
            raise Unavailable('reanudación anterior sin confirmar')
        vm.validate(self.now())
        operation = {'id': uuid4().hex, 'accion': action, 'desde': stamp(self.now())}
        atomic_json(path, operation)
        self.boat.power(vm, action)
        if action == 'resume':
            self.record_start(vm, operation, events(vm.journal))

    def wait_power(self, vm, target, pending):
        deadline = time.monotonic() + POWER_TIMEOUT
        while True:
            state = self.observe(vm)
            if state in target:
                return
            if state not in pending or time.monotonic() >= deadline:
                raise Unavailable('Boat no confirmó el cambio de estado')
            self.sleep(1)

    def start(self, vm, state):
        if state in READY:
            return
        if state in STOPPING:
            self.wait_power(vm, STOPPED, STOPPING)
            state = 'archived'
        if state in STOPPED or state == 'error':
            with lock(self.state / 'arranques.lock'):
                self.check_gates(vm)
                self.send_power(vm, 'resume')
        elif state not in STARTING:
            raise Unavailable('estado de VM no reanudable')
        self.wait_power(vm, READY, STARTING | STOPPED)

    def stop(self, vm, state, *, wait):
        if state in STOPPED:
            return
        if state in READY:
            self.send_power(vm, 'stop')
        elif state not in STOPPING:
            raise Unavailable('VM aún no se puede detener')
        if wait:
            self.wait_power(vm, STOPPED, READY | STOPPING)
        else:
            self.observe(vm)

    def turns(self):
        waiting = []
        for path in private_dir(self.state / 'espera').glob('*.json'):
            turn = read_json(path)
            if turn['host'] != self.host or not pid_alive(turn['pid']):
                remove(path)
            else:
                waiting.append(turn)
        return sorted(waiting, key=lambda item: (PRIORITY[item['prioridad']], item['orden'], item['turno']))

    def claim(self, candidates, turn, path, locks):
        with lock(self.state / 'espera.lock'):
            queue = self.turns()
            for vm in candidates:
                if not locks.enter_context(lock(vm.path / 'lock', wait=False)):
                    locks.close()
                    continue
                vm.validate(self.now())
                leases = vm.leases(self.now())
                first = next((item for item in queue if vm.accepts(item) and (
                    item['clase'] == 'preparar' or vm.fits(leases, item['clase'])
                )), None)
                if first is not None and first['turno'] == turn['turno'] and vm.fits(leases, turn['clase']):
                    remove(path)
                    return vm, leases
                locks.close()
        return None, []

    def ask(self, args):
        if args.vm in self.vms and self.vms[args.vm].repo != args.repo:
            raise InvalidUse('la VM pertenece a otro repositorio')
        deadline = time.monotonic() + args.esperar
        turn = {'turno': uuid4().hex, 'repo': args.repo, 'corrida': args.corrida,
                'clase': args.clase, 'vm': args.vm, 'prioridad': args.prioridad,
                'desde': stamp(self.now()), 'orden': time.time_ns(), 'host': self.host, 'pid': os.getpid()}
        candidates = [vm for vm in self.candidates(repo=args.repo, name=args.vm) if vm.accepts(turn)]
        if not candidates:
            raise Unavailable('sin VM apta o registrada')
        path = private_dir(self.state / 'espera') / (turn['turno'] + '.json')
        with lock(self.state / 'espera.lock'):
            atomic_json(path, turn)
        last_error = None
        try:
            while candidates:
                with ExitStack() as locks:
                    vm, leases = self.claim(candidates, turn, path, locks)
                    if vm is not None:
                        try:
                            self.check_identity(vm)
                            state = self.observe(vm)
                            if leases and state not in READY:
                                raise Unavailable('VM con arriendos vivos fuera de servicio')
                            self.start(vm, state)
                        except (Unavailable, BoatError) as error:
                            self.reject(str(error), vm, repo=args.repo, corrida=args.corrida)
                            last_error = error
                        else:
                            now = self.now()
                            lease = {'arriendo': uuid4().hex, 'repo': args.repo, 'corrida': args.corrida,
                                     'host': self.host, 'pid': os.getpid(), 'clase': args.clase,
                                     'desde': stamp(now), 'latido': stamp(now),
                                     'vence': stamp(now + args.ttl)}
                            vm.change_lease('concedido', now, lease)
                            return {'arriendo': lease['arriendo'], 'vm': vm.name,
                                    'id': vm.id, 'vence': lease['vence']}
                if vm is not None:
                    candidates.remove(vm)
                    if candidates:
                        with lock(self.state / 'espera.lock'):
                            atomic_json(path, turn)
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.sleep(min(0.25, remaining))
            raise Unavailable(str(last_error) if last_error else 'sin lugar al vencer la espera')
        finally:
            with lock(self.state / 'espera.lock'):
                remove(path)

    def lease_command(self, token, ttl=None):
        if not TOKEN.fullmatch(token):
            return 0 if ttl is None else 69
        for vm in self.candidates():
            if not (vm.path / 'arriendos' / (token + '.json')).exists() and not (
                vm.path / 'arriendo-pendiente.json'
            ).exists():
                continue
            with lock(vm.path / 'lock'):
                vm.validate(self.now())
                vm.finish_lease_change()
                path = vm.lease_path(token)
                try:
                    lease = read_json(path)
                except FileNotFoundError:
                    continue
                now = self.now()
                expiry = epoch(lease['vence'])
                if expiry <= now:
                    vm.change_lease('vencido', expiry, lease)
                    return 0 if ttl is None else 69
                if ttl is None:
                    vm.change_lease('soltado', now, lease)
                else:
                    lease.update(latido=stamp(now), vence=stamp(now + ttl))
                    atomic_json(path, lease)
                return 0
        return 0 if ttl is None else 69

    def power_command(self, args):
        selected = self.candidates(name=args.nombre)
        if not selected:
            raise Unavailable('sin VM con registro válido')
        failed = False
        for vm in selected:
            with lock(vm.path / 'lock'):
                try:
                    leases = vm.leases(self.now(), expire=True)
                    if args.command == 'apagar' and leases:
                        raise Unavailable('VM con arriendos vivos')
                    self.check_identity(vm)
                    state = self.observe(vm)
                    if args.command == 'encender':
                        if leases and state not in READY:
                            raise Unavailable('VM con arriendos vivos fuera de servicio')
                        self.start(vm, state)
                    else:
                        self.stop(vm, state, wait=True)
                except (Unavailable, BoatError) as error:
                    self.reject(str(error), vm)
                    print(str(error), file=sys.stderr)
                    failed = True
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
                        row['lugares_libres'] = max(0, vm.places - len(row['arriendos']))
            rows.append(row)
        try:
            gates = self.gates()
        except BoatError:
            gates = {name: {'valor': None, 'minimo': minimum, 'abierta': False}
                     for name, minimum in self.thresholds.items()}
        return {'vms': rows, 'puertas': gates}

    def renew_vm(self, vm, info):
        deadline = info.get('archiveAfter')
        stopped_disk = deadline is None and info['state'] in STOPPED
        if stopped_disk:
            deadline = epoch(info['stoppedAt']) + STOPPED_RETENTION
        if deadline is None or epoch(deadline) - self.now() >= RENEW_BEFORE:
            return
        vm.validate(self.now())
        result = self.boat.renew_lifetime(vm, RENEW_FOR, stopped_disk=stopped_disk)
        if (not isinstance(result, dict)
                or (result.get('id'), result.get('name')) != (vm.id, vm.name)
                or epoch(result.get('archiveAfter')) - self.now() < RENEW_BEFORE):
            raise BoatError('Boat no confirmó la renovación')

    def watch_vm(self, vm, notices):
        with ExitStack() as locks:
            with lock(self.state / 'espera.lock'):
                queue = self.turns()
                if not locks.enter_context(lock(vm.path / 'lock', wait=False)):
                    return
                busy = vm.leases(self.now(), expire=True) or any(vm.accepts(turn) for turn in queue)
            self.check_identity(vm)
            info = self.sandbox_info(vm)
            state = self.observe(vm, info)
            if state in READY | STOPPED:
                try:
                    self.renew_vm(vm, info)
                except (Unavailable, BoatError, KeyError, ValueError, TypeError, OverflowError):
                    notices['ttl:' + vm.name] = 'No se pudo renovar el plazo de Boat de ' + vm.name
            if busy or state not in READY or vm.idle is None:
                return
            last = max((epoch(item['en']) for item in events(vm.journal)
                        if item['evento'] in ('soltado', 'vencido', 'encendida')), default=vm.prepared)
            if self.now() - last >= vm.idle:
                self.stop(vm, state, wait=False)

    def watch(self):
        candidates = self.candidates()
        with lock(self.state / 'vigilar.lock', wait=False) as acquired:
            if not acquired:
                return
            notices = {}
            for vm in candidates:
                try:
                    self.watch_vm(vm, notices)
                except (Unavailable, BoatError) as error:
                    with lock(self.state / 'eventos.lock'):
                        append_event(self.state / 'eventos.jsonl',
                                     vm.event('rechazo', self.now(), motivo=str(error)))
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

    def warn(self, notices):
        path = self.state / 'avisos.json'
        try:
            previous = read_json(path)
        except FileNotFoundError:
            previous = {}
        for key, message in notices.items():
            if previous.get(key) != message:
                append_line(self.state / 'avisos.log', stamp(self.now()) + ' ' + message)
                self.notify(message)
        if notices != previous:
            atomic_json(path, notices)

    def report(self, day):
        self.candidates()
        history = []
        for directory in (self.state / 'vms').glob('*/'):
            vm = self.vms.get(directory.name)
            with lock(directory / 'lock'):
                if vm is not None:
                    vm.finish_lease_change()
                history.extend(events(directory / 'eventos.jsonl'))
        now = self.now()
        history.sort(key=lambda item: epoch(item['en']))
        today = datetime.fromtimestamp(now, timezone.utc).date()
        first = today if not history else min(today, datetime.fromtimestamp(epoch(history[0]['en']), timezone.utc).date())
        days = [day] if day else [first + timedelta(days=i) for i in range((today - first).days + 1)]
        intervals = []
        powered = {}
        for item in history:
            kind = item['evento']
            vm = item['vm']
            when = epoch(item['en'])
            if kind == 'encendida':
                powered.setdefault(vm, (item['tipo'], when))
            elif kind == 'apagada' and vm in powered:
                tipo, start = powered.pop(vm)
                intervals.append((tipo, start, when))
        intervals.extend((tipo, start, now) for tipo, start in powered.values())

        writer = csv.writer(sys.stdout, delimiter='\t', lineterminator='\n')
        for day in days:
            start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp()
            end = start + 86400
            hours = Counter()
            for tipo, on, off in intervals:
                hours[tipo] += max(0, min(end, off) - max(start, on)) / 3600
            cost = sum(hours[tipo] * rate for tipo, rate in RATES.items())
            daily = [item for item in history if start <= epoch(item['en']) < end]
            starts = sum(item['evento'] == 'arranque' for item in daily)
            runs = {(item['repo'], item['corrida']) for item in daily if item['evento'] == 'concedido'}
            try:
                org = self.boat.daily_org_cost(day)
                org_hours, org_cost = ('NA', 'NA') if org is None else org
            except BoatError:
                org_hours, org_cost = 'NA', 'NA'
            writer.writerow([day.isoformat(), *(f'{hours[tipo]:.6f}' for tipo in RATES),
                             f'{cost:.6f}', starts, len(runs), org_hours, org_cost])


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
    cmd.add_argument('--esperar', type=wait_seconds, default=0)
    cmd.add_argument('--ttl', type=ttl, default=900)
    cmd = commands.add_parser('latido')
    cmd.add_argument('arriendo')
    cmd.add_argument('--ttl', type=ttl, default=900)
    commands.add_parser('soltar').add_argument('arriendo')
    commands.add_parser('estado').add_argument('--json', action='store_true')
    for name in ('encender', 'apagar'):
        cmd = commands.add_parser(name)
        target = cmd.add_mutually_exclusive_group(required=True)
        target.add_argument('nombre', nargs='?')
        target.add_argument('--todas', action='store_true')
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
            args.prioridad = 'preparar' if preparing else (args.prioridad or 'otra')
        config = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'boat-pool' / 'pool.json'
        state = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 'boat-pool'
        pool = Pool(read_json(config), state, **pool_options)
    except InvalidUse as error:
        print(str(error), file=sys.stderr)
        return 64
    except Unavailable as error:
        print(str(error), file=sys.stderr)
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
        elif args.command in ('encender', 'apagar'):
            return pool.power_command(args)
        elif args.command == 'estado':
            status = pool.status()
            if args.json:
                print(json.dumps(status, ensure_ascii=False))
            else:
                for vm in status['vms']:
                    print(f"{vm['vm']}\t{vm['estado']}\t{vm['lugares_libres']}")
                for name, gate in status['puertas'].items():
                    print(f"{name}\t{'abierta' if gate['abierta'] else 'cerrada'}")
        elif args.command == 'vigilar':
            pool.watch()
        else:
            pool.report(args.dia)
    except InvalidUse as error:
        print(str(error), file=sys.stderr)
        return 64
    except (Unavailable, BoatError) as error:
        pool.reject(str(error))
        print(str(error), file=sys.stderr)
        return 75
    except (OSError, KeyError, TypeError, ValueError, OverflowError):
        print('estado del pool inválido o no disponible', file=sys.stderr)
        return 75
    return 0
