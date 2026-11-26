"""Coordinador desacoplado: reenganche por clave sobre un registro durable POSIX."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time


INTENTOS_TRANSPORTE = 3
SALIDAS_TRANSPORTE = {255, 127}
CANCELAR = 'cancelar.json'
ACUSE_CANCELACION = 'cancelacion.json'
SONDEO_CANCELACION_S = .1


def clave_registro(registro):
    return 'pao-jale-' + hashlib.sha256(os.fsencode(registro.resolve())).hexdigest()


def clave_iniciada(anterior, campo='clave'):
    clave = anterior.get(campo) if isinstance(anterior, dict) else None
    if isinstance(clave, str) and re.fullmatch(r'[A-Za-z0-9._-]{8,128}', clave):
        return clave


def guardar(ruta, valor):
    temporal = ruta.with_suffix('.tmp')
    with temporal.open('w', encoding='utf-8') as archivo:
        json.dump(valor, archivo, sort_keys=True)
        archivo.flush()
        os.fsync(archivo.fileno())
    os.replace(str(temporal), str(ruta))
    descriptor = os.open(str(ruta.parent), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def ejecutar(registro, argv, entorno, salida):
    pedido = registro / CANCELAR
    if pedido.exists():
        return 75
    if not (registro / 'prelanzada.json').exists():
        return subprocess.run(argv, env=entorno, stdout=salida, stderr=salida,
                              check=False).returncode
    # Sólo el coordinador que tiene el candado puede señalar a su propio hijo.
    # El tick no necesita conocer ni recuperar un PID.
    with subprocess.Popen(argv, env=entorno, stdout=salida, stderr=salida) as lanzador:
        while lanzador.poll() is None:
            if pedido.exists():
                try:
                    lanzador.send_signal(signal.SIGTERM)
                    acuse = 'senal-enviada' if lanzador.returncode is None else 'lanzador-terminado'
                except OSError as error:
                    acuse = type(error).__name__
                guardar(registro / ACUSE_CANCELACION, acuse)
                return lanzador.wait()
            time.sleep(SONDEO_CANCELACION_S)
        return lanzador.returncode


def coordinar(registro, descriptor=None):
    with (os.fdopen(descriptor, 'a') if descriptor is not None else
          (registro / 'candado').open('a')) as candado:
        if descriptor is None:
            fcntl.flock(candado, fcntl.LOCK_EX)
        if (registro / 'codigo.json').exists():
            return
        if (registro / CANCELAR).exists():
            guardar(registro / 'codigo.json', 75)
            return
        entorno_guardado = registro / 'entorno.json'
        variables = json.loads(entorno_guardado.read_text()) if entorno_guardado.exists() else {}
        iniciado = registro / 'iniciado.json'
        if iniciado.exists():
            clave = clave_iniciada(json.loads(iniciado.read_text()))
            # Los registros anteriores no acreditan deduplicación del lanzador.
            if clave is None:
                return
        else:
            clave = variables.get('SEN_RUN_ID') or clave_registro(registro)
            guardar(iniciado, {'clave': clave})
        argv = json.loads((registro / 'solicitud.json').read_text())
        entorno = dict(os.environ, REMOTE_RUN_CLAVE=clave)
        for nombre, valor in variables.items():
            if valor is None:
                entorno.pop(nombre, None)
            else:
                entorno[nombre] = valor
        try:
            with (registro / 'coordinador.log').open('a') as salida:
                for _ in range(INTENTOS_TRANSPORTE):
                    codigo = ejecutar(registro, argv, entorno, salida)
                    if codigo not in SALIDAS_TRANSPORTE or (registro / CANCELAR).exists():
                        break
        except (OSError, subprocess.SubprocessError):
            codigo = None
        guardar(registro / 'codigo.json', codigo)


if __name__ == '__main__':
    coordinar(Path(sys.argv[-1]), int(sys.argv[1]) if len(sys.argv) == 3 else None)
