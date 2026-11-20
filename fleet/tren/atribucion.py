"""D5 puro: el historial determina el próximo ensayo o los conjuntos mínimos."""
from dataclasses import dataclass, replace
from itertools import combinations
import re
from typing import Optional, Tuple

from dominio import Causa, Fallo, Reproduccion, clave


@dataclass(frozen=True)
class Absolucion:
    fallo: Fallo
    caso: int
    intentos_C: Tuple[str, ...]
    intentos_B: Tuple[str, ...]


@dataclass(frozen=True)
class Plan:
    siguiente: Optional[Reproduccion] = None
    causas: Tuple[Causa, ...] = ()
    terminado: bool = False
    incierto: str = ""
    absoluciones: Tuple[Absolucion, ...] = ()


def identidad(fallo):
    return (fallo.parte, fallo.check, fallo.unidad)


def unidad(resultado, fallo):
    encontradas = [u for u in resultado.unidades
                   if (u.parte, u.check, u.unidad) == identidad(fallo)]
    return encontradas[0] if len(encontradas) == 1 else None


def intentos_observados(u):
    return (u is not None and len(u.intentos) == 2 and {i.n for i in u.intentos} == {1, 2}
            and all(i.veredicto in ("pasa", "falla") for i in u.intentos))


def estable(u, firma=None):
    """Sólo dos testigos positivos. Ausente/omitida no demuestran que pase."""
    if not intentos_observados(u):
        return "incierto"
    if all(i.veredicto == "pasa" for i in u.intentos):
        return "pasa"
    firmas = {i.firma for i in u.intentos}
    if (all(i.veredicto == "falla" for i in u.intentos) and len(firmas) == 1
            and next(iter(firmas)) and (firma is None or firmas == {firma})):
        return "falla"
    return "incierto"


def absolucion(fallo, caso, uc, ub):
    return Absolucion(fallo, caso,
                      tuple(i.veredicto for i in sorted(uc.intentos, key=lambda i: i.n)),
                      tuple(i.veredicto for i in sorted(ub.intentos, key=lambda i: i.n)))


def planificar(fallos, prs, evidencias, presupuesto_s, archivos=(), *, absolver=False,
               firma_distingue_fallo=False):
    """Una repetición = dos intentos de una unidad; N <= 3.

    Costo: 2 * máximo(segundos del check de origen, duraciones observadas).
    El check entero es una cota conservadora cuando falta tiempo por test.
    Archivos sólo prioriza tickets: (PR, rutas). Nunca es evidencia causal.
    Con absolver, devuelve None si un fallo exige atribución al lote.
    La absolución con B mixto requiere una firma que identifique el fallo.
    """
    prs = tuple(prs)
    if not 1 <= len(prs) <= 3 or len(set(prs)) != len(prs):
        return Plan(incierto="tamano-de-lote-no-soportado")
    gastado = sum(max(e.solicitud.costo_s,
        sum(i.duracion_s for u in e.resultado.unidades for i in u.intentos))
        for e in evidencias)
    if presupuesto_s <= 0:
        return Plan(incierto="sin-costo-de-suite-sana")
    por_archivo = dict(archivos)
    causas = []
    absoluciones = []
    for fallo in sorted(fallos, key=identidad):
        historial = [e for e in evidencias if identidad(e.solicitud.fallo) == identidad(fallo)]
        por_objetivo = {(e.solicitud.objetivo, e.solicitud.prs): e for e in historial}
        duraciones = [fallo.duracion_s] + [i.duracion_s for e in historial
            for u in e.resultado.unidades for i in u.intentos]
        costo = 2 * max(duraciones)
        if costo <= 0:
            return Plan(incierto="sin-duracion:" + fallo.unidad)

        def pedir(objetivo, miembros=()):
            if gastado + costo > presupuesto_s:
                return Plan(incierto="presupuesto-agotado:" + fallo.unidad)
            return Plan(siguiente=Reproduccion(
                clave("reproduccion", identidad(fallo), objetivo, miembros),
                fallo, objetivo, miembros, costo))

        for objetivo in ("C", "B"):
            if (objetivo, ()) not in por_objetivo:
                return pedir(objetivo)
        uc = unidad(por_objetivo[("C", ())].resultado, fallo)
        ub = unidad(por_objetivo[("B", ())].resultado, fallo)
        candidato = estable(uc, fallo.firma or None)
        ruta = fallo.unidad.split("::", 1)[0]
        firma_suite = fallo.firma.strip()
        puede_absolver = (absolver and ruta and fallo.unidad != fallo.check and firma_suite
                          and not re.fullmatch(r"codigo=-?[0-9]+", firma_suite))
        if candidato != "falla":
            if (puede_absolver and candidato == "pasa"
                    and all(i.caso_ejecutado is True for i in uc.intentos)
                    and estable(ub) == "pasa"
                    and ub.definicion == uc.definicion):
                absoluciones.append(absolucion(fallo, 1, uc, ub))
                continue
            return Plan(incierto="confirmacion-candidato:" + fallo.unidad)
        # La firma puede estar vacía en el origen anterior a U3: las dos
        # observaciones nuevas deben tener una firma estable y no vacía.
        firma = uc.intentos[0].firma
        if ub is None or ub.definicion != uc.definicion:
            return Plan(incierto="definicion-distinta:" + fallo.unidad)
        if estable(ub, firma) == "falla":
            continue
        if estable(ub) != "pasa":
            if (puede_absolver and firma_distingue_fallo and intentos_observados(ub)
                    and any(i.veredicto == "falla" and i.firma == firma for i in ub.intentos)):
                absoluciones.append(absolucion(replace(fallo, firma=firma), 2, uc, ub))
                continue
            return Plan(incierto="confirmacion-base:" + fallo.unidad)
        if absolver:
            return None
        orden = sorted(prs, key=lambda pr: (
            not any(ruta in fallo.unidad for ruta in por_archivo.get(pr, ())), prs.index(pr)))
        minimos = []
        for tamano in range(1, len(prs)):
            for grupo in combinations(orden, tamano):
                grupo = tuple(pr for pr in prs if pr in grupo)
                if any(set(m).issubset(grupo) for m in minimos):
                    continue
                e = por_objetivo.get(("subconjunto", grupo))
                if e is None:
                    return pedir("subconjunto", grupo)
                u = unidad(e.resultado, fallo)
                if u is None or u.definicion != uc.definicion:
                    return Plan(incierto="definicion-distinta:" + fallo.unidad)
                valor = estable(u, firma)
                if valor == "incierto":
                    return Plan(incierto="reproduccion-incierta:" + fallo.unidad)
                if valor == "falla":
                    minimos.append(grupo)
            # D5 sólo pide combinaciones cuando ningún ticket solo reproduce.
            if tamano == 1 and minimos:
                break
        if not minimos:
            minimos = [prs]  # C confirmado es exactamente B + todos los tickets.
        causas.append(Causa(replace(fallo, firma=firma), tuple(minimos)))
    return Plan(causas=tuple(causas), terminado=True, absoluciones=tuple(absoluciones))
