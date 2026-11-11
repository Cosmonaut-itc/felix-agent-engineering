"""host-reservation@1 adapter: an effective hostgate reservation persisted before any start.

R (submit/recover) opens a round and pairs a fresh supervisor S; S alone sends,
confirms, claims and reaches the creation barrier. The authority always comes
from ``admin.local_authority()``; nothing here reads an environment variable or
argument to choose it. The Fleet lock is never held during pairing or IPC.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys
from typing import Optional

if __package__:
    from . import inputs, native, storage
    from .model import (Closure, Fault, HostConfirmation, HostHeld, HostIntent, HostRound, dump, now)
    from .storage import Ledger
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from fleet import inputs, native, storage
    from fleet.model import (Closure, Fault, HostConfirmation, HostHeld, HostIntent, HostRound, dump, now)
    from fleet.storage import Ledger

SCHEMA = "fleet/host-reservation@1"
# hostgate raises an empty demand to the profile minimum; slots:1 keeps it non-empty.
DEMAND = {"cpuMilli": 0, "memoryBytes": 0, "diskBytes": {}, "slots": 1}
TIMEOUT = 30
SENDS = 3


@dataclass(frozen=True)
class HostObservation:
    round: Optional[HostRound]
    action: str  # none, retired, held, released, closed_unused or rejected


def _checkpoint(name):
    """Test seam reinstalled in S after exec; production does nothing."""


def authority():
    """Effective hostgate configuration, or backend_unavailable without creating state."""
    try:
        from hostgate import admin
        config = admin.load_config(admin.local_authority())
    except Exception:
        raise Fault("backend_unavailable") from None
    if config.node.mode != "effective":
        raise Fault("backend_unavailable")
    return config


def state_of(round_):
    if isinstance(round_, HostConfirmation):
        return "confirmed"
    return "held" if isinstance(round_, HostHeld) else intent_of(round_).state


def intent_of(round_):
    return round_ if isinstance(round_, HostIntent) else round_.intent


def prepare(db, attempt, spec, requester):
    """Caller holds the transaction. Head of FIFO, free slot, no drain, no open round; else None."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='host_rounds'").fetchone():
        raise Fault("backend_unavailable")
    config = authority()
    row = db.execute("SELECT a.id,a.job FROM attempts a JOIN jobs j ON j.id=a.job "
                     "WHERE a.state='queued' ORDER BY j.seq LIMIT 1").fetchone()
    rounds = storage.host_rounds(db, attempt)
    if (db.control["drained"] or db.execute("SELECT attempt FROM slot").fetchone()[0] is not None
            or row is None or row["id"] != attempt
            or any(state_of(r) not in storage.HOST_TERMINAL for r in rounds)):
        return None
    permitted = config.principals.get(str(os.geteuid()), {}).get("executors", ())
    executor = next((name for name in config.node.executors if name in permitted), None)
    if executor is None:
        raise Fault("backend_unavailable")
    ordinal = len(rounds) + 1
    operation = storage.host_operation_id(attempt, ordinal)
    request = {"schema": SCHEMA, "operationId": operation, "operation": "reservar", "jobId": row["job"],
               "attemptId": attempt, "executorId": executor, "physicalHostId": config.node.host_id,
               "demand": DEMAND, "exclusiveResources": [], "profileId": spec.profile_id}
    intent = HostIntent(attempt, ordinal, operation, request, config.node.host_id, executor,
                        config.node.store_id, config.node.revision, requester)
    storage.put_host_intent(db, intent)
    return intent


def supervisor_command(state, intent, handoff_fd, ready_fd):
    return [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "supervise", str(state),
            intent.attempt, str(intent.ordinal), str(handoff_fd), str(ready_fd)]


def _log(role, exc):
    """Diagnostics only (S: supervisor.log; R: stderr); never part of the JSON contract."""
    print("host {}: {}: {}".format(role, type(exc).__name__, exc), file=sys.stderr, flush=True)


def _retire(db, intent, reason):
    with db.transaction():
        storage.retire_host_round(db, intent, reason)


def launch(db, intent, requester):
    """R: offer, Popen S with a private handoff pipe, approve the observed child, keep the channel until S reports."""
    from hostgate.client import request_supervisor
    from hostgate.darwin_peer import DarwinPeers
    try:
        session = request_supervisor(intent.request, mode="effective")
    except Exception:
        _retire(db, intent, "hostgate_unavailable")  # nothing was sent; queued with a reason
        return
    child, fds = None, []
    try:
        fds.extend(os.pipe())  # registered pair by pair: a failed second pipe still closes the first
        fds.extend(os.pipe())
        handoff_r, handoff_w, ready_r, ready_w = fds
        fd = os.open(db.state / "supervisor.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT | inputs.NOFOLLOW, 0o600)
        with os.fdopen(fd, "ab", buffering=0) as log:
            info = os.fstat(log.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                    info.st_mode & 0o077 or info.st_nlink != 1):
                raise OSError("invalid_supervisor_log")
            child = subprocess.Popen(supervisor_command(db.state, intent, handoff_r, ready_w),
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                                     close_fds=True, pass_fds=(handoff_r, ready_w),
                                     env={"PATH": os.defpath, "LANG": "C"})
        for item in (handoff_r, ready_w):
            os.close(item)
            fds.remove(item)
        os.write(handoff_w, json.dumps(session.handoff.__dict__).encode("utf-8"))
        os.close(handoff_w)
        fds.remove(handoff_w)
        # The audit token changes with each exec: observe S only once it says it runs as itself.
        started, _, _ = select.select([ready_r], [], [], 5)
        if not started or os.read(ready_r, 1) != b"S":
            raise OSError("supervisor_not_started")
        peers = DarwinPeers()
        try:
            identity = peers.observe_process(intent.executor_id, child.pid)
        finally:
            peers.close()
        session.approve(identity)
        # S reports once reservar settled. EOF or timeout proves nothing about S.
        select.select([ready_r], [], [], TIMEOUT * SENDS)
    except Exception as exc:
        _log("requester", exc)
        if child is None:
            _retire(db, intent, "supervisor_spawn_failed")
        # Once S exists only its CAS decides; recover retires a round S never sent.
    finally:
        session.close()
        for item in fds:
            os.close(item)


def classify(intent, result):
    """(kind, detail, key, receipt) from an integral effective reply of this operation; None: repeat it."""
    from hostgate.protocol import validate_result
    try:
        validate_result(result, "effective", intent.store_id)
        source = ("same-channel-replay" if "replayed" in result else "reply") if result.get("reservationKey") else None
        storage.host_receipt(dict(operation_id=intent.operation_id, config_revision=intent.config_revision,
                                  confirmation_source=source, state="held"), result.get("reservationKey"), result)
    except (ValueError, KeyError, TypeError, RecursionError, OverflowError):
        return "held", "response_not_effective", None, None
    granted = result.get("granted") is True
    key = result.get("reservationKey")
    state = result.get("state")
    if (result.get("storeId") != intent.store_id
            or result.get("configRevision") != intent.config_revision
            or result.get("operationId") != intent.operation_id):
        return "held", "response_not_effective", None, None
    if result.get("exitCode") == 3:
        return None
    key_ok = (isinstance(key, dict) and set(key) == {"storeId", "reservationId", "generation"}
              and key["storeId"] == intent.store_id and str(key["reservationId"]).startswith("e_"))
    if result.get("replayed") is True:
        original, current = result.get("originalResult") or {}, result.get("currentState") or {}
        if (key_ok and original.get("granted") is True and original.get("reservationKey") == key
                and original.get("exitCode") == 0 and original.get("reasons") == []
                and original.get("state") == "reserved" and result.get("exitCode") == 0
                and result.get("reasons") == []
                and current.get("state") == "reserved" and current.get("reasons") == []):
            return "confirmed", "same-channel-replay", key, result
        return "held", "replay_not_reserved", key if key_ok else None, result
    if granted and key_ok and result.get("exitCode") == 0 and state == "reserved" and result.get("reasons") == []:
        return "confirmed", "reply", key, result
    if (result.get("granted") is False and result.get("exitCode") == 1 and key is None
            and result.get("reasons") and "state" not in result):
        return "rejected", None, None, result
    return "held", "response_unrecognized", None, None


def _reserve(client, intent):
    request = intent.request
    for _ in range(SENDS):
        try:
            result = client.call(request, timeout=TIMEOUT)
        except Exception:
            return "held", "channel_lost", None, None  # never another operation or channel
        verdict = classify(intent, result)
        if verdict is not None:
            return verdict
        # Same channel, same operation: an exit 3 reply never licenses a new operationId.
    return "held", "outcome_unknown", None, None


def _call(client, operation, key, **fields):
    try:
        return client.call(dict(schema=SCHEMA, operation=operation, key=key, **fields), timeout=TIMEOUT)
    except Exception:
        return None


def require_creation_provider(confirmation):
    """No accredited creation provider exists in this PR: the permit never opens."""
    raise Fault("enforcement_unavailable", 3)


def barrier(db, intent, generation, identity, reason):
    """Close the permit unconsumed, hold the round, record never_created only as a local fact."""
    command = {"kind": "never_created", "exit_code": None, "signal": None, "reason": reason}
    with db.transaction():
        if not storage.hold_host(db, intent, reason):
            raise Fault("host_round_changed", 3, "unknown")
        changed = db.execute(
            "UPDATE attempts SET gate='closed',command=?,recorded_outcome='incomplete',collection=?,"
            "publication=?,ended_at=? WHERE id=? AND generation=? AND state='starting' AND gate='available' "
            "AND supervisor=?", (dump(command), dump({"state": "incomplete", "errors": [reason]}),
                                 dump({"state": "incomplete", "root_sha256": None}), now(), intent.attempt,
                                 generation, dump(identity))).rowcount
        if changed != 1:
            raise Fault("host_round_changed", 3, "unknown")
        db.event(intent.attempt, "finished", "supervisor:" + str(identity["pid"]), reason)


def _report(ready_fd):
    try:
        os.write(ready_fd, b"R")
    except OSError:
        pass


def supervise(db, intent, handoff, ready_fd):
    """S: pair, CAS prepared->outcome_unknown, send, confirm-and-claim, heartbeat, barrier, doubt."""
    from hostgate.client import HostgateClient
    if (handoff.request_digest != storage.request_sha256(intent.request)
            or handoff.config_revision != intent.config_revision):
        raise Fault("host_correlation_invalid", 3)
    identity = native.identity(os.getpid())
    if not native.verifiable(identity):
        raise Fault("supervisor_identity_unverifiable", 3)
    try:
        client = HostgateClient.connect(handoff, mode="effective")
    except Exception as exc:
        _log("supervisor", exc)
        return  # nothing sent: the round stays prepared for recover
    with client:
        _checkpoint("before_unknown")
        with db.transaction():
            owned = storage.mark_host_unknown(db, intent, identity)
        if not owned:
            return  # recover retired it first: never send late
        _checkpoint("outcome_unknown")
        kind, detail, key, receipt = _reserve(client, intent)
        if kind == "rejected":
            with db.transaction():
                storage.reject_host_round(db, intent, receipt)
            return
        if kind == "held":
            with db.transaction():
                source = ("same-channel-replay" if "replayed" in receipt else "reply") if key else None
                storage.hold_host(db, intent, detail, key, receipt, source)
            if key:
                _call(client, "dudosa", key, operationId=intent.operation_id + ":dudosa",
                      reason=detail, evidenceIds=[])
            return
        with db.transaction():
            generation = storage.confirm_host_and_claim(db, intent, key, receipt, detail,
                                                        intent.requester, identity)
        _report(ready_fd)
        if generation is None:
            _call(client, "dudosa", key, operationId=intent.operation_id + ":dudosa",
                  reason="claim_unavailable", evidenceIds=[])
            return
        _checkpoint("confirmed")
        beat = _call(client, "latido", key, operationId=intent.operation_id + ":latido:1", seq=1)
        try:
            if (not beat or beat.get("exitCode") != 0 or beat.get("reasons")
                    or beat.get("state") != "reserved" or beat.get("reservationKey") != key):
                raise Fault("heartbeat_uncertain", 3)
            with db.transaction():
                db.execute("UPDATE host_rounds SET heartbeat_seq=1 WHERE attempt=? AND ordinal=? "
                           "AND state='confirmed'", (intent.attempt, intent.ordinal))
                db.revision()
            require_creation_provider(HostConfirmation(intent, key, receipt, detail, identity))
        except Fault as fault:
            barrier(db, intent, generation, identity, fault.code)
            _checkpoint("barrier")
            _call(client, "dudosa", key, operationId=intent.operation_id + ":dudosa",
                  reason=fault.code, evidenceIds=[])


def reconcile(db, row):
    """recover: retire a round never sent, hold one whose S is gone, mirror hostgate's original closure.

    Never adopts S nor reopens. Time, an empty list, uncertain, a new boot or a new
    supervisor release nothing: only the closure hostgate already keeps leaves held or closing.
    """
    with db.transaction():
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='host_rounds'").fetchone():
            raise Fault("backend_unavailable")
        rounds = storage.host_rounds(db, row["id"])
        current = next((r for r in rounds if state_of(r) not in storage.HOST_TERMINAL), None)
        action = "none"
        if current is not None:
            state, intent = state_of(current), intent_of(current)
            if state == "prepared":
                storage.retire_host_round(db, intent, "recovered_before_send")
                action = "retired"
            elif (state in ("outcome_unknown", "confirmed") + storage.CREATION_PHASES
                  and not native.confirmed(intent.supervisor)):
                storage.hold_host(db, intent, "supervisor_lost")
                if state == "confirmed":
                    db.execute("UPDATE attempts SET gate='closed',detail='host_supervisor_lost' "
                               "WHERE id=? AND gate='available'", (row["id"],))
                action = "held"
                state = "held"
    if action == "retired":
        _checkpoint("retired")  # after commit, without the Fleet lock
    elif current is not None and state in ("held", "closing"):
        estado = _estado(intent_of(current))  # without the Fleet lock
        if estado is not None:
            with db.transaction():
                action = _mirror(db, intent_of(current), estado) or action
    return HostObservation(current, action)


def _estado(intent):
    """One estado over an effective query channel. Any failure, TransportError included, is an
    unknown result: None, and the round keeps its doubt."""
    from hostgate.client import HostgateClient
    try:
        with HostgateClient.connect(mode="effective") as client:
            return client.call({"schema": SCHEMA, "operationId": intent.operation_id + ":estado",
                                "operation": "estado"}, timeout=TIMEOUT)
    except Exception as exc:
        _log("recover", exc)
        return None


def _mirror(db, intent, estado):
    """Copy hostgate's original outcome of this exact round, or nothing; the decoder re-checks it."""
    from hostgate.protocol import validate_result
    current = next((r for r in storage.host_rounds(db, intent.attempt) if intent_of(r).ordinal == intent.ordinal), None)
    if current is None or state_of(current) not in ("held", "closing"):
        return None
    key = getattr(current, "key", None)
    try:
        validate_result(estado, "effective", intent.store_id)
        # hostgate keys an operation by (principal, operationId): another principal may reuse the id.
        grants = [p for p in estado["pending"]
                  if (p["principal"], p["operationId"]) == (storage.host_principal(intent), intent.operation_id)]
        if (estado["configRevision"] != intent.config_revision or estado["physicalHostId"] != intent.physical_host_id
                or len(grants) != 1 or grants[0]["requestDigest"] != storage.request_sha256(intent.request)
                or (grants[0]["attemptId"], grants[0]["jobId"]) != (intent.attempt, intent.request["jobId"])):
            return None
        grant = grants[0]
        if key is None and grant["state"] == "rejected":
            outcome, change = "rejected", lambda: storage.reject_discovered(db, current, grant)
        else:
            # A grant discovered without its local key (held keeps no creation then) closes unused.
            discovered = grant if key is None else None
            key = key or grant["reservationKey"]
            found = [r for r in estado["reservations"] if r["key"] == key]
            if (key is None or grant["reservationKey"] != key or len(found) != 1
                    or (found[0]["state"], found[0]["effectiveState"]) != ("released", "released")
                    or (found[0]["jobId"], found[0]["attemptId"], found[0]["profileId"], found[0]["executorId"],
                        found[0]["physicalHostId"]) != (intent.request["jobId"], intent.attempt,
                                                        intent.request["profileId"], intent.executor_id,
                                                        intent.physical_host_id)):
                return None
            closure = Closure(found[0]["permit"], found[0]["closureEvidence"])
            if getattr(current, "creation", None) is None:
                outcome, change = "closed_unused", lambda: storage.close_unused(db, current, closure,
                                                                                discovered=discovered)
            else:
                outcome, change = "released", lambda: storage.release_host(db, current, closure)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return None
    db.execute("SAVEPOINT mirror")
    try:
        done = change()
    except Fault as fault:
        if fault.code != "host_correlation_invalid":
            raise
        done = False  # the closure does not correlate with this round: nothing is copied
    db.execute("RELEASE mirror" if done else "ROLLBACK TO mirror")
    if not done:
        db.execute("RELEASE mirror")
    return outcome if done else None


def main(argv):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    from hostgate.client import SupervisorHandoff
    state, attempt, ordinal, handoff_fd, ready_fd = Path(argv[1]), argv[2], int(argv[3]), int(argv[4]), int(argv[5])
    os.write(ready_fd, b"S")  # after this process's last exec, before pairing
    try:
        with os.fdopen(handoff_fd, "rb") as source:
            handoff = SupervisorHandoff(**json.loads(source.read(65536).decode("utf-8")))
        with Ledger(state, create=True, initialize=False) as db:
            intent = next(intent_of(r) for r in storage.host_rounds(db, attempt) if intent_of(r).ordinal == ordinal)
            supervise(db, intent, handoff, ready_fd)
    finally:
        _report(ready_fd)
        os.close(ready_fd)


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    if len(sys.argv) != 7 or sys.argv[1] != "supervise":
        raise SystemExit("internal host supervisor entry point")
    try:
        main(sys.argv[1:])
    except BaseException:
        raise SystemExit(3) from None
