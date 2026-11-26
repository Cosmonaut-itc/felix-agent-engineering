"""Política de pausas y decisiones puras; sin reloj, ledger ni transporte."""
from dataclasses import replace
import json
import shlex

from dominio import (Armado, Congelado, Corriendo, Diagnosticando,
                     Efecto, EsperandoCI, Pausa, Promoviendo, Rechazado, Terminal, Tren,
                     argv_reproduccion, cambiar_fase, clave, datos, efecto_diagnostico,
                     efecto_promover, efecto_reanudar_suite, efectos_status, separar_devueltos)


# Motivo/detalle -> decisión -> operación. Los prefijos terminan en ':' y
# admiten sólo el nombre de unidad que agrega el planificador D5.
ROJO = {"absolver": "suite-nueva", "expulsar": "devolver", "abortar": "abortar"}
POLITICA = {
    ("etapa-1", ""): {"reanudar": "continuar"},
    ("certificado-sin-base", ""): {"abortar": "abortar"},
    ("escritor-externo", ""): {"reanudar": "continuar"},
    ("infraestructura", "tres-reanudaciones-agotadas"):
        {"reanudar": "infraestructura", "absolver": "suite-nueva", "abortar": "abortar"},
    ("infraestructura", "lanzador-rechazo"):
        {"reanudar": "lanzador-nuevo", "abortar": "abortar"},
    ("infraestructura", "corrida-interrumpida"):
        {"reanudar": "lanzador-nuevo", "abortar": "abortar"},
    ("infraestructura", "cobertura-parcial-boat"):
        {"reanudar": "lanzador-nuevo", "abortar": "abortar"},
    ("infraestructura", "boat-sin-recibo"):
        {"reanudar": "lanzador-nuevo", "abortar": "abortar"},
    ("infraestructura", "salida-sin-recibo"):
        {"reanudar": "reenganchar", "abortar": "abortar"},
    ("infraestructura", "checkout-operador:"):
        {"reanudar": "continuar", "abortar": "abortar"},
    ("infraestructura", "ci-sin-resultado"):
        {"reanudar": "ci-nuevo", "abortar": "abortar"},
    ("infraestructura", "pre-aterrizaje:"):
        {"reanudar": "promover", "abortar": "abortar"},
    ("infraestructura", "cancelacion-pre-aterrizaje"):
        {"reanudar": "continuar", "abortar": "abortar"},
    ("rama-tren-ocupada", ""): {"reanudar": "publicar", "abortar": "abortar"},
    ("conflicto-en-cadena", ""): {"expulsar": "devolver", "abortar": "abortar"},
    ("candidato-no-corresponde", ""): {"reanudar": "armar", "abortar": "abortar"},
    ("head-cambiado-en-tren", ""): {"expulsar": "devolver", "abortar": "abortar"},
    ("promocion-rechazada", ""): {"reanudar": "promover", "abortar": "abortar"},
    ("manual", ""): {"levantar": "continuar"},
}
for _detalle in (
        "incompleto-invalido", "resultado-desconocido", "verde-incoherente",
        "subconjunto-no-corresponde", "conflicto-en-subconjunto",
        "repeticion-no-corresponde", "origen-distinto",
        "presupuesto-agotado-en-reanudacion", "suite-incompleta",
        "tamano-de-lote-no-soportado", "sin-costo-de-suite-sana",
        "sin-duracion:", "presupuesto-agotado:", "confirmacion-candidato:",
        "definicion-distinta:", "confirmacion-base:", "reproduccion-incierta:",
        "intermitente-reincidente:"):
    POLITICA[("necesita-juicio", _detalle)] = ROJO


def tren_pausado(estado):
    if not estado.pausa or estado.pausa.motivo in ("manual", "etapa-1"):
        return None
    if estado.pausa.motivo == "escritor-externo":
        return next((t for t in reversed(estado.trenes)
                     if t.fase == Terminal("REARMAR")), None)
    return next((t for t in estado.trenes if not isinstance(t.fase, Terminal)), None)


def politica(pausa):
    if pausa is None:
        return {}
    if pausa.motivo == "manual":
        return POLITICA[("manual", "")]
    exacta = POLITICA.get((pausa.motivo, pausa.detalle))
    if exacta is not None:
        return exacta
    return next((acciones for (motivo, detalle), acciones in POLITICA.items()
                 if motivo == pausa.motivo and detalle.endswith(":") and
                 pausa.detalle.startswith(detalle) and len(pausa.detalle) > len(detalle)), {})


def candidato_de(tren):
    return getattr(tren.fase, "candidato", None) if tren else None


def base_y_commit(tren):
    fase = tren.fase
    evidencia = candidato_de(tren) or (fase.certificado if isinstance(fase, Promoviendo) else fase)
    return getattr(evidencia, "B", ""), getattr(evidencia, "C", "")


def error_decision(estado, decision, pausa_id):
    """También valida el CLI; el reductor repite la validación al consumir."""
    if decision.repo != estado.repo or not decision.motivo.strip():
        return "repo-o-motivo-invalido"
    if decision.pausa_id != pausa_id or decision.pausa != estado.pausa:
        return "pausa-no-vigente"
    if decision.decision == "pausar":
        return "carril-ya-pausado" if estado.pausa else (
            "alcance-invalido" if decision.tren is not None or decision.pr is not None else "")
    tren = tren_pausado(estado)
    if decision.tren != (tren.id if tren else None):
        return "tren-no-corresponde"
    operacion = politica(estado.pausa).get(decision.decision)
    if operacion is None:
        return "decision-no-admitida"
    if tren is None and operacion != "continuar":
        return "tren-no-vigente"
    if tren and isinstance(tren.fase, Terminal) and (
            estado.pausa.motivo != "escritor-externo" or operacion != "continuar"):
        return "tren-terminal"
    if decision.decision == "expulsar":
        miembros = {e.entrega.pr for e in estado.entradas if e.entrega.recibo in tren.recibos}
        if decision.pr not in miembros:
            return "pr-no-miembro"
        if estado.pausa.motivo == "conflicto-en-cadena" and decision.pr != estado.pausa.pr:
            return "pr-no-es-conflicto"
        if not base_y_commit(tren)[0]:
            return "sin-base"
    elif decision.pr is not None:
        return "pr-solo-para-expulsar"
    fase = tren.fase if tren else None
    if operacion == "suite-nueva":
        if not isinstance(fase, (Corriendo, Diagnosticando)):
            return "fase-no-corresponde"
        if candidato_de(tren) is None:
            return "sin-candidato"
        if any(isinstance(e.admision, Rechazado) for e in estado.entradas
               if e.entrega.recibo in tren.recibos):
            return "miembro-rechazado"
    if operacion in ("lanzador-nuevo", "reenganchar") and not isinstance(fase, (Corriendo, Diagnosticando)):
        return "fase-no-corresponde"
    if operacion == "reenganchar" and not estado.pausa.archivos:
        return "sin-registro-para-reenganchar"
    if operacion == "infraestructura" and (
            not isinstance(fase, (Corriendo, Diagnosticando)) or not fase.reanudar):
        return "sin-reanudacion"
    if operacion == "infraestructura" and isinstance(fase, Diagnosticando):
        gastado = sum(max(e.solicitud.costo_s,
            sum(i.duracion_s for u in e.resultado.unidades for i in u.intentos))
            for e in fase.evidencias)
        if fase.pendiente is None:
            return "sin-reproduccion"
        if gastado + fase.costo_infra_s + 2 * fase.pendiente.costo_s > fase.presupuesto_s:
            return "presupuesto-de-diagnostico-agotado"
    if operacion == "publicar" and not isinstance(fase, (Armado, EsperandoCI)):
        return "fase-no-corresponde"
    if operacion == "ci-nuevo" and not isinstance(fase, EsperandoCI):
        return "fase-no-corresponde"
    if operacion == "armar" and not isinstance(fase, Congelado):
        return "fase-no-corresponde"
    if operacion == "promover" and not isinstance(fase, Promoviendo):
        return "fase-no-corresponde"
    return ""


def resolver(estado, observacion):
    decision = observacion.decision
    error = error_decision(estado, decision, observacion.pausa_id)
    resultado = Efecto(clave("resolver-juicio", decision.id), "resolver-juicio",
        (decision.id, "rechazada" if error else "aplicada", error))
    if error:
        return estado, (resultado,)
    if decision.decision == "pausar":
        return replace(estado, pausa=Pausa("manual", detalle=decision.motivo)), (resultado,)
    tren = tren_pausado(estado)
    # El detalle manual es texto humano; no es una clave de política.
    operacion = politica(estado.pausa)[decision.decision]
    estado = replace(estado, pausa=None)
    efectos = ()
    if operacion == "abortar":
        estado = cambiar_fase(estado, tren, Terminal("ABORTADO"))
        estado = replace(estado, entradas=tuple(
            replace(e, tren=None) if e.entrega.recibo in tren.recibos else e
            for e in estado.entradas))
    elif operacion == "suite-nueva":
        # Otro espacio de recibos evita que el adaptador reutilice el rojo.
        # Se conserva C exacto y se vuelve a exigir CI antes de la suite.
        estado = cambiar_fase(estado, tren, Terminal("ABSUELTO"))
        nuevo = Tren(clave("juicio-suite", tren.id, decision.id), tren.recibos,
                     Armado(candidato_de(tren)))
        estado = replace(estado, trenes=estado.trenes + (nuevo,), entradas=tuple(
            replace(e, tren=nuevo.id) if e.entrega.recibo in tren.recibos else e
            for e in estado.entradas))
        efectos = efectos_status(nuevo, "avisar-en-tren")
    elif operacion == "devolver":
        base, commit = base_y_commit(tren)
        entrada = next(e for e in estado.entradas
                       if e.entrega.recibo in tren.recibos and e.entrega.pr == decision.pr)
        fallos = []
        for f in getattr(tren.fase, "fallos", ()):
            fallos.append({"unidad": f.unidad, "check": f.check, "firma": f.firma,
                "conjunto_causal": (decision.pr,),
                "comando": shlex.join(argv_reproduccion(f, commit))})
        payload = json.dumps({"lote": tren.id, "B": base, "C": commit,
            "pr": decision.pr, "head": entrada.entrega.head, "fallos": fallos,
            "juicio": {"motivo": decision.motivo, "pausa": datos(decision.pausa)}}, sort_keys=True)
        efecto = Efecto(clave("devolver", tren.id, entrada.entrega.recibo), "devolver",
                        (tren.id, entrada.entrega.recibo, payload))
        estado, efectos = separar_devueltos(estado, tren, base, {decision.pr}, (efecto,))
    elif operacion == "ci-nuevo":
        fase = replace(tren.fase, desde=observacion.ahora,
                       solicitud=tren.fase.solicitud + 1)
        estado = cambiar_fase(estado, tren, fase)
        efectos = (Efecto(clave("ci", tren.id, fase.solicitud),
                          "solicitar-ci", (tren.id,)),)
    elif operacion in ("armar", "publicar"):
        tipo = "armar-candidato" if operacion == "armar" else "solicitar-ci"
        efectos = (Efecto(clave(tipo, tren.id, decision.id), tipo, (tren.id,)),)
    elif operacion in ("lanzador-nuevo", "reenganchar"):
        if operacion == "reenganchar":
            fase = replace(tren.fase, reenganches=tren.fase.reenganches + 1,
                           registro_reenganche=decision.pausa.archivos[0])
        else:
            fase = replace(tren.fase, reintentos_lanzador=tren.fase.reintentos_lanzador + 1,
                           reenganches=0, registro_reenganche="")
        estado = cambiar_fase(estado, tren, fase)
        efectos = ((efecto_reanudar_suite(tren, fase),) if isinstance(fase, Corriendo)
                   else (efecto_diagnostico(tren, fase),))
    elif operacion == "infraestructura":
        fase = replace(tren.fase, reanudaciones=tren.fase.reanudaciones + 1,
                       esperar_hasta=None, reenganches=0, registro_reenganche="")
        if isinstance(fase, Diagnosticando):
            fase = replace(fase, costo_infra_s=fase.costo_infra_s + fase.pendiente.costo_s)
        estado = cambiar_fase(estado, tren, fase)
        efectos = ((efecto_reanudar_suite(tren, fase),)
            if isinstance(fase, Corriendo) else (efecto_diagnostico(tren, fase),))
    elif operacion == "promover":
        fase = replace(tren.fase, intentos=tren.fase.intentos + 1, reintento_pendiente=False)
        estado = cambiar_fase(estado, tren, fase)
        efectos = (efecto_promover(tren.id, fase.intentos),)
    return estado, (resultado,) + efectos


def expediente(ledger, repo):
    """Proyección local del expediente; nunca lee recibos ni consulta GitHub."""
    from dominio import Decision
    estado = ledger.estado(repo, operativo=True)
    tren = tren_pausado(estado)
    pausa_id = ledger.pausa_id(repo, estado)
    admitidas = []
    miembros = [e.entrega for e in estado.entradas if tren and e.entrega.recibo in tren.recibos]
    for accion in politica(estado.pausa):
        prs = [e.pr for e in miembros] if accion == "expulsar" else [None]
        for pr in prs:
            decision = Decision("expediente", repo, tren.id if tren else None,
                                accion, pr, "<motivo>", 0, pausa_id, estado.pausa)
            if error_decision(estado, decision, pausa_id):
                continue
            argv = ["pao-tren", "pausa" if accion == "levantar" else "juicio", "--repo", repo]
            if accion == "levantar":
                argv += ["--levantar"]
            else:
                if tren:
                    argv += ["--tren", tren.id]
                argv += ["--decision", accion]
                if pr is not None:
                    argv += ["--pr", str(pr)]
            argv += ["--motivo", "<motivo>"]
            admitidas.append({"decision": accion, "pr": pr, "comando": shlex.join(argv)})
    resultados = []
    contexto = tren or next((t for t in estado.trenes if not isinstance(t.fase, Terminal)), None)
    miembros = [e.entrega for e in estado.entradas
                if contexto and e.entrega.recibo in contexto.recibos]
    if contexto:
        for fila in ledger.db.execute(
                "SELECT e.payload, r.payload AS resultado FROM effects e JOIN effect_results r "
                "ON e.clave=r.clave WHERE e.repo=? ORDER BY e.rowid", (repo,)):
            efecto = json.loads(fila["payload"])
            resultado = json.loads(fila["resultado"])
            if efecto["datos"] and efecto["datos"][0] == contexto.id and (
                    resultado.get("observacion", {}).get("resultado_suite") is not None or
                    resultado.get("observacion", {}).get("repeticion") is not None):
                resultados.append({"efecto": efecto, "resultado": resultado})
    return {"pausa_id": pausa_id, "pausa": datos(estado.pausa),
            "tren": contexto.id if contexto else None,
            "fase": datos(contexto.fase) if contexto else None,
            "miembros": [{"pr": e.pr, "head": e.head} for e in miembros],
            "resultados": resultados, "admitidas": admitidas,
            "decisiones": ledger.decisiones(repo)}
