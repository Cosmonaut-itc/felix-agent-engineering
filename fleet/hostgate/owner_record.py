"""Registro propuesto U1; lectura dudosa protege, raíz terminal no marca ended."""
import json
import math
import os
import tempfile
from pathlib import Path
from .protocol import canonical, key, strict_json


def _validate(record):
    fields = {'schema', 'runId', 'jobId', 'attemptId', 'key', 'physicalHostId', 'executorId',
              'boot', 'pid', 'start', 'started', 'ended', 'root', 'recordSeq', 'scope'}
    if not isinstance(record, dict) or set(record) != fields or record['schema'] != 'fleet/host-owner@1':
        raise ValueError('registro incompleto')
    for name in ('runId', 'jobId', 'attemptId', 'physicalHostId', 'executorId', 'boot'):
        value = record[name]
        if not isinstance(value, str) or not value or '/' in value or '\x00' in value or value in ('.', '..'):
            # boot contiene host/fecha: se valida aparte.
            if name != 'boot' or not isinstance(value, str) or not value or '\x00' in value:
                raise ValueError('identificador de registro invalido')
    key(record['key'])
    if type(record['pid']) is not int or record['pid'] <= 0 or type(record['recordSeq']) is not int or record['recordSeq'] <= 0:
        raise ValueError('PID/secuencia invalida')
    if not isinstance(record['start'], list) or not record['start'] or any(type(x) is not int or x < 0 for x in record['start']):
        raise ValueError('inicio exacto ausente')
    root = record['root']
    if root is not None:
        if (not isinstance(root, dict) or type(root.get('pid')) is not int or root['pid'] <= 0
                or root['pid'] == record['pid'] or not isinstance(root.get('start'), list)
                or not root['start'] or any(type(x) is not int or x < 0 for x in root['start'])):
            raise ValueError('raiz sin identidad exacta separada del supervisor')
    if type(record['started']) not in (float, int) or not math.isfinite(record['started']) or record['started'] <= 0:
        raise ValueError('started invalido')
    if not isinstance(record['scope'], dict) or record['scope'].get('status') not in ('unconfirmed', 'confirmed-empty'):
        raise ValueError('ambito invalido')
    # La publicación de fin real espera evidencia y aceptación U1; v1 sólo protege.
    if record['ended'] is not None: raise ValueError('enforcement-unavailable: fin sin proveedor acreditado')
    return record


def publish_owner(record, path):
    _validate(record)
    path = Path(path)
    if path.name != 'owner.json' or any(p.is_symlink() for p in (path,) + tuple(path.parents)):
        raise ValueError('ruta de registro invalida')
    if path.exists():
        previous = _validate(strict_json(path.read_text()))
        if (record['recordSeq'] <= previous['recordSeq'] or record['key'] != previous['key']
                or record['pid'] != previous['pid'] or record['start'] != previous['start']):
            raise ValueError('registro obsoleto o dueño distinto')
    fd, tmp = tempfile.mkstemp(prefix='.owner-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(canonical(record) + '\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def read_owner(path):
    try:
        path = Path(path)
        if path.is_symlink(): raise ValueError('symlink')
        with path.open() as stream: record = _validate(strict_json(stream.read(65537)))
        return dict(protection='protect', record=record)
    except (OSError, ValueError, TypeError, KeyError):
        return dict(protection='uncertain', reason='registro incompleto/ilegible; conservar proteccion')
