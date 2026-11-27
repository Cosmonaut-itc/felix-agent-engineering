"""Reconstrucción de la latencia a partir de snapshots operativos del ledger."""
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
import json
import math
from typing import Tuple

from dominio import Motivo
from medicion import instante_iso


CUSTODIA = ("Retenido", Motivo.CUSTODIA)
RETENIDOS_SESION = {Motivo.CONFLICTO, Motivo.APTO, Motivo.HALLAZGOS, Motivo.DIRIGIDA,
                    Motivo.CI, Motivo.VERIFICADO}
VERDES = {"ATERRIZADO", "EN CURSO"}
MINUTO = 60
HORA = 3600


class Historial:
    def __init__(self):
        self.instantes = []
        self.valores = []

    def guardar(self, instante, valor):
        if not self.valores or self.valores[-1] != valor:
            self.instantes.append(instante)
            self.valores.append(valor)

    def en(self, instante, defecto=None):
        i = bisect_right(self.instantes, instante)
        return self.valores[i - 1] if i else defecto


@dataclass
class Lote:
    id: str
    inicio: float
    fin: float
    miembros: Tuple[int, ...]
    resultado: str = "EN CURSO"


def estado_admision(entrada):
    if entrada.get("terminal") is not None:
        return "terminal", None
    if entrada.get("caducada") is not None:
        return "caducada", None
    admision = entrada["admision"]
    return admision["_tipo"], admision.get("motivo")


def firma_pausa(pausa):
    if pausa is not None:
        return (pausa["motivo"], pausa.get("detalle") or "", pausa.get("pr"), tipo_pausa(pausa))


def proyectar(events, repo, hasta, pr_de, recibo_de):
    admisiones = defaultdict(Historial)
    lotes = {}
    pausas = Historial()
    levantadas = []
    for fila in events:
        instante = fila["instante"]
        if fila["repo"] != repo or fila["tipo"] != "operativo" or instante >= hasta:
            continue
        estado = json.loads(fila["payload"])["estado"]
        pausa = estado.get("pausa")
        anterior = pausas.valores[-1] if pausas.valores else None
        if firma_pausa(pausa) != firma_pausa(anterior):
            if anterior is not None:
                levantadas.append((pausas.instantes[-1], instante, anterior))
            pausas.guardar(instante, pausa)
        for entrada in estado["entradas"]:
            entrega = entrada["entrega"]
            recibo = recibo_de.get((entrega["pr"], entrega["head"]))
            if recibo is not None:
                admisiones[recibo].guardar(instante, estado_admision(entrada))
        for tren in estado["trenes"]:
            ident = tren["id"]
            lote = lotes.get(ident)
            if lote is None:
                lote = lotes[ident] = Lote(ident, instante, hasta, ())
            fase = tren["fase"]
            if lote.resultado == "EN CURSO":
                lote.miembros = tuple(pr_de[r] for r in tren["recibos"] if r in pr_de)
                if fase["_tipo"] == "Terminal":
                    lote.fin = instante
                    lote.resultado = fase["resultado"]
    return admisiones, tuple(lotes.values()), pausas, levantadas


def tipo_pausa(pausa):
    motivo = pausa["motivo"]
    detalle = pausa.get("detalle") or ""
    subtipo = detalle.partition(":")[0]
    if motivo == "manual":
        return "pausa manual"
    if detalle == "salida-sin-recibo" or any(
            "coordinador.log" in ruta for ruta in pausa.get("archivos") or ()):
        return "infra: salida sin recibo del lanzador"
    if detalle.startswith("pre-aterrizaje"):
        return "ventana de alto riesgo (gancho previo)"
    if detalle.startswith("confirmacion-"):
        return "juicio: prueba roja que no se confirma (intermitente)"
    if motivo == "necesita-juicio":
        return "juicio: " + subtipo
    return f"{motivo}: {subtipo}" if subtipo else motivo


def carril_por_instante(lotes):
    cambios = defaultdict(list)
    for orden, lote in enumerate(lotes):
        if lote.inicio < lote.fin:
            cambios[lote.inicio].append((orden, lote))
            cambios[lote.fin].append((orden, None))
    historial = Historial()
    activos = {}
    for instante, cambio in sorted(cambios.items()):
        for orden, lote in cambio:
            if lote is None:
                del activos[orden]
            else:
                activos[orden] = lote
        historial.guardar(instante, tuple(activos[i] for i in sorted(activos)))
    return historial


def causa_carril(pausa, activos):
    if pausa is not None:
        return tipo_pausa(pausa)
    if any(lote.resultado in VERDES for lote in activos):
        return "lote verde (trabajo útil)"
    if activos:
        return "lote fallido: " + activos[0].resultado
    return "carril libre"


def causa_por_minuto(recibos, admisiones, merges, pausas, carril, desde, hasta):
    horas = defaultdict(lambda: {"horas_pr": 0.0, "horas_carril": 0.0})
    instante = desde
    while instante < hasta:
        peso = min(MINUTO, hasta - instante) / HORA
        causa = causa_carril(pausas.en(instante), carril.en(instante, ()))
        horas[causa]["horas_carril"] += peso
        esperando = 0
        for pr, entregas in recibos.items():
            recibo = entregas.en(instante)
            if recibo is None or (pr in merges and merges[pr] <= instante):
                continue
            tipo, motivo = admisiones[recibo].en(instante, CUSTODIA)
            if tipo == "Admitido":
                esperando += 1
            elif tipo == "Retenido" and motivo != Motivo.CUSTODIA:
                horas["retención de admisión: " + motivo]["horas_pr"] += peso
        horas[causa]["horas_pr"] += esperando * peso
        instante += MINUTO
    return dict(horas)


def devoluciones(effects, repo, pr_de):
    responsables = defaultdict(set)
    for fila in effects:
        if fila["repo"] != repo:
            continue
        efecto = json.loads(fila["payload"])
        if efecto["tipo"] == "devolver":
            lote, recibo = efecto["datos"][:2]
            if recibo in pr_de:
                responsables[lote].add(pr_de[recibo])
    return responsables


def responsabilidad(pr, admision, pausa, activos, devueltos):
    tipo, motivo = admision
    if tipo in ("Rechazado", "terminal", "caducada") or (
            tipo == "Retenido" and motivo in RETENIDOS_SESION):
        return "sesion"
    if tipo == "Retenido":
        return "tren"
    if pausa is not None and pausa.get("pr") is not None:
        return "sesion" if pr == pausa["pr"] else "otras"
    if pausa is not None:
        culpables = next((devueltos[lote.id] for lote in activos
                          if lote.resultado == "DEVUELTO" and devueltos.get(lote.id)), ())
    elif activos:
        lote = next((lote for lote in activos if pr in lote.miembros), activos[0])
        culpables = devueltos.get(lote.id, ()) if lote.resultado == "DEVUELTO" else ()
    else:
        culpables = ()
    if culpables:
        return "sesion" if pr in culpables else "otras"
    return "tren"


def horas_por_pr(recibos, admisiones, merges, pausas, carril, devueltos, desde, hasta):
    filas = []
    for pr, merge in sorted(merges.items()):
        if pr not in recibos or not desde <= merge < hasta:
            continue
        entregas = recibos[pr]
        primera = entregas.instantes[0]
        if primera > merge:
            continue
        fila = {"pr": pr, "total": (merge - primera) / HORA,
                "tren": 0.0, "sesion": 0.0, "otras": 0.0}
        instante = primera
        while instante < merge:
            recibo = entregas.en(instante)
            grupo = responsabilidad(pr, admisiones[recibo].en(instante, CUSTODIA),
                                    pausas.en(instante), carril.en(instante, ()), devueltos)
            fila[grupo] += min(MINUTO, merge - instante) / HORA
            instante += MINUTO
        filas.append(fila)
    return filas


def percentil(ordenados, proporcion):
    if not ordenados:
        return None
    posicion = (len(ordenados) - 1) * proporcion
    inferior, superior = ordenados[math.floor(posicion)], ordenados[math.ceil(posicion)]
    return inferior + (superior - inferior) * (posicion % 1)


def latencia_decisiones(levantadas, desde):
    minutos = defaultdict(list)
    for inicio, fin, pausa in levantadas:
        if fin >= desde and pausa["motivo"] in ("necesita-juicio", "infraestructura"):
            minutos[tipo_pausa(pausa)].append((fin - inicio) / MINUTO)
    return {tipo: {"n": len(valores), "p50_min": percentil(sorted(valores), 0.5),
                   "max_min": max(valores)} for tipo, valores in minutos.items()}


def medir_latencia(requests, events, effects, repo, desde, hasta, prs=None):
    """Proyecta filas de un snapshot de lectura, con events en orden de seq."""
    if not all(math.isfinite(t) for t in (desde, hasta)) or desde >= hasta:
        raise ValueError("ventana inválida")
    recibos = defaultdict(Historial)
    pr_de = {}
    recibo_de = {}
    for fila in sorted(requests, key=lambda fila: (fila["recibida"], fila["recibo"])):
        if fila["repo"] == repo and fila["recibida"] < hasta:
            recibo, pr = fila["recibo"], fila["pr"]
            pr_de[recibo] = pr
            recibo_de[pr, fila["head"]] = recibo
            recibos[pr].guardar(fila["recibida"], recibo)
    admisiones, lotes, pausas, levantadas = proyectar(events, repo, hasta, pr_de, recibo_de)
    aterrizados = {}
    for lote in lotes:
        if lote.resultado == "ATERRIZADO":
            for pr in lote.miembros:
                aterrizados[pr] = min(aterrizados.get(pr, lote.fin), lote.fin)
    merges = aterrizados if prs is None else {
        pr["number"]: instante_iso(pr["mergedAt"]) for pr in prs
        if pr["state"] == "MERGED" and pr["mergedAt"]}
    # Un PR que el ledger da por aterrizado deja de esperar aunque --prs no lo traiga.
    carril = carril_por_instante(lotes)
    causas = causa_por_minuto(recibos, admisiones, {**aterrizados, **merges},
                              pausas, carril, desde, hasta)
    devueltos = devoluciones(effects, repo, pr_de)
    por_pr = horas_por_pr(recibos, admisiones, merges, pausas, carril, devueltos, desde, hasta)
    horas_tren = sorted(fila["tren"] for fila in por_pr)
    return {"repo": repo, "desde": desde, "hasta": hasta,
            "horas_pr_por_causa": causas, "por_pr": por_pr,
            "resumen": {"n": len(horas_tren),
                        "media": math.fsum(horas_tren) / len(horas_tren) if horas_tren else None,
                        "p50": percentil(horas_tren, 0.5), "p90": percentil(horas_tren, 0.9)},
            "latencia_decisiones": latencia_decisiones(levantadas, desde)}
