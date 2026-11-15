"""Ejecutor del outbox activo, repetible sobre la misma instantánea.

La observación devuelta conserva las vistas de admisión del llamador. Publicar
la rama solicita CI, pero nunca declara CI verde: eso requiere otra lectura.
"""
from dataclasses import dataclass, replace

from dominio import (Admitido, Armado, Candidato, ConflictoCadena, Congelado,
                     Corriendo, Diagnosticando, Efecto, EnBase, EsperandoCI, Incierto,
                     Observacion, Promoviendo, RamaOcupada, admision_entrada,
                     clave, efecto_diagnostico, efecto_promover, rama_ci, recibo_ci)
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
        admitidos = all(isinstance(admision_entrada(entradas[r], vistas, observacion), Admitido)
                        for r in tren.recibos)
        if efecto.tipo == "armar-subconjunto" and isinstance(fase, Diagnosticando):
            if fase.pendiente is None or efecto != efecto_diagnostico(tren, fase):
                raise ValueError("subconjunto obsoleto")
            if not admitidos:
                return Ejecucion(limpia, False)
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
            if not admitidos:
                return Ejecucion(limpia, False)
            raise ValueError("falta adaptador Hasito")
        try:
            if efecto.tipo == "iniciar-suite" and isinstance(fase, EsperandoCI):
                if efecto != Efecto(clave("suite", tren.id), "iniciar-suite", (tren.id,)):
                    raise ValueError("suite obsoleta")
                recibo = hasito.suite(fase.candidato, tren.id,
                    permitir_lanzar=admitidos and (not fase.solicitud or
                    (observacion.tren_id == tren.id and observacion.ci_recibo == recibo_ci(tren))) and
                    observacion.ci_candidato == "verde")
                if recibo is None:
                    return Ejecucion(limpia, False)
                ruta, resultado = recibo
                # La clave sólo pudo lanzarse con CI verde sobre este C. Al
                # recuperar no sustituir esa evidencia por el CI de hoy.
                return Ejecucion(replace(limpia, suite=ruta, resultado_suite=resultado,
                    ci_candidato="verde", ci_recibo=recibo_ci(tren) if fase.solicitud else
                    observacion.ci_recibo if observacion.ci_candidato == "verde"
                    else recibo_ci(tren)), True)
            if efecto.tipo == "reanudar-suite" and isinstance(fase, Corriendo):
                esperado = Efecto(clave("reanudar-suite", tren.id, fase.reanudaciones),
                    "reanudar-suite", (tren.id, str(fase.reanudaciones)))
                if efecto != esperado or fase.esperar_hasta is not None or not fase.reanudar:
                    raise ValueError("reanudación obsoleta")
                recibo = hasito.suite(fase.candidato, tren.id,
                    fase.reanudaciones, fase.reanudar, permitir_lanzar=admitidos)
                if recibo is None:
                    return Ejecucion(limpia, False)
                ruta, resultado = recibo
                return Ejecucion(replace(limpia, suite=ruta, resultado_suite=resultado,
                                         suite_intento=fase.reanudaciones), True)
            if efecto.tipo == "repetir" and isinstance(fase, Diagnosticando):
                if (fase.pendiente is None or fase.esperar_hasta is not None or
                        efecto != efecto_diagnostico(tren, fase)):
                    raise ValueError("repetición obsoleta")
                s = fase.pendiente
                commit = (fase.candidato.C if s.objetivo == "C" else fase.candidato.B
                          if s.objetivo == "B" else fase.subconjunto.C)
                resultado = hasito.repetir(fase.candidato, fase.suite, s, commit,
                                          tren.id, fase.reanudaciones, fase.reanudar,
                                          permitir_lanzar=admitidos)
                if resultado is None:
                    return Ejecucion(limpia, False)
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
        resultado = git.publicar(rama_ci(tren), fase.candidato.C)
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
        # Antes de repetir IO, reconciliar: un proceso muerto pudo aterrizar C.
        # Una lectura incierta conserva el contrato U2: intentar el CAS sigue
        # siendo seguro por el lease, y su lectura posterior decide el resultado.
        lectura = git.reconciliar(cert.B, cert.C, cert.arbol, heads)
        if isinstance(lectura, (EnBase, Incierto)):
            lectura = git.promover(cert.B, cert.C, cert.arbol, heads)
        return Ejecucion(replace(limpia, main=lectura, intento_promocion=fase.intentos),
                         not isinstance(lectura, Incierto))
    raise ValueError("efecto o fase no soportados")
