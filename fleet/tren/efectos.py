"""Ejecutor explícito para pruebas/integración futura; el CLI no lo importa.

La observación devuelta conserva las vistas de admisión del llamador. Publicar
la rama solicita CI, pero nunca declara CI verde: eso requiere otra lectura.
"""
from dataclasses import dataclass, replace

from dominio import (Admitido, Armado, Candidato, ConflictoCadena, Congelado,
                     Corriendo, Diagnosticando, Efecto, EsperandoCI, Incierto,
                     Observacion, Promoviendo, RamaOcupada, admision_entrada,
                     clave, efecto_diagnostico, efecto_promover)
from git_ops import Publicado
from adaptadores.hasito import ResultadoInvalido


class EfectoFallido(Exception):
    def __init__(self, resultado):
        super().__init__(type(resultado).__name__)
        self.resultado = resultado


@dataclass(frozen=True)
class Ejecucion:
    observacion: Observacion
    completo: bool


def ejecutar(efecto, estado, observacion, git, hasito=None):
    """Repetible sobre la misma instantánea; los leases hacen converger el IO.

    Devuelve Ejecucion; sólo completo=True permite cerrar el efecto del outbox.
    Incierto queda pendiente. Otros errores de IO quedan en EfectoFallido.
    """
    if not efecto.datos:
        raise ValueError("efecto sin tren")
    tren = next(t for t in estado.trenes if t.id == efecto.datos[0])
    fase = tren.fase
    limpia = replace(observacion, candidato=None, conflicto_cadena=None, main=None,
                     tren_id=tren.id, intento_promocion=0, rama_ocupada=None,
                     resultado_suite=None, repeticion=None, reproduccion_id="",
                     suite=None, suite_intento=0)
    if efecto.tipo in ("iniciar-suite", "reanudar-suite", "repetir", "armar-subconjunto"):
        vistas = {v.recibo or str(v.pr): v for v in observacion.prs}
        entradas = {e.entrega.recibo: e for e in estado.entradas}
        if any(not isinstance(admision_entrada(entradas[r], vistas, observacion), Admitido)
               for r in tren.recibos):
            return Ejecucion(limpia, False)
        if efecto.tipo == "armar-subconjunto" and isinstance(fase, Diagnosticando):
            if fase.pendiente is None or efecto != efecto_diagnostico(tren, fase):
                raise ValueError("subconjunto obsoleto")
            miembros = tuple((pr, head) for pr, head, _ in fase.candidato.eslabones
                             if pr in fase.pendiente.prs)
            resultado = git.armar(fase.candidato.B, miembros)
            llegada = replace(limpia, reproduccion_id=fase.pendiente.id)
            if isinstance(resultado, Candidato):
                return Ejecucion(replace(llegada, candidato=resultado), True)
            if isinstance(resultado, ConflictoCadena):
                return Ejecucion(replace(llegada, conflicto_cadena=resultado), True)
            raise EfectoFallido(resultado)
        if hasito is None:
            raise ValueError("falta adaptador Hasito")
        try:
            if efecto.tipo == "iniciar-suite" and isinstance(fase, EsperandoCI):
                if efecto != Efecto(clave("suite", tren.id), "iniciar-suite", (tren.id,)):
                    raise ValueError("suite obsoleta")
                ruta, resultado = hasito.suite(fase.candidato, tren.id)
                return Ejecucion(replace(limpia, suite=ruta, resultado_suite=resultado), True)
            if efecto.tipo == "reanudar-suite" and isinstance(fase, Corriendo):
                esperado = Efecto(clave("reanudar-suite", tren.id, fase.reanudaciones),
                    "reanudar-suite", (tren.id, str(fase.reanudaciones)))
                if efecto != esperado or fase.esperar_hasta is not None or not fase.reanudar:
                    raise ValueError("reanudación obsoleta")
                ruta, resultado = hasito.suite(fase.candidato, tren.id,
                                             fase.reanudaciones, fase.reanudar)
                return Ejecucion(replace(limpia, suite=ruta, resultado_suite=resultado,
                                         suite_intento=fase.reanudaciones), True)
            if efecto.tipo == "repetir" and isinstance(fase, Diagnosticando):
                if fase.pendiente is None or efecto != efecto_diagnostico(tren, fase):
                    raise ValueError("repetición obsoleta")
                s = fase.pendiente
                commit = (fase.candidato.C if s.objetivo == "C" else fase.candidato.B
                          if s.objetivo == "B" else fase.subconjunto.C)
                resultado = hasito.repetir(fase.candidato, fase.suite, s, commit,
                                          tren.id, fase.reanudaciones, fase.reanudar)
                return Ejecucion(replace(limpia, repeticion=resultado, reproduccion_id=s.id,
                                         suite_intento=fase.reanudaciones), True)
        except ResultadoInvalido as exc:
            raise EfectoFallido(exc.resultado) from None
        raise ValueError("efecto de suite incompatible con fase")
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
        vistas = {v.recibo or str(v.pr): v for v in observacion.prs}
        if any(not isinstance(admision_entrada(entradas[r], vistas, observacion), Admitido)
               for r in tren.recibos):
            # Puede haber aterrizado antes de perder el ACK: leer sin empujar.
            lectura = git.reconciliar(cert.B, cert.C, cert.arbol, heads)
            return Ejecucion(replace(limpia, main=lectura,
                                    intento_promocion=fase.intentos),
                             not isinstance(lectura, Incierto))
        lectura = git.promover(cert.B, cert.C, cert.arbol, heads)
        return Ejecucion(replace(limpia, main=lectura, intento_promocion=fase.intentos),
                         not isinstance(lectura, Incierto))
    raise ValueError("efecto o fase no soportados")
