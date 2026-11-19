#!/usr/bin/env python3
"""Avisa en macOS de grupos cerrados con fila a partir del rack de pruebas."""

from collections import Counter
import fcntl
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import NamedTuple, Optional
from urllib.parse import urlsplit
from urllib.request import urlopen


HOSTS = ('desk', 'mini')
MOTIVOS = ('manual', 'ambiguous')
ESTADOS_TAREA = ('Queued', 'Stashed', 'Locked', 'Running', 'Paused', 'Done')
ANTIGUEDAD_MAXIMA = 120  # El rack sondea cada 15 s y admite hasta 25 s por host.
ESQUEMA_ESTADO = 'pao-aviso-fila@1'
UMBRAL_MINUTOS = 30


class Aviso(NamedTuple):
    host: str
    grupo: Optional[str]
    mensaje: str


def grupos_con_fila(control, tareas):
    if (not isinstance(control, dict) or control.get('schema') != 'rack-host-result@1'
            or control.get('status') != 'ok'):
        raise ValueError('Control no disponible')
    razones = control['pause_reasons']
    if not isinstance(razones, dict) or not isinstance(tareas, list):
        raise ValueError('Pausas o tareas inválidas')
    for grupo, motivos in razones.items():
        if (not isinstance(grupo, str) or not grupo.isprintable() or not isinstance(motivos, list)
                or any(not isinstance(motivo, str) for motivo in motivos)):
            raise ValueError('Pausa inválida')
    conteos = Counter()
    for tarea in tareas:
        grupo, estado = tarea['group'], tarea['state']
        if not isinstance(grupo, str) or estado not in ESTADOS_TAREA:
            raise ValueError('Tarea inválida')
        if estado == 'Queued':
            conteos[grupo] += 1
    return {grupo: (tuple(motivos), conteos[grupo]) for grupo, motivos in razones.items()
            if conteos[grupo] and any(motivo in motivos for motivo in MOTIVOS)}


def observar(documento, ahora):
    """Proyecta cada host a sus grupos de interés. None indica una lectura fallida."""
    lecturas = dict.fromkeys(HOSTS)
    if not isinstance(documento, dict) or documento.get('schema') != 'rack@1':
        return lecturas
    hosts = documento.get('hosts')
    if not isinstance(hosts, list):
        return lecturas
    for host in HOSTS:
        filas = [fila for fila in hosts if isinstance(fila, dict) and fila.get('id') == host]
        if len(filas) != 1 or filas[0].get('fresh') is not True:
            continue
        try:
            foto = filas[0]['snapshot']
            tomada = foto['taken']
            if (foto['schema'] != 'rack-host@1' or foto['host'] != host
                    or type(tomada) not in (int, float) or not math.isfinite(tomada)
                    or abs(ahora - tomada) > ANTIGUEDAD_MAXIMA
                    or any(error['stage'] in ('control', 'pueue') for error in foto['errors'])):
                continue
            lecturas[host] = grupos_con_fila(foto['control'], foto['tasks'])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return lecturas


def episodio_nuevo(ahora):
    return {'inicio': ahora, 'avisado': False}


def vence(episodio, ahora, umbral):
    return ahora - episodio['inicio'] > umbral and not episodio['avisado']


def decidir(estado, lecturas, ahora, umbral):
    """Devuelve los episodios vigentes y los avisos pendientes de entrega."""
    siguiente, avisos = {}, []
    for host in HOSTS:
        anterior = estado.get(host, {})
        grupos = anterior.get('grupos', {})
        sin_lectura = anterior.get('sin_lectura')
        lectura = lecturas[host]
        if lectura is None:
            sin_lectura = sin_lectura or episodio_nuevo(ahora)
            siguiente[host] = dict(grupos=dict(grupos), sin_lectura=sin_lectura)
            if vence(sin_lectura, ahora, umbral):
                minutos = (ahora - sin_lectura['inicio']) // 60
                avisos.append(Aviso(host, None, f'No pude leer {host} desde hace {minutos} min.'))
            continue
        vigentes = {}
        for grupo, (motivos, cantidad) in lectura.items():
            episodio = grupos.get(grupo) or episodio_nuevo(ahora)
            vigentes[grupo] = episodio
            if vence(episodio, ahora, umbral):
                minutos = (ahora - episodio['inicio']) // 60
                razon = ', '.join(motivos)
                mensaje = f'{host}/{grupo} · {razon} · {minutos} min · {cantidad} en fila'
                avisos.append(Aviso(host, grupo, mensaje))
        siguiente[host] = dict(grupos=vigentes, sin_lectura=None)
    return siguiente, avisos


def episodio_valido(episodio):
    return (isinstance(episodio, dict) and type(episodio.get('inicio')) is int
            and episodio['inicio'] >= 0 and type(episodio.get('avisado')) is bool)


def cargar_estado(ruta):
    try:
        documento = json.loads(ruta.read_text(encoding='utf-8'))
        estado = documento['hosts']
        if documento['schema'] != ESQUEMA_ESTADO or not isinstance(estado, dict) or set(estado) != set(HOSTS):
            return {}
        for host in estado.values():
            sin_lectura, grupos = host['sin_lectura'], host['grupos']
            if (sin_lectura is not None and not episodio_valido(sin_lectura)
                    or not isinstance(grupos, dict) or any(not episodio_valido(e) for e in grupos.values())):
                return {}
        return estado
    except (KeyError, TypeError, ValueError, FileNotFoundError):
        return {}


def guardar_estado(ruta, estado):
    temporal = ruta.with_suffix('.nuevo')
    fd = os.open(temporal, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as archivo:
        json.dump(dict(schema=ESQUEMA_ESTADO, hosts=estado), archivo)
        archivo.flush()
        os.fsync(archivo.fileno())
    temporal.replace(ruta)


def leer_rack(url):
    try:
        with urlopen(url, timeout=10) as respuesta:
            return json.load(respuesta)
    except (HTTPException, OSError, ValueError):
        return None


def notificar(aviso):
    script = 'on run argv\ndisplay notification (item 1 of argv) with title "Pueue"\nend run'
    subprocess.run(['/usr/bin/osascript', '-e', script, aviso.mensaje], check=True, timeout=10,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def pasada(directorio, url, umbral):
    directorio.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directorio / 'pasada.lock').open('a') as candado:
        try:
            fcntl.flock(candado, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        ruta = directorio / 'estado.json'
        estado = cargar_estado(ruta)
        documento = leer_rack(url)
        ahora = int(time.time())
        siguiente, avisos = decidir(estado, observar(documento, ahora), ahora, umbral)
        if siguiente != estado:
            guardar_estado(ruta, siguiente)
        resultado = 0
        for aviso in avisos:
            try:
                notificar(aviso)
            except (OSError, subprocess.SubprocessError):
                print(f'Falló la notificación de {aviso.host}. Se reintentará.', file=sys.stderr)
                resultado = 1
                continue
            host = siguiente[aviso.host]
            episodio = host['sin_lectura'] if aviso.grupo is None else host['grupos'][aviso.grupo]
            episodio['avisado'] = True
            guardar_estado(ruta, siguiente)
        return resultado


def main():
    home = Path.home()
    try:
        config = json.loads((home / '.config/pao/aviso-fila-cerrada.json').read_text(encoding='utf-8'))
        url = config['rack_url']
        minutos = config.get('minutos', UMBRAL_MINUTOS)
        parsed = urlsplit(url)
        if (not isinstance(url, str) or parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.path != '/api/rack.json' or parsed.username is not None
                or type(minutos) is not int or minutos <= 0):
            raise ValueError('Configuración inválida')
    except (OSError, KeyError, TypeError, ValueError, AttributeError):
        print('Revisa ~/.config/pao/aviso-fila-cerrada.json.', file=sys.stderr)
        return 2
    try:
        return pasada(home / '.local/state/pao/aviso-fila-cerrada', url, minutos * 60)
    except OSError:
        print('No se pudo leer o guardar el estado local del vigía.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
