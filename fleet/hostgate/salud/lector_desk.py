"""Admisión del desk para `auto` según contrato-lectura-desk-u3.md.

Valida sólo los campos del contrato e ignora cualquier otra clave, incluida
`observations`: una ampliación aditiva del productor no cambia la admisión.
"""
import math
import re
from datetime import datetime, timezone

GIB = 1024 ** 3
_TS = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?Z$')


def _ts(texto):
    m = _TS.match(texto) if isinstance(texto, str) else None
    if not m:
        return None
    try:
        base = datetime.strptime(m.group(1), '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return base.timestamp() + float('0.' + (m.group(2) or '0'))


def _numero(x, minimo=0.0, maximo=math.inf):
    return type(x) in (int, float) and math.isfinite(x) and minimo <= x <= maximo


def _dict(x):
    return x if isinstance(x, dict) else {}


def _metrica(muestra, nombre, motivos, maximo=math.inf):
    m = _dict(muestra.get('metrics')).get(nombre)
    if (not isinstance(m, dict) or m.get('error') is not None or m.get('status') != 'ok'
            or not _numero(m.get('value'), maximo=maximo)):
        motivos.append(f'{nombre} incompleta')
        return None
    return m['value']


def evaluar(muestra, ahora, boot_actual):
    """Devuelve la lista de motivos de no sano; vacía si el desk está sano."""
    if not isinstance(muestra, dict):
        return ['snapshot no es objeto']
    motivos = [f'{clave} no es objeto' for clave in ('metrics', 'volumes', 'boot', 'guestRuntime', 'persist')
               if not isinstance(muestra.get(clave), dict)]
    if muestra.get('schema') != 'desktop-vigilante/muestra@1':
        motivos.append('schema')
    if muestra.get('host') != 'FELIX-DESKTOP':
        motivos.append('host')
    ts = _ts(muestra.get('ts'))
    if ts is None or not -60 <= ahora - ts <= 180:
        motivos.append('vigencia')
    boot = muestra.get('boot')
    if (not isinstance(boot, dict) or boot.get('error') is not None or not boot.get('bootId')
            or boot_actual is None or boot['bootId'] != boot_actual):
        motivos.append('boot')
    if muestra.get('status') != 'ok':
        motivos.append('status')

    cpu = _metrica(muestra, 'cpuRootPct', motivos, maximo=100)
    if cpu is not None and cpu >= 90:
        motivos.append('cpuRootPct')
    ratio = _metrica(muestra, 'commitRatio', motivos, maximo=1)
    usado = _metrica(muestra, 'commitBytes', motivos)
    limite = _metrica(muestra, 'commitLimitBytes', motivos)
    if None not in (ratio, usado, limite):
        if limite <= 0 or usado > limite or abs(usado / limite - ratio) > 1e-6:
            motivos.append('commit incoherente')
        elif ratio >= 0.85:
            motivos.append('commitRatio')

    lat = _dict(muestra.get('metrics')).get('eLatencyMs')
    idle = (isinstance(lat, dict) and lat.get('status') == 'idle' and lat.get('value') is None
            and lat.get('error') is None and type(lat.get('transfers')) is int and lat['transfers'] == 0)
    if not idle:
        valor = _metrica(muestra, 'eLatencyMs', motivos)
        if valor is not None and valor >= 50:
            motivos.append('eLatencyMs')

    volumenes = _dict(muestra.get('volumes'))
    for letra in ('C:', 'E:'):
        v = volumenes.get(letra)
        if (not isinstance(v, dict) or v.get('status') != 'ok' or v.get('error') is not None
                or not _numero(v.get('totalBytes'), minimo=1) or not _numero(v.get('freeBytes'), maximo=v['totalBytes'])):
            motivos.append(f'volumen {letra} incompleto')
            continue
        if letra == 'C:' and v['freeBytes'] < 20 * GIB:
            motivos.append('C: libre')
        if letra == 'E:' and v['freeBytes'] / v['totalBytes'] < 0.10:
            motivos.append('E: libre')

    guest = muestra.get('guestRuntime')
    if not isinstance(guest, dict) or guest.get('status') != 'ok':
        motivos.append('guestRuntime')
    persist = muestra.get('persist')
    if (not isinstance(persist, dict) or persist.get('error') is not None
            or persist.get('jsonl') not in ('written', 'capped') or persist.get('alerts') not in ('written', 'capped')):
        motivos.append('persist')
    if muestra.get('errors') != []:
        motivos.append('errors')
    return motivos
