#!/usr/bin/python3
"""Reinicia fseventsd cuando su footprint o CPU sostenida delatan la fuga."""
import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time


@dataclass(frozen=True)
class Muestra:
    pid: int
    footprint: int
    cpu: float


@dataclass(frozen=True)
class Estado:
    pid: int = 0
    consecutivas: int = 0
    ultimo_reinicio: float = 0


@dataclass(frozen=True)
class Umbrales:
    footprint: int = 8 * 1024 ** 3
    cpu: float = 90
    consecutivas: int = 18
    enfriamiento: float = 3600


def memoria_bytes(texto):
    match = re.fullmatch(r'(\d+(?:\.\d+)?)([KMGT]?)[+-]?', texto.strip(), re.I)
    if not match:
        raise ValueError('memoria inválida: ' + texto)
    numero, unidad = match.groups()
    return int(float(numero) * 1024 ** ('KMGT'.index(unidad.upper()) + 1 if unidad else 0))


def parsear_top(texto, pid):
    muestras = []
    for linea in texto.splitlines():
        columnas = linea.split()
        if len(columnas) == 3 and columnas[0] == str(pid):
            muestras.append(Muestra(pid, memoria_bytes(columnas[1]), float(columnas[2].rstrip('%'))))
    if len(muestras) != 2:
        raise ValueError('top no devolvió dos muestras para el pid')
    return muestras[1]


def decidir(muestra, estado, umbrales, ahora):
    cuenta = estado.consecutivas if muestra.pid == estado.pid else 0
    cuenta = cuenta + 1 if muestra.cpu >= umbrales.cpu else 0
    nuevo = Estado(muestra.pid, cuenta, estado.ultimo_reinicio)
    motivo = ('footprint' if muestra.footprint >= umbrales.footprint else
              'CPU sostenida' if cuenta >= umbrales.consecutivas else '')
    if not motivo:
        return 'observar', nuevo, 'bajo umbrales'
    if estado.ultimo_reinicio and ahora - estado.ultimo_reinicio < umbrales.enfriamiento:
        return 'observar', nuevo, 'enfriamiento'
    return 'reiniciar', nuevo, motivo


def guardar_estado(ruta, estado):
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=str(ruta.parent), delete=False) as archivo:
        temporal = archivo.name
        os.fchmod(archivo.fileno(), 0o644)
        json.dump(asdict(estado), archivo)
        archivo.flush()
        os.fsync(archivo.fileno())
    os.replace(temporal, ruta)
    descriptor = os.open(str(ruta.parent), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def leer_pid():
    salida = subprocess.check_output(['pgrep', '-x', 'fseventsd'], text=True).split()
    if len(salida) != 1:
        raise ValueError('se esperaba un único fseventsd')
    return int(salida[0])


def pasar(ruta, umbrales, dry_run, comando, obtener_pid, reloj, dormir, imprimir):
    fecha = datetime.fromtimestamp(reloj(), timezone.utc).isoformat()
    try:
        estado = Estado(**json.loads(ruta.read_text())) if ruta.exists() else Estado()
        pid = obtener_pid()
        muestra = parsear_top(comando(['top', '-l', '2', '-s', '3', '-pid', str(pid),
                                      '-stats', 'pid,mem,cpu']), pid)
        accion, nuevo, motivo = decidir(muestra, estado, umbrales, reloj())
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        imprimir(f'{fecha} acción=error motivo={error}')
        return 1
    contador = nuevo.consecutivas
    confirmado = True
    try:
        if not dry_run:
            if accion == 'reiniciar':
                comando(['killall', 'fseventsd'])
                # Persistir el enfriamiento aunque launchd tarde en reemplazar el pid.
                nuevo = Estado(pid, nuevo.consecutivas, reloj())
                guardar_estado(ruta, nuevo)
                for _ in range(10):
                    dormir(1)
                    try:
                        reemplazo = obtener_pid()
                    except (ValueError, subprocess.SubprocessError):
                        continue
                    if reemplazo != pid:
                        nuevo = Estado(reemplazo, 0, nuevo.ultimo_reinicio)
                        motivo += f' pid_nuevo={reemplazo}'
                        break
                else:
                    confirmado = False
                    motivo += ' error=no se confirmó pid nuevo en 10 s'
            guardar_estado(ruta, nuevo)
        imprimir(f'{fecha} pid={pid} footprint_MiB={muestra.footprint / 1024 ** 2:.2f} '
                 f'CPU={muestra.cpu:.2f} contador={contador} '
                 f'acción={accion} dry_run={dry_run} motivo={motivo}')
        return 0 if confirmado else 1
    except (OSError, subprocess.SubprocessError) as error:
        imprimir(f'{fecha} pid={pid} acción=error motivo={error}')
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--footprint-gib', type=float, default=8)
    parser.add_argument('--cpu', type=float, default=90)
    parser.add_argument('--consecutivas', type=int, default=18)
    parser.add_argument('--enfriamiento-min', type=float, default=60)
    args = parser.parse_args()
    if min(args.footprint_gib, args.cpu, args.consecutivas, args.enfriamiento_min) <= 0:
        parser.error('los umbrales deben ser positivos')
    ruta = Path('/Library/Application Support/pao/fseventsd-guardia/estado.json')
    umbrales = Umbrales(int(args.footprint_gib * 1024 ** 3), args.cpu,
                       args.consecutivas, args.enfriamiento_min * 60)
    return pasar(ruta, umbrales, args.dry_run,
                 lambda cmd: subprocess.check_output(cmd, text=True), leer_pid,
                 time.time, time.sleep, print)


if __name__ == '__main__':
    raise SystemExit(main())
