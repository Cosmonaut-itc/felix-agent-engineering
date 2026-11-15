"""D5 puro: el historial determina el próximo ensayo o los conjuntos mínimos."""
from dataclasses import dataclass, replace
from itertools import combinations
from typing import Optional, Tuple

from dominio import Causa, Reproduccion, clave


@dataclass(frozen=True)
class Plan:
    siguiente: Optional[Reproduccion] = None
    causas: Tuple[Causa, ...] = ()
    terminado: bool = False
    incierto: str = ""


def identidad(fallo):
    return (fallo.parte, fallo.check, fallo.unidad)


def unidad(resultado, fallo):
    encontradas = [u for u in resultado.unidades
                   if (u.parte, u.check, u.unidad) == identidad(fallo)]
    return encontradas[0] if len(encontradas) == 1 else None


def estable(u, firma=None):
    """Sólo dos testigos positivos. Ausente/omitida no demuestran que pase."""
    if u is None or len(u.intentos) != 2 or {i.n for i in u.intentos} != {1, 2}:
        return "incierto"
    if all(i.veredicto == "pasa" for i in u.intentos):
        return "pasa"
    firmas = {i.firma for i in u.intentos}
    if (all(i.veredicto == "falla" for i in u.intentos) and len(firmas) == 1
            and next(iter(firmas)) and (firma is None or firmas == {firma})):
        return "falla"
    return "incierto"


def planificar(fallos, prs, evidencias, presupuesto_s, archivos=()):
    """Una repetición = dos intentos de una unidad; N <= 3.

    Costo: 2 * máximo(segundos del check de origen, duraciones observadas).
    El check entero es una cota conservadora cuando falta tiempo por test.
    Archivos sólo prioriza tickets: (PR, rutas). Nunca es evidencia causal.
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
        if estable(uc, fallo.firma or None) != "falla":
            return Plan(incierto="confirmacion-candidato:" + fallo.unidad)
        # La firma puede estar vacía en el origen anterior a U3: las dos
        # observaciones nuevas deben tener una firma estable y no vacía.
        firma = uc.intentos[0].firma
        if ub is None or ub.definicion != uc.definicion:
            return Plan(incierto="definicion-distinta:" + fallo.unidad)
        if estable(ub, firma) == "falla":
            continue
        if estable(ub) != "pasa":
            return Plan(incierto="confirmacion-base:" + fallo.unidad)
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
    return Plan(causas=tuple(causas), terminado=True)
