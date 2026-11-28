"""Selección desde la configuración privada de cada repositorio."""
from pathlib import Path

from adaptadores.hasito import Hasito
from ledger import directorio_estado
from operador import checkout_configurado

# Caja habla el contrato de Hasito (suite-resultado@1); sólo cambia su estado.
CONTRATO_HASITO = ('hasito', 'caja')


def seleccionar(config, git):
    nombre = config.get('adaptador', 'hasito')
    if nombre not in CONTRATO_HASITO + ('jale',):
        raise ValueError('adaptador desconocido')
    lanzador = config.get('lanzador', config.get('lanzador_hasito') if nombre == 'hasito' else None)
    if lanzador is None and nombre == 'hasito':
        return None
    if not isinstance(lanzador, str) or not Path(lanzador).is_absolute():
        raise ValueError('lanzador debe ser ruta absoluta')
    resolver = checkout_configurado(config) is None
    if nombre in CONTRATO_HASITO:
        return Hasito(lanzador, git, estado=directorio_estado() / nombre, resolver_lanzador=resolver)
    from adaptadores.jale import ESPERA_BOAT_S, Jale, MODOS
    return Jale(lanzador, git, host=config.get('host', 'auto'), modos=config.get('modos', MODOS),
                resolver_lanzador=resolver, prelanzar=config.get('prelanzar', False),
                boat_esperar_s=config.get('boat_esperar_s', ESPERA_BOAT_S),
                repeticiones_paralelas=config.get('repeticiones_paralelas', 1))
