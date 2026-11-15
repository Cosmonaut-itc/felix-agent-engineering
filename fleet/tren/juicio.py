"""Juicio y pausa manual: tabla motivo × decisión y transiciones puras.

Una decisión es una observación más de `paso`: sin reloj, IO ni GitHub. La
misma función `rechazo` responde en el CLI (exit 2) y en el reductor (se
registra rechazada, sin efecto). Ninguna transición certifica ni promueve:
`absolver` sólo vuelve a pedir CI verde y suite completa sobre el mismo C.
"""
from dataclasses import dataclass, replace
import json
import shlex
from typing import Dict, Tuple, Union

from dominio import (Certificado, Congelado, Corriendo, Diagnosticando,
                     Efecto, EsperandoCI, Pausa, Promoviendo, Rechazado, Terminal,
                     Tren, cambiar_fase, clave, datos, efectos_status)


# Las cuatro del CLI `juicio`; `pausar` y `levantar` sólo vienen de `pausa`.
JUICIO = ("absolver", "expulsar", "reanudar", "abortar")
# Actúan sobre el tren pausado y exigen su id; las demás nunca llevan tren.
SOBRE_TREN = frozenset(("absolver", "expulsar", "abortar"))


@dataclass(frozen=True)
class Admite:
    decisiones: Tuple[str, ...]
    # A quién se puede expulsar: "miembro" del tren o sólo el PR "en-conflicto".
    expulsar: str = ""


DIAGNOSTICO = Admite(("absolver", "expulsar", "abortar"), "miembro")
DESCONOCIDA = Admite(("abortar",))

# necesita-juicio: familia del detalle (prefijo antes de «:»). Todas ocurren
# con un C ya construido, en Corriendo o Diagnosticando.
DETALLES_JUICIO = (
    "incompleto-invalido", "resultado-desconocido", "verde-incoherente",
    "subconjunto-no-corresponde", "conflicto-en-subconjunto",
    "repeticion-no-corresponde", "origen-distinto",
    "presupuesto-agotado-en-reanudacion", "suite-incompleta",
    "tamano-de-lote-no-soportado", "sin-costo-de-suite-sana", "sin-duracion",
    "presupuesto-agotado", "confirmacion-candidato", "definicion-distinta",
    "confirmacion-base", "reproduccion-incierta")

# Una fila por motivo de pausa. `reanudar` sólo figura donde seguir desde el
# estado guardado avanza; en las demás el tick volvería a pausar enseguida.
TABLA = {
    "etapa-1": Admite(("reanudar",)),
    "escritor-externo": Admite(("reanudar",)),
    "manual": Admite(("levantar",)),
    "certificado-sin-base": Admite(("abortar",)),
    "infraestructura": Admite(("absolver", "abortar")),
    "rama-tren-ocupada": Admite(("abortar",)),
    "conflicto-en-cadena": Admite(("expulsar", "abortar"), "en-conflicto"),
    "candidato-no-corresponde": Admite(("abortar",)),
    "head-cambiado-en-tren": Admite(("expulsar", "abortar"), "miembro"),
    "promocion-rechazada": Admite(("abortar",)),
    "necesita-juicio": {detalle: DIAGNOSTICO for detalle in DETALLES_JUICIO},
}  # type: Dict[str, Union[Admite, Dict[str, Admite]]]


def admitidas(pausa):
    fila = TABLA.get(pausa.motivo, DESCONOCIDA)
    if isinstance(fila, dict):
        fila = fila.get(pausa.detalle.split(":", 1)[0], DESCONOCIDA)
    return fila


def tren_activo(estado):
    return next((t for t in estado.trenes if not isinstance(t.fase, Terminal)), None)


def miembros(estado, tren):
    entradas = {e.entrega.recibo: e for e in estado.entradas}
    return tuple(entradas[r] for r in tren.recibos)


def expulsables(estado, tren):
    alcance = admitidas(estado.pausa).expulsar
    return tuple(e.entrega.pr for e in miembros(estado, tren)
                 if alcance == "miembro" or e.entrega.pr == estado.pausa.pr)


def rechazo(estado, decision):
    """Cadena vacía si la decisión corresponde al estado; si no, el motivo."""
    if decision.repo != estado.repo:
        return "repo-distinto"
    if decision.decision == "pausar":
        if estado.pausa is not None:
            return "carril-pausado"
        return "pausa-distinta" if decision.pausa is not None else ""
    if estado.pausa is None:
        return "sin-pausa"
    if decision.pausa != estado.pausa:
        return "pausa-distinta"
    if decision.decision not in admitidas(estado.pausa).decisiones:
        return "decision-no-admitida"
    if decision.decision != "expulsar" and decision.pr is not None:
        return "pr-no-corresponde"
    if decision.decision not in SOBRE_TREN:
        return "tren-no-corresponde" if decision.tren else ""
    tren = tren_activo(estado)
    if tren is None or decision.tren != tren.id:
        return "tren-no-corresponde"
    if decision.decision == "expulsar":
        if decision.pr is None:
            return "falta-pr"
        if decision.pr not in expulsables(estado, tren):
            return "pr-no-expulsable"
    if decision.decision == "absolver":
        # Mismo C: sólo con los mismos miembros y sus heads entregados.
        if not isinstance(tren.fase, (Corriendo, Diagnosticando)):
            return "sin-candidato"
        if any(isinstance(e.admision, Rechazado) for e in miembros(estado, tren)):
            return "miembro-rechazado"
    return ""


def bases(fase):
    if isinstance(fase, Congelado):
        return fase.B, ""
    if isinstance(fase, Certificado):
        return fase.B, fase.C
    if isinstance(fase, Promoviendo):
        return fase.certificado.B, fase.certificado.C
    return fase.candidato.B, fase.candidato.C


def generacion(estado, tren, final, nuevo):
    """Cierra el tren pausado y mueve sus miembros vigentes a `nuevo`."""
    estado = cambiar_fase(replace(estado, pausa=None), tren, final)
    if nuevo is None:
        return estado
    return replace(estado, trenes=estado.trenes + (nuevo,), entradas=tuple(
        replace(e, tren=nuevo.id) if e.entrega.recibo in nuevo.recibos else e
        for e in estado.entradas))


def pausar(estado, decision):
    return replace(estado, pausa=Pausa("manual", detalle=decision.motivo)), ()


def levantar(estado, decision):
    return replace(estado, pausa=None), ()


def abortar(estado, decision):
    """Sin culpa ni feedback rojo: vuelven a la fila con su fecha FIFO."""
    tren = tren_activo(estado)
    estado = generacion(estado, tren, Terminal("ABORTADO"), None)
    return replace(estado, entradas=tuple(
        replace(e, tren=None) if e.entrega.recibo in tren.recibos and e.terminal is None
        else e for e in estado.entradas)), ()


def absolver(estado, decision):
    """Descarta el resultado: otra generación lee CI y corre la suite sobre el mismo C."""
    tren = tren_activo(estado)
    nuevo = Tren(clave("juicio", tren.id, decision.id), tren.recibos,
                 EsperandoCI(tren.fase.candidato))
    return (generacion(estado, tren, Terminal("ABSUELTO"), nuevo),
            efectos_status(nuevo, "avisar-en-tren"))


def expulsar(estado, decision):
    """`devolver` de U14 al expulsado; los demás, generación nueva con suite completa."""
    tren = tren_activo(estado)
    fase = tren.fase
    B, C = bases(fase)
    expulsada = next(e for e in miembros(estado, tren) if e.entrega.pr == decision.pr)
    entrega = expulsada.entrega
    fallos = []
    if isinstance(fase, Diagnosticando):
        # Sin atribución: el juicio es el conjunto causal. Plantilla sobre C.
        for f in fase.fallos:
            fallos.append({"unidad": f.unidad, "check": f.check, "firma": f.firma,
                           "conjunto_causal": [entrega.pr], "comando": shlex.join([
                               "bash", "scripts/mini/run.sh", "--repetir", fase.suite,
                               "--sha", C, "--worktree", "<worktree-desacoplado-en-" + C + ">",
                               "--veces", "2", "--unidad", f.check + "=" + f.unidad])})
    payload = json.dumps({"lote": tren.id, "B": B, "C": C, "pr": entrega.pr,
                          "head": entrega.head, "fallos": fallos,
                          "juicio": decision.id}, sort_keys=True)
    efectos = [Efecto(clave("devolver", tren.id, entrega.recibo), "devolver",
                      (tren.id, entrega.recibo, payload))]
    # Un rechazado (head cambiado) ya recibió su caducidad: no sigue ni se devuelve.
    sobrevivientes = tuple(e.entrega.recibo for e in miembros(estado, tren)
                           if e.entrega.recibo != entrega.recibo and
                           not isinstance(e.admision, Rechazado))
    nuevo = (Tren(clave("juicio", tren.id, decision.id), sobrevivientes, Congelado(B))
             if sobrevivientes else None)
    estado = generacion(estado, tren, Terminal("EXPULSADO"), nuevo)
    estado = replace(estado, entradas=tuple(
        replace(e, terminal=Terminal("DEVUELTO")) if e.entrega.recibo == entrega.recibo else e
        for e in estado.entradas))
    if nuevo is not None:
        efectos.append(Efecto(clave("armar", nuevo.id), "armar-candidato", (nuevo.id,)))
        efectos.extend(efectos_status(nuevo, "avisar-en-tren"))
    return estado, tuple(efectos)


TRANSICIONES = {"pausar": pausar, "levantar": levantar, "reanudar": levantar,
                "abortar": abortar, "absolver": absolver, "expulsar": expulsar}


def aplicar(estado, decision):
    """Una decisión que ya no corresponde no cambia nada; el tick la registra."""
    if rechazo(estado, decision):
        return estado, ()
    return TRANSICIONES[decision.decision](estado, decision)


def comandos(repo, estado):
    """Comando exacto de cada decisión admitida; sólo el motivo queda por escribir."""
    pausa = estado.pausa
    if pausa is None:
        return [{"decision": "pausar", "comando": shlex.join(
            ["pao-tren", "pausa", "--repo", repo, "--motivo", "<motivo>"])}]
    tren = tren_activo(estado)
    salida = []
    for decision in admitidas(pausa).decisiones:
        if decision == "levantar":
            argv = [["pao-tren", "pausa", "--repo", repo, "--levantar"]]
        else:
            base = ["pao-tren", "juicio", "--repo", repo]
            if decision in SOBRE_TREN:
                if tren is None:
                    continue
                base += ["--tren", tren.id]
            base += ["--decision", decision]
            argv = ([base + ["--pr", str(pr)] for pr in expulsables(estado, tren)]
                    if decision == "expulsar" else [base])
        for a in argv:
            salida.append({"decision": decision,
                           "comando": shlex.join(a + ["--motivo", "<motivo>"])})
    return salida


def expediente(repo, estado, registradas):
    """Lo que necesita quien juzga, sin abrir el ledger a mano."""
    tren = tren_activo(estado) if estado.pausa is not None else None
    fase = tren.fase if tren else None
    causa = None
    if isinstance(fase, (Corriendo, Diagnosticando)):
        causa = {"suite": fase.suite, "ci": fase.ci}
        if isinstance(fase, Diagnosticando):
            causa.update({"fallos": datos(fase.fallos), "evidencias": datos(fase.evidencias),
                          "pendiente": datos(fase.pendiente), "presupuesto_s": fase.presupuesto_s,
                          "restantes": list(fase.restantes)})
    B, C = bases(fase) if fase else (None, None)
    return {
        "pausa": datos(estado.pausa),
        "tren": tren.id if tren else None,
        "fase": type(fase).__name__ if fase else None,
        "B": B, "C": C,
        "miembros": [{"pr": e.entrega.pr, "head": e.entrega.head, "recibo": e.entrega.recibo,
                      "admision": datos(e.admision)} for e in miembros(estado, tren)]
                    if tren else [],
        "causa": causa,
        "admitidas": comandos(repo, estado),
        "decisiones": registradas,
    }
