"""One directory lock, one WAL ledger, and detached read snapshots.

Existing ledgers are inspected through a copy before opening any SQLite writer.
Transactions serialize keys, claims, drain epochs, and operator audit decisions.
"""
import contextlib
import fcntl
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import time
import uuid

from . import inputs
from .model import APP_ID, SCHEMA_VERSION, Fault, dump, load, now

MAX_DATABASE = 256 * 1024 * 1024
SCHEMA = (
    "CREATE TABLE authority (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
    "id TEXT NOT NULL, uid INTEGER NOT NULL, revision INTEGER NOT NULL)",
    "CREATE TABLE control (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
    "drained INTEGER NOT NULL, epoch INTEGER NOT NULL, drain_id TEXT, revision INTEGER NOT NULL)",
    "CREATE TABLE slot (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
    "attempt TEXT, generation INTEGER NOT NULL)",
    "CREATE TABLE preparations (id TEXT PRIMARY KEY, owner TEXT NOT NULL, status TEXT NOT NULL, "
    "detail TEXT)",
    "CREATE TABLE jobs (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, "
    "uid INTEGER NOT NULL, key TEXT, attempt TEXT UNIQUE NOT NULL, capsule TEXT NOT NULL, "
    "content TEXT NOT NULL, execution TEXT NOT NULL, input TEXT NOT NULL, client TEXT NOT NULL, "
    "submitted_by TEXT NOT NULL, admission TEXT NOT NULL, UNIQUE(uid,key))",
    "CREATE TABLE attempts (id TEXT PRIMARY KEY, job TEXT UNIQUE NOT NULL REFERENCES jobs(id), "
    "state TEXT NOT NULL, generation INTEGER, reservation TEXT, dispatcher TEXT, supervisor TEXT, "
    "gate TEXT NOT NULL, child TEXT, detail TEXT, started_at TEXT, ended_at TEXT, "
    "command TEXT, closure TEXT, closure_body TEXT, recorded_outcome TEXT, collection TEXT NOT NULL, "
    "publication TEXT NOT NULL, evidence_root TEXT, disposition TEXT, capacity_resolution TEXT, "
    "relevant_revision INTEGER NOT NULL, enqueue_only INTEGER NOT NULL)",
    "CREATE TABLE transitions (seq INTEGER PRIMARY KEY AUTOINCREMENT, revision INTEGER NOT NULL, "
    "job TEXT NOT NULL, attempt TEXT NOT NULL, from_state TEXT, to_state TEXT NOT NULL, "
    "actor TEXT NOT NULL, cause TEXT NOT NULL, generation INTEGER, at TEXT NOT NULL)",
    "CREATE TABLE operations (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, "
    "request TEXT NOT NULL, receipt TEXT NOT NULL, exit_code INTEGER NOT NULL, error TEXT)",
    "CREATE TABLE proofs (id TEXT PRIMARY KEY, body TEXT NOT NULL, sha256 TEXT NOT NULL)",
    "CREATE TABLE audit (seq INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL)",
)


def lock(fd, mode=fcntl.LOCK_EX):
    deadline = time.monotonic() + 10
    while True:
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise Fault("state_lock_timeout", 3, "unknown") from None
            time.sleep(0.02)


def state_path(explicit=None):
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    path = Path(os.path.abspath(Path(explicit or os.environ.get("FLEET_STATE_DIR") or
                                    base / "fleet-pilot").expanduser()))
    inputs._path_limit(path)
    for root in (base, Path.home() / ".local/state"):
        root = root.resolve()
        for name in ("pao", "mini-carga", "u3"):
            reserved = root / name
            if path == reserved or reserved in path.parents:
                raise Fault("reserved_state_directory")
    return path


def check_files(state, privacy=True):
    for name in ("state.db", "state.db-wal", "state.db-shm", "attempts", "capsules"):
        path = state / name
        if os.path.lexists(path):
            info = path.lstat()
            kind = stat.S_ISDIR if name in ("attempts", "capsules") else stat.S_ISREG
            if (not kind(info.st_mode) or info.st_uid != os.getuid() or (privacy and info.st_mode & 0o077)
                    or (kind == stat.S_ISREG and info.st_nlink != 1)):
                raise Fault("unsafe_state")


def temp_base(state):
    # Never honor reader TMPDIR/TEMP/TMP. No SQLite sidecars in the source.
    base = Path("/tmp").resolve(strict=True)
    original = state.stat()
    for path in inputs._ancestors(base):
        with inputs.directory(path) as fd:
            info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == (original.st_dev, original.st_ino):
            raise Fault("unsafe_reader_temp_base")
        if info.st_uid != 0 or (info.st_mode & 0o022 and
                                (path != base or not info.st_mode & stat.S_ISVTX)):
            raise Fault("unsafe_reader_temp_base")
    return base


def snapshot(state):
    copy = tempfile.TemporaryDirectory(prefix="fleet-read-", dir=temp_base(state))
    try:
        with inputs.directory(state) as fd:
            for name in ("state.db", "state.db-wal"):
                if name.endswith("-wal") and not os.path.lexists(state / name):
                    continue
                with inputs.open_file(fd, name) as source:
                    data, _ = inputs.read_stable(source, limit=MAX_DATABASE)
                inputs.write_new(Path(copy.name) / name, data)
        database = Path(copy.name) / "state.db"
        conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True,
                               timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn, copy
    except BaseException:
        copy.cleanup()
        raise


def compatible(conn):
    if (conn.execute("PRAGMA application_id").fetchone()[0] != APP_ID or
            conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION):
        raise Fault("unsupported_schema")


class Ledger:
    def __init__(self, state, create=False, initialize=True):
        self.state = state
        self.fd = self.conn = self.copy = None
        self.writer = create
        try:
            # For existing incompatible state, this walk only opens directories.
            with inputs.directory(state, create=create and initialize) as fd:
                self.fd = os.dup(fd)
                info = os.fstat(fd)
                if info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise Fault("state_directory_not_private")
            lock(self.fd, fcntl.LOCK_EX if create else fcntl.LOCK_SH)
            with inputs.directory(state) as check:
                info2 = os.fstat(check)
                if (info.st_dev, info.st_ino) != (info2.st_dev, info2.st_ino):
                    raise Fault("state_directory_changed")
                try:
                    fcntl.flock(check, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    fcntl.flock(check, fcntl.LOCK_UN)
                    raise Fault("exclusive_directory_lock_required")
            check_files(state, privacy=False)
            exists = os.path.lexists(state / "state.db")
            if exists:
                copied, temporary = snapshot(state)
                try:
                    compatible(copied)
                    check_files(state)
                    authority = copied.execute("SELECT * FROM authority").fetchone()
                    if authority is None or authority["uid"] != os.getuid():
                        raise Fault("operator_unauthorized")
                except BaseException:
                    copied.close()
                    temporary.cleanup()
                    raise
                if not create:
                    self.conn, self.copy = copied, temporary
                else:
                    copied.close()
                    temporary.cleanup()
            elif not create or not initialize:
                raise Fault("state_not_found")
            elif any(state.iterdir()):
                raise Fault("state_not_empty")
            if create:
                if not exists:
                    inputs.write_new(state / "state.db", b"")
                    inputs.fsync_dir(state)
                self.conn = sqlite3.connect(str(state / "state.db"), timeout=10,
                                             isolation_level=None)
                self.conn.row_factory = sqlite3.Row
                if self.conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                    raise Fault("wal_required")
                self.conn.execute("PRAGMA synchronous=FULL")
                self.conn.execute("PRAGMA foreign_keys=ON")
                if self.conn.execute("PRAGMA synchronous").fetchone()[0] != 2:
                    raise Fault("full_sync_required")
                if not exists:
                    self.conn.execute("BEGIN IMMEDIATE")
                    self.conn.execute("PRAGMA application_id={}".format(APP_ID))
                    self.conn.execute("PRAGMA user_version={}".format(SCHEMA_VERSION))
                    for statement in SCHEMA:
                        self.conn.execute(statement)
                    self.conn.execute("INSERT INTO authority VALUES (1,?,?,0)",
                                      (str(uuid.uuid4()), os.getuid()))
                    self.conn.execute("INSERT INTO control VALUES (1,0,0,NULL,0)")
                    self.conn.execute("INSERT INTO slot VALUES (1,NULL,0)")
                    self.conn.commit()
                    for name in ("attempts", "capsules"):
                        (state / name).mkdir(mode=0o700)
                    inputs.fsync_dir(state)
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        except FileNotFoundError:
            self.close()
            raise Fault("state_not_found") from None
        except PermissionError:
            self.close()
            raise Fault("operator_unauthorized") from None
        except BaseException:
            self.close()
            raise

    def execute(self, sql, parameters=()):
        return self.conn.execute(sql, parameters)

    @property
    def authority(self):
        return dict(self.execute("SELECT id,uid,revision FROM authority").fetchone())

    @property
    def control(self):
        row = dict(self.execute("SELECT drained,epoch,drain_id,revision FROM control").fetchone())
        row["drained"] = bool(row["drained"])
        return row

    @contextlib.contextmanager
    def transaction(self):
        if not self.writer:
            raise RuntimeError("read_only")
        lock(self.fd)
        try:
            check_files(self.state)
            compatible(self.conn)
            self.conn.execute("BEGIN IMMEDIATE")
            yield self
            try:
                self.conn.commit()
            except sqlite3.Error:
                raise Fault("persistence_unconfirmed", 3, "unknown") from None
        except BaseException:
            self.conn.rollback()
            raise
        finally:
            fcntl.flock(self.fd, fcntl.LOCK_UN)

    def revision(self):
        self.execute("UPDATE authority SET revision=revision+1")
        return self.authority["revision"]

    def event(self, attempt, target, actor, cause):
        row = self.execute("SELECT * FROM attempts WHERE id=?", (attempt,)).fetchone()
        previous = row["state"] if row else None
        if target != previous and (previous, target) not in (
                ("queued", "starting"), ("starting", "running"),
                ("starting", "finished"), ("running", "finished")):
            raise RuntimeError("illegal_transition")
        revision = self.revision()
        self.execute("UPDATE attempts SET state=?,relevant_revision=? WHERE id=?",
                     (target, revision, attempt))
        self.execute("INSERT INTO transitions (revision,job,attempt,from_state,to_state,actor,cause,"
                     "generation,at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (revision, row["job"], attempt, previous, target, actor, cause,
                      row["generation"], now()))
        return revision

    def close(self):
        # Reader SQLite handles belong only to the detached copy. In particular,
        # an incompatible reader must not upgrade its shared preflight lock.
        if self.fd is not None and self.writer:
            lock(self.fd)
        try:
            if self.conn is not None:
                self.conn.close()
                self.conn = None
            if self.copy is not None:
                self.copy.cleanup()
                self.copy = None
        finally:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
