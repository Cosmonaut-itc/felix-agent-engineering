"""Proyección pura de filas requests/events; ninguna lectura de red o reloj."""
from dataclasses import replace
from datetime import datetime
import json
import math

from dominio import Admitido, Entrada, Motivo, Rechazado, Retenido, desde_datos
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


def medir(requests, events, repo, desde, hasta):
    if not all(math.isfinite(t) for t in (desde, hasta)) or desde >= hasta:
        raise ValueError("ventana inválida")
    entregas = {}
    for row in requests:
        if row["repo"] == repo and desde <= row["recibida"] < hasta:
            entrega = desde_datos(json.loads(row["payload"]))
            entregas[entrega.recibo] = Entrada(entrega, row["recibida"])
    iniciales, fuentes, admitidas = {}, {}, {}
    for row in sorted(events, key=lambda r: r["seq"]):
        if row["repo"] != repo or row["instante"] >= hasta:
            continue
        payload = json.loads(row["payload"])
        if row["tipo"] == "admision-entrega":
            entradas = (desde_datos(payload),)
        elif row["tipo"] in ("sombra", "sombra-doble-camino"):
            entradas = desde_datos(payload["estado"]).entradas
        else:
            continue
        for entrada in entradas:
            recibo = entrada.entrega.recibo
            if recibo not in entregas:
                continue
            if row["tipo"] == "admision-entrega":
                iniciales[recibo] = entrada.admision
                fuentes[recibo] = "entrega"
            elif recibo not in iniciales:
                # U1 no observaba al entregar: no inventar esa admisión.
                iniciales[recibo] = Retenido(Motivo.CUSTODIA)
                fuentes[recibo] = "sin-observacion-al-entregar"
            if isinstance(entrada.admision, Admitido) and recibo not in admitidas:
                admitidas[recibo] = replace(entrada, admitida=(
                    entrada.admitida if entrada.admitida is not None else row["instante"]))
            if recibo in admitidas and isinstance(entrada.admision, Rechazado):
                anterior = admitidas[recibo]
                if anterior.caducada is None:
                    admitidas[recibo] = replace(anterior, caducada=(
                        entrada.caducada if entrada.caducada is not None else row["instante"]))
    lotes, pendientes = lotes_fifo(admitidas.values(), hasta)
    detalle = []
    for recibo, entrada in sorted(entregas.items(), key=lambda item: (
            item[1].recibida, item[0])):
        detalle.append({"recibo": recibo, "pr": entrada.entrega.pr,
                        "head": entrada.entrega.head, "recibida": entrada.recibida,
                        "admision": decision_json(iniciales.get(recibo, entrada.admision)),
                        "fuente": fuentes.get(recibo, "sin-observacion-al-entregar"),
                        "admitida": admitidas[recibo].admitida if recibo in admitidas else None})
    # Las admisiones históricas siguen en detalle; sólo muestras que llegaron
    # a un lote o permanecen en fila pueden producir ahorro de suites.
    muestras = sum(len(es) for _, _, es in lotes) + len(pendientes)
    return {"repo": repo, "modo": "sombra", "desde": desde, "hasta": hasta,
            "entregas": detalle, "admitidas": len(admitidas),
            "lotes": [{"instante": t, "motivo": motivo,
                       "recibos": [e.entrega.recibo for e in miembros],
                       "prs": [e.entrega.pr for e in miembros], "tamano": len(miembros)}
                      for t, motivo, miembros in lotes],
            "distribucion": {str(n): sum(len(es) == n for _, _, es in lotes)
                             for n in (1, 2, 3)},
            "pendientes": [e.entrega.recibo for e in pendientes],
            "suites_ahorradas_estimadas": muestras - len(lotes)}
