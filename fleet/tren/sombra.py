"""Sombra pura: admisión real y lotes hipotéticos, sin ejecutar efectos."""
from dataclasses import replace

from dominio import (Admitido, Estado, Terminal, Tren, admision_entrada, clave)


def lotes_fifo(entradas, hasta):
    """Reloj de admisión, sin duración de suite; el borde de 60 min vence primero."""
    llegadas = sorted((e for e in entradas if e.admitida is not None and
                       e.admitida <= hasta),
                      key=lambda e: (e.admitida, e.recibida, e.entrega.recibo))
    fila, lotes = [], []

    def sacar(instante, motivo):
        n = 1 if fila[0].solo else 0
        if not n:
            for entrada in fila[:3]:
                if entrada.solo:
                    break
                n += 1
        miembros = tuple(fila[:n])
        fila[:n] = []
        lotes.append((max(instante, lotes[-1][0] if lotes else instante), motivo, miembros))

    def vaciar(ahora):
        while fila:
            if fila[0].solo:
                sacar(fila[0].admitida, "solo")
            elif len(fila) >= 3 and not any(e.solo for e in fila[:3]):
                sacar(max(e.admitida for e in fila[:3]), "tamano")
            elif fila[0].admitida + 3600 <= ahora:
                sacar(fila[0].admitida + 3600, "ventana")
            else:
                break

    for entrada in llegadas:
        vaciar(entrada.admitida)
        fila.append(entrada)
        vaciar(entrada.admitida)
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
