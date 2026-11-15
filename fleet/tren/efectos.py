"""Ejecutor explícito para pruebas/integración futura; el CLI no lo importa.

La observación devuelta conserva las vistas de admisión del llamador. Publicar
la rama solicita CI, pero nunca declara CI verde: eso requiere otra lectura.
"""
from dataclasses import replace

from dominio import (Armado, Candidato, ConflictoCadena, Congelado,
                     EsperandoCI, Promoviendo, efecto_promover)
from git_ops import Publicado


class EfectoFallido(Exception):
    def __init__(self, resultado):
        super().__init__(type(resultado).__name__)
        self.resultado = resultado


def ejecutar(efecto, estado, observacion, git):
    """Repetible sobre la misma instantánea; los leases hacen converger el IO.

    Errores tipados de armado/publicación quedan en EfectoFallido.resultado;
    no producen evidencia positiva ni completan el efecto en el outbox.
    """
    if not efecto.datos:
        raise ValueError("efecto sin tren")
    tren = next(t for t in estado.trenes if t.id == efecto.datos[0])
    fase = tren.fase
    limpia = replace(observacion, candidato=None, conflicto_cadena=None, main=None,
                     tren_id=tren.id, intento_promocion=0)
    if efecto.tipo == "armar-candidato" and isinstance(fase, Congelado):
        entradas = {e.entrega.recibo: e for e in estado.entradas}
        miembros = tuple((entradas[r].entrega.pr, entradas[r].entrega.head)
                         for r in tren.recibos)
        resultado = git.armar(fase.B, miembros)
        if isinstance(resultado, Candidato):
            return replace(limpia, candidato=resultado)
        if isinstance(resultado, ConflictoCadena):
            return replace(limpia, conflicto_cadena=resultado)
        raise EfectoFallido(resultado)
    if efecto.tipo == "solicitar-ci" and isinstance(fase, (Armado, EsperandoCI)):
        resultado = git.publicar(tren.id, fase.candidato.C)
        if not isinstance(resultado, Publicado):
            raise EfectoFallido(resultado)
        return replace(limpia, ci_candidato="pendiente", suite=None)
    if efecto.tipo == "promover" and isinstance(fase, Promoviendo):
        if efecto != efecto_promover(tren.id, fase.intentos):
            raise ValueError("intento de promoción obsoleto")
        cert = fase.certificado
        entradas = {e.entrega.recibo: e for e in estado.entradas}
        heads = tuple(entradas[r].entrega.head for r in tren.recibos)
        lectura = git.promover(cert.B, cert.C, cert.arbol, heads)
        return replace(limpia, main=lectura, intento_promocion=fase.intentos)
    raise ValueError("efecto o fase no soportados")
