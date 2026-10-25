"""Read-only projections and total completion precedence for fleet-job-api@1."""
from . import evidence, inputs, native
from .model import Fault, envelope, identifier, key, load, now, safe_queued
from .storage import Ledger


def select_job(db, job=None, idempotency_key=None, attempt=None):
    if (job is None) == (idempotency_key is None):
        raise Fault("exactly_one_selector_required")
    if job is not None:
        identifier(job, "j")
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
    else:
        key(idempotency_key)
        row = db.execute("SELECT * FROM jobs WHERE uid=? AND key=?",
                         (db.authority["uid"], idempotency_key)).fetchone()
    if row is None:
        raise Fault("unknown_job" if job is not None else "key_not_found",
                    as_of_revision=db.authority["revision"], authority_id=db.authority["id"],
                    principal_uid=db.authority["uid"])
    if attempt is not None:
        identifier(attempt, "a")
        if attempt != row["attempt"]:
            raise Fault("attempt_mismatch")
    return row


def owner_reason(db, row):
    from .runner import closure_valid
    if row["state"] == "finished":
        if not closure_valid(db, row):
            return "owner_uncertain"
        if load(row["collection"])["state"] == "incomplete":
            return "owner_collection_incomplete"
        publication = load(row["publication"])["state"]
        if publication != "committed":
            return "owner_publication_" + publication
        return "owner_release_pending"
    if not native.confirmed(load(row["supervisor"])) or row["detail"]:
        return "owner_uncertain"
    return "slot_" + row["state"]


def observe(db, row):
    from .runner import closure_valid
    job = db.execute("SELECT * FROM jobs WHERE id=?", (row["job"],)).fetchone()
    execution = load(job["execution"])
    secrets = inputs.credential_values(execution["environment"], execution["credential_names"])
    control = db.control
    state, view, reasons, scope = row["state"], row["state"], [], None
    dispatch = {"safe_pending": False, "needs_recover": False, "reasons": []}
    blocked = None
    slot = db.execute("SELECT attempt,generation FROM slot").fetchone()
    if state == "queued":
        safe = safe_queued(row)
        dispatch["safe_pending"] = safe
        if not safe:
            view, reasons, scope = "uncertain", ["observation_inconsistent"], "self"
        if slot["attempt"]:
            owner = db.execute("SELECT * FROM attempts WHERE id=?", (slot["attempt"],)).fetchone()
            if owner is None:
                view, reasons, scope = "uncertain", ["observation_inconsistent"], "blocking_owner"
            else:
                reason = owner_reason(db, owner)
                blocked = {"job": owner["job"], "attempt": owner["id"],
                           "reservation_id": owner["reservation"], "generation": owner["generation"],
                           "reason": reason}
                if reason == "owner_uncertain":
                    view, reasons, scope = "uncertain", ["owner_unverified"], "blocking_owner"
        predecessor = db.execute("SELECT j.id FROM jobs j JOIN attempts a ON a.job=j.id "
                                 "WHERE a.state='queued' AND j.seq<? ORDER BY j.seq LIMIT 1",
                                 (job["seq"],)).fetchone()
        if predecessor:
            dispatch["reasons"].append("fifo_predecessor")
            dispatch["fifo_predecessor"] = predecessor[0]
        if not blocked:
            dispatch["reasons"].append("dispatch_not_requested" if row["enqueue_only"] else "recovery_required")
            dispatch["needs_recover"] = True
    elif state in ("starting", "running"):
        if (slot["attempt"], slot["generation"]) != (row["id"], row["generation"]):
            view, reasons, scope = "uncertain", ["generation_mismatch"], "self"
        elif row["supervisor"] is None:
            view, reasons, scope = "uncertain", ["dispatch_unconfirmed"], "self"
        elif not native.confirmed(load(row["supervisor"])):
            view, reasons, scope = "uncertain", ["owner_unverified"], "self"
        elif row["detail"]:
            view, reasons, scope = "uncertain", [row["detail"]], "self"
    elif state != "finished":
        view, reasons, scope = "uncertain", ["observation_inconsistent"], "self"
    collection = load(row["collection"])
    publication = load(row["publication"])
    inconsistent = (collection["state"] not in ("pending", "valid", "incomplete")
                    or publication["state"] not in ("pending", "committed", "incomplete")
                    or (publication["state"] == "committed" and not row["evidence_root"]))
    if state in ("starting", "running"):
        inconsistent = inconsistent or (
            row["gate"] not in ("available", "consumed") or row["reservation"] is None
            or row["generation"] is None or row["disposition"] != "retained"
            or any(row[name] is not None for name in ("command", "closure", "closure_body", "recorded_outcome"))
            or (state == "running" and (row["gate"] != "consumed" or row["child"] is None)))
    elif state == "finished":
        inconsistent = inconsistent or (
            row["disposition"] not in ("retained", "released") or row["reservation"] is None
            or row["generation"] is None
            or (row["disposition"] == "retained" and
                tuple(slot) != (row["id"], row["generation"]))
            or (row["disposition"] == "released" and slot["attempt"] == row["id"]))
    if inconsistent:
        view, reasons, scope = "uncertain", ["observation_inconsistent"], "self"
    command = load(row["command"]) or {"kind": "unknown", "exit_code": None, "signal": None, "reason": None}
    closure = load(row["closure"]) or {"status": "unknown", "coverage": None, "generation": row["generation"],
                                       "reason": None, "proof_id": None}
    reservation = None if row["reservation"] is None else {
        "kind": "pilot_slot", "id": row["reservation"], "generation": row["generation"],
        "disposition": row["disposition"]}
    proof = {"valid": None, "errors": [], "files": []}
    outcome = row["recorded_outcome"] if state == "finished" else None
    remaining = []
    if state == "finished":
        if not closure_valid(db, row):
            view, reasons, scope = "uncertain", ["closure_unconfirmed"], "self"
        if publication["state"] == "committed":
            proof = evidence.verify(db.state / "attempts" / row["id"], row["evidence_root"], row["job"],
                                    row["id"], load(job["input"]), command, closure)
            if not proof["valid"]:
                outcome = "incomplete"
                reasons.append("evidence_invalid")
        elif publication["state"] == "incomplete":
            proof["valid"] = False
            proof["errors"] = ["publication_incomplete"]
            outcome = "incomplete"
        else:
            remaining.append("publication")
            outcome = "incomplete" if collection["state"] == "incomplete" else None
        if collection["state"] == "incomplete":
            outcome = "incomplete"
            reasons.append("evidence_incomplete")
        if command["kind"] == "signalled":
            outcome = "incomplete"
        if reservation and reservation["disposition"] != "released":
            remaining.append("release")
            if collection["state"] == "incomplete" or publication["state"] == "incomplete":
                remaining.append("operator_decision")
        complete = (view == "finished" and command["kind"] in ("exited", "never_created")
                    and outcome in ("succeeded", "failed") and collection["state"] == "valid"
                    and publication["state"] == "committed" and proof["valid"] is True
                    and reservation is not None and reservation["disposition"] == "released")
        completion = "complete" if complete else "incomplete"
    else:
        remaining = ["dispatch"] if state == "queued" else ["execution", "closure"]
        completion = "incomplete" if view == "uncertain" or collection["state"] == "incomplete" else "pending"
        if collection["state"] == "incomplete":
            reasons.append("evidence_incomplete")
    supervisor = load(row["supervisor"])
    if supervisor:
        supervisor = dict(supervisor, host=inputs.redact(supervisor["host"], secrets))
    transitions = [dict(t) for t in db.execute("SELECT * FROM transitions WHERE attempt=? ORDER BY seq", (row["id"],))]
    for transition in transitions:
        transition["actor"] = inputs.redact(transition["actor"], secrets)
    submitted_by = {name: inputs.redact(value, secrets) if isinstance(value, str) else value
                    for name, value in load(job["submitted_by"]).items()}
    logs = {entry["path"].rsplit("/", 1)[-1]: entry["path"] for entry in proof["files"]
            if entry["kind"] == "log"}
    return {"revision": db.authority["revision"], "observed_at": now(), "state": state, "view": view,
            "seq": job["seq"], "client": job["client"], "submitted_by": submitted_by,
            "interruptible": False, "started_at": row["started_at"], "ended_at": row["ended_at"],
            "stdout_log": logs.get("stdout"), "stderr_log": logs.get("stderr"),
            "uncertainty_scope": scope, "reasons": sorted(set(reasons)), "dispatch": dispatch,
            "blocked_by": blocked, "admission": {"operator_drained": control["drained"],
                                                     "reasons": ["operator_drained"] if control["drained"] else []},
            "control": control, "supervisor": supervisor,
            "child_pid": load(row["child"])["pid"] if row["child"] else None,
            "transitions": transitions, "command": command, "closure": closure,
            "recorded_outcome": row["recorded_outcome"], "outcome": outcome,
            "collection": collection, "publication": publication, "evidence": proof,
            "reservation": reservation, "capacity_resolution": load(row["capacity_resolution"]),
            "completion": completion, "work_remaining": remaining}


def view(state, operation, job=None, idempotency_key=None, attempt=None):
    with Ledger(state) as db:
        db.execute("BEGIN")
        selected = select_job(db, job, idempotency_key, attempt)
        row = db.execute("SELECT * FROM attempts WHERE id=?", (selected["attempt"],)).fetchone()
        result = envelope(operation, db.authority, job=selected["id"], attempt=selected["attempt"],
                          accepted=True, admission=load(selected["admission"]), input=load(selected["input"]),
                          observation=observe(db, row))
        return result
