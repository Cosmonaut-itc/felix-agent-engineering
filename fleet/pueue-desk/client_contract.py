"""Contrato local CC0: validación pura; sin daemon, locks, disco ni transportes."""
import datetime
import json
import re

SCHEMA = "pueue-client-control@1"
CORPUS_SCHEMA = "pueue-client-corpus@1"
MAX_BYTES = 16 * 1024
OPS = ("capabilities", "hold", "cancel-no-payload", "signal", "signal-manual",
       "retire-held", "retire-interrupted", "finish", "status")
LAUNCHERS = ("remote-run", "jale", "apotheca", "vigilante")
STATES = ("Queued", "Stashed", "Running", "Paused", "Done", "Locked", "Absent")
REASONS = ("lock_busy", "host_action", "manual", "pressure", "startup", "locked",
           "stale_state", "not_found", "unsupported", "identity", "invalid_request",
           "idempotency_conflict", "rpc_unresolved", "evidence_missing")
POLICY_EXIT = 64
POLICY_MESSAGE = ("Pueue administrado: esta operación requiere el controlador "
                  "coordinado; actualiza el lanzador")
LOCK_ORDER = ("launcher-run", "launcher-mirror", "launcher-result", "control.lock")
COORDINATED_GATE = False  # Sólo CC8 puede acreditar el host; esto no configura control.py.


class Invalid(ValueError):
    """Sólo un código cerrado, nunca contenido de la petición."""


def require(condition, rule):
    if not condition:
        raise Invalid(rule)


def keys(value, names):
    require(type(value) is dict and set(value) == set(names.split()), "keys")


def token(value):
    require(type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{7,63}", value)
            is not None and ".." not in value, "token")


def digest(value):
    require(type(value) is str and re.fullmatch(r"[a-f0-9]{64}", value) is not None, "digest")


def integer(value):
    require(type(value) is int and 0 <= value <= 2**31 - 1, "integer")


def timestamp(value):
    require(type(value) is str and re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z", value)
        is not None, "timestamp")
    try:
        datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        raise Invalid("timestamp") from None


def bounded(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise Invalid("json") from None
    require(len(raw) <= MAX_BYTES, "size")


def decode(raw):
    """Un único documento UTF-8. Limitar también la lectura a MAX_BYTES+1 en CC1."""
    require(type(raw) in (bytes, str), "wire_type")
    try:
        data = raw.encode("utf-8") if type(raw) is str else raw
        require(len(data) <= MAX_BYTES, "size")
        def pairs(items):
            result = {}
            for key, value in items:
                require(key not in result, "duplicate")
                result[key] = value
            return result
        value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(Invalid("json")))
    except (UnicodeError, ValueError, RecursionError) as error:
        if isinstance(error, Invalid):
            raise
        raise Invalid("json") from None
    return value


def parse(raw, response=False, request=None):
    value = decode(raw)
    return validate_response(value, request) if response else validate_request(value)


def identity(value):
    keys(value, "generation task_id created_at fingerprint attempt state enqueued_at enqueue_at started_at")
    digest(value["generation"])
    digest(value["fingerprint"])
    integer(value["task_id"])
    timestamp(value["created_at"])
    timestamp(value["attempt"])
    for field in ("enqueued_at", "enqueue_at", "started_at"):
        if value[field] is not None:
            timestamp(value[field])
    state = value["state"]
    enqueued, scheduled, started = (value[k] for k in ("enqueued_at", "enqueue_at", "started_at"))
    valid = {
        "Queued": enqueued == value["attempt"] and scheduled is None and started is None,
        "Stashed": enqueued is None and started is None,
        "Running": enqueued is not None and scheduled is None and started is not None,
        "Paused": enqueued is not None and scheduled is None and started is not None,
        "Done": enqueued is not None and scheduled is None and started is not None,
        "Locked": enqueued is None and scheduled is None and started is None,
        "Absent": enqueued is None and scheduled is None and started is None,
    }
    require(type(state) is str and state in valid and valid[state], "phase")
    # El alta stock toma enqueued_at antes de Task::new/created_at.
    require((enqueued is None or value["attempt"] <= enqueued) and
            (started is None or (enqueued is not None and enqueued <= started and
                                 value["created_at"] <= started)), "chronology")
    return value


def acceptance(value, action_id, attempt):
    keys(value, "action_id attempt accepted_at origin receipt")
    token(value["action_id"])
    timestamp(value["attempt"])
    timestamp(value["accepted_at"])
    digest(value["receipt"])
    require(value["origin"] in (None, "rack-panel"), "origin")
    require(value["action_id"] == action_id and value["attempt"] == attempt and
            attempt <= value["accepted_at"], "acceptance_link")


def proof(value, attempt, accepted):
    keys(value, "kind attempt receipt acceptance_receipt exit")
    digest(value["receipt"])
    timestamp(value["attempt"])
    require(type(value["exit"]) is int and 0 <= value["exit"] <= 255, "exit")
    require(value["kind"] in ("no-payload", "terminated", "interrupted") and
            value["attempt"] == attempt, "proof")
    require((value["kind"] == "interrupted" and value["acceptance_receipt"] is None) or
            (value["kind"] != "interrupted" and accepted is not None and value["exit"] == 130 and
             value["acceptance_receipt"] == accepted["receipt"]), "cancel_proof")


def envelope(value):
    bounded(value)
    require(type(value) is dict and value.get("schema") == SCHEMA, "schema")
    require(type(value.get("op")) is str and value["op"] in OPS, "op")


def actor(op, mode, launcher):
    require((not mode.startswith("rack-") or launcher in ("remote-run", "jale")) and
            (mode != "rack-queued" or op in ("hold", "cancel-no-payload", "finish", "status")) and
            (mode != "rack-running" or op in ("signal", "finish", "status")) and
            (op != "retire-held" or mode == "own-legacy") and
            (op != "retire-interrupted" or mode in ("own", "own-legacy")) and
            (op != "cancel-no-payload" or launcher in ("remote-run", "jale")), "scope")
    require((mode == "manual") == (launcher == "vigilante") and
            (op in ("status", "finish") or (mode == "manual") == (op == "signal-manual")), "manual")


def action_projection(value):
    """Quita sólo la envoltura de consulta; conserva la fase y todo su cuerpo."""
    action_op = value.get("action_op")
    require(type(action_op) is str and action_op in OPS[1:-1], "action_op")
    return dict({k: v for k, v in value.items() if k != "action_op"}, op=action_op)


def validate_request(value):
    envelope(value)
    op = value["op"]
    if op == "capabilities":
        scoped = "launcher" in value or "ref" in value
        keys(value, "schema op launcher ref" if scoped else "schema op")
        if scoped:
            require(value["launcher"] in LAUNCHERS, "launcher")
            token(value["ref"])
        return value
    if op == "status":
        validate_request(action_projection(value))
        return value
    base = "schema op launcher ref action_id phase"
    keys(value, base + " mode expected acceptance proof" +
         (" signal" if op in ("signal", "signal-manual") else ""))
    require(value["launcher"] in LAUNCHERS, "launcher")
    token(value["ref"])
    token(value["action_id"])
    expected = identity(value["expected"])
    mode = value["mode"]
    require(mode in ("rack-queued", "rack-running", "own", "own-legacy", "manual"), "mode")
    actor(op, mode, value["launcher"])
    allowed = {"hold": ("Queued",), "cancel-no-payload": ("Stashed",),
               "signal": ("Running", "Paused"), "signal-manual": ("Running",),
               "retire-held": ("Stashed",), "retire-interrupted": ("Done",),
               "finish": ("Done", "Absent")}
    require(expected["state"] in allowed.get(op, STATES), "operation_state")
    require(op not in ("cancel-no-payload", "retire-held") or
            expected["enqueue_at"] is None, "unscheduled")
    if op in ("signal", "signal-manual"):
        require(value["signal"] in ("TERM", "KILL"), "signal")
    require(value["phase"] == (op + ":" + value["signal"] if op in ("signal", "signal-manual") else op),
            "phase_key")
    accepted = value["acceptance"]
    require(op != "hold" or accepted is None, "acceptance_order")
    if accepted is not None:
        acceptance(accepted, value["action_id"], expected["attempt"])
    require((op != "cancel-no-payload" and mode != "rack-running") or accepted is not None,
            "acceptance_required")
    if value["proof"] is not None:
        proof(value["proof"], expected["attempt"], accepted)
    require((op == "finish") == (value["proof"] is not None), "finish_proof")
    if op == "finish":
        terminal_identity(expected, value["proof"], mode)
        if value["proof"]["kind"] == "no-payload":
            start_after_acceptance(expected, accepted)
    return value


def terminal_identity(current, evidence, mode):
    cancellation = evidence["kind"] != "interrupted"
    require((not cancellation or current["state"] == "Done") and
            (not mode.startswith("rack-") or cancellation) and
            (mode != "rack-queued" or evidence["kind"] == "no-payload") and
            (mode != "manual" or current["state"] == "Done"), "retain_id")


def start_after_acceptance(current, accepted):
    require(accepted is not None and current["enqueued_at"] is not None and
            current["enqueued_at"] >= accepted["accepted_at"], "start_time")


def transition(before, after, op):
    identity(before)
    identity(after)
    stable = ("generation", "task_id", "created_at", "fingerprint", "attempt")
    require(all(before[k] == after[k] for k in stable), "identity_link")
    edges = {"hold": {("Queued", "Stashed")},
             "cancel-no-payload": {("Stashed", "Running"), ("Stashed", "Done")},
             "signal": {("Running", "Running"), ("Running", "Done"),
                        ("Paused", "Paused"), ("Paused", "Done")},
             "signal-manual": {("Running", "Running"), ("Running", "Done")},
             "retire-held": {("Stashed", "Absent")},
             "retire-interrupted": {("Done", "Absent")},
             "finish": {("Done", "Done"), ("Absent", "Absent")}}
    require((before["state"], after["state"]) in edges.get(op, set()), "transition")
    if op in ("signal", "signal-manual", "finish"):
        require(all(before[k] == after[k] for k in ("enqueued_at", "enqueue_at", "started_at")),
                "attempt_continuity")
    return after


def invalid_request_response():
    return dict(schema=SCHEMA, op=None, status="rejected", effects=False,
                reservation="none", reason="invalid_request")


def validate_response(value, request=None):
    # Rechazo de sintaxis sin inventar ref/action_id de un cuerpo ilegible.
    if type(value) is dict and value.get("op") is None:
        bounded(value)
        keys(value, "schema op status effects reservation reason")
        require(value["schema"] == SCHEMA, "schema")
        require(value == invalid_request_response() and value["effects"] is False,
                "invalid_response")
        if request is not None:
            validate_request(request)
        return value
    envelope(value)
    if request is not None:
        validate_request(request)
        require(value["op"] == request["op"], "echo")
    op = value["op"]
    if op == "status":
        projected = action_projection(value)
        original = action_projection(request) if request is not None else None
        validate_response(projected, original)
        if request is not None:
            current = value["identity"]
            if current is not None:
                require(all(current[k] == request["expected"][k] for k in
                            ("generation", "task_id", "created_at", "fingerprint", "attempt")), "identity_link")
        return value
    if op == "capabilities":
        scoped = any(k in value for k in ("launcher", "ref", "identity", "reason"))
        keys(value, "schema op version ops" + (" launcher ref identity reason" if scoped else ""))
        require(type(value["version"]) is int and value["version"] == 1 and
                type(value["ops"]) is list and value["ops"] == list(OPS), "capabilities")
        if scoped:
            require(value["launcher"] in LAUNCHERS, "launcher")
            token(value["ref"])
            if value["identity"] is not None:
                identity(value["identity"])
            require((value["reason"] is None if value["identity"] is not None else
                     value["reason"] in ("not_found", "unsupported", "identity")), "capability_identity")
        if request is not None:
            require(scoped == ("launcher" in request) and
                    (not scoped or all(value[k] == request[k] for k in ("launcher", "ref"))), "echo")
        return value
    keys(value, "schema op launcher ref action_id phase mode status effects reservation identity acceptance proof reason")
    require(value["launcher"] in LAUNCHERS, "launcher")
    token(value["ref"])
    token(value["action_id"])
    mode = value["mode"]
    require(mode in ("rack-queued", "rack-running", "own", "own-legacy", "manual"), "mode")
    actor(op, mode, value["launcher"])
    phase = value["phase"]
    require(phase in ((op + ":TERM", op + ":KILL") if op in ("signal", "signal-manual")
                      else (op,)), "phase_key")
    status = value["status"]
    require(type(status) is str and status in ("held", "started", "signalled", "retired", "finished",
                                             "pending", "deferred", "unknown", "rejected"), "status")
    applied = status in ("held", "started", "signalled", "retired")
    require((type(value["effects"]) is bool and value["effects"] == applied) if status != "unknown"
            else value["effects"] is None, "effects")
    reserve = value["reservation"]
    require((reserve == "held" if status in ("held", "started", "signalled", "retired", "pending", "unknown")
             else reserve == "closed" if status == "finished" else reserve in ("none", "held")), "reservation")
    require(status != "deferred" or op not in ("cancel-no-payload", "finish") or reserve == "held",
            "deferred_reservation")
    require((value["reason"] in REASONS if status in ("deferred", "unknown", "rejected", "pending")
             else value["reason"] is None), "reason")
    current = value["identity"]
    require(current is not None or status in ("deferred", "unknown", "rejected"), "response_identity")
    if current is not None:
        identity(current)
    accepted = value["acceptance"]
    if accepted is not None:
        require(current is not None, "response_identity")
        acceptance(accepted, value["action_id"], current["attempt"])
    if value["proof"] is not None:
        require(current is not None, "response_identity")
        proof(value["proof"], current["attempt"], accepted)
    require((status == "finished") == (value["proof"] is not None), "terminal_proof")
    phases = {"held": ("Stashed",), "started": ("Running", "Done"),
              "signalled": ("Running", "Paused", "Done"), "retired": ("Absent",),
              "finished": ("Done", "Absent"), "pending": ("Stashed", "Running", "Paused")}
    require(status not in phases or current is None or current["state"] in phases[status], "response_phase")
    status_ops = {"held": ("hold",), "started": ("cancel-no-payload",),
                  "signalled": ("signal", "signal-manual"),
                  "retired": ("retire-held", "retire-interrupted"), "finished": ("finish",)}
    if status in status_ops:
        # También el resultado observado debe pertenecer al actor autorizado.
        require(any_actor(status_ops[status], mode, value["launcher"]), "scope")
    require(status not in status_ops or op in status_ops[status], "response_op")
    require(reserve != "held" or current is None or current["enqueue_at"] is None, "unscheduled")
    require(op != "hold" or accepted is None, "acceptance_order")
    require(status != "started" or accepted is not None, "acceptance_required")
    if status == "started":
        start_after_acceptance(current, accepted)
    if status == "finished":
        terminal_identity(current, value["proof"], mode)
        if value["proof"]["kind"] == "no-payload":
            start_after_acceptance(current, accepted)
    if request is not None:
        require(all(value[k] == request[k] for k in ("schema", "op", "launcher", "ref", "action_id", "phase")), "echo")
        require("mode" not in request or mode == request["mode"], "echo")
        if "expected" in request and status in status_ops:
            transition(request["expected"], current, op)
        if "acceptance" in request:
            require(value["acceptance"] == request["acceptance"], "acceptance_continuity")
        if status == "finished" and "proof" in request:
            require(value["proof"] == request["proof"], "proof_continuity")
    return value


def any_actor(ops, mode, launcher):
    for op in ops:
        try:
            actor(op, mode, launcher)
            return True
        except Invalid:
            pass
    return False


def negotiated(raw, exit_code):
    """Única negociación positiva. Cualquier fallo bloquea; jamás autoriza raw."""
    value = parse(raw, response=True)
    require(type(exit_code) is int and exit_code == 0 and value["op"] == "capabilities", "negotiation")
    return value
