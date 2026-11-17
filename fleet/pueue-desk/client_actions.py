"""CC1: diario durable y coordinador local de acciones cliente (pueue-client-control@1).

Python 3.9 y biblioteca estándar. El llamador posee control.lock en toda operación
que puede enviar un RPC o escribir el diario. Con ese candado este módulo nunca llama
a un lanzador, nunca toma su result.lock, nunca invoca `client` de forma recursiva y
nunca espera la gracia de una señal: sólo lee archivos de control con forma fija.

El diario guarda sólo ids, estados, huellas opacas y motivos cerrados. Ninguna
petición elige host, grupo, socket, configuración, binario, directorio, comando,
señal libre ni prioridad: todo sale de la instalación y de los adaptadores fijos.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import types
from datetime import datetime, timedelta, timezone


def _contract():
    # Sin bytecode: igual que el editor de prioridad, una lectura nunca escribe pyc.
    path = Path(__file__).with_name('client_contract.py')
    module = types.ModuleType('client_contract_cc1')
    module.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), 'exec'), module.__dict__)
    return module


contract = _contract()
Invalid = contract.Invalid

APPLIED = {'hold': 'held', 'cancel-no-payload': 'started', 'signal': 'signalled',
           'signal-manual': 'signalled', 'retire-held': 'retired', 'retire-interrupted': 'retired'}
# Un rechazo por estas razones no tuvo efecto y puede reevaluarse con el MISMO cuerpo.
RETRYABLE = ('locked', 'evidence_missing')
FINGERPRINT_FIELDS = ('id', 'created_at', 'group', 'command', 'original_command', 'path',
                      'dependencies', 'label')
READ_LIMIT = 16 * 1024
TABLES = ('CREATE TABLE IF NOT EXISTS client_actions (key TEXT PRIMARY KEY, grp TEXT NOT NULL, '
          'digest TEXT NOT NULL, body TEXT NOT NULL)',
          'CREATE TABLE IF NOT EXISTS client_reservations (action TEXT PRIMARY KEY, grp TEXT NOT NULL, '
          'body TEXT NOT NULL)')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def sha(value, domain):
    data = value if type(value) is bytes else canonical(value).encode('utf-8')
    return hashlib.sha256(domain.encode() + b'\0' + data).hexdigest()


def request_digest(request):
    return sha(request, 'pueue-client-request@1')


def phase_key(request):
    return canonical([request['launcher'], request['ref'], request['action_id'], request['phase']])


def action_key(request):
    return canonical([request['launcher'], request['ref'], request['action_id']])


STOCK_TIME = re.compile(r'([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(?:\.([0-9]{1,9}))?'
                        r'(Z|[+-][0-9]{2}:[0-9]{2})')


def utc(value):
    """Formato stock RFC 3339 -> UTC canónico con microsegundos truncados."""
    if type(value) is not str:
        raise ValueError('Fecha ilegible')
    match = STOCK_TIME.fullmatch(value)
    if match is None:
        raise ValueError('Fecha ilegible')
    base = datetime.strptime(match[1], '%Y-%m-%dT%H:%M:%S')
    micros = int(((match[2] or '') + '000000')[:6])
    offset = timedelta(0)
    if match[3] != 'Z':
        sign = 1 if match[3][0] == '+' else -1
        offset = sign * timedelta(hours=int(match[3][1:3]), minutes=int(match[3][4:6]))
    moment = (base - offset).replace(microsecond=micros, tzinfo=timezone.utc)
    return moment.strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def epoch_utc(value):
    if type(value) not in (int, float) or value != value or not 0 < value < 2**40:
        raise ValueError('Instante ilegible')
    return datetime.fromtimestamp(value, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


class Refused(Exception):
    """Razón cerrada de REASONS; nunca texto del daemon ni del lanzador."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------- adaptadores fijos

def read_small(path, limit=READ_LIMIT):
    """Archivo regular propio, sin enlaces, acotado. Ausente -> None."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0))
    except FileNotFoundError:
        return None
    except OSError:
        raise Refused('identity')
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > limit:
            raise Refused('identity')
        data = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise Refused('identity')
    return data


def read_object(path):
    data = read_small(path)
    if data is None:
        return None, None
    try:
        value = contract.decode(data)
    except Invalid:
        raise Refused('identity')
    if type(value) is not dict:
        raise Refused('identity')
    return value, data


def exists(path):
    return os.path.lexists(str(path))


class Run:
    def __init__(self, directory, task_id, group, manifest):
        self.directory, self.task_id, self.group, self.manifest = directory, task_id, group, manifest


class Adapter:
    """Interfaz fija por lanzador. Por defecto falla cerrado."""
    pattern = None
    modes = ()

    def __init__(self, home):
        self.home = Path(home)

    def directory(self, ref):
        if self.pattern is None or not re.fullmatch(self.pattern, ref):
            raise Refused('not_found')
        base = self.root
        path = base / ref
        try:
            info = os.lstat(str(path))
        except FileNotFoundError:
            raise Refused('not_found')
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise Refused('identity')
        if os.path.realpath(str(path)) != os.path.join(os.path.realpath(str(base)), ref):
            raise Refused('identity')
        return path

    def resolve(self, ref, mode):
        raise Refused('unsupported')

    def acceptance(self, run, attempt):
        raise Refused('unsupported')

    def proof(self, run, attempt, acceptance):
        raise Refused('unsupported')

    def interrupted(self, run):
        raise Refused('unsupported')

    def manual(self, run):
        """Acredita que la tarea Running es manual y propia del lanzador."""
        raise Refused('unsupported')

    def attempt(self, run):
        """attempt durable que publica el lanzador (CC3-CC5); None si no publica ninguno."""
        return None


def single_id(data):
    text = data.decode('ascii', 'strict') if data is not None else None
    if text is None or not re.fullmatch(r'[0-9]{1,10}\n?', text) or int(text) > 2**31 - 1:
        raise Refused('identity')
    return int(text)


class RemoteRun(Adapter):
    """Hasito remote-run: ~/remote-run/runs/<ref>, pueue.id de un campo y run-control.json.

    Acredita pertenencia por directorio físico, pueue.id y manifiesto rack-cancel@1.
    Aceptación: pueue.cancellation.json enlazada a pueue.attempt.json. Prueba:
    pueue.worker-result.json y pueue.result.json del mismo intento. Nunca lee meta,
    body.sh, cmd.sh, log ni el comando de la tarea.
    """
    pattern = r'[0-9]{8}-[0-9]{6}-[0-9a-f]{6}'

    @property
    def root(self):
        return self.home / 'remote-run/runs'

    def resolve(self, ref, mode):
        path = self.directory(ref)
        raw = read_small(path / 'pueue.id', 64)
        if raw is None:
            raise Refused('not_found')
        if exists(path / 'pueue.group'):
            raise Refused('identity')  # registro de apotheca, no de Hasito
        task_id = single_id(raw)
        manifest, _ = read_object(path / 'run-control.json')
        if manifest is not None:
            if (manifest.get('schema') != 'rack-cancel@1' or manifest.get('consumer') != 'remote-run@2'
                    or manifest.get('ref') != ref or manifest.get('host') != 'mini'
                    or type(manifest.get('task_id')) is not int or manifest['task_id'] != task_id
                    or type(manifest.get('group')) is not str):
                raise Refused('identity')
        if manifest is None and mode != 'own-legacy':
            raise Refused('unsupported')
        if mode.startswith('rack-') and manifest.get('cancel_origin') is not True:
            raise Refused('unsupported')
        return Run(path, task_id, manifest['group'] if manifest else None, manifest)

    def _acceptance_file(self, run):
        value, data = read_object(run.directory / 'pueue.cancellation.json')
        current, _ = read_object(run.directory / 'pueue.attempt.json')
        if value is None or current is None:
            raise Refused('evidence_missing')
        if value.get('attempt') != current.get('id') or type(value.get('attempt')) is not str:
            raise Refused('evidence_missing')
        return value, data

    def acceptance(self, run, attempt):
        value, data = self._acceptance_file(run)
        origin = value.get('origin')
        try:
            projected = dict(action_id=value['action_id'], attempt=utc(value['enqueued_at']),
                             accepted_at=epoch_utc(value['started']),
                             origin=origin if origin == 'rack-panel' else None,
                             receipt=sha(data, 'remote-run-acceptance@1'))
        except (KeyError, ValueError):
            raise Refused('evidence_missing')
        if 'origin' in value and origin != 'rack-panel':
            raise Refused('unsupported')
        if projected['attempt'] != attempt:
            raise Refused('identity')
        return projected

    def proof(self, run, attempt, acceptance):
        value, accepted = self._acceptance_file(run)
        worker, data = read_object(run.directory / 'pueue.worker-result.json')
        result, _ = read_object(run.directory / 'pueue.result.json')
        if worker is None or result is None:
            raise Refused('evidence_missing')
        if (worker.get('attempt') != value['attempt'] or result.get('attempt') != value['attempt']
                or worker.get('cancelled') is not True or result.get('cancelled') is not True
                or type(result.get('exit')) is not int or result['exit'] != 130):
            raise Refused('evidence_missing')
        kind = ('no-payload' if worker.get('no_payload') is True else
                'terminated' if worker.get('payload_complete') is True else None)
        if kind is None:
            raise Refused('evidence_missing')
        return dict(kind=kind, attempt=attempt, receipt=sha(data, 'remote-run-proof@1'),
                    acceptance_receipt=sha(accepted, 'remote-run-acceptance@1'), exit=130)

    def interrupted(self, run):
        # Sin resultado normal publicado: ni exit ni resultados del trabajador.
        return not any(exists(run.directory / n) for n in ('exit', 'pueue.result.json', 'pueue.worker-result.json'))


class Apotheca(Adapter):
    """apotheca: misma raíz física, pueue.id de tres campos «id grupo origen».

    Sin aceptación ni prueba de trabajador acreditables: sólo parada propia. El
    formato antiguo de un campo deriva el grupo de meta, que no se lee: unsupported.
    """
    pattern = r'[0-9]{8}-[0-9]{6}-[0-9a-f]{6}'

    @property
    def root(self):
        return self.home / 'remote-run/runs'

    def resolve(self, ref, mode):
        path = self.directory(ref)
        raw = read_small(path / 'pueue.id', 128)
        if raw is None:
            raise Refused('not_found')
        try:
            fields = raw.decode('ascii').split()
        except UnicodeError:
            raise Refused('identity')
        if len(fields) == 1:
            group_raw = read_small(path / 'pueue.group', 64)
            if group_raw is None:
                raise Refused('unsupported')
            try:
                fields = fields + [group_raw.decode('ascii').strip(), 'explicito']
            except UnicodeError:
                raise Refused('identity')
        if (len(fields) != 3 or not re.fullmatch(r'[0-9]{1,10}', fields[0]) or int(fields[0]) > 2**31 - 1
                or fields[2] not in ('explicito', 'implicito', 'relevo')
                or not re.fullmatch(r'[a-z0-9-]{1,32}', fields[1])):
            raise Refused('identity')
        return Run(path, int(fields[0]), fields[1], None)

    def interrupted(self, run):
        return not exists(run.directory / 'exit')


class Jale(Adapter):
    """Jale: ~/.local/state/jale/pueue/<job-…>/run-control.json (rack-cancel@1).

    cancel-request.json no enlaza el intento de Pueue: aceptación, prueba y retirada
    devuelven unsupported hasta CC5. Sin manifiesto, el id no se acredita.
    """
    pattern = r'job-[A-Za-z0-9]{8}'

    @property
    def root(self):
        return self.home / '.local/state/jale/pueue'

    def resolve(self, ref, mode):
        path = self.directory(ref)
        manifest, _ = read_object(path / 'run-control.json')
        if manifest is None:
            raise Refused('unsupported')
        if (manifest.get('contract') != 'rack-cancel@1' or manifest.get('protocol') not in (1, 2)
                or type(manifest.get('protocol')) is not int or manifest.get('ref') != ref
                or type(manifest.get('group')) is not str or type(manifest.get('task_id')) is not int
                or not 0 <= manifest['task_id'] <= 2**31 - 1):
            raise Refused('identity')
        if mode.startswith('rack-') and manifest.get('cancel_origin') is not True:
            raise Refused('unsupported')
        return Run(path, manifest['task_id'], manifest['group'], manifest)


class Vigilante(Adapter):
    """La clasificación manual exige leer comando y ruta de la tarea: falla cerrado."""


def default_adapters(home):
    return {'remote-run': RemoteRun(home), 'apotheca': Apotheca(home), 'jale': Jale(home),
            'vigilante': Vigilante(home)}


# ---------------------------------------------------------------- diario

def retryable(response):
    """Recibo sin efecto que el mismo cuerpo puede reevaluar."""
    return response['status'] == 'deferred' or response['status'] == 'rejected' and response['reason'] in RETRYABLE


def tables_exist(db):
    names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {'client_actions', 'client_reservations'} <= names


def ensure_tables(db):
    for statement in TABLES:
        db.execute(statement)


def open_records(db):
    """Fases cuyo RPC pudo ocurrir sin resolverse: intención huérfana o unknown."""
    if not tables_exist(db):
        return []
    rows = []
    for key, group, body in db.execute('SELECT key,grp,body FROM client_actions'):
        record = json.loads(body)
        if record['state'] == 'intent' or record['response']['status'] == 'unknown':
            rows.append((key, group, record))
    return rows


def held_reservations(db):
    if not tables_exist(db):
        return []
    return [(action, group, json.loads(body)) for action, group, body
            in db.execute('SELECT action,grp,body FROM client_reservations')
            if json.loads(body)['state'] == 'held']


def blocked_groups(engine):
    """Grupo -> motivo cerrado, para el reordenar y su vista. Sólo lectura."""
    if not engine.path.exists():
        return {}
    with engine.connect() as db:
        result = {group: 'reservation' for _, group, _ in held_reservations(db)}
        result.update({group: 'unknown' for _, group, _ in open_records(db)})
    return result


def reserved_task_ids(directory):
    """Ids que la poda no debe retirar mientras una reserva cliente siga abierta."""
    path = Path(directory) / 'control.sqlite3'
    if not os.path.lexists(str(path)):
        return set()
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.2)
    try:
        return {str(body['task_id']) for _, _, body in held_reservations(db)}
    finally:
        db.close()


# ---------------------------------------------------------------- coordinador

class Coordinator:
    """Operaciones de negocio cerradas. `handle` exige control.lock salvo en lecturas."""

    rpc_timeout = 3.0

    def __init__(self, engine, home=None, adapters=None):
        self.engine = engine
        self.home = Path.home() if home is None else Path(home)
        self.adapters = default_adapters(self.home) if adapters is None else adapters
        self.boundary = lambda phase: None

    # -- proyección de identidad
    def generation_digest(self, generation):
        return sha(generation.encode('utf-8'), 'pueue-client-generation@1')

    def fingerprint(self, task):
        return sha({k: task.get(k) for k in FINGERPRINT_FIELDS}, 'pueue-client-fingerprint@1')

    def project(self, task, task_id, generation, attempt):
        """Identidad del contrato para la tarea observada; attempt sale del diario/petición."""
        base = dict(generation=self.generation_digest(generation), task_id=task_id)
        if task is None:
            return None
        status = task['status']
        name = next(iter(status)) if type(status) is dict else status
        detail = status[name] if type(status) is dict else {}
        if name not in ('Queued', 'Stashed', 'Running', 'Paused', 'Done', 'Locked'):
            raise Refused('identity')
        created = utc(task['created_at'])
        out = dict(base, created_at=created, fingerprint=self.fingerprint(task), attempt=attempt,
                   state=name, enqueued_at=None, enqueue_at=None, started_at=None)
        if name == 'Queued':
            out['enqueued_at'] = utc(detail['enqueued_at'])
            if attempt is None:
                out['attempt'] = out['enqueued_at']
        elif name == 'Stashed':
            out['enqueue_at'] = utc(detail['enqueue_at']) if detail.get('enqueue_at') is not None else None
        elif name in ('Running', 'Paused', 'Done'):
            out['enqueued_at'] = utc(detail['enqueued_at'])
            out['started_at'] = utc(detail['start'])
            if attempt is None:
                out['attempt'] = out['enqueued_at']
        if out['attempt'] is None:
            raise Refused('identity')
        try:
            return contract.identity(out)
        except Invalid:
            raise Refused('identity')

    @staticmethod
    def absent(expected):
        return dict({k: expected[k] for k in ('generation', 'task_id', 'created_at', 'fingerprint', 'attempt')},
                    state='Absent', enqueued_at=None, enqueue_at=None, started_at=None)

    @staticmethod
    def stable(identity):
        return tuple(identity[k] for k in ('generation', 'task_id', 'created_at', 'fingerprint', 'attempt'))

    # -- respuestas
    def reply(self, request, status, reason=None, identity=None, reservation='none'):
        applied = status in APPLIED.values()
        if identity is None and request['acceptance'] is not None:
            # La aceptación exige identidad; la última conocida es la de la petición.
            identity = request['expected']
        if reservation == 'held' and identity is not None and identity['enqueue_at'] is not None:
            identity = dict(request['expected']) if request['expected']['enqueue_at'] is None else None
        response = dict(schema=contract.SCHEMA, op=request['op'], launcher=request['launcher'],
                        ref=request['ref'], action_id=request['action_id'], phase=request['phase'],
                        mode=request['mode'], status=status,
                        effects=None if status == 'unknown' else applied,
                        reservation=reservation, identity=identity, acceptance=request['acceptance'],
                        proof=request['proof'] if status == 'finished' else None,
                        reason=None if applied or status == 'finished' else reason)
        contract.validate_response(response, request)
        return response

    def capabilities(self, request):
        answer = dict(schema=contract.SCHEMA, op='capabilities', version=1, ops=list(contract.OPS))
        if 'launcher' not in request:
            return answer
        answer.update(launcher=request['launcher'], ref=request['ref'], identity=None, reason=None)
        try:
            if self.engine.host != 'mini':
                raise Refused('unsupported')
            adapter = self.adapters[request['launcher']]
            run = adapter.resolve(request['ref'], 'own-legacy')
            with self.engine.connect() as db:
                state = self.engine.state(db)
                attempt = None
                for _, _, body in held_reservations(db):
                    if (body['launcher'], body['ref'], body['task_id']) == (request['launcher'], request['ref'], run.task_id):
                        attempt = body['identity'][4]
            task = self.engine.pueue.status()['tasks'].get(str(run.task_id))
            if task is None:
                raise Refused('not_found')
            attempt = attempt or adapter.attempt(run)
            if attempt is None and next(iter(task['status'])) in ('Stashed', 'Locked'):
                raise Refused('identity')  # sin diario no se acredita el intento
            answer['identity'] = self.project(task, run.task_id, self.engine.generation(state), attempt)
        except Refused as error:
            answer['identity'] = None
            answer['reason'] = error.reason if error.reason in ('not_found', 'unsupported') else 'identity'
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError):
            answer['identity'] = None
            answer['reason'] = 'identity'
        contract.validate_response(answer, request)
        return answer

    def load(self, db, key):
        if not tables_exist(db):
            return None
        row = db.execute('SELECT digest,body FROM client_actions WHERE key=?', (key,)).fetchone()
        return None if row is None else dict(json.loads(row[1]), digest=row[0])

    def reservation(self, db, action):
        if not tables_exist(db):
            return None
        row = db.execute('SELECT body FROM client_reservations WHERE action=?', (action,)).fetchone()
        return None if row is None else json.loads(row[0])

    def status(self, request):
        """Proyección de sólo lectura; no necesita control.lock ni envía RPC."""
        original = contract.action_projection(request)
        with self.engine.connect() as db:
            record = self.load(db, phase_key(original))
            held = self.reservation(db, action_key(original))
        reservation = 'held' if held and held['state'] == 'held' else 'none'
        if record is None:
            return self.status_reply(request, original, 'rejected', 'not_found', None, reservation)
        if record['digest'] != request_digest(original):
            return self.status_reply(request, original, 'rejected', 'idempotency_conflict', None, reservation)
        if record['state'] == 'intent':
            return self.status_reply(request, original, 'unknown', 'rpc_unresolved', None, 'held')
        stored = dict(record['response'])
        if (stored['status'] == 'deferred' and stored['reservation'] == 'held' and stored['identity'] is not None
                and stored['identity']['state'] in ('Stashed', 'Running', 'Paused')):
            stored.update(status='pending')  # nada nuevo en esta consulta; falta progreso
        stored.update(op='status', action_op=original['op'])
        contract.validate_response(stored, request)
        return stored

    def status_reply(self, request, original, status, reason, identity, reservation):
        response = self.reply(original, status, reason, identity, reservation)
        response.update(op='status', action_op=original['op'])
        contract.validate_response(response, request)
        return response

    def busy(self, request, reason='lock_busy'):
        """Sin efecto de esta petición: ninguna escritura; reserva leída sin candado."""
        held = False
        if self.engine.path.exists():
            with self.engine.connect() as db:
                record = self.load(db, phase_key(request))
                found = self.reservation(db, action_key(request))
                held = bool(found and found['state'] == 'held')
            if record is not None and record['digest'] != request_digest(request):
                return self.reply(request, 'rejected', 'idempotency_conflict', reservation='held' if held else 'none')
            if record is not None and not retryable(record['response']):
                return record['response']  # mismo cuerpo, mismo recibo, también con el lock ocupado
        if request['op'] in ('cancel-no-payload', 'finish') and not held:
            return self.reply(request, 'rejected', reason)
        return self.reply(request, 'deferred', reason, reservation='held' if held else 'none')

    def fallback(self, request):
        """Fallo inesperado: nunca afirma ausencia de efecto si quedó una intención."""
        if request['op'] == 'capabilities':
            return None
        target = contract.action_projection(request) if request['op'] == 'status' else request
        response = self.busy(target, 'host_action')
        if request['op'] == 'status':
            response = dict(response, op='status', action_op=target['op'])
            contract.validate_response(response, request)
        return response

    def handle(self, request, locked=True):
        op = request['op']
        if op == 'capabilities':
            return self.capabilities(request)
        if op == 'status':
            return self.status(request)
        if self.engine.host != 'mini':
            return self.reply(request, 'rejected', 'unsupported')
        if not locked:
            return self.busy(request)
        return self.mutate(request)

    # -- operación mutadora, bajo control.lock
    def save(self, request, group, state, response, reservation=None, rpc=None):
        record = dict(request=request, state=state, response=response, rpc=rpc)
        with self.engine.connect(write=True) as db:
            ensure_tables(db)
            db.execute('INSERT OR REPLACE INTO client_actions VALUES (?,?,?,?)',
                       (phase_key(request), group, request_digest(request), canonical(record)))
            if reservation is not None:
                db.execute('INSERT OR REPLACE INTO client_reservations VALUES (?,?,?)',
                           (action_key(request), group, canonical(reservation)))

    def mutate(self, request):
        key, digest = phase_key(request), request_digest(request)
        with self.engine.connect() as db:
            record = self.load(db, key)
            reserved = self.reservation(db, action_key(request))
        held = bool(reserved and reserved['state'] == 'held')
        if record is not None:
            if record['digest'] != digest:
                return self.reply(request, 'rejected', 'idempotency_conflict', reservation='held' if held else 'none')
            # Una intención huérfana guarda ya su unknown: el RPC pudo salir, nunca un segundo envío.
            if not retryable(record['response']):
                return record['response']
        try:
            outcome = self.evaluate(request, reserved)
        except Refused as error:
            outcome = dict(status='rejected', reason=error.reason, identity=None, group=None)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError):
            # Lectura previa fallida: ningún RPC salió por esta petición.
            outcome = dict(status='deferred', reason='host_action', identity=None, group=None)
        if 'rpc' not in outcome:
            return self.settle_without_rpc(request, outcome, reserved)
        return self.apply(request, outcome, reserved)

    def settle_without_rpc(self, request, outcome, reserved):
        held = bool(reserved and reserved['state'] == 'held')
        status = outcome['status']
        if status == 'deferred' and request['op'] in ('cancel-no-payload', 'finish') and not held:
            status, outcome['reason'] = 'rejected', 'evidence_missing'
        if status == 'finished':
            response = self.reply(request, 'finished', identity=outcome['identity'], reservation='closed')
            self.save(request, outcome['group'], 'done', response, dict(reserved, state='closed', closed_by='finish'))
            return response
        response = self.reply(request, status, outcome['reason'], outcome['identity'],
                              'held' if held else 'none')
        # También un rechazo sin grupo queda ligado: otro cuerpo dará conflicto.
        self.save(request, outcome['group'] or '', 'done', response)
        return response

    def evaluate(self, request, reserved):
        """Lecturas y precondiciones bajo control.lock. Devuelve el plan, sin efectos."""
        op, mode, expected = request['op'], request['mode'], request['expected']
        adapter = self.adapters[request['launcher']]
        run = adapter.resolve(request['ref'], mode)
        if run.task_id != expected['task_id']:
            raise Refused('identity')
        if reserved is not None:
            if reserved['state'] != 'held':
                raise Refused('stale_state')  # acción ya cerrada: no se reabre
            if (reserved['mode'] != mode or tuple(reserved['identity']) != self.stable(expected)
                    or reserved['task_id'] != run.task_id):
                raise Refused('identity')
        engine = self.engine
        with engine.connect() as db:
            state = engine.state(db)
            others = [(k, g, r) for k, g, r in open_records(db) if k != phase_key(request)]
            foreign = [(a, g, b) for a, g, b in held_reservations(db)
                       if a != action_key(request) and b['task_id'] == run.task_id]
        snapshot = engine.pueue.status()
        generation = engine.generation(state)
        task = snapshot['tasks'].get(str(run.task_id))
        group = task['group'] if task is not None else (reserved or {}).get('group')
        if group is None:
            raise Refused('not_found')
        if group not in engine.profile or (run.group is not None and run.group != group):
            raise Refused('unsupported' if group not in engine.profile else 'identity')
        if self.generation_digest(generation) != expected['generation']:
            raise Refused('identity')
        observed = self.project(task, run.task_id, generation, expected['attempt']) if task is not None else None
        plan = dict(group=group, identity=observed, run=run)
        if observed is not None and self.stable(observed) != self.stable(expected):
            raise Refused('identity')

        if op == 'finish':
            return self.finish_plan(request, reserved, adapter, run, observed, plan)
        if op in ('cancel-no-payload', 'retire-held') and not (reserved and reserved.get('stashed')):
            raise Refused('evidence_missing')  # sólo el Stashed que esta acción apartó
        # Barreras de grupo: reordenar, preparación, recuperación, acción incierta.
        startup = state.get('startup', {}).get('phase') not in (None, 'ready')
        handoff = state.get('mini_handoff', {}).get('phase') not in (None, 'done', 'withdrawn', 'aborted')
        reorder = ('reorder' in state['reasons'].get(group, []) or
                   any(a['group'] == group and a['phase'] != 'terminal' for a in engine.reorder_actions()))
        if startup:
            return dict(plan, status='deferred', reason='startup')
        if (reorder or handoff or generation != state['generation']
                or os.path.lexists(str(engine.directory / 'maintenance'))):
            return dict(plan, status='deferred', reason='host_action')
        signals = ('signal', 'signal-manual')
        for other_key, other_group, other in others:
            same = json.loads(other_key)[:3] == json.loads(phase_key(request))[:3]
            if other_group == group and not (same and op in signals and other['request']['op'] in signals):
                return dict(plan, status='deferred', reason='rpc_unresolved')
        if foreign:
            return dict(plan, status='deferred', reason='host_action')
        if observed is None:
            raise Refused('not_found')
        if observed['state'] == 'Locked':
            raise Refused('locked')  # Locked ajeno: nunca se toca

        argv = None
        if op == 'hold':
            if observed['state'] != 'Queued' or observed['enqueued_at'] != expected['attempt']:
                raise Refused('stale_state')  # Queued->Running: jamás se escala a señal
            argv = ('stash', str(run.task_id))
        elif op == 'cancel-no-payload':
            if observed['state'] != 'Stashed' or observed['enqueue_at'] is not None:
                raise Refused('stale_state')
            if adapter.acceptance(run, expected['attempt']) != request['acceptance']:
                raise Refused('evidence_missing')
            closed = self.closed_reason(state, snapshot, group)
            if closed:
                return dict(plan, status='deferred', reason=closed)
            argv = ('start', str(run.task_id))
        elif op in signals:
            if op == 'signal-manual':
                adapter.manual(run)
            if observed['state'] not in ('Running', 'Paused') or any(
                    observed[k] != expected[k] for k in ('enqueued_at', 'started_at')):
                raise Refused('stale_state')
            if mode == 'rack-running' and adapter.acceptance(run, expected['attempt']) != request['acceptance']:
                raise Refused('evidence_missing')
            argv = ('kill', '-s', 'SIG' + request['signal'], str(run.task_id))
        elif op == 'retire-held':
            if observed['state'] != 'Stashed' or observed['enqueue_at'] is not None:
                raise Refused('stale_state')
            argv = ('remove', str(run.task_id))
        elif op == 'retire-interrupted':
            result = snapshot['tasks'][str(run.task_id)]['status'].get('Done', {}).get('result')
            normal = result == 'Success' or (type(result) is dict and 'Failed' in result)
            if (observed['state'] != 'Done' or normal
                    or any(observed[k] != expected[k] for k in ('enqueued_at', 'started_at'))):
                raise Refused('stale_state')
            if not adapter.interrupted(run):
                raise Refused('stale_state')  # nunca retirar un resultado publicado
            argv = ('remove', str(run.task_id))
        return dict(plan, rpc=argv)

    def closed_reason(self, state, snapshot, group):
        """Una cancelación pendiente nunca abre por encima de manual, presión ni arranque."""
        reasons = set(state['reasons'].get(group, []))
        fence = self.engine.fence_reason(group)
        if fence:
            reasons.add(fence)
        if reasons & {'manual', 'ambiguous', 'handoff'}:
            return 'manual'
        if 'startup' in reasons:
            return 'startup'
        if 'pressure' in reasons:
            return 'pressure'
        if 'reorder' in reasons:
            return 'host_action'
        return None if snapshot['groups'][group]['status'] == 'Running' else 'manual'

    def finish_plan(self, request, reserved, adapter, run, observed, plan):
        if not (reserved and reserved['state'] == 'held'):
            raise Refused('evidence_missing')
        expected, proof = request['expected'], request['proof']
        current = observed if observed is not None else self.absent(expected)
        if current['state'] != expected['state'] or any(current[k] != expected[k] for k in
                                                         ('enqueued_at', 'enqueue_at', 'started_at')):
            raise Refused('stale_state')
        if proof['kind'] == 'interrupted':
            with self.engine.connect() as db:
                retired = [self.load(db, canonical([request['launcher'], request['ref'], request['action_id'], p]))
                           for p in ('retire-held', 'retire-interrupted')]
            receipts = {sha(r['response'], 'pueue-client-receipt@1') for r in retired
                        if r is not None and r['state'] == 'done' and r['response']['status'] == 'retired'}
            if proof['receipt'] not in receipts:
                raise Refused('evidence_missing')
        else:
            if adapter.acceptance(run, expected['attempt']) != request['acceptance']:
                raise Refused('evidence_missing')
            if adapter.proof(run, expected['attempt'], request['acceptance']) != proof:
                raise Refused('evidence_missing')
        return dict(plan, identity=current, status='finished', reason=None)

    def apply(self, request, plan, reserved):
        """Intención y reserva con fsync ANTES del RPC; nunca un segundo envío."""
        group, op = plan['group'], request['op']
        expected = request['expected']
        reservation = dict(reserved) if reserved else dict(
            launcher=request['launcher'], ref=request['ref'], action_id=request['action_id'],
            group=group, task_id=expected['task_id'], mode=request['mode'],
            identity=list(self.stable(expected)), state='held', stashed=False, at=self.engine.clock())
        pending = self.reply(request, 'unknown', 'rpc_unresolved', plan['identity'] if request['acceptance'] else None,
                             'held')
        self.save(request, group, 'intent', pending, reservation, rpc=plan['rpc'][0])
        self.boundary('intent')
        outcome = 'ok'
        try:
            self.engine.pueue.call(*plan['rpc'])
        except subprocess.CalledProcessError:
            outcome = 'failed'  # el CLI terminó y fue recogido; su mensaje no queda en vuelo
        except (subprocess.TimeoutExpired, OSError, ValueError, TimeoutError):
            outcome = 'lost'  # respuesta perdida o hijo sobreviviente: efecto incierto
        self.boundary('rpc')
        try:
            after = self.engine.pueue.status()['tasks'].get(str(expected['task_id']))
            with self.engine.connect() as db:
                generation = self.engine.generation(self.engine.state(db))
            observed = self.project(after, expected['task_id'], generation, expected['attempt']) if after else None
            if observed is not None and self.stable(observed) != self.stable(expected):
                observed = None
                raise ValueError('identity')
        except (Refused, OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError):
            observed, outcome = 'unreadable', 'lost'
        status, reason, identity = self.classify(request, outcome, observed)
        if status == 'held':
            reservation['stashed'] = True
        if status == 'rejected':
            reservation = dict(reservation, state='released') if not reserved else reservation
        held = reservation['state'] == 'held'
        response = self.reply(request, status, reason, identity, 'held' if held else 'none')
        self.save(request, group, 'done', response, reservation, rpc=plan['rpc'][0])
        self.boundary('receipt')
        return response

    def classify(self, request, outcome, observed):
        op = request['op']
        unknown = ('unknown', 'rpc_unresolved', None)
        if observed == 'unreadable':
            return unknown
        state = observed['state'] if observed is not None else 'Absent'
        if op == 'hold':
            if state == 'Stashed' and observed['enqueue_at'] is None:
                return 'held', None, observed
            if outcome == 'failed' and state in ('Running', 'Paused', 'Done'):
                return 'rejected', 'stale_state', observed  # la carrera ganó; stash no aplica
            return unknown
        if op == 'cancel-no-payload':
            if (state in ('Running', 'Done') and observed['enqueued_at'] >= request['acceptance']['accepted_at']):
                return 'started', None, observed
            return unknown
        if op in ('signal', 'signal-manual'):
            if outcome == 'ok' and state in ('Running', 'Paused', 'Done'):
                return 'signalled', None, observed
            return unknown
        if op in ('retire-held', 'retire-interrupted'):
            if state == 'Absent':
                return 'retired', None, self.absent(request['expected'])
            return unknown
        return unknown


def receipt_digest(response):
    """Recibo que el lanzador cita como prueba `interrupted` de su propia retirada."""
    return sha(response, 'pueue-client-receipt@1')


def read_request(stream):
    """Lectura acotada a MAX_BYTES+1 antes de decodificar."""
    raw = stream.read(contract.MAX_BYTES + 1)
    if len(raw) > contract.MAX_BYTES:
        raise Invalid('size')
    return contract.parse(raw)
