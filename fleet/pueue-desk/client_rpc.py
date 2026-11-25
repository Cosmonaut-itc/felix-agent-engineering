#!/usr/bin/python3 -I -S
"""CC3-P1: one-shot emitter that speaks stock Pueue 4.0.4. Stdlib only.

CC1 starts it waiting on stdin and only then publishes the plan. The single
selector is `dispatch`, read from stdin; every path is fixed code under
A/client-rpc/<dispatch>/. The emitter never takes a lock, never retries,
never reconnects and never writes a second frame 3. Only the durable claim
winner seals, also negatively. A local abort after frame 3 leaves no seal.
"""
import json
import os
from pathlib import Path
import re
import signal
import socket
import struct
import sys
import tempfile
import time
import types


def _load(name):
    path = Path(__file__).with_name(name)
    module = types.ModuleType(name[:-3])
    module.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), 'exec'), module.__dict__)
    return module


c = _load('client_contract.py')

HANDSHAKE_SECONDS = 2.0   # Connect, peer, secret and frame 2: no effect yet.
LIFE_POLL_SECONDS = 5.0   # No correctness timeout for frame 4, only D checks.
SECRET_MAX_BYTES = 64 * 1024
REPLY_MAX_BYTES = 1024 * 1024
KILL_SIGNALS = {'kill:SigTerm': 'SigTerm', 'kill:SigKill': 'SigKill'}


class Abort(Exception):
    """Frame 3 may have reached the daemon: stay silent, never seal."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


# --- CBOR subset: null, unsigned integer, text, array and text-keyed map -----

def _head(major, number):
    if number < 24:
        return bytes([(major << 5) | number])
    for info, fmt in ((24, '>B'), (25, '>H'), (26, '>I'), (27, '>Q')):
        if number < 1 << (8 * struct.calcsize(fmt)):
            return bytes([(major << 5) | info]) + struct.pack(fmt, number)
    raise ValueError('rpc_cbor')


def encode(value):
    if value is None:
        return b'\xf6'
    if type(value) is int and value >= 0:
        return _head(0, value)
    if type(value) is str:
        data = value.encode('utf-8')
        return _head(3, len(data)) + data
    if type(value) is list:
        return _head(4, len(value)) + b''.join(encode(v) for v in value)
    if type(value) is dict and all(type(k) is str for k in value):
        return _head(5, len(value)) + b''.join(encode(k) + encode(v) for k, v in value.items())
    raise ValueError('rpc_cbor')


def request_payload(verb, task_id):
    """The four closed single-id forms. kill always carries its signal."""
    if type(task_id) is not int or not 0 <= task_id <= 2**31 - 1:
        raise ValueError('rpc_task')
    tasks = {'TaskIds': [task_id]}
    if verb == 'stash':
        value = {'Stash': {'tasks': tasks, 'enqueue_at': None}}
    elif verb == 'start':
        value = {'Start': {'tasks': tasks}}
    elif verb in KILL_SIGNALS:
        value = {'Kill': {'tasks': tasks, 'signal': KILL_SIGNALS[verb]}}
    elif verb == 'remove':
        value = {'Remove': [task_id]}
    else:
        raise ValueError('rpc_verb')
    return encode(value)


def _text(buffer, index):
    if index >= len(buffer) or buffer[index] >> 5 != 3:
        raise ValueError('rpc_cbor')
    info = buffer[index] & 31
    index += 1
    if info < 24:
        length = info
    elif 24 <= info <= 27:
        size = 1 << (info - 24)
        if index + size > len(buffer):
            raise ValueError('rpc_cbor')
        length = int.from_bytes(buffer[index:index + size], 'big')
        index += size
    else:
        raise ValueError('rpc_cbor')
    if index + length > len(buffer):
        raise ValueError('rpc_cbor')
    return buffer[index:index + length].decode('utf-8'), index + length


def reply_class(payload):
    """success | failure | unrecognized. The daemon text is never kept."""
    try:
        if payload[:1] != b'\xa1':
            return 'unrecognized'
        key, index = _text(payload, 1)
        _, index = _text(payload, index)
    except (ValueError, UnicodeError):
        return 'unrecognized'
    if index != len(payload):
        return 'unrecognized'
    return {'Success': 'success', 'Failure': 'failure'}.get(key, 'unrecognized')


def frame(payload):
    return struct.pack('>Q', len(payload)) + payload


# --- Originals: 0600 temporary, fsync, exclusive link, directory fsync ------

def sync_directory(path):
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish(directory, name, document, limit=c.RPC_MAX_BYTES):
    data = canonical(document).encode()
    if len(data) > limit:
        raise ValueError('rpc_size')
    descriptor, temporary = tempfile.mkstemp(prefix='.' + name + '-', dir=str(directory))
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, str(Path(directory) / name))  # FileExistsError: never replaced.
    finally:
        os.unlink(temporary)
    sync_directory(directory)


def read_original(path, limit):
    with open(str(path), 'rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise c.Invalid('size')
    return raw


def folder(authority, dispatch):
    if type(dispatch) is not str or re.fullmatch(r'[0-9a-f]{64}', dispatch) is None:
        raise ValueError('rpc_dispatch')
    return Path(authority) / 'client-rpc' / dispatch


def not_sent(reason, peer=None):
    return dict(peer=peer, outcome='not-sent', reply=None, reason=reason)


def emit(authority, dispatch, own, talk):
    """Claim, then talk, then seal. Returns the seal, or None when silent."""
    directory = folder(authority, dispatch)
    plan = c.parse_rpc_plan(read_original(directory / 'plan.json', c.RPC_PLAN_MAX_BYTES))
    if plan['dispatch'] != dispatch or (plan['pid'], plan['birth']) != tuple(own):
        return None  # Not this emitter's delivery: no claim, no connection.
    plan_digest = c.rpc_digest(plan)
    claim = c.validate_rpc_claim(dict(schema=c.RPC_CLAIM, dispatch=dispatch, plan_digest=plan_digest,
                                      pid=own[0], birth=own[1]))
    try:
        publish(directory, 'claim.json', claim)
    except FileExistsError:
        return None  # The loser leaves without connecting and without a seal.
    try:
        result = talk(plan)
    except Abort:
        return None
    seal = dict(schema=c.RPC_SEAL, dispatch=dispatch, plan_digest=plan_digest,
                pid=own[0], birth=own[1], **result)
    seal['receipt'] = c.rpc_digest(seal)
    c.validate_rpc_seal(seal)
    publish(directory, 'seal.json', seal)
    return seal


# --- Socket conversation ----------------------------------------------------

class Closed(Exception):
    """The daemon closed or reset the connection."""


def peer_pid(connection):
    if sys.platform == 'darwin':
        return connection.getsockopt(0, 0x002)  # SOL_LOCAL, LOCAL_PEERPID; Python 3.9 lacks both names.
    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
    return struct.unpack('3i', raw)[0]


def receive(connection, size, wait=None):
    data = b''
    while len(data) < size:
        try:
            chunk = connection.recv(size - len(data))
        except socket.timeout:
            if wait is None:
                raise
            wait()
            continue
        except ConnectionResetError:
            raise Closed() from None
        if not chunk:
            raise Closed()
        data += chunk
    return data


def socket_talk(plan, socket_path, secret_path, birth):
    daemon = plan['daemon']
    deadline = time.monotonic() + HANDSHAKE_SECONDS
    remaining = lambda: max(.001, deadline - time.monotonic())
    try:
        with open(str(secret_path), 'rb') as stream:
            secret = stream.read(SECRET_MAX_BYTES + 1)
        if len(secret) > SECRET_MAX_BYTES:
            return not_sent('evidence_missing')
        payload = request_payload(plan['verb'], plan['task_id'])
    except (OSError, ValueError):
        return not_sent('evidence_missing')
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        try:
            connection.settimeout(remaining())
            connection.connect(str(socket_path))
        except OSError:
            return not_sent('startup')  # Missing socket, refused or slow: no frame was written.
        try:
            pid = peer_pid(connection)
            born = birth(pid)
        except (OSError, ValueError):
            return not_sent('evidence_missing')
        if pid != daemon['pid'] or born != daemon['birth']:
            return not_sent('startup')  # Another daemon life cannot receive this plan.
        peer = dict(pid=pid, birth=born)
        try:
            connection.settimeout(remaining())
            connection.sendall(frame(secret))
            connection.settimeout(remaining())
            header = receive(connection, 8)
            length = struct.unpack('>Q', header)[0]
            if length > 64:
                return not_sent('unsupported', peer)
            connection.settimeout(remaining())
            greeting = header + receive(connection, length)
        except (OSError, Closed):
            return not_sent('startup', peer)
        try:
            c.pueue_greeting(greeting)
        except c.Invalid:
            return not_sent('unsupported', peer)  # Fail closed before writing the request.
        data, written = frame(payload), 0
        connection.settimeout(None)
        try:
            while written < len(data):
                written += connection.send(data[written:])
        except OSError:
            if written == 0:
                return not_sent('startup', peer)
            raise Abort() from None

        def wait():
            # No correctness timeout: a stopped daemon keeps us waiting. A
            # finished life D already closed the connection on the kernel side.
            try:
                if birth(daemon['pid']) != daemon['birth']:
                    raise Closed()
            except (OSError, ValueError):
                pass

        connection.settimeout(LIFE_POLL_SECONDS)
        try:
            length = struct.unpack('>Q', receive(connection, 8, wait))[0]
            reply = 'unrecognized' if length > REPLY_MAX_BYTES else reply_class(receive(connection, length, wait))
        except Closed:
            return dict(peer=peer, outcome='closed', reply=None, reason=None)
        except OSError:
            raise Abort() from None
        return dict(peer=peer, outcome='replied', reply=reply, reason=None)
    finally:
        connection.close()  # The daemon keeps a replied connection open.


def main(argv):
    for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT, signal.SIGPIPE):
        signal.signal(number, signal.SIG_IGN)
    if len(argv) != 4:
        return 2
    authority, socket_path, secret_path = argv[1:]
    raw = sys.stdin.buffer.read(65)
    if re.fullmatch(rb'[0-9a-f]{64}', raw) is None:
        return 2  # EOF or anything else: the gate never opened.
    birth = _load('editor_prioridad.py').birth
    try:
        own = (os.getpid(), birth(os.getpid()))
        seal = emit(authority, raw.decode('ascii'), own,
                    lambda plan: socket_talk(plan, socket_path, secret_path, birth))
    except (OSError, ValueError):
        return 1
    return 0 if seal else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
