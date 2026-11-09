"""Inventario durable del store: un WAL ausente equivale a uno de cero bytes."""
import hashlib


def durable_inventory(paths, *, metadata=False):
    result = {}
    for path in paths:
        if path.name.endswith('-shm'):
            continue
        content = path.read_bytes()
        if path.name.endswith('-wal') and not content:
            continue
        digest = hashlib.sha256(content).hexdigest()
        stat = path.stat()
        result[path.name] = (stat.st_size, stat.st_mtime_ns, digest) if metadata else digest
    return result
