"""Un paso por tick; buzón durable de resultados y outbox bajo flock.

El ACK del buzón se guarda con el paso que lo consume, nunca con el IO.
Sólo se reponen campos producidos por el ejecutor: hora, base y admisiones
siempre proceden de la observación fresca. No modifica el reductor puro.
"""
from dataclasses import replace
import json
import subprocess

from dominio import Incierto, Promoviendo, Terminal, datos, desde_datos, paso
from efectos import EfectoFallido, ejecutar
from git_ops import FalloGit


EJECUTABLES = frozenset(("armar-candidato", "solicitar-ci", "iniciar-suite",
                        "reanudar-suite", "repetir", "armar-subconjunto", "promover"))
CAMPOS_RESULTADO = ("candidato", "suite", "conflicto_cadena", "main", "tren_id",
                    "intento_promocion", "rama_ocupada", "resultado_suite",
                    "suite_intento", "repeticion", "reproduccion_id")


def resultado_observacion(efecto, observacion):
    campos = CAMPOS_RESULTADO
    if efecto.tipo == "iniciar-suite":
        # Evidencia de CI con la que se lanzó la suite, ligada al mismo C.
        campos += ("ci_candidato", "ci_recibo")
    return {k: datos(getattr(observacion, k)) for k in campos}


def resumen_outbox(ledger, repo):
    return [{"clave": r["clave"], "efecto": json.loads(r["payload"]),
             "estado": r["estado"],
             "ejecutor": json.loads(r["payload"])["tipo"] in EJECUTABLES,
             "resultado": json.loads(r["resultado"]) if r["resultado"] else None}
            for r in ledger.outbox(repo)]


def tick(ledger, repo, observador, ahora, git, hasito=None, frontera=None):
    """El llamador mantiene el candado durante TODO el tick, incluido el IO.

    `frontera` es sólo una costura de inyección para pruebas de muerte real.
    Un fallo no confirma el efecto; cada clave se intenta a lo sumo una vez
    por tick. Los efectos de feedback no se despachan ni bloquean el drenaje.
    """
    intentados = set()
    frontera = frontera or (lambda nombre, efecto: None)
    # Sólo para entregas aún no incorporadas: conserva la primera admisión
    # muestreada en custodia, sin importar ningún lote simulado de sombra.
    estado = ledger.estado(repo, operativo=True)
    observacion = observador.observar(estado, ahora)

    def drenar(estado, observacion, recuperacion=False):
        if estado.pausa:
            return
        for fila in ledger.outbox(repo):
            if fila["estado"] == "resultado" or fila["clave"] in intentados:
                continue
            if recuperacion and fila["estado"] != "ejecutando":
                continue
            efecto = desde_datos(json.loads(fila["payload"]))
            if efecto.tipo not in EJECUTABLES:
                continue
            # No ejecutar trabajo de un tren ya cerrado. Se conserva visible.
            tren = next((t for t in estado.trenes if efecto.datos and
                         t.id == efecto.datos[0]), None)
            if tren is None or isinstance(tren.fase, Terminal):
                continue
            intentados.add(efecto.clave)
            ledger.iniciar_efecto(repo, efecto, ahora)
            frontera("antes-ejecutar", efecto)
            try:
                ejecucion = ejecutar(efecto, estado, observacion, git, hasito)
                frontera("efecto-ejecutado", efecto)
                if ejecucion.completo:
                    resultado = {"observacion": resultado_observacion(efecto, ejecucion.observacion)}
                else:
                    resultado = {"error": "ejecucion-incompleta",
                                 "observacion": resultado_observacion(efecto, ejecucion.observacion)}
                completo = ejecucion.completo
            except EfectoFallido as exc:
                resultado, completo = {"error": datos(exc.resultado)}, False
            except (FalloGit, OSError, ValueError, KeyError, TypeError,
                    StopIteration, subprocess.SubprocessError) as exc:
                # Nada de argv, stderr, tokens ni rutas privadas en los errores.
                resultado, completo = {"error": {"clase": type(exc).__name__}}, False
            frontera("antes-registrar", efecto)
            ledger.registrar_efecto(repo, efecto, ahora, resultado, completo)
            frontera("resultado-guardado", efecto)

    # Una muerte con el marcador durable puede haber producido IO sin ACK.
    # Recuperarlo antes del paso evita caducar PR cerrados por nuestro push.
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
                    recibida["main"] = datos(lectura)
                    if isinstance(lectura, Incierto):
                        ledger.registrar_efecto(repo, efecto, ahora,
                            {"error": "reconciliacion-incierta", "observacion": recibida})
                        intentados.add(efecto.clave)
                        continue
                observacion = replace(observacion, **{
                    k: desde_datos(v) for k, v in recibida.items()})
                consumido = fila["clave"]
                break
    nuevo, efectos = paso(estado, observacion)
    ledger.guardar_paso(nuevo, efectos, ahora, consumido)
    frontera("paso-guardado", None)
    drenar(nuevo, observacion)
    return {"modo": "activo", "estado": datos(nuevo),
            "efectos": datos(efectos), "outbox": resumen_outbox(ledger, repo)}
