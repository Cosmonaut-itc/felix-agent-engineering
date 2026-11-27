"""Sonda de admisión para elegir la espera de Boat antes del lanzamiento."""
import re
import subprocess


GRUPOS_DESK = ('pesadas', 'jale')
COMANDO = 'NO_COLOR=1 ~/.local/bin/pueue group && cat /proc/loadavg && nproc'


def sondear(host, grupos, ejecutar):
    salud = dict(sano=False, carga_1m=None, nproc=None)
    try:
        salida = ejecutar(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
                           host, COMANDO], timeout=6, text=True,
                          stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return dict(salud, motivo='plazo-agotado')
    except (OSError, subprocess.SubprocessError):
        return dict(salud, motivo='sonda-fallida')
    except UnicodeError:
        return dict(salud, motivo='salida-ilegible')

    lineas = salida.strip().splitlines()
    if len(lineas) < 3 or not re.fullmatch(r'[1-9][0-9]*', lineas[-1]):
        return dict(salud, motivo='salida-ilegible')
    carga = re.fullmatch(r'([0-9]+\.[0-9]+) [0-9]+\.[0-9]+ [0-9]+\.[0-9]+ \d+/\d+ \d+',
                         lineas[-2])
    if carga is None:
        return dict(salud, motivo='salida-ilegible')
    estados = {}
    for linea in lineas[:-2]:
        grupo = re.fullmatch(r'Group "([^"]+)" \(\d+ parallel\): (running|paused)', linea.strip())
        if grupo is not None and grupo[1] in grupos:
            estados[grupo[1]] = grupo[2]
    salud.update(carga_1m=float(carga[1]), nproc=int(lineas[-1]), grupos=estados)
    if len(estados) != len(grupos):
        return dict(salud, motivo='grupo-ilegible')
    if 'paused' in estados.values():
        return dict(salud, motivo='grupo-en-pausa')
    if salud['carga_1m'] > salud['nproc']:
        return dict(salud, motivo='carga-alta')
    return dict(salud, sano=True, motivo='desk-sano')
