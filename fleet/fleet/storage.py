"""One directory lock, one WAL ledger, and detached read snapshots.

Existing ledgers are inspected through a copy before opening any SQLite writer.
Transactions serialize keys, claims, drain epochs, and operator audit decisions.
"""
import contextlib
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
from .model import (APP_ID, PILOT_SCHEMA_VERSION, SCHEMA_VERSION, Fault, HostConfirmation, HostHeld,
                    HostIntent, dump, load, now)

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
# Schema 5 adds only host rounds. The CHECKs fix nullability per state; the
# partial index allows one non-terminal round (held retains) per attempt.
HOST_SCHEMA = (
    "CREATE TABLE host_rounds (attempt TEXT NOT NULL REFERENCES attempts(id), "
    "ordinal INTEGER NOT NULL CHECK(ordinal>=1), operation_id TEXT UNIQUE NOT NULL "
    "CHECK(length(operation_id) BETWEEN 1 AND 256), request_json TEXT NOT NULL, "
    "request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64), physical_host_id TEXT NOT NULL, "
    "executor_id TEXT NOT NULL, store_id TEXT NOT NULL, config_revision TEXT NOT NULL, "
    "requester_identity TEXT NOT NULL, supervisor_identity TEXT, state TEXT NOT NULL CHECK(state IN "
    "('prepared','outcome_unknown','confirmed','held','rejected','retired')), key_json TEXT, "
    "receipt_json TEXT, confirmation_source TEXT CHECK(confirmation_source IN ('reply','same-channel-replay')), "
    "reason TEXT, heartbeat_seq INTEGER NOT NULL DEFAULT 0 CHECK(heartbeat_seq>=0), "
    "PRIMARY KEY(attempt,ordinal), "
    "CHECK((supervisor_identity IS NULL)=(state IN ('prepared','retired'))), "
    "CHECK((key_json IS NULL)=(confirmation_source IS NULL)), "
    "CHECK(state NOT IN ('prepared','outcome_unknown','retired') OR "
    "(key_json IS NULL AND receipt_json IS NULL)), "
    "CHECK(state<>'confirmed' OR (key_json IS NOT NULL AND receipt_json IS NOT NULL AND reason IS NULL)), "
    "CHECK(state<>'rejected' OR (key_json IS NULL AND receipt_json IS NOT NULL)), "
    "CHECK(state<>'held' OR reason IS NOT NULL))",
    "CREATE UNIQUE INDEX host_rounds_open ON host_rounds(attempt) "
    "WHERE state NOT IN ('rejected','retired')",
)


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
            or int.from_bytes(header[60:64], "big") not in (PILOT_SCHEMA_VERSION, SCHEMA_VERSION)):
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
            conn.execute("PRAGMA user_version").fetchone()[0] not in (PILOT_SCHEMA_VERSION, SCHEMA_VERSION)):
        raise Fault("unsupported_schema")


def migrate(conn):
    """Writer-only, under the exclusive directory lock: v4 gains host rounds, jobs stay byte-identical.

    An owned slot defers it: its supervisor may be a v4-only runtime that could not record its closure in v5.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        if (conn.execute("PRAGMA user_version").fetchone()[0] == PILOT_SCHEMA_VERSION
                and conn.execute("SELECT attempt FROM slot").fetchone()[0] is None):
            for statement in HOST_SCHEMA:
                conn.execute(statement)
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


HOST_TERMINAL = ("rejected", "retired")


def request_sha256(request):
    return hashlib.sha256(dump(request).encode("utf-8")).hexdigest()


def decode_host(row):
    """Every read revalidates the intent and its correlation; incoherence retains, never dispatches."""
    try:
        request = load(row["request_json"])
        intent = HostIntent(row["attempt"], row["ordinal"], row["operation_id"], request,
                            row["physical_host_id"], row["executor_id"], row["store_id"],
                            row["config_revision"], load(row["requester_identity"]), row["state"],
                            load(row["supervisor_identity"]), load(row["receipt_json"]), row["reason"])
        key = load(row["key_json"])
        if (dump(request) != row["request_json"] or request_sha256(request) != row["request_sha256"]
                or row["operation_id"] != host_operation_id(row["attempt"], row["ordinal"])
                or (request["operationId"], request["attemptId"], request["executorId"],
                    request["physicalHostId"]) != (row["operation_id"], row["attempt"], row["executor_id"],
                                                    row["physical_host_id"])
                or (key is not None and (set(key) != {"storeId", "reservationId", "generation"}
                                         or key["storeId"] != row["store_id"]
                                         or not str(key["reservationId"]).startswith("e_")
                                         or type(key["generation"]) is not int or key["generation"] < 1))):
            raise ValueError("host_round")
        if intent.receipt is not None:
            host_receipt(row, key, intent.receipt)
    except (KeyError, TypeError, ValueError):
        raise Fault("host_correlation_invalid", 3, "unknown") from None
    if row["state"] == "confirmed":
        return HostConfirmation(intent, key, intent.receipt, row["confirmation_source"], intent.supervisor)
    if row["state"] == "held":
        return HostHeld(intent, row["reason"], intent.supervisor, key, intent.receipt, row["confirmation_source"])
    return intent


def host_receipt(row, key, receipt):
    """The stored reply is effective evidence of this operation and means what its state claims."""
    from hostgate.protocol import validate_result
    validate_result(receipt, "effective", row["store_id"])
    replayed = "replayed" in receipt
    original, current = (receipt["originalResult"], receipt["currentState"]) if replayed else (receipt, receipt)
    grant = (original["granted"] and original["exitCode"] == 0 and not original["reasons"] and current is not None
             and current["state"] == "reserved" and not current["reasons"]
             and original["reservationKey"] == key == receipt.get("reservationKey")
             and row["confirmation_source"] == ("same-channel-replay" if replayed else "reply"))
    denial = (not replayed and not receipt["granted"] and receipt["exitCode"] == 1 and receipt["reasons"]
              and "reservationKey" not in receipt)
    ok = {"confirmed": grant, "rejected": denial}.get(row["state"], key in (None, receipt.get("reservationKey")))
    if not ok or (receipt["operationId"], receipt["configRevision"]) != (row["operation_id"], row["config_revision"]):
        raise ValueError("host_receipt")


def host_operation_id(attempt, ordinal):
    # Stable per (attempt, ordinal): a retry repeats it, a new round never reuses it.
    return "fleet:{}:{}".format(attempt, ordinal)


def host_rounds(db, attempt):
    return [decode_host(row) for row in
            db.execute("SELECT * FROM host_rounds WHERE attempt=? ORDER BY ordinal", (attempt,))]


def put_host_intent(db, intent):
    db.execute("INSERT INTO host_rounds (attempt,ordinal,operation_id,request_json,request_sha256,"
               "physical_host_id,executor_id,store_id,config_revision,requester_identity,state) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,'prepared')",
               (intent.attempt, intent.ordinal, intent.operation_id, dump(intent.request),
                request_sha256(intent.request), intent.physical_host_id, intent.executor_id,
                intent.store_id, intent.config_revision, dump(intent.requester)))
    db.revision()


def _cas(db, intent, sources, target, assignments="", parameters=()):
    changed = db.execute("UPDATE host_rounds SET state=?" + assignments + " WHERE attempt=? AND ordinal=? "
                         "AND state IN (" + ",".join("?" * len(sources)) + ")",
                         (target,) + tuple(parameters) + (intent.attempt, intent.ordinal) + tuple(sources)).rowcount
    if changed:
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
    return _cas(db, intent, ("outcome_unknown", "confirmed"), "held",
                ",reason=?,key_json=COALESCE(?,key_json),receipt_json=COALESCE(?,receipt_json),"
                "confirmation_source=COALESCE(?,confirmation_source)",
                (reason, None if key is None else dump(key), None if receipt is None else dump(receipt), source))
