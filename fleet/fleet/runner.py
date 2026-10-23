import ctypes
import enum
import getpass
import json
import os
from pathlib import Path
import select
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import uuid

APP_ID = 0x464C5431
GROUP_DRAINING = "waiting for process group to empty"
StateError = sqlite3.Error
LEGAL = {(None, "queued"), ("queued", "starting"),
         ("starting", "running"), ("starting", "finished"),
         ("running", "finished")}
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS jobs (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
    "id TEXT UNIQUE NOT NULL, argv TEXT NOT NULL, cwd TEXT NOT NULL, client TEXT NOT NULL, "
    "submitted_by TEXT NOT NULL, submitted_at REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, job_id TEXT UNIQUE NOT NULL "
    "REFERENCES jobs(id), n INTEGER NOT NULL DEFAULT 1, state TEXT, outcome TEXT, "
    "exit_code INTEGER, signal INTEGER, supervisor TEXT, child_pid INTEGER, "
    "started_at REAL, ended_at REAL, detail TEXT)",
    "CREATE TABLE IF NOT EXISTS transitions (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
    "job_id TEXT NOT NULL, attempt_id TEXT NOT NULL, from_state TEXT, to_state TEXT NOT NULL, "
    "actor TEXT NOT NULL, at REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS slot (id INTEGER PRIMARY KEY CHECK(id=1), "
    "holder_attempt TEXT, generation INTEGER NOT NULL)",
    "INSERT OR IGNORE INTO slot VALUES (1, NULL, 0)",
)


def state_path(explicit=None):
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    raw = Path(explicit or os.environ.get("FLEET_STATE_DIR") or base / "fleet-pilot").expanduser()
    if raw.is_symlink():
        raise ValueError("state directory must not be a symlink")
    path = raw.resolve()
    for root in (base.resolve(), (Path.home() / ".local/state").resolve()):
        for name in ("pao", "mini-carga", "u3"):
            reserved = root / name
            if path == reserved or reserved in path.parents:
                raise ValueError("reserved state directory: {}".format(reserved))
    return path


def _connect(state, create=False):
    database = state / "state.db"
    if database.is_symlink() or (state / "attempts").is_symlink():
        raise ValueError("state files must not be symlinks")
    if create:
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not database.exists() and any(state.iterdir()):
            raise ValueError("state directory must be empty or belong to Fleet")
        conn = sqlite3.connect(str(database), timeout=10, isolation_level=None)
    else:
        conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True,
                               timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        if create:
            conn.execute("BEGIN IMMEDIATE")
            app = conn.execute("PRAGMA application_id").fetchone()[0]
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if app != APP_ID and (app != 0 or tables):
                raise ValueError("not a Fleet pilot database")
            conn.execute("PRAGMA application_id={}".format(APP_ID))
            for statement in SCHEMA:
                conn.execute(statement)
            conn.commit()
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
        elif conn.execute("PRAGMA application_id").fetchone()[0] != APP_ID:
            raise ValueError("not a Fleet pilot database")
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
    if supervisor:
        conn.execute("UPDATE attempts SET supervisor=? WHERE id=?",
                     (json.dumps(supervisor), attempt))
    return True


def _finish_and_next(conn, attempt, actor, outcome, code, sig, detail, supervisor=None):
    holder = conn.execute("SELECT holder_attempt FROM slot WHERE id=1").fetchone()[0]
    if holder != attempt:
        raise RuntimeError("attempt does not own slot")
    _transition(conn, attempt, "finished", actor)
    conn.execute("UPDATE attempts SET outcome=?,exit_code=?,signal=?,ended_at=?,detail=? WHERE id=?",
                 (outcome, code, sig, time.time(), detail, attempt))
    conn.execute("UPDATE slot SET holder_attempt=NULL WHERE id=1")
    row = conn.execute("SELECT a.id FROM attempts a JOIN jobs j ON j.id=a.job_id "
                       "WHERE a.state='queued' ORDER BY j.seq LIMIT 1").fetchone()
    if row:
        _claim(conn, row[0], actor, supervisor)
        return row[0]
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


def _logs(state, attempt):
    directory = state / "attempts" / attempt
    return directory / "stdout", directory / "stderr"


class Launch(enum.Enum):
    READY = "ready"
    NOT_CREATED = "not created"
    UNCONFIRMED = "unconfirmed"


def _reserve(conn, state, argv, cwd, client):
    job, attempt = "j_" + uuid.uuid4().hex, "a_" + uuid.uuid4().hex
    actor = "client:" + client
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO jobs (id,argv,cwd,client,submitted_by,submitted_at) "
                 "VALUES (?,?,?,?,?,?)", (job, json.dumps(argv), str(cwd), client,
                 json.dumps({"user": getpass.getuser(), "uid": os.getuid(),
                             "host": socket.gethostname(), "pid": os.getpid()}), time.time()))
    conn.execute("INSERT INTO attempts (id,job_id) VALUES (?,?)", (attempt, job))
    _transition(conn, attempt, "queued", actor)
    out, err = _logs(state, attempt)
    out.parent.mkdir(mode=0o700, parents=True)
    out.touch(mode=0o600)
    err.touch(mode=0o600)
    claimed = _claim(conn, attempt, actor)
    conn.commit()
    return job, attempt, claimed


def _launch_supervisor(state, attempt):
    read_fd = write_fd = None
    created = False
    launch, error = Launch.NOT_CREATED, None
    try:
        read_fd, write_fd = os.pipe()
        # Popen's exec-error pipe proves exec succeeded; readiness below also
        # proves Python imported the runner and persisted the supervisor.
        with (state / "supervisor.log").open("ab", buffering=0) as log:
            subprocess.Popen(
                [sys.executable, "-B", str(Path(__file__).resolve()),
                 "supervise", str(state), attempt, str(write_fd)],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=True, close_fds=True, pass_fds=(write_fd,))
            created = True
        fd, write_fd = write_fd, None
        os.close(fd)
        try:
            readable, _, _ = select.select([read_fd], [], [], 10)
            ready = bool(readable) and os.read(read_fd, 1) == b"R"
        except ValueError as exc:
            launch, error = Launch.UNCONFIRMED, str(exc)
        else:
            launch = Launch.READY if ready else Launch.UNCONFIRMED
            if not ready:
                error = "supervisor readiness unconfirmed; slot retained; do not retry"
    except OSError as exc:
        launch = Launch.UNCONFIRMED if created else Launch.NOT_CREATED
        error = str(exc)
    finally:
        for fd in (read_fd, write_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError as exc:
                    if created:
                        launch, error = Launch.UNCONFIRMED, str(exc)
    return launch, error


def _fail_and_hand_off(conn, state, attempt, actor, error):
    while attempt:
        conn.execute("BEGIN IMMEDIATE")
        attempt = _finish_and_next(conn, attempt, actor, "failed", None, None,
                                   "supervisor spawn failed: {}".format(error))
        conn.commit()
        if attempt is None:
            return
        launch, error = _launch_supervisor(state, attempt)
        if launch is not Launch.NOT_CREATED:
            return


def submit(state, argv, cwd, client):
    if os.name != "posix":
        raise ValueError("pilot supports POSIX hosts only")
    conn = _connect(state, create=True)
    try:
        job, attempt, claimed = _reserve(conn, state, argv, cwd, client)
        result = {"job": job, "attempt": attempt, "client": client, "accepted": False}
        if not claimed:
            return dict(result, state="queued", accepted=True), 0
        launch, error = _launch_supervisor(state, attempt)
        if launch is Launch.READY:
            return dict(result, state="starting", accepted=True), 0
        if launch is Launch.UNCONFIRMED:
            return dict(result, state="starting", view="uncertain", error=error), 3
        _fail_and_hand_off(conn, state, attempt, "client:" + client, error)
        return dict(result, state="finished", outcome="failed", error=error), 1
    finally:
        conn.close()


def view(state, job):
    conn = _connect(state)
    try:
        conn.execute("BEGIN")
        row = conn.execute("SELECT j.*,a.id AS attempt,a.state,a.outcome,a.exit_code,a.signal,"
                           "a.supervisor,a.child_pid,a.started_at,a.ended_at,a.detail "
                           "FROM jobs j JOIN attempts a ON a.job_id=j.id WHERE j.id=?", (job,)).fetchone()
        if row is None:
            raise ValueError("unknown job: {}".format(job))
        result = dict(row)
        result.pop("id")
        result["job"] = job
        for key in ("argv", "submitted_by", "supervisor"):
            result[key] = json.loads(result[key]) if result[key] else None
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
        out, err = _logs(state, result["attempt"])
        result.update(stdout_log=str(out), stderr_log=str(err))
        if result["state"] == "finished" and not (out.is_file() and err.is_file()):
            result.update(outcome="incomplete", detail="attempt log missing")
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


def _execute(conn, state, attempt, actor):
    row = conn.execute("SELECT j.argv,j.cwd FROM jobs j JOIN attempts a ON a.job_id=j.id "
                       "WHERE a.id=?", (attempt,)).fetchone()
    handles = []
    code = sig = None
    outcome, detail = "incomplete", None
    child = None
    try:
        try:
            for path in _logs(state, attempt):
                handles.append(path.open("ab", buffering=0))
        except OSError as exc:
            outcome, detail = "incomplete", "log I/O failed: {}".format(exc)
        else:
            try:
                child = subprocess.Popen(json.loads(row["argv"]), cwd=row["cwd"],
                                         stdin=subprocess.DEVNULL, stdout=handles[0], stderr=handles[1],
                                         close_fds=True, start_new_session=True)
            except OSError as exc:
                outcome, detail = "failed", "command spawn failed: {}".format(exc)
            if child is not None:
                conn.execute("BEGIN IMMEDIATE")
                _transition(conn, attempt, "running", actor)
                conn.execute("UPDATE attempts SET child_pid=?,started_at=? WHERE id=?",
                             (child.pid, time.time(), attempt))
                conn.commit()
                returncode = child.wait()
                if _group_may_be_occupied(child.pid):
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("UPDATE attempts SET detail=? WHERE id=?",
                                 (GROUP_DRAINING, attempt))
                    conn.commit()
                    while _group_may_be_occupied(child.pid):
                        time.sleep(0.2)
                if returncode < 0:
                    outcome, sig = "incomplete", -returncode
                else:
                    code = returncode
                    outcome = "succeeded" if code == 0 else "failed"
    finally:
        for handle in handles:
            try:
                os.fsync(handle.fileno())
                handle.close()
            except OSError as exc:
                outcome, detail = "incomplete", "log close failed: {}".format(exc)
                handle.close()
    return outcome, code, sig, detail


def supervise(state, attempt, ready_fd):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    identity = _observe_identity(os.getpid())
    actor = "supervisor:{}".format(os.getpid())
    conn = _connect(state, create=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        holder = conn.execute("SELECT holder_attempt FROM slot WHERE id=1").fetchone()[0]
        if holder != attempt:
            raise RuntimeError("supervisor does not own slot")
        conn.execute("UPDATE attempts SET supervisor=? WHERE id=?", (json.dumps(identity), attempt))
        conn.commit()
        try:
            os.write(ready_fd, b"R")
        except BrokenPipeError:
            pass
        finally:
            os.close(ready_fd)
        while attempt:
            outcome, code, sig, detail = _execute(conn, state, attempt, actor)
            conn.execute("BEGIN IMMEDIATE")
            attempt = _finish_and_next(conn, attempt, actor, outcome, code, sig, detail, identity)
            conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    if len(sys.argv) != 5 or sys.argv[1] != "supervise":
        raise SystemExit("internal supervisor entry point")
    supervise(Path(sys.argv[2]), sys.argv[3], int(sys.argv[4]))
