"""Espera entre ticks de prueba que consumen resultados de Jale."""
import time

from operador import lanzamiento_activo


def esperar_coordinadores(raiz, patron='**/candado'):
    limite = time.monotonic() + 60
    while True:
        candado = next((c for c in raiz.glob(patron) if lanzamiento_activo(c)), None)
        if candado is None:
            return
        if time.monotonic() >= limite:
            raise AssertionError(f'El coordinador de Jale sigue activo: {candado}')
        time.sleep(.025)
