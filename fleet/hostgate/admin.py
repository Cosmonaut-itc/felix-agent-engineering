"""Autoridad administrativa Darwin: configuración inmutable, ancla y lease.

No provisiona archivos ni abre endpoints. Fallos de observación cierran el acceso.
"""
import ctypes as C
import errno
import fcntl
import os
from pathlib import Path
import stat
import struct
import sys
from dataclasses import dataclass
from typing import Mapping
import uuid

from .model import NodeConfig
from .protocol import canonical, demand, digest, identifier, integer, strict_json
from .store import Busy, ShadowStore, StoreError

_ADMIN_UID, _ADMIN_GID = 0, 0
_BINDING = 'hardwareUuid physicalHostId storeId stateDir mode'.split()


class AdminError(ValueError):
    exit_code = 6


class _FrozenDict(dict):
    def _immutable(self, *args, **kwargs): raise TypeError('configuracion inmutable')
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable
    def __deepcopy__(self, memo): return self


def _freeze(value):
    if isinstance(value, dict): return _FrozenDict((k, _freeze(v)) for k, v in value.items())
    if isinstance(value, list): return tuple(_freeze(v) for v in value)
    return value


@dataclass(frozen=True)
class AuthorityLocator:
    config_path: Path
    anchor_path: Path
    lock_path: Path
    socket_path: Path


@dataclass(frozen=True)
class AdminConfig:
    node: NodeConfig
    service_uid: int
    service_gid: int
    hardware_uuid: str
    principals: Mapping
    executor_uids: Mapping
    volumes: Mapping
    health_snapshot_path: Path
    content_digest: str


@dataclass(frozen=True)
class AuthorityAnchor:
    hardware_uuid: str
    host_id: str
    store_id: str
    state_dir: Path
    mode: str


def local_authority():
    root = Path('/Library/Application Support/Fleet/hostgate')
    return AuthorityLocator(root / 'node.json', root / 'authority.json', root / 'authority.lock',
                            Path('/private/var/run/fleet-hostgate/ipc/control.sock'))


def _libc():
    if sys.platform != 'darwin' or C.sizeof(C.c_void_p) != 8: raise AdminError('Darwin 64 requerido')
    return C.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)


def _call(name, result, types, *args):
    fn = getattr(_libc(), name)
    fn.restype, fn.argtypes = result, types
    return fn(*args)


def _hardware_uuid():
    raw, timeout = (C.c_ubyte * 16)(), (C.c_long * 2)(5, 0)
    if _call('gethostuuid', C.c_int, [C.c_void_p, C.c_void_p], raw, timeout):
        raise AdminError('hardware UUID no observable')
    value = uuid.UUID(bytes=bytes(raw))
    if not value.int: raise AdminError('hardware UUID nulo')
    return str(value)


def _ncpu():
    value, size = C.c_int(), C.c_size_t(C.sizeof(C.c_int))
    if (_call('sysctlbyname', C.c_int, [C.c_char_p, C.c_void_p, C.c_void_p, C.c_void_p, C.c_size_t],
              b'hw.ncpu', C.byref(value), C.byref(size), None, 0) or size.value != C.sizeof(value) or value.value <= 0):
        raise AdminError('hw.ncpu no observable')
    return value.value


def _check_filesystem(fd):
    # Darwin statfs64: flags en 64, fstypename[16] en 72, tamaño 2168.
    data = C.create_string_buffer(2168)
    if _call('fstatfs64', C.c_int, [C.c_int, C.c_void_p], fd, data):
        raise AdminError('filesystem no observable')
    flags = struct.unpack_from('=I', data.raw, 64)[0]
    if not flags & 0x1000 or data.raw[72:88].split(b'\0')[0] != b'apfs':
        raise AdminError('se requiere APFS local persistente')


def _check_acl(fd):
    acl = _call('acl_get_fd_np', C.c_void_p, [C.c_int, C.c_int], fd, 0x100)
    if not acl: raise AdminError('ACL no observable')
    try:
        if _call('acl_valid', C.c_int, [C.c_void_p], acl): raise AdminError('ACL invalida')
        entry, tag, mask = C.c_void_p(), C.c_int(), C.c_uint64()
        for index in range(129):
            rc = _call('acl_get_entry', C.c_int, [C.c_void_p, C.c_int, C.c_void_p], acl, index, C.byref(entry))
            if rc == -1 and C.get_errno() == errno.EINVAL: return
            if rc or index == 128: raise AdminError('ACL no observable/acotada')
            if (_call('acl_get_tag_type', C.c_int, [C.c_void_p, C.c_void_p], entry, C.byref(tag)) or
                    _call('acl_get_permset_mask_np', C.c_int, [C.c_void_p, C.c_void_p], entry, C.byref(mask))):
                raise AdminError('entrada ACL no observable')
            # Conservador: ningún ALLOW de escritura, borrado o cambio de permisos.
            if tag.value not in (1, 2) or (tag.value == 1 and mask.value & 0x3574):
                raise AdminError('ACL concede escritura/reemplazo')
    finally:
        _call('acl_free', C.c_int, [C.c_void_p], acl)


def _path(value):
    if (not isinstance(value, str) or not value.startswith('/') or value.startswith('//') or
            str(Path(value)) != value or '..' in Path(value).parts or '~' in value or '$' in value):
        raise AdminError('ruta absoluta canonica requerida')
    return Path(value)


class _Paths:
    """Descriptores fijados, openat sin symlinks y revalidación de toda la cadena."""
    def __init__(self, config=None):
        self.config, self.entries, self.targets = config, [], []

    def _validate(self, fd, kind):
        info = os.fstat(fd)
        directory = kind in ('dir', 'state')
        if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
            raise AdminError('tipo de archivo no permitido')
        uid, gid = _ADMIN_UID, _ADMIN_GID
        if kind == 'lock': gid = self.config.service_gid
        if kind in ('state', 'db'): uid, gid = self.config.service_uid, self.config.service_gid
        if info.st_uid not in ({0, uid} if kind == 'dir' else {uid}): raise AdminError('owner incorrecto')
        mode = stat.S_IMODE(info.st_mode)
        expected = {'config': 0o644, 'anchor': 0o444, 'lock': 0o440, 'state': 0o700, 'db': 0o600}
        if (mode & 0o7022 if kind == 'dir' else mode != expected[kind]): raise AdminError('permisos inseguros')
        if not directory and (info.st_nlink != 1 or info.st_gid != gid): raise AdminError('links/grupo incorrectos')
        _check_acl(fd); _check_filesystem(fd)
        return info.st_dev, info.st_ino

    def open(self, path, kind):
        path = _path(str(path)); self.targets.append((path, kind))
        parent, current = None, Path('/')
        for part in ('/',) + path.parts[1:]:
            if part != '/': current = current / part
            role = kind if current == path else 'dir'
            if self.config and current == self.config.node.state_dir: role = 'state'
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if role in ('dir', 'state'): flags |= os.O_DIRECTORY
            fd = os.open(part, flags, dir_fd=parent)
            self.entries.append((current, fd, role, None))
            identity = self._validate(fd, role)
            self.entries[-1] = (current, fd, role, identity)
            parent = fd
        return parent

    def read(self, path, kind, maximum):
        fd = self.open(path, kind)
        before = os.fstat(fd)
        raw = os.read(fd, maximum + 1)
        after = os.fstat(fd)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise AdminError('documento alterado durante lectura')
        if after.st_size != len(raw) or len(raw) > maximum: raise AdminError('documento administrativo demasiado grande')
        return strict_json(raw.decode('utf-8'))

    def check(self):
        with _Paths(self.config) as current:
            for path, kind in self.targets: current.open(path, kind)
            for old, new in zip(self.entries, current.entries):
                if old[0] != new[0] or old[3] != new[3] or self._validate(old[1], old[2]) != old[3]:
                    raise AdminError('pathname/inode reemplazado')

    def __enter__(self): return self
    def __exit__(self, *args):
        for _, fd, _, _ in reversed(self.entries): os.close(fd)
        self.entries.clear()


def _fields(doc, names):
    if not isinstance(doc, dict) or set(doc) != set(names.split()): raise AdminError('campos administrativos invalidos')


def _ids(values):
    if not isinstance(values, list) or len(set(identifier(v) for v in values)) != len(values):
        raise AdminError('catalogo invalido/duplicado')


def _load_config(locator, paths):
    doc = paths.read(locator.config_path, 'config', 65536)
    _fields(doc, 'schema operatorRevision hardwareUuid physicalHostId serviceUid serviceGid storeId stateDir mode '
            'ncpu budget volumes executors exclusiveResources principals profiles pendingTtlSec heartbeatTtlSec '
            'snapshotHost healthSnapshotPath drained')
    if doc['schema'] != 'fleet/hostgate-node@1' or integer(doc['operatorRevision']) == 0:
        raise AdminError('schema/revision invalidos')
    if doc['hardwareUuid'] != _hardware_uuid(): raise AdminError('hardware UUID ajeno')
    if integer(doc['serviceUid']) != os.geteuid() or integer(doc['serviceGid']) != os.getegid():
        raise AdminError('identidad del servicio incorrecta')
    if type(doc['ncpu']) is not int or doc['ncpu'] != _ncpu(): raise AdminError('ncpu no corresponde al host')
    demand(doc['budget']); _ids(doc['exclusiveResources'])
    for field in ('volumes', 'executors', 'principals', 'profiles'):
        if not isinstance(doc[field], dict) or not doc[field]: raise AdminError('catalogo requerido')
    for volume, location in doc['volumes'].items(): identifier(volume); _path(location)
    if not set(doc['budget']['diskBytes']) <= set(doc['volumes']): raise AdminError('volumen no declarado')
    for executor, spec in doc['executors'].items():
        identifier(executor); _fields(spec, 'platform scopeKind allowedUids')
        if spec['platform'] != 'darwin' or spec['scopeKind'] != 'unix': raise AdminError('ejecutor no local Darwin')
        if not isinstance(spec['allowedUids'], list) or not spec['allowedUids']: raise AdminError('uids requeridos')
        if len({integer(v) for v in spec['allowedUids']}) != len(spec['allowedUids']): raise AdminError('uid duplicado')
    acl = {}
    for uid, spec in doc['principals'].items():
        if str(integer(int(uid))) != uid: raise AdminError('uid no canonico')
        _fields(spec, 'roles executors'); _ids(spec['roles']); _ids(spec['executors'])
        if not spec['roles'] or not set(spec['roles']) <= {'query', 'requester', 'supervisor'}:
            raise AdminError('rol no autorizado')
        for executor in spec['executors']:
            if executor not in doc['executors'] or int(uid) not in doc['executors'][executor]['allowedUids']:
                raise AdminError('ejecutor no autorizado para uid')
        acl['uid:' + uid] = spec['roles']
    minima = {}
    for profile, spec in doc['profiles'].items():
        identifier(profile); _fields(spec, 'minimumDemand'); demand(spec['minimumDemand'])
        if not set(spec['minimumDemand']['diskBytes']) <= set(doc['volumes']): raise AdminError('volumen no declarado')
        minima[profile] = spec['minimumDemand']
    content_digest = digest(doc)
    node = NodeConfig(doc['physicalHostId'], doc['storeId'], tuple(doc['executors']), _freeze(doc['budget']),
                      tuple(doc['exclusiveResources']), _path(doc['stateDir']), doc['pendingTtlSec'],
                      doc['heartbeatTtlSec'], 'cfg_' + content_digest, _freeze(acl), _freeze(minima),
                      doc['snapshotHost'], doc['ncpu'], mode=doc['mode'],
                      executor_scope_kinds=_freeze({e: s['scopeKind'] for e, s in doc['executors'].items()}), drained=doc['drained'])
    return AdminConfig(node, doc['serviceUid'], doc['serviceGid'], doc['hardwareUuid'], _freeze(doc['principals']),
                       _freeze({e: s['allowedUids'] for e, s in doc['executors'].items()}), _freeze(doc['volumes']),
                       _path(doc['healthSnapshotPath']), content_digest)


def _load_anchor(locator, paths):
    doc = paths.read(locator.anchor_path, 'anchor', 4096)
    _fields(doc, 'schema ' + ' '.join(_BINDING))
    if doc['schema'] != 'fleet/hostgate-authority@1' or doc['mode'] != 'shadow': raise AdminError('ancla invalida')
    for key in ('physicalHostId', 'storeId'): identifier(doc[key])
    if str(uuid.UUID(doc['hardwareUuid'])) != doc['hardwareUuid']: raise AdminError('UUID no canonico')
    return AuthorityAnchor(doc['hardwareUuid'], doc['physicalHostId'], doc['storeId'], _path(doc['stateDir']), doc['mode'])


def _guard(operation):
    try: return operation()
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError, StoreError) as exc:
        if isinstance(exc, (AdminError, Busy)): raise
        raise AdminError('autoridad no disponible: ' + str(exc)) from exc


def load_config(locator):
    def load():
        with _Paths() as paths:
            result = _load_config(locator, paths); paths.check(); return result
    return _guard(load)


def load_anchor(locator):
    def load():
        with _Paths() as paths:
            result = _load_anchor(locator, paths); paths.check(); return result
    return _guard(load)


class AuthorityLease:
    def __init__(self, locator, config):
        self.locator, self.config, self.paths = locator, config, None

    def __enter__(self):
        if self.paths is not None: raise AdminError('lease ya abierto')
        self.paths = _Paths(self.config)
        def acquire():
            current = _load_config(self.locator, self.paths)
            self.anchor = _load_anchor(self.locator, self.paths)
            expected = AuthorityAnchor(current.hardware_uuid, current.node.host_id, current.node.store_id,
                                       current.node.state_dir, current.node.mode)
            if current != self.config or self.anchor != expected: raise AdminError('config/ancla no coinciden')
            self.lock_fd = self.paths.open(self.locator.lock_path, 'lock')
            try: fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc: raise Busy('autoridad ocupada') from exc
            self.check()
            self.paths.open(current.node.state_dir, 'state')
            self.paths.open(current.node.state_dir / 'shadow.db', 'db')
            self.check()
            conn = ShadowStore(current.node).connect(writable=False)
            conn.close()
            self.check()
            return self
        try: return _guard(acquire)
        except BaseException:
            self.__exit__(None, None, None); raise

    def check(self):
        def verify():
            if self.paths is None: raise AdminError('lease cerrado')
            self.paths.check()
            if load_config(self.locator) != self.config or load_anchor(self.locator) != self.anchor:
                raise AdminError('revision/ancla alterada; reinicio requerido')
            for suffix in ('-wal', '-shm'):
                path = self.config.node.state_dir / ('shadow.db' + suffix)
                # Los sidecars pueden recrearse; no se fijan inodes de WAL/SHM.
                if path.is_symlink() or path.exists():
                    with _Paths(self.config) as sidecars: sidecars.open(path, 'db'); sidecars.check()
        return _guard(verify)

    def __exit__(self, *args):
        if self.paths is not None: self.paths.__exit__(*args); self.paths = None
