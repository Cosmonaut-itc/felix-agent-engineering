"""Coordinador desacoplado: reenganche por clave sobre un registro durable POSIX."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


INTENTOS_TRANSPORTE = 3


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


def coordinar(registro):
    with (registro / 'candado').open('a') as candado:
        fcntl.flock(candado, fcntl.LOCK_EX)
        if (registro / 'codigo.json').exists():
            return
        iniciado = registro / 'iniciado.json'
        if iniciado.exists():
            anterior = json.loads(iniciado.read_text())
            clave = anterior.get('clave') if isinstance(anterior, dict) else None
            # Los registros anteriores no acreditan deduplicación del lanzador.
            if not isinstance(clave, str) or not re.fullmatch(r'[A-Za-z0-9._-]{8,128}', clave):
                return
        else:
            clave = 'pao-jale-' + hashlib.sha256(os.fsencode(registro.resolve())).hexdigest()
            guardar(iniciado, {'clave': clave})
        argv = json.loads((registro / 'solicitud.json').read_text())
        entorno = dict(os.environ, REMOTE_RUN_CLAVE=clave)
        try:
            with (registro / 'coordinador.log').open('a') as salida:
                for _ in range(INTENTOS_TRANSPORTE):
                    codigo = subprocess.run(argv, env=entorno, stdout=salida,
                                            stderr=salida, check=False).returncode
                    if codigo != 255:
                        break
        except (OSError, subprocess.SubprocessError):
            codigo = None
        guardar(registro / 'codigo.json', codigo)


if __name__ == '__main__':
    coordinar(Path(sys.argv[1]))
