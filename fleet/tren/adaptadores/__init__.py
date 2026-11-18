"""Selección desde la configuración privada de cada repositorio."""
from pathlib import Path

from adaptadores.hasito import Hasito
from operador import checkout_configurado


def seleccionar(config, git):
    nombre = config.get('adaptador', 'hasito')
    if nombre not in ('hasito', 'jale'):
        raise ValueError('adaptador desconocido')
    lanzador = config.get('lanzador', config.get('lanzador_hasito') if nombre == 'hasito' else None)
    if lanzador is None and nombre == 'hasito':
        return None
    if not isinstance(lanzador, str) or not Path(lanzador).is_absolute():
        raise ValueError('lanzador debe ser ruta absoluta')
    resolver = checkout_configurado(config) is None
    if nombre == 'hasito':
        return Hasito(lanzador, git, resolver_lanzador=resolver)
    from adaptadores.jale import Jale, MODOS
    return Jale(lanzador, git, host=config.get('host', 'auto'), modos=config.get('modos', MODOS),
                resolver_lanzador=resolver)
