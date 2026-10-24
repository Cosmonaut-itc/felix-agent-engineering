"""Ledger SQLite exclusivamente de sombra; creación sólo administrativa explícita."""
import json
import os
import sqlite3
from urllib.parse import quote
from .protocol import canonical, digest
from .model import validate_view

APPLICATION_ID = 1212633939
SCHEMA_VERSION = 2


class StoreError(RuntimeError):
    pass


class Busy(StoreError):
    pass


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
            view = dict(pending=[], reservations=[], generations={}, drained=False)
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
        for path in (self.config.state_dir, self.path, self.path.with_name('shadow.db-wal'), self.path.with_name('shadow.db-shm')):
            if path.is_symlink(): raise StoreError('symlink de estado')
        try:
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

    def read(self):
        conn = self.connect()
        try:
            conn.execute('BEGIN')
            return self.load(conn)
        finally:
            conn.close()

    def mutate(self, principal, op_id, operation_digest, apply, world, auth_context=None):
        conn = self.connect(writable=True)
        committed = False
        try:
            conn.execute('BEGIN IMMEDIATE')
            view = self.load(conn)
            original = conn.execute('SELECT digest, result, auth_context FROM operations WHERE principal=? AND operation_id=?', (principal, op_id)).fetchone()
            world.checkpoint('before_write')
            result = apply(view, original)
            conn.execute('UPDATE host_view SET payload=?, checksum=? WHERE id=1', (canonical(view), digest(view)))
            if original is None:
                conn.execute('INSERT INTO operations VALUES (?, ?, ?, ?, ?)', (principal, op_id, operation_digest, canonical(result), canonical(auth_context)))
            conn.execute('INSERT INTO audit(principal, operation_id, payload) VALUES (?, ?, ?)', (principal, op_id, canonical(result)))
            world.checkpoint('before_commit')
            conn.commit()
            committed = True
            world.checkpoint('after_commit')
            return result
        except sqlite3.OperationalError as exc:
            if 'locked' in str(exc).lower() or 'busy' in str(exc).lower(): raise Busy('escritor ocupado') from exc
            raise OSError('resultado SQLite incierto') from exc
        except sqlite3.DatabaseError as exc:
            raise OSError('persistencia incierta') from exc
        finally:
            # La limpieza no debe ocultar el resultado incierto de escritura/commit.
            if not committed:
                try: conn.rollback()
                except sqlite3.Error: pass
            try: conn.close()
            except sqlite3.Error: pass
