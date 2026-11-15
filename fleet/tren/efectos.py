"""Ejecutor explícito para pruebas/integración futura; el CLI no lo importa.

La observación devuelta conserva las vistas de admisión del llamador. Publicar
la rama solicita CI, pero nunca declara CI verde: eso requiere otra lectura.
"""
from dataclasses import dataclass, replace

from dominio import (Armado, Candidato, ConflictoCadena, Congelado,
                     EsperandoCI, Incierto, Observacion, Promoviendo, RamaOcupada,
                     efecto_promover)
from git_ops import Publicado


class EfectoFallido(Exception):
    def __init__(self, resultado):
        super().__init__(type(resultado).__name__)
        self.resultado = resultado


@dataclass(frozen=True)
class Ejecucion:
    observacion: Observacion
    completo: bool


def ejecutar(efecto, estado, observacion, git):
    """Repetible sobre la misma instantánea; los leases hacen converger el IO.

    Devuelve Ejecucion; sólo completo=True permite cerrar el efecto del outbox.
    Incierto queda pendiente. Otros errores de IO quedan en EfectoFallido.
    """
    if not efecto.datos:
        raise ValueError("efecto sin tren")
    tren = next(t for t in estado.trenes if t.id == efecto.datos[0])
    fase = tren.fase
    limpia = replace(observacion, candidato=None, conflicto_cadena=None, main=None,
                     tren_id=tren.id, intento_promocion=0, rama_ocupada=None)
    if efecto.tipo == "armar-candidato" and isinstance(fase, Congelado):
        entradas = {e.entrega.recibo: e for e in estado.entradas}
        miembros = tuple((entradas[r].entrega.pr, entradas[r].entrega.head)
                         for r in tren.recibos)
        resultado = git.armar(fase.B, miembros)
        if isinstance(resultado, Candidato):
            return Ejecucion(replace(limpia, candidato=resultado), True)
        if isinstance(resultado, ConflictoCadena):
            return Ejecucion(replace(limpia, conflicto_cadena=resultado), True)
        raise EfectoFallido(resultado)
    if efecto.tipo == "solicitar-ci" and isinstance(fase, (Armado, EsperandoCI)):
        resultado = git.publicar(tren.id, fase.candidato.C)
        if isinstance(resultado, RamaOcupada):
            return Ejecucion(replace(limpia, rama_ocupada=resultado,
                                    ci_candidato="pendiente", suite=None), True)
        if not isinstance(resultado, Publicado):
            raise EfectoFallido(resultado)
        return Ejecucion(replace(limpia, ci_candidato="pendiente", suite=None), True)
    if efecto.tipo == "promover" and isinstance(fase, Promoviendo):
        if efecto != efecto_promover(tren.id, fase.intentos):
            raise ValueError("intento de promoción obsoleto")
        cert = fase.certificado
        entradas = {e.entrega.recibo: e for e in estado.entradas}
        heads = tuple(entradas[r].entrega.head for r in tren.recibos)
        lectura = git.promover(cert.B, cert.C, cert.arbol, heads)
        return Ejecucion(replace(limpia, main=lectura, intento_promocion=fase.intentos),
                         not isinstance(lectura, Incierto))
    raise ValueError("efecto o fase no soportados")
