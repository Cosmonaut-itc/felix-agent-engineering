"""Ledger SQLite exclusivamente de sombra; creación sólo administrativa explícita."""
import json
import os
import sqlite3
from dataclasses import dataclass
from urllib.parse import quote
from .protocol import SCHEMA, CODES, canonical, digest, key
from .model import validate_view

APPLICATION_ID = 1212633939
SCHEMA_VERSION = 2


class StoreError(RuntimeError):
    pass


class Busy(StoreError):
    pass


class WriteUncertain(RuntimeError):
    pass


class ForeignOwner(Exception):
    def __init__(self, result):
        super().__init__('dueno o canal ajeno')
        self.result = result


@dataclass(frozen=True)
class UnrecordedResult:
    result: dict


class ShadowStore:
    def __init__(self, config):
        self.config = config
        self.path = config.state_dir / 'shadow.db'

    @classmethod
    def initialize(cls, config):
        store = cls(config)
        if config.state_dir.exists():
            raise StoreError('inicializacion exige directorio nuevo')
        config.state_dir.mkdir(mode=0o700, parents=False)
        fd = os.open(str(store.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        conn = sqlite3.connect(str(store.path))
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            store._durability(conn)
            conn.execute('PRAGMA application_id=%d' % APPLICATION_ID)
            conn.execute('PRAGMA user_version=2')
            conn.execute('CREATE TABLE metadata (store_id TEXT NOT NULL, schema_version INTEGER NOT NULL, kind TEXT NOT NULL)')
            conn.execute('INSERT INTO metadata VALUES (?, ?, ?)', (config.store_id, SCHEMA_VERSION, 'shadow'))
            conn.execute('CREATE TABLE host_view (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL, checksum TEXT NOT NULL)')
            conn.execute('CREATE TABLE operations (principal TEXT NOT NULL, operation_id TEXT NOT NULL, digest TEXT NOT NULL, result TEXT NOT NULL, auth_context TEXT NOT NULL, PRIMARY KEY(principal, operation_id))')
            conn.execute('CREATE TABLE audit (seq INTEGER PRIMARY KEY, principal TEXT NOT NULL, operation_id TEXT NOT NULL, payload TEXT NOT NULL)')
            view = dict(pending=[], reservations=[], generations={}, drained=config.drained)
            conn.execute('INSERT INTO host_view VALUES (1, ?, ?)', (canonical(view), digest(view)))
            conn.commit()
        finally:
            conn.close()
        fd = os.open(str(config.state_dir), os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)

    @staticmethod
    def _durability(conn):
        conn.execute('PRAGMA synchronous=FULL')
        conn.execute('PRAGMA fullfsync=ON')
        conn.execute('PRAGMA checkpoint_fullfsync=ON')
        conn.execute('PRAGMA foreign_keys=ON')

    def connect(self, writable=False):
        try:
            for path in (self.config.state_dir, self.path,
                         self.path.with_name('shadow.db-wal'), self.path.with_name('shadow.db-shm')):
                if path.is_symlink(): raise StoreError('symlink de estado')
            stat = self.config.state_dir.stat()
            if stat.st_uid != os.getuid() or stat.st_mode & 0o077: raise StoreError('directorio no privado')
            if self.path.stat().st_uid != os.getuid() or self.path.stat().st_mode & 0o077: raise StoreError('BD no privada')
            mode = 'rw' if writable else 'ro'
            conn = sqlite3.connect('file:' + quote(str(self.path)) + '?mode=' + mode, uri=True, timeout=0.2, isolation_level=None)
            try:
                if writable: self._durability(conn)
                if conn.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID: raise StoreError('application_id')
                if conn.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION: raise StoreError('schema')
                if conn.execute('PRAGMA journal_mode').fetchone()[0] != 'wal': raise StoreError('WAL requerido')
                if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok': raise StoreError('BD corrupta')
                metadata = conn.execute('SELECT store_id, schema_version, kind FROM metadata').fetchall()
                if metadata != [(self.config.store_id, SCHEMA_VERSION, 'shadow')]: raise StoreError('storeId/schema/sombra no coincide')
                self.load(conn)
                return conn
            except BaseException:
                conn.close()
                raise
        except sqlite3.OperationalError as exc:
            if 'locked' in str(exc).lower() or 'busy' in str(exc).lower(): raise Busy('escritor ocupado') from exc
            raise StoreError('estado SQLite no disponible') from exc
        except (OSError, sqlite3.DatabaseError, ValueError, TypeError, KeyError) as exc:
            raise StoreError('estado ausente/corrupto; no se recrea') from exc

    def load(self, conn):
        row = conn.execute('SELECT payload, checksum FROM host_view WHERE id=1').fetchone()
        if row is None: raise StoreError('vista ausente')
        try:
            view = json.loads(row[0])
            if digest(view) != row[1]: raise ValueError('checksum')
            validate_view(view, self.config)
            return view
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
            raise StoreError('vista no integra') from exc

    @staticmethod
    def _validate_operation(original, principal, op_id, view):
        if original is None: return
        try:
            operation_digest, raw_result, raw_context = original
            if len(operation_digest) != 64 or any(c not in '0123456789abcdef' for c in operation_digest):
                raise ValueError('digest de operacion')
            context = json.loads(raw_context)
            if (not isinstance(context, dict) or set(context) != {'principal', 'peer', 'anchor', 'channel'}
                    or context['principal'] != principal.split('\x00', 1)[0]):
                raise ValueError('autorizacion de operacion')
            result = json.loads(raw_result)
            canonical(result)
            if principal.endswith('\x00preparation'):
                if set(result) != {'pendingId'} or not any(p['id'] == result['pendingId'] for p in view['pending']):
                    raise ValueError('preparacion sin correlacion')
                return
            if (not isinstance(result, dict) or result['schema'] != SCHEMA or result['result'] != 'shadow'
                    or result['operationId'] != op_id or type(result['wouldGrant']) is not bool
                    or type(result['exitCode']) is not int or result['exitCode'] not in range(7)
                    or not isinstance(result['reasons'], list)):
                raise ValueError('resultado de operacion')
            for reason in result['reasons']:
                if reason['code'] not in CODES or type(reason['retryable']) is not bool or 'detail' not in reason:
                    raise ValueError('motivos de operacion')
            if 'simulationKey' in result:
                key(result['simulationKey'])
                if not any(r['key'] == result['simulationKey'] for r in view['reservations']):
                    raise ValueError('operacion sin reserva')
            if result['wouldGrant'] and 'simulationKey' not in result:
                raise ValueError('admision sin reserva')
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
            raise StoreError('operacion no integra') from exc

    def read(self):
        conn = self.connect()
        try:
            conn.execute('BEGIN')
            return self.load(conn)
        finally:
            conn.close()

    def mutate(self, principal, op_id, operation_digest, apply, world, auth_context=None):
        conn = self.connect(writable=True)
        commit_attempted = False
        write_started = False
        try:
            conn.execute('BEGIN IMMEDIATE')
            view = self.load(conn)
            original = conn.execute('SELECT digest, result, auth_context FROM operations WHERE principal=? AND operation_id=?', (principal, op_id)).fetchone()
            self._validate_operation(original, principal, op_id, view)
            world.checkpoint('before_write')
            foreign = None
            try:
                result = apply(view, original)
            except ForeignOwner as exc:
                foreign = exc
                result = UnrecordedResult(exc.result)
            record_operation = not isinstance(result, UnrecordedResult)
            if not record_operation:
                result = result.result
            write_started = True
            conn.execute('UPDATE host_view SET payload=?, checksum=? WHERE id=1', (canonical(view), digest(view)))
            if original is None and record_operation:
                conn.execute('INSERT INTO operations VALUES (?, ?, ?, ?, ?)', (principal, op_id, operation_digest, canonical(result), canonical(auth_context)))
            conn.execute('INSERT INTO audit(principal, operation_id, payload) VALUES (?, ?, ?)', (principal, op_id, canonical(result)))
            world.checkpoint('before_commit')
            commit_attempted = True
            try:
                conn.commit()
                world.checkpoint('after_commit')
            except Exception as exc:
                raise WriteUncertain('commit/ACK incierto; releer misma operacion') from exc
            if foreign is not None:
                raise foreign
            return result
        except sqlite3.OperationalError as exc:
            if not write_started and ('locked' in str(exc).lower() or 'busy' in str(exc).lower()):
                raise Busy('escritor ocupado') from exc
            raise WriteUncertain('resultado SQLite incierto') from exc
        except sqlite3.DatabaseError as exc:
            raise WriteUncertain('persistencia incierta') from exc
        finally:
            # La limpieza no debe ocultar el resultado incierto de escritura/commit.
            if not commit_attempted:
                try: conn.rollback()
                except Exception: pass
            try: conn.close()
            except Exception: pass
