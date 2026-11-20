"""Proyección pura de filas requests/events; ninguna lectura de red o reloj."""
from dataclasses import replace
from datetime import datetime
import json
import math

from dominio import LIMITE_LOTE, Admitido, Entrada, Rechazado, desde_datos, orden_fifo
from sombra import lotes_fifo


def instante_iso(valor):
    fecha = datetime.fromisoformat(valor.replace("Z", "+00:00"))
    if fecha.tzinfo is None:
        raise ValueError("ISO debe incluir zona horaria")
    return fecha.timestamp()


def decision_json(decision):
    return {"estado": type(decision).__name__.lower(),
            "motivo": "admision-d7" if isinstance(decision, Admitido)
            else decision.motivo.value}


def proyectar_sombra(admitidas, inicio, fin):
    entradas = (replace(e, admitida=max(e.admitida, inicio)) for e in admitidas.values())
    lotes, pendientes = lotes_fifo(entradas, math.nextafter(fin, -math.inf))
    return [(t, motivo, miembros, "sombra") for t, motivo, miembros in lotes], pendientes


def medir(requests, events, repo, desde, hasta):
    if not all(math.isfinite(t) for t in (desde, hasta)) or desde >= hasta:
        raise ValueError("ventana inválida")
    entregas = {}
    for row in requests:
        if row["repo"] == repo and desde <= row["recibida"] < hasta:
            entrega = desde_datos(json.loads(row["payload"]))
            entregas[entrega.recibo] = Entrada(entrega, row["recibida"])
    iniciales, admitidas = {}, {}
    modo = None
    inicio_sombra = -math.inf
    cola_sombra = {}
    pendientes = ()
    trenes_vistos, despachados = set(), set()
    lotes = []
    for row in sorted(events, key=lambda r: r["seq"]):
        instante, tipo = row["instante"], row["tipo"]
        if row["repo"] != repo or instante >= hasta:
            continue
        if tipo == "operativo":
            tramo = "activo"
        # Un tick --seco del reductor activo deja un evento sombra legado.
        elif tipo in ("admision-entrega", "sombra-doble-camino") or (
                tipo == "sombra" and modo != "activo"):
            tramo = "sombra"
        else:
            continue
        if modo == "sombra" and tramo == "activo":
            cerrados, _ = proyectar_sombra(cola_sombra, inicio_sombra, instante)
            lotes.extend(cerrados)
            despachados.update(e.entrega.recibo for _, _, miembros, _ in cerrados for e in miembros)
            cola_sombra.clear()
        elif modo == "activo" and tramo == "sombra":
            inicio_sombra = instante
            cola_sombra.update((e.entrega.recibo, e) for e in pendientes)
        modo = tramo
        payload = json.loads(row["payload"])
        if tipo == "admision-entrega":
            entradas = (desde_datos(payload),)
        else:
            estado = desde_datos(payload["estado"])
            entradas = estado.entradas
            if modo == "activo":
                por_recibo = {e.entrega.recibo: e for e in entradas}
                for tren in estado.trenes:
                    if tren.id in trenes_vistos:
                        continue
                    trenes_vistos.add(tren.id)
                    despachados.update(tren.recibos)
                    miembros = tuple(por_recibo[r] for r in tren.recibos)
                    motivo = "solo" if miembros[0].solo else "disponible"
                    lotes.append((instante, motivo, miembros, "real"))
                pendientes = sorted((e for e in entradas if isinstance(e.admision, Admitido)
                                     and e.tren is None and e.terminal is None), key=orden_fifo)
        for entrada in entradas:
            recibo = entrada.entrega.recibo
            if tipo == "admision-entrega":
                iniciales[recibo] = entrada.admision
            if isinstance(entrada.admision, Admitido):
                admitida = entrada.admitida if entrada.admitida is not None else instante
                if recibo in entregas:
                    admitidas.setdefault(recibo, admitida)
                if modo == "sombra" and recibo not in despachados:
                    cola_sombra.setdefault(recibo, replace(entrada, admitida=admitida))
            if modo == "sombra" and recibo in cola_sombra and isinstance(entrada.admision, Rechazado):
                anterior = cola_sombra[recibo]
                if anterior.caducada is None:
                    cola_sombra[recibo] = replace(anterior, caducada=(
                        entrada.caducada if entrada.caducada is not None else instante))
    if modo == "sombra":
        cerrados, pendientes = proyectar_sombra(cola_sombra, inicio_sombra, hasta)
        lotes.extend(cerrados)
    lotes_cohorte = []
    for instante, motivo, miembros, fuente in lotes:
        recibos = [e.entrega.recibo for e in miembros if e.entrega.recibo in entregas]
        if recibos:
            lotes_cohorte.append({"instante": instante, "motivo": motivo, "fuente": fuente,
                                 "recibos": recibos, "prs": [entregas[r].entrega.pr for r in recibos],
                                 "tamano": len(recibos)})
    pendientes = [e.entrega.recibo for e in pendientes if e.entrega.recibo in entregas]
    detalle = []
    for recibo, entrada in sorted(entregas.items(), key=lambda item: (
            item[1].recibida, item[0])):
        detalle.append({"recibo": recibo, "pr": entrada.entrega.pr,
                        "head": entrada.entrega.head, "recibida": entrada.recibida,
                        "admision": decision_json(iniciales.get(recibo, entrada.admision)),
                        "fuente": ("entrega" if recibo in iniciales
                                   else "sin-observacion-al-entregar"),
                        "admitida": admitidas.get(recibo)})
    # Las admisiones históricas siguen en detalle; sólo muestras que llegaron
    # a un lote o permanecen en fila pueden producir ahorro de suites.
    muestras = sum(lote["tamano"] for lote in lotes_cohorte) + len(pendientes)
    return {"repo": repo, "modo": modo or "sombra", "desde": desde, "hasta": hasta,
            "entregas": detalle, "admitidas": len(admitidas),
            "lotes": lotes_cohorte,
            "distribucion": {str(n): sum(lote["tamano"] == n for lote in lotes_cohorte)
                             for n in range(1, LIMITE_LOTE + 1)},
            "pendientes": pendientes,
            "suites_ahorradas_estimadas": muestras - len(lotes_cohorte)}
