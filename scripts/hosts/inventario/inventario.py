#!/usr/bin/env python3
"""Inventario de remote-run de sólo lectura, compatible con Python 3.9."""
import argparse
import calendar
from datetime import datetime
from collections import Counter
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import time

DIA = 86400
REGLAS = {'copiaDias': 7, 'corridaDias': 30, 'ventanaInicioS': 120}


def consultar_inicio(pid):
    """Devuelve epoch, None si no existe; los fallos se propagan como duda."""
    entorno = dict(os.environ, LC_ALL='C', TZ='UTC0')
    resultado = subprocess.run(['ps', '-o', 'lstart=', '-p', str(pid)],
                               capture_output=True, text=True, timeout=5, env=entorno)
    if resultado.returncode == 1 and not resultado.stdout.strip() and not resultado.stderr.strip():
        return None
    if resultado.returncode != 0:
        raise OSError('ps falló: ' + resultado.stderr.strip())
    meses = {nombre: i for i, nombre in enumerate(
        ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'), 1)}
    partes = resultado.stdout.split()
    if len(partes) != 5 or partes[1] not in meses:
        raise ValueError('lstart inválido')
    hora = [int(n) for n in partes[3].split(':')]
    if len(hora) != 3:
        raise ValueError('hora de lstart inválida')
    fecha = datetime(int(partes[4]), meses[partes[1]], int(partes[2]), *hora)
    return calendar.timegm(fecha.timetuple())


def listar(ruta):
    if not stat.S_ISDIR(ruta.lstat().st_mode):
        raise ValueError('no es directorio regular o es symlink: ' + str(ruta))
    with os.scandir(ruta) as entradas:
        return sorted((Path(e.path) for e in entradas), key=lambda p: p.name)


def leer(ruta):
    try:
        fd = os.open(str(ruta), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if ruta.is_symlink():
            raise ValueError('symlink colgante: ' + str(ruta))
        return None
    with os.fdopen(fd, 'r', encoding='utf-8') as archivo:
        if not stat.S_ISREG(os.fstat(archivo.fileno()).st_mode):
            raise ValueError('no es archivo regular: ' + str(ruta))
        return archivo.read()


def numero(texto):
    valor = float(texto.strip())
    if not math.isfinite(valor) or valor <= 0:
        raise ValueError('epoch inválido')
    return valor


def espejo(texto):
    if texto is None:
        return None
    campos = {}
    # cmd puede ser multilínea: remote-run lo escribe literalmente al final.
    for linea in texto.splitlines():
        if 'cmd' in campos:
            break
        clave, separador, valor = linea.partition('=')
        if not separador or clave in campos or clave not in ('src', 'host', 'mirror', 'sub', 'cmd'):
            raise ValueError('meta mal formado')
        campos[clave] = valor
    mirror = campos.get('mirror', '')
    if not re.fullmatch(r'remote-run/work/[^/]+', mirror) or mirror.rsplit('/', 1)[-1] in ('.', '..'):
        raise ValueError('meta sin mirror válido')
    return mirror.rsplit('/', 1)[-1]


def elemento(ruta, clase, motivo, ultimo=None, asociados=None):
    return {'ruta': str(ruta), 'clase': clase, 'borrable': False, 'motivo': motivo,
            'ultimoUso': ultimo, 'corridasAsociadas': asociados or [], 'tamanoBytes': None}


def preparacion(entradas):
    nombres = {p.name for p in entradas}
    directorios = {'git-input', 'git-preparado', 'manifest'}
    archivos = {'meta', 'cmd.sh', 'manifest-helper.sh', 'script.sh', 'log'}
    if not nombres <= directorios | archivos:
        return False
    for p in entradas:
        if p.name in directorios:
            listar(p)
        elif leer(p) is None:
            raise ValueError('artefacto desaparecido: ' + p.name)
    if nombres & {'log', 'manifest-helper.sh', 'script.sh'}:
        if 'manifest' not in nombres:
            return False
        # files.jsonl identifica el manifiesto del productor anterior al despacho.
        if leer(entradas[0].parent / 'manifest' / 'files.jsonl') is None:
            return False
    return True


def corrida(ruta, ahora):
    item = elemento(ruta, 'dudosa', 'sin evidencia suficiente')
    mirror = None
    inicio_registrado = None
    try:
        entradas = listar(ruta)
        item['ultimoUso'] = ruta.lstat().st_mtime
        mirror = espejo(leer(ruta / 'meta'))
        for p in entradas:
            if p.is_symlink():
                raise ValueError('symlink en registro: ' + p.name)
        started = leer(ruta / 'started')
        if started is not None:
            inicio_registrado = numero(started)
            item['ultimoUso'] = inicio_registrado
        pid = leer(ruta / 'pid')
        fin = leer(ruta / 'exit')
        if fin is not None:
            item.update(clase='terminal', motivo='existe exit')
        elif pid is None and started is None:
            if preparacion(entradas):
                item.update(clase='preparacion-abortada', motivo='artefactos de preparación; runner no transferido; sin identidad de ejecución')
            else:
                item['motivo'] = 'ejecución no descartable: sin pid ni started y preparación no reconocida'
        elif pid is None or started is None:
            item['motivo'] = 'falta pid o started para validar la identidad'
        else:
            if not re.fullmatch(r'[1-9][0-9]*', pid.strip()):
                raise ValueError('pid inválido')
            inicio = consultar_inicio(int(pid.strip()))
            if inicio is None:
                item.update(clase='terminal', motivo='el pid ya no existe')
            elif not math.isfinite(inicio):
                raise ValueError('inicio de proceso no finito')
            elif abs(inicio - item['ultimoUso']) > 120:
                item.update(clase='terminal', motivo='pid reutilizado: inicio fuera de ±120 s')
            else:
                item.update(clase='viva', motivo='pid con identidad validada dentro de ±120 s')
        if item['clase'] in ('terminal', 'preparacion-abortada'):
            item['borrable'] = ahora - item['ultimoUso'] > 30 * DIA
            item['motivo'] += '; más de 30 días' if item['borrable'] else '; no supera 30 días'
    except (OSError, ValueError, UnicodeError, OverflowError, subprocess.SubprocessError) as exc:
        item.update(clase='dudosa', borrable=False, motivo=str(exc))
    return item, mirror, inicio_registrado


def tamano(ruta):
    """Bytes asignados (st_blocks × 512), sin seguir enlaces ni duplicar hardlinks."""
    vistos = set()
    pendientes = [ruta]
    total = 0
    while pendientes:
        p = pendientes.pop()
        info = p.lstat()
        identidad = (info.st_dev, info.st_ino)
        if identidad in vistos:
            continue
        vistos.add(identidad)
        total += info.st_blocks * 512
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(p) as entradas:
                pendientes.extend(Path(e.path) for e in entradas)
    return total


def inventariar(raiz, ahora=None, tamanos=False):
    raiz = Path(os.path.abspath(os.path.expanduser(str(raiz))))
    ahora = time.time() if ahora is None else ahora
    corridas = []
    por_id = {}
    por_copia = {}
    inicios = {}
    sin_dueno = []
    try:
        # Evita atravesar una raíz que ya sea un enlace antes de listar sus hijos.
        listar(raiz)
        rutas = listar(raiz / 'runs')
    except (OSError, ValueError) as exc:
        rutas = []
        corridas.append(elemento(raiz / 'runs', 'dudosa', str(exc)))
        sin_dueno.append('no se pudieron listar todas las corridas')
    for ruta in rutas:
        item, mirror, inicio_registrado = corrida(ruta, ahora)
        inicios[ruta.name] = inicio_registrado
        corridas.append(item)
        por_id[ruta.name] = item
        if mirror is not None:
            por_copia.setdefault(mirror, []).append(item)
        elif item['clase'] in ('dudosa', 'viva'):
            sin_dueno.append(ruta.name)
    proteccion_copias = {'bloqueoGlobal': list(sin_dueno),
                         'porCopia': {nombre: [x for x in items if x['clase'] in ('viva', 'dudosa')]
                                      for nombre, items in por_copia.items()}}
    copias = []
    try:
        listar(raiz)
        rutas = listar(raiz / 'work')
    except (OSError, ValueError) as exc:
        rutas = []
        copias.append(elemento(raiz / 'work', 'protegida-dudosa', str(exc)))
    for ruta in rutas:
        asociadas = por_copia.get(ruta.name, [])
        item = elemento(ruta, 'protegida-dudosa', 'sin evidencia suficiente',
                        asociados=[Path(x['ruta']).name for x in asociadas])
        copias.append(item)
        try:
            listar(ruta)
            usos = [ruta.lstat().st_mtime]
            usos.extend(inicios[Path(x['ruta']).name] for x in asociadas
                        if inicios[Path(x['ruta']).name] is not None)
            lock = leer(ruta / '.remote-run.lock')
            bloqueada = None
            if lock is not None:
                usos.append((ruta / '.remote-run.lock').lstat().st_mtime)
                bloqueada = por_id.get(lock.strip())
            item['ultimoUso'] = max(usos)
            if any(x['clase'] == 'viva' for x in asociadas) or (bloqueada and bloqueada['clase'] == 'viva'):
                item.update(clase='en-uso', motivo='corrida asociada o lock con identidad viva')
            elif sin_dueno or any(x['clase'] == 'dudosa' for x in asociadas):
                item['motivo'] = ('ejecución no descartable; corridas de dueño desconocido: ' + ', '.join(sin_dueno)
                                  if sin_dueno else 'corrida asociada dudosa')
            elif lock is not None and (bloqueada is None or bloqueada['clase'] != 'terminal'):
                item['motivo'] = ('lock de preparación reconocida; sin evidencia terminal: '
                                  if bloqueada and bloqueada['clase'] == 'preparacion-abortada'
                                  else 'lock sin corrida terminal verificable: ') + lock.strip()
            else:
                clase = 'lock-huerfano' if lock is not None else 'inactiva'
                motivo = 'lock huérfano con corrida terminal' if lock is not None else 'sin lock ni corridas vivas o dudosas'
                vieja = ahora - item['ultimoUso'] >= 7 * DIA
                item.update(clase=clase if vieja else 'reciente', borrable=vieja,
                            motivo=motivo + ('; al menos 7 días sin uso' if vieja else '; menos de 7 días sin uso'))
        except (OSError, ValueError, UnicodeError) as exc:
            item.update(clase='protegida-dudosa', borrable=False, motivo=str(exc))
    if tamanos:
        for item in copias + corridas:
            if item['borrable']:
                try:
                    item['tamanoBytes'] = tamano(Path(item['ruta']))
                except (OSError, ValueError) as exc:
                    item.update(clase='protegida-dudosa' if item in copias else 'dudosa',
                                borrable=False, motivo='tamaño no verificable: ' + str(exc))
    totales = {}
    for nombre, elementos in [('copias', copias), ('corridas', corridas)]:
        totales[nombre] = {'cantidad': len(elementos), 'porClase': dict(Counter(x['clase'] for x in elementos)),
                           'borrables': sum(x['borrable'] for x in elementos),
                           'tamanoBorrableBytes': sum(x['tamanoBytes'] or 0 for x in elementos if x['borrable']) if tamanos else None}
    return {'schema': 'remote-run-inventario@1', 'host': socket.gethostname(), 'ts': ahora,
            'raiz': str(raiz), 'reglas': dict(REGLAS), 'copias': copias,
            'corridas': corridas, 'totales': totales, 'proteccionCopias': proteccion_copias}


def resumen(datos):
    lineas = ['Inventario de {} en {}'.format(datos['host'], datos['raiz'])]
    for tipo in ('copias', 'corridas'):
        total = datos['totales'][tipo]
        lineas.append('{}: {}, borrables: {}'.format(tipo, total['cantidad'], total['borrables']))
        lineas.extend('  {}: {}'.format(clase, cantidad) for clase, cantidad in sorted(total['porClase'].items()))
        if total['tamanoBorrableBytes'] is not None:
            lineas.append('  espacio borrable: {} bytes'.format(total['tamanoBorrableBytes']))
    return '\n'.join(lineas)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--raiz', default='~/remote-run')
    parser.add_argument('--resumen', action='store_true')
    parser.add_argument('--tamanos', action='store_true')
    args = parser.parse_args(argv)
    datos = inventariar(args.raiz, tamanos=args.tamanos)
    print(resumen(datos) if args.resumen else json.dumps(datos, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
