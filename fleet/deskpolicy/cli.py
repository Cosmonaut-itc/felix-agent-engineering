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
from deskpolicy.engine import DeskPolicy, admission_receipt
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
    for name in ("admitir", "cerrar", "ejecutar"):
        item = sub.add_parser(name)
        item.add_argument("--request-id", required=True)
        item.add_argument("--run-id", required=True)
        if name != "cerrar":
            item.add_argument("--lane", type=int, required=True)
            item.add_argument("--local-limit", type=int)
        if name != "ejecutar":
            item.add_argument("--pid", type=int, required=True)
            item.add_argument("--mutex-fd", type=int, default=9)
            item.add_argument("--life-fd", type=int, default=8)
        else:
            item.add_argument("--recibo", action="store_true",
                              help="Guardar runs/<run-id>/pao-admission.json antes del guion")
            item.add_argument("argv", nargs=argparse.REMAINDER)
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
    if os.getsid(0) != os.getpid():
        os.setsid()
    record = read_json(engine.record_path(args.request_id))
    path = engine.life_path(record["project"], args.lane)
    receipt_path = engine.runtime.receipt_path(args.run_id)
    if args.recibo:
        secure_dir(receipt_path.parent, modes=RECEIPT_DIR_MODES)
    life = open_file(path, create=True)
    try:
        with mutex(engine.root) as lock:
            previous = None
            if args.recibo:
                try:
                    previous = read_json(receipt_path, directory_modes=RECEIPT_DIR_MODES)
                except FileNotFoundError:
                    pass
                if previous is not None:
                    require(previous["request_id"] == args.request_id and previous["run_id"] == args.run_id,
                            "receipt-conflict")
            receipt = engine.admit(args.request_id, args.run_id, args.lane, os.getpid(),
                                   life, lock, args.local_limit)
        if args.recibo:
            if previous is None or previous["state"] != "admitted":
                atomic_json(receipt_path, receipt, directory_modes=RECEIPT_DIR_MODES)
        output(receipt)
        if not receipt["execute"]:
            return 75 if receipt["state"] == "denied" else 0
        command = ["/usr/bin/taskset", "-c", ",".join(map(str, receipt["cpus"])), *args.argv]
        child = subprocess.Popen(command, close_fds=True)
        code = child.wait()
        with mutex(engine.root) as lock:
            engine.close(args.request_id, args.run_id, os.getpid(), life, lock)
        return 128 - code if code < 0 else code
    finally:
        os.close(life)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        engine = DeskPolicy()
        if args.command == "pendiente":
            output(engine.register(args.request_id, args.project, args.kind, args.owner,
                                   args.weight, args.retirar))
        elif args.command == "admitir":
            receipt = engine.admit(args.request_id, args.run_id, args.lane, args.pid,
                                   args.life_fd, args.mutex_fd, args.local_limit)
            output(receipt)
            return 75 if receipt["state"] == "denied" else 0
        elif args.command == "ejecutar":
            return execute(engine, args)
        elif args.command == "cerrar":
            output(engine.close(args.request_id, args.run_id, args.pid, args.life_fd, args.mutex_fd))
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
