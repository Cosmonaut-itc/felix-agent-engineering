"""Valida mini-vigilante/muestra@1; boot_actual viene del entorno independiente."""
import sys
from .lector_desk import GIB, _dict, _ts


def _numero(value, minimo=0, maximo=sys.float_info.max):
    return (type(value) in (int, float) and 0 <= value <= sys.float_info.max
            and minimo <= value <= maximo)


def evaluar(muestra, ahora, boot_actual):
    if not isinstance(muestra, dict): return ['snapshot no es objeto']
    reasons = []
    if muestra.get('schema') != 'mini-vigilante/muestra@1': reasons.append('schema')
    host = muestra.get('host')
    boot = _dict(muestra.get('boot'))
    ts = _ts(muestra.get('ts'))
    if ts is None or not _numero(ahora) or not -60 <= ahora - ts <= 180: reasons.append('vigencia')
    if (not isinstance(host, str) or not host or not isinstance(boot_actual, str)
            or not boot_actual.startswith(host + '/')): reasons.append('host')
    last = boot.get('lastBootUtc')
    if (not isinstance(last, str) or _ts(last) is None or len(last) != 27
            or boot.get('error') is not None or not isinstance(host, str)
            or boot.get('bootId') != host + '/' + last or boot.get('bootId') != boot_actual): reasons.append('boot')
    if muestra.get('status') != 'ok': reasons.append('status')
    metrics = _dict(muestra.get('metrics'))
    units = dict(memLevelPct='pct', memPressure='level', swapUsedBytes='bytes', load1='load', ncpu='count')
    for name, unit in units.items():
        metric = _dict(metrics.get(name)); value = metric.get('value')
        maximum = 100 if name == 'memLevelPct' else float('inf')
        if (metric.get('status') != 'ok' or metric.get('error') is not None
                or metric.get('unit') != unit or not _numero(value, maximo=maximum)
                or not isinstance(metric.get('source'), str) or not metric['source']
                or (name == 'ncpu' and (type(value) is not int or value <= 0))):
            reasons.append(name + ' incompleta'); continue
        if name == 'memLevelPct' and value <= 30: reasons.append('memLevelPct')
        if name == 'memPressure' and (type(value) is not int or value != 1):
            reasons.append('memPressure' if value in (2, 4) and type(value) is int else 'memPressure desconocida')
    volume = _dict(_dict(muestra.get('volumes')).get('/System/Volumes/Data'))
    if (volume.get('status') != 'ok' or volume.get('error') is not None
            or not _numero(volume.get('totalBytes'), minimo=1)
            or not _numero(volume.get('freeBytes'), maximo=volume.get('totalBytes', 0))):
        reasons.append('volumen incompleto')
    elif volume['freeBytes'] < 50 * GIB: reasons.append('data libre')
    persist = _dict(muestra.get('persist'))
    if persist.get('ultimo') != 'written' or persist.get('error') is not None: reasons.append('persist')
    if muestra.get('errors') != []: reasons.append('errors')
    return reasons
