import ctypes
import enum
import fcntl
import getpass
import json
import os
from pathlib import Path
import select
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid

if __package__:
    from . import evidence, inputs
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from fleet import evidence, inputs

APP_ID = 0x464C5431
SCHEMA_VERSION = 3
GROUP_DRAINING = "waiting for process group to empty"
StateError = sqlite3.Error
LEGAL = {(None, "queued"), ("queued", "starting"),
         ("starting", "running"), ("starting", "finished"),
         ("running", "finished")}
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS jobs (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
    "id TEXT UNIQUE NOT NULL, execution TEXT NOT NULL, input TEXT NOT NULL, policy TEXT NOT NULL, "
    "client TEXT NOT NULL, "
    "submitted_by TEXT NOT NULL, submitted_at REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, job_id TEXT UNIQUE NOT NULL "
    "REFERENCES jobs(id), n INTEGER NOT NULL DEFAULT 1, state TEXT, outcome TEXT, "
    "exit_code INTEGER, signal INTEGER, supervisor TEXT, child_pid INTEGER, "
    "started_at REAL, ended_at REAL, detail TEXT, generation INTEGER, "
    "gate_consumed INTEGER NOT NULL DEFAULT 0, command TEXT, collection TEXT, evidence_root TEXT)",
    "CREATE TABLE IF NOT EXISTS transitions (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
    "job_id TEXT NOT NULL, attempt_id TEXT NOT NULL, from_state TEXT, to_state TEXT NOT NULL, "
    "actor TEXT NOT NULL, at REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS slot (id INTEGER PRIMARY KEY CHECK(id=1), "
    "holder_attempt TEXT, generation INTEGER NOT NULL)",
    "INSERT OR IGNORE INTO slot VALUES (1, NULL, 0)",
)


def _lock(fd, operation=fcntl.LOCK_EX):
    deadline = time.monotonic() + 10
    while True:
        try:
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise ValueError("state_lock_timeout") from None
            time.sleep(0.02)


class Ledger(sqlite3.Connection):
    lock_fd = None
    locked = False
    snapshot = None

    def execute(self, sql, parameters=()):
        if self.lock_fd is not None and sql.startswith("BEGIN"):
            _lock(self.lock_fd)
            self.locked = True
            if (super().execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION
                    or super().execute("PRAGMA application_id").fetchone()[0] != APP_ID):
                raise ValueError("unsupported Fleet pilot schema; no migration")
        return super().execute(sql, parameters)

    def commit(self):
        super().commit()
        if self.locked:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            self.locked = False

    def close(self):
        if self.lock_fd is not None and not self.locked:
            _lock(self.lock_fd)
        try:
            super().close()
        finally:
            if self.lock_fd is not None:
                os.close(self.lock_fd)
                self.lock_fd = None
            if self.snapshot is not None:
                self.snapshot.cleanup()
                self.snapshot = None


def state_path(explicit=None):
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    raw = Path(explicit or os.environ.get("FLEET_STATE_DIR") or base / "fleet-pilot").expanduser()
    path = Path(os.path.abspath(raw))
    for root in (base.resolve(), (Path.home() / ".local/state").resolve()):
        for name in ("pao", "mini-carga", "u3"):
            reserved = root / name
            if path == reserved or reserved in path.parents:
                raise ValueError("reserved_state_directory")
    return path


def _connect(state, create=False):
    try:
        with inputs.directory(state, create=create) as lock_fd:
            directory = os.fstat(lock_fd)
            if (directory.st_uid != os.getuid() or stat.S_IMODE(directory.st_mode) & 0o077):
                raise ValueError("state directory must be private to the current user")
            if create:
                _lock(lock_fd)
                with inputs.directory(state) as probe:
                    if (os.fstat(probe).st_dev, os.fstat(probe).st_ino) != (directory.st_dev, directory.st_ino):
                        raise ValueError("state_directory_changed")
                    try:
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        pass
                    else:
                        raise ValueError("state_lock_not_exclusive")
            else:
                _lock(lock_fd, fcntl.LOCK_SH)
            conn = _connect_locked(state, create)
            if create:
                conn.lock_fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY | inputs.NOFOLLOW)
            return conn
    except FileNotFoundError:
        raise ValueError("state_not_found") from None


def _connect_locked(state, create):
    database = state / "state.db"
    for name in ("state.db", "state.db-wal", "state.db-shm", "attempts", "capsules"):
        path = state / name
        if os.path.lexists(path):
            info = path.lstat()
            expected = stat.S_ISDIR if name in ("attempts", "capsules") else stat.S_ISREG
            if not expected(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("state files must be private, owned and without symlinks")
    fresh = not database.exists()
    if create and fresh:
        if any(state.iterdir()):
            raise ValueError("state directory must be empty or belong to Fleet")
        inputs.write_new(database, b"")
        inputs.fsync_dir(state)
    if create:
        conn = sqlite3.connect(str(database), timeout=10, isolation_level=None, factory=Ledger)
    else:
        # SQLite's readonly WAL readers can update shared memory. Copy under the
        # transaction lock so public reads never create or repair ledger files.
        snapshot = tempfile.TemporaryDirectory(prefix="fleet-read-")
        try:
            with inputs.directory(state) as fd:
                for name in ("state.db", "state.db-wal"):
                    if name.endswith("-wal") and not os.path.lexists(state / name):
                        continue
                    with inputs.open_file(fd, name) as source:
                        data, _ = inputs.read_stable(source, limit=inputs.MAX_BYTES)
                    inputs.write_new(Path(snapshot.name) / name, data)
            copied = Path(snapshot.name) / "state.db"
            conn = sqlite3.connect(copied.as_uri() + "?mode=ro", uri=True,
                                   timeout=10, isolation_level=None, factory=Ledger)
            conn.snapshot = snapshot
        except BaseException:
            snapshot.cleanup()
            raise
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        app = conn.execute("PRAGMA application_id").fetchone()[0]
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if not (create and fresh) and (app != APP_ID or version != SCHEMA_VERSION):
            raise ValueError("unsupported Fleet pilot schema; no migration")
        if create:
            if conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise ValueError("wal_required")
            conn.execute("PRAGMA synchronous=FULL")
            if conn.execute("PRAGMA synchronous").fetchone()[0] != 2:
                raise ValueError("full_sync_required")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("PRAGMA application_id={}".format(APP_ID))
            for statement in SCHEMA:
                conn.execute(statement)
            conn.execute("PRAGMA user_version={}".format(SCHEMA_VERSION))
            conn.commit()
            for name in ("attempts", "capsules"):
                (state / name).mkdir(mode=0o700, exist_ok=True)
            inputs.fsync_dir(state)
        else:
            conn.execute("PRAGMA query_only=ON")
        return conn
    except BaseException:
        conn.close()
        raise


def _transition(conn, attempt, target, actor):
    row = conn.execute("SELECT state, job_id FROM attempts WHERE id=?", (attempt,)).fetchone()
    if row is None or (row["state"], target) not in LEGAL:
        raise RuntimeError("illegal attempt transition to {}".format(target))
    conn.execute("UPDATE attempts SET state=? WHERE id=?", (target, attempt))
    conn.execute("INSERT INTO transitions (job_id,attempt_id,from_state,to_state,actor,at) "
                 "VALUES (?,?,?,?,?,?)",
                 (row["job_id"], attempt, row["state"], target, actor, time.time()))


def _claim(conn, attempt, actor, supervisor=None):
    changed = conn.execute("UPDATE slot SET holder_attempt=?,generation=generation+1 "
                           "WHERE id=1 AND holder_attempt IS NULL", (attempt,)).rowcount
    if not changed:
        return False
    _transition(conn, attempt, "starting", actor)
    generation = conn.execute("SELECT generation FROM slot WHERE id=1").fetchone()[0]
    conn.execute("UPDATE attempts SET generation=?,supervisor=? WHERE id=?",
                 (generation, json.dumps(supervisor) if supervisor else None, attempt))
    return generation


def _finish_and_next(conn, attempt, generation, actor, command, root, collection,
                     detail, supervisor=None):
    holder = conn.execute("SELECT holder_attempt,generation FROM slot WHERE id=1").fetchone()
    row = conn.execute("SELECT supervisor FROM attempts WHERE id=?", (attempt,)).fetchone()
    owner = json.dumps(supervisor) if supervisor else None
    if tuple(holder) != (attempt, generation) or row[0] != owner:
        raise RuntimeError("attempt does not own slot")
    kind = command["kind"]
    code, sig = command.get("exit_code"), command.get("signal")
    outcome = "incomplete"
    if root and collection["collection"] == "valid":
        if kind == "exited":
            outcome = "succeeded" if code == 0 else "failed"
        elif kind == "never_created":
            outcome = "failed"
    _transition(conn, attempt, "finished", actor)
    conn.execute("UPDATE attempts SET outcome=?,exit_code=?,signal=?,ended_at=?,detail=?,"
                 "command=?,collection=?,evidence_root=? WHERE id=?",
                 (outcome, code, sig, time.time(), detail, json.dumps(command),
                  json.dumps({"state": collection["collection"], "errors": collection["errors"]}), root, attempt))
    conn.execute("UPDATE slot SET holder_attempt=NULL WHERE id=1")
    row = conn.execute("SELECT a.id FROM attempts a JOIN jobs j ON j.id=a.job_id "
                       "WHERE a.state='queued' ORDER BY j.seq LIMIT 1").fetchone()
    if row:
        return row[0], _claim(conn, row[0], actor, supervisor)
    return None


def _observe_identity(pid):
    result = {"pid": pid, "host": socket.gethostname(), "boot_id": None, "started_at": None}
    try:
        if sys.platform.startswith("linux"):
            result["boot_id"] = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            fields = Path("/proc/{}/stat".format(pid)).read_text().rsplit(")", 1)[1].split()
            if fields[0] in ("Z", "X"):
                return result
            result["started_at"] = fields[19]
        elif sys.platform == "darwin":
            result["boot_id"] = subprocess.check_output(
                ["/usr/sbin/sysctl", "-n", "kern.boottime"], timeout=2,
                stderr=subprocess.DEVNULL, text=True).strip()
            # libproc's BSD info includes microseconds; ps lstart truncates to
            # seconds and is insufficient to rule out rapid PID reuse.
            class BSDInfo(ctypes.Structure):
                _fields_ = [("prefix", ctypes.c_uint32 * 12),
                            ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
                            ("suffix", ctypes.c_uint32 * 6),
                            ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64)]
            info = BSDInfo()
            lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            query = lib.proc_pidinfo
            query.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                              ctypes.c_void_p, ctypes.c_int]
            query.restype = ctypes.c_int
            size = ctypes.sizeof(info)
            if query(pid, 3, 0, ctypes.byref(info), size) == size and info.prefix[1] != 5:
                result["started_at"] = "{}:{}".format(info.start_sec, info.start_usec)
    except (OSError, ValueError, IndexError, AttributeError, subprocess.SubprocessError):
        pass
    return result


def _identity_confirmed(identity):
    if not identity or not identity.get("boot_id") or not identity.get("started_at"):
        return False
    return identity == _observe_identity(identity["pid"])


class Launch(enum.Enum):
    READY = "ready"
    NOT_CREATED = "not created"
    UNCONFIRMED = "unconfirmed"


def _reserve(conn, job, attempt, execution, identity, policy, client):
    actor = "client:" + client
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO jobs (id,execution,input,policy,client,submitted_by,submitted_at) "
                 "VALUES (?,?,?,?,?,?,?)", (job, json.dumps(execution), json.dumps(identity),
                 json.dumps(policy), client,
                 json.dumps({"user": getpass.getuser(), "uid": os.getuid(),
                             "host": socket.gethostname(), "pid": os.getpid()}), time.time()))
    conn.execute("INSERT INTO attempts (id,job_id) VALUES (?,?)", (attempt, job))
    _transition(conn, attempt, "queued", actor)
    claimed = _claim(conn, attempt, actor)
    conn.commit()
    return job, attempt, claimed


def _launch_supervisor(state, attempt, generation):
    read_fd = write_fd = None
    created = False
    launch, error = Launch.NOT_CREATED, None
    try:
        read_fd, write_fd = os.pipe()
        # Popen's exec-error pipe proves exec succeeded; readiness below also
        # proves Python imported the runner and persisted the supervisor.
        log_fd = os.open(state / "supervisor.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT | inputs.NOFOLLOW, 0o600)
        with os.fdopen(log_fd, "ab", buffering=0) as log:
            info = os.fstat(log.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise OSError("invalid_supervisor_log")
            subprocess.Popen(
                [sys.executable, "-I", "-B", str(Path(__file__).resolve()),
                 "supervise", str(state), attempt, str(generation), str(write_fd)],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=True, close_fds=True, pass_fds=(write_fd,),
                env={"PATH": os.defpath, "LANG": "C"})
            created = True
        fd, write_fd = write_fd, None
        os.close(fd)
        try:
            readable, _, _ = select.select([read_fd], [], [], 10)
            ready = bool(readable) and os.read(read_fd, 1) == b"R"
        except ValueError:
            launch, error = Launch.UNCONFIRMED, "supervisor_readiness_unconfirmed"
        else:
            launch = Launch.READY if ready else Launch.UNCONFIRMED
            if not ready:
                error = "supervisor readiness unconfirmed; slot retained; do not retry"
    except OSError:
        launch = Launch.UNCONFIRMED if created else Launch.NOT_CREATED
        error = "supervisor spawn failed" if not created else "supervisor_readiness_unconfirmed"
    finally:
        for fd in (read_fd, write_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    if created:
                        launch, error = Launch.UNCONFIRMED, "supervisor_pipe_close_failed"
    return launch, error


def _fail_and_hand_off(conn, state, attempt, generation, actor, error):
    while attempt:
        row = conn.execute("SELECT j.id,j.input FROM jobs j JOIN attempts a ON a.job_id=j.id "
                           "WHERE a.id=?", (attempt,)).fetchone()
        command = {"kind": "never_created", "reason": "supervisor_spawn_failed"}
        root, collection = evidence.seal(state / "attempts" / attempt, row[0], attempt,
                                         json.loads(row[1]), command)
        conn.execute("BEGIN IMMEDIATE")
        dispatch = _finish_and_next(conn, attempt, generation, actor, command, root, collection, error)
        conn.commit()
        if dispatch is None:
            return
        attempt, generation = dispatch
        launch, error = _launch_supervisor(state, attempt, generation)
        if launch is not Launch.NOT_CREATED:
            return


def submit(state, argv, cwd, client, includes=(), private=(), artifacts=(), credentials=()):
    if os.name != "posix":
        raise ValueError("pilot supports POSIX hosts only")
    env = dict(os.environ)
    plan = inputs.Plan(cwd, state, includes, private, artifacts, argv, env, credentials)
    client = inputs.redact(client, plan.secrets)
    conn = _connect(state, create=True)
    try:
        job, attempt = "j_" + uuid.uuid4().hex, "a_" + uuid.uuid4().hex
        identity = plan.capture(state / "capsules" / job)
        (state / "attempts" / attempt).mkdir(mode=0o700)
        inputs.fsync_dir(state / "attempts")
        execution = {"contract": "fleet-execution@1", "input": identity, "argv": argv,
                     "environment": env, "outputs": {"contract": "fleet-outputs@1", "required": plan.artifacts},
                     "context_version": inputs.CONTEXT_VERSION, "credential_names": list(credentials)}
        job, attempt, claimed = _reserve(conn, job, attempt, execution, identity, plan.policy, client)
        result = {"job": job, "attempt": attempt, "client": client, "registered": True,
                  "accepted": False, "input": identity, "input_policy": plan.policy}
        if not claimed:
            uncertain = view(state, job)["view"] == "uncertain"
            return dict(result, state="queued", view="uncertain" if uncertain else "queued", accepted=True), 3 if uncertain else 0
        launch, error = _launch_supervisor(state, attempt, claimed)
        if launch is Launch.READY:
            return dict(result, state="starting", accepted=True), 0
        if launch is Launch.UNCONFIRMED:
            return dict(result, state="starting", view="uncertain", error=error), 3
        _fail_and_hand_off(conn, state, attempt, claimed, "client:" + client, error)
        return dict(result, state="finished", outcome="failed", error=error), 1
    finally:
        conn.close()


def view(state, job):
    conn = _connect(state)
    try:
        conn.execute("BEGIN")
        row = conn.execute("SELECT j.seq,j.id,j.input,j.policy,j.client,j.submitted_by,j.submitted_at,"
                           "a.id AS attempt,a.state,a.outcome,a.exit_code,a.signal,"
                           "a.supervisor,a.child_pid,a.started_at,a.ended_at,a.detail,a.command,a.collection,a.evidence_root "
                           "FROM jobs j JOIN attempts a ON a.job_id=j.id WHERE j.id=?", (job,)).fetchone()
        if row is None:
            raise ValueError("unknown_job")
        result = dict(row)
        result.pop("id")
        result["job"] = job
        for key in ("input", "policy", "submitted_by", "supervisor", "command", "collection"):
            result[key] = json.loads(result[key]) if result[key] else None
        result["input_policy"] = result.pop("policy")
        result["registered"] = True
        result["interruptible"] = False
        result["view"] = result["state"]
        owner = conn.execute("SELECT a.supervisor,a.detail FROM slot s LEFT JOIN attempts a "
                             "ON a.id=s.holder_attempt WHERE s.id=1").fetchone()
        if result["state"] != "finished":
            identity = json.loads(owner[0]) if owner and owner[0] else None
            if not _identity_confirmed(identity) or (owner and owner[1] == GROUP_DRAINING):
                result["view"] = "uncertain"
        result["transitions"] = [
            {"seq": t["seq"], "job": t["job_id"], "attempt": t["attempt_id"],
             "from": t["from_state"], "to": t["to_state"], "actor": t["actor"], "at": t["at"]}
            for t in conn.execute("SELECT * FROM transitions WHERE job_id=? ORDER BY seq", (job,))]
        result.update(stdout_log=None, stderr_log=None)
        if result["state"] == "finished":
            result["recorded_outcome"] = result["outcome"]
            proof = evidence.verify(state / "attempts" / result["attempt"], result.pop("evidence_root"),
                                    job, result["attempt"], result["input"], result["command"])
            result["evidence"] = proof
            paths = {Path(f["path"]).name: f["path"] for f in proof["files"] if f["kind"] == "log"}
            result.update(stdout_log=paths.get("stdout"), stderr_log=paths.get("stderr"))
            if not proof["valid"]:
                result.update(outcome="incomplete", detail="evidence_invalid")
        else:
            result.pop("evidence_root")
            result["evidence"] = {"valid": False, "errors": ["attempt_not_finished"], "files": []}
        return result
    finally:
        conn.close()


def _group_may_be_occupied(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def _owned(conn, attempt, generation, identity):
    row = conn.execute("SELECT a.supervisor,a.generation,s.holder_attempt,s.generation "
                       "FROM attempts a CROSS JOIN slot s WHERE a.id=? AND s.id=1", (attempt,)).fetchone()
    if row is None or tuple(row) != (json.dumps(identity), generation, attempt, generation):
        raise RuntimeError("execution_owner_mismatch")


def _execute(conn, state, attempt, generation, actor, identity):
    row = conn.execute("SELECT j.id,j.input,j.execution FROM jobs j JOIN attempts a ON a.job_id=j.id "
                       "WHERE a.id=?", (attempt,)).fetchone()
    job, input_identity, execution = row[0], json.loads(row[1]), json.loads(row[2])
    directory = state / "attempts" / attempt
    command = {"kind": "never_created", "reason": "preparation_failed"}
    capture = None
    detail = "preparation_failed"
    materialized = False
    try:
        cwd = inputs.materialize(state / "capsules" / job, directory / "workspace", input_identity)
        materialized = True
        secrets = inputs.credential_values(execution["environment"], execution["credential_names"])
        capture = evidence.Capture(directory, cwd, execution["outputs"]["required"], secrets)
    except (OSError, ValueError, KeyError):
        detail = "preparation_failed" if materialized else "content_mismatch"
        command["reason"] = detail
        if capture:
            capture.close()
        root, collection = evidence.seal(directory, job, attempt, input_identity, command, capture,
                                         errors=(detail,))
        return command, root, collection, detail
    conn.execute("BEGIN IMMEDIATE")
    _owned(conn, attempt, generation, identity)
    changed = conn.execute("UPDATE attempts SET gate_consumed=1 WHERE id=? AND gate_consumed=0",
                           (attempt,)).rowcount
    if changed != 1:
        raise RuntimeError("execution_gate_consumed")
    conn.commit()
    try:
        child = subprocess.Popen(execution["argv"], cwd=cwd, env=execution["environment"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 close_fds=True, start_new_session=True)
    except OSError:
        capture.close()
        command = {"kind": "never_created", "reason": "command_spawn_failed"}
        detail = "command spawn failed"
    else:
        conn.execute("BEGIN IMMEDIATE")
        _owned(conn, attempt, generation, identity)
        _transition(conn, attempt, "running", actor)
        conn.execute("UPDATE attempts SET child_pid=?,started_at=? WHERE id=?",
                     (child.pid, time.time(), attempt))
        conn.commit()

        def draining():
            if _group_may_be_occupied(child.pid):
                conn.execute("BEGIN IMMEDIATE")
                _owned(conn, attempt, generation, identity)
                conn.execute("UPDATE attempts SET detail=? WHERE id=?", (GROUP_DRAINING, attempt))
                conn.commit()

        capture.observe(child, draining)
        returncode = child.wait()
        draining()
        while _group_may_be_occupied(child.pid):
            time.sleep(0.2)
        command = ({"kind": "signalled", "signal": -returncode} if returncode < 0 else
                   {"kind": "exited", "exit_code": returncode})
        detail = None
    root, collection = evidence.seal(directory, job, attempt, input_identity, command, capture)
    if collection["collection"] != "valid":
        detail = "evidence_collection_incomplete"
    return command, root, collection, detail


def supervise(state, attempt, generation, ready_fd):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    identity = _observe_identity(os.getpid())
    actor = "supervisor:{}".format(os.getpid())
    conn = _connect(state, create=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        holder = conn.execute("SELECT holder_attempt,generation FROM slot WHERE id=1").fetchone()
        if tuple(holder) != (attempt, generation):
            raise RuntimeError("supervisor does not own slot")
        changed = conn.execute("UPDATE attempts SET supervisor=? WHERE id=? AND generation=? "
                               "AND supervisor IS NULL AND gate_consumed=0",
                               (json.dumps(identity), attempt, generation)).rowcount
        if changed != 1:
            raise RuntimeError("supervisor_already_bound")
        conn.commit()
        try:
            os.write(ready_fd, b"R")
        except BrokenPipeError:
            pass
        finally:
            os.close(ready_fd)
        while attempt:
            command, root, collection, detail = _execute(conn, state, attempt, generation, actor, identity)
            conn.execute("BEGIN IMMEDIATE")
            dispatch = _finish_and_next(conn, attempt, generation, actor, command, root, collection, detail, identity)
            conn.commit()
            if dispatch is None:
                break
            attempt, generation = dispatch
    finally:
        conn.close()


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    if len(sys.argv) != 6 or sys.argv[1] != "supervise":
        raise SystemExit("internal supervisor entry point")
    try:
        supervise(Path(sys.argv[2]), sys.argv[3], int(sys.argv[4]), int(sys.argv[5]))
    except BaseException:
        raise SystemExit(3) from None
