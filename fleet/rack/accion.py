"""Cancelación por lanzador, lugares por grupo y diario durable. Python 3.9, biblioteca estándar."""
import importlib.util
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import signal  # compatibility for process-group tests
import sqlite3
import subprocess
import threading
import time

# -I excludes cwd and PYTHONPATH: load only the sibling installed with the panel.
_spec = importlib.util.spec_from_file_location('rack_desk_channel', Path(__file__).with_name('desk_action.py'))
desk_channel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(desk_channel)

SCHEMA = 'rack-action@1'
ACTION = re.compile(r'[A-Za-z0-9-]{8,64}')
REFS = {'remote-run': re.compile(r'[0-9]{8}-[0-9]{6}-[0-9a-f]{6}'),
        'jale': re.compile(r'job-[A-Za-z0-9]{8}')}
ERRORS = {
    'invalid_request': 'Petición inválida.',
    'idempotency_conflict': 'Ese ID ya pertenece a otra petición.',
    'stale_state': 'La corrida cambió desde que la viste. Actualiza antes de decidir.',
    'not_found': 'No se encontró la corrida.',
    'unsupported': 'Esta corrida no confirma la cancelación.',
    'disabled': 'Las acciones están apagadas localmente.',
    'deadline': 'No se lanzó una cancelación nueva: se agotó el plazo. Consulta el mismo recibo.',
    'manually_closed': 'Recibo cerrado a mano tras comprobar la corrida en Pueue. No acredita una cancelación.',
    'forbidden': 'No se pudo autorizar la petición.',
}
OPEN = ('cancelling', 'unknown', 'cancel_unconfirmed')
DESK_ARGV = ['/usr/bin/ssh', '-F', '/dev/null', '-T',
             '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
             '-o', 'IdentityAgent=none', '-o', 'ConnectTimeout=10',
             '-o', 'StrictHostKeyChecking=yes',
             '-o', 'UserKnownHostsFile=~/.ssh/known_hosts',
             '-o', 'ClearAllForwardings=yes', '-o', 'PermitLocalCommand=no',
             '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
             '-i', '~/.ssh/id_ed25519_pao_rack_accion', 'Felix@felix-desktop']
DESK_TIMEOUT = 40
# Local cancel: the previous 10 s plus up to 2 s for the origin probe (desk_action.send).
LOCAL_CANCEL_TIMEOUT = 12
# Reserve for the journal, backed by an independent HTTP wait deadline.
RECEIPT_RESERVE = 1
MIN_TRANSPORT = 1
JOURNAL_WAIT = .5

CONTROLLERS = {'remote-run': '~/.local/lib/remote-run/run-control.py',
               'jale': '~/.local/lib/jale/pueue-control.py'}
# Autoridad de cuotas del host (fleet/pueue-desk/control.py). Ruta fija por host;
# el panel nunca la recibe del navegador.
HOST_CONTROL = {'mini': '~/.local/lib/pao-pueue-mini/control.py'}
HOST_OPS = ('slots', 'undo')
HOST_OPEN = ('unknown', 'applying', 'partial')
HOST_TIMEOUT = 10
GROUP = re.compile(r'[A-Za-z0-9_-]{1,32}')
DIGEST = re.compile(r'[0-9a-f]{64}')
HOST_ERRORS = {
    'invalid_request': ERRORS['invalid_request'],
    'idempotency_conflict': ERRORS['idempotency_conflict'],
    'disabled': ERRORS['disabled'],
    'deadline': 'No se envió el cambio: se agotó el plazo. Consulta el mismo recibo.',
    'stale_state': 'Los lugares cambiaron desde que los viste. Actualiza antes de decidir.',
    'drift': 'Pueue no tiene las cuotas guardadas; la guardia las está reponiendo.',
    'busy': 'Hay un cambio de lugares en curso en este host; espera su resultado.',
    'min': 'Cada grupo necesita al menos 1 lugar.',
    'max': 'Un grupo admite como máximo 8 lugares.',
    'capacity': 'Se llenó el tope de lugares del host. Quita un lugar a otro grupo para añadirlo aquí.',
    'undo_conflict': 'Ya no se puede deshacer: los lugares cambiaron después.',
    'not_adopted': 'Falta actualizar el control de este host.',
    'unsupported': 'Falta actualizar el control de este host.',
    'unreadable': 'El estado de cuotas del host no se puede leer; no se abre ni aumenta nada.',
    'unavailable': 'No se pudo leer Pueue en el host; no se cambió nada.',
}


def request_budget(host):
    return 44 if host == 'desk' else 14


def unique(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError('Clave repetida')
        data[key] = value
    return data


def decode(raw):
    if len(raw) > 16384:
        raise ValueError()
    return json.loads(raw, object_pairs_hook=unique)


def valid_ref(launcher, ref):
    return (type(launcher) is str and launcher in REFS and type(ref) is str
            and REFS[launcher].fullmatch(ref) is not None)


def expected(value):
    return (type(value) is dict and set(value) == {'task_id', 'state'}
            and type(value['task_id']) is int and value['task_id'] >= 0
            and value['state'] in ('Queued', 'Running'))


def slot_vector(value):
    return (type(value) is dict and 0 < len(value) <= 16
            and all(type(k) is str and GROUP.fullmatch(k) and type(v) is int and 0 <= v <= 1000
                    for k, v in value.items()))


def validate_host(request):
    """Closed schema per host operation; booleans are never integers."""
    op = request['op']
    keys = {'schema', 'op', 'host', 'action_id', 'dry_run', 'expected', 'change' if op == 'slots' else 'undo_of'}
    expected = request.get('expected')
    if (set(request) != keys or request['schema'] != SCHEMA
            or type(request['host']) is not str or not re.fullmatch(r'[A-Za-z0-9_-]+', request['host'])
            or type(request['action_id']) is not str or not ACTION.fullmatch(request['action_id'])
            or type(request['dry_run']) is not bool
            or type(expected) is not dict or set(expected) != {'revision', 'policy_hash', 'slots'}
            or type(expected['revision']) is not int or expected['revision'] < 0
            or type(expected['policy_hash']) is not str or not DIGEST.fullmatch(expected['policy_hash'])
            or not slot_vector(expected['slots'])):
        raise ValueError()
    if op == 'slots':
        change = request['change']
        if (type(change) is not dict or set(change) != {'group', 'delta'}
                or type(change['group']) is not str or change['group'] not in expected['slots']
                or type(change['delta']) is not int or change['delta'] not in (-1, 1)):
            raise ValueError()
    elif (type(request['undo_of']) is not str or not ACTION.fullmatch(request['undo_of'])
          or request['undo_of'] == request['action_id']):
        raise ValueError()
    return request


def validate(request):
    if type(request) is dict and request.get('op') in HOST_OPS:
        return validate_host(request)
    keys = {'schema', 'op', 'host', 'launcher', 'ref', 'expected'}
    if type(request) is not dict or request.get('op') not in ('cancel', 'capabilities'):
        raise ValueError()
    if request['op'] == 'cancel':
        keys.add('action_id')
        if type(request.get('action_id')) is not str or not ACTION.fullmatch(request['action_id']):
            raise ValueError()
    if (set(request) != keys or request['schema'] != SCHEMA
            or type(request['host']) is not str or not re.fullmatch(r'[A-Za-z0-9_-]+', request['host'])
            or not valid_ref(request['launcher'], request['ref']) or not expected(request['expected'])):
        raise ValueError()
    return request


def result(request, status, code=None, exit=None, reason=None):
    return dict(schema=SCHEMA, action_id=request.get('action_id'), op=request.get('op'),
                host=request.get('host'), launcher=request.get('launcher'), ref=request.get('ref'),
                status=status, result=None if exit is None else {'exit': exit},
                error=None if code is None else dict(code=code, message=reason or ERRORS[code]))


def host_result(request, status, code=None, revision=None, value=None):
    return dict(schema=SCHEMA, action_id=request.get('action_id'), op=request.get('op'),
                host=request.get('host'), status=status, revision=revision, result=value,
                error=None if code is None else dict(code=code, message=HOST_ERRORS[code]))


def host_http(response):
    status = response['status']
    if status == 'rejected':
        code = response['error']['code']
        return 403 if code == 'disabled' else 400 if code == 'invalid_request' else 409
    return 409 if status == 'conflict' else 202 if status == 'applying' else 200


def host_view(value):
    """Whitelist of the host's capabilities answer for the snapshot; anything else is absent."""
    try:
        caps, control = value['capabilities'], value['control']
        if (value['schema'] != 'rack-host-result@1' or value['status'] != 'ok'
                or type(caps['slots']) is not bool or type(caps['undo']) is not bool
                or type(control['cap']) is not int or not 1 <= control['cap'] <= 64
                or control['min'] != 1 or control['max'] != 8
                or control['reason'] is not None and control['reason'] not in HOST_ERRORS):
            return None
        revision, digest = control['revision'], control['policy_hash']
        desired, observed, pending = control['desired'], control['observed'], control['pending']
        if (revision is not None and (type(revision) is not int or revision < 0)
                or digest is not None and (type(digest) is not str or not DIGEST.fullmatch(digest))
                or desired is not None and not slot_vector(desired)
                or observed is not None and not slot_vector(observed)
                or pending is not None and (type(pending) is not dict or set(pending) != {'action_id', 'phase'}
                    or type(pending['action_id']) is not str or not ACTION.fullmatch(pending['action_id'])
                    or pending['phase'] not in ('applying', 'partial'))):
            return None
        slots = caps['slots'] and control['reason'] is None and None not in (revision, digest, desired, observed)
        return dict(slots=slots, undo=caps['undo'] and slots, revision=revision, policy_hash=digest,
                    cap=control['cap'], min=1, max=8, desired=desired, observed=observed,
                    pending=pending, reason=None if control['reason'] is None else HOST_ERRORS[control['reason']])
    except (KeyError, TypeError):
        return None


def sanitize_host(request, response):
    """Only fixed codes and integer vectors reach the browser; never controller text."""
    if (type(response) is not dict or response.get('schema') != 'rack-host-result@1'
            or response.get('action_id') != request['action_id'] or response.get('op') != request['op']):
        raise ValueError()
    status, error, revision, value = (response.get(k) for k in ('status', 'error', 'revision', 'result'))
    if status not in ('dry_run', 'applied', 'partial', 'applying', 'rejected', 'conflict', 'unknown'):
        raise ValueError()
    code = None
    if status in ('rejected', 'conflict'):
        code = error.get('code') if type(error) is dict else None
        if code not in HOST_ERRORS:
            raise ValueError()
    elif error is not None:
        raise ValueError()
    if revision is not None and (type(revision) is not int or revision < 0):
        raise ValueError()
    clean = None
    if value is not None:
        if type(value) is not dict or not set(value) <= {'before', 'desired', 'observed', 'plan', 'undo_of'}:
            raise ValueError()
        clean = {}
        for key in ('before', 'desired', 'observed'):
            if key in value:
                if value[key] is not None and not slot_vector(value[key]):
                    raise ValueError()
                clean[key] = value[key]
        if 'plan' in value:
            plan = value['plan']
            if (type(plan) is not dict or set(plan) != {'decreases', 'increases'}
                    or any(type(v) is not list or not all(type(g) is str and GROUP.fullmatch(g) for g in v)
                           for v in plan.values())):
                raise ValueError()
            clean['plan'] = plan
        if 'undo_of' in value:
            if type(value['undo_of']) is not str or not ACTION.fullmatch(value['undo_of']):
                raise ValueError()
            clean['undo_of'] = value['undo_of']
    return host_result(request, status, code, revision, clean)


def control_data(value, task):
    """Whitelist the snapshot's cancellation identity; never use activity/metadata."""
    if (type(value) is dict and set(value) == {'launcher', 'ref', 'expected', 'reason'}
            and (valid_ref(value['launcher'], value['ref']) or value['launcher'] is None and value['ref'] is None)
            and (value['expected'] is None or expected(value['expected']))
            and (value['expected'] is None or value['expected'] == dict(task_id=task['id'], state=task['state']))
            and (value['reason'] is None or type(value['reason']) is str and len(value['reason']) <= 240)):
        return value
    return None


def action_config(value):
    """Closed controller table and exact SSH argv; no configurable commands."""
    if value is None:
        return dict(transport='disabled', reason='Este host no tiene canal de escritura.', controllers={})
    base = ({'transport', 'reason', 'controllers', 'argv'} if type(value) is dict and value.get('transport') == 'ssh'
            else {'transport', 'reason', 'controllers'})
    if (type(value) is not dict or set(value) - {'host_control'} != base
            or 'host_control' in value and (value['transport'] != 'local'
                                            or value['host_control'] not in HOST_CONTROL.values())
            or value['transport'] not in ('local', 'ssh', 'disabled')
            or value['transport'] == 'ssh' and value['argv'] != DESK_ARGV
            or type(value['controllers']) is not dict
            or any(k not in CONTROLLERS or v != CONTROLLERS[k] for k, v in value['controllers'].items())
            or value['reason'] is not None and (type(value['reason']) is not str
                or not 0 < len(value['reason']) <= 240
                or any(ord(c) < 32 or ord(c) == 127 for c in value['reason']))
            or value['transport'] in ('local', 'ssh') and (not value['controllers'] or value['reason'] is not None)
            or value['transport'] == 'disabled' and not value['reason']):
        raise ValueError('Configuración de acciones inválida')
    return value


def transport_reason(host, launcher):
    config = action_config(host.get('actions'))
    if config['transport'] == 'disabled':
        return config['reason']
    if config['transport'] == 'ssh' and (host.get('id') != 'desk' or host.get('transport') != 'ssh'):
        return 'El canal SSH de acciones sólo actúa sobre el desk.'
    if config['transport'] == 'local' and host.get('transport') != 'local':
        return 'El canal local sólo actúa sobre el host donde corre el panel.'
    if launcher not in config['controllers']:
        return 'Este lanzador no tiene controlador configurado en este host.'
    return None


def host_reason(host):
    """Only the local channel next to the panel reaches its own host's control."""
    config = action_config(host.get('actions'))
    if config['transport'] == 'disabled':
        return config['reason']
    if (config['transport'] != 'local' or host.get('transport') != 'local'
            or config.get('host_control') != HOST_CONTROL.get(host.get('id'))):
        return HOST_ERRORS['unsupported']
    return None


class Executor:
    """One transport seam, selected by host configuration; no caller-supplied argv."""
    def __init__(self, runner=subprocess.Popen, home=None, timeout=None):
        self.runner, self.timeout = runner, timeout
        self.home = Path(home or pwd.getpwuid(os.getuid()).pw_dir)

    def __call__(self, host, launcher, request, timeout=None):
        if transport_reason(host, launcher):
            raise ValueError('Transporte deshabilitado')
        config = action_config(host.get('actions'))
        remote = config['transport'] == 'ssh'
        limit = (self.timeout if self.timeout is not None else DESK_TIMEOUT if remote
                 else LOCAL_CANCEL_TIMEOUT if request.get('op') == 'cancel' else 10)
        timeout = limit if timeout is None else min(limit, timeout)
        env = {'HOME': str(self.home), 'PATH': '/usr/bin:/bin:/opt/homebrew/bin', 'TMPDIR': '/tmp'}
        if remote:
            # The envelope never carries origin: the desk dispatcher negotiates it locally.
            argv = list(config['argv'])
            request = dict(schema='rack-desk-action@1', launcher=launcher, request=request)
            code, response = desk_channel.exchange(argv, request, self.runner, env, timeout)
        else:
            controller = self.home / config['controllers'][launcher][2:]
            argv = ['/usr/bin/python3', '-I', str(controller)]
            code, response = desk_channel.send(argv, request, self.runner, env, timeout)
        if code not in ((0,) if remote else (0, 2)):
            raise ValueError('Respuesta inválida')
        return response

    def host(self, host, request, timeout):
        """Fixed argv to this host's own control; the request only travels on stdin."""
        if host_reason(host):
            raise ValueError('Transporte deshabilitado')
        path = self.home / action_config(host['actions'])['host_control'][2:]
        argv = ['/usr/bin/python3', '-I', str(path), 'rack', '--host', host['id']]
        env = {'HOME': str(self.home), 'PATH': '/usr/bin:/bin:/opt/homebrew/bin', 'TMPDIR': '/tmp'}
        code, response = desk_channel.exchange(argv, request, self.runner, env, min(HOST_TIMEOUT, timeout))
        if code != 0:
            raise ValueError('Respuesta inválida')
        return response


def sanitize(request, response, op):
    """Do not forward controller error text, paths or extra fields to the browser."""
    if (type(response) is not dict or response.get('schema') != 'rack-cancel-result@1'
            or response.get('ref') != request['ref']):
        raise ValueError()
    if op == 'capabilities':
        caps = response.get('capabilities')
        if (type(caps) is not dict or type(caps.get('queued')) is not bool
                or type(caps.get('running')) is not bool):
            raise ValueError()
        answer = result(request, 'capabilities')
        answer['capabilities'] = dict(queued=caps['queued'], running=caps['running'],
            reason=None if caps['queued'] and caps['running'] else ERRORS['unsupported'])
        return answer
    if response.get('action_id') != request['action_id']:
        raise ValueError()
    status = response.get('status')
    if status not in (*OPEN, 'cancelled', 'already_finished', 'rejected'):
        raise ValueError()
    code, exit = None, None
    if status == 'rejected':
        error = response.get('error')
        code = error.get('code') if type(error) is dict else None
        if code not in ('stale_state', 'not_found', 'unsupported', 'idempotency_conflict', 'invalid_request'):
            raise ValueError()
    if status in ('cancelled', 'already_finished'):
        value = response.get('result')
        exit = value.get('exit') if type(value) is dict else None
        if type(exit) is not int or not 0 <= exit <= 255 or status == 'cancelled' and exit != 130:
            raise ValueError()
    return result(request, status, code, exit)


class Actions:
    def __init__(self, hosts, directory, read, executor=None, clock=time.time, timer=time.monotonic):
        self.hosts = {h['id']: h for h in hosts}
        self.directory, self.read, self.executor, self.clock = Path(directory), read, executor or Executor(), clock
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'actions.sqlite3'
        self.lock = threading.RLock()
        self.timer = timer
        self.inflight = {}
        # Independent of the journal lock: HTTP must not wait on filesystem I/O.
        self.http_lock = threading.Lock()
        self.identities = {}
        self.workers = set()
        # The authoritative index is never rotated. Full SQLite commit precedes transport.
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS actions (action_id TEXT PRIMARY KEY COLLATE BINARY, '
                       'digest TEXT NOT NULL, request TEXT NOT NULL, response TEXT NOT NULL, '
                       'created REAL NOT NULL, updated REAL NOT NULL)')
            if 'last_status' not in {row[1] for row in db.execute('PRAGMA table_info(actions)')}:
                db.execute('ALTER TABLE actions ADD COLUMN last_status REAL')
            self.identities = {key: json.loads(body) for key, body in
                               db.execute('SELECT action_id,request FROM actions')}
        self.path.chmod(0o600)

    def wait(self, event, timeout):
        return event.wait(timeout)

    def respond(self, operation, value, started=None):
        """Bound HTTP waiting, including a blocked fsync or transport spawn.

        The worker retains its original deadline and action reservation. A late
        intent commit must still pass call()'s budget check before any effect.
        """
        started = self.timer() if started is None else started
        if operation == 'post':
            validate(value)
            identity = dict(value)
            if value['op'] == 'cancel' or value['op'] in HOST_OPS and not value['dry_run']:
                with self.http_lock:
                    self.identities.setdefault(value['action_id'], identity)
        else:
            if not ACTION.fullmatch(value):
                raise ValueError()
            with self.http_lock:
                identity = self.identities.get(value, dict(action_id=value, op='cancel'))
        # An unknown GET uses the shorter budget until its journal identity is known.
        deadline = started + request_budget(identity.get('host'))
        done, answers, errors = threading.Event(), [], []
        def run():
            try:
                answers.append(getattr(self, operation)(value, started=started))
            except Exception as error:
                errors.append(error)
            finally:
                with self.http_lock:
                    self.workers.discard(threading.current_thread())
                done.set()
        worker = threading.Thread(target=run, name='rack-action', daemon=True)
        with self.http_lock:
            self.workers.add(worker)
        worker.start()
        if not self.wait(done, max(0, deadline - self.timer())):
            return 200, (host_result(identity, 'unknown') if identity.get('op') in HOST_OPS
                         else result(identity, 'unknown'))
        if errors:
            raise errors[0]
        return answers[0]

    @contextmanager
    def connect(self, deadline=None):
        wait = JOURNAL_WAIT if deadline is None else min(JOURNAL_WAIT, max(0, deadline - self.timer()))
        db = sqlite3.connect(str(self.path), timeout=wait)
        if deadline is not None:
            db.set_progress_handler(lambda: int(self.timer() >= deadline), 100)
        try:
            db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def journal(self, deadline):
        # Admission is bounded even if another request or the filesystem stalls.
        wait = min(JOURNAL_WAIT, max(0, deadline - self.timer()))
        if not self.lock.acquire(timeout=wait):
            raise TimeoutError()
        try:
            if self.timer() >= deadline:
                raise TimeoutError()
            yield
        finally:
            self.lock.release()

    def active(self, action_id):
        done = self.inflight.get(action_id)
        return done is not None and not done.is_set()

    def disabled(self):
        return (self.directory / 'actions.disabled').exists()

    def eligible(self, request):
        host = self.hosts.get(request['host'])
        if host is None:
            return 'Este host no tiene canal de escritura.'
        reason = transport_reason(host, request['launcher'])
        if reason:
            return reason
        payload = self.read()
        current = next((h for h in payload['hosts'] if h['id'] == request['host']), None)
        if not current or not current['fresh'] or current.get('age') is None or current['age'] > 45 or self.clock() - payload['now'] > 45:
            return 'Actualiza la instantánea antes de cancelar.'
        task = next((t for t in (current.get('snapshot') or {}).get('tasks', [])
                     if t['id'] == request['expected']['task_id']), None)
        control = control_data(task.get('control'), task) if task else None
        if not control or control['reason'] or any(control[k] != request[k] for k in ('launcher', 'ref', 'expected')):
            return control['reason'] if control and control['reason'] else ERRORS['stale_state']
        return None

    def call(self, request, op, deadline):
        wire = dict(schema='rack-cancel@1', op=op, ref=request['ref'])
        if op != 'capabilities':
            wire['action_id'] = request['action_id']
        if op == 'cancel':
            wire['expected'] = request['expected']
        remaining = deadline - self.timer() - RECEIPT_RESERVE
        if remaining < MIN_TRANSPORT:
            return result(request, 'rejected', 'deadline') if op != 'status' else result(request, 'unknown')
        timeout = min(DESK_TIMEOUT if request['host'] == 'desk'
                      else LOCAL_CANCEL_TIMEOUT if op == 'cancel' else 10, remaining)
        try:
            answer = sanitize(request, self.executor(self.hosts[request['host']], request['launcher'], wire,
                                                     timeout=timeout), op)
            return answer if self.timer() <= deadline - RECEIPT_RESERVE else result(request, 'unknown')
        except Exception:
            # This is the controller boundary, after client validation. Any failure
            # reading/decoding/validating its answer leaves the effect uncertain.
            return result(request, 'unknown')

    def save(self, request, response, deadline=None):
        with self.connect(deadline) as db:
            # Share the SQLite writer lock with the manual CLI. Never overwrite
            # its terminal result with a transport that was already in flight.
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT response FROM actions WHERE action_id=?',
                             (request['action_id'],)).fetchone()
            current = json.loads(row[0]) if row else None
            if current and (current.get('error') or {}).get('code') == 'manually_closed':
                return current
            db.execute('UPDATE actions SET response=?,updated=? WHERE action_id=?',
                       (json.dumps(response), self.clock(), request['action_id']))
        return response

    def close_receipt(self, action_id):
        if not ACTION.fullmatch(action_id):
            raise ValueError('ID inválido.')
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT request,response FROM actions WHERE action_id=?', (action_id,)).fetchone()
            if row is None:
                raise ValueError('No existe ese recibo.')
            request, response = map(json.loads, row)
            if response['status'] not in OPEN:
                if (response.get('error') or {}).get('code') == 'manually_closed':
                    return response  # Repeating this exact administrative operation is harmless.
                raise ValueError('El recibo ya es terminal.')
            response = result(request, 'rejected', 'manually_closed')
            db.execute('UPDATE actions SET response=?,updated=? WHERE action_id=?',
                       (json.dumps(response), self.clock(), action_id))
        return response

    def pending_reason(self, db, request):
        for body, answer in db.execute('SELECT request,response FROM actions WHERE action_id != ?',
                                       (request.get('action_id', ''),)):
            previous, response = json.loads(body), json.loads(answer)
            if response['status'] in OPEN and all(previous.get(k) == request[k] for k in ('host', 'launcher', 'ref')):
                return 'Esta corrida tiene una cancelación pendiente (' + previous['action_id'] + '); consulta su recibo.'
        return None

    def complete(self, request, op, done, deadline):
        host = request['op'] in HOST_OPS
        unknown = host_result if host else result
        try:
            response = self.host_call(request, op, deadline) if host else self.call(request, op, deadline)
            if op == 'status' and response['status'] == 'rejected' and not host:
                response = result(request, 'unknown')
            try:
                with self.journal(deadline):
                    response = self.save(request, response, deadline)
                    self.inflight.pop(request['action_id'], None)
            except TimeoutError:
                # The committed intent still owns this ID and blocks a second cancel.
                return 200, unknown(request, 'unknown')
            return (host_http(response) if host else 200), response
        finally:
            # Event.set needs no journal lock. A failed commit can be reconciled by GET.
            done.set()

    def host_eligible(self, request):
        """Better message before transport; the host validates everything again."""
        host = self.hosts.get(request['host'])
        if host is None:
            return 'unsupported'
        if host_reason(host):
            return 'unsupported'
        payload = self.read()
        current = next((h for h in payload['hosts'] if h['id'] == request['host']), None)
        if (not current or not current['fresh'] or current.get('age') is None or current['age'] > 45
                or self.clock() - payload['now'] > 45):
            return 'stale_state'
        control = (current.get('snapshot') or {}).get('control')
        if not control or not control.get('slots'):
            return 'unsupported'
        expected = request['expected']
        if (control['revision'] != expected['revision'] or control['policy_hash'] != expected['policy_hash']
                or control['desired'] != expected['slots'] or control['observed'] != expected['slots']):
            return 'stale_state'
        return None

    def host_pending(self, db, request):
        for body, answer in db.execute('SELECT request,response FROM actions WHERE action_id != ?',
                                       (request['action_id'],)):
            previous, response = json.loads(body), json.loads(answer)
            if previous.get('op') in HOST_OPS and previous['host'] == request['host'] and response['status'] in HOST_OPEN:
                return True
        return False

    def host_call(self, request, op, deadline):
        """One host exchange. status that finds no intent resends the same body once:
        the host binds the ID before any effect, so it applies at most once."""
        wire = dict(schema='rack-host-control@1', op=request['op'], action_id=request['action_id'],
                    dry_run=request['dry_run'], expected=request['expected'])
        wire.update({'change': request['change']} if request['op'] == 'slots' else {'undo_of': request['undo_of']})
        for attempt in (('status', wire) if op == 'status' else (wire,)):
            remaining = deadline - self.timer() - RECEIPT_RESERVE
            if remaining < MIN_TRANSPORT:
                return host_result(request, 'unknown') if op == 'status' else host_result(request, 'rejected', 'deadline')
            if attempt == 'status':
                attempt = dict(schema='rack-host-control@1', op='status', action_id=request['action_id'])
            try:
                answer = self.executor.host(self.hosts[request['host']], attempt, timeout=remaining)
                if attempt['op'] == 'status':
                    answer = dict(answer, op=request['op']) if type(answer) is dict else answer
                    if type(answer) is dict and answer.get('status') == 'rejected' and \
                            (answer.get('error') or {}).get('code') == 'not_found' and answer.get('action_id') == request['action_id']:
                        if self.disabled():
                            return host_result(request, 'unknown')
                        continue
                response = sanitize_host(request, answer)
            except Exception:
                return host_result(request, 'unknown')
            return response if self.timer() <= deadline - RECEIPT_RESERVE else host_result(request, 'unknown')
        return host_result(request, 'unknown')

    def post_host(self, request, started):
        deadline = started + request_budget(request['host'])
        if request['dry_run']:
            # Dry run: no journal row, no reservation; the host does not write either.
            code = self.host_eligible(request)
            if code:
                response = host_result(request, 'conflict' if code == 'stale_state' else 'rejected', code)
                return host_http(response), response
            response = self.host_call(request, 'dry_run', deadline)
            return host_http(response), response
        body = json.dumps(request, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
        digest = hashlib.sha256(body.encode()).hexdigest()
        try:
            with self.journal(deadline - RECEIPT_RESERVE):
                with self.connect(deadline) as db:
                    db.execute('BEGIN IMMEDIATE')
                    row = db.execute('SELECT digest,response FROM actions WHERE action_id=?', (request['action_id'],)).fetchone()
                    if row:
                        if row[0] != digest:
                            return 409, host_result(request, 'rejected', 'idempotency_conflict')
                        stored = json.loads(row[1])
                        return host_http(stored), stored
                    db.execute('INSERT INTO actions (action_id,digest,request,response,created,updated) VALUES (?,?,?,?,?,?)',
                               (request['action_id'], digest, body, json.dumps(host_result(request, 'unknown')),
                                self.clock(), self.clock()))
                    # Rejections also bind the ID. No transport before this commit.
                    code = ('disabled' if self.disabled() else 'busy' if self.host_pending(db, request)
                            else self.host_eligible(request))
                    if code:
                        rejected = host_result(request, 'conflict' if code == 'stale_state' else 'rejected', code)
                        db.execute('UPDATE actions SET response=? WHERE action_id=?',
                                   (json.dumps(rejected), request['action_id']))
                        return host_http(rejected), rejected
                done = threading.Event()
                self.inflight[request['action_id']] = done
        except TimeoutError:
            return 503, host_result(request, 'unknown', 'deadline')
        return self.complete(request, request['op'], done, deadline)

    def post(self, request, started=None):
        started = self.timer() if started is None else started
        validate(request)
        if request['op'] in HOST_OPS:
            return self.post_host(request, started)
        if request['op'] == 'cancel':
            with self.http_lock:
                self.identities.setdefault(request['action_id'], dict(request))
        deadline = started + request_budget(request['host'])
        try:
            with self.journal(deadline - RECEIPT_RESERVE):
                if request['op'] == 'capabilities':
                    with self.connect(deadline) as db:
                        reason = ERRORS['disabled'] if self.disabled() else self.pending_reason(db, request) or self.eligible(request)
                    if reason:
                        return 200, result(request, 'rejected', 'unsupported', reason=reason)
                else:
                    body = json.dumps(request, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
                    digest = hashlib.sha256(body.encode()).hexdigest()
                    with self.connect(deadline) as db:
                        db.execute('BEGIN IMMEDIATE')
                        row = db.execute('SELECT digest,response FROM actions WHERE action_id=?', (request['action_id'],)).fetchone()
                        if row:
                            if row[0] != digest:
                                return 409, result(request, 'rejected', 'idempotency_conflict')
                            return 200, json.loads(row[1])
                        intent = result(request, 'unknown')
                        db.execute('INSERT INTO actions (action_id,digest,request,response,created,updated) VALUES (?,?,?,?,?,?)',
                                   (request['action_id'], digest, body, json.dumps(intent), self.clock(), self.clock()))
                        # Rejections also bind the ID. No transport before this commit.
                        disabled = self.disabled()
                        pending = self.pending_reason(db, request)
                        reason = ERRORS['disabled'] if disabled else pending or self.eligible(request)
                        if reason:
                            code = 'disabled' if disabled else 'idempotency_conflict' if pending else 'stale_state' if reason == ERRORS['stale_state'] else 'unsupported'
                            rejected = result(request, 'rejected', code, reason=reason)
                            db.execute('UPDATE actions SET response=? WHERE action_id=?',
                                       (json.dumps(rejected), request['action_id']))
                            return 403 if disabled else 409, rejected
                    done = threading.Event()
                    self.inflight[request['action_id']] = done
        except TimeoutError:
            # No new transport; the existing ID may already have an effect/receipt.
            return 503, result(request, 'unknown', 'deadline')
        if request['op'] == 'capabilities':
            return 200, self.call(request, 'capabilities', deadline)
        return self.complete(request, 'cancel', done, deadline)

    def get(self, action_id, started=None):
        started = self.timer() if started is None else started
        if not ACTION.fullmatch(action_id):
            raise ValueError()
        identity = dict(action_id=action_id, op='cancel')
        try:
            # Host comes from the journal, never from a GET parameter. Admission
            # itself is capped at JOURNAL_WAIT, even before that host is known.
            with self.journal(started + request_budget('desk') - RECEIPT_RESERVE):
                with self.connect(started + request_budget('desk')) as db:
                    db.execute('BEGIN IMMEDIATE')
                    row = db.execute('SELECT request,response,created,last_status FROM actions WHERE action_id=?', (action_id,)).fetchone()
                    if row is None:
                        return 404, result(identity, 'unknown')
                    request, response = map(json.loads, row[:2])
                    if response['status'] not in (HOST_OPEN if request['op'] in HOST_OPS else OPEN) or self.active(action_id):
                        return 200, response
                    if request['host'] == 'desk':
                        now = self.clock()
                        interval = 2 if now - row[2] < 60 else 30
                        if row[3] is not None and now - row[3] < interval:
                            return 200, response
                        db.execute('UPDATE actions SET last_status=? WHERE action_id=?', (now, action_id))
                done = threading.Event()
                self.inflight[action_id] = done
        except TimeoutError:
            return 503, result(identity, 'unknown')
        return self.complete(request, 'status', done, started + request_budget(request['host']))


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Administración local del diario del rack.')
    parser.add_argument('--state-dir', type=Path, default=Path.home() / '.local/state/pao/rack')
    commands = parser.add_subparsers(dest='command', required=True)
    close = commands.add_parser('close-receipt', help='Cerrar un recibo tras comprobar la corrida en Pueue.')
    close.add_argument('action_id')
    args = parser.parse_args()
    if not (args.state_dir / 'actions.sqlite3').is_file():
        parser.exit(2, 'No existe el diario de acciones.\n')
    try:
        actions = Actions([], args.state_dir, lambda: None)
        response = actions.close_receipt(args.action_id)
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.exit(2, str(error) + '\n')
    print(json.dumps(response, ensure_ascii=False))


if __name__ == '__main__':
    main()
