#!/usr/bin/python3
"""Productor one-shot de mini-vigilante/muestra@1, sin dependencias."""
import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import fcntl
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

VOLUME = '/System/Volumes/Data'
DEFAULT_CONFIG = Path(__file__).with_name('pao-mini-vigilante-umbrales.json' if Path(__file__).name == 'pao-mini-vigilante.py' else 'umbrales.json')
DEFAULT_OUTPUT = Path.home() / '.local/state/pao/mini-vigilante/ultimo.json'


def ejecutar_comando(argv):
    return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=5).stdout.strip()


def ahora_utc():
    return datetime.now(timezone.utc)


def iso_utc(value):
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def leer_umbrales(path):
    try:
        with Path(path).open(encoding='utf-8') as stream:
            config = json.load(stream)
    except (OSError, ValueError):
        raise ValueError('umbrales: archivo ausente, ilegible o JSON inválido') from None
    keys = {'memLevelPct', 'memPressure', 'freeBytes.' + VOLUME}
    if not isinstance(config, dict) or set(config) != keys:
        raise ValueError('umbrales: se requieren memLevelPct, memPressure y freeBytes.' + VOLUME)
    for name, maximum in [('memLevelPct', 100), ('memPressure', 4), ('freeBytes.' + VOLUME, 9007199254740991)]:
        pair = config[name]
        if not isinstance(pair, dict) or set(pair) != {'warn', 'crit'}:
            raise ValueError('umbrales: par warn/crit inválido')
        if any(type(v) is not int or not 0 <= v <= maximum for v in pair.values()):
            raise ValueError('umbrales: valor entero fuera de rango')
        if name == 'memPressure':
            if pair != {'warn': 2, 'crit': 4}:
                raise ValueError('umbrales: memPressure requiere warn=2 y crit=4')
        elif pair['crit'] >= pair['warn']:
            raise ValueError('umbrales: crit debe ser menor que warn')
    return config


def numero(text, integer=False, minimum=0, maximum=math.inf):
    if not re.fullmatch(r'\d+' if integer else r'\d+(?:\.\d+)?', text):
        raise ValueError('formato')
    value = int(text) if integer else float(text)
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError('rango')
    return value


def swap_bytes(text):
    match = re.search(r'\bused\s*=\s*(\d+(?:\.\d+)?)\s*([KMGT]?)(?= |$)', text)
    if not match:
        raise ValueError('formato')
    value = Decimal(match[1]) * (1024 ** (' KMGT'.index(match[2]) if match[2] else 0))
    return int(value)


def load_one(text):
    match = re.fullmatch(r'\{\s*(\d+(?:\.\d+)?)\s+\d+(?:\.\d+)?\s+\d+(?:\.\d+)?\s*\}', text)
    if not match:
        raise ValueError('formato')
    return numero(match[1])


def bajo(value, pair, inclusive=False):
    if value < pair['crit'] or (inclusive and value == pair['crit']):
        return 'crit'
    if value < pair['warn'] or (inclusive and value == pair['warn']):
        return 'warn'
    return 'ok'


def capturar(config, comando=None, reloj=None, statvfs=None):
    comando = comando or ejecutar_comando
    reloj = reloj or ahora_utc
    statvfs = statvfs or os.statvfs
    errors = []

    def fallo(stage, exc):
        if isinstance(exc, subprocess.TimeoutExpired):
            code, message = 'timeout', 'lectura excedió 5 s'
        elif isinstance(exc, (OSError, subprocess.CalledProcessError)):
            code, message = 'read_failed', 'lectura no disponible'
        else:
            code, message = 'invalid_value', 'lectura inválida'
        errors.append(dict(stage=stage, code=code, message=message))
        return message

    host = None
    try:
        host = comando(['/usr/sbin/scutil', '--get', 'LocalHostName'])
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,62}', host):
            host = None
            raise ValueError('host')
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        fallo('host', exc)

    boot = dict(bootId=None, lastBootUtc=None, error=None)
    try:
        raw = comando(['/usr/sbin/sysctl', '-n', 'kern.boottime'])
        match = re.match(r'^\{\s*sec\s*=\s*(\d+),\s*usec\s*=\s*(\d+)\s*\}', raw)
        if not match or not 0 <= int(match[2]) < 1000000 or host is None:
            raise ValueError('boot')
        # La suma entera evita el redondeo de microsegundos de un timestamp float.
        date = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=int(match[1]), microseconds=int(match[2]))
        boot['lastBootUtc'] = iso_utc(date)
        boot['bootId'] = host + '/' + boot['lastBootUtc']
    except (OSError, subprocess.SubprocessError, ValueError, OverflowError) as exc:
        boot['error'] = fallo('boot', exc)

    metrics = {}
    for name, key, unit, parser in [
        ('memLevelPct', 'kern.memorystatus_level', 'pct', lambda t: numero(t, integer=True, maximum=100)),
        ('memPressure', 'kern.memorystatus_vm_pressure_level', 'level', lambda t: numero(t, integer=True)),
        ('swapUsedBytes', 'vm.swapusage', 'bytes', swap_bytes),
        ('load1', 'vm.loadavg', 'load', load_one),
        ('ncpu', 'hw.ncpu', 'count', lambda t: numero(t, integer=True, minimum=1)),
    ]:
        metric = dict(value=None, unit=unit, status='unknown', error=None, source='sysctl ' + key)
        try:
            value = parser(comando(['/usr/sbin/sysctl', '-n', key]))
            status = 'ok'
            if name == 'memLevelPct':
                status = bajo(value, config[name], inclusive=True)
            elif name == 'memPressure':
                if value not in (1, 2, 4):
                    raise ValueError('presión desconocida')
                status = {1: 'ok', config[name]['warn']: 'warn', config[name]['crit']: 'crit'}[value]
            metric.update(value=value, status=status)
        except (OSError, subprocess.SubprocessError, ValueError, OverflowError) as exc:
            metric['error'] = fallo('metrics.' + name, exc)
        metrics[name] = metric

    volume = dict(status='unknown', error=None, totalBytes=None, freeBytes=None)
    try:
        stat = statvfs(VOLUME)
        total, free = stat.f_blocks * stat.f_frsize, stat.f_bavail * stat.f_frsize
        if total <= 0 or not 0 <= free <= total:
            raise ValueError('volumen')
        volume.update(totalBytes=total, freeBytes=free, status=bajo(free, config['freeBytes.' + VOLUME]))
    except (OSError, ValueError, OverflowError) as exc:
        volume['error'] = fallo('volumes.' + VOLUME, exc)
    levels = [metric['status'] for metric in metrics.values()] + [volume['status'], 'unknown' if errors else 'ok']
    status = max(levels, key=['ok', 'unknown', 'warn', 'crit'].index)
    return dict(schema='mini-vigilante/muestra@1', host=host, ts=iso_utc(reloj()), boot=boot,
                status=status, metrics=metrics, volumes={VOLUME: volume}, errors=errors,
                persist=dict(error=None, ultimo='written'))


def escribir_atomico(path, sample):
    temporal = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=str(path.parent), prefix='.ultimo-', suffix='.tmp', delete=False) as stream:
            temporal = stream.name
            json.dump(sample, stream, ensure_ascii=False, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporal, path)
    finally:
        if temporal is not None and os.path.exists(temporal):
            os.unlink(temporal)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--salida', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--umbrales', type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args(argv)
    try:
        config = leer_umbrales(args.umbrales)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        args.salida.parent.mkdir(parents=True, exist_ok=True)
        with args.salida.with_name('ultimo.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print('busy', file=sys.stderr)
                return 0
            escribir_atomico(args.salida, capturar(config))
    except (OSError, ValueError):
        print('mini-vigilante: no se pudo persistir ultimo.json', file=sys.stderr)
        return 3
    return 0


if __name__ == '__main__':
    sys.exit(main())
