"""Explicit authenticated local capacity decisions. No ordinary path calls mutate()."""
import os
import unicodedata

from . import inputs, native
from .model import Fault, dump, envelope, identifier, key, load, now, opaque
from .storage import Ledger


def reason_valid(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 512 or not value.strip()
            or any(unicodedata.category(c) in ("Cc", "Cs") for c in value)):
        raise Fault("invalid_operator_reason")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise Fault("invalid_operator_reason") from None


def secrets_for(db, job=None):
    rows = db.execute("SELECT execution FROM jobs" if job is None else
                      "SELECT execution FROM jobs WHERE id=?", () if job is None else (job,))
    secrets = set()
    for row in rows:
        execution = load(row[0])
        secrets.update(inputs.credential_values(execution["environment"], execution["credential_names"]))
    return sorted(secrets, key=len, reverse=True)


# Only these fields originate in the protocol, rather than copied operator data.
# Context matters: request.backend/reason and target selectors remain data even
# when their bytes coincide with a public literal. Unknown fields default to data.
PUBLIC_SCHEMAS = {
    "response": {"api": "literal", "operation": "literal", "binding": "literal",
                 "status": "literal", "error": "error", "receipt": "decision",
                 "operations": "operation_record", "audit_events": "decision"},
    "operation_record": {"status": "literal", "receipt": "decision"},
    "decision": {"action": "literal", "decision": "literal", "reasons": "literal",
                 "reservation_before": "literal", "reservation_after": "literal",
                 "actor": "actor", "request": "request"},
    "request": {"action": "literal"},
    "actor": {"role": "literal"},
    "error": {"code": "literal", "effects": "literal"},
}


def public_projection(value, secrets, schema="response"):
    """Project operator JSON by provenance, including persisted history.

    Fixed protocol literals retain their meaning; all other strings use the same
    redactor, recursively, including unknown fields and nested targets. This makes
    a new projection without changing private binding or consulting reader env.
    Storage is not a promise of safety under the snapshot's current declarations.
    """
    if isinstance(value, str):
        return value if schema == "literal" else inputs.redact(value, secrets)
    if isinstance(value, dict):
        fields = PUBLIC_SCHEMAS.get(schema, {})
        return {name: public_projection(item, secrets, fields.get(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [public_projection(item, secrets, schema) for item in value]
    return value


def public_request(request, secrets):
    """Complete validated request, including null selectors, with copied data sane.

    Keep the complete denied stimulus, not a reconstruction from the old binding.
    Redaction uses persisted declarations, never the operator reader's environment.
    """
    return public_projection(request, secrets, "request")


def validate_request(action, operation_id, reason, job=None, attempt=None, reservation=None,
                     generation=None, drain_id=None, proof=None, backend="pilot_slot"):
    key(operation_id)
    reason_valid(reason)
    if (not isinstance(backend, str) or not 1 <= len(backend) <= 128
            or any(unicodedata.category(c) in ("Cc", "Cs") for c in backend)):
        raise Fault("invalid_operator_backend")
    if action in ("assess", "release"):
        identifier(job, "j")
        identifier(attempt, "a")
        identifier(reservation, "r")
        if type(generation) is not int or generation <= 0:
            raise Fault("invalid_generation")
    if action in ("assess", "release", "reopen"):
        identifier(drain_id, "d")
    if action == "release":
        identifier(proof, "p")
    return {"action": action, "operation_id": operation_id, "reason": reason, "job": job,
            "attempt": attempt, "reservation": reservation, "generation": generation,
            "drain_id": drain_id, "proof": proof, "backend": backend}


def target(db, request):
    row = db.execute("SELECT * FROM attempts WHERE job=?", (request["job"],)).fetchone()
    if row is None:
        raise Fault("unknown_job")
    if row["id"] != request["attempt"]:
        raise Fault("attempt_mismatch")
    if request["backend"] != "pilot_slot":
        raise Fault("operator_backend_unsupported")
    slot = db.execute("SELECT attempt,generation FROM slot").fetchone()
    if (row["reservation"], row["generation"]) != (request["reservation"], request["generation"]):
        raise Fault("generation_mismatch", 5)
    # Historical G cannot affect H, including after H has itself been released.
    if slot["generation"] != row["generation"] or slot["attempt"] not in (None, row["id"]):
        raise Fault("generation_mismatch", 5)
    if slot["attempt"] is None and row["disposition"] != "released":
        raise Fault("generation_mismatch", 5)
    return row


def drained(db, drain_id):
    control = db.control
    if not control["drained"]:
        raise Fault("not_drained", 3)
    if control["drain_id"] != drain_id:
        raise Fault("drain_mismatch", 5)
    return control


def proof_body(db, row, control, actor, eligible):
    return {"contract": "fleet-local-capacity-proof@1", "authority_id": db.authority["id"],
            "principal_uid": db.authority["uid"], "job": row["job"], "attempt": row["id"],
            "reservation": row["reservation"], "generation": row["generation"],
            "drain_id": control["drain_id"], "epoch": control["epoch"],
            "relevant_revision": row["relevant_revision"],
            "original_closure": load(row["closure"]),
            "original_body_sha256": inputs.digest((row["closure_body"] or "null").encode()),
            "verifier": actor, "release_eligible": eligible, "assessed_at": now()}


def verify_proof(db, row, control, proof_id):
    from .runner import closure_valid
    stored = db.execute("SELECT * FROM proofs WHERE id=?", (proof_id,)).fetchone()
    if stored is None:
        raise Fault("proof_invalid")
    if inputs.digest(stored["body"].encode()) != stored["sha256"]:
        raise Fault("proof_invalid")
    body = load(stored["body"])
    if (body["authority_id"], body["principal_uid"], body["job"], body["attempt"],
            body["reservation"], body["generation"]) != (
                db.authority["id"], db.authority["uid"], row["job"], row["id"],
                row["reservation"], row["generation"]):
        raise Fault("proof_invalid")
    if (body["drain_id"], body["epoch"], body["relevant_revision"]) != (
            control["drain_id"], control["epoch"], row["relevant_revision"]):
        raise Fault("proof_stale", 3)
    if (body["original_closure"] != load(row["closure"]) or
            body["original_body_sha256"] != inputs.digest((row["closure_body"] or "null").encode())):
        raise Fault("proof_invalid")
    if not body["release_eligible"] or not closure_valid(db, row):
        raise Fault("proof_insufficient", 3)
    # Closure is stable: original Popen/wait + closed permission. Do not adopt or
    # penalize an unrelated process that later happens to reuse the numeric PGID.
    return body


def decide(db, request, actor):
    from .runner import closure_valid
    action = request["action"]
    row = None
    if action in ("assess", "release"):
        row = target(db, request)
    elif request["backend"] != "pilot_slot":
        raise Fault("operator_backend_unsupported")
    control = db.control
    details = {"decision": "committed", "target": None, "drain_id": control["drain_id"],
               "proof_id": None, "release_eligible": None, "reservation_before": None,
               "reservation_after": None, "validation_processed": True}
    if row is not None:
        details.update(target={name: request[name] for name in ("job", "attempt", "reservation", "generation")},
                       reservation_before=row["disposition"], reservation_after=row["disposition"])
    if action == "drain":
        if control["drained"]:
            details["decision"] = "already_drained"
        else:
            drain_id = opaque("d")
            revision = db.revision()
            db.execute("UPDATE control SET drained=1,epoch=epoch+1,drain_id=?,revision=?",
                       (drain_id, revision))
            details["drain_id"] = drain_id
    elif action == "assess":
        control = drained(db, request["drain_id"])
        eligible = closure_valid(db, row)
        proof_id = opaque("p")
        body = proof_body(db, row, control, actor, eligible)
        encoded = dump(body)
        db.execute("INSERT INTO proofs VALUES (?,?,?)", (proof_id, encoded, inputs.digest(encoded.encode())))
        details.update(proof_id=proof_id, release_eligible=eligible, drain_id=control["drain_id"])
        if not eligible:
            details["decision"] = "proof_insufficient"
            return details, 3, "proof_insufficient"
    elif action == "release":
        control = drained(db, request["drain_id"])
        details["drain_id"] = control["drain_id"]
        if row["disposition"] == "released":
            if not closure_valid(db, row):
                raise Fault("proof_insufficient", 3)
            details.update(decision="already_released", reservation_after="released")
        else:
            verify_proof(db, row, control, request["proof"])
            details.update(proof_id=request["proof"], release_eligible=True, reservation_after="released")
            # Capacity, resolution reference, operation receipt and audit share the
            # surrounding transaction; a receipt failure rolls the capacity back.
            changed = db.execute("UPDATE slot SET attempt=NULL WHERE attempt=? AND generation=?",
                                 (row["id"], row["generation"])).rowcount
            if changed != 1:
                raise Fault("generation_mismatch", 5)
            resolution = {"operation_id": request["operation_id"], "mode": "operator", "state": "committed",
                          "proof_id": request["proof"], "reason": request["reason"],
                          "drain_id": control["drain_id"], "preserved_collection": load(row["collection"]),
                          "preserved_recorded_outcome": row["recorded_outcome"]}
            db.execute("UPDATE attempts SET disposition='released',capacity_resolution=? WHERE id=?",
                       (dump(resolution), row["id"]))
            db.event(row["id"], "finished", "operator", "operator_capacity_release")
    elif action == "reopen":
        control = drained(db, request["drain_id"])
        slot = db.execute("SELECT attempt FROM slot").fetchone()[0]
        retained = db.execute("SELECT id FROM attempts WHERE generation IS NOT NULL AND "
                              "(disposition IS NULL OR disposition!='released')").fetchall()
        released = db.execute("SELECT * FROM attempts WHERE disposition='released'").fetchall()
        if slot is not None or retained or any(not closure_valid(db, item) for item in released):
            raise Fault("reopen_blocked", 3, reasons=["capacity_not_reconciled"])
        revision = db.revision()
        db.execute("UPDATE control SET drained=0,revision=?", (revision,))
        details["drain_id"] = control["drain_id"]
    else:
        raise Fault("invalid_operator_action")
    return details, 0, None


def mutate(state, request):
    actor = native.actor()
    with Ledger(state, create=True, initialize=False) as db:
        response, code = None, 0
        with db.transaction():
            secrets = secrets_for(db)
            if any(secret in request["reason"].encode() or secret in request["operation_id"].encode()
                   for secret in secrets):
                revision = db.revision()
                event = {"operation_id": request["operation_id"],
                         "action": request["action"], "decision": "credential_in_operator_request",
                         "request": public_request(request, secrets),
                         "decision_revision": revision, "actor": actor,
                         "reason": request["reason"], "at": now()}
                event.update(authority_id=db.authority["id"], principal_uid=db.authority["uid"])
                event = public_projection(event, secrets, "decision")
                audit_seq = db.execute("INSERT INTO audit (event) VALUES (?)", (dump(event),)).lastrowid
                response = envelope("operator." + request["action"], db.authority,
                                    audit_recorded=True, audit_seq=audit_seq,
                                    error={"code": "credential_in_operator_request",
                                           "effects": "no_capacity_change"})
                code = 2
            existing = db.execute("SELECT * FROM operations WHERE id=?", (request["operation_id"],)).fetchone()
            if response is not None:
                pass
            elif existing:
                if existing["request"] != dump(request):
                    revision = db.revision()
                    event = {"operation_id": request["operation_id"], "decision": "operation_conflict",
                             "action": request["action"], "request": public_request(request, secrets),
                             "decision_revision": revision, "actor": actor,
                             "reason": request["reason"], "at": now()}
                    event.update(authority_id=db.authority["id"], principal_uid=db.authority["uid"])
                    event = public_projection(event, secrets, "decision")
                    cursor = db.execute("INSERT INTO audit (event) VALUES (?)", (dump(event),))
                    code = 5
                    response = envelope("operator." + request["action"], db.authority,
                                        operation_id=request["operation_id"], audit_recorded=True,
                                        audit_seq=cursor.lastrowid,
                                        existing_operation={"operation_id": existing["id"], "seq": existing["seq"]},
                                        error={"code": "operation_conflict", "effects": "no_capacity_change"})
                else:
                    code = existing["exit_code"]
                    response = envelope("operator." + request["action"], db.authority,
                                        operation_id=request["operation_id"], binding="replay",
                                        status="committed", receipt=load(existing["receipt"]))
                    if existing["error"]:
                        response["error"] = {"code": existing["error"], "effects": "no_capacity_change"}
            else:
                error_code = None
                try:
                    details, code, error_code = decide(db, request, actor)
                except Fault as fault:
                    code, error_code = fault.exit_code, fault.code
                    details = {"decision": fault.code, "validation_processed": True,
                               "target": {name: request[name] for name in ("job", "attempt", "reservation", "generation")},
                               "drain_id": request["drain_id"], "proof_id": request["proof"],
                               "release_eligible": False, "reservation_before": None, "reservation_after": None,
                               "reasons": fault.fields.get("reasons", [])}
                revision = db.revision()
                event = dict(details, action=request["action"], operation_id=request["operation_id"],
                             decision_revision=revision, actor=actor,
                             reason=request["reason"], at=now())
                event.update(authority_id=db.authority["id"], principal_uid=db.authority["uid"])
                event = public_projection(event, secrets, "decision")
                audit_seq = db.execute("INSERT INTO audit (event) VALUES (?)", (dump(event),)).lastrowid
                receipt = dict(details, action=request["action"], operation_id=request["operation_id"],
                               decision_revision=revision, actor=actor,
                               reason=request["reason"], audit_seq=audit_seq,
                               audit_recorded=True, at=now())
                receipt = public_projection(receipt, secrets, "decision")
                db.execute("INSERT INTO operations (id,request,receipt,exit_code,error) VALUES (?,?,?,?,?)",
                           (request["operation_id"], dump(request), dump(receipt), code, error_code))
                response = envelope("operator." + request["action"], db.authority,
                                    operation_id=request["operation_id"], binding="new", status="committed", receipt=receipt)
                if error_code:
                    response["error"] = {"code": error_code, "effects": "no_capacity_change"}
        response.update(control=db.control, observed_at=now())
        return public_projection(response, secrets), code


def status(state, operation_id=None, after_seq=0, limit=50, after_audit_seq=None):
    if operation_id is not None:
        key(operation_id)
    # Omitted audit cursor retains the original shared starting offset. Subsequent
    # pages must carry BOTH returned offsets; the streams grow independently.
    if after_audit_seq is None:
        after_audit_seq = after_seq
    # SQLite sequence parameters are signed int64. Reject out-of-range cursors
    # before opening the snapshot or binding any SQL parameter, also with an ID.
    if (type(after_seq) is not int or not 0 <= after_seq <= 9223372036854775807
            or type(after_audit_seq) is not int or not 0 <= after_audit_seq <= 9223372036854775807
            or type(limit) is not int or not 1 <= limit <= 100):
        raise Fault("invalid_pagination")
    with Ledger(state) as db:
        db.execute("BEGIN")
        result = envelope("operator.status", db.authority, control=db.control,
                          observed_at=now(), as_of_revision=db.authority["revision"])
        if operation_id is not None:
            row = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if row is None:
                raise Fault("operation_not_found", as_of_revision=db.authority["revision"],
                            authority_id=db.authority["id"], principal_uid=db.authority["uid"])
            result.update(operation_id=operation_id, status="committed", receipt=load(row["receipt"]))
        else:
            rows = db.execute("SELECT * FROM operations WHERE seq>? ORDER BY seq LIMIT ?",
                              (after_seq, limit + 1)).fetchall()
            events = db.execute("SELECT * FROM audit WHERE seq>? ORDER BY seq LIMIT ?",
                                (after_audit_seq, limit + 1)).fetchall()
            has_more = len(rows) > limit or len(events) > limit
            rows, events = rows[:limit], events[:limit]
            result.update(operations=[{"seq": row["seq"], "operation_id": row["id"],
                                       "status": "committed", "receipt": load(row["receipt"])} for row in rows],
                          audit_events=[dict(seq=row["seq"], **load(row["event"])) for row in events],
                          next_after_seq=rows[-1]["seq"] if rows else after_seq,
                          next_after_audit_seq=events[-1]["seq"] if events else after_audit_seq,
                          has_more=has_more)
        return public_projection(result, secrets_for(db)), 0
