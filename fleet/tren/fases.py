"""Proyección de hitos de cada tren a partir de las filas del ledger."""
from collections import Counter
import json
import math

from medicion import instante_iso


TRAMOS = {
    "armado_s": ("cierre", "ci_solicitado"),
    "ci_s": ("ci_solicitado", "suite_iniciada"),
    "suite_s": ("suite_iniciada", "suite_terminada"),
    "promocion_s": ("suite_terminada", "fin"),
    "total_s": ("cierre", "fin"),
}
HITOS = ("cierre", "ci_solicitado", "suite_iniciada", "suite_terminada",
         "certificado", "fin")
HITOS_EFECTOS = {
    ("efecto-resultado", "solicitar-ci"): "ci_solicitado",
    ("efecto-iniciado", "iniciar-suite"): "suite_iniciada",
}
SUITES = ("iniciar-suite", "reanudar-suite")


def duracion(inicio, fin):
    return fin - inicio if inicio is not None and fin is not None else None


def percentiles(valores):
    valores = sorted(v for v in valores if v is not None)
    if not valores:
        return dict.fromkeys(("p50", "p90", "max"))
    return {"p50": valores[math.ceil(0.5 * len(valores)) - 1],
            "p90": valores[math.ceil(0.9 * len(valores)) - 1],
            "max": valores[-1]}


def medir_fases(events, effects, repo, desde, hasta):
    inicio, fin = instante_iso(desde), instante_iso(hasta)
    if inicio >= fin:
        raise ValueError("ventana inválida")
    efectos = {}
    desviados = set()
    for fila in effects:
        if fila["repo"] != repo:
            continue
        efecto = json.loads(fila["payload"])
        tipo = efecto["tipo"]
        if tipo in ("solicitar-ci", "iniciar-suite", "reanudar-suite"):
            tren = efecto["datos"][0]
            efectos[fila["clave"]] = tipo, tren
            if tipo == "reanudar-suite":
                desviados.add(tren)

    vistos = set()
    trenes = {}
    for fila in sorted(events, key=lambda r: r["seq"]):
        tipo, instante = fila["tipo"], fila["instante"]
        if fila["repo"] != repo:
            continue
        if tipo == "operativo":
            paso = json.loads(fila["payload"])
            estado = paso["estado"]
            # El resultado de una suite lleva el instante del tick que la lanzó: el fin lo marca el paso que lo consume.
            efecto, ident = efectos.get(paso.get("consumido"), (None, None))
            if efecto in SUITES and ident in trenes and trenes[ident]["terminal"] is None:
                trenes[ident]["hitos"]["suite_terminada"] = instante
            for tren in estado["trenes"]:
                ident = tren["id"]
                if ident not in vistos:
                    vistos.add(ident)
                    if inicio <= instante < fin:
                        hitos = dict.fromkeys(HITOS)
                        hitos["cierre"] = instante
                        trenes[ident] = {"id": ident, "miembros": len(tren["recibos"]),
                                         "terminal": None, "hitos": hitos}
                if ident not in trenes or trenes[ident]["terminal"] is not None:
                    continue
                registro = trenes[ident]
                fase = tren["fase"]
                if fase["_tipo"] == "Terminal":
                    registro["terminal"] = fase["resultado"]
                    registro["hitos"]["fin"] = instante
                    continue
                if estado.get("pausa") is not None or fase["_tipo"] == "Diagnosticando":
                    desviados.add(ident)
                if (fase["_tipo"] in ("Certificado", "Promoviendo") and
                        registro["hitos"]["certificado"] is None):
                    registro["hitos"]["certificado"] = instante
        elif tipo in ("efecto-iniciado", "efecto-resultado"):
            clave = json.loads(fila["payload"])["clave"]
            if clave not in efectos:
                continue
            efecto, ident = efectos[clave]
            hito = HITOS_EFECTOS.get((tipo, efecto))
            if hito is not None and ident in trenes:
                hitos = trenes[ident]["hitos"]
                if hitos[hito] is None:
                    hitos[hito] = instante

    verdes = []
    otros = Counter()
    for ident, tren in trenes.items():
        terminal, hitos = tren["terminal"], tren["hitos"]
        tren["verde_directo"] = terminal == "ATERRIZADO" and ident not in desviados
        tren["tramos_s"] = {nombre: duracion(hitos[a], hitos[b])
                            for nombre, (a, b) in TRAMOS.items()}
        if tren["verde_directo"]:
            verdes.append(tren["tramos_s"])
        elif terminal is not None:
            otros[terminal] += 1
    return {"repo": repo, "desde": desde, "hasta": hasta, "trenes": list(trenes.values()),
            "resumen": {"verdes_directos": {"n": len(verdes), "tramos_s": {
                nombre: percentiles(v[nombre] for v in verdes) for nombre in TRAMOS}},
                "otros": dict(otros)}}
