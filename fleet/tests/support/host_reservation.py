"""Test-only entry: reinstall hostgate fixture seams in every process, then run the public parser.

Usage: python -I -B host_reservation.py <hostgate-root> fleet <fleet argv...>
       python -I -B host_reservation.py <hostgate-root> query
       python -I -B host_reservation.py <hostgate-root> server
       python -I -B host_reservation.py <hostgate-root> supervise <S argv...>   (R launches it)

Files in the root steer one process at a time: ``.cut`` (hostgate checkpoint,
consumed into ``.cut-hit`` when H reaches it), ``.wire`` (one altered response,
consumed into ``.wire-hit``), ``.pause-<name>`` (the first S there renames it to
``.held-<name>``, writes ``.at-<name>`` and waits until it is deleted) and
``.shadow-reply`` (S receives a reply with shadow fields instead of calling
hostgate). ``.reservar`` and ``.offers`` log what H saw; ``.trace`` logs each
checkpoint and exit of S as ``<pid> <name>``.

Production code never reads a variable or argument to choose its authority; only
this launcher redirects ``admin.local_authority`` to a private fixture root.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import sys
import time
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


def server(root):
    """Campaign harness of hostgate with one-shot cuts: proves H reached each barrier."""
    from hostgate import ipc, ipc_io, wire
    from hostgate.admission import Hostgate
    from hostgate.service import Runtime
    from hostgate.tests import test_ipc_campaign as campaign

    def checkpoint(world, name):
        cut = root / ".cut"
        if cut.exists() and cut.read_text() == name:
            os.replace(str(cut), str(root / ".cut-hit"))
            raise OSError("injected " + name)

    def frames(value):
        path = root / ".wire"
        if not path.exists():
            yield from wire.response_frames(value)
            return
        altered = campaign.altered_frames(root, value)
        first = next(altered)
        os.replace(str(path), str(root / ".wire-hit"))
        yield first
        yield from altered

    original_reservar, original_validate = Hostgate.reservar, ipc.validate

    def reservar(gate, request, auth):
        with open(str(root / ".reservar"), "a") as log:
            log.write(request["operationId"] + "\n")
        return original_reservar(gate, request, auth)

    def validate(value, operation=None):
        if operation == "reservar":
            with open(str(root / ".offers"), "a") as log:
                log.write(value.get("operationId", "") + "\n")
        return original_validate(value, operation)

    with patch.object(Runtime, "checkpoint", checkpoint), patch.object(ipc_io, "response_frames", frames), \
            patch.object(Hostgate, "reservar", reservar), patch.object(ipc, "validate", validate):
        return campaign.progress_server(root)


def trace(root, name):
    with open(str(root / ".trace"), "a") as log:
        log.write("{} {}\n".format(os.getpid(), name))


def hooks(root):
    """S after exec: pauses at named checkpoints and an optional reply with shadow fields."""
    from fleet import host_reservation
    from hostgate.client import HostgateClient
    stack = ExitStack()

    def checkpoint(name):
        trace(root, name)
        held = root / (".held-" + name)
        try:  # one S takes the pause; the test deletes .held-<name> to resume it
            os.replace(str(root / (".pause-" + name)), str(held))
        except FileNotFoundError:
            return
        (root / (".at-" + name)).write_text(str(os.getpid()))
        deadline = time.monotonic() + 60
        while held.exists() and time.monotonic() < deadline:
            time.sleep(0.02)

    original = HostgateClient.call

    def call(client, request, **options):
        if request.get("operation") == "reservar" and (root / ".shadow-reply").exists():
            key = {"storeId": client.store_id, "reservationId": "e_" + "0" * 32, "generation": 1}
            return {"schema": request["schema"], "operationId": request["operationId"], "result": "effective",
                    "mode": "effective", "storeId": client.store_id, "configRevision": client.revision,
                    "wouldGrant": True, "simulationKey": key, "simulationState": "reserved",
                    "pendingId": "p_shadow", "reasons": [], "exitCode": 0}
        return original(client, request, **options)

    stack.enter_context(patch.object(host_reservation, "_checkpoint", checkpoint))
    stack.enter_context(patch.object(HostgateClient, "call", call))
    return stack


def relaunch(root):
    """R: S is launched through this file so the seams survive its exec (-I, minimal env)."""
    try:
        from fleet import host_reservation
    except ImportError:
        return ExitStack()  # base without the adapter: the public CLI decides
    if not hasattr(host_reservation, "supervisor_command"):
        return ExitStack()  # main admits host jobs but has no sending adapter yet
    product = host_reservation.supervisor_command

    def command(state, intent, handoff_fd, ready_fd):
        argv = product(state, intent, handoff_fd, ready_fd)
        return argv[:3] + [str(Path(__file__).resolve()), str(root)] + argv[4:]

    stack = ExitStack()
    stack.enter_context(patch.object(host_reservation, "supervisor_command", command))
    stack.enter_context(hooks(root))
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
        if role == "server":
            return server(root)
        if role == "supervise":
            from fleet import host_reservation
            try:
                with hooks(root):
                    host_reservation.main(["supervise"] + rest)
            finally:
                trace(root, "exit")
            return 0
        from fleet import cli
        with relaunch(root):
            return cli.main(rest)


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main(sys.argv[1:]))
