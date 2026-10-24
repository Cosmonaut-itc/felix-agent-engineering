#!/usr/bin/env python3
"""Valida cuatro copias autorizadas de remote-run; en seco por defecto."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

from inventario import inventariar


COPIAS_AUTORIZADAS = (
    '.comprador-felix-mode-83cc1dfe',
    '.comprador-humo-099-f3371349',
    '.comprador-humo-103-680cb5db',
    '.comprador-humo-int-e5fab47b',
)
ERRORES = (OSError, ValueError, UnicodeError, subprocess.SubprocessError)


def hora():
    return datetime.now(timezone.utc).isoformat()


def directorio(ruta):
    info = ruta.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError('no es directorio real o es symlink: ' + str(ruta))
    return [info.st_dev, info.st_ino]


def contenedor(raiz):
    identidades = {'raiz': directorio(raiz), 'work': directorio(raiz / 'work')}
    work = (raiz / 'work').resolve(strict=True)
    if work.parent != raiz.resolve(strict=True):
        raise ValueError('work fuera de la raíz real')
    return work, identidades


def validar(raiz, nombre, datos, error=None):
    paso = {'horaUTC': hora(), 'valida': False, 'item': None, 'motivos': [],
            'identidades': None, 'inventarioTs': datos['ts'] if datos is not None else None}
    ruta = raiz / 'work' / nombre
    if datos is not None:
        paso['item'] = next((x for x in datos['copias'] if x['ruta'] == str(ruta)), None)
    if error is not None:
        paso['motivos'].append('inventario falló: ' + error)
    try:
        work, identidades = contenedor(raiz)
        identidades['copia'] = directorio(ruta)
        if ruta.resolve(strict=True).parent != work:
            raise ValueError('el padre real de la copia no es raiz/work')
        paso['identidades'] = identidades
        try:
            (ruta / '.remote-run.lock').lstat()
        except FileNotFoundError:
            pass
        else:
            paso['motivos'].append('existe .remote-run.lock; también se rechazan locks huérfanos')
    except ERRORES as exc:
        paso['motivos'].append(str(exc))
    item = paso['item']
    # El inventario conserva la asociación, la duda y el bloqueo global sin reinterpretar ps o meta.
    if item is None:
        paso['motivos'].append('la copia no figura en el inventario')
    elif item['borrable'] is not True:
        paso['motivos'].append('inventario no borrable: ' + item['motivo'])
    paso['valida'] = not paso['motivos']
    paso['fin'] = {'horaUTC': hora()}
    return paso


def observar(raiz):
    try:
        return inventariar(raiz, tamanos=True), None
    except ERRORES as exc:
        return None, str(exc)


def medir_du(ruta, tamano_bytes):
    paso = {'horaUTC': hora(), 'tamanoBytes': tamano_bytes,
            'codigo': None, 'stdout': '', 'stderr': ''}
    try:
        salida = subprocess.run(['/usr/bin/du', '-sk', str(ruta)],
                                capture_output=True, text=True, timeout=30)
        paso.update(codigo=salida.returncode, stdout=salida.stdout, stderr=salida.stderr)
        if salida.returncode != 0:
            paso['error'] = 'du terminó con código distinto de cero'
        else:
            partes = salida.stdout.split(maxsplit=1)
            if len(partes) != 2 or not partes[0].isdigit():
                paso['error'] = 'salida de du inválida'
    except ERRORES as exc:
        paso['error'] = str(exc)
    paso['fin'] = {'horaUTC': hora()}
    return paso


def eliminar(raiz, nombre, identidades):
    if not getattr(shutil.rmtree, 'avoids_symlink_attacks', False):
        raise ValueError('shutil.rmtree.avoids_symlink_attacks no es verdadero')
    work, actuales = contenedor(raiz)
    ruta = raiz / 'work' / nombre
    actuales['copia'] = directorio(ruta)
    if actuales != identidades or ruta.resolve(strict=True).parent != work:
        raise ValueError('la ruta o su identidad cambió antes de rmtree')
    fd = os.open(str(work), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if [info.st_dev, info.st_ino] != identidades['work']:
            raise ValueError('work cambió al abrirlo')
        anterior = os.open('.', os.O_RDONLY | os.O_DIRECTORY)
        try:
            # Python 3.9 no ofrece dir_fd en rmtree; el cwd anclado evita atravesar ancestros sustituidos.
            os.fchdir(fd)
            if directorio(Path(nombre)) != identidades['copia']:
                raise ValueError('la copia cambió al anclar work')
            shutil.rmtree(nombre)
        finally:
            os.fchdir(anterior)
            os.close(anterior)
    finally:
        os.close(fd)


def listado_posterior(raiz, copias):
    paso = {'horaUTC': hora(), 'nombres': [], 'comprobaciones': []}
    try:
        contenedor(raiz)
        with os.scandir(raiz / 'work') as entradas:
            paso['nombres'] = sorted(e.name for e in entradas)
        for copia in copias:
            ruta = raiz / 'work' / copia['nombre']
            try:
                ruta.lstat()
                existe = True
            except FileNotFoundError:
                existe = False
            borrada = copia['resultado']['estado'] == 'borrada'
            paso['comprobaciones'].append({'nombre': copia['nombre'], 'existe': existe,
                                           'borrada': borrada, 'horaUTC': hora()})
            if borrada and existe:
                raise ValueError('una copia borrada sigue existiendo: ' + copia['nombre'])
            if copia['resultado']['estado'] == 'conservada-en-seco' and not existe:
                raise ValueError('una copia desapareció durante el modo seco: ' + copia['nombre'])
    except ERRORES as exc:
        paso['error'] = str(exc)
    paso['fin'] = {'horaUTC': hora()}
    return paso


def procesar(raiz, ejecutar):
    informe = {'schema': 'remote-run-borrado-copias@1', 'horaUTC': hora(),
               'modo': 'ejecutar' if ejecutar else 'seco', 'raiz': str(raiz),
               'inventario': None, 'copias': [], 'resultado': 'rechazado'}
    datos, error = observar(raiz)
    if datos is not None:
        informe['inventario'] = {'horaUTC': hora(), 'ts': datos['ts'], 'host': datos['host'],
                                  'totales': datos['totales']}
    for nombre in COPIAS_AUTORIZADAS:
        informe['copias'].append({
            'nombre': nombre, 'validacion': validar(raiz, nombre, datos, error),
            'du': None, 'reverificacion': None,
            'resultado': {'horaUTC': hora(), 'estado': 'no-procesada'},
        })
    if not getattr(shutil.rmtree, 'avoids_symlink_attacks', False):
        informe['error'] = 'shutil.rmtree.avoids_symlink_attacks no es verdadero'
    elif not all(x['validacion']['valida'] for x in informe['copias']):
        informe['error'] = 'validación previa rechazada; no se borra ninguna copia'
    else:
        for copia in informe['copias']:
            copia['du'] = medir_du(raiz / 'work' / copia['nombre'],
                                   copia['validacion']['item']['tamanoBytes'])
        if any('error' in x['du'] for x in informe['copias']):
            informe['error'] = 'du no acreditado para las cuatro; no se borra ninguna copia'
        else:
            informe['resultado'] = 'detenido'
            for copia in informe['copias']:
                datos, error = observar(raiz)
                paso = validar(raiz, copia['nombre'], datos, error)
                copia['reverificacion'] = paso
                if paso['identidades'] != copia['validacion']['identidades']:
                    paso['valida'] = False
                    paso['motivos'].append('identidad distinta de la validación previa')
                if not paso['valida']:
                    copia['resultado'] = {'horaUTC': hora(), 'estado': 'rechazada'}
                    informe['error'] = 're-verificación rechazada: ' + copia['nombre']
                    break
                try:
                    if ejecutar:
                        eliminar(raiz, copia['nombre'], paso['identidades'])
                    copia['resultado'] = {'horaUTC': hora(),
                                           'estado': 'borrada' if ejecutar else 'conservada-en-seco'}
                except ERRORES as exc:
                    copia['resultado'] = {'horaUTC': hora(), 'estado': 'error', 'error': str(exc)}
                    informe['error'] = 'borrado detenido: ' + copia['nombre']
                    break
            else:
                informe['resultado'] = 'borrado-completo' if ejecutar else 'validado-en-seco'
    informe['listadoPosterior'] = listado_posterior(raiz, informe['copias'])
    if 'error' in informe['listadoPosterior']:
        informe['errorListado'] = informe['listadoPosterior']['error']
        if informe['resultado'] != 'rechazado':
            informe['resultado'] = 'detenido'
    informe['fin'] = {'horaUTC': hora()}
    return informe


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--raiz', default='~/remote-run')
    parser.add_argument('--ejecutar', action='store_true')
    args = parser.parse_args(argv)
    raiz = Path(os.path.abspath(os.path.expanduser(args.raiz)))
    informe = procesar(raiz, args.ejecutar)
    print(json.dumps(informe, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if informe['resultado'] in ('validado-en-seco', 'borrado-completo') else 1


if __name__ == '__main__':
    sys.exit(main())
