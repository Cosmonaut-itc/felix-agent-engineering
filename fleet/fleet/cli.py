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
    for name in ("submit", "status", "wait"):
        sub = commands.add_parser(name)
        sub.add_argument("--state-dir", default=argparse.SUPPRESS)
        if name == "submit":
            sub.add_argument("--client")
            sub.add_argument("--cwd", default=".")
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
            cwd = Path(args.cwd).expanduser().resolve(strict=True)
            if not cwd.is_dir():
                raise ValueError("cwd must be a directory")
            client = args.client or os.environ.get("FLEET_CLIENT")
            client = client or "{}@{}".format(getpass.getuser(), socket.gethostname())
            result, code = runner.submit(state, command, cwd, client)
            _emit(result)
            return code
        if args.command == "status":
            _emit(runner.view(state, args.job))
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
        _emit({"error": str(exc)})
        return 2
