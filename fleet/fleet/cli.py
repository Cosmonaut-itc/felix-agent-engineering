import argparse
import getpass
import json
import math
import os
from pathlib import Path
import socket
import time

from . import runner


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def _parser():
    parser = Parser(description="Fleet: opt-in local single-slot pilot")
    parser.add_argument("--state-dir", default=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("submit", "status", "wait", "evidence"):
        sub = commands.add_parser(name)
        sub.add_argument("--state-dir", default=argparse.SUPPRESS)
        if name == "submit":
            sub.add_argument("--client")
            sub.add_argument("--cwd", default=".")
            sub.add_argument("--input", action="append", default=[])
            sub.add_argument("--private", action="append", default=[])
            sub.add_argument("--credential-env", action="append", default=[])
            sub.add_argument("--artifact", action="append", default=[])
            sub.add_argument("argv", nargs=argparse.REMAINDER)
        else:
            sub.add_argument("job")
        if name == "wait":
            sub.add_argument("--timeout", type=float)
    return parser


def _emit(value):
    print(json.dumps(value, ensure_ascii=True, allow_nan=False), flush=True)


def main(argv=None):
    try:
        args = _parser().parse_args(argv)
        state = runner.state_path(getattr(args, "state_dir", None))
        if args.command == "submit":
            command = args.argv
            if command[:1] == ["--"]:
                command = command[1:]
            if not command or not command[0] or any("\x00" in a for a in command):
                raise ValueError("submit requires -- COMMAND [ARG ...] without NUL bytes")
            cwd = Path(os.path.abspath(Path(args.cwd).expanduser()))
            client = args.client or os.environ.get("FLEET_CLIENT")
            client = client or "{}@{}".format(getpass.getuser(), socket.gethostname())
            result, code = runner.submit(state, command, cwd, client, args.input, args.private,
                                         args.artifact, args.credential_env)
            _emit(result)
            return code
        if args.command == "status":
            _emit(runner.view(state, args.job))
            return 0
        if args.command == "evidence":
            value = runner.view(state, args.job)
            _emit({"job": value["job"], "attempt": value["attempt"], "input": value["input"],
                   "input_policy": value["input_policy"], "outcome": value["outcome"],
                   "collection": value["collection"],
                   "evidence": value["evidence"]})
            return 0
        if args.timeout is not None and (not math.isfinite(args.timeout) or args.timeout < 0):
            raise ValueError("timeout must be finite and nonnegative")
        deadline = None if args.timeout is None else time.monotonic() + args.timeout
        delay = 0.2
        while True:
            result = runner.view(state, args.job)
            if result["view"] == "uncertain":
                _emit(result)
                return 3
            if result["state"] == "finished":
                _emit(result)
                return {"succeeded": 0, "failed": 1, "incomplete": 3}[result["outcome"]]
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                result["timed_out"] = True
                _emit(result)
                return 4
            time.sleep(delay if remaining is None else min(delay, remaining))
            delay = min(2.0, delay * 1.5)
    except (OSError, ValueError, KeyError, runner.StateError, RuntimeError) as exc:
        safe = ("state directory must be private to the current user",
                "reserved_state_directory", "unsupported Fleet pilot schema; no migration")
        message = str(exc) if isinstance(exc, runner.inputs.InputError) or str(exc) in safe else "invalid_request_or_state"
        _emit({"error": message})
        return 2
