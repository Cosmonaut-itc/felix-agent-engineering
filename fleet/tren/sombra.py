"""Sombra pura: admisión real y lotes hipotéticos, sin ejecutar efectos."""
from dataclasses import replace
from itertools import groupby

from dominio import (Admitido, Estado, Rechazado, Terminal, Tren, admision_entrada,
                     clave, orden_fifo, siguiente_lote)


def lotes_fifo(entradas, hasta):
    """Agrupa las admisiones simultáneas con disponibilidad inmediata entre lotes."""
    llegadas = sorted((e for e in entradas if e.admitida is not None
                       and e.admitida <= hasta
                       and (e.caducada is None or e.caducada > e.admitida)),
                      key=orden_fifo)
    lotes = []
    for instante, grupo in groupby(llegadas, key=lambda e: e.admitida):
        fila = list(grupo)
        while fila:
            miembros = siguiente_lote(fila)
            del fila[:len(miembros)]
            motivo = "solo" if miembros[0].solo else "disponible"
            lotes.append((instante, motivo, miembros))
    return tuple(lotes), ()


def paso_sombra(estado, observacion):
    vistas = {v.recibo or str(v.pr): v for v in observacion.prs}
    entradas = []
    for entrada in estado.entradas:
        if entrada.terminal:
            entradas.append(entrada)
            continue
        vista = vistas.get(entrada.entrega.recibo, vistas.get(str(entrada.entrega.pr)))
        if (vista and vista.mergeado and not vista.abierto and
                vista.head == entrada.entrega.head and vista.base == "main"):
            entrada = replace(entrada, terminal=Terminal("MERGEADO-FUERA"))
        else:
            decision = admision_entrada(entrada, vistas, observacion)
            # Primera admisión histórica: el merge externo no borra la muestra.
            admitida = entrada.admitida
            if isinstance(decision, Admitido) and admitida is None:
                admitida = observacion.ahora
            entrada = replace(entrada, admision=decision, admitida=admitida,
                              caducada=(entrada.caducada if entrada.caducada is not None
                                        else observacion.ahora if isinstance(decision, Rechazado)
                                        else None),
                              solo=entrada.solo or bool(vista and vista.requiere_solo))
        entradas.append(entrada)
    lotes, _ = lotes_fifo(entradas, observacion.ahora)
    trenes, asignados = [], {}
    for instante, _, miembros in lotes:
        recibos = tuple(e.entrega.recibo for e in miembros)
        ident = clave("lote-sombra", estado.repo, instante, recibos)
        trenes.append(Tren(ident, recibos, Terminal("SIMULADO")))
        asignados.update((r, ident) for r in recibos)
    # Una sombra nunca espera candidatos, CI, suite ni promoción. Las pausas
    # operativas (incluida etapa-1) siguen perteneciendo al reductor activo.
    return Estado(estado.repo, tuple(replace(e, tren=asignados.get(e.entrega.recibo))
                                    for e in entradas), tuple(trenes)), ()
