"""Un paso por tick; buzón durable de resultados y outbox bajo flock.

El ACK del buzón se guarda con el paso que lo consume, nunca con el IO.
Sólo se reponen campos producidos por el ejecutor: hora, base y admisiones
siempre proceden de la observación fresca. No modifica el reductor puro.
"""
from copy import copy
from dataclasses import replace
import json
import os
import subprocess
import time

from dominio import (Admitido, Aterrizado, BloqueoPrevia, Diagnosticando, EnBase, EscritorExterno, EsperandoCI, Incierto,
                     Pausa, Promoviendo, Terminal, admision_entrada, aterrizaje, base_vieja, clave,
                     datos, desde_datos, lectura_ci, paso, recibo_ci)
from efectos import EfectoFallido, ejecutar, prelanzar_ci, suite_autorizada
from feedback import FEEDBACK, EscritorGitHub, pr_feedback
from git_ops import FalloGit
from operador import FalloSnapshot, Snapshots
from preaterrizaje import Previo


EJECUTABLES = frozenset(("armar-candidato", "solicitar-ci", "iniciar-suite",
                        "reanudar-suite", "repetir", "armar-subconjunto", "promover")) | FEEDBACK
EFECTOS_SUITE = ("iniciar-suite", "reanudar-suite", "repetir")
CAMPOS_RESULTADO = ("candidato", "suite", "conflicto_cadena", "main", "tren_id",
                    "intento_promocion", "rama_ocupada", "resultado_suite",
                    "suite_intento", "repeticion", "reproduccion_id")


def resultado_observacion(efecto, observacion, *, conserva_ci=True):
    campos = CAMPOS_RESULTADO
    if efecto.tipo == "iniciar-suite" and conserva_ci:
        # Evidencia de CI con la que se emitió el efecto de suite.
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


PLAZO_RESERVA_BOAT_S = 10


def actualizar_reserva_boat(ledger, config, adaptador, anterior, estado, ahora):
    clases = getattr(adaptador, "clases_boat", ())
    if not clases:
        return
    activo = next((t for t in estado.trenes if not isinstance(t.fase, Terminal)), None)
    acciones = []
    if anterior is not None and (estado.pausa or activo is None or activo.id != anterior.id):
        acciones.append(("liberar-reserva", anterior.id, ()))
    if activo is not None and not estado.pausa:
        acciones.append(("reservar", activo.id, ("--clases", ",".join(clases), "--ttl", "1800")))
    limite = time.monotonic() + PLAZO_RESERVA_BOAT_S
    for comando, lote, opciones in acciones:
        try:
            binario = os.path.expanduser(config.get("boat_pool", "~/.local/bin/boat-pool"))
            argv = [binario, comando, "--repo", estado.repo, "--lote", lote, *opciones]
            restante = limite - time.monotonic()
            if restante <= 0:
                raise subprocess.TimeoutExpired(argv, PLAZO_RESERVA_BOAT_S)
            subprocess.run(argv, check=True, timeout=restante, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError, TypeError, ValueError) as exc:
            fallo = {"lote": lote, "comando": comando, "clase": type(exc).__name__}
            if isinstance(exc, subprocess.CalledProcessError):
                fallo["codigo"] = exc.returncode
            ledger.evento(estado.repo, "fallo-reserva-boat", ahora, fallo)


def tick(ledger, repo, observador, ahora, git, adaptador=None, frontera=None,
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
    trenes_antes = estado.trenes
    config = getattr(observador, "config", {})
    snapshots = Snapshots(ledger, repo, git, config)
    if adaptador is not None and hasattr(adaptador, "operador"):
        adaptador = copy(adaptador)
        adaptador.operador = snapshots
        if snapshots.lanzador_configurado is None:
            snapshots.lanzador_configurado = adaptador.lanzador_configurado
    previo = Previo(ledger, git, config, frontera, snapshots)

    def limpiar(nuevo):
        previo.limpiar(nuevo)
        if (previo.fallo_snapshot is not None and nuevo.pausa is None and
                any(not isinstance(t.fase, Terminal) for t in nuevo.trenes)):
            nuevo = replace(nuevo, pausa=Pausa("infraestructura",
                detalle="checkout-operador:" + str(previo.fallo_snapshot)))
        return nuevo

    def suite_de_ci_vigente(tren):
        # El IO de la suite empieza con su primer efecto-iniciado: drenar no
        # inicia iniciar-suite sin el verde de la solicitud vigente. Ese verde
        # llega después del primer efecto-iniciado del solicitar-ci de N.
        # Reiniciar una fila no cambia la solicitud con la que empezó.
        if not isinstance(tren.fase, EsperandoCI):
            return False
        inicio = ledger.primer_inicio(repo, clave("suite", tren.id))
        if inicio is None or not tren.fase.solicitud:
            return inicio is not None
        ci = ledger.primer_inicio(repo, clave("ci", tren.id, tren.fase.solicitud))
        return ci is not None and ci < inicio

    previo.limpiar(estado)
    observacion = observador.observar(estado, ahora)
    activo = next((t for t in estado.trenes if not isinstance(t.fase, Terminal)), None)
    consulta = getattr(adaptador, "suite_con_coordinador", None)
    if activo is not None and ledger.efecto_iniciado(
            repo, clave("suite", activo.id),
            None if consulta is None else (lambda: consulta(activo.id))):
        observacion = replace(observacion, suite_iniciada=True,
                              suite_de_ci_vigente=suite_de_ci_vigente(activo))
    if not estado.pausa and any(isinstance(t.fase, Diagnosticando) for t in estado.trenes):
        observacion = replace(
            observacion, absoluciones_recientes=ledger.absoluciones_recientes(repo, ahora),
            firma_distingue_fallo=getattr(adaptador, "firma_distingue_fallo", False) is True,
            repeticiones_en_paralelo=getattr(adaptador, "repeticiones_paralelas", 1) > 1)
    previo.limpiar(estado, observacion)

    def guardar_paso(nuevo, efectos, consumido=None, fallo=None):
        cerrados = {t.id for t in nuevo.trenes if isinstance(t.fase, Terminal)}
        terminales = [t for t in trenes_antes if t.id in cerrados and
                      not isinstance(t.fase, Terminal)]
        # Captura las corridas sin leer sólo de los trenes que este paso cierra.
        confirmadas = tuple(corridas_sin_leer({t.id for t in terminales}, consumido))
        cancelar = getattr(adaptador, "cancelar_corridas", None)
        prelanzadas = cancelar(terminales, confirmadas) if terminales and cancelar is not None else ()
        ledger.guardar_paso(nuevo, efectos, ahora, consumido, fallo_operador=fallo,
                            prelanzadas=prelanzadas,
                            suites_sin_leer=(aviso for _, aviso in confirmadas))

    def corridas_sin_leer(cerrados, consumido):
        consultar = getattr(adaptador, "corridas_sin_leer", None)
        if consultar is None or not cerrados:
            return
        vistos = set()
        for fila in ledger.outbox(repo):
            if fila["estado"] == "pendiente" or fila["clave"] == consumido:
                continue
            efecto = desde_datos(json.loads(fila["payload"]))
            if efecto.tipo in EFECTOS_SUITE and efecto.datos[0] in cerrados:
                for registro, aviso in consultar(efecto):
                    if registro not in vistos:
                        vistos.add(registro)
                        yield registro, aviso

    def drenar(estado, observacion, recuperacion=False):
        completados = set()
        fallo_operador = None
        for fila, efecto, espera in outbox_en_orden(ledger, repo, estado, completados):
            # Se comprueba antes de iniciar: esperar no es intento ni fallo.
            # Recuperación también respeta predecesores pendientes/fallidos.
            if espera is not None:
                continue
            if fila["estado"] == "resultado" or fila["clave"] in intentados:
                continue
            # Los fallos de suite pueden tener una salida durable del lanzador.
            # Incorporarla antes del paso permite recuperar instalaciones viejas.
            if recuperacion and fila["estado"] != "ejecutando" and not (
                    fila["estado"] == "fallido" and efecto.tipo in EFECTOS_SUITE):
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
            # Recuperar, levantar una pausa o reanudar no publica el CI ni lanza
            # la suite de una B que main dejó atrás: el paso la rearma.
            if (efecto.tipo in ("solicitar-ci", "iniciar-suite") and
                    base_vieja(tren.fase, observacion)):
                continue
            if efecto.tipo == "solicitar-ci":
                # La clave puede preceder una muerte/cambio de evidencia. La
                # emisión pasada no autoriza publicar con admisión hoy retenida.
                vistas = {v.recibo or str(v.pr): v for v in observacion.prs}
                if any(not isinstance(admision_entrada(e, vistas, observacion), Admitido)
                       for e in estado.entradas if e.entrega.recibo in tren.recibos):
                    continue
            # Una fila emitida con otro verde no autoriza lanzar: iniciarla
            # adelantaría el efecto-iniciado que fecha el arranque de la suite.
            if (efecto.tipo == "iniciar-suite" and fila["estado"] == "pendiente" and
                    isinstance(tren.fase, EsperandoCI) and
                    not suite_autorizada(estado, tren, observacion)):
                continue
            intentados.add(efecto.clave)
            if fila["estado"] != "ejecutando" or efecto.tipo not in EFECTOS_SUITE:
                ledger.iniciar_efecto(repo, efecto, ahora, limpiar_resultado=(
                    fila["estado"] == "fallido" and efecto.tipo in EFECTOS_SUITE))
            frontera("antes-ejecutar", efecto)
            try:
                if feedback:
                    resultado = escritor.ejecutar_feedback(efecto, estado, frontera)
                    completo = True
                else:
                    ejecucion = ejecutar(efecto, estado, observacion, git, adaptador, previo,
                                         leer_ci=getattr(observador, "ci_candidato", None))
                    if ejecucion.aviso is not None:
                        ledger.evento(repo, "prelanzamiento-fallido", ahora, ejecucion.aviso)
                    if ejecucion.en_curso:
                        frontera("efecto-ejecutado", efecto)
                        continue
                    ci_numerado = isinstance(tren.fase, EsperandoCI) and tren.fase.solicitud > 0
                    resultado = {"observacion": resultado_observacion(
                        efecto, ejecucion.observacion, conserva_ci=not ci_numerado)}
                    completo = ejecucion.completo
                    if not completo:
                        resultado["error"] = "ejecucion-incompleta"
                frontera("efecto-ejecutado", efecto)
            except EfectoFallido as exc:
                resultado, completo = {"error": datos(exc.resultado)}, False
            except FalloSnapshot as exc:
                detalle = str(exc)
                if estado.pausa is None:
                    estado = replace(estado, pausa=Pausa(
                        "infraestructura", detalle="checkout-operador:" + detalle))
                # Se registra junto con la pausa al guardar el paso.
                fallo_operador = efecto, detalle
                continue
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
        return estado, fallo_operador

    def reintentar_prelanzamiento(nuevo):
        aplazado = getattr(adaptador, "prelanzamiento_aplazado", None)
        if nuevo.pausa or aplazado is None:
            return
        for tren in nuevo.trenes:
            if not isinstance(tren.fase, EsperandoCI) or not aplazado(tren.id):
                continue
            # Si falta publicar CI, el efecto hará el prelanzamiento tras publicar.
            if any(efecto.tipo == "solicitar-ci" and efecto.datos == (tren.id,) and
                   fila["estado"] != "resultado"
                   for fila, efecto, _ in outbox_en_orden(ledger, repo, nuevo, set())):
                continue
            vistas = {v.recibo or str(v.pr): v for v in observacion.prs}
            if any(not isinstance(admision_entrada(e, vistas, observacion), Admitido)
                   for e in nuevo.entradas if e.entrega.recibo in tren.recibos):
                continue
            aviso = prelanzar_ci(adaptador, tren, observacion)
            if aviso is not None:
                ledger.evento(repo, "prelanzamiento-fallido", ahora, aviso)

    def terminar(nuevo, efectos):
        # La reserva pasa al lote vivo antes de que su prelanzamiento aplazado pida VMs.
        actualizar_reserva_boat(ledger, config, adaptador, activo, nuevo, ahora)
        reintentar_prelanzamiento(nuevo)
        drenado, fallo = drenar(nuevo, observacion)
        if fallo is not None:
            ledger.guardar_paso(drenado, (), ahora, fallo_operador=fallo)
            frontera("paso-guardado", None)
            activo_confirmado = next((t for t in nuevo.trenes if not isinstance(t.fase, Terminal)), None)
            actualizar_reserva_boat(ledger, config, adaptador, activo_confirmado, drenado, ahora)
        snapshots.limpiar(drenado, previo.pendientes(repo), ahora)
        return {"modo": "activo", "estado": datos(drenado), "efectos": datos(efectos),
                "outbox": resumen_outbox(ledger, repo)}

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
                return terminar(estado, ())
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
        guardar_paso(nuevo, efectos)
        frontera("paso-guardado", None)
        return terminar(nuevo, efectos)
    # La limpieza no crea pausas: sólo retiene el push, incluso al reanudar.
    clave_suite = clave("suite", activo.id) if activo is not None else None
    fallo_previo = observacion.suite_de_ci_vigente and any(
        f["clave"] == clave_suite and f["estado"] == "fallido" for f in ledger.outbox(repo))
    estado, fallo = drenar(estado, observacion, recuperacion=True)
    filas = ledger.outbox(repo)
    if fallo_previo and any(f["clave"] == clave_suite and f["estado"] == "fallido" for f in filas):
        # Tras reintentar la fila fallida, un recibo aún ilegible deja correr el plazo.
        observacion = replace(observacion, suite_de_ci_vigente=False)
    consumido = None
    if not estado.pausa:
        for fila in filas:
            if fila["estado"] == "resultado":
                recibida = json.loads(fila["resultado"])["observacion"]
                efecto = desde_datos(json.loads(fila["payload"]))
                if efecto.tipo == "iniciar-suite":
                    tren = next((t for t in estado.trenes if t.id == efecto.datos[0]), None)
                    if tren is None:
                        continue
                    if isinstance(tren.fase, EsperandoCI) and tren.fase.solicitud:
                        # La suite terminó. Si falta verde, corre el plazo de CI.
                        observacion = replace(observacion, suite_de_ci_vigente=False)
                        if (observacion.tren_id != tren.id or observacion.ci_candidato != "verde" or
                                observacion.ci_recibo != recibo_ci(tren)):
                            continue
                        # Los recibos anteriores pueden incluir el CI del momento del arranque.
                        recibida.pop("ci_candidato", None)
                        recibida.pop("ci_recibo", None)
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
    if (not estado.pausa and activo is not None and isinstance(activo.fase, Promoviendo)
            and observacion.main is None and observacion.base != activo.fase.certificado.B
            and lectura_ci(activo, observacion) != "verde"):
        # Un push anterior pudo aterrizar antes de perder el ACK.
        cert = activo.fase.certificado
        heads = tuple(e.entrega.head for e in estado.entradas
                      if e.entrega.recibo in activo.recibos)
        lectura = git.reconciliar(cert.B, cert.C, cert.arbol, heads)
        if isinstance(lectura, (Aterrizado, EscritorExterno)):
            observacion = replace(observacion, main=lectura, tren_id=activo.id,
                                  intento_promocion=activo.fase.intentos)
        elif isinstance(lectura, Incierto):
            observacion = replace(observacion, ci_candidato="pendiente", ci_rojos=())
    nuevo, efectos = paso(estado, observacion)
    nuevo = limpiar(nuevo)
    guardar_paso(nuevo, efectos, consumido, fallo)
    frontera("paso-guardado", None)
    return terminar(nuevo, efectos)
