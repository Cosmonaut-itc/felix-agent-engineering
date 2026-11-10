"""Observación Darwin local. Sin servicio, RPC de reservas ni autoridad administrativa."""
import ctypes as C
import json
import os
import secrets
import select
import socket
import struct
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from .identity import DarwinBSDInfo, Identity, Observation, Timeval
from .protocol import strict_json


class PeerError(OSError):
    """Observación incompleta, identidad cambiante o canal no verificable."""


@dataclass(frozen=True)
class NativePeer:
    identity: Identity
    egid: int
    pidversion: int
    unique_id: int


class _Audit(C.Structure):
    _fields_ = [('value', C.c_uint32 * 8)]


class _Unique(C.Structure):
    _fields_ = [('uuid', C.c_ubyte * 16), ('unique', C.c_uint64), ('parent', C.c_uint64),
               ('version', C.c_int32), ('parent_version', C.c_int32),
               ('reserved2', C.c_uint64), ('reserved3', C.c_uint64)]


class _Watch:
    def __init__(self, pid):
        self.queue = select.kqueue()
        try:
            os.set_inheritable(self.queue.fileno(), False)
            event = select.kevent(pid, filter=select.KQ_FILTER_PROC,
                                  flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT | 0x40,
                                  fflags=select.KQ_NOTE_EXIT)  # EV_RECEIPT=0x40
            receipt = self.queue.control([event], 1, 0)
            if (len(receipt) != 1 or receipt[0].ident != pid or
                    not receipt[0].flags & select.KQ_EV_ERROR or receipt[0].data != 0):
                raise PeerError('registro NOTE_EXIT no confirmado')
            self.check()
        except BaseException:
            self.close()
            raise

    def check(self):
        if self.queue.control(None, 1, 0): raise PeerError('NOTE_EXIT/error pendiente')

    def close(self): self.queue.close()


class _Native:
    def __init__(self):
        if sys.platform != 'darwin': raise PeerError('plataforma Darwin requerida')
        if tuple(map(C.sizeof, (_Audit, _Unique, DarwinBSDInfo, Timeval))) != (32, 56, 136, 16):
            raise PeerError('ABI Darwin no acreditado')
        self.libc = C.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        self.proc = C.CDLL('/usr/lib/libproc.dylib', use_errno=True).proc_pidinfo
        self.proc.argtypes = [C.c_int, C.c_int, C.c_uint64, C.c_void_p, C.c_int]
        self.proc.restype = C.c_int
        self.libc.getpeereid.argtypes = [C.c_int, C.POINTER(C.c_uint32), C.POINTER(C.c_uint32)]
        self.libc.getpeereid.restype = C.c_int
        self.libc.sysctlbyname.argtypes = [C.c_char_p, C.c_void_p, C.POINTER(C.c_size_t), C.c_void_p, C.c_size_t]
        self.libc.sysctlbyname.restype = C.c_int
        bsm = C.CDLL('/usr/lib/libbsm.dylib', use_errno=True)
        self.accessors = [getattr(bsm, 'audit_token_to_' + key) for key in ('pid', 'euid', 'egid', 'pidversion')]
        for fn in self.accessors:
            fn.argtypes, fn.restype = [_Audit], C.c_uint32

    def _sysctl(self, name, value):
        size = C.c_size_t(C.sizeof(value))
        if self.libc.sysctlbyname(name, C.byref(value), C.byref(size), None, 0) != 0:
            raise PeerError('sysctl no verificable')
        return size.value

    def boot(self):
        value = C.create_string_buffer(37)
        if self._sysctl(b'kern.bootsessionuuid', value) != 37 or value.raw[-1:] != b'\0':
            raise PeerError('UUID parcial')
        text = value.raw[:-1].decode('ascii')
        parsed = uuid.UUID(text)
        if not parsed.int or str(parsed) != text.lower(): raise PeerError('UUID inválido')
        return str(parsed)

    def boottime(self):
        value = Timeval()
        if (self._sysctl(b'kern.boottime', value) != C.sizeof(value) or
                value.sec <= 0 or not 0 <= value.usec < 1000000):
            raise PeerError('boottime no verificable')
        return value.sec, value.usec

    def peercred(self, stream):
        uid, gid = C.c_uint32(), C.c_uint32()
        if self.libc.getpeereid(stream.fileno(), C.byref(uid), C.byref(gid)) != 0:
            raise PeerError('getpeereid no verificable')
        return uid.value, gid.value

    def _option(self, stream, option, length):
        raw = stream.getsockopt(0, option, length)
        if len(raw) != length: raise PeerError('getsockopt parcial')
        return raw

    def peerpid(self, stream): return struct.unpack('=i', self._option(stream, 2, 4))[0]

    def token(self, stream):
        value = _Audit.from_buffer_copy(self._option(stream, 6, 32))
        return tuple(fn(value) for fn in self.accessors)

    def _proc(self, pid, flavor, value):
        if self.proc(pid, flavor, 0, C.byref(value), C.sizeof(value)) != C.sizeof(value):
            raise PeerError('proc_pidinfo parcial/ausente')
        return value

    def unique(self, pid):
        value = self._proc(pid, 17, _Unique())
        return bytes(value.uuid), value.unique, value.version & 0xffffffff

    def bsd(self, pid): return self._proc(pid, 3, DarwinBSDInfo())
    def watch(self, pid): return _Watch(pid)


class DarwinPeers:
    def __init__(self, *, _native=None):
        self._native = _native if _native is not None else _Native()
        self._boot = self._native.boot()
        self._invalid = False
        self._streams = {}

    def boot(self):
        current = self._native.boot()
        if current != self._boot: self._invalid = True
        if self._invalid: raise PeerError('boot del runtime cambió')
        return current

    def hardware_uuid(self):
        from .admin import _hardware_uuid
        return _hardware_uuid()

    def health_boot(self):
        before = self.boot()
        value = self._native.boottime()
        if self.boot() != before: raise PeerError('boot cambió durante boottime')
        return before, value

    def _observe(self, executor, stream=None, pid=None):
        before = self.boot()
        if stream is not None:
            cred0, pid0, token0 = self._native.peercred(stream), self._native.peerpid(stream), self._native.token(stream)
            pid = pid0
        if type(pid) is not int or pid <= 0: raise PeerError('PID inválido')
        unique0 = self._native.unique(pid)
        bsd = self._native.bsd(pid)
        unique1 = self._native.unique(pid)
        if stream is not None:
            token1, pid1, cred1 = self._native.token(stream), self._native.peerpid(stream), self._native.peercred(stream)
            if (cred0 != cred1 or pid0 != pid1 or token0 != token1 or
                    token0[:3] != (pid, *cred0) or token0[3] != unique0[2] or
                    cred0 != (bsd.uid, bsd.gid)):
                raise PeerError('credenciales nativas divergentes')
        after = self.boot()
        if (before != after or unique0 != unique1 or unique0[1] <= 0 or bsd.pid != pid or
                not 1 <= bsd.status <= 4 or bsd.start_sec <= 0 or not 0 <= bsd.start_usec < 1000000):
            raise PeerError('nacimiento/boot no verificable')
        identity = Identity(executor, bsd.uid, pid, (bsd.start_sec, bsd.start_usec, unique0[1], unique0[2]), before, before)
        return NativePeer(identity, bsd.gid, unique0[2], unique0[1])

    def observe_process(self, executor, pid):
        """Identidad nativa observada del hijo; no otorga autoridad ni binding."""
        return self._observe(executor, pid=pid).identity

    def capture(self, stream, executor):
        try:
            return self._observe(executor, stream=stream)
        except (OSError, ValueError, OverflowError) as exc:
            raise PeerError('captura nativa no verificable') from exc

    def verify(self, expected):
        if not isinstance(expected, Identity) or len(expected.start_token) != 4: return Observation('unverifiable')
        try:
            actual = self._observe(expected.executor_id, pid=expected.pid).identity
            return Observation('same' if actual == expected else 'confirmed_absent_or_different', actual)
        except (OSError, ValueError, OverflowError):
            return Observation('unverifiable')

    @staticmethod
    def _open(stream):
        if stream.fileno() < 0: raise PeerError('descriptor cerrado')
        if select.select([stream], [], [], 0)[0] and not stream.recv(1, socket.MSG_PEEK):
            raise PeerError('EOF del peer')

    @staticmethod
    def _timeout(stream, deadline):
        left = deadline - time.monotonic()
        if left <= 0: raise PeerError('deadline del reto')
        stream.settimeout(left)

    def _recv(self, stream, size, deadline):
        data = bytearray()
        while len(data) < size:
            self._timeout(stream, deadline)
            chunk = stream.recv(size - len(data))
            if not chunk: raise PeerError('frame incompleto')
            data.extend(chunk)
        return bytes(data)

    def _frame(self, stream, deadline, message=None):
        if message is not None:
            raw = json.dumps(message, separators=(',', ':')).encode('ascii')
            self._timeout(stream, deadline)
            stream.sendall(struct.pack('!I', len(raw)) + raw)
            return
        size, = struct.unpack('!I', self._recv(stream, 4, deadline))
        if not 1 <= size <= 65536: raise PeerError('límite del reto')
        return strict_json(self._recv(stream, size, deadline).decode('utf-8'))

    def challenge_steps(self):
        """Un yield por frame saliente; send recibe el frame remoto completo."""
        nonce = secrets.token_hex(32)
        remote = yield {'kind': 'challenge', 'nonce': nonce}
        if (not isinstance(remote, dict) or set(remote) != {'kind', 'nonce'} or remote['kind'] != 'challenge' or
                not isinstance(remote['nonce'], str) or len(remote['nonce']) != 64 or
                any(c not in '0123456789abcdef' for c in remote['nonce'])):
            raise PeerError('reto inválido')
        proof = yield {'kind': 'proof', 'nonce': remote['nonce']}
        if proof != {'kind': 'proof', 'nonce': nonce}:
            raise PeerError('prueba no corresponde al reto fresco')

    def _challenge(self, stream, deadline):
        steps = self.challenge_steps()
        outgoing = next(steps)
        try:
            while True:
                self._frame(stream, deadline, outgoing)
                outgoing = steps.send(self._frame(stream, deadline))
        except StopIteration:
            pass
        finally:
            steps.close()

    def authentication_steps(self, stream, executor, deadline):
        """Suspende sólo durante el reto; el dueño debe cerrar el generador al abortar."""
        if stream in self._streams: raise PeerError('canal ya autenticado')
        watch, previous_timeout = None, stream.gettimeout()
        try:
            os.set_inheritable(stream.fileno(), False)
            candidate = self.capture(stream, executor)
            watch = self._native.watch(candidate.identity.pid)
            watch.check()
            if self.capture(stream, executor) != candidate: raise PeerError('peer cambió antes del reto')
            yield
            if self.capture(stream, executor) != candidate: raise PeerError('peer cambió tras el reto')
            watch.check(); self._open(stream)
            self._timeout(stream, deadline)
            self._streams[stream] = (candidate, watch)
            watch = None  # El registro es ahora dueño del watch.
            return candidate
        except (OSError, ValueError, OverflowError) as exc:
            raise PeerError('autenticación no verificable') from exc
        finally:
            if watch is not None: watch.close()
            if stream.fileno() >= 0: stream.settimeout(previous_timeout)

    def authenticate(self, stream, executor, *, _deadline=None):
        deadline = _deadline if _deadline is not None else time.monotonic() + 5
        steps = self.authentication_steps(stream, executor, deadline)
        try:
            next(steps)
            self._challenge(stream, deadline)
            try: next(steps)
            except StopIteration as done: return done.value
        except (OSError, ValueError, OverflowError) as exc:
            raise PeerError('autenticación no verificable') from exc
        finally:
            steps.close()

    def alive(self, stream, expected):
        known = self._streams.get(stream)
        if known is None: return False
        try:
            if known[0] != expected: raise PeerError('peer distinto')
            known[1].check(); self._open(stream)
            if self.capture(stream, expected.identity.executor_id) != expected: raise PeerError('peer cambió')
            known[1].check()
            return True
        except (OSError, ValueError, OverflowError):
            self._streams.pop(stream)[1].close()
            return False

    def close(self):
        for _, watch in self._streams.values(): watch.close()
        self._streams.clear()

    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class DarwinHealth:
    def __init__(self, peers, snapshot_host, path):
        self.peers, self.snapshot_host, self.path = peers, snapshot_host, path
        self._pair = peers.health_boot()
        self._invalid = False

    def read(self):
        sample = None
        try:
            before = self.peers.boot(), self.peers._native.boottime()
            self._invalid = self._invalid or before != self._pair
            sample = strict_json(self.path.read_bytes().decode('utf-8'))
            after_time = self.peers._native.boottime()
            # Enclavar la observación antes de que la siguiente lectura pueda fallar.
            self._invalid = self._invalid or after_time != self._pair[1]
            after_uuid = self.peers.boot()
            if before != self._pair or (after_uuid, after_time) != self._pair:
                self._invalid = True
            if self._invalid: raise PeerError('par UUID/boottime cambió')
            sec, usec = before[1]
            timestamp = (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=sec, microseconds=usec))
            witness = self.snapshot_host + '/' + timestamp.isoformat(timespec='microseconds').replace('+00:00', 'Z')
            return 'mini', sample, witness
        except (OSError, ValueError, OverflowError):
            return 'mini', sample, None
