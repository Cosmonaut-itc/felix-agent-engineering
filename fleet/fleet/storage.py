"""One directory lock, one WAL ledger, and detached read snapshots.

Existing ledgers are inspected through a copy before opening any SQLite writer.
Transactions serialize keys, claims, drain epochs, and operator audit decisions.
"""
import contextlib
from dataclasses import replace
import fcntl
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import struct
import tempfile
import time
import uuid

from . import inputs
from .model import (APP_ID, HOST_SCHEMA_VERSION, PILOT_SCHEMA_VERSION, SCHEMA_VERSION, Closure, Creation, Fault,
                    HostConfirmation, HostCreation, HostHeld, HostIntent, Step, dump, load, now, opaque, safe_queued)

MAX_DATABASE = 256 * 1024 * 1024
# Readers accept every version a writer can still migrate; none of them migrates.
VERSIONS = (PILOT_SCHEMA_VERSION, HOST_SCHEMA_VERSION, SCHEMA_VERSION)
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
# Schema 5 added host rounds; schema 6 rebuilds them with the creation phases and
# creation_json. SQL constrains part of the state shape; every read enforces the
# complete shape. The partial index allows one open round (held retains) per attempt.
HOST_TABLE = (
    "CREATE TABLE host_rounds (attempt TEXT NOT NULL REFERENCES attempts(id), "
    "ordinal INTEGER NOT NULL CHECK(ordinal>=1), operation_id TEXT UNIQUE NOT NULL "
    "CHECK(length(operation_id) BETWEEN 1 AND 256), request_json TEXT NOT NULL, "
    "request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64), physical_host_id TEXT NOT NULL, "
    "executor_id TEXT NOT NULL, store_id TEXT NOT NULL, config_revision TEXT NOT NULL, "
    "requester_identity TEXT NOT NULL, supervisor_identity TEXT, state TEXT NOT NULL CHECK(state IN "
    "('prepared','outcome_unknown','confirmed','held','rejected','retired','authorizing','authorized',"
    "'spawning','running','closing','released','closed_unused')), key_json TEXT, "
    "receipt_json TEXT, confirmation_source TEXT CHECK(confirmation_source IN ('reply','same-channel-replay')), "
    "reason TEXT, heartbeat_seq INTEGER NOT NULL DEFAULT 0 CHECK(heartbeat_seq>=0), creation_json TEXT, "
    "PRIMARY KEY(attempt,ordinal), "
    "CHECK((supervisor_identity IS NULL)=(state IN ('prepared','retired'))), "
    "CHECK((key_json IS NULL)=(confirmation_source IS NULL)), "
    "CHECK(state NOT IN ('prepared','outcome_unknown','retired') OR "
    "(key_json IS NULL AND receipt_json IS NULL)), "
    "CHECK(state<>'confirmed' OR (key_json IS NOT NULL AND receipt_json IS NOT NULL AND reason IS NULL)), "
    "CHECK(state<>'rejected' OR (key_json IS NULL AND (receipt_json IS NOT NULL OR creation_json IS NOT NULL))), "
    "CHECK(state NOT IN ('held','closed_unused') OR reason IS NOT NULL), "
    "CHECK(state IN ('held','rejected') OR "
    "(creation_json IS NULL)=(state IN ('prepared','outcome_unknown','confirmed','retired'))), "
    "CHECK(state NOT IN ('authorizing','authorized','spawning','running','closing','released') OR "
    "(key_json IS NOT NULL AND receipt_json IS NOT NULL AND reason IS NULL)))"
)
HOST_INDEX = ("CREATE UNIQUE INDEX host_rounds_open ON host_rounds(attempt) "
              "WHERE state NOT IN ('rejected','retired','released','closed_unused')")
HOST_SCHEMA = (HOST_TABLE, HOST_INDEX)
HOST_COLUMNS = ("attempt,ordinal,operation_id,request_json,request_sha256,physical_host_id,executor_id,"
                "store_id,config_revision,requester_identity,supervisor_identity,state,key_json,receipt_json,"
                "confirmation_source,reason,heartbeat_seq")


def lock(fd, mode=fcntl.LOCK_EX, timeout=10):
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if deadline is not None and time.monotonic() >= deadline:
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


def writer_header(state):
    """Inspect committed page one before SQLite can mutate an existing ledger.

    Writers must not depend on the reader's full disk copy (e.g. RLIMIT_FSIZE).
    The directory EX lock excludes Fleet writers/checkpoints. Validate WAL
    checksums/salts and use only committed frames, including a fresh ledger
    whose schema still lives entirely in WAL. SQLite validates the actual
    schema and authority again after opening. Authority is checked from the
    committed pages first, without a disk copy or opening SQLite on the source.
    """
    with inputs.directory(state) as fd:
        with inputs.open_file(fd, "state.db") as source:
            main, _ = inputs.read_stable(source, limit=MAX_DATABASE)
        wal = b""
        if os.path.lexists(state / "state.db-wal"):
            with inputs.open_file(fd, "state.db-wal") as source:
                wal, _ = inputs.read_stable(source, limit=MAX_DATABASE)
    header = main[:100]
    committed, pending = {}, {}
    if wal:
        if len(wal) < 32:
            raise Fault("state_unreadable")
        magic, version, page_size = struct.unpack(">III", wal[:12])
        if magic not in (0x377f0682, 0x377f0683) or version != 3007000:
            raise Fault("state_unreadable")
        if page_size < 512 or page_size > 65536 or page_size & (page_size - 1):
            raise Fault("state_unreadable")
        order = "<" if magic == 0x377f0682 else ">"

        def checksum(data, sums=(0, 0)):
            words = struct.unpack(order + str(len(data) // 4) + "I", data)
            first, second = sums
            for index in range(0, len(words), 2):
                first = (first + words[index] + second) & 0xffffffff
                second = (second + words[index + 1] + first) & 0xffffffff
            return first, second

        sums = checksum(wal[:24])
        if sums != struct.unpack(">II", wal[24:32]):
            raise Fault("state_unreadable")
        candidate = header
        for offset in range(32, len(wal) - page_size - 23, page_size + 24):
            frame = wal[offset:offset + 24]
            page = wal[offset + 24:offset + 24 + page_size]
            if frame[8:16] != wal[16:24]:
                break  # Stale tail after a WAL restart is outside this epoch.
            next_sums = checksum(page, checksum(frame[:8], sums))
            if next_sums != struct.unpack(">II", frame[16:24]):
                break  # SQLite also stops recovery at the first invalid frame.
            sums = next_sums
            number, commit = struct.unpack(">II", frame[:8])
            if number == 0:
                raise Fault("state_unreadable")
            pending[number] = page
            if number == 1:
                candidate = page[:100]
            if commit:
                header = candidate
                committed.update(pending)
                pending.clear()
    if (len(header) < 100 or header[:16] != b"SQLite format 3\x00"
            or int.from_bytes(header[68:72], "big") != APP_ID
            or int.from_bytes(header[60:64], "big") not in VERSIONS):
        raise Fault("unsupported_schema")
    writer_authority(main, committed, header)


def writer_authority(main, committed, header):
    """Read only schema/authority b-trees from the validated committed image.

    A writer must reject another UID before SQLite can recover/checkpoint WAL.
    Do not require a full temporary file: admitted writes must still work when
    RLIMIT_FSIZE prevents the reader snapshot. SQLite revalidates after preflight.
    Only schema and authority pages are decoded; malformed layouts fail closed
    before opening the original.
    """
    page_size = int.from_bytes(header[16:18], "big")
    if page_size == 1:
        page_size = 65536
    count = int.from_bytes(header[28:32], "big")
    encoding = {1: "utf-8", 2: "utf-16le", 3: "utf-16be"}.get(
        int.from_bytes(header[56:60], "big"))
    if (page_size < 512 or page_size > 65536 or page_size & (page_size - 1)
            or not encoding or not 0 < count <= MAX_DATABASE // page_size):
        raise Fault("state_unreadable")

    def page(number):
        if not 0 < number <= count:
            raise ValueError("invalid page")
        data = committed.get(number, main[(number - 1) * page_size:number * page_size])
        if len(data) != page_size:
            raise ValueError("truncated page")
        return data

    def varint(data, offset):
        value = 0
        for index in range(9):
            byte = data[offset]
            offset += 1
            value = (value << (8 if index == 8 else 7)) | (byte if index == 8 else byte & 127)
            if index == 8 or byte < 128:
                return value, offset
        raise ValueError("invalid varint")

    def records(root):
        todo, seen = [root], set()
        while todo:
            number = todo.pop()
            if number in seen:
                raise ValueError("cyclic b-tree")
            seen.add(number)
            data = page(number)
            offset = 100 if number == 1 else 0
            kind, cells = data[offset], int.from_bytes(data[offset + 3:offset + 5], "big")
            if kind not in (5, 13):
                raise ValueError("not a table b-tree")
            pointers = offset + (12 if kind == 5 else 8)
            if pointers + 2 * cells > page_size:
                raise ValueError("invalid cell count")
            if kind == 5:
                todo.append(int.from_bytes(data[offset + 8:offset + 12], "big"))
            for index in range(cells):
                start = int.from_bytes(data[pointers + 2 * index:pointers + 2 * index + 2], "big")
                if start < pointers + 2 * cells or start >= page_size:
                    raise ValueError("invalid cell")
                if kind == 5:
                    todo.append(int.from_bytes(data[start:start + 4], "big"))
                    continue
                size, start = varint(data, start)
                rowid, start = varint(data, start)
                usable = page_size - header[20]
                local = size
                if size > usable - 35:
                    minimum = (usable - 12) * 32 // 255 - 23
                    local = minimum + (size - minimum) % (usable - 4)
                    if local > usable - 35:
                        local = minimum
                if size > MAX_DATABASE or start + local > usable:
                    raise ValueError("invalid payload size")
                payload = data[start:start + local]
                overflow = int.from_bytes(data[start + local:start + local + 4], "big") if local < size else 0
                chain = set()
                while len(payload) < size:
                    if overflow in chain:
                        raise ValueError("cyclic overflow")
                    chain.add(overflow)
                    extra = page(overflow)
                    payload += extra[4:4 + min(size - len(payload), usable - 4)]
                    overflow = int.from_bytes(extra[:4], "big")
                end, cursor = varint(payload, 0)
                types = []
                while cursor < end:
                    serial, cursor = varint(payload, cursor)
                    types.append(serial)
                if cursor != end:
                    raise ValueError("invalid record header")
                values = []
                for serial in types:
                    width = ((0, 1, 2, 3, 4, 6, 8, 8, 0, 0)[serial] if serial < 10
                             else (serial - 12) // 2)
                    if width < 0 or cursor + width > len(payload):
                        raise ValueError("invalid record field")
                    field = payload[cursor:cursor + width]
                    cursor += width
                    if serial == 0:
                        value = None
                    elif 1 <= serial <= 6:
                        value = int.from_bytes(field, "big", signed=True)
                    elif serial in (8, 9):
                        value = serial - 8
                    elif serial >= 12:
                        value = field.decode(encoding) if serial % 2 else field
                    else:
                        raise ValueError("unsupported preflight field")
                    values.append(value)
                if cursor != len(payload):
                    raise ValueError("invalid record size")
                yield rowid, values

    try:
        tables = [values for _, values in records(1)
                  if len(values) == 5 and values[:3] == ["table", "authority", "authority"]]
        if (len(tables) != 1 or type(tables[0][3]) is not int
                or tables[0][4] != SCHEMA[0]):
            raise ValueError("invalid authority schema")
        authority = list(records(tables[0][3]))
        if (len(authority) != 1 or authority[0][0] != 1 or len(authority[0][1]) != 4
                or type(authority[0][1][2]) is not int or authority[0][1][2] != os.getuid()):
            raise Fault("operator_unauthorized")
    except (IndexError, UnicodeError, ValueError, TypeError) as exc:
        if isinstance(exc, Fault):
            raise
        raise Fault("state_unreadable") from None


def compatible(conn):
    if (conn.execute("PRAGMA application_id").fetchone()[0] != APP_ID or
            conn.execute("PRAGMA user_version").fetchone()[0] not in VERSIONS):
        raise Fault("unsupported_schema")


def migrate(conn):
    """Writer-only, under the exclusive directory lock: v4 gains host rounds, v5 rebuilds them for v6.

    Jobs and every v5 round column stay byte-identical. An owned slot defers it: its
    supervisor may be an older runtime that could not record its closure in v6.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION and conn.execute("SELECT attempt FROM slot").fetchone()[0] is None:
            if version == PILOT_SCHEMA_VERSION:
                for statement in HOST_SCHEMA:
                    conn.execute(statement)
            elif version == HOST_SCHEMA_VERSION:
                # The new table keeps the name and SQL of a fresh v6 ledger; the v5 one goes with its index.
                conn.execute("ALTER TABLE host_rounds RENAME TO host_rounds_v5")
                conn.execute(HOST_TABLE)
                conn.execute("INSERT INTO host_rounds ({0}) SELECT {0} FROM host_rounds_v5 ORDER BY rowid"
                             .format(HOST_COLUMNS))
                conn.execute("DROP TABLE host_rounds_v5")
                conn.execute(HOST_INDEX)
            conn.execute("PRAGMA user_version={}".format(SCHEMA_VERSION))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


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
                if create:
                    writer_header(state)
                    check_files(state)
                else:
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
                    self.conn, self.copy = copied, temporary
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
                if exists:
                    compatible(self.conn)
                    authority = self.execute("SELECT * FROM authority").fetchone()
                    if authority is None or authority["uid"] != os.getuid():
                        raise Fault("operator_unauthorized")
                if self.conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                    raise Fault("wal_required")
                self.conn.execute("PRAGMA synchronous=FULL")
                self.conn.execute("PRAGMA foreign_keys=ON")
                if self.conn.execute("PRAGMA synchronous").fetchone()[0] != 2:
                    raise Fault("full_sync_required")
                if exists:
                    migrate(self.conn)
                if not exists:
                    self.conn.execute("BEGIN IMMEDIATE")
                    self.conn.execute("PRAGMA application_id={}".format(APP_ID))
                    self.conn.execute("PRAGMA user_version={}".format(SCHEMA_VERSION))
                    for statement in SCHEMA + HOST_SCHEMA:
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
        if self.fd is not None and self.writer and self.conn is not None:
            lock(self.fd, timeout=None)
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


HOST_TERMINAL = ("rejected", "retired", "released", "closed_unused")
# Phases with a live creation; held keeps the one it was held from.
CREATION_PHASES = ("authorizing", "authorized", "spawning", "running", "closing")


def request_sha256(request):
    return hashlib.sha256(dump(request).encode("utf-8")).hexdigest()


# Presence of supervisor, key, receipt, source, reason, creation, in that order.
GRANTED = (True, True, True, True, False, True)
HOST_SHAPES = {
    "prepared": {(False, False, False, False, False, False)},
    "outcome_unknown": {(True, False, False, False, False, False)},
    "confirmed": {(True, True, True, True, False, False)},
    "rejected": {(True, False, True, False, False, False), (True, False, False, False, True, True)},
    "held": {(True, False, False, False, True, False), (True, False, True, False, True, False),
             (True, True, True, True, True, False), (True, True, True, True, True, True)},
    "retired": {(False, False, False, False, False, False), (False, False, False, False, True, False)},
    "released": {GRANTED},
    "closed_unused": {(True, True, True, True, True, True)},
    **{phase: {GRANTED} for phase in CREATION_PHASES},
}
# Fields of creation_json per phase. Steps follow authorize, consume, start; release may
# follow any prefix. Each prefix step but the last carries its receipt. closing may keep hostgate's
# original closure while the local release is still pending.
_PREFIXES = ((), ("authorize",), ("authorize", "consume"), ("authorize", "consume", "start"))
CREATION_SHAPES = {
    "authorizing": {("authorize",)},
    "authorized": {("authorize",)},
    "spawning": {("authorize", "consume"), ("authorize", "consume", "start")},
    "running": {("authorize", "consume", "start")},
    "closing": {prefix + ("release",) + proof for prefix in _PREFIXES for proof in ((), ("closure",))},
    "released": {prefix + ("release", "closure") for prefix in _PREFIXES},
    "closed_unused": {("closure",), ("release", "closure")},
    "rejected": {("discovered",)},
}
OPERATIONS = {"authorize": "authorize_creation", "consume": "consume_creation", "start": "record_started",
              "release": "soltar"}
STEPS = ("authorize", "consume", "start", "release")


def creation_operation_id(attempt, ordinal, step, seq=1):
    """One ID per step of a round; only soltar repeats with a new seq after a busy observation."""
    return "{}:{}:{}".format(host_operation_id(attempt, ordinal), step, seq)


def creation_confirmation(intent, source):
    """The admission authorize_creation repeats: this round's grant, never another."""
    return {"operationId": intent.operation_id, "requestDigest": request_sha256(intent.request),
            "attemptId": intent.attempt, "source": source}


def creation_json(creation):
    value = {"phase": creation.phase}
    for name in STEPS:
        step = getattr(creation, name)
        if step is not None:
            value[name] = {"request": step.request, "receipt": step.receipt}
    if creation.closure is not None:
        value["closure"] = {"permit": creation.closure.permit, "evidence": creation.closure.evidence}
    if creation.discovered is not None:
        value["discovered"] = creation.discovered
    return dump(value)


def decode_creation(row, intent, key, raw):
    """Exact variant of the row's phase: every request and receipt correlated with this round."""
    value = host_json(raw)
    state = row["state"]
    phase = value["phase"]
    if (state == "held" and phase not in CREATION_PHASES) or (state != "held" and phase != state):
        raise ValueError("creation phase")
    present = tuple(name for name in STEPS + ("closure", "discovered") if name in value)
    if set(value) != {"phase"} | set(present) or present not in CREATION_SHAPES[phase]:
        raise ValueError("creation variant")
    steps = {}
    for name in present:
        if name in STEPS:
            if set(value[name]) != {"request", "receipt"}:
                raise ValueError("creation step")
            steps[name] = Step(value[name]["request"], value[name]["receipt"])
    creation = Creation(phase, closure=None if "closure" not in value else Closure(**value["closure"]),
                        discovered=value.get("discovered"), **steps)
    if phase == "rejected":
        _discovered(row, intent, creation.discovered)
        return creation
    prefix = [name for name in ("authorize", "consume", "start") if name in steps]
    for position, name in enumerate(prefix):
        receipt = steps[name].receipt
        last = position == len(prefix) - 1
        _creation_request(row, intent, key, creation, name)
        if receipt is None:
            if not last or phase in ("authorized", "running") or (phase == "spawning" and name == "authorize"):
                raise ValueError("missing creation receipt")
        else:
            _creation_receipt(row, key, creation, name, receipt)
    if phase == "authorizing" and steps["authorize"].receipt is not None:
        raise ValueError("authorizing with its receipt")
    if phase == "spawning" and "start" in steps and steps["start"].receipt is not None:
        raise ValueError("spawning with its start receipt")
    if "release" in steps:
        _creation_request(row, intent, key, creation, "release")
        if steps["release"].receipt is not None:
            positive = _creation_receipt(row, key, creation, "release", steps["release"].receipt)
            if positive and creation.closure is not None and steps["release"].receipt["permit"] != creation.closure.permit:
                raise ValueError("positive release of another closure")
    if creation.closure is not None:
        _closure(row, key, creation)
    return creation


def _permit(creation):
    receipt = creation.authorize and creation.authorize.receipt
    return None if receipt is None else (receipt.get("originalResult") or receipt)["permit"]


def _creation_request(row, intent, key, creation, name):
    from hostgate.protocol import digest, validate
    request = getattr(creation, name).request
    validate(request, OPERATIONS[name])
    prefix = creation_operation_id(row["attempt"], row["ordinal"], name, "")
    seq = request["operationId"][len(prefix):]
    if (not request["operationId"].startswith(prefix) or not seq.isdigit() or seq != str(int(seq))
            or (name != "release" and seq != "1") or int(seq) < 1 or request["key"] != key):
        raise ValueError("creation request identity")
    permit = _permit(creation)
    if name == "authorize":
        if request["confirmation"] != creation_confirmation(intent, row["confirmation_source"]):
            raise ValueError("confirmation of another grant")
    elif name == "release":
        if request["evidenceIds"] != []:
            raise ValueError("soltar names its own evidence")
    elif (permit is None or request["permitId"] != permit["permitId"]
          or (name == "consume" and request["launchDigest"] != digest(creation.authorize.request["launch"]))):
        raise ValueError("creation request permit")


def _creation_receipt(row, key, creation, name, receipt):
    """Original reply of a step; for soltar, True when it is the positive closure."""
    from hostgate.protocol import validate_result
    validate_result(receipt, "effective", row["store_id"], OPERATIONS[name])
    replayed = "replayed" in receipt
    original = receipt["originalResult"] if replayed else receipt
    request = getattr(creation, name).request
    if (receipt["operationId"] != request["operationId"] or original["operationId"] != request["operationId"]
            or receipt["configRevision"] != row["config_revision"] or receipt.get("reservationKey") != key
            or original.get("reservationKey") != key or (not replayed and {"originalResult", "currentState"} & set(receipt))):
        raise ValueError("creation receipt correlation")
    permit, view = _permit(creation), original.get("permit") or {}
    expected = {"authorize": ("reserved", "issued"), "consume": ("reserved", "consumed"),
                "start": ("running", "registered")}
    if name in expected:
        if (original["exitCode"] != 0 or original["reasons"] or (original["state"], view.get("state")) != expected[name]
                or receipt["exitCode"] != 0 or receipt["reasons"]):
            raise ValueError("not an original creation success")
        if name == "consume" and (replayed or receipt.get("creationAllowed") is not True):
            raise ValueError("consume without its original creationAllowed")
        if name == "start" and (not view["scopeReceipt"].startswith("scp_")
                                or receipt.get("permit", {}).get("scopeReceipt") != view["scopeReceipt"]):
            raise ValueError("record_started without its scope receipt")
    elif original["exitCode"] == 0 and original["state"] == "released" and not original["reasons"]:
        if view.get("state") not in ("closed", "sealed") or not receipt["exitCode"] == 0:
            raise ValueError("not a positive closure")
    elif (original["exitCode"] != 1 or receipt["exitCode"] != 1 or not original["reasons"]
          or original["state"] not in ("reserved", "running")):
        raise ValueError("soltar neither busy nor released")
    elif creation.phase not in ("closing", "released"):
        raise ValueError("busy soltar outside closing")
    if view and permit is not None and any(view.get(field) != permit[field] for field in
                                           ("permitId", "issuerEpoch", "profileRevision", "baselineDigest")):
        raise ValueError("permit of another authorization")
    return name == "release" and original["state"] == "released"


def _closure(row, key, creation):
    """hostgate's positive closure, correlated in permit, root and baseline before any copy."""
    from hostgate import scope
    from hostgate.protocol import digest
    permit, evidence = creation.closure.permit, creation.closure.evidence
    proof = scope.evidence_from_json(evidence)
    obs = proof.observation
    if (proof.signed().evidence_digest != evidence["evidence_digest"] or permit.get("closedBy") != proof.evidence_digest
            or proof.provenance != scope.REAL_PROVENANCE or obs is None
            or proof.issuer != scope.issuer(row["store_id"], obs.issuer_epoch) or proof.key != key
            or proof.scope != key["reservationId"] or obs.boot != proof.boot
            or not (proof.complete and proof.creators_sealed and proof.resources_clean)):
        raise ValueError("closure not integral")
    authorized, issued = _permit(creation), creation.authorize
    if obs.permit_id is None:
        if authorized is not None or set(permit) != {"state", "closedBy"}:
            raise ValueError("closure without the authorized permit")
    else:
        if (issued is None or obs.confirmation_digest != digest(issued.request["confirmation"])
                or obs.launch_digest != digest(issued.request["launch"])
                or (obs.permit_id, obs.issuer_epoch, obs.profile_revision, obs.baseline_digest) !=
                (permit.get("permitId"), permit.get("issuerEpoch"), permit.get("profileRevision"), permit.get("baselineDigest"))
                or (authorized is not None and any(permit.get(field) != authorized[field] for field in
                                                   ("permitId", "issuerEpoch", "profileRevision", "baselineDigest")))):
            raise ValueError("closure of another permit or baseline")
    started = creation.start
    if proof.kind == "unix-scope-empty":
        if (started is None or started.receipt is None or permit["state"] != "closed"
                or permit.get("scopeReceipt") != started.receipt["permit"]["scopeReceipt"]
                or obs.root is None or (obs.root["pid"], obs.root["unique"]) != (started.request["root"]["pid"],
                                                                                started.request["root"]["unique"])
                or not (proof.root_terminal and proof.scope_empty and proof.lineage_complete and proof.profile_verified)):
            raise ValueError("closure of another root")
    elif proof.kind == "never-created":
        consumed = creation.consume is not None and creation.consume.receipt is not None
        if (started is not None or consumed or permit["state"] != "sealed" or obs.root is not None
                or not (proof.permit_revoked and proof.no_external_effects)):
            raise ValueError("never-created after creation")
    else:
        raise ValueError("closure kind")


def _discovered(row, intent, pending):
    """estado's pending entry of this exact round: a durable rejection that never reserved."""
    if (not isinstance(pending, dict) or pending.get("operationId") != row["operation_id"]
            or pending.get("requestDigest") != row["request_sha256"] or pending.get("attemptId") != row["attempt"]
            or pending.get("jobId") != intent.request["jobId"] or pending.get("state") != "rejected"
            or pending.get("effectiveState") != "rejected" or pending.get("reservationKey") is not None
            or not pending.get("reasons")):
        raise ValueError("not this round's durable rejection")


def host_json(raw):
    if raw is None:
        return None
    value = load(raw)
    if value is None or dump(value) != raw:
        raise ValueError("absent or noncanonical JSON")
    return value


def host_identity(value):
    if (not isinstance(value, dict) or set(value) !=
            {"pid", "uid", "host", "namespace", "boot_id", "birth", "alive"}
            or type(value["pid"]) is not int or value["pid"] < 1
            or type(value["uid"]) is not int or value["uid"] < 0
            or not isinstance(value["host"], str) or not value["host"]
            or type(value["alive"]) is not bool
            or any(value[k] is not None and (not isinstance(value[k], str) or not value[k])
                   for k in ("namespace", "boot_id", "birth"))):
        raise ValueError("native identity")


def decode_host(row, job):
    """Interpret columns, require the complete state shape, then correlate every read."""
    from hostgate.protocol import key as validate_key, validate, validate_result
    try:
        request, requester, supervisor, key, receipt = (
            host_json(row[column]) for column in
            ("request_json", "requester_identity", "supervisor_identity", "key_json", "receipt_json"))
        validate(request, "reservar")
        host_identity(requester)
        if supervisor is not None:
            host_identity(supervisor)
        for column in ("store_id", "config_revision", "physical_host_id", "executor_id"):
            if not isinstance(row[column], str) or not row[column]:
                raise ValueError("empty host text")
        for column, minimum in (("ordinal", 1), ("heartbeat_seq", 0)):
            if type(row[column]) is not int or row[column] < minimum:
                raise ValueError("host integer")
        if key is not None:
            validate_key(key)
            if key["storeId"] != row["store_id"] or not key["reservationId"].startswith("e_"):
                raise ValueError("host key")
        if receipt is not None:
            validate_result(receipt, "effective", row["store_id"])
        source, reason = row["confirmation_source"], row["reason"]
        if ((source is not None and source not in ("reply", "same-channel-replay"))
                or (reason is not None and (not isinstance(reason, str) or not reason))):
            raise ValueError("source or reason")
        # A v5 row read before its migration has no creation_json column.
        raw = row["creation_json"] if "creation_json" in row.keys() else None
        presence = tuple(value is not None for value in (supervisor, key, receipt, source, reason, raw))
        if presence not in HOST_SHAPES.get(row["state"], set()):
            raise ValueError("host state shape")
        if (request_sha256(request) != row["request_sha256"]
                or row["operation_id"] != host_operation_id(row["attempt"], row["ordinal"])
                or (request["operationId"], request["attemptId"], request["executorId"],
                    request["physicalHostId"]) != (row["operation_id"], row["attempt"], row["executor_id"],
                                                    row["physical_host_id"])
                # No column repeats these two: only the attempt's job can contradict them.
                or request["jobId"] != job["id"]
                or request["profileId"] != load(job["content"])["execution"]["hostSpec"]["profileId"]):
            raise ValueError("host request correlation")
        if receipt is not None:
            host_receipt(row, key, receipt)
        intent = HostIntent(row["attempt"], row["ordinal"], row["operation_id"], request,
                            row["physical_host_id"], row["executor_id"], row["store_id"],
                            row["config_revision"], requester, row["state"], supervisor, receipt, reason)
        creation = None if raw is None else decode_creation(row, intent, key, raw)
        if row["state"] == "confirmed":
            return HostConfirmation(intent, key, receipt, source, supervisor)
        if row["state"] == "held":
            return HostHeld(intent, reason, supervisor, key, receipt, source, creation)
        if row["state"] == "rejected":
            return replace(intent, creation=creation)
        if creation is not None:
            return HostCreation(intent, key, receipt, source, supervisor, creation)
        return intent
    except (KeyError, IndexError, TypeError, ValueError, AttributeError, RecursionError, OverflowError):
        raise Fault("host_correlation_invalid", 3, "unknown") from None


def host_receipt(row, key, receipt):
    """Wire validation alone does not establish the durable state's meaning."""
    replayed = "replayed" in receipt
    original = receipt["originalResult"] if replayed else receipt
    if (receipt["operationId"] != row["operation_id"]
            or receipt["configRevision"] != row["config_revision"]
            or original["configRevision"] != row["config_revision"]):
        raise ValueError("host receipt correlation")
    if not replayed and {"originalResult", "currentState"} & set(receipt):
        raise ValueError("unmarked replay")
    expected = receipt.get("reservationKey")
    source = ("same-channel-replay" if replayed else "reply") if expected is not None else None
    if key != expected or row["confirmation_source"] != source:
        raise ValueError("host receipt key/source")
    if row["state"] in ("confirmed", "released") + CREATION_PHASES:
        current = receipt["currentState"] if replayed else receipt
        if (not original["granted"] or original["exitCode"] != 0 or original["reasons"]
                or original.get("state") != "reserved" or original.get("reservationKey") != key
                or receipt["exitCode"] != 0 or receipt["reasons"] or current is None
                or current["state"] != "reserved" or current["reasons"]):
            raise ValueError("not an integral grant")
    if row["state"] == "rejected":
        if (replayed or receipt["granted"] or receipt["exitCode"] != 1 or not receipt["reasons"]
                or "reservationKey" in receipt or "state" in receipt):
            raise ValueError("not an integral rejection")


def host_operation_id(attempt, ordinal):
    # Stable per (attempt, ordinal): a retry repeats it, a new round never reuses it.
    return "fleet:{}:{}".format(attempt, ordinal)


def host_rounds(db, attempt):
    job = db.execute("SELECT jobs.id, jobs.content FROM jobs JOIN attempts ON attempts.job=jobs.id "
                     "WHERE attempts.id=?", (attempt,)).fetchone()
    rounds = [decode_host(row, job) for row in
              db.execute("SELECT * FROM host_rounds WHERE attempt=? ORDER BY ordinal", (attempt,))]
    # The index allows one open round at any ordinal: a later round must not hide a retained one.
    for position, item in enumerate(rounds, 1):
        intent = item if isinstance(item, HostIntent) else item.intent
        if intent.ordinal != position or (position < len(rounds) and intent.state not in HOST_TERMINAL):
            raise Fault("host_correlation_invalid", 3, "unknown")
    return rounds


def put_host_intent(db, intent):
    db.execute("INSERT INTO host_rounds (attempt,ordinal,operation_id,request_json,request_sha256,"
               "physical_host_id,executor_id,store_id,config_revision,requester_identity,state) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,'prepared')",
               (intent.attempt, intent.ordinal, intent.operation_id, dump(intent.request),
                request_sha256(intent.request), intent.physical_host_id, intent.executor_id,
                intent.store_id, intent.config_revision, dump(intent.requester)))
    host_rounds(db, intent.attempt)
    db.revision()


def _cas(db, intent, sources, target, assignments="", parameters=(), creation=False):
    """creation, when given, is the exact creation_json the caller read (None: absent)."""
    guard = "" if creation is False else " AND creation_json IS ?"
    changed = db.execute("UPDATE host_rounds SET state=?" + assignments + " WHERE attempt=? AND ordinal=? "
                         "AND state IN (" + ",".join("?" * len(sources)) + ")" + guard,
                         (target,) + tuple(parameters) + (intent.attempt, intent.ordinal) + tuple(sources)
                         + (() if creation is False else (creation,))).rowcount
    if changed:
        host_rounds(db, intent.attempt)
        db.revision()
    return changed == 1


def mark_host_unknown(db, intent, supervisor):
    """The only path to sending: whoever loses this CAS must not call reservar."""
    return _cas(db, intent, ("prepared",), "outcome_unknown", ",supervisor_identity=?", (dump(supervisor),))


def retire_host_round(db, intent, reason):
    return _cas(db, intent, ("prepared",), "retired", ",reason=?", (reason,))


def reject_host_round(db, intent, receipt):
    return _cas(db, intent, ("outcome_unknown",), "rejected", ",receipt_json=?", (dump(receipt),))


def hold_host(db, intent, reason, key=None, receipt=None, source=None):
    """Never writes creation_json: held keeps the exact prefix of phase, receipts and permit."""
    return _cas(db, intent, ("outcome_unknown", "confirmed") + CREATION_PHASES, "held",
                ",reason=?,key_json=COALESCE(?,key_json),receipt_json=COALESCE(?,receipt_json),"
                "confirmation_source=COALESCE(?,confirmation_source)",
                (reason, None if key is None else dump(key), None if receipt is None else dump(receipt), source))


def confirm_host_and_claim(db, intent, key, receipt, source, dispatcher, supervisor):
    """One transaction: the round is confirmed and this exact attempt claimed, or the round is held."""
    row = db.execute("SELECT a.* FROM attempts a JOIN jobs j ON j.id=a.job "
                     "WHERE a.state='queued' ORDER BY j.seq LIMIT 1").fetchone()
    slot = db.execute("SELECT attempt,generation FROM slot").fetchone()
    if (db.control["drained"] or slot["attempt"] is not None or row is None
            or row["id"] != intent.attempt or not safe_queued(row)):
        if not hold_host(db, intent, "claim_unavailable", key, receipt, source):
            raise Fault("host_round_changed", 3, "unknown")
        return None
    if not _cas(db, intent, ("outcome_unknown",), "confirmed",
                ",key_json=?,receipt_json=?,confirmation_source=?", (dump(key), dump(receipt), source)):
        raise Fault("host_round_changed", 3, "unknown")
    generation = slot["generation"] + 1
    db.execute("UPDATE slot SET attempt=?,generation=?", (intent.attempt, generation))
    db.execute("UPDATE attempts SET generation=?,reservation=?,dispatcher=?,supervisor=?,"
               "disposition='retained' WHERE id=?",
               (generation, opaque("r"), dump(dispatcher), dump(supervisor), intent.attempt))
    db.event(intent.attempt, "starting", "dispatcher", "claim")
    return generation


# Creation steps, as the 4e supervisor will call them. Each CAS needs the same state and the same
# creation_json the caller read; the decoder then re-validates the whole variant before commit.
_BEGIN = {"authorize": (("confirmed",), "authorizing"), "consume": (("authorized",), "spawning"),
          "start": (("spawning",), "spawning"), "release": (("confirmed",) + CREATION_PHASES, "closing")}
_RECORD = {"authorize": ("authorizing", "authorized"), "consume": ("spawning", "spawning"),
           "start": ("spawning", "running"), "release": ("closing", "closing")}


def _creation_cas(db, current, sources, target, creation):
    previous = getattr(current, "creation", None)
    clear = ",reason=NULL" if target in ("released",) + CREATION_PHASES else ""  # a held source keeps no reason
    return _cas(db, current.intent, sources, target, ",creation_json=?" + clear,
                (creation_json(replace(creation, phase=target)),),
                None if previous is None else creation_json(previous))


def begin_step(db, current, name, request):
    """Persist a step's request before sending it; consume also closes the local gate, once."""
    sources, target = _BEGIN[name]
    base = current.creation if isinstance(current, HostCreation) else Creation(target)
    if not _creation_cas(db, current, sources, target, replace(base, **{name: Step(request, None)})):
        return False
    if name == "consume" and db.execute("UPDATE attempts SET gate='consumed' WHERE id=? AND gate='available' "
                                        "AND state='starting'", (current.intent.attempt,)).rowcount != 1:
        raise Fault("host_round_changed", 3, "unknown")
    return True


def record_step(db, current, name, receipt):
    """Persist the original reply of the pending step: running needs the root and the scope receipt."""
    source, target = _RECORD[name]
    step = replace(getattr(current.creation, name), receipt=receipt)
    return _creation_cas(db, current, (source,), target, replace(current.creation, **{name: step}))


def release_host(db, current, closure):
    """closing or held -> released: hostgate's original closure and the local release CAS together.

    An attempt that never reached its terminal ends incomplete; a command already recorded stays.
    """
    intent = current.intent
    row = db.execute("SELECT state,generation FROM attempts WHERE id=?", (intent.attempt,)).fetchone()
    if current.creation is None or not _creation_cas(db, current, ("closing", "held"), "released",
                                                     replace(current.creation, closure=closure)):
        return False
    if row["state"] != "finished":
        db.execute("UPDATE attempts SET gate='closed',recorded_outcome=COALESCE(recorded_outcome,'incomplete'),"
                   "collection=?,publication=?,ended_at=COALESCE(ended_at,?) WHERE id=?",
                   (dump({"state": "incomplete", "errors": ["host_released_before_terminal"]}),
                    dump({"state": "incomplete", "root_sha256": None}), now(), intent.attempt))
        db.event(intent.attempt, "finished", "host", "host_released_before_terminal")
    if db.execute("UPDATE slot SET attempt=NULL WHERE attempt=? AND generation=?",
                  (intent.attempt, row["generation"])).rowcount != 1:
        raise Fault("host_round_changed", 3, "unknown")
    db.execute("UPDATE attempts SET disposition='released' WHERE id=?", (intent.attempt,))
    db.event(intent.attempt, "finished", "host", "host_released")
    return True


def close_unused(db, current, closure, release=None):
    """A grant never claimed: held -> closed_unused only with hostgate's original sealed release.

    The attempt stays queued (queued -> finished is illegal) and a later round may compete.
    """
    row = db.execute("SELECT state FROM attempts WHERE id=?", (current.intent.attempt,)).fetchone()
    if row["state"] != "queued" or current.creation is not None:
        return False
    return _creation_cas(db, current, ("held",), "closed_unused",
                         Creation("closed_unused", release=release, closure=closure))


def reject_discovered(db, current, pending):
    """held without key -> rejected by estado's durable rejection of exactly this reservar."""
    return _creation_cas(db, current, ("held",), "rejected", Creation("rejected", discovered=pending))
