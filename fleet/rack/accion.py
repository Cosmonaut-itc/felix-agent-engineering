"""Cancelación por lanzador y diario durable. Python 3.9, biblioteca estándar."""
import ctypes
import errno
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
import stat
import subprocess
import sys
import threading
import time

# -I excludes cwd and PYTHONPATH: load only the sibling installed with the panel.
_spec = importlib.util.spec_from_file_location('rack_desk_channel', Path(__file__).with_name('desk_action.py'))
desk_channel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(desk_channel)
_spec = importlib.util.spec_from_file_location('rack_host_channel', Path(__file__).with_name('host_action.py'))
host_channel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(host_channel)

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
    'preflight_unavailable': 'No se pudo comprobar el control del host; la acción no se envió. Vuelve a intentarlo.',
}
CANCEL_OPEN = ('cancelling', 'unknown', 'cancel_unconfirmed')
OPEN = (*CANCEL_OPEN, 'applying', 'partial')
DESK_DESTINATION = '<usuario>@<host-desk>'
DESK_ARGV = ['/usr/bin/ssh', '-F', '/dev/null', '-T',
             '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
             '-o', 'IdentityAgent=none', '-o', 'ConnectTimeout=10',
             '-o', 'StrictHostKeyChecking=yes',
             '-o', 'UserKnownHostsFile=~/.ssh/known_hosts',
             '-o', 'ClearAllForwardings=yes', '-o', 'PermitLocalCommand=no',
             '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
             '-i', '~/.ssh/id_ed25519_pao_rack_accion', DESK_DESTINATION]
_DNS_LABEL = r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
_DESTINATION = re.compile(rf'[A-Za-z0-9_][A-Za-z0-9_.-]*@{_DNS_LABEL}(?:\.{_DNS_LABEL})*')
DESK_TIMEOUT = 40
# Local cancel: the previous 10 s plus up to 2 s for the origin probe (desk_action.send).
LOCAL_CANCEL_TIMEOUT = 12
# Reserve for the journal, backed by an independent HTTP wait deadline.
RECEIPT_RESERVE = 1
MIN_TRANSPORT = 1
JOURNAL_WAIT = .5

CONTROLLERS = {'remote-run': '~/.local/lib/remote-run/run-control.py',
               'jale': '~/.local/lib/jale/pueue-control.py'}


def _desk_config_path():
    return Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'pao/rack-desk-ssh'


class DeskDestinationError(ValueError):
    """The desk transport could not get its destination before dispatch."""


def _has_no_extended_acl(fd):
    if sys.platform == 'darwin':
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            get_acl = libc.acl_get_fd_np
            free_acl = libc.acl_free
        except AttributeError:
            return False
        get_acl.argtypes = (ctypes.c_int, ctypes.c_int)
        get_acl.restype = ctypes.c_void_p
        free_acl.argtypes = (ctypes.c_void_p,)
        free_acl.restype = ctypes.c_int
        acl_type_extended = 0x00000100  # ACL_TYPE_EXTENDED in <sys/acl.h>
        ctypes.set_errno(0)
        acl = get_acl(fd, acl_type_extended)
        if acl:
            free_acl(acl)
            return False
        return ctypes.get_errno() == errno.ENOENT
    if sys.platform == 'linux':
        try:
            return 'system.posix_acl_access' not in os.listxattr(fd)
        except OSError as error:
            return error.errno == errno.ENOTSUP
    return False


@contextmanager
def _open_trusted_config(path, *, encoding=None):
    # Check the opened file itself. No symlink traversal or blocking on FIFOs.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                or not _has_no_extended_acl(fd)):
            raise PermissionError(errno.EACCES, 'Archivo de configuración no confiable')
        with os.fdopen(fd, encoding=encoding, closefd=False) as stream:
            yield stream
    finally:
        os.close(fd)


def desk_destination():
    """Read the private, fixed user@hostname used by both desk channels."""
    try:
        with _open_trusted_config(_desk_config_path(), encoding='ascii') as stream:
            destination = stream.read().rstrip('\r\n')
        if _DESTINATION.fullmatch(destination):
            return destination
    except (OSError, UnicodeError):
        pass
    raise DeskDestinationError('Configura pao/rack-desk-ssh con la cuenta y el hostname SSH del desk')


def adopt_desk_destination(installed):
    """Preserve the shared destination from the previous installed hosts file."""
    path = _desk_config_path()
    if not path.exists() and not path.is_symlink():
        try:
            with _open_trusted_config(installed) as stream:
                hosts = json.load(stream)
            desk = next(host for host in hosts if host['id'] == 'desk')
            destination = desk['argv'][-1]
            expected = [*DESK_ARGV[:-1], destination]
            if (_DESTINATION.fullmatch(destination) and desk['transport'] == 'ssh'
                    and desk['actions']['argv'] == expected):
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                try:
                    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
                        stream.write(destination + '\n')
                except FileExistsError:
                    pass
        except (KeyError, IndexError, TypeError, ValueError, OSError, StopIteration):
            pass
    return desk_destination()


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


def validate(request):
    if type(request) is dict and request.get('op') in host_channel.OPS:
        return host_channel.validate(request)
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
        identity = value['expected']
        return dict(launcher=value['launcher'], ref=value['ref'], reason=value['reason'],
                    expected=None if identity is None else dict(task_id=identity['task_id'], state=identity['state']))
    return None


def action_config(value):
    """Closed controller table and exact SSH argv; no configurable commands."""
    if value is None:
        return dict(transport='disabled', reason='Este host no tiene canal de escritura.', controllers={})
    if (type(value) is not dict or set(value) != ({'transport', 'reason', 'controllers', 'argv'}
                if value.get('transport') == 'ssh' else {'transport', 'reason', 'controllers'})
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


def host_http(response):
    status = response['status']
    if status == 'rejected':
        return 403 if (response.get('error') or {}).get('code') == 'disabled' else 409
    if status in ('conflict', 'unsupported', 'undo_conflict'):
        return 409
    return 202 if status in ('accepted', 'applying') else 200


class Executor:
    """One transport seam, selected by host configuration; no caller-supplied argv."""
    def __init__(self, runner=subprocess.Popen, home=None, timeout=None):
        self.runner, self.timeout = runner, timeout
        self.home = Path(home or pwd.getpwuid(os.getuid()).pw_dir)

    def __call__(self, host, launcher, request, timeout=None):
        if launcher == 'host-control':
            config = action_config(host.get('actions'))
            if host['id'] == 'desk' and host['transport'] == 'ssh' and config['transport'] == 'ssh':
                destination = desk_destination()
                envelope = dict(schema='rack-desk-action@1', target='host-control', request=request)
                argv = [a.replace('~/', str(self.home) + '/') for a in config['argv'][:-1]]
                argv.append(destination)
                code, response = desk_channel.exchange(argv, envelope, self.runner,
                    {'HOME': str(self.home), 'PATH': '/usr/bin:/bin', 'TMPDIR': '/tmp'},
                    min(DESK_TIMEOUT, timeout if timeout is not None else DESK_TIMEOUT))
                if code != 0:
                    raise ValueError('Respuesta inválida')
                return response
            if host['id'] != 'mini' or host['transport'] != 'local' or config['transport'] != 'local':
                return host_channel.unavailable()
            controller = self.home / '.local/lib/pao-pueue-mini/control.py'
            if not controller.is_file():
                return host_channel.unavailable()
            code, response = desk_channel.exchange(
                ['/usr/bin/python3', '-I', str(controller), 'rack', '--host', 'mini'], request,
                self.runner, {'HOME': str(self.home), 'PATH': '/usr/bin:/bin:/opt/homebrew/bin', 'TMPDIR': '/tmp'},
                min(10, timeout if timeout is not None else 10))
            if code != 0:
                raise ValueError('Respuesta inválida')
            return response
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
            argv = [*config['argv'][:-1], desk_destination()]
            request = dict(schema='rack-desk-action@1', launcher=launcher, request=request)
            code, response = desk_channel.exchange(argv, request, self.runner, env, timeout)
        else:
            controller = self.home / config['controllers'][launcher][2:]
            argv = ['/usr/bin/python3', '-I', str(controller)]
            code, response = desk_channel.send(argv, request, self.runner, env, timeout)
        if code not in ((0,) if remote else (0, 2)):
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
    if status not in (*CANCEL_OPEN, 'cancelled', 'already_finished', 'rejected'):
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
            if value['op'] in ('cancel', 'slots', 'mode', 'undo', 'reorder') and not value.get('dry_run'):
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
            code, response = 200, result(identity, 'unknown')
        elif errors:
            raise errors[0]
        else:
            code, response = answers[0]
        # Stamped per reply, never journaled: the panel judges a receipt's expiry by this, not by a snapshot.
        return code, dict(response, served_at=self.clock())

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
        if request['op'] in host_channel.OPS:
            transport = action_config(host.get('actions'))['transport']
            if not ((host['id'], host['transport'], transport) in (('mini','local','local'), ('desk','ssh','ssh'))):
                return 'Falta actualizar el control de este host.'
            payload = self.read()
            current = next((h for h in payload['hosts'] if h['id'] == request['host']), None)
            if not current or not current['fresh'] or current.get('age') is None or current['age'] > 45 or self.clock() - payload['now'] > 45:
                return ERRORS['stale_state']
            if (request['op']=='reorder' or 'queue' in request.get('expected',{})) and not isinstance(((current.get('snapshot') or {}).get('control') or {}).get('reorder_groups'),dict):
                return 'Falta actualizar el control de este host.'
            if request['op'] == 'mode' and ((current.get('snapshot') or {}).get('control') or {}).get('capabilities', {}).get('mode') is not True:
                return 'Falta actualizar el control de este host.'
            return None
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
        if request['op'] in host_channel.OPS:
            mutation_sent = False
            def unsent():
                return dict(result(request,'rejected','preflight_unavailable'),effects=False)
            wire = {k: v for k, v in request.items() if k not in ('schema', 'host')}
            wire.update(schema='rack-host-control@1', op='capabilities' if request['op'] == 'host_capabilities' else request['op'])
            if op == 'status':
                wire = dict(schema='rack-host-control@1', op='status', action_id=request['action_id'])
            remaining = deadline - self.timer() - RECEIPT_RESERVE
            if remaining < MIN_TRANSPORT:
                return result(request, 'unknown') if op=='status' else unsent()
            try:
                if op != 'status' and (request['op']=='reorder' or 'queue' in request.get('expected',{})):
                    # Snapshot may predate a rollback. Probe the actual controller,
                    # including undo: an old host already knows slots undo.
                    caps = self.executor(self.hosts[request['host']], 'host-control',
                                         dict(schema='rack-host-control@1',op='capabilities'), timeout=min(.5,remaining))
                    current = host_channel.control(caps, request['host'])
                    if current['status']=='ok' and 'reorder_groups' not in current:
                        answer = dict(host_channel.unavailable(), action_id=request['action_id'], effects=False,
                                      schema=SCHEMA, op=request['op'], host=request['host'])
                        return answer
                    if current['status']!='ok':
                        return unsent()
                    remaining = deadline - self.timer() - RECEIPT_RESERVE
                    if remaining < MIN_TRANSPORT:
                        return unsent()
                mutation_sent = True
                response = self.executor(self.hosts[request['host']], 'host-control', wire, timeout=min(10, remaining))
                response = host_channel.sanitize(request, response, wire['op'])
                verified = response.pop('receipt_verified', False)
                if op == 'status' and response['status'] not in ('applied', 'applying', 'partial', 'unknown') and not verified:
                    return result(request, 'unknown')
                response.update(schema=SCHEMA, op=request['op'], host=request['host'])
                return response if self.timer() <= deadline - RECEIPT_RESERVE else result(request, 'unknown')
            except DeskDestinationError:
                return result(request, 'unknown') if op == 'status' else unsent()
            except Exception:
                return result(request, 'unknown') if mutation_sent or op=='status' else unsent()
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
        except DeskDestinationError:
            return (result(request, 'unknown') if op == 'status'
                    else result(request, 'rejected', 'preflight_unavailable'))
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
            if request['op'] in host_channel.OPS or previous['op'] in host_channel.OPS:
                if (response['status'] in OPEN and previous['host']==request['host']
                        and (request['op']=='reorder' or previous['op']=='reorder'
                             or 'queue' in request.get('expected',{}) or 'queue' in previous.get('expected',{}))):
                    return 'Hay una acción pendiente en este host; consulta su recibo.'
                if (request['op'] in host_channel.OPS and previous['op'] in host_channel.OPS
                        and response['status'] in OPEN and previous['host'] == request['host']):
                    return 'Hay un ajuste pendiente en este host; consulta su recibo.'
                continue
            if response['status'] in OPEN and all(previous[k] == request[k] for k in ('host', 'launcher', 'ref')):
                return 'Esta corrida tiene una cancelación pendiente (' + previous['action_id'] + '); consulta su recibo.'
        return None

    def complete(self, request, op, done, deadline):
        try:
            response = self.call(request, op, deadline)
            if op == 'status' and response['status'] == 'rejected' and request['op'] not in host_channel.OPS:
                response = result(request, 'unknown')
            try:
                with self.journal(deadline):
                    response = self.save(request, response, deadline)
                    self.inflight.pop(request['action_id'], None)
            except TimeoutError:
                # The committed intent still owns this ID and blocks a second cancel.
                return 200, result(request, 'unknown')
            return host_http(response) if request['op'] in host_channel.OPS else 200, response
        finally:
            # Event.set needs no journal lock. A failed commit can be reconciled by GET.
            done.set()

    def post(self, request, started=None):
        started = self.timer() if started is None else started
        validate(request)
        if request['op'] == 'host_capabilities' or request.get('dry_run') is True:
            reason = ERRORS['disabled'] if self.disabled() else self.eligible(request)
            if reason:
                response = result(request, 'unsupported', 'unsupported', reason=reason)
                response['capabilities'] = host_channel.unavailable()['capabilities']
                return host_http(response), response
            response = self.call(request, 'capabilities' if request['op'] == 'host_capabilities' else request['op'],
                                 started + request_budget(request['host']))
            return host_http(response), response
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
                            response = json.loads(row[1])
                            return host_http(response) if request['op'] in host_channel.OPS else 200, response
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
                    if response['status'] not in OPEN or self.active(action_id):
                        return host_http(response) if request['op'] in host_channel.OPS else 200, response
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
