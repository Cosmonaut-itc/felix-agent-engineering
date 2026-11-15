"""Sombra pura: admisión real y lotes hipotéticos, sin ejecutar efectos."""
from dataclasses import replace
from itertools import groupby
import math

from dominio import (Admitido, Estado, Rechazado, Terminal, Tren, admision_entrada, clave)


def lotes_fifo(entradas, hasta):
    """Reloj de admisión, sin duración de suite; el borde de 60 min vence primero."""
    llegadas = sorted((e for e in entradas if e.admitida is not None and
                       e.admitida <= hasta),
                      key=lambda e: (e.admitida, e.recibida, e.entrega.recibo))
    fila, lotes, piso = [], [], float("-inf")

    def sacar(instante, motivo):
        n = 1 if fila[0].solo else 0
        if not n:
            for entrada in fila[:3]:
                if entrada.solo:
                    break
                n += 1
        miembros = tuple(fila[:n])
        fila[:n] = []
        lotes.append((max(instante, piso, lotes[-1][0] if lotes else instante), motivo, miembros))

    def vaciar(ahora):
        while fila:
            if fila[0].solo:
                instante, motivo = fila[0].admitida, "solo"
            elif len(fila) >= 3 and not any(e.solo for e in fila[:3]):
                instante, motivo = max(e.admitida for e in fila[:3]), "tamano"
            else:
                instante, motivo = fila[0].admitida + 3600, "ventana"
            if max(instante, piso, lotes[-1][0] if lotes else instante) > ahora:
                break
            sacar(instante, motivo)

    cambios = [(e.admitida, 1, e) for e in llegadas]
    cambios += [(e.caducada, 0, e) for e in llegadas
                if e.caducada is not None and e.caducada <= hasta]
    cambios.sort(key=lambda c: (c[0], c[1], c[2].recibida, c[2].entrega.recibo))
    for instante, grupo in groupby(cambios, key=lambda c: c[0]):
        # Sólo un lote cerrado ANTES de observar la caducidad queda intacto.
        vaciar(math.nextafter(instante, float("-inf")))
        eventos = tuple(grupo)
        caducados = {e.entrega.recibo for _, tipo, e in eventos if tipo == 0}
        if any(e.entrega.recibo in caducados for e in fila):
            fila[:] = [e for e in fila if e.entrega.recibo not in caducados]
            piso = instante
        # El vencimiento gana a las admisiones simultáneas, como antes.
        vaciar(instante)
        for _, tipo, entrada in eventos:
            if tipo == 1 and (entrada.caducada is None or entrada.caducada > instante):
                fila.append(entrada)
                vaciar(instante)
    vaciar(hasta)
    return tuple(lotes), tuple(fila)


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
                              solo=entrada.solo or bool(vista and (
                                  vista.sensible or vista.alto_riesgo)))
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
