"""Frames acotados de entrada; DATA/END conserva la respuesta lógica completa."""
import hashlib
import json
import math
import struct
import tempfile
import time
from .protocol import canonical, strict_json, SCHEMA


class TransportError(OSError):
    outcome_unknown = True


class ResponseLimitError(TransportError):
    pass


def _timeout(stream, deadline):
    left = deadline - time.monotonic()
    if left <= 0: raise TransportError('deadline de transporte')
    stream.settimeout(min(30, left))


def _read(stream, size, deadline):
    data = bytearray()
    while len(data) < size:
        _timeout(stream, deadline)
        chunk = stream.recv(size - len(data))
        if not chunk: raise TransportError('respuesta/frame interrumpido')
        data.extend(chunk)
    return bytes(data)


def _send(stream, raw, deadline):
    _timeout(stream, deadline)
    stream.sendall(struct.pack('!I', len(raw)) + raw)


def send_json(stream, value, deadline=None):
    raw = canonical(value).encode('ascii')
    if not 1 <= len(raw) <= 65536: raise TransportError('entrada excede 64 KiB')
    _send(stream, raw, deadline if deadline is not None else time.monotonic() + 300)


def receive_json(stream, deadline=None):
    deadline = deadline if deadline is not None else time.monotonic() + 300
    size, = struct.unpack('!I', _read(stream, 4, deadline))
    if not 1 <= size <= 65536: raise TransportError('longitud de entrada inválida')
    return strict_json(_read(stream, size, deadline).decode('utf-8'))


def send_response(stream, value, *, _maximum=2**64 - 1, _deadline=None):
    deadline, count, checksum, buffer = _deadline or time.monotonic() + 300, 0, hashlib.sha256(), bytearray()
    try:
        encoder = json.JSONEncoder(sort_keys=True, separators=(',', ':'), allow_nan=False, ensure_ascii=True)
        for piece in encoder.iterencode(value):
            raw = piece.encode('ascii')
            offset = 0
            while offset < len(raw):
                take = min(65536 - len(buffer), len(raw) - offset)
                buffer.extend(raw[offset:offset + take]); offset += take
                if len(buffer) == 65536:
                    chunk = bytes(buffer[:65536]); del buffer[:65536]
                    if count + len(chunk) > _maximum: raise ResponseLimitError('contador uint64')
                    _send(stream, b'\x01' + chunk, deadline); checksum.update(chunk); count += len(chunk)
        if buffer:
            if count + len(buffer) > _maximum: raise ResponseLimitError('contador uint64')
            _send(stream, b'\x01' + buffer, deadline); checksum.update(buffer); count += len(buffer)
    except (ValueError, TypeError, OverflowError, RecursionError, MemoryError, ResponseLimitError) as exc:
        code = 'response-limit' if isinstance(exc, ResponseLimitError) else 'serialization-failed'
        _send(stream, b'\x03' + canonical(dict(code=code, operationId=value.get('operationId'), outcomeUnknown=True)).encode('ascii'), deadline)
        raise TransportError(code) from exc
    _send(stream, b'\x02' + struct.pack('!Q', count) + checksum.digest(), deadline)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result: raise ValueError('clave duplicada')
        result[key] = value
    return result


def _float(value):
    result = float(value)
    if not math.isfinite(result): raise ValueError('número no finito')
    return result


def _nonfinite(value): raise ValueError('número no finito')


def receive_response(stream, operation_id, spool_dir, maximum=256 * 1024**2, *, _deadline=None):
    deadline, count, checksum = _deadline or time.monotonic() + 300, 0, hashlib.sha256()
    try:
        if type(maximum) is not int or maximum < 1: raise ResponseLimitError('límite local inválido')
        with tempfile.SpooledTemporaryFile(max_size=1024**2, dir=spool_dir) as spool:
            while True:
                size, = struct.unpack('!I', _read(stream, 4, deadline))
                if not 2 <= size <= 65537: raise TransportError('longitud de respuesta inválida')
                kind = _read(stream, 1, deadline)
                if kind == b'\x01':
                    if count + size - 1 > min(maximum, 2**64 - 1): raise ResponseLimitError('respuesta excede límite local ' + str(maximum))
                    raw = _read(stream, size - 1, deadline)
                    spool.write(raw); checksum.update(raw); count += len(raw)
                elif kind == b'\x02' and size == 41:
                    end = _read(stream, 40, deadline)
                    if not count or end != struct.pack('!Q', count) + checksum.digest(): raise TransportError('END no coincide')
                    spool.seek(0)
                    value = json.loads(spool.read().decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_nonfinite, parse_float=_float)
                    if (not isinstance(value, dict) or value.get('schema') != SCHEMA or
                            value.get('operationId') != operation_id or value.get('result') != 'shadow'):
                        raise TransportError('respuesta ajena')
                    return value
                elif kind == b'\x03' and size <= 4097:
                    error = strict_json(_read(stream, size - 1, deadline).decode('utf-8'))
                    if (not isinstance(error, dict) or set(error) != {'code', 'operationId', 'outcomeUnknown'} or
                            error['operationId'] != operation_id or error['outcomeUnknown'] is not True or
                            error['code'] not in ('serialization-failed', 'response-limit')):
                        raise TransportError('ERROR inválido')
                    raise TransportError(error['code'])
                else: raise TransportError('tipo de respuesta inválido')
    except ResponseLimitError: raise
    except (OSError, ValueError, TypeError, MemoryError, OverflowError, RecursionError) as exc:
        raise TransportError('respuesta no entregada íntegra') from exc
