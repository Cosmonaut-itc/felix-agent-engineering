#!/usr/bin/python3 -I
"""Respuesta terminal para los consumidores anteriores al corte a Pueue."""
import argparse
import json
import os
from pathlib import Path
import pwd
import sys

MESSAGE = (
    "deskpolicy retirado el 2026-11-12: el desk toma turno con Pueue. "
    "Actualiza tu rama a origin/main y usa el lanzador del repositorio."
)


def close_previous(argv):
    """Única excepción al rechazo: acreditar una claim ya admitida."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--mutex-fd", type=int, default=9)
    parser.add_argument("--life-fd", type=int, default=8)
    parser.add_argument("--claim-token-fd", type=int, required=True)
    parser.add_argument("--script-exit-code", type=int)
    parser.add_argument("--cause", choices=("completed", "stopped", "sync-timeout"), default="completed")
    args = parser.parse_args(argv)
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    sys.path.insert(0, str(home / ".local/lib/pao-deskpolicy/v1"))
    try:
        from deskpolicy.engine import DeskPolicy
        from deskpolicy.store import Runtime
        engine = DeskPolicy(Runtime(check_installation=False), prepare=False)
        token = os.read(args.claim_token_fd, 66).decode("ascii").strip()
        record = engine.close(args.request_id, args.run_id, args.pid, args.life_fd,
                              args.mutex_fd, claim_token=token,
                              script_exit_code=args.script_exit_code, cause=args.cause)
        print(json.dumps(record, sort_keys=True))
        return 0
    except (ImportError, OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        print(json.dumps({"schema": "pao/desk-error@1", "reason": "census-unknown",
                          "detail": str(exc), "exit_code": 75}))
        return 75


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "cerrar":
        return close_previous(sys.argv[2:])
    print(MESSAGE, file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main())
