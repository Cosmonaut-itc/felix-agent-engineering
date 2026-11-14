#!/usr/bin/python3 -I
"""Instalación desde la MacBook mediante una carga autónoma por SSH."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
from typing import Iterator, Optional
from xml.sax.saxutils import escape

LABEL = 'local.pao.pueue-vigilante'
SERVICE = 'pao-pueue-vigilante.service'
TIMER = 'pao-pueue-vigilante.timer'
FILES = {}


def archivos(root: Path) -> dict[str, str]:
    rutas = [root / 'vigilante.py', root / 'launchagents' / (LABEL + '.plist'),
             root / 'units' / SERVICE, root / 'units' / TIMER]
    return {p.name: p.read_text(encoding='utf-8') for p in rutas}


def cargar_vigilante(source: str) -> types.ModuleType:
    modulo = types.ModuleType('_pao_vigilante_carga')
    sys.modules[modulo.__name__] = modulo
    exec(compile(source, 'vigilante.py', 'exec'), modulo.__dict__)
    return modulo


def destinos(nombre: str, home: Path) -> dict[str, Path]:
    resultado = {'vigilante.py': home / '.local/lib/pao-pueue-vigilante/vigilante.py'}
    if nombre == 'mini':
        resultado[LABEL + '.plist'] = home / 'Library/LaunchAgents' / (LABEL + '.plist')
    else:
        resultado.update({n: home / '.config/systemd/user' / n for n in (SERVICE, TIMER)})
    return resultado


def renderizar(nombre: str, home: Path, modo: str, files: dict[str, str]) -> dict[Path, str]:
    resultado = {}
    for clave, ruta in destinos(nombre, home).items():
        valor_home = escape(str(home)) if clave.endswith('.plist') else str(home)
        # systemd expande % incluso en argumentos entre comillas.
        if clave.endswith('.service'):
            valor_home = valor_home.replace('%', '%%').replace('\\', '\\\\').replace('"', '\\"')
        resultado[ruta] = (files[clave] if clave == 'vigilante.py' else
                           files[clave].replace('@HOME@', valor_home).replace('@MODO@', modo))
    return resultado


def ejecutar(argv: list[str], *, comprobar: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(argv, check=comprobar, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, timeout=30)


def previas(nombre: str, home: Path, files: dict[str, str]) -> None:
    if nombre == 'mini':
        ejecutar(['launchctl', 'print', f'gui/{os.getuid()}/sh.brew.pueue'])
        binario = Path('/opt/homebrew/bin/pueue')
    else:
        ejecutar(['systemctl', '--user', 'is-active', 'pueued.service'])
        binario = home / '.local/bin/pueue'
    v = cargar_vigilante(files['vigilante.py'])
    h = v.host(nombre, binario, home)
    _, ilegibles = v.leer_cola(v.Pueue(binario).status())
    if ilegibles:
        raise ValueError('comprobación previa: tareas ilegibles')
    lectura = v.censo(h, home)
    if json.loads(lectura)['ilegibles']:
        raise ValueError('comprobación previa: censo con tareas ilegibles')
    print('censo: ' + lectura, flush=True)


def sello() -> str:
    return datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')


def atomico(ruta: Path, contenido: bytes) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporal = tempfile.mkstemp(prefix=ruta.name + '.', suffix='.tmp', dir=str(ruta.parent))
    try:
        with os.fdopen(descriptor, 'wb') as salida:
            salida.write(contenido)
            salida.flush()
            os.fsync(salida.fileno())
        os.replace(temporal, ruta)
    finally:
        if os.path.exists(temporal):
            os.unlink(temporal)


def detener(nombre: str, home: Path) -> None:
    if nombre == 'mini':
        servicio = f'gui/{os.getuid()}/{LABEL}'
        if ejecutar(['launchctl', 'print', servicio], comprobar=False).returncode == 0:
            ejecutar(['launchctl', 'bootout', servicio])
    else:
        # No detener pueued ni ninguna guardia. Un oneshot vivo también se detiene.
        for unidad, accion in ((TIMER, ['disable', '--now']), (SERVICE, ['stop'])):
            if (destinos(nombre, home)[unidad].exists()
                    or ejecutar(['systemctl', '--user', 'is-active', '--quiet', unidad],
                                comprobar=False).returncode == 0):
                ejecutar(['systemctl', '--user'] + accion + [unidad])


def cargar(nombre: str, home: Path) -> None:
    if nombre == 'mini':
        ejecutar(['launchctl', 'bootstrap', f'gui/{os.getuid()}',
                  str(destinos(nombre, home)[LABEL + '.plist'])])
    else:
        ejecutar(['systemctl', '--user', 'daemon-reload'])
        ejecutar(['systemctl', '--user', 'enable', '--now', TIMER])


@contextmanager
def bloquear(estado: Path) -> Iterator[None]:
    estado.mkdir(parents=True, exist_ok=True)
    with (estado / 'pasada.lock').open('a') as candado:
        fcntl.flock(candado, fcntl.LOCK_EX)
        yield


def respaldo_reciente(rutas: list[Path]) -> dict[Path, bytes]:
    juegos = None
    for ruta in rutas:
        sellos = {p.name[len(ruta.name + '.bak-'):] for p in ruta.parent.glob(ruta.name + '.bak-*')}
        juegos = sellos if juegos is None else juegos.intersection(sellos)
    if not juegos:
        raise ValueError('no hay un juego completo de respaldos')
    ultimo = max(juegos)
    return {p: p.with_name(p.name + '.bak-' + ultimo).read_bytes() for p in rutas}


def remoto(accion: str, nombre: str, modo: Optional[str], seco: bool,
           files: dict[str, str]) -> int:
    home = Path.home()
    rutas = list(destinos(nombre, home).values())
    print(json.dumps(dict(accion=accion, host=nombre, modo=modo, seco=seco,
                          archivos=[str(p) for p in rutas]), ensure_ascii=False), flush=True)
    # También en seco: las comprobaciones y el censo son sólo lectura.
    previas(nombre, home, files)
    if accion == 'instalar':
        contenido = {p: s.encode('utf-8') for p, s in renderizar(nombre, home, modo, files).items()}
    elif accion == 'volver':
        contenido = respaldo_reciente(rutas)
    else:
        contenido = {}
    if seco:
        return 0
    existentes = [p for p in rutas if p.exists()]
    if accion == 'retirar' and not existentes:
        return 0
    # Detener sólo el trabajo del vigilante; después esperar su candado.
    if existentes:
        detener(nombre, home)
    estado = home / '.local/state/pao/pueue-vigilante'
    with bloquear(estado):
        marca = sello()
        if accion == 'retirar':
            for p in existentes:
                p.rename(p.with_name(p.name + '.retirado-' + marca))
        else:
            cambiado = any(not p.exists() or p.read_bytes() != s for p, s in contenido.items())
            # volver respalda con otro sufijo para que el juego .bak- más reciente no cambie.
            sufijo = '.bak-' if accion == 'instalar' else '.antes-volver-'
            if cambiado:
                for p in existentes:
                    shutil.copy2(p, p.with_name(p.name + sufijo + marca))
            for p, valor in contenido.items():
                if not p.exists() or p.read_bytes() != valor:
                    atomico(p, valor)
    if accion == 'retirar':
        if nombre == 'desk':
            ejecutar(['systemctl', '--user', 'daemon-reload'])
    else:
        cargar(nombre, home)
    return 0


def programa(root: Path, accion: str, nombre: str, modo: Optional[str], seco: bool) -> str:
    source = (root / 'instalar.py').read_text(encoding='utf-8').rsplit("\nif __name__ == '__main__':", 1)[0]
    return (source + '\nFILES = ' + repr(archivos(root)) + '\n'
            + f'remoto({accion!r}, {nombre!r}, {modo!r}, {seco!r}, FILES)\n')


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('accion', choices=('instalar', 'retirar', 'volver'))
    parser.add_argument('--host', required=True, choices=('mini', 'desk'))
    parser.add_argument('--modo', choices=('informe', 'cortar'))
    parser.add_argument('--seco', action='store_true')
    args = parser.parse_args(argv)
    if args.accion == 'instalar' and args.modo is None:
        parser.error('instalar requiere --modo')
    payload = programa(Path(__file__).resolve().parent, args.accion, args.host, args.modo, args.seco)
    return subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ControlPath=none', args.host,
                           '/usr/bin/python3', '-I', '-'], input=payload, text=True).returncode


if __name__ == '__main__':
    raise SystemExit(main())
