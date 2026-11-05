#!/usr/bin/env python3
"""Installed entrypoint: python3 -I /fixed/install/deskpolicy/cli.py."""

import argparse
import contextlib
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskpolicy import VERSION
from deskpolicy.engine import DeskPolicy, admission_receipt, validate_record
from deskpolicy.model import Invalid, require
from deskpolicy.store import RECEIPT_DIR_MODES, atomic_json, mutex, open_file, read_json, secure_dir


def output(value):
    print(json.dumps(value, sort_keys=True, allow_nan=False), flush=True)


@contextlib.contextmanager
def cycle_timeout():
    def expired(signum, frame):
        raise TimeoutError("writer-cycle-timeout")
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 10)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def parser():
    result = argparse.ArgumentParser(description="PAO desk policy " + VERSION)
    sub = result.add_subparsers(dest="command", required=True)
    publish = sub.add_parser("publicar")
    publish.add_argument("--una-vez", action="store_true")
    publish.add_argument("--desactivar-noche", action="store_true")
    pending = sub.add_parser("pendiente")
    pending.add_argument("--request-id", required=True)
    pending.add_argument("--project", choices=("jale", "hasito"), required=True)
    pending.add_argument("--kind", choices=("chain", "verify", "exclusive", "script"), required=True)
    pending.add_argument("--owner", required=True)
    pending.add_argument("--weight", type=int, required=True)
    pending.add_argument("--retirar", action="store_true")
    for name in ("admitir", "traspasar", "cerrar", "ejecutar"):
        item = sub.add_parser(name)
        item.add_argument("--request-id", required=True)
        item.add_argument("--run-id", required=True)
        if name in ("admitir", "ejecutar"):
            item.add_argument("--lane", type=lambda raw: "any" if raw == "any" else int(raw), required=True)
            item.add_argument("--local-limit", type=int)
        if name != "ejecutar":
            item.add_argument("--pid", type=int, required=True)
            item.add_argument("--mutex-fd", type=int, default=9)
            item.add_argument("--life-fd", type=int, default=8)
            if name == "admitir":
                item.add_argument("--life-fds", nargs="+", metavar="LANE:FD")
            if name in ("traspasar", "cerrar"):
                item.add_argument("--claim-token-fd", type=int, required=True)
            if name == "cerrar":
                item.add_argument("--script-exit-code", type=int)
                item.add_argument("--cause", choices=("completed", "stopped"), default="completed")
        else:
            item.add_argument("--recibo", action="store_true",
                              help="Guardar runs/<run-id>/pao-admission.json antes del guion")
            item.add_argument("argv", nargs=argparse.REMAINDER)
    sub.add_parser("reconciliar")
    status = sub.add_parser("estado")
    status.add_argument("--request-id")
    preflight = sub.add_parser("preflight")
    preflight.add_argument("--preparar", action="store_true")
    preflight.add_argument("--armar", action="store_true")
    preflight.add_argument("--evidencia", type=Path)
    return result


def execute(engine, args):
    if args.argv and args.argv[0] == "--":
        args.argv.pop(0)
    if not args.argv:
        raise Invalid("command-required")
    require(not args.argv[0].startswith("-"), "command-executable")
    supervisor = os.getsid(0)
    runner_group = os.getpgrp()
    require(supervisor in (os.getpid(), os.getppid()) and runner_group == supervisor,
            "wrapper-session-required")
    # remote-run kills its process group. Keep the control process alive to
    # record the script's signal exit; only the script joins the runner group.
    if supervisor != os.getpid():
        os.setpgid(0, 0)
    receipt_path = engine.runtime.receipt_path(args.run_id)
    if args.recibo:
        secure_dir(receipt_path.parent, modes=RECEIPT_DIR_MODES)
    life = None
    code = None
    previous_handlers = {}

    def bind_life(path):
        nonlocal life
        life = open_file(path, create=True)
        return life

    def release_life():
        nonlocal life
        if life is not None:
            fd, life = life, None
            os.close(fd)

    def diagnostic(exc):
        value = {"schema": "pao/desk-close-diagnostic@1", "reason": "close-unproven"
                 if str(exc) == "close-unproven" else "close-error",
                 "detail": str(exc), "script_exit_code": code}
        print(json.dumps(value, sort_keys=True), file=sys.stderr, flush=True)
        return value

    def publish_receipt(value):
        # All callers hold the admission mutex, including retries after a
        # failed close. Preserve the admitted identity and any known result.
        if not args.recibo:
            return value
        try:
            prior = read_json(receipt_path, directory_modes=RECEIPT_DIR_MODES)
        except FileNotFoundError:
            prior = None
        if prior and prior["state"] == "admitted":
            merged = {**prior}
            if prior.get("claim_state") != "closed":
                merged.update({k: value[k] for k in ("claim_state", "script_exit_code", "close_cause")
                               if value.get(k) is not None})
            if value.get("close_error") is not None:
                merged["close_error"] = value["close_error"]
            value = merged
        atomic_json(receipt_path, value, directory_modes=RECEIPT_DIR_MODES)
        return value

    def record_result(error=None):
        record = validate_record(read_json(engine.record_path(args.request_id)), args.request_id)
        claim = record["claim"]
        require(claim is not None and claim["run_id"] == args.run_id, "request-conflict")
        if claim["state"] != "closed":
            claim.update(state="uncertain", script_exit_code=code)
            engine.write_record(record)
        receipt.update(claim_state=claim["state"], script_exit_code=code,
                       close_cause=claim["close_cause"])
        if error is not None:
            receipt["close_error"] = error
        return receipt
    try:
        with mutex(engine.root) as lock:
            previous = None
            if args.recibo:
                try:
                    previous = read_json(receipt_path, directory_modes=RECEIPT_DIR_MODES)
                except FileNotFoundError:
                    pass
                if previous is not None:
                    require(type(previous) is dict and previous.get("schema") == "pao/desk-admission@1"
                            and previous.get("state") in ("admitted", "denied"), "receipt-conflict")
                    require(previous["request_id"] == args.request_id and previous["run_id"] == args.run_id,
                            "receipt-conflict")
                    if previous["state"] == "admitted":
                        current = read_json(engine.record_path(args.request_id))
                        require(current["state"] == "admitted" and current["admitted_run_id"] == args.run_id,
                                "receipt-conflict")
            receipt = engine.admit(args.request_id, args.run_id, args.lane, supervisor,
                                   bind_life, lock, args.local_limit)
            publish_receipt(receipt)
            if not receipt["execute"]:
                release_life()
        output({**receipt, "claim_token": None})
        if not receipt["execute"]:
            return 75 if receipt["state"] == "denied" else 0
        stopped = None
        child = None

        def stop(signum, frame):
            nonlocal stopped
            stopped = signum
            if child is not None and child.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    child.send_signal(signum)

        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            previous_handlers[signum] = signal.signal(signum, stop)
        command = ["/usr/bin/taskset", "-c", ",".join(map(str, receipt["cpus"])), *args.argv]
        if stopped is None:
            child = subprocess.Popen(command, close_fds=True,
                                     preexec_fn=lambda: os.setpgid(0, runner_group))
            if stopped is not None:
                stop(stopped, None)
            code = child.wait()
            if code < 0:
                stopped = stopped or -code
                code = 128 - code
        else:
            code = 128 + stopped
        close_error = None
        try:
            with mutex(engine.root) as lock:
                try:
                    record = engine.close(args.request_id, args.run_id, os.getpid(), life, lock,
                                          claim_token=receipt["claim_token"], script_exit_code=code,
                                          cause="stopped" if stopped else "completed")
                    receipt.update(claim_state=record["claim"]["state"], script_exit_code=code,
                                   close_cause=record["claim"]["close_cause"])
                finally:
                    # No censo can observe a closed claim with its life fd held.
                    release_life()
                publish_receipt(receipt)
        except Exception as exc:
            close_error = diagnostic(exc)
            receipt.update(claim_state="uncertain", script_exit_code=code, close_error=close_error)
            try:
                # A timed-out or failed transaction may not touch the claim
                # until it owns the mutex again. Retry only the result journal.
                with mutex(engine.root):
                    try:
                        record_result(close_error)
                    except Exception as journal_error:
                        diagnostic(journal_error)
                    finally:
                        release_life()
                    publish_receipt(receipt)
            except Exception as recovery_error:
                diagnostic(recovery_error)
        if not args.recibo:
            try:
                output({**receipt, "claim_token": None})
            except Exception as exc:
                diagnostic(exc)
        return code
    finally:
        for signum, handler in previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except Exception as exc:
                if code is None:
                    raise
                diagnostic(exc)
        try:
            release_life()
        except Exception as exc:
            if code is None:
                raise
            diagnostic(exc)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command in ("admitir", "traspasar", "cerrar"):
            engine = DeskPolicy(prepare=False)
            engine.prepare(inherited=args.mutex_fd)
        else:
            engine = DeskPolicy()
        if args.command == "pendiente":
            output(engine.register(args.request_id, args.project, args.kind, args.owner,
                                   args.weight, args.retirar))
        elif args.command == "admitir":
            life_fds = args.life_fd
            if args.life_fds:
                pairs = [tuple(map(int, raw.split(":"))) for raw in args.life_fds]
                require(all(len(pair) == 2 and 1 <= pair[0] <= 12 and pair[1] >= 0 for pair in pairs)
                        and len({pair[0] for pair in pairs}) == len(pairs), "life-fds")
                life_fds = dict(pairs)
            receipt = engine.admit(args.request_id, args.run_id, args.lane, args.pid,
                                   life_fds, args.mutex_fd, args.local_limit)
            output(receipt)
            return 75 if receipt["state"] == "denied" else 0
        elif args.command == "ejecutar":
            return execute(engine, args)
        elif args.command == "traspasar":
            token = os.read(args.claim_token_fd, 66).decode("ascii").strip()
            record = engine.handoff(args.request_id, args.run_id, args.pid, args.life_fd,
                                    args.mutex_fd, claim_token=token)
            output({"schema": "pao/desk-handoff@1", "request_id": args.request_id,
                    "run_id": args.run_id, "keeper": record["claim"]["keepers"][-1]})
        elif args.command == "cerrar":
            token = os.read(args.claim_token_fd, 66).decode("ascii").strip()
            output(engine.close(args.request_id, args.run_id, args.pid, args.life_fd, args.mutex_fd,
                                claim_token=token, script_exit_code=args.script_exit_code, cause=args.cause))
        elif args.command == "reconciliar":
            output(engine.reconcile())
        elif args.command == "estado":
            output(engine.status(args.request_id))
        elif args.command == "preflight":
            if args.preparar:
                engine.prepare()
            evidence = read_json(args.evidencia) if args.evidencia else None
            output(engine.preflight(evidence, args.armar))
        elif args.command == "publicar":
            if args.desactivar_noche:
                output(engine.publish(disable_night=True))
                return 0
            if args.una_vez:
                output(engine.publish())
            else:
                with mutex(engine.root, name="writer.lock"):
                    while True:
                        started = time.monotonic()
                        try:
                            with cycle_timeout():
                                output(engine.publish())
                        except (OSError, ValueError, TypeError) as exc:
                            output({"schema": "pao/desk-error@1", "reason": "census-unknown",
                                    "detail": str(exc), "exit_code": 75})
                        time.sleep(max(0, 20 - (time.monotonic() - started)))
        return 0
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        if args.command == "admitir":
            try:
                policy, _ = engine.read_policy(engine.runtime.sample())
            except (OSError, ValueError, UnboundLocalError):
                policy = {"mode": "day", "revision": None}
            receipt = admission_receipt(args.request_id, args.run_id, policy, args.lane,
                                        reason="census-unknown")
            receipt["detail"] = str(exc)
            output(receipt)
            return 75
        reason = "weight-too-large" if str(exc) == "weight-too-large" else "census-unknown"
        output({"schema": "pao/desk-error@1", "reason": reason,
                "detail": str(exc), "exit_code": 75})
        return 75


if __name__ == "__main__":
    sys.exit(main())
