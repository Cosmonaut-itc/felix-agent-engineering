"""Espera entre ticks de prueba que consumen resultados de Jale."""
from contextlib import contextmanager
from contextvars import ContextVar
import time
from unittest.mock import patch

import adaptadores
from adaptadores.jale import Jale
from operador import lanzamiento_activo


_candados = ContextVar('candados_de_tick', default='**/candado')


@contextmanager
def coordinadores_pendientes(*, esperar=None):
    """El caso libera o cancela coordinadores por su cuenta.

    Si se indica un patrón, esos candados sí se esperan entre ticks.
    """
    token = _candados.set(esperar)
    try:
        yield
    finally:
        _candados.reset(token)


def ejecutar_tick(ejecutar, *args, adaptador=None, **kwargs):
    """Devuelve el resultado del tick después de que Jale suelte sus candados."""
    if adaptador is None:
        seleccionar = adaptadores.seleccionar

        def recordar(config, git):
            nonlocal adaptador
            adaptador = seleccionar(config, git)
            return adaptador

        with patch.object(adaptadores, 'seleccionar', recordar):
            resultado = ejecutar(*args, **kwargs)
    else:
        resultado = ejecutar(*args, adaptador=adaptador, **kwargs)

    patron = _candados.get()
    if patron is not None and isinstance(adaptador, Jale):
        esperar_coordinadores(adaptador.estado, patron)
    return resultado


def esperar_coordinadores(raiz, patron='**/candado'):
    limite = time.monotonic() + 60
    while True:
        candado = next((c for c in raiz.glob(patron) if lanzamiento_activo(c)), None)
        if candado is None:
            return
        if time.monotonic() >= limite:
            raise AssertionError(f'El coordinador de Jale sigue activo: {candado}')
        time.sleep(.025)
