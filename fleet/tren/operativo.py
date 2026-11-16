"""Un paso por tick; buzón durable de resultados y outbox bajo flock.

El ACK del buzón se guarda con el paso que lo consume, nunca con el IO.
Sólo se reponen campos producidos por el ejecutor: hora, base y admisiones
siempre proceden de la observación fresca. No modifica el reductor puro.
"""
from dataclasses import replace
import json
import subprocess

from dominio import (Admitido, Aterrizado, BloqueoPrevia, EnBase, Incierto, Promoviendo,
                     Terminal, admision_entrada, aterrizaje, datos, desde_datos, paso)
from efectos import EfectoFallido, ejecutar
from feedback import FEEDBACK, EscritorGitHub, pr_feedback
from git_ops import FalloGit
from preaterrizaje import Previo


EJECUTABLES = frozenset(("armar-candidato", "solicitar-ci", "iniciar-suite",
                        "reanudar-suite", "repetir", "armar-subconjunto", "promover")) | FEEDBACK
CAMPOS_RESULTADO = ("candidato", "suite", "conflicto_cadena", "main", "tren_id",
                    "intento_promocion", "rama_ocupada", "resultado_suite",
                    "suite_intento", "repeticion", "reproduccion_id")


def resultado_observacion(efecto, observacion):
    campos = CAMPOS_RESULTADO
    if efecto.tipo == "iniciar-suite":
        # Evidencia de CI con la que se lanzó la suite, ligada al mismo C.
        campos += ("ci_candidato", "ci_recibo")
    return {k: datos(getattr(observacion, k)) for k in campos}


def outbox_en_orden(ledger, repo, estado, completados):
    """Primero incompleto por PR; el llamador confirma cierres durante el IO."""
    anteriores = {}
    for fila in ledger.outbox(repo):
        efecto = desde_datos(json.loads(fila["payload"]))
        espera = None
        if efecto.tipo in FEEDBACK:
            pr = pr_feedback(efecto, estado)
            anterior = anteriores.get(pr)
            if anterior is not None and anterior not in completados:
                espera = {"motivo": "feedback-anterior-incompleto", "pr": pr,
                          "clave": anterior}
            else:
                anteriores[pr] = efecto.clave
        yield fila, efecto, espera


def resumen_outbox(ledger, repo):
    estado = ledger.estado(repo, operativo=True)
    return [{"clave": r["clave"], "efecto": datos(efecto),
             "estado": r["estado"],
             "ejecutor": efecto.tipo in EJECUTABLES, "espera": espera,
             "resultado": json.loads(r["resultado"]) if r["resultado"] else None}
            for r, efecto, espera in outbox_en_orden(ledger, repo, estado, set())]


def tick(ledger, repo, observador, ahora, git, hasito=None, frontera=None,
         escritor=None):
    """El llamador mantiene el candado durante TODO el tick, incluido el IO.

    `frontera` es sólo una costura de inyección para pruebas de muerte real.
    Un fallo no confirma el efecto; cada clave se intenta a lo sumo una vez
    por tick. Feedback no requiere tren vivo ni carril sin pausa.
    """
    intentados = set()
    frontera = frontera or (lambda nombre, efecto: None)
    escritor = escritor or EscritorGitHub()
    # Sólo para entregas aún no incorporadas: conserva la primera admisión
    # muestreada en custodia, sin importar ningún lote simulado de sombra.
    estado = ledger.estado(repo, operativo=True)
    previo = Previo(ledger, git, getattr(observador, "config", {}), frontera)

    def limpiar(nuevo):
        previo.limpiar(nuevo)
        return nuevo

    previo.limpiar(estado)
    observacion = observador.observar(estado, ahora)
    previo.limpiar(estado, observacion)

    def drenar(estado, observacion, recuperacion=False):
        completados = set()
        for fila, efecto, espera in outbox_en_orden(ledger, repo, estado, completados):
            # Se comprueba antes de iniciar: esperar no es intento ni fallo.
            # Recuperación también respeta predecesores pendientes/fallidos.
            if espera is not None:
                continue
            if fila["estado"] == "resultado" or fila["clave"] in intentados:
                continue
            if recuperacion and fila["estado"] != "ejecutando":
                continue
            if efecto.tipo not in EJECUTABLES:
                continue
            feedback = efecto.tipo in FEEDBACK
            # No ejecutar trabajo de un tren ya cerrado. Se conserva visible.
            tren = next((t for t in estado.trenes if efecto.datos and
                         t.id == efecto.datos[0]), None)
            if not feedback and (estado.pausa or tren is None or isinstance(tren.fase, Terminal)):
                continue
            # La obligación es durable y de todo el repo. No gastar un intento
            # ni reabrir verificar mientras otra operación exige cancelación.
            if efecto.tipo == "promover" and previo.bloqueado(repo):
                continue
            if efecto.tipo == "solicitar-ci":
                # La clave puede preceder una muerte/cambio de evidencia. La
                # emisión pasada no autoriza publicar con admisión hoy retenida.
                vistas = {v.recibo or str(v.pr): v for v in observacion.prs}
                if any(not isinstance(admision_entrada(e, vistas, observacion), Admitido)
                       for e in estado.entradas if e.entrega.recibo in tren.recibos):
                    continue
            intentados.add(efecto.clave)
            ledger.iniciar_efecto(repo, efecto, ahora)
            frontera("antes-ejecutar", efecto)
            try:
                if feedback:
                    resultado = escritor.ejecutar_feedback(efecto, estado, frontera)
                    completo = True
                else:
                    ejecucion = ejecutar(efecto, estado, observacion, git, hasito, previo)
                    resultado = {"observacion": resultado_observacion(efecto, ejecucion.observacion)}
                    completo = ejecucion.completo
                    if not completo:
                        resultado["error"] = "ejecucion-incompleta"
                frontera("efecto-ejecutado", efecto)
            except EfectoFallido as exc:
                resultado, completo = {"error": datos(exc.resultado)}, False
            except (FalloGit, OSError, ValueError, KeyError, TypeError,
                    StopIteration, subprocess.SubprocessError) as exc:
                # Nada de argv, stderr, tokens ni rutas privadas en los errores.
                resultado, completo = {"error": {"clase": type(exc).__name__}}, False
            frontera("antes-registrar", efecto)
            ledger.registrar_efecto(repo, efecto, ahora, resultado, completo,
                                    sin_consumo=feedback)
            if feedback and completo:
                completados.add(efecto.clave)
            frontera("resultado-guardado", efecto)

    # Una muerte con el marcador durable puede haber producido IO sin ACK.
    # Recuperarlo antes del paso evita caducar PR cerrados por nuestro push.
    decision = ledger.siguiente_decision(repo)
    if decision is not None:
        # Antes de recuperar IO: una pausa manual ya registrada lo impide.
        # El juicio ocupa este paso entero; no mezcla un ACK de otra generación.
        observacion = replace(observacion, decision=decision,
                              pausa_id=ledger.pausa_id(repo, estado))
        frontera("decision-observada", None)
        recuperados = ()
        from juicios import error_decision
        tren = next((t for t in estado.trenes if t.id == decision.tren), None)
        if (decision.decision == "abortar" and tren is not None and
                isinstance(tren.fase, Promoviendo) and
                not error_decision(estado, decision, observacion.pausa_id)):
            cert = tren.fase.certificado
            heads = tuple(e.entrega.head for e in estado.entradas
                          if e.entrega.recibo in tren.recibos)
            # Sólo lectura: un aborto no puede esconder un CAS sin ACK.
            lectura = previo.reconciliar(cert.B, cert.C, cert.arbol, heads)
            if isinstance(lectura, Incierto):
                # Sigue pendiente; otro tick vuelve a acreditar sin empujar.
                drenar(estado, observacion)
                return {"modo": "activo", "estado": datos(estado), "efectos": [],
                        "outbox": resumen_outbox(ledger, repo)}
            if isinstance(lectura, Aterrizado):
                estado, recuperados = aterrizaje(replace(estado, pausa=None), tren,
                    replace(observacion, main=lectura, tren_id=tren.id,
                            intento_promocion=tren.fase.intentos))
                # El juicio queda rechazado por pausa ya resuelta; el éxito y
                # su feedback se guardan atómicamente con ese rechazo.
        nuevo, efectos = paso(estado, observacion)
        efectos = recuperados + efectos
        frontera("decision-reducida", None)
        nuevo = limpiar(nuevo)
        ledger.guardar_paso(nuevo, efectos, ahora)
        frontera("paso-guardado", None)
        drenar(nuevo, observacion)
        return {"modo": "activo", "estado": datos(nuevo),
                "efectos": datos(efectos), "outbox": resumen_outbox(ledger, repo)}
    # La limpieza no crea pausas: sólo retiene el push, incluso al reanudar.
    drenar(estado, observacion, recuperacion=True)
    consumido = None
    if not estado.pausa:
        for fila in ledger.outbox(repo):
            if fila["estado"] == "resultado":
                recibida = json.loads(fila["resultado"])["observacion"]
                efecto = desde_datos(json.loads(fila["payload"]))
                if efecto.tipo == "promover":
                    tren = next(t for t in estado.trenes if t.id == efecto.datos[0])
                    if not isinstance(tren.fase, Promoviendo):
                        continue
                    cert = tren.fase.certificado
                    heads = tuple(e.entrega.head for e in estado.entradas
                                  if e.entrega.recibo in tren.recibos)
                    # main es mutable: el ACK guardado no sustituye la lectura
                    # actual, aunque ningún efecto nuevo vaya a ejecutarse.
                    lectura = git.reconciliar(cert.B, cert.C, cert.arbol, heads)
                    bloqueo = isinstance(desde_datos(recibida.get("main")), BloqueoPrevia)
                    if not bloqueo or not isinstance(lectura, (EnBase, Incierto)):
                        recibida["main"] = datos(lectura)
                    if isinstance(lectura, Incierto) and not bloqueo:
                        ledger.registrar_efecto(repo, efecto, ahora,
                            {"error": "reconciliacion-incierta", "observacion": recibida})
                        intentados.add(efecto.clave)
                        continue
                observacion = replace(observacion, **{
                    k: desde_datos(v) for k, v in recibida.items()})
                consumido = fila["clave"]
                break
    nuevo, efectos = paso(estado, observacion)
    nuevo = limpiar(nuevo)
    ledger.guardar_paso(nuevo, efectos, ahora, consumido)
    frontera("paso-guardado", None)
    drenar(nuevo, observacion)
    return {"modo": "activo", "estado": datos(nuevo),
            "efectos": datos(efectos), "outbox": resumen_outbox(ledger, repo)}
