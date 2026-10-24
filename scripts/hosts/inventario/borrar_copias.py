#!/usr/bin/env python3
"""Valida cuatro copias autorizadas de remote-run; en seco por defecto."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys

sys.dont_write_bytecode = True

from inventario import inventariar


COPIAS_AUTORIZADAS = (
    '.comprador-felix-mode-83cc1dfe',
    '.comprador-humo-099-f3371349',
    '.comprador-humo-103-680cb5db',
    '.comprador-humo-int-e5fab47b',
)
HOST_MINI = 'Felixs-Mac-mini'
RAIZ_DEFAULT = '~/remote-run'
FLAGS_DIRECTORIO = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
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


def identidad(info):
    return [info.st_dev, info.st_ino]


def capacidades():
    if (not all(f in os.supports_dir_fd for f in
                (os.rename, os.open, os.unlink, os.rmdir, os.stat))
            or os.scandir not in os.supports_fd):
        raise ValueError('faltan operaciones seguras por descriptor')


def leer_host():
    salida = subprocess.run(['/usr/sbin/scutil', '--get', 'LocalHostName'],
                            capture_output=True, text=True, timeout=10,
                            env=dict(os.environ, LC_ALL='C'))
    if salida.returncode != 0 or not salida.stdout.strip():
        raise ValueError('scutil no acreditó LocalHostName: ' + salida.stderr.strip())
    return salida.stdout.strip()


def abrir_work(raiz, identidades):
    fd = os.open(str(raiz / 'work'), FLAGS_DIRECTORIO)
    try:
        if identidad(os.fstat(fd)) != identidades['work']:
            raise ValueError('work cambió al abrir el descriptor')
        return fd
    except BaseException:
        os.close(fd)
        raise


def comprobar_work(raiz, work_fd, identidades):
    actuales = {'raiz': directorio(raiz), 'work': directorio(raiz / 'work')}
    if (actuales['raiz'] != identidades['raiz'] or actuales['work'] != identidades['work']
            or actuales['work'] != identidad(os.fstat(work_fd))):
        raise ValueError('raíz o work cambió de identidad por ruta')


def existe(nombre, fd):
    try:
        return os.stat(nombre, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def cuarentenas_previas(raiz, work_fd):
    halladas = []
    with os.scandir(work_fd) as entradas:
        for entrada in entradas:
            if any(entrada.name.startswith('.borrando-' + n + '-') for n in COPIAS_AUTORIZADAS):
                halladas.append({'ruta': str(raiz / 'work' / entrada.name),
                                 'identidad': identidad(entrada.stat(follow_symlinks=False))})
    return sorted(halladas, key=lambda x: x['ruta'])


def progreso(copia, estado):
    print('{} {} {} {}'.format(hora(), estado, copia['nombre'], copia.get('cuarentena', '')),
          file=sys.stderr, flush=True)


def cuarentenar(work_fd, nombre, cuarentena):
    if existe(cuarentena, work_fd) is not None:
        raise ValueError('el nombre de cuarentena ya existe')
    os.rename(nombre, cuarentena, src_dir_fd=work_fd, dst_dir_fd=work_fd)


def consultar_lsof(ruta):
    argumentos = ['/usr/sbin/lsof', '-a', '-p', '^' + str(os.getpid()), '-nP', '+D', str(ruta)]
    paso = {'horaUTC': hora(), 'argumentos': argumentos, 'timeout': 120,
            'codigo': None, 'stdout': '', 'stderr': '', 'libre': False}
    try:
        salida = subprocess.run(argumentos, capture_output=True, text=True, timeout=120)
        paso.update(codigo=salida.returncode, stdout=salida.stdout, stderr=salida.stderr)
        paso['libre'] = salida.returncode == 1 and salida.stdout == ''
        if not paso['libre']:
            paso['error'] = 'lsof detectó uso o no acreditó ausencia de uso'
    except ERRORES as exc:
        paso['error'] = str(exc)
    paso['fin'] = {'horaUTC': hora()}
    return paso


def consultar_ps(nombres):
    paso = {'horaUTC': hora(), 'codigo': None, 'coincidencias': [], 'libre': False}
    try:
        salida = subprocess.run(['/bin/ps', '-axo', 'pid=,command='],
                                capture_output=True, text=True, timeout=30)
        paso.update(codigo=salida.returncode, stderr=salida.stderr)
        if salida.returncode != 0:
            raise ValueError('ps terminó con código distinto de cero')
        if not salida.stdout.strip():
            raise ValueError('ps devolvió una lista vacía')
        for linea in salida.stdout.splitlines():
            campos = linea.strip().split(maxsplit=1)
            if len(campos) != 2 or not campos[0].isdigit():
                raise ValueError('línea de ps inválida')
            if int(campos[0]) != os.getpid() and any(n in campos[1] for n in nombres):
                paso['coincidencias'].append(linea)
        paso['libre'] = not paso['coincidencias']
    except ERRORES as exc:
        paso['error'] = str(exc)
    paso['fin'] = {'horaUTC': hora()}
    return paso


def comprobar_uso(fd, ruta, raiz, nombre):
    paso = {'horaUTC': hora(), 'libre': False}
    lock = {'horaUTC': hora(), 'libre': False}
    try:
        lock['libre'] = existe('.remote-run.lock', fd) is None
    except ERRORES as exc:
        lock['error'] = str(exc)
    paso['lock'] = lock
    paso['lsof'] = consultar_lsof(ruta)
    paso['ps'] = consultar_ps((nombre, Path(ruta).name))
    inventario = {'horaUTC': hora(), 'libre': False}
    try:
        datos = inventariar(raiz, tamanos=False)
        proteccion = datos['proteccionCopias']
        inventario.update(ts=datos['ts'], bloqueoGlobal=proteccion['bloqueoGlobal'],
                          corridas=proteccion['porCopia'].get(nombre, []))
        inventario['libre'] = not inventario['bloqueoGlobal'] and not inventario['corridas']
    except ERRORES as exc:
        inventario['error'] = str(exc)
    paso['inventario'] = inventario
    paso['libre'] = all(paso[k]['libre'] for k in ('lock', 'lsof', 'ps', 'inventario'))
    paso['fin'] = {'horaUTC': hora()}
    return paso


def devolver(work_fd, copia, error):
    nombre = copia['nombre']
    cuarentena = Path(copia['cuarentena']).name
    paso = {'horaUTC': hora(), 'devuelta': False}
    copia['devolucion'] = paso
    try:
        if existe(nombre, work_fd) is not None:
            raise ValueError('nombre original recreado; se conserva la cuarentena')
        os.rename(cuarentena, nombre, src_dir_fd=work_fd, dst_dir_fd=work_fd)
        paso['devuelta'] = True
        estado = 'devuelta'
        progreso(copia, estado)
    except ERRORES as exc:
        paso['error'] = str(exc)
        estado = 'en-cuarentena'
        progreso(copia, 'error')
    copia['resultado'] = {'horaUTC': hora(), 'estado': estado, 'error': error}
    paso['fin'] = {'horaUTC': hora()}


def vaciar(fd, dispositivo):
    with os.scandir(fd) as entradas:
        for entrada in entradas:
            if entrada.is_dir(follow_symlinks=False):
                hijo = os.open(entrada.name, FLAGS_DIRECTORIO, dir_fd=fd)
                try:
                    info = os.fstat(hijo)
                    if info.st_dev != dispositivo:
                        raise ValueError('directorio en otro dispositivo: ' + entrada.name)
                    vaciar(hijo, dispositivo)
                finally:
                    os.close(hijo)
                os.rmdir(entrada.name, dir_fd=fd)
            else:
                os.unlink(entrada.name, dir_fd=fd)


def eliminar(raiz, work_fd, copia, informe):
    nombre = copia['nombre']
    identidades = copia['reverificacion']['identidades']
    comprobar_work(raiz, work_fd, identidades)
    cuarentena = '.borrando-{}-{}-{}'.format(nombre, datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
                                           secrets.token_hex(8))
    ruta = raiz / 'work' / cuarentena
    cuarentenar(work_fd, nombre, cuarentena)
    informe.setdefault('primeraEscritura', {'horaUTC': hora()})
    copia['cuarentena'] = str(ruta)
    copia['resultado'] = {'horaUTC': hora(), 'estado': 'en-cuarentena'}
    progreso(copia, 'cuarentena')
    fd = None
    try:
        try:
            fd = os.open(cuarentena, FLAGS_DIRECTORIO, dir_fd=work_fd)
            info = os.fstat(fd)
            if identidad(info) != identidades['copia']:
                raise ValueError('identidad distinta tras la cuarentena')
            copia['uso'] = comprobar_uso(fd, ruta, raiz, nombre)
            if not copia['uso']['libre']:
                raise ValueError('lock, proceso o inventario no acreditan ausencia de uso')
        except ERRORES as exc:
            progreso(copia, 'error')
            devolver(work_fd, copia, str(exc))
            return False
        try:
            copia['borrado'] = {'horaUTC': hora()}
            vaciar(fd, info.st_dev)
            os.rmdir(cuarentena, dir_fd=work_fd)
            copia['resultado'] = {'horaUTC': hora(), 'estado': 'borrada'}
            progreso(copia, 'borrada')
            return True
        except ERRORES as exc:
            copia['resultado'] = {'horaUTC': hora(), 'estado': 'error-parcial', 'error': str(exc)}
            copia['duPosterior'] = medir_du(ruta, None)
            progreso(copia, 'error')
            return False
        except BaseException as exc:
            copia['resultado'] = {'horaUTC': hora(), 'estado': 'error-parcial',
                                  'error': type(exc).__name__ + ': ' + str(exc)}
            copia['duPosterior'] = medir_du(ruta, None)
            progreso(copia, 'error')
            raise
    finally:
        if fd is not None:
            os.close(fd)


def listado_posterior(raiz, copias, work_fd=None, identidades=None):
    paso = {'horaUTC': hora(), 'nombres': [], 'comprobaciones': []}
    propio = work_fd is None
    try:
        if propio:
            _, identidades = contenedor(raiz)
            work_fd = abrir_work(raiz, identidades)
        with os.scandir(work_fd) as entradas:
            items = {e.name: identidad(e.stat(follow_symlinks=False)) for e in entradas}
        paso['nombres'] = sorted(items)
        comprobar_work(raiz, work_fd, identidades)
        for copia in copias:
            nombre = copia['nombre']
            borrada = copia['resultado']['estado'] == 'borrada'
            comprobacion = {'horaUTC': hora(), 'nombre': nombre, 'existe': nombre in items,
                            'borrada': borrada, 'estado': 'presente' if nombre in items else 'ausente'}
            paso['comprobaciones'].append(comprobacion)
            if borrada:
                validada = copia['validacion']['identidades']['copia']
                if validada in items.values() or Path(copia['cuarentena']).name in items:
                    raise ValueError('persiste una identidad borrada o su nombre de cuarentena: ' + nombre)
                if nombre in items:
                    comprobacion['estado'] = 'recreada-por-productor'
            if copia['resultado']['estado'] == 'conservada-en-seco' and nombre not in items:
                raise ValueError('una copia desapareció durante el modo seco: ' + nombre)
    except ERRORES as exc:
        paso['error'] = str(exc)
    finally:
        if propio and work_fd is not None:
            os.close(work_fd)
    paso['fin'] = {'horaUTC': hora()}
    return paso


def procesar(raiz, ejecutar, informe):
    work_fd = None
    identidades = None
    try:
        capacidades()
        _, identidades = contenedor(raiz)
        work_fd = abrir_work(raiz, identidades)
        informe['cuarentenasPrevias'] = cuarentenas_previas(raiz, work_fd)
        if informe['cuarentenasPrevias']:
            raise ValueError('hay cuarentenas previas; requieren una autorización nueva')
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
        if not all(x['validacion']['valida'] for x in informe['copias']):
            raise ValueError('validación previa rechazada; no se borra ninguna copia')
        if any(x['validacion']['identidades']['raiz'] != identidades['raiz']
               or x['validacion']['identidades']['work'] != identidades['work']
               or x['validacion']['identidades']['work'] != identidad(os.fstat(work_fd))
               for x in informe['copias']):
            raise ValueError('work cambió desde su apertura')
        for copia in informe['copias']:
            copia['du'] = medir_du(raiz / 'work' / copia['nombre'],
                                   copia['validacion']['item']['tamanoBytes'])
        if any('error' in x['du'] for x in informe['copias']):
            raise ValueError('du no acreditado para las cuatro; no se borra ninguna copia')
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
                informe['error'] = 're-verificación rechazada en raiz/work: ' + copia['nombre']
                break
            if ejecutar:
                if not eliminar(raiz, work_fd, copia, informe):
                    informe['error'] = 'borrado detenido: ' + copia['nombre']
                    break
            else:
                copia['uso'] = {'horaUTC': hora(),
                                'lsof': consultar_lsof(raiz / 'work' / copia['nombre']),
                                'ps': consultar_ps((copia['nombre'],))}
                if not all(copia['uso'][k]['libre'] for k in ('lsof', 'ps')):
                    copia['resultado'] = {'horaUTC': hora(), 'estado': 'rechazada'}
                    informe['error'] = 'uso detectado o no verificable en seco: ' + copia['nombre']
                    break
                copia['resultado'] = {'horaUTC': hora(), 'estado': 'conservada-en-seco'}
        else:
            informe['resultado'] = 'borrado-completo' if ejecutar else 'validado-en-seco'
    except ERRORES as exc:
        informe['error'] = str(exc)
        if informe.get('primeraEscritura'):
            progreso(next((c for c in reversed(informe['copias']) if 'cuarentena' in c),
                          {'nombre': 'sin-copia'}), 'error')
    finally:
        try:
            informe['listadoPosterior'] = listado_posterior(raiz, informe['copias'], work_fd, identidades)
            if 'error' in informe['listadoPosterior'] and informe['resultado'] in ('borrado-completo', 'validado-en-seco'):
                informe['resultado'] = 'detenido'
            informe['fin'] = {'horaUTC': hora()}
        finally:
            if work_fd is not None:
                os.close(work_fd)
    return informe


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--raiz', default=None)
    parser.add_argument('--ejecutar', action='store_true')
    args = parser.parse_args(argv)
    host = {'horaUTC': hora(), 'esperado': HOST_MINI, 'leido': None, 'coincide': False}
    try:
        host['leido'] = leer_host()
        host['coincide'] = host['leido'] == HOST_MINI
    except ERRORES as exc:
        host['error'] = str(exc)
    if args.ejecutar and (args.raiz is not None or not host['coincide']):
        parser.error('--ejecutar exige el LocalHostName autorizado y no admite --raiz')
    raiz = Path(os.path.abspath(os.path.expanduser(args.raiz if args.raiz is not None else RAIZ_DEFAULT)))
    informe = {'schema': 'remote-run-borrado-copias@2', 'horaUTC': hora(),
               'modo': 'ejecutar' if args.ejecutar else 'seco', 'raiz': str(raiz), 'host': host,
               'inventario': None, 'copias': [], 'resultado': 'rechazado'}
    try:
        procesar(raiz, args.ejecutar, informe)
    except BaseException as exc:
        if informe.get('primeraEscritura'):
            informe.update(resultado='interrumpido', interrupcion=type(exc).__name__)
            progreso(next((c for c in reversed(informe['copias']) if 'cuarentena' in c),
                          {'nombre': 'sin-copia'}), 'error')
            print(json.dumps(informe, ensure_ascii=False, indent=2, allow_nan=False), flush=True)
        raise
    print(json.dumps(informe, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if informe['resultado'] in ('validado-en-seco', 'borrado-completo') else 1


if __name__ == '__main__':
    sys.exit(main())
