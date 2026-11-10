"""Lector de la coalición de recursos de Darwin: línea base y barrido validado.

Desconectado (SEN-5650, 4a): ni el runtime de hostgate ni Fleet lo importan.
Un barrido sólo prueba algo si los contadores de tareas de la coalición no
cambian a su alrededor y el número de miembros legibles es started − exited.
"""
import ctypes as C
import errno
import sys
import time
import uuid
from dataclasses import dataclass
from typing import FrozenSet, Optional, Tuple, Union
from .darwin_peer import _Unique
from .protocol import canonical, digest, strict_json

MAX_MEMBERS = 256  # 256 miembros caben con holgura en los 64 KiB de strict_json.
SCAN_LIMIT = 0.050
_U32, _U64 = 2 ** 32, 2 ** 64

# Motivos de Retry: el barrido no prueba nada todavía.
COUNTERS_CHANGED, CARDINALITY = 'counters-changed', 'cardinality-mismatch'
SATURATED, IDENTITY_CHANGED, DEADLINE = 'list-saturated', 'identity-changed', 'deadline'
# Motivos de Invalid: la base de la prueba se perdió.
BOOT_CHANGED, COALITION_NULL, COALITION_CHANGED = 'boot-changed', 'coalition-null', 'coalition-changed'
ABI_SIZE, READ_ERROR, OWNER_MISMATCH = 'abi-size', 'read-error', 'owner-mismatch'
COUNTERS_INCOHERENT, TOO_LARGE = 'counters-incoherent', 'baseline-too-large'
# Motivos de RootRejected además de los anteriores.
NOT_IN_COALITION, NOT_DIRECT_CHILD = 'not-in-coalition', 'not-direct-child'


@dataclass(frozen=True)
class Birth:
    """Identidad de pertenencia. El pid solo nunca identifica."""
    boot: str
    pid: int
    unique: int


@dataclass(frozen=True)
class Member:
    birth: Birth
    uid: int  # euid
    ruid: int
    svuid: int
    parent_unique: int
    pidversion: int  # Diagnóstico: exec lo cambia y conserva uniqueid.

    @property
    def elevated(self):
        return self.uid != self.ruid or self.svuid != self.ruid


def _uint(value, bound, low=0):
    return type(value) is int and low <= value < bound


def _body(boot, coalition, members, counters):
    rows = [{'pid': m.birth.pid, 'unique': m.birth.unique, 'uid': m.uid, 'ruid': m.ruid, 'svuid': m.svuid,
             'parentUnique': m.parent_unique, 'pidversion': m.pidversion} for m in members]
    return {'v': 1, 'boot': boot, 'coalition': coalition, 'counters': list(counters), 'members': rows}


@dataclass(frozen=True)
class Baseline:
    """Conjunto completo tomado en un barrido aceptado. Inmutable y verificado al construirse."""
    boot: str
    coalition: int
    members: Tuple[Member, ...]
    counters: Tuple[int, int]  # (tasks_started, tasks_exited)
    digest: str

    def __post_init__(self):
        try:
            boot_ok = isinstance(self.boot, str) and str(uuid.UUID(self.boot)) == self.boot
        except ValueError:
            boot_ok = False
        counters, members = self.counters, self.members
        if (not boot_ok or not _uint(self.coalition, _U64, 1) or type(counters) is not tuple or
                len(counters) != 2 or not all(_uint(x, _U64) for x in counters) or counters[0] < counters[1]):
            raise ValueError('línea base: boot, coalición o contadores inválidos')
        if type(members) is not tuple or not 1 <= len(members) <= MAX_MEMBERS:
            raise ValueError('línea base: miembros fuera de límite')
        for m in members:
            if (type(m) is not Member or type(m.birth) is not Birth or m.birth.boot != self.boot or
                    not _uint(m.birth.pid, 2 ** 31, 1) or not _uint(m.birth.unique, _U64, 1) or
                    not all(_uint(x, _U32) for x in (m.uid, m.ruid, m.svuid, m.pidversion)) or
                    not _uint(m.parent_unique, _U64)):
                raise ValueError('línea base: miembro inválido')
        pids = [m.birth.pid for m in members]
        if pids != sorted(set(pids)) or len(members) != counters[0] - counters[1]:
            raise ValueError('línea base: orden o cardinalidad discordante')
        if self.digest != digest(_body(self.boot, self.coalition, members, counters)):
            raise ValueError('línea base: digest discordante')

    @classmethod
    def build(cls, boot, coalition, members, counters):
        members = tuple(sorted(members, key=lambda m: m.birth.pid))
        return cls(boot, coalition, members, tuple(counters), digest(_body(boot, coalition, members, counters)))

    @property
    def births(self) -> FrozenSet[Birth]:
        return frozenset(m.birth for m in self.members)

    def to_json(self):
        return canonical(dict(_body(self.boot, self.coalition, self.members, self.counters), digest=self.digest))

    @classmethod
    def from_json(cls, text):
        """Ida y vuelta estricta: cualquier forma distinta de la canónica falla, no se repara."""
        value = strict_json(text)
        keys = {'v', 'boot', 'coalition', 'counters', 'members', 'digest'}
        if type(value) is not dict or set(value) != keys or value['v'] != 1 or type(value['members']) is not list:
            raise ValueError('línea base: forma inválida')
        row_keys = {'pid', 'unique', 'uid', 'ruid', 'svuid', 'parentUnique', 'pidversion'}
        members = []
        for row in value['members']:
            if type(row) is not dict or set(row) != row_keys:
                raise ValueError('línea base: miembro con forma inválida')
            members.append(Member(Birth(value['boot'], row['pid'], row['unique']), row['uid'], row['ruid'],
                                  row['svuid'], row['parentUnique'], row['pidversion']))
        counters = value['counters']
        if type(counters) is not list:
            raise ValueError('línea base: contadores inválidos')
        baseline = cls(value['boot'], value['coalition'], tuple(members), tuple(counters), value['digest'])
        if baseline.to_json() != text:
            raise ValueError('línea base: forma no canónica')
        return baseline


@dataclass(frozen=True)
class Complete:
    """Ningún miembro vivo fuera de la línea base, en un barrido aceptado."""
    before: Tuple[int, int]
    after: Tuple[int, int]
    members: int
    vanished: int  # Candidatos omitidos por ESRCH; la concordancia final los cubre.
    survivors: str  # Digest de la línea base y de sus miembros todavía vivos.
    kind = 'complete'


@dataclass(frozen=True)
class Occupied:
    novel: Tuple[Member, ...]  # Toda novedad viva, con credenciales y Member.elevated.
    before: Tuple[int, int]
    after: Tuple[int, int]
    members: int
    vanished: int
    kind = 'occupied'


@dataclass(frozen=True)
class Retry:
    reason: str
    kind = 'retry'


@dataclass(frozen=True)
class Invalid:
    reason: str
    errno: Optional[int] = None
    kind = 'invalid'


ScopeObservation = Union[Complete, Occupied, Retry, Invalid]


@dataclass(frozen=True)
class RootIdentity:
    member: Member
    coalition: int

    @property
    def birth(self): return self.member.birth

    @property
    def parent_unique(self): return self.member.parent_unique


class ReadError(OSError):
    """errno nativo; None si el tamaño devuelto no es el de la estructura."""


class CoalitionUnavailable(Exception):
    """certify no pudo fijar una línea base; observation es Retry o Invalid."""
    def __init__(self, observation):
        super().__init__(observation.reason)
        self.observation = observation


class RootRejected(Exception):
    pass


class _Coalition(C.Structure):
    _fields_ = [('ids', C.c_uint64 * 2), ('reserved', C.c_uint64 * 3)]


class _Short(C.Structure):
    _fields_ = [(n, C.c_uint32) for n in ('pid', 'ppid', 'pgid', 'status')] + [('comm', C.c_char * 16)]
    _fields_ += [(n, C.c_uint32) for n in ('flags', 'uid', 'gid', 'ruid', 'rgid', 'svuid', 'svgid', 'rfu')]


class _Native:
    """Único lugar con ctypes. Sabores 17 (56 B), 20 (40 B) y 13 (64 B) de proc_pidinfo."""
    def __init__(self):
        if sys.platform != 'darwin': raise ReadError(None, 'plataforma Darwin requerida')
        if tuple(map(C.sizeof, (_Unique, _Coalition, _Short))) != (56, 40, 64):
            raise ReadError(None, 'ABI Darwin no acreditado')
        libc = C.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        proc = C.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        self._info = proc.proc_pidinfo
        self._info.argtypes, self._info.restype = [C.c_int, C.c_int, C.c_uint64, C.c_void_p, C.c_int], C.c_int
        self._list = proc.proc_listallpids
        self._list.argtypes, self._list.restype = [C.c_void_p, C.c_int], C.c_int
        self._usage = libc.coalition_info_resource_usage
        self._usage.argtypes, self._usage.restype = [C.c_uint64, C.c_void_p, C.c_size_t], C.c_int
        self._sysctl = libc.sysctlbyname
        self._sysctl.argtypes = [C.c_char_p, C.c_void_p, C.POINTER(C.c_size_t), C.c_void_p, C.c_size_t]
        self._sysctl.restype = C.c_int

    def boot(self):
        value, size = C.create_string_buffer(37), C.c_size_t(37)
        C.set_errno(0)
        if (self._sysctl(b'kern.bootsessionuuid', value, C.byref(size), None, 0) != 0 or
                size.value != 37 or value.raw[-1:] != b'\0'):
            raise ReadError(C.get_errno() or None, 'kern.bootsessionuuid')
        try:
            return str(uuid.UUID(value.raw[:36].decode('ascii')))
        except ValueError as exc:
            raise ReadError(None, 'UUID de boot inválido') from exc

    def listpids(self):
        """(pids, saturada). Buffer holgado; llenar el buffer es saturar."""
        C.set_errno(0)
        n = self._list(None, 0)
        if n <= 0: raise ReadError(C.get_errno() or None, 'proc_listallpids sin tamaño')
        capacity = n + max(256, n // 2)
        buf = (C.c_int * capacity)()
        C.set_errno(0)
        m = self._list(buf, C.sizeof(buf))
        if m <= 0: raise ReadError(C.get_errno() or None, 'proc_listallpids')
        return [p for p in buf[:min(m, capacity)] if p > 0], m >= capacity

    def _pidinfo(self, pid, flavor, value):
        C.set_errno(0)
        n = self._info(pid, flavor, 0, C.byref(value), C.sizeof(value))
        if n == C.sizeof(value): return value
        code = C.get_errno()
        raise ReadError(code if n <= 0 and code else None, 'proc_pidinfo sabor %d' % flavor)

    def unique(self, pid):
        value = self._pidinfo(pid, 17, _Unique())
        return value.unique, value.parent, value.version & 0xffffffff

    def coalition(self, pid): return self._pidinfo(pid, 20, _Coalition()).ids[0]

    def short(self, pid):
        v = self._pidinfo(pid, 13, _Short())
        return v.pid, v.uid, v.ruid, v.svuid, v.status

    def usage(self, cid):
        value = (C.c_uint64 * 2)()  # Prefijo (tasks_started, tasks_exited); el kernel copia MIN(size).
        C.set_errno(0)
        if self._usage(cid, value, C.sizeof(value)) != 0:
            raise ReadError(C.get_errno() or None, 'coalition_info_resource_usage')
        return value[0], value[1]


@dataclass(frozen=True)
class _Scan:
    members: Tuple[Member, ...]
    before: Tuple[int, int]
    after: Tuple[int, int]
    vanished: int


def _failed(exc):
    return Invalid(ABI_SIZE) if exc.errno is None else Invalid(READ_ERROR, exc.errno)


class DarwinCoalition:
    def __init__(self, *, _native=None, _clock=time.monotonic, _limit=SCAN_LIMIT):
        self._native = _native if _native is not None else _Native()
        self._clock, self._limit = _clock, _limit

    def certify(self, owner) -> Baseline:
        """Línea base de la coalición del dueño; CoalitionUnavailable si el barrido no la prueba."""
        if type(owner) is not Birth: raise TypeError('owner debe ser Birth')
        try:
            boot = self._native.boot()
            if boot != owner.boot: raise CoalitionUnavailable(Invalid(BOOT_CHANGED))
            cid = self._owner_coalition(owner)
            if not cid: raise CoalitionUnavailable(Invalid(COALITION_NULL))
            scan = self._scan(boot, cid, {owner.pid: owner.unique})
            if not isinstance(scan, _Scan): raise CoalitionUnavailable(scan)
            if self._owner_coalition(owner) != cid: raise CoalitionUnavailable(Invalid(COALITION_CHANGED))
        except ReadError as exc:
            raise CoalitionUnavailable(_failed(exc)) from exc
        if owner not in {m.birth for m in scan.members}: raise CoalitionUnavailable(Invalid(OWNER_MISMATCH))
        if len(scan.members) > MAX_MEMBERS: raise CoalitionUnavailable(Invalid(TOO_LARGE))
        return Baseline.build(boot, cid, scan.members, scan.after)

    def observe(self, baseline) -> ScopeObservation:
        """Nunca amplía la línea base ni descuenta una novedad por padre, nombre, uid o sesión."""
        if type(baseline) is not Baseline: raise TypeError('baseline debe ser Baseline')
        known = {m.birth.pid: m.birth.unique for m in baseline.members}
        scan = self._scan(baseline.boot, baseline.coalition, known)
        if not isinstance(scan, _Scan): return scan
        if scan.before[0] < baseline.counters[0] or scan.before[1] < baseline.counters[1]:
            return Invalid(COUNTERS_INCOHERENT)
        births = baseline.births
        novel = tuple(sorted((m for m in scan.members if m.birth not in births), key=lambda m: m.birth.pid))
        if novel: return Occupied(novel, scan.before, scan.after, len(scan.members), scan.vanished)
        survivors = sorted([m.birth.pid, m.birth.unique] for m in scan.members)
        return Complete(scan.before, scan.after, len(scan.members), scan.vanished,
                        digest({'baseline': baseline.digest, 'survivors': survivors}))

    def observe_root(self, owner, pid) -> RootIdentity:
        """Raíz hija directa del dueño y en su coalición, leída dos veces entre dos boots."""
        if type(owner) is not Birth or type(pid) is not int or pid <= 0: raise TypeError('owner/pid inválidos')
        n = self._native
        try:
            boot = n.boot()
            cid = self._owner_coalition(owner)
            reads = []
            for _ in range(2):
                first, coalition, short = n.unique(pid), n.coalition(pid), n.short(pid)
                if n.unique(pid) != first: raise RootRejected(IDENTITY_CHANGED)
                reads.append((first, coalition, short))
            after = n.boot()
        except ReadError as exc:
            raise RootRejected(_failed(exc).reason) from exc
        except CoalitionUnavailable as exc:
            raise RootRejected(exc.observation.reason) from exc
        (unique, parent, version), coalition, (spid, uid, ruid, svuid, status) = reads[0]
        if boot != owner.boot or after != boot: raise RootRejected(BOOT_CHANGED)
        if reads[0] != reads[1] or spid != pid or not 1 <= status <= 4: raise RootRejected(IDENTITY_CHANGED)
        if not cid or coalition != cid: raise RootRejected(NOT_IN_COALITION)
        if parent != owner.unique: raise RootRejected(NOT_DIRECT_CHILD)
        return RootIdentity(Member(Birth(boot, pid, unique), uid, ruid, svuid, parent, version), cid)

    def _owner_coalition(self, owner):
        n = self._native
        first = n.unique(owner.pid)
        cid = n.coalition(owner.pid)
        if first[0] != owner.unique or n.unique(owner.pid) != first:
            raise CoalitionUnavailable(Invalid(OWNER_MISMATCH))
        return cid

    def _scan(self, boot, cid, known):
        """Algoritmo de §2: boot, contadores, lista, lecturas dobles, contadores y boot otra vez."""
        n, start = self._native, self._clock()
        try:
            if n.boot() != boot: return Invalid(BOOT_CHANGED)
            before = n.usage(cid)
            pids, saturated = n.listpids()
        except ReadError as exc:
            return _failed(exc)
        if saturated: return Retry(SATURATED)
        members, vanished = [], 0
        for pid in dict.fromkeys(pids):
            if self._clock() - start > self._limit: return Retry(DEADLINE)
            try:
                read = self._read(boot, cid, pid, known)
            except ReadError as exc:
                if exc.errno == errno.ESRCH:
                    vanished += 1
                    continue
                return _failed(exc)
            if isinstance(read, Member): members.append(read)
            elif read is not None: return read
        try:
            after = n.usage(cid)
            if n.boot() != boot: return Invalid(BOOT_CHANGED)
        except ReadError as exc:
            return _failed(exc)
        if before[0] < before[1] or after[0] < after[1]: return Invalid(COUNTERS_INCOHERENT)
        if before != after: return Retry(COUNTERS_CHANGED)
        if len(members) != after[0] - after[1]: return Retry(CARDINALITY)
        if self._clock() - start > self._limit: return Retry(DEADLINE)
        return _Scan(tuple(members), before, after, vanished)

    def _read(self, boot, cid, pid, known):
        n = self._native
        first = n.unique(pid)
        coalition = n.coalition(pid)
        if coalition != cid:
            if known.get(pid) != first[0]: return None
            return Invalid(COALITION_CHANGED) if n.unique(pid) == first else Retry(IDENTITY_CHANGED)
        spid, uid, ruid, svuid, _ = n.short(pid)
        if n.unique(pid) != first or spid != pid: return Retry(IDENTITY_CHANGED)
        return Member(Birth(boot, pid, first[0]), uid, ruid, svuid, first[1], first[2])
