"""Coordinador desacoplado: una sola ejecución por registro durable POSIX."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys


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
        if (registro / 'codigo.json').exists() or (registro / 'iniciado.json').exists():
            return
        argv = json.loads((registro / 'solicitud.json').read_text())
        # Si muere también este coordinador, no adivinar si hubo lanzamiento.
        guardar(registro / 'iniciado.json', True)
        try:
            with (registro / 'coordinador.log').open('a') as salida:
                codigo = subprocess.run(argv, stdout=salida, stderr=salida, check=False).returncode
        except (OSError, subprocess.SubprocessError):
            codigo = None
        guardar(registro / 'codigo.json', codigo)


if __name__ == '__main__':
    coordinar(Path(sys.argv[1]))
