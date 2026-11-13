"""Cancelación por lanzador y diario durable. Python 3.9, biblioteca estándar."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time

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
    'forbidden': 'No se pudo autorizar la petición.',
}
OPEN = ('cancelling', 'unknown', 'cancel_unconfirmed')
CONTROLLERS = {'remote-run': '~/.local/lib/remote-run/run-control.py',
               'jale': '~/.local/lib/jale/pueue-control.py'}


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


def validate(request):
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
    """Only administrator-selected, contract-fixed controller paths; never argv."""
    if value is None:
        return dict(transport='disabled', reason='Este host no tiene canal de escritura.', controllers={})
    if (type(value) is not dict or set(value) != {'transport', 'reason', 'controllers'}
            or value['transport'] not in ('local', 'disabled')
            or type(value['controllers']) is not dict
            or any(k not in CONTROLLERS or v != CONTROLLERS[k] for k, v in value['controllers'].items())
            or value['reason'] is not None and (type(value['reason']) is not str
                or not 0 < len(value['reason']) <= 240
                or any(ord(c) < 32 or ord(c) == 127 for c in value['reason']))
            or value['transport'] == 'local' and (not value['controllers'] or value['reason'] is not None)
            or value['transport'] == 'disabled' and not value['reason']):
        raise ValueError('Configuración de acciones inválida')
    return value


def transport_reason(host, launcher):
    config = action_config(host.get('actions'))
    if config['transport'] != 'local':
        return config['reason']
    if host.get('transport') != 'local':
        return 'El canal local sólo actúa sobre el host donde corre el panel.'
    if launcher not in config['controllers']:
        return 'Este lanzador no tiene controlador configurado en este host.'
    return None


class Executor:
    """One transport seam, selected by host configuration; no caller-supplied argv."""
    def __init__(self, runner=subprocess.Popen, home=None, timeout=10):
        self.runner, self.timeout = runner, timeout
        self.home = Path(home or pwd.getpwuid(os.getuid()).pw_dir)

    def __call__(self, host, launcher, request):
        if transport_reason(host, launcher):
            raise ValueError('Transporte deshabilitado')
        config = action_config(host.get('actions'))
        controller = self.home / config['controllers'][launcher][2:]
        # A file bounds the public response and avoids an unbounded stdout pipe.
        with tempfile.TemporaryFile() as output:
            process = self.runner(['/usr/bin/python3', '-I', str(controller)],
                stdin=subprocess.PIPE, stdout=output, stderr=subprocess.DEVNULL,
                env={'HOME': str(self.home), 'PATH': '/usr/bin:/bin:/opt/homebrew/bin', 'TMPDIR': '/tmp'},
                cwd='/', start_new_session=True)
            try:
                process.communicate(json.dumps(request).encode(), timeout=self.timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                raise
            output.seek(0)
            response = decode(output.read(16385))
            if process.returncode not in (0, 2):
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
    def __init__(self, hosts, directory, read, executor=None, clock=time.time):
        self.hosts = {h['id']: h for h in hosts}
        self.directory, self.read, self.executor, self.clock = Path(directory), read, executor or Executor(), clock
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'actions.sqlite3'
        self.lock = threading.RLock()
        # The authoritative index is never rotated. Full SQLite commit precedes transport.
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS actions (action_id TEXT PRIMARY KEY COLLATE BINARY, '
                       'digest TEXT NOT NULL, request TEXT NOT NULL, response TEXT NOT NULL, '
                       'created REAL NOT NULL, updated REAL NOT NULL)')
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=5)
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

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
        if task and task['group'] in ('otros', 'pg5434'):
            return 'Este grupo no ofrece cancelación desde el rack.'
        control = control_data(task.get('control'), task) if task else None
        if not control or control['reason'] or any(control[k] != request[k] for k in ('launcher', 'ref', 'expected')):
            return control['reason'] if control and control['reason'] else ERRORS['stale_state']
        return None

    def call(self, request, op):
        wire = dict(schema='rack-cancel@1', op=op, ref=request['ref'])
        if op != 'capabilities':
            wire['action_id'] = request['action_id']
        if op == 'cancel':
            wire['expected'] = request['expected']
        try:
            return sanitize(request, self.executor(self.hosts[request['host']], request['launcher'], wire), op)
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
            return result(request, 'unknown')

    def save(self, request, response):
        with self.connect() as db:
            db.execute('UPDATE actions SET response=?,updated=? WHERE action_id=?',
                       (json.dumps(response), self.clock(), request['action_id']))

    def post(self, request):
        validate(request)
        with self.lock:
            if request['op'] == 'capabilities':
                reason = ERRORS['disabled'] if self.disabled() else self.eligible(request)
                if reason:
                    return 200, result(request, 'rejected', 'unsupported', reason=reason)
                return 200, self.call(request, 'capabilities')
            body = json.dumps(request, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
            digest = hashlib.sha256(body.encode()).hexdigest()
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT digest,response FROM actions WHERE action_id=?', (request['action_id'],)).fetchone()
                if row:
                    if row[0] != digest:
                        return 409, result(request, 'rejected', 'idempotency_conflict')
                    return 200, json.loads(row[1])
                intent = result(request, 'unknown')
                db.execute('INSERT INTO actions VALUES (?,?,?,?,?,?)',
                           (request['action_id'], digest, body, json.dumps(intent), self.clock(), self.clock()))
                # Rejections are bound too: a valid authorized ID is never reusable
                # with another body, even if this first attempt had no effect.
                disabled = self.disabled()
                reason = ERRORS['disabled'] if disabled else self.eligible(request)
                if reason:
                    code = 'disabled' if disabled else 'stale_state' if reason == ERRORS['stale_state'] else 'unsupported'
                    rejected = result(request, 'rejected', code, reason=reason)
                    db.execute('UPDATE actions SET response=? WHERE action_id=?',
                               (json.dumps(rejected), request['action_id']))
                    return 403 if disabled else 409, rejected
            response = self.call(request, 'cancel')
            self.save(request, response)
            return 200, response

    def get(self, action_id):
        if not ACTION.fullmatch(action_id):
            raise ValueError()
        with self.lock:
            with self.connect() as db:
                row = db.execute('SELECT request,response FROM actions WHERE action_id=?', (action_id,)).fetchone()
            if row is None:
                # Absence in this journal is not proof of absence of an effect.
                return 404, result(dict(action_id=action_id, op='cancel'), 'unknown')
            request, response = map(json.loads, row)
            if response['status'] in OPEN:
                response = self.call(request, 'status')
                # No status rejection proves that an accepted action had no effect.
                if response['status'] == 'rejected':
                    response = result(request, 'unknown')
                self.save(request, response)
            return 200, response
