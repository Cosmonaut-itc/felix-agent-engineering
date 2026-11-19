"""Contrato local CC0: validación pura; sin daemon, locks, disco ni transportes.

CC3-P0 añade el contrato interno del emisor stock (plan, claim, sello y
veredicto). No cambia el cable `pueue-client-control@1`.
"""
import datetime
import hashlib
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
# Only an explicit duplicate with the same digest may reevaluate these receipts.
REEVALUATED = ('deferred',)
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
    return strict_decode(raw, MAX_BYTES)


def strict_decode(raw, limit):
    """Decodificador estricto común; el límite es explícito para los originales CC3."""
    require(type(raw) in (bytes, str), "wire_type")
    try:
        data = raw.encode("utf-8") if type(raw) is str else raw
        require(len(data) <= limit, "size")
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


# --- CC3-P0: contrato interno del emisor stock (perfil stock) -----------------
# Documentos originales de una entrega de un solo uso bajo A/client-rpc/<dispatch>/.
# Ninguno viaja por el cable; ninguna regla de esta sección usa `require`, para que
# la paridad regla↔corpus de CC0 siga intacta. Sus reglas tienen su propio corpus
# (corpus/client-rpc-stock-v1.json) y su propio mutador.
RPC_PLAN = "pao-client-rpc-plan@1"
RPC_CLAIM = "pao-client-rpc-claim@1"
RPC_SEAL = "pao-client-rpc-seal@1"
RPC_CORPUS_SCHEMA = "pao-client-rpc-corpus@1"
FRAMES_CORPUS_SCHEMA = "pao-pueue-frames@1"
RPC_MAX_BYTES = 16 * 1024
RPC_PLAN_MAX_BYTES = 32 * 1024  # El plan lleva una petición CC0 completa.
PUEUE_RELEASE = "4.0.4"
# La trama 2 trae PROTOCOL_VERSION de pueue_lib, no la versión del binario (E-stock).
PUEUE_PROTOCOL = "0.31.1"
RPC_OPS = ("hold", "cancel-no-payload", "signal", "signal-manual", "retire-held", "retire-interrupted")
# Única forma cerrada por fase: un solo id, `kill` siempre con señal.
RPC_VERBS = {"hold": "stash", "cancel-no-payload": "start",
             "signal:TERM": "kill:SigTerm", "signal:KILL": "kill:SigKill",
             "signal-manual:TERM": "kill:SigTerm", "signal-manual:KILL": "kill:SigKill",
             "retire-held": "remove", "retire-interrupted": "remove"}
RPC_OUTCOMES = ("not-sent", "replied", "closed")
RPC_REPLIES = ("success", "failure", "unrecognized")
RPC_NOT_SENT = ("startup", "unsupported", "evidence_missing")
RPC_PROCESS_STATES = ("gated", "sent")
# Lo fija la instalación de CC7, nunca el cable. Sólo `filtered` permite atribuir.
EXCLUSION_PROFILES = ("coordinated", "filtered")


def rpc_require(condition, rule):
    """Regla interna CC3; código cerrado propio, mutado aparte de las de CC0."""
    if condition:
        return
    raise Invalid(rule)


def rpc_digest(value):
    """H(x): SHA-256 del JSON canónico, la misma forma que el diario de client_actions."""
    try:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise Invalid("json") from None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def rpc_pid(value):
    rpc_require(type(value) is int and 0 < value <= 2**31 - 1, "rpc_pid")


def rpc_birth(value):
    # Nacimiento nativo: proc_pidinfo en Darwin, boot_id y ticks en Linux.
    rpc_require(type(value) is str and len(value) <= 128 and re.fullmatch(
        r"darwin:[0-9]{1,20}:[0-9]{1,6}|linux:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        r"[0-9a-f]{4}-[0-9a-f]{12}:[0-9]{1,20}", value) is not None, "rpc_birth")


def rpc_process(value):
    keys(value, "pid birth")
    rpc_pid(value["pid"])
    rpc_birth(value["birth"])
    return value


def rpc_life(value):
    """Vida D del daemon: pid y nacimiento nativos más la generación acreditada."""
    keys(value, "pid birth generation")
    rpc_pid(value["pid"])
    rpc_birth(value["birth"])
    digest(value["generation"])
    return value


def validate_rpc_plan(value):
    """Escritor: CC1, bajo control.lock y antes de abrir la compuerta del emisor."""
    rpc_require(type(value) is dict and value.get("schema") == RPC_PLAN, "rpc_schema")
    keys(value, "schema dispatch request group verb task_id daemon pid birth")
    digest(value["dispatch"])
    request = validate_request(value["request"])
    rpc_require(request.get("op") in RPC_OPS and
                value["verb"] == RPC_VERBS.get(request.get("phase")), "rpc_verb")
    rpc_require(type(value["group"]) is str and
                re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value["group"]) is not None, "rpc_group")
    rpc_require(type(value["task_id"]) is int and
                value["task_id"] == request["expected"]["task_id"], "rpc_task")
    life = rpc_life(value["daemon"])
    rpc_require(life["generation"] == request["expected"]["generation"], "rpc_generation")
    rpc_pid(value["pid"])
    rpc_birth(value["birth"])
    return value


def validate_rpc_claim(value):
    """Escritor: el emisor, con link exclusivo, antes de conectar."""
    rpc_require(type(value) is dict and value.get("schema") == RPC_CLAIM, "rpc_schema")
    keys(value, "schema dispatch plan_digest pid birth")
    digest(value["dispatch"])
    digest(value["plan_digest"])
    rpc_pid(value["pid"])
    rpc_birth(value["birth"])
    return value


def validate_rpc_seal(value):
    """Escritor: sólo el ganador del claim, también para el sello negativo."""
    rpc_require(type(value) is dict and value.get("schema") == RPC_SEAL, "rpc_schema")
    keys(value, "schema dispatch plan_digest pid birth peer outcome reply reason receipt")
    digest(value["dispatch"])
    digest(value["plan_digest"])
    digest(value["receipt"])
    rpc_pid(value["pid"])
    rpc_birth(value["birth"])
    peer = value["peer"]
    if peer is not None:
        rpc_process(peer)
    outcome, reply, reason = value["outcome"], value["reply"], value["reason"]
    rpc_require(type(outcome) is str and (
        (outcome == "not-sent" and reply is None and type(reason) is str and reason in RPC_NOT_SENT) or
        (outcome == "replied" and peer is not None and type(reply) is str and reply in RPC_REPLIES
         and reason is None) or
        (outcome == "closed" and peer is not None and reply is None and reason is None)), "rpc_outcome")
    rpc_require(value["receipt"] == rpc_digest({k: v for k, v in value.items() if k != "receipt"}),
                "rpc_receipt")
    return value


def parse_rpc_plan(raw):
    return validate_rpc_plan(strict_decode(raw, RPC_PLAN_MAX_BYTES))


def parse_rpc_claim(raw):
    return validate_rpc_claim(strict_decode(raw, RPC_MAX_BYTES))


def parse_rpc_seal(raw):
    return validate_rpc_seal(strict_decode(raw, RPC_MAX_BYTES))


def rpc_record(record):
    """Fase del diario con un emisor CC3: `process` liga pid, nacimiento y plan."""
    process = record.get("process") if type(record) is dict else None
    rpc_require(type(process) is dict and
                set(process) == {"pid", "birth", "dispatch", "plan_digest", "state"} and
                type(process["state"]) is str and process["state"] in RPC_PROCESS_STATES and
                "request" in record and "digest" in record, "rpc_record")
    rpc_pid(process["pid"])
    rpc_birth(process["birth"])
    digest(process["dispatch"])
    digest(process["plan_digest"])
    return process


def execution_link(plan, claim, seal, record):
    """Forma y enlace de los originales de una entrega. No autentica al escritor.

    claim y seal pueden faltar mientras el emisor trabaja; un sello sin claim no.
    """
    validate_rpc_plan(plan)
    process = rpc_record(record)
    if claim is not None:
        validate_rpc_claim(claim)
    if seal is not None:
        validate_rpc_seal(seal)
    rpc_require(seal is None or claim is not None, "rpc_seal_claim")
    present = [d for d in (process, claim, seal) if d is not None]
    plan_digest = rpc_digest(plan)
    rpc_require(all(d["plan_digest"] == plan_digest for d in present), "rpc_plan_digest")
    rpc_require(all(d["dispatch"] == plan["dispatch"] for d in present), "rpc_dispatch")
    rpc_require(plan["request"] == record["request"] and
                record["digest"] == rpc_digest(record["request"]), "rpc_request")
    # Sólo el dueño durable del claim sella: el mismo pid y nacimiento en todos.
    owners = (plan, process, claim, seal)
    rpc_require(all((d["pid"], d["birth"]) == (plan["pid"], plan["birth"])
                    for d in owners if d is not None), "rpc_owner")
    daemon = plan["daemon"]
    rpc_require(seal is None or seal["peer"] is None or
                seal["peer"] == {"pid": daemon["pid"], "birth": daemon["birth"]}, "rpc_peer")


def rpc_observation(value, posterior):
    """P o Q: vida del daemon e identidad CC0; Q nombra el sello que leyó antes."""
    keys(value, "life identity seal" if posterior else "life identity")
    rpc_life(value["life"])
    identity(value["identity"])
    if posterior and value["seal"] is not None:
        digest(value["seal"])
    return value


def rpc_prior(record, plan, pre):
    """P es la lectura bajo control.lock: identidad esperada y vida del plan."""
    rpc_observation(pre, False)
    rpc_require(pre["identity"] == record["request"]["expected"] and
                pre["life"] == plan["daemon"], "rpc_pre")


def pueue_greeting(frame):
    """Trama 2 completa: cabecera big-endian de 8 bytes y versión cruda de pueue_lib."""
    rpc_require(type(frame) is bytes and len(frame) == 8 + len(PUEUE_PROTOCOL) and
                int.from_bytes(frame[:8], "big") == len(frame) - 8 and
                frame[8:] == PUEUE_PROTOCOL.encode("ascii"), "rpc_greeting")


def rpc_attributed(record, plan, seal, pre, post, exclusion):
    """R+ ∧ P ∧ Q ∧ Ex: el único camino a effect_applied con Pueue stock."""
    if not (seal["outcome"] == "replied" and seal["reply"] == "success"):
        return False
    if exclusion != "filtered":
        return False
    # Q sólo vale leída después de este sello (respuesta recibida) y en la vida D.
    if post is None or post["seal"] != seal["receipt"]:
        return False
    if post["life"] != plan["daemon"]:
        return False
    op, before, after = record["request"]["op"], pre["identity"], post["identity"]
    if op in ("retire-held", "retire-interrupted") and after["state"] != "Absent":
        # Tras R+, otra creación con el mismo id cuenta como Absent de X.
        return (after["task_id"] == before["task_id"] and
                after["generation"] == before["generation"] and
                after["created_at"] != before["created_at"])
    try:
        transition(before, after, op)
    except Invalid:
        return False
    return op != "hold" or after["enqueue_at"] is None


def rpc_verdict(record, plan, claim, seal, emitter_alive, life_alive, pre, post, exclusion):
    """Devuelve (settled, effect, reason) de una fase de cola. Función pura.

    effect True: efecto atribuido. False: no aplicado con prueba positiva
    (not-sent con su motivo, o Failure de un solo id con stale_state). None:
    indistinguible. Los booleanos de vida los calcula el colector.
    """
    rpc_require(type(emitter_alive) is bool and type(life_alive) is bool, "rpc_flag")
    rpc_require(type(exclusion) is str and exclusion in EXCLUSION_PROFILES, "rpc_exclusion")
    rpc_observation(pre, False)
    if post is not None:
        rpc_observation(post, True)
    try:
        execution_link(plan, claim, seal, record)
        rpc_prior(record, plan, pre)
    except Invalid:
        return False, None, "evidence_missing"
    if seal is None:
        if not emitter_alive and not life_alive:
            return True, None, "startup"  # S4
        return False, None, "rpc_unresolved"
    if seal["outcome"] == "not-sent":
        return True, False, seal["reason"]  # S2 y N2
    if rpc_attributed(record, plan, seal, pre, post, exclusion):
        return True, True, None  # S1 con atribución
    if seal["outcome"] == "closed":
        # S3: el cierre del daemon termina la tarea de la conexión (E6), viva o no la vida.
        return True, None, "evidence_missing" if life_alive else "startup"
    if seal["reply"] == "failure":
        return True, False, "stale_state"  # R−: E8c, sin mutación con un solo id
    if seal["reply"] == "success" and exclusion != "filtered":
        return True, None, "unsupported"
    return True, None, "evidence_missing"
