"""Identidad puntual del kernel; no enumera procesos ni ejecuta subprocesos."""
import ctypes
import errno
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple


@dataclass(frozen=True)
class Identity:
    executor_id: str
    uid: int
    pid: int
    start_token: Tuple[int, ...]
    executor_boot: str
    physical_boot: str


@dataclass(frozen=True)
class Observation:
    status: str
    identity: Optional[Identity] = None


class DarwinBSDInfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in
                ('flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid', 'rgid', 'svuid', 'svgid', 'rfu')]
    _fields_ += [('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)]
    _fields_ += [(name, ctypes.c_uint32) for name in ('nfiles', 'pgid', 'pjobc', 'tdev', 'tpgid')]
    _fields_ += [('nice', ctypes.c_int32), ('start_sec', ctypes.c_uint64), ('start_usec', ctypes.c_uint64)]


class Timeval(ctypes.Structure):
    _fields_ = [('sec', ctypes.c_long), ('usec', ctypes.c_int32)]


def parse_linux_stat(text):
    end = text.rfind(')')
    if end < 0 or '(' not in text[:end]: raise ValueError('stat incompleto')
    fields = text[end + 1:].split()
    if len(fields) < 20: raise ValueError('stat incompleto')
    return fields[0], int(fields[1]), int(fields[2]), int(fields[3]), (int(fields[19]),)


class ProcessTable:
    def __init__(self, executor_id, host, proc_root=Path('/proc')):
        self.executor_id, self.host, self.proc_root = executor_id, host, proc_root

    def boot(self):
        try:
            if sys.platform == 'darwin':
                libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
                function = libc.sysctlbyname
                function.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
                function.restype = ctypes.c_int
                value = Timeval(); length = ctypes.c_size_t(ctypes.sizeof(value))
                if function(b'kern.boottime', ctypes.byref(value), ctypes.byref(length), None, 0) != 0:
                    return None
                if length.value != ctypes.sizeof(value) or value.sec <= 0 or not 0 <= value.usec < 1000000:
                    return None
                timestamp = datetime.fromtimestamp(value.sec, timezone.utc).replace(microsecond=value.usec)
                return self.host + '/' + timestamp.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
            if sys.platform.startswith('linux'):
                value = (self.proc_root / 'sys/kernel/random/boot_id').read_text().strip()
                return value if value else None
        except (OSError, ValueError, OverflowError, AttributeError):
            return None
        return None

    def identity(self, pid):
        if type(pid) is not int or pid <= 0: return Observation('unverifiable')
        before = self.boot()
        if not before: return Observation('unverifiable')
        try:
            if sys.platform == 'darwin':
                if ctypes.sizeof(DarwinBSDInfo) != 136: return Observation('unverifiable')
                lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
                fn = lib.proc_pidinfo
                fn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
                fn.restype = ctypes.c_int
                info = DarwinBSDInfo()
                ctypes.set_errno(0)
                count = fn(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
                if count != ctypes.sizeof(info):
                    if count <= 0 and ctypes.get_errno() == errno.ESRCH:
                        return Observation('confirmed_absent_or_different' if before == self.boot() else 'unverifiable')
                    return Observation('unverifiable')
                if info.pid != pid or info.start_sec <= 0 or info.start_usec >= 1000000: return Observation('unverifiable')
                uid, token, zombie = info.uid, (info.start_sec, info.start_usec), info.status == 5
            elif sys.platform.startswith('linux'):
                directory = self.proc_root / str(pid)
                first = (directory / 'stat').read_text()
                state, _, _, _, token = parse_linux_stat(first)
                status = (directory / 'status').read_text().splitlines()
                uid_lines = [line for line in status if line.startswith('Uid:')]
                if len(uid_lines) != 1: return Observation('unverifiable')
                uid = int(uid_lines[0].split()[1])
                second = parse_linux_stat((directory / 'stat').read_text())
                if second[4] != token: return Observation('unverifiable')
                zombie = state in ('Z', 'X') or second[0] in ('Z', 'X')
            else:
                return Observation('unverifiable')
            if self.boot() != before: return Observation('unverifiable')
            if zombie: return Observation('confirmed_absent_or_different')
            return Observation('same', Identity(self.executor_id, uid, pid, token, before, before))
        except FileNotFoundError:
            return Observation('confirmed_absent_or_different' if self.boot() == before else 'unverifiable')
        except (OSError, ValueError, IndexError, AttributeError, OverflowError):
            return Observation('unverifiable')

    def verify(self, expected):
        if not isinstance(expected, Identity) or not expected.start_token or not expected.executor_boot:
            return Observation('unverifiable')
        boot = self.boot()
        if boot is None: return Observation('unverifiable')
        if boot != expected.executor_boot: return Observation('confirmed_absent_or_different')
        observed = self.identity(expected.pid)
        if observed.status == 'same' and observed.identity != expected:
            return Observation('confirmed_absent_or_different', observed.identity)
        return observed


def host_monotonic():
    if sys.platform.startswith('linux'):
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    return time.clock_gettime(time.CLOCK_MONOTONIC)


def from_json(value):
    return Identity(value['executorId'], value['uid'], value['pid'], tuple(value['startToken']),
                    value['executorBoot'], value['physicalBoot'])
