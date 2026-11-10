"""Test-only entry: reinstall hostgate fixture seams in every process, then run the public parser.

Usage: python -I -B host_reservation.py <hostgate-root> fleet <fleet argv...>
       python -I -B host_reservation.py <hostgate-root> query

Production code never reads a variable or argument to choose its authority; only
this launcher redirects ``admin.local_authority`` to a private fixture root.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import sys
from unittest.mock import patch

FLEET = Path(__file__).resolve().parents[2]


def seams(root):
    """fixture_policy, the authority locator and a socket that resolves the 104-byte sun_path."""
    for path in (str(FLEET), str(FLEET / "tests")):
        if path not in sys.path:
            sys.path.insert(0, path)
    from hostgate import admin
    from hostgate.tests.test_admin import fixture_policy
    from hostgate.tests.test_ipc import FixtureSocket

    class RootSocket(FixtureSocket):
        def connect(self, address):
            previous = os.getcwd()
            os.chdir(str(Path(address).parent))
            try:
                return super().connect(address)
            finally:
                os.chdir(previous)

    locator = admin.AuthorityLocator(*(root / name for name in
                                       ("node.json", "authority.json", "authority.lock", "control.sock")))
    stack = ExitStack()
    stack.enter_context(fixture_policy(root))
    stack.enter_context(patch.object(admin, "local_authority", return_value=locator))
    stack.enter_context(patch.object(socket, "socket", RootSocket))
    return stack


def main(argv):
    root, role, rest = Path(argv[0]).resolve(), argv[1], argv[2:]
    with seams(root):
        if role == "query":
            from hostgate.client import HostgateClient
            from hostgate.tests.test_ipc import query
            with HostgateClient.connect(mode="effective") as client:
                print(json.dumps(client.call(query(), timeout=10), sort_keys=True), flush=True)
            return 0
        from fleet import cli
        return cli.main(rest)


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main(sys.argv[1:]))
