import argparse
import getpass
import json
import math
import os
from pathlib import Path
import socket
import sqlite3
import time

from . import inputs, operator, query, runner
from .model import Fault, envelope, error
from .storage import state_path


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse text may contain raw credential-bearing arguments.
        raise Fault("invalid_usage")


def _state(parser):
    parser.add_argument("--state-dir", default=argparse.SUPPRESS)


def _parser():
    parser = Parser(description="Fleet opt-in local single-slot authority, fleet-job-api@1")
    _state(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    sub = commands.add_parser("submit")
    _state(sub)
    sub.add_argument("--client")
    sub.add_argument("--cwd", default=".")
    sub.add_argument("--idempotency-key")
    sub.add_argument("--enqueue-only", action="store_true")
    sub.add_argument("--backend", default="pilot_slot")
    for flag in ("input", "private", "artifact", "credential-env"):
        sub.add_argument("--" + flag, action="append", default=[])
    sub.add_argument("argv", nargs=argparse.REMAINDER)
    for name in ("status", "wait", "evidence"):
        sub = commands.add_parser(name)
        _state(sub)
        sub.add_argument("job", nargs="?")
        sub.add_argument("--idempotency-key")
        sub.add_argument("--attempt")
        if name == "wait":
            sub.add_argument("--timeout", type=float)
    sub = commands.add_parser("recover")
    _state(sub)
    sub = commands.add_parser("operator")
    _state(sub)
    actions = sub.add_subparsers(dest="action", required=True)
    for action in ("status", "drain", "assess", "release", "reopen"):
        command = actions.add_parser(action)
        _state(command)
        command.add_argument("--operation-id", required=action != "status")
        if action == "status":
            command.add_argument("--after-seq", type=int, default=0)
            command.add_argument("--limit", type=int, default=50)
        else:
            command.add_argument("--reason", required=True)
            command.add_argument("--backend", default="pilot_slot")
        if action in ("assess", "release"):
            command.add_argument("job")
            command.add_argument("--attempt", required=True)
            command.add_argument("--reservation", required=True)
            command.add_argument("--generation", type=int, required=True)
        if action in ("assess", "release", "reopen"):
            command.add_argument("--drain-id", required=True)
        if action == "release":
            command.add_argument("--proof", required=True)
    return parser


def _emit(value):
    print(json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")), flush=True)


def _run(args, state):
    if args.command == "submit":
        if args.backend != "pilot_slot":
            raise Fault("backend_unsupported")
        argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        if not argv or not argv[0] or any("\x00" in arg for arg in argv):
            raise Fault("invalid_command")
        cwd = Path(os.path.abspath(Path(args.cwd).expanduser()))
        client = args.client or os.environ.get("FLEET_CLIENT") or "{}@{}".format(getpass.getuser(), socket.gethostname())
        return runner.submit(state, argv, cwd, client, args.input, args.private, args.artifact,
                             args.credential_env, args.idempotency_key, args.enqueue_only)
    if args.command == "recover":
        return runner.recover(state)
    if args.command == "operator":
        if args.action == "status":
            return operator.status(state, args.operation_id, args.after_seq, args.limit)
        fields = {name: getattr(args, name, None) for name in
                  ("job", "attempt", "reservation", "generation", "drain_id", "proof")}
        request = operator.validate_request(args.action, args.operation_id, args.reason,
                                             backend=args.backend, **fields)
        return operator.mutate(state, request)
    if args.command == "wait":
        if args.timeout is not None and (not math.isfinite(args.timeout) or args.timeout < 0):
            raise Fault("invalid_timeout")
        deadline = None if args.timeout is None else time.monotonic() + args.timeout
    while True:
        result = query.view(state, args.command, args.job, args.idempotency_key, args.attempt)
        if args.command != "wait":
            return result, 0
        view = result["observation"]
        if view["view"] == "uncertain" or view["completion"] == "incomplete":
            return result, 3
        if view["completion"] == "complete":
            return result, 0 if view["outcome"] == "succeeded" else 1
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            result["timed_out"] = True
            return result, 4
        time.sleep(0.1 if remaining is None else min(0.1, remaining))


def main(argv=None):
    operation = "unknown"
    writer = False
    try:
        args = _parser().parse_args(argv)
        operation = "operator." + args.action if args.command == "operator" else args.command
        writer = args.command in ("submit", "recover") or (args.command == "operator" and args.action != "status")
        state = state_path(getattr(args, "state_dir", None))
        result, code = _run(args, state)
    except KeyboardInterrupt:
        result = error(operation, Fault("client_interrupted", 130), effects="unknown" if writer else "read_only")
        code = 130
    except Fault as fault:
        result = error(operation, fault, effects="no_capacity_change" if operation.startswith("operator.") else
                       "not_admitted" if operation == "submit" else "unknown" if writer else "read_only")
        code = fault.exit_code
        if operation == "submit":
            if code == 2:
                result.update(accepted=False, candidate={"accepted": False, "effects": "not_admitted"},
                              key_binding="not_evaluated")
            elif code == 3:
                result.update(accepted=None, binding="unresolved")
        if operation.startswith("operator.") and "audit_recorded" not in result:
            result["audit_recorded"] = None if code == 3 else False
    except inputs.InputError as exc:
        result = error(operation, Fault(str(exc)), effects="not_admitted" if operation == "submit" else "read_only")
        code = 2
        if operation == "submit":
            result.update(accepted=False, candidate={"accepted": False, "effects": "not_admitted"},
                          key_binding="not_evaluated")
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, RuntimeError):
        code = 3 if writer else 2
        result = error(operation, Fault("persistence_unconfirmed" if writer else "state_unreadable", code),
                       effects="unknown" if writer else "read_only")
        if operation == "submit":
            result.update(accepted=None, binding="unresolved")
    try:
        _emit(result)
    except BrokenPipeError:
        # The confirmed decision remains durable. Do not generate a second receipt.
        return code
    return code
