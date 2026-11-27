"""Ejecutor del outbox activo, repetible sobre la misma instantánea.

La observación devuelta conserva las vistas de admisión del llamador. Publicar
la rama solicita CI, pero nunca declara CI verde: eso requiere otra lectura.
"""
from dataclasses import dataclass, replace
import subprocess
from typing import Optional

from dominio import (Admitido, Armado, Candidato, ConflictoCadena, Congelado,
                     Corriendo, Diagnosticando, Efecto, EnBase, EsperandoCI, Incierto,
                     BloqueoPrevia, EsperaPrevia,
                     Observacion, Promoviendo, RamaOcupada, admision_entrada,
                     clave, efecto_diagnostico, efecto_promover, efecto_reanudar_suite,
                     intento_suite, rama_ci, recibo_ci)
from atribucion import planificar
from git_ops import FalloGit, Publicado
from adaptadores.hasito import ResultadoInvalido
from adaptadores.jale import EN_CURSO
from operador import FalloSnapshot


class EfectoFallido(Exception):
    def __init__(self, resultado):
        super().__init__(type(resultado).__name__)
        self.resultado = resultado


@dataclass(frozen=True)
class Ejecucion:
    observacion: Observacion
    completo: bool
    aviso: Optional[dict] = None
    en_curso: bool = False


def lote_admitido(estado, tren, observacion):
    vistas = {v.recibo or str(v.pr): v for v in observacion.prs}
    entradas = {e.entrega.recibo: e for e in estado.entradas}
    return all(isinstance(admision_entrada(entradas[r], vistas, observacion), Admitido)
               for r in tren.recibos)


def suite_autorizada(estado, tren, observacion):
    """iniciar-suite sólo lanza con el lote admitido y el verde de la solicitud vigente."""
    return (lote_admitido(estado, tren, observacion) and observacion.ci_candidato == "verde" and
            (not tren.fase.solicitud or (observacion.tren_id == tren.id and
                                         observacion.ci_recibo == recibo_ci(tren))))


def aviso_prelanzamiento(tren, exc):
    clase = type(exc).__name__
    motivo = (exc.resultado.codigo if isinstance(exc, ResultadoInvalido) else
              str(exc) if isinstance(exc, FalloSnapshot) else clase)
    return {"tren": tren, "motivo": motivo, "clase": clase}


def prelanzar_ci(adaptador, tren, observacion):
    prelanzar = getattr(adaptador, "prelanzar", None)
    fase = tren.fase
    # El rojo de una solicitud anterior no cuenta al publicar otra.
    rojo = (isinstance(fase, EsperandoCI) and observacion.ci_candidato == "rojo" and
            observacion.tren_id == tren.id and observacion.ci_recibo == recibo_ci(tren))
    if prelanzar is not None and not rojo:
        try:
            prelanzar(fase.candidato, tren.id)
        except (ResultadoInvalido, FalloSnapshot, OSError, subprocess.SubprocessError) as exc:
            return aviso_prelanzamiento(tren.id, exc)


def pedidos_adelantados(fase, git, adaptador, tren):
    c = fase.candidato
    prs = tuple(pr for pr, _, _ in c.eslabones)
    plan = planificar(fase.fallos, prs, fase.evidencias,
                      fase.presupuesto_s - fase.costo_infra_s, en_paralelo=True)
    demas = [(s, adaptador.adelanto(c, tren, s)) for s in plan.tanda
             if s.id != fase.pendiente.id]
    cupo = adaptador.repeticiones_paralelas - 1 - sum(a == "vivo" for _, a in demas)
    for solicitud, adelanto in demas:
        if cupo <= 0:
            return
        if adelanto:
            continue
        commit = c.C if solicitud.objetivo == "C" else c.B
        if solicitud.objetivo == "subconjunto":
            miembros = tuple((pr, head) for pr, head, _ in c.eslabones if pr in solicitud.prs)
            subconjunto = git.armar(c.B, miembros)
            if not isinstance(subconjunto, Candidato):
                continue
            commit = subconjunto.C
        cupo -= 1
        yield solicitud, commit


def ejecutar(efecto, estado, observacion, git, adaptador=None, previo=None):
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
        admitidos = lote_admitido(estado, tren, observacion)
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
        if adaptador is None:
            if not admitidos:
                return Ejecucion(limpia, False)
            raise ValueError("falta adaptador de suite")
        try:
            opciones = {"permitir_lanzar": admitidos}
            if isinstance(fase, (Corriendo, Diagnosticando)):
                if fase.reenganches:
                    opciones["reenganche"] = (fase.reenganches, fase.registro_reenganche)
                if fase.registro_absuelto:
                    opciones["registro_absuelto"] = fase.registro_absuelto
            if efecto.tipo == "iniciar-suite" and isinstance(fase, EsperandoCI):
                if efecto != Efecto(clave("suite", tren.id), "iniciar-suite", (tren.id,)):
                    raise ValueError("suite obsoleta")
                recibo = adaptador.suite(fase.candidato, tren.id,
                    permitir_lanzar=suite_autorizada(estado, tren, observacion))
                if recibo is EN_CURSO:
                    return Ejecucion(limpia, False, en_curso=True)
                if recibo is None:
                    return Ejecucion(limpia, False)
                ruta, resultado = recibo
                # Consumir la suite requiere haber acreditado CI verde sobre C.
                # Al recuperar se conserva esa evidencia.
                return Ejecucion(replace(limpia, suite=ruta, resultado_suite=resultado,
                    ci_candidato="verde", ci_recibo=recibo_ci(tren) if fase.solicitud else
                    observacion.ci_recibo if observacion.ci_candidato == "verde"
                    else recibo_ci(tren)), True)
            if efecto.tipo == "reanudar-suite" and isinstance(fase, Corriendo):
                esperado = efecto_reanudar_suite(tren, fase)
                if efecto != esperado or fase.esperar_hasta is not None:
                    raise ValueError("reanudación obsoleta")
                recibo = adaptador.suite(fase.candidato, tren.id,
                    intento_suite(fase), fase.reanudar, **opciones)
                if recibo is EN_CURSO:
                    return Ejecucion(limpia, False, en_curso=True)
                if recibo is None:
                    return Ejecucion(limpia, False)
                ruta, resultado = recibo
                return Ejecucion(replace(limpia, suite=ruta, resultado_suite=resultado,
                                         suite_intento=intento_suite(fase)), True)
            if efecto.tipo == "repetir" and isinstance(fase, Diagnosticando):
                if (fase.pendiente is None or fase.esperar_hasta is not None or
                        efecto != efecto_diagnostico(tren, fase)):
                    raise ValueError("repetición obsoleta")
                s = fase.pendiente
                commit = (fase.candidato.C if s.objetivo == "C" else fase.candidato.B
                          if s.objetivo == "B" else fase.subconjunto.C)
                aviso = None
                adelantar = getattr(adaptador, "adelantar_repeticiones", None)
                if (admitidos and not estado.pausa and adelantar is not None and
                        getattr(adaptador, "repeticiones_paralelas", 1) > 1):
                    try:
                        pedidos = tuple(pedidos_adelantados(fase, git, adaptador, tren.id))
                        adelantar(fase.candidato, fase.suite, pedidos, tren.id)
                    except (ResultadoInvalido, FalloSnapshot, FalloGit, OSError,
                            subprocess.SubprocessError) as exc:
                        aviso = aviso_prelanzamiento(tren.id, exc)
                resultado = adaptador.repetir(fase.candidato, fase.suite, s, commit,
                                          tren.id, intento_suite(fase), fase.reanudar,
                                          **opciones)
                if resultado is EN_CURSO:
                    return Ejecucion(limpia, False, aviso, en_curso=True)
                if resultado is None:
                    return Ejecucion(limpia, False, aviso)
                return Ejecucion(replace(limpia, repeticion=resultado, reproduccion_id=s.id,
                                         suite_intento=intento_suite(fase)), True, aviso)
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
        aviso = prelanzar_ci(adaptador, tren, observacion)
        return Ejecucion(replace(limpia, ci_candidato="pendiente", suite=None), True, aviso)
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
            if previo is not None and previo.necesario(tren, observacion):
                if isinstance(lectura, Incierto):
                    if (previo.operador is not None and previo.operador.checkout is not None
                            and "pre_aterrizaje" in previo.config):
                        raise FalloSnapshot("main-no-disponible")
                    lectura = BloqueoPrevia(cert.B, cert.C, "base-incierta")
                else:
                    lectura = git.promover(cert.B, cert.C, cert.arbol, heads,
                        antes_push=lambda: previo.verificar(estado, tren, observacion, efecto))
            else:
                lectura = git.promover(cert.B, cert.C, cert.arbol, heads)
        if previo is not None:
            lectura = previo.resolver(estado, tren, lectura)
        return Ejecucion(replace(limpia, main=lectura, intento_promocion=fase.intentos),
                         not isinstance(lectura, (Incierto, EsperaPrevia)))
    raise ValueError("efecto o fase no soportados")
