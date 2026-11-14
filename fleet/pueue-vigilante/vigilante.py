#!/usr/bin/python3 -I
"""Pasada conservadora para tareas de Pueue sin lanzador. Python 3.9+."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import time
from typing import Callable, Optional, Union

TOPE_GRUPO_MIN = {'rapidas': 10, 'suites': 45, 'hasito': 45, 'otros': 45,
                 'jale': 45, 'pesadas': 150}
TOPE_DESCONOCIDO_MIN = 45
SILENCIO_MIN = 15
TOPE_DECLARADO_MAX_MIN = {'rapidas': 10}
TOPE_DECLARADO_MAX_DEFECTO_MIN = 150
GRACIA_S = 20
HUECO_S = 180
BITACORA_MAX_BYTES = 1024 * 1024


@dataclass(frozen=True)
class Host:
    nombre: str
    pueue: Path
    task_logs: Path
    estado: Path
    home: Path


def host(nombre: str, pueue: Path, home: Path) -> Host:
    if nombre not in ('mini', 'desk'):
        raise ValueError('host desconocido')
    logs = ('Library/Application Support/pueue/task_logs' if nombre == 'mini'
            else '.local/share/pueue/task_logs')
    return Host(nombre, pueue, home / logs, home / '.local/state/pao/pueue-vigilante', home)


@dataclass(frozen=True)
class Tarea:
    id: int
    huella: str
    grupo: str
    estado: str
    inicio: Optional[float]
    etiqueta: Optional[str]
    comando: str
    ruta: str


@dataclass(frozen=True)
class Ilegible:
    clave: str
    motivo: str


class Clase(Enum):
    LANZADOR = 'lanzador'
    DUDOSA = 'dudosa'
    MANUAL = 'manual'


@dataclass(frozen=True)
class Clasificacion:
    clase: Clase
    regla: str


@dataclass(frozen=True)
class Limites:
    tope_s: float
    silencio_s: float
    origen: str
    aviso: Optional[str]


class Motivo(Enum):
    TOPE = 'tope'
    SILENCIO = 'silencio'


@dataclass(frozen=True)
class Seguir:
    tarea: Tarea
    transcurrido_s: float
    sin_salida_s: Optional[float]


@dataclass(frozen=True)
class Cortar:
    tarea: Tarea
    motivo: Motivo
    limites: Limites
    transcurrido_s: float
    sin_salida_s: Optional[float]


@dataclass(frozen=True)
class Indeterminada:
    tarea: Tarea
    motivo: str


Decision = Union[Seguir, Cortar, Indeterminada]


class Modo(Enum):
    INFORME = 'informe'
    CORTAR = 'cortar'


class Desenlace(Enum):
    TERMINADA = 'terminada'
    FORZADA = 'forzada'
    PERSISTE = 'persiste'
    CAMBIO = 'cambio'


def epoch(texto: Optional[str]) -> Optional[float]:
    if not isinstance(texto, str):
        return None
    # fromisoformat de 3.9 admite 3 o 6 decimales. Rellenar y recortar.
    texto = re.sub(r'\.(\d+)', lambda m: '.' + (m[1] + '000000')[:6], texto)
    try:
        fecha = datetime.fromisoformat(texto.replace('Z', '+00:00'))
        if fecha.tzinfo is None:
            return None
        return fecha.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def leer_cola(raw: bytes) -> tuple[list[Tarea], list[Ilegible]]:
    cola = json.loads(raw)
    if not isinstance(cola, dict) or not isinstance(cola.get('tasks'), dict):
        raise ValueError('cola sin tasks')
    tareas, ilegibles = [], []
    for clave, t in cola['tasks'].items():
        try:
            if not isinstance(t, dict):
                raise ValueError('tarea no es objeto')
            ident = t['id']
            if type(ident) is not int or ident < 0 or str(ident) != clave:
                raise ValueError('id inválido')
            for campo in ('group', 'command', 'path', 'created_at'):
                if not isinstance(t[campo], str) or not t[campo]:
                    raise ValueError('campo inválido: ' + campo)
            if epoch(t['created_at']) is None:
                raise ValueError('created_at inválido')
            etiqueta = t.get('label')
            if etiqueta is not None and not isinstance(etiqueta, str):
                raise ValueError('label inválido')
            estado, detalle = t['status'], {}
            if isinstance(estado, dict) and len(estado) == 1:
                estado, detalle = next(iter(estado.items()))
                if not isinstance(detalle, dict):
                    raise ValueError('detalle de estado inválido')
            if estado not in ('Running', 'Paused', 'Queued', 'Stashed', 'Locked', 'Done'):
                raise ValueError('estado inválido')
            inicio = detalle.get('start')
            tareas.append(Tarea(ident, f"{ident}|{t['created_at']}|{inicio}", t['group'],
                                estado, epoch(inicio), etiqueta, t['command'], t['path']))
        except (KeyError, TypeError, ValueError):
            ilegibles.append(Ilegible(str(clave), 'forma de tarea inválida'))
    return tareas, ilegibles


class Pueue:
    def __init__(self, binario: Path) -> None:
        self.binario = binario

    def status(self) -> bytes:
        return subprocess.run([str(self.binario), 'status', '--json'], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10).stdout

    def kill(self, tarea: int, senal: Optional[str]) -> bool:
        argv = [str(self.binario), 'kill']
        if senal is not None:
            argv.extend(['-s', senal])
        argv.append(str(tarea))
        return subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=10).returncode == 0


def clasificar(t: Tarea, home: Path) -> Clasificacion:
    if t.etiqueta == 'apotheca-remote-run':
        return Clasificacion(Clase.LANZADOR, 'bodega')
    ruta = Path(str(home) + t.ruta[1:] if t.ruta.startswith('~/') else t.ruta)
    runs = home / 'remote-run/runs'
    if ruta.parent == runs and re.fullmatch(r'[0-9]{8}-[0-9]{6}-[0-9a-f]{6}', ruta.name):
        return Clasificacion(Clase.LANZADOR, 'remote-run')
    jale = re.fullmatch(r'exec bash (/.*/job-[A-Za-z0-9]{8}/task\.sh)', t.comando)
    if jale and Path(jale[1]).parent.parent == home / '.local/state/jale/pueue':
        return Clasificacion(Clase.LANZADOR, 'jale')
    if ruta == runs or runs in ruta.parents:
        return Clasificacion(Clase.DUDOSA, 'sospecha:ruta')
    if ('/remote-run/runs/' in t.comando or 'pueue.py exec' in t.comando
            or re.search(r'exec bash .*/job-[^/\s]+/task\.sh', t.comando)):
        return Clasificacion(Clase.DUDOSA, 'sospecha:comando')
    if 'remote-run' in (t.etiqueta or ''):
        return Clasificacion(Clase.DUDOSA, 'sospecha:etiqueta')
    return Clasificacion(Clase.MANUAL, 'ninguna')


def limites(grupo: str, etiqueta: Optional[str]) -> Limites:
    valores = {'tope': TOPE_GRUPO_MIN.get(grupo, TOPE_DESCONOCIDO_MIN),
               'silencio': SILENCIO_MIN}
    avisos, usados = [], []
    for nombre in ('tope', 'silencio'):
        palabras = [p for p in (etiqueta or '').split() if p.startswith(nombre + '=')
                    or p == nombre]
        if not palabras:
            continue
        valido = re.fullmatch(nombre + r'=([1-9][0-9]{0,2})', palabras[0])
        if len(palabras) != 1 or not valido or int(valido[1]) > 150:
            avisos.append(nombre + ' inválido; se usa grupo')
            continue
        maximo = (TOPE_DECLARADO_MAX_MIN.get(grupo, TOPE_DECLARADO_MAX_DEFECTO_MIN)
                  if nombre == 'tope' else TOPE_DECLARADO_MAX_DEFECTO_MIN)
        valores[nombre] = min(int(valido[1]), maximo)
        usados.append(nombre)
    return Limites(valores['tope'] * 60, valores['silencio'] * 60,
                   'etiqueta' if usados else 'grupo', '; '.join(avisos) or None)


def decidir(t: Tarea, lim: Limites, log_mtime: Optional[float],
            reanudado_en: float, ahora: float) -> Decision:
    if t.inicio is None:
        return Indeterminada(t, 'sin-inicio')
    if t.inicio > ahora + 60:
        return Indeterminada(t, 'inicio-futuro')
    base = max(t.inicio, reanudado_en)
    transcurrido = max(0, ahora - base)
    silencio = None if log_mtime is None else max(0, ahora - max(log_mtime, base))
    if t.estado == 'Running':
        if transcurrido >= lim.tope_s:
            return Cortar(t, Motivo.TOPE, lim, transcurrido, silencio)
        if silencio is not None and silencio >= lim.silencio_s:
            return Cortar(t, Motivo.SILENCIO, lim, transcurrido, silencio)
    return Seguir(t, transcurrido, silencio)


def bitacora(h: Host, evento: str, **campos) -> None:
    h.estado.mkdir(parents=True, exist_ok=True)
    with (h.estado / 'bitacora.lock').open('a') as candado:
        fcntl.flock(candado, fcntl.LOCK_EX)
        ruta = h.estado / 'vigilante.log'
        if ruta.exists() and ruta.stat().st_size >= BITACORA_MAX_BYTES:
            os.replace(ruta, ruta.with_name('vigilante.log.1'))
        fila = dict(utc=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                    evento=evento, host=h.nombre, **campos)
        with ruta.open('a', encoding='utf-8') as salida:
            salida.write(json.dumps(fila, ensure_ascii=False) + '\n')


def fila(c: Cortar) -> dict:
    try:
        tokens = shlex.split(c.tarea.comando)
    except ValueError:
        tokens = []
    programa = next((t for t in tokens if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', t)), None)
    return dict(tarea=c.tarea.id, huella=c.tarea.huella, grupo=c.tarea.grupo,
                etiqueta=c.tarea.etiqueta, programa=programa, ruta=c.tarea.ruta,
                motivo=c.motivo.value,
                limite_min=(c.limites.tope_s if c.motivo is Motivo.TOPE else c.limites.silencio_s) / 60,
                tope_origen=c.limites.origen, aviso=c.limites.aviso,
                transcurrido_min=round(c.transcurrido_s / 60, 2),
                sin_salida_min=None if c.sin_salida_s is None else round(c.sin_salida_s / 60, 2))


def observar(p: Pueue, t: Tarea, home: Path) -> str:
    tareas, _ = leer_cola(p.status())
    fresca = next((x for x in tareas if x.id == t.id), None)
    if (fresca is None or fresca.huella != t.huella
            or clasificar(fresca, home).clase is not Clase.MANUAL):
        return 'cambio'
    return 'done' if fresca.estado == 'Done' else 'running' if fresca.estado == 'Running' else 'cambio'


def esperar_fin(p: Pueue, t: Tarea, home: Path, segundos: float) -> str:
    fin = time.monotonic() + segundos
    while True:
        estado = observar(p, t, home)
        restante = fin - time.monotonic()
        if estado != 'running' or restante <= 0:
            return estado
        time.sleep(min(0.5, restante))


def marcar_log(h: Host, tarea: int, c: Cortar) -> None:
    campos = fila(c)
    nombres = ('motivo', 'limite_min', 'grupo', 'transcurrido_min', 'sin_salida_min')
    with (h.task_logs / f'{tarea}.log').open('a', encoding='utf-8') as salida:
        salida.write('\nPAO_VIGILANTE_CORTE ' + ' '.join(f'{k}={campos[k]}' for k in nombres) + '\n')


def cortar(p: Pueue, h: Host, c: Cortar, home: Path, gracia_s: float) -> Desenlace:
    if observar(p, c.tarea, home) != 'running':
        bitacora(h, 'cambio', tarea=c.tarea.id)
        return Desenlace.CAMBIO
    bitacora(h, 'corte', modo='cortar', **fila(c))
    aceptada = p.kill(c.tarea.id, 'SIGTERM')
    estado = esperar_fin(p, c.tarea, home, gracia_s)
    desenlace = Desenlace.TERMINADA
    if estado == 'running':
        estado = observar(p, c.tarea, home)
        if estado == 'running':
            aceptada = p.kill(c.tarea.id, None)
            estado = esperar_fin(p, c.tarea, home, 10)
            desenlace = Desenlace.FORZADA
    if estado == 'cambio':
        desenlace = Desenlace.CAMBIO
        bitacora(h, 'cambio', tarea=c.tarea.id)
    elif estado == 'running':
        desenlace = Desenlace.PERSISTE
        bitacora(h, 'persiste', tarea=c.tarea.id, huella=c.tarea.huella)
    bitacora(h, 'fin_corte', tarea=c.tarea.id, huella=c.tarea.huella,
             desenlace=desenlace.value, resultado_pueue=estado, aceptada=aceptada)
    if estado == 'done':
        try:
            marcar_log(h, c.tarea.id, c)
        except OSError:
            bitacora(h, 'error', operacion='marca', tarea=c.tarea.id)
    return desenlace


def pulso(h: Host, ahora: float) -> float:
    ruta = h.estado / 'pulso.json'
    try:
        dato = json.loads(ruta.read_text())
        ultima, reanudado = dato['ultima'], dato['reanudado_en']
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in (ultima, reanudado)):
            raise ValueError('pulso no finito')
        hueco = ahora - ultima > HUECO_S or ahora < ultima or reanudado > ultima
    except (OSError, ValueError, KeyError, TypeError):
        hueco = True
    if hueco:
        reanudado = ahora
        bitacora(h, 'hueco', reanudado_en=reanudado)
    temporal = ruta.with_suffix('.tmp')
    temporal.write_text(json.dumps(dict(ultima=ahora, reanudado_en=reanudado)))
    os.replace(temporal, ruta)
    return reanudado


def pasada(h: Host, modo: Modo, *, ahora: Callable[[], float] = time.time,
           gracia_s: float = GRACIA_S) -> int:
    h.estado.mkdir(parents=True, exist_ok=True)
    with (h.estado / 'pasada.lock').open('a') as candado:
        try:
            fcntl.flock(candado, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            bitacora(h, 'omitida', modo=modo.value)
            return 0
        try:
            p = Pueue(h.pueue)
            tareas, ilegibles = leer_cola(p.status())
            instante = ahora()
            reanudado = pulso(h, instante)
            home = h.home
            conteos = Counter()
            for ilegible in ilegibles:
                bitacora(h, 'ilegible', clave=ilegible.clave, motivo=ilegible.motivo)
            for t in tareas:
                if t.estado != 'Running':
                    continue
                clase = clasificar(t, home)
                conteos[clase.clase.value] += 1
                if clase.clase is not Clase.MANUAL:
                    continue
                try:
                    mtime = (h.task_logs / f'{t.id}.log').stat().st_mtime
                except FileNotFoundError:
                    mtime = None
                lim = limites(t.grupo, t.etiqueta)
                if lim.aviso:
                    bitacora(h, 'aviso', tarea=t.id, aviso=lim.aviso)
                decision = decidir(t, lim, mtime, reanudado, instante)
                if isinstance(decision, Indeterminada):
                    bitacora(h, 'indeterminada', tarea=t.id, motivo=decision.motivo)
                elif isinstance(decision, Cortar):
                    if modo is Modo.INFORME:
                        bitacora(h, 'cortaria', modo=modo.value, **fila(decision))
                    else:
                        cortar(p, h, decision, home, gracia_s)
            if conteos['manual'] or conteos['dudosa'] or ilegibles:
                bitacora(h, 'pasada', modo=modo.value, clases=dict(conteos), ilegibles=len(ilegibles))
            return 0
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            bitacora(h, 'error', modo=modo.value, operacion='pasada')
            return 1


def censo(h: Host, home: Path) -> str:
    tareas, ilegibles = leer_cola(Pueue(h.pueue).status())
    conteos = Counter()
    reglas = Counter()
    for t in tareas:
        clase = clasificar(t, home)
        conteos[clase.clase.value] += 1
        reglas[clase.regla] += 1
    return json.dumps(dict(host=h.nombre, clases={c.value: conteos[c.value] for c in Clase},
                           reglas=dict(reglas), ilegibles=len(ilegibles)), ensure_ascii=False)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('accion', choices=('pasada', 'censo'))
    parser.add_argument('--host', required=True, choices=('mini', 'desk'))
    parser.add_argument('--pueue', required=True, type=Path)
    parser.add_argument('--modo', choices=('informe', 'cortar'))
    args = parser.parse_args(argv)
    h = host(args.host, args.pueue, Path.home())
    if args.accion == 'censo':
        try:
            print(censo(h, Path.home()))
            return 0
        except (OSError, ValueError, subprocess.SubprocessError):
            return 1
    if args.modo is None:
        parser.error('pasada requiere --modo')
    return pasada(h, Modo(args.modo))


if __name__ == '__main__':
    raise SystemExit(main())
