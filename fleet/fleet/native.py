"""Native process observations and handled-SIGINT dispatch boundary."""
import contextlib
import ctypes
import os
from pathlib import Path
import pwd
import signal
import socket
import subprocess
import sys

from .model import Fault


def identity(pid):
    result = {"pid": pid, "uid": os.getuid(), "host": socket.gethostname(),
              "namespace": None, "boot_id": None, "birth": None, "alive": False}
    try:
        if sys.platform.startswith("linux"):
            result["boot_id"] = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            result["namespace"] = os.readlink("/proc/{}/ns/pid".format(pid))
            fields = Path("/proc/{}/stat".format(pid)).read_text().rsplit(")", 1)[1].split()
            result["birth"] = fields[19]
            result["alive"] = fields[0] not in ("Z", "X")
            result["uid"] = Path("/proc/{}".format(pid)).stat().st_uid
        elif sys.platform == "darwin":
            result["boot_id"] = subprocess.check_output(
                ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"], timeout=2,
                stderr=subprocess.DEVNULL, text=True).strip().lower()
            result["namespace"] = "darwin-host"

            class BSDInfo(ctypes.Structure):
                _fields_ = [("prefix", ctypes.c_uint32 * 12),
                            ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
                            ("suffix", ctypes.c_uint32 * 6),
                            ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64)]

            info = BSDInfo()
            lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            query = lib.proc_pidinfo
            query.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                              ctypes.c_void_p, ctypes.c_int]
            query.restype = ctypes.c_int
            if query(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) == ctypes.sizeof(info):
                result["birth"] = "{}:{}".format(info.start_sec, info.start_usec)
                result["uid"] = info.prefix[5]
                result["alive"] = info.prefix[1] != 5
    except (OSError, ValueError, IndexError, AttributeError, subprocess.SubprocessError):
        pass
    return result


def verifiable(value):
    return bool(value and value.get("boot_id") and value.get("namespace") and value.get("birth")
                and value.get("uid") == os.getuid())


def confirmed(value):
    return verifiable(value) and value.get("alive") is True and value == identity(value["pid"])


def actor():
    value = identity(os.getpid())
    if not verifiable(value):
        raise Fault("operator_identity_unverifiable")
    return dict(value, account=pwd.getpwuid(os.getuid()).pw_name, role="local-authority-owner")


def group_occupied(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


@contextlib.contextmanager
def dispatch_boundary():
    """Do not let a handled SIGINT strand a locally provable pre-spawn claim.

    This handler records SIGINT instead of raising inside Popen or a transaction.
    On exit it raises only after the caller has preserved creation/binding or a
    positive closed never-created. SIGKILL and unhandled signals have no guarantee.
    """
    previous = signal.getsignal(signal.SIGINT)
    interrupted = []

    def defer(number, frame):
        interrupted.append(number)

    signal.signal(signal.SIGINT, defer)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)
        if interrupted:
            raise KeyboardInterrupt
