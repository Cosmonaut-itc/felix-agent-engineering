"""Single-slot execution authority; a claim never grants a replacement launcher."""
import os
from pathlib import Path
import select
import signal
import sqlite3
import stat
import subprocess
import sys
import time

if __package__:
    from . import admission, evidence, inputs, native
    from .model import Fault, dump, envelope, load, now, opaque, safe_queued
    from .storage import Ledger, state_path
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from fleet import admission, evidence, inputs, native
    from fleet.model import Fault, dump, envelope, load, now, opaque, safe_queued
    from fleet.storage import Ledger, state_path

StateError = sqlite3.Error
GROUP_DRAINING = "closure_unconfirmed"


def attempt_row(db, attempt):
    row = db.execute("SELECT * FROM attempts WHERE id=?", (attempt,)).fetchone()
    if row is None:
        raise Fault("unknown_attempt")
    return row


def owned(db, attempt, generation, identity, launcher=False):
    row = attempt_row(db, attempt)
    slot = db.execute("SELECT attempt,generation FROM slot").fetchone()
    owner = load(row["dispatcher"] if launcher else row["supervisor"])
    if (tuple(slot) != (attempt, generation) or row["generation"] != generation
            or owner != identity or not native.confirmed(identity)):
        raise Fault("execution_owner_mismatch", 3, "unknown")
    return row


def claim(db, dispatcher, supervisor=None):
    """Caller holds the sole transaction shared with operator drain/release."""
    if db.control["drained"]:
        return None
    slot = db.execute("SELECT attempt,generation FROM slot").fetchone()
    if slot["attempt"] is not None:
        return None
    row = db.execute("SELECT a.* FROM attempts a JOIN jobs j ON j.id=a.job "
                     "WHERE a.state='queued' ORDER BY j.seq LIMIT 1").fetchone()
    if row is None:
        return None
    if not safe_queued(row):
        raise Fault("observation_inconsistent", 3, "unknown")
    generation, reservation = slot["generation"] + 1, opaque("r")
    db.execute("UPDATE slot SET attempt=?,generation=?", (row["id"], generation))
    db.execute("UPDATE attempts SET generation=?,reservation=?,dispatcher=?,supervisor=?,"
               "disposition='retained' WHERE id=?",
               (generation, reservation, dump(dispatcher), dump(supervisor) if supervisor else None, row["id"]))
    db.event(row["id"], "starting", "dispatcher", "claim")
    return row["id"], generation


def closure_valid(db, row):
    """Trust the committed original producer chain, never user-provided proof JSON."""
    try:
        body = load(row["closure_body"])
        closure = load(row["closure"])
        command = load(row["command"])
        job = db.execute("SELECT input FROM jobs WHERE id=?", (row["job"],)).fetchone()
        if not body or not closure or not command or job is None:
            return False
        producer = load(row["supervisor"] or row["dispatcher"])
        return (row["state"] == "finished" and row["gate"] == "closed"
                and closure["status"] in ("confirmed", "never_created")
                and closure["coverage"] == "pilot-compatible-posix"
                and closure["generation"] == row["generation"]
                and closure["proof_id"] == body["proof_id"]
                and closure["proof_sha256"] == inputs.digest(dump(body).encode())
                and body["authority_id"] == db.authority["id"]
                and body["principal_uid"] == db.authority["uid"]
                and (body["job"], body["attempt"], body["reservation_id"], body["generation"]) ==
                    (row["job"], row["id"], row["reservation"], row["generation"])
                and body["producer"] == producer and native.verifiable(producer)
                and body["input"] == load(job["input"]) and body["command"] == command
                and body["permission_closed"] is True and body["resources_closed"] is True
                and body["coverage"] == "pilot-compatible-posix"
                and (command["kind"] == "never_created" or body["original_child"] == load(row["child"])))
    except (ValueError, KeyError, TypeError):
        return False


def record_terminal(db, attempt, generation, identity, command, launcher=False):
    with db.transaction():
        row = owned(db, attempt, generation, identity, launcher)
        if row["gate"] == "closed":
            raise Fault("execution_permission_closed", 3)
        if launcher and (row["supervisor"] is not None or row["gate"] != "available"):
            raise Fault("dispatch_unconfirmed", 3)
        input_identity = load(db.execute("SELECT input FROM jobs WHERE id=?", (row["job"],)).fetchone()[0])
        proof_id = opaque("p")
        body = {"contract": "fleet-original-closure@1", "authority_id": db.authority["id"],
                "principal_uid": db.authority["uid"], "proof_id": proof_id,
                "job": row["job"], "attempt": attempt, "reservation_id": row["reservation"],
                "generation": generation, "producer": identity, "input": input_identity,
                "command": command, "original_child": load(row["child"]),
                "coverage": "pilot-compatible-posix", "permission_closed": True,
                "resources_closed": True, "observed_at": now(),
                "basis": "original-launcher-popen-error" if launcher else
                         "original-supervisor-no-command" if command["kind"] == "never_created" else
                         "original-popen-wait-and-compatible-group-empty"}
        closure = {"status": "never_created" if command["kind"] == "never_created" else "confirmed",
                   "coverage": "pilot-compatible-posix", "generation": generation,
                   "reason": command["reason"], "proof_id": proof_id,
                   "proof_sha256": inputs.digest(dump(body).encode())}
        db.execute("UPDATE attempts SET gate='closed',command=?,closure=?,closure_body=?,ended_at=? "
                   "WHERE id=?", (dump(command), dump(closure), dump(body), now(), attempt))
        db.event(attempt, "finished", "launcher" if launcher else "supervisor:" + str(identity["pid"]),
                 command["reason"] or "command_terminal")
    return closure


def release_ordinary(db, row):
    slot = db.execute("SELECT attempt,generation FROM slot").fetchone()
    if row["disposition"] == "released":
        return True
    if tuple(slot) != (row["id"], row["generation"]) or not closure_valid(db, row):
        return False
    collection, publication = load(row["collection"]), load(row["publication"])
    if collection["state"] != "valid" or publication["state"] != "committed":
        return False
    job = db.execute("SELECT input FROM jobs WHERE id=?", (row["job"],)).fetchone()
    verified = evidence.verify(db.state / "attempts" / row["id"], row["evidence_root"], row["job"],
                               row["id"], load(job[0]), load(row["command"]), load(row["closure"]))
    if not verified["valid"]:
        return False
    db.execute("UPDATE slot SET attempt=NULL WHERE attempt=? AND generation=?",
               (row["id"], row["generation"]))
    db.execute("UPDATE attempts SET disposition='released' WHERE id=?", (row["id"],))
    db.event(row["id"], "finished", "authority", "ordinary_release")
    return True


def publish_terminal(db, attempt, capture=None):
    row = attempt_row(db, attempt)
    job = db.execute("SELECT input FROM jobs WHERE id=?", (row["job"],)).fetchone()
    command, closure = load(row["command"]), load(row["closure"])
    # Preparation failure is a command result, not a missing command stream.
    root, manifest = evidence.seal(db.state / "attempts" / attempt, row["job"], attempt,
                                   load(job[0]), command, closure, capture)
    collection = {"state": manifest["collection"], "errors": manifest["errors"]}
    outcome = "incomplete"
    if collection["state"] == "valid" and root:
        if command["kind"] == "never_created":
            outcome = "failed"
        elif command["kind"] == "exited":
            outcome = "succeeded" if command["exit_code"] == 0 else "failed"
    with db.transaction():
        current = attempt_row(db, attempt)
        if not closure_valid(db, current):
            raise Fault("closure_unconfirmed", 3)
        db.execute("UPDATE attempts SET collection=?,publication=?,evidence_root=?,recorded_outcome=? "
                   "WHERE id=?", (dump(collection), dump({"state": "pending" if root else "incomplete",
                                                        "root_sha256": root}), root, outcome, attempt))
        db.event(attempt, "finished", "authority", "publication_prepared" if root else "publication_failed")
    with db.transaction():
        row = attempt_row(db, attempt)
        if root:
            db.execute("UPDATE attempts SET publication=? WHERE id=?",
                       (dump({"state": "committed", "root_sha256": root}), attempt))
            db.event(attempt, "finished", "authority", "publication_committed")
        release_ordinary(db, attempt_row(db, attempt))


def launch(db, attempt, generation, identity):
    read_fd = write_fd = None
    created = False
    try:
        read_fd, write_fd = os.pipe()
        fd = os.open(db.state / "supervisor.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT | inputs.NOFOLLOW, 0o600)
        with os.fdopen(fd, "ab", buffering=0) as log:
            info = os.fstat(log.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                    info.st_mode & 0o077 or info.st_nlink != 1):
                raise OSError("invalid_supervisor_log")
            subprocess.Popen([sys.executable, "-I", "-B", str(Path(__file__).resolve()), "supervise",
                              str(db.state), attempt, str(generation), str(write_fd)],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True, close_fds=True, pass_fds=(write_fd,),
                             env={"PATH": os.defpath, "LANG": "C"})
            created = True
        os.close(write_fd)
        write_fd = None
        readable, _, _ = select.select([read_fd], [], [], 10)
        if readable:
            os.read(read_fd, 1)
        # EOF/readiness timeout never proves that the supervisor was not created.
    except OSError:
        if not created:
            command = {"kind": "never_created", "exit_code": None, "signal": None,
                       "reason": "supervisor_spawn_failed"}
            record_terminal(db, attempt, generation, identity, command, launcher=True)
            publish_terminal(db, attempt)
    finally:
        for fd in (read_fd, write_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


def dispatch(db):
    identity = native.identity(os.getpid())
    if not native.verifiable(identity):
        raise Fault("launcher_identity_unverifiable", 3, "unknown")
    with native.dispatch_boundary():
        with db.transaction():
            assignment = claim(db, identity)
        if assignment:
            launch(db, *assignment, identity)
    return assignment


def submit(state, argv, cwd, client, includes=(), private=(), artifacts=(), credentials=(),
           idempotency_key=None, enqueue_only=False):
    if os.name != "posix":
        raise Fault("unsupported_execution_context")
    with Ledger(state, create=True) as db:
        receipt = admission.admit(db, argv, cwd, client, includes, private, artifacts,
                                  credentials, idempotency_key, enqueue_only)
        authority = db.authority
        # The historical receipt already exists. Dispatch failure cannot revoke it.
        if receipt["binding"] != "replay" and not enqueue_only:
            try:
                dispatch(db)
            except (OSError, ValueError, sqlite3.Error, RuntimeError):
                pass
        return envelope("submit", authority, **receipt), 0


def execute(db, attempt, generation, identity):
    row = attempt_row(db, attempt)
    job = db.execute("SELECT * FROM jobs WHERE id=?", (row["job"],)).fetchone()
    execution, input_identity = load(job["execution"]), load(job["input"])
    directory = db.state / "attempts" / attempt
    capture = None
    reason = "content_mismatch"

    def collection_error(errors):
        # A known irreversible stream loss is observable before command exit.
        # A storage failure cannot stop draining/reaping our original child.
        try:
            with db.transaction():
                current = owned(db, attempt, generation, identity)
                previous = load(current["collection"])
                errors = sorted(set(previous["errors"] + errors))
                if previous != {"state": "incomplete", "errors": errors}:
                    db.execute("UPDATE attempts SET collection=? WHERE id=?",
                               (dump({"state": "incomplete", "errors": errors}), attempt))
                    db.event(attempt, current["state"], "supervisor:" + str(identity["pid"]),
                             "collection_loss_observed")
        except (OSError, ValueError, sqlite3.Error, RuntimeError):
            pass

    try:
        cwd = inputs.materialize(db.state / "capsules" / job["capsule"], directory / "workspace", input_identity)
        reason = "unsupported_execution_context"
        environment = inputs.execution_environment(execution, cwd)
        reason = "preparation_failed"
        secrets = inputs.credential_values(execution["environment"], execution["credential_names"])
        capture = evidence.Capture(directory, cwd, execution["outputs"]["required"], secrets)
    except (OSError, ValueError, KeyError):
        if capture:
            capture.close()
        command = {"kind": "never_created", "exit_code": None, "signal": None, "reason": reason}
        return command, None
    with db.transaction():
        owned(db, attempt, generation, identity)
        changed = db.execute("UPDATE attempts SET gate='consumed' WHERE id=? AND gate='available'",
                             (attempt,)).rowcount
        if changed != 1:
            raise Fault("execution_permission_closed", 3)
        db.event(attempt, "starting", "supervisor:" + str(identity["pid"]), "execution_permit_consumed")
    try:
        child = subprocess.Popen(execution["argv"], cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 close_fds=True, start_new_session=True)
    except OSError:
        capture.close()
        return {"kind": "never_created", "exit_code": None, "signal": None,
                "reason": "command_spawn_failed"}, None
    capture.on_error = collection_error
    child_identity = native.identity(child.pid)
    # Popen's original, unreaped child reference supplies creation provenance;
    # birth is captured before observe() can reap it. No later PID adoption.
    if not native.verifiable(child_identity):
        # Still drain streams and reap our child; inability to attribute preserves uncertainty.
        capture.observe(child, lambda: None)
        child.wait()
        raise Fault("execution_unconfirmed", 3)
    with db.transaction():
        owned(db, attempt, generation, identity)
        db.execute("UPDATE attempts SET child=?,started_at=? WHERE id=?",
                   (dump(child_identity), now(), attempt))
        db.event(attempt, "running", "supervisor:" + str(identity["pid"]), "original_popen_child")

    def draining():
        if native.group_occupied(child.pid):
            with db.transaction():
                owned(db, attempt, generation, identity)
                db.execute("UPDATE attempts SET detail=? WHERE id=?", (GROUP_DRAINING, attempt))
                db.event(attempt, "running", "supervisor:" + str(identity["pid"]), GROUP_DRAINING)

    capture.observe(child, draining)
    code = child.wait()
    draining()
    while native.group_occupied(child.pid):
        time.sleep(0.2)
    command = {"kind": "signalled" if code < 0 else "exited", "exit_code": code if code >= 0 else None,
               "signal": -code if code < 0 else None, "reason": None}
    return command, capture


def supervise(state, attempt, generation, ready_fd):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    identity = native.identity(os.getpid())
    if not native.verifiable(identity):
        raise Fault("supervisor_identity_unverifiable", 3)
    with Ledger(state, create=True, initialize=False) as db:
        with db.transaction():
            row = attempt_row(db, attempt)
            slot = db.execute("SELECT attempt,generation FROM slot").fetchone()
            if tuple(slot) != (attempt, generation):
                raise Fault("generation_mismatch", 5)
            changed = db.execute("UPDATE attempts SET supervisor=? WHERE id=? AND generation=? "
                                 "AND supervisor IS NULL AND gate='available' AND state='starting'",
                                 (dump(identity), attempt, generation)).rowcount
            if changed != 1:
                raise Fault("supervisor_already_bound", 3)
            db.event(attempt, "starting", "supervisor:" + str(identity["pid"]), "original_supervisor_bound")
        try:
            os.write(ready_fd, b"R")
        except BrokenPipeError:
            pass
        finally:
            os.close(ready_fd)
        while attempt:
            command, capture = execute(db, attempt, generation, identity)
            record_terminal(db, attempt, generation, identity, command)
            publish_terminal(db, attempt, capture)
            with db.transaction():
                # drain is checked here as well as in submit/recover.
                assignment = claim(db, identity, identity)
            if not assignment:
                break
            attempt, generation = assignment


def recover(state):
    with Ledger(state, create=True, initialize=False) as db:
        result = envelope("recover", db.authority, examined_revision=db.authority["revision"],
                          recovery_complete=True, dispatch_claimed=[], live_confirmed=[], finished_confirmed=[],
                          still_uncertain=[], pending=[], operator_pending=[], confirmed_actions=[])
        try:
            return reconcile(db, result)
        except (OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
            result["recovery_complete"] = False
            result["error"] = {"code": exc.code if isinstance(exc, Fault) else "reconciliation_unconfirmed",
                               "effects": "unknown"}
            return result, 3


def reconcile(db, result):
    from .query import observe
    rows = db.execute("SELECT id FROM attempts ORDER BY rowid").fetchall()
    for item in rows:
        row = attempt_row(db, item[0])
        if row["state"] == "finished" and row["disposition"] != "released":
            with db.transaction():
                row = attempt_row(db, item[0])
                publication = load(row["publication"])
                if closure_valid(db, row) and publication["state"] == "pending":
                    if row["evidence_root"]:
                        job = db.execute("SELECT input FROM jobs WHERE id=?", (row["job"],)).fetchone()
                        proof = evidence.verify(db.state / "attempts" / row["id"], row["evidence_root"],
                                                row["job"], row["id"], load(job[0]),
                                                load(row["command"]), load(row["closure"]))
                        if proof["valid"]:
                            db.execute("UPDATE attempts SET publication=? WHERE id=?",
                                       (dump(dict(publication, state="committed")), row["id"]))
                            db.event(row["id"], "finished", "recover", "publication_committed")
                        else:
                            db.execute("UPDATE attempts SET publication=? WHERE id=?",
                                       (dump(dict(publication, state="incomplete")), row["id"]))
                            db.event(row["id"], "finished", "recover", "original_publication_unavailable")
                    elif not native.confirmed(load(row["supervisor"] or row["dispatcher"])):
                        db.execute("UPDATE attempts SET publication=?,collection=?,recorded_outcome="
                                   "COALESCE(recorded_outcome,'incomplete') WHERE id=?",
                                   (dump(dict(publication, state="incomplete")),
                                    dump({"state": "incomplete", "errors": ["original_publication_unavailable"]}),
                                    row["id"]))
                        db.event(row["id"], "finished", "recover", "original_publication_unavailable")
                release_ordinary(db, attempt_row(db, item[0]))
                confirmed = {"job": row["job"], "attempt": row["id"],
                             "decision_revision": db.authority["revision"], "reason": "terminal_reconciled"}
            result["confirmed_actions"].append(confirmed)
    assignment = dispatch(db)
    if assignment:
        row = attempt_row(db, assignment[0])
        claim_revision = db.execute("SELECT revision FROM transitions WHERE attempt=? AND cause='claim' "
                                    "ORDER BY seq LIMIT 1", (row["id"],)).fetchone()[0]
        result["dispatch_claimed"].append({"job": row["job"], "attempt": row["id"],
                                           "generation": assignment[1], "reason": "claim",
                                           "disposition": "retained", "work_remaining": ["execution", "closure"],
                                           "decision_revision": claim_revision})
    for item in rows:
        with db.transaction():
            row = attempt_row(db, item[0])
            view = observe(db, row)
            entry = {"job": row["job"], "attempt": row["id"], "generation": row["generation"],
                     "decision_revision": view["revision"], "reason": view["reasons"],
                     "disposition": row["disposition"], "work_remaining": view["work_remaining"]}
            if view["view"] == "uncertain":
                result["still_uncertain"].append(entry)
                result["recovery_complete"] = False
            elif row["state"] == "finished":
                result["finished_confirmed"].append(entry)
                if row["disposition"] != "released":
                    result["recovery_complete"] = False
            elif row["state"] == "queued":
                result["pending"].append(entry)
            else:
                result["live_confirmed"].append(entry)
                if view["completion"] == "incomplete":
                    result["recovery_complete"] = False
    return result, 0 if result["recovery_complete"] else 3


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    if len(sys.argv) != 6 or sys.argv[1] != "supervise":
        raise SystemExit("internal supervisor entry point")
    try:
        supervise(Path(sys.argv[2]), sys.argv[3], int(sys.argv[4]), int(sys.argv[5]))
    except BaseException:
        raise SystemExit(3) from None
