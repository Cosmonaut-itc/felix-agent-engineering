#!/usr/bin/python3
"""Retira esperas de remote-run cuyo directorio ya desapareció."""
import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import os
import re
import signal
import subprocess
import time


@dataclass(frozen=True)
class Proceso:
    pid: int
    ppid: int
    edad_s: int
    args: str


ESPERA = re.compile(r'\bcd remote-run/runs/(\d{8}-\d{6}-[0-9a-f]+) \|\| exit 3(?:[;\s]|$)')
SHELL = re.compile(r'^(?:\S*/)?(?:sh|bash|zsh)\s')
ANCESTRO = re.compile(r'(?:^|[\s/])remote-run\s+wait(?:\s|$)')


def edad_segundos(texto):
    dias, separador, resto = texto.partition('-')
    partes = [int(p) for p in (resto if separador else dias).split(':')]
    segundos = 0
    for parte in partes:
        segundos = segundos * 60 + parte
    return segundos + (int(dias) * 86400 if separador else 0)


def parsear_instantanea(texto):
    procesos = []
    for linea in texto.splitlines():
        fila = re.match(r'^\s*(\d+)\s+(\d+)\s+([\d:-]+)\s+(.*)$', linea)
        if fila:
            pid, ppid, edad, args = fila.groups()
            procesos.append(Proceso(int(pid), int(ppid), edad_segundos(edad), args))
        elif procesos:
            previo = procesos[-1]
            procesos[-1] = Proceso(previo.pid, previo.ppid, previo.edad_s,
                                   previo.args + '\n' + linea)
    return procesos


def elegir_victimas(procesos, directorios_existentes):
    indice = {p.pid: p for p in procesos}
    grupos = []
    for proceso in procesos:
        coincidencia = ESPERA.search(proceso.args) if SHELL.match(proceso.args) else None
        if not coincidencia or proceso.edad_s <= 600:
            continue
        corrida = coincidencia.group(1)
        if corrida in directorios_existentes:
            continue
        cadena = [proceso]
        padre = proceso.ppid
        vistos = {proceso.pid}
        while padre > 1 and padre in indice and padre not in vistos:
            ancestro = indice[padre]
            if not ANCESTRO.search(ancestro.args):
                break
            cadena.append(ancestro)
            vistos.add(padre)
            padre = ancestro.ppid
        grupos.append((corrida, cadena))
    return grupos


def ejecutar(procesos, existe, enviar, vivo, dormir, reloj, imprimir, dry_run=False):
    ids = {m.group(1) for p in procesos for m in [ESPERA.search(p.args)] if m}
    grupos = elegir_victimas(procesos, {i for i in ids if existe(i)})
    total = 0
    enviados = set()
    for corrida, cadena in grupos:
        pids = ','.join(str(p.pid) for p in cadena)
        pendientes = []
        for proceso in cadena:
            if proceso.pid in enviados or existe(corrida):
                continue
            total += 1
            enviados.add(proceso.pid)
            resultado = 'simulado' if dry_run else enviar(proceso.pid, signal.SIGTERM)
            imprimir(f'{reloj()} id={corrida} pids={pids} pid={proceso.pid} '
                     f'edad_s={proceso.edad_s} señal=TERM resultado={resultado}')
            if not dry_run and resultado == 'enviada':
                pendientes.append(proceso)
        if pendientes:
            dormir(5)
            for proceso in pendientes:
                if vivo(proceso.pid) and not existe(corrida):
                    resultado = enviar(proceso.pid, signal.SIGKILL)
                    imprimir(f'{reloj()} id={corrida} pids={pids} pid={proceso.pid} '
                             f'edad_s={proceso.edad_s} señal=KILL resultado={resultado}')
    imprimir(f'{reloj()} víctimas={total} dry_run={dry_run}')
    return total


def enviar_senal(pid, senal):
    try:
        os.kill(pid, senal)
        return 'enviada'
    except ProcessLookupError:
        return 'ya terminó'
    except PermissionError:
        return 'permiso denegado'


def esta_vivo(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    texto = subprocess.check_output(['ps', '-Ao', 'pid=,ppid=,etime=,args='], text=True)
    raiz = Path.home() / 'remote-run/runs'
    ejecutar(parsear_instantanea(texto), lambda i: (raiz / i).exists(),
             enviar_senal, esta_vivo, time.sleep,
             lambda: datetime.now(timezone.utc).isoformat(), print, args.dry_run)


if __name__ == '__main__':
    main()
