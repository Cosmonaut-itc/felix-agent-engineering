"""Tick activo (U13): paso operativo y ejecutor del outbox bajo el candado del repo.

Un tick hace, sin soltar el candado:

1. Lee el estado operativo, observa GitHub/git y aplica `paso`. Guarda en una
   transacción el estado nuevo y los efectos nuevos en effects/outbox.
2. Recorre una vez, en orden, los efectos ejecutables no confirmados. Cada uno
   se marca `en-curso` antes de tocar git o el lanzador.
3. El resultado vuelve al reductor en el acto: `paso(estado, observación del
   efecto)`. Su estado y sus efectos nuevos se guardan en la misma transacción
   que confirma el efecto (`hecho`). Nunca hay un efecto confirmado cuyo
   resultado falte, ni un resultado aplicado dos veces.

Si el proceso muere antes de esa transacción, el efecto sigue sin confirmar y
el tick siguiente lo repite sobre la misma instantánea. Repetir converge: el
candidato es reproducible, la rama del tren se publica con lease vacío, el
adaptador reengancha su recibo por clave y promover es un CAS. Un `promover`
que quedó `en-curso` se reconcilia antes de volver a empujar.
"""
import subprocess

from dominio import EnBase, Rechazado, Terminal, datos, paso
import efectos as ejecutor
from inscripcion import activacion
from medicion import decision_json

# Los demás tipos (devolver, avisar-caducidad y el feedback de D6) esperan a
# una unidad posterior: quedan `pendiente`, visibles en `estado`, sin bloquear.
EJECUTABLES = frozenset(("armar-candidato", "solicitar-ci", "iniciar-suite",
                         "reanudar-suite", "repetir", "armar-subconjunto", "promover"))


class PuertaCerrada(Exception):
    def __init__(self, inscripcion):
        super().__init__(inscripcion or "no-inscrito")
        self.inscripcion = inscripcion or "no-inscrito"


def tick(ledger, repo, observador, git, hasito, ahora):
    with ledger.candado(repo):
        # La inscripción pudo cambiar mientras se esperaba el candado.
        inscripcion = activacion(repo)
        if inscripcion != "activo":
            raise PuertaCerrada(inscripcion)
        estado = ledger.operativo(repo)
        observacion = observador.observar(estado, ahora)
        nuevo, efectos = paso(estado, observacion)
        encolados = ledger.avanzar(estado, nuevo, efectos, ahora, "observacion")
        ejecutados = drenar(ledger, repo, observacion, git, hasito, ahora)
        informe = resumen(ledger.operativo(repo))
    return dict(informe, modo="activo", encolados=list(encolados), ejecutados=ejecutados)


def drenar(ledger, repo, observacion, git, hasito, ahora):
    informe = []
    for clave, previo, efecto in ledger.outbox(repo):
        if efecto.tipo not in EJECUTABLES:
            continue
        estado = ledger.operativo(repo)
        if estado.pausa:
            # Pausado no escribe: lo pendiente espera al juicio o a reanudar.
            informe.append({"clave": clave, "tipo": efecto.tipo, "resultado": "pausado"})
            break
        recuperacion = previo == "en-curso"
        ledger.marcar(repo, clave, "en-curso", "efecto-inicio",
                      {"tipo": efecto.tipo, "recuperacion": recuperacion}, ahora)
        try:
            ejecucion = None
            if recuperacion and efecto.tipo == "promover":
                # main == C aterrizó; main == B reintenta el mismo intento; otro pausa.
                ejecucion = ejecutor.reconciliar(efecto, estado, observacion, git)
                if isinstance(ejecucion.observacion.main, EnBase):
                    ejecucion = None
            if ejecucion is None:
                ejecucion = ejecutor.ejecutar(efecto, estado, observacion, git, hasito)
        except ejecutor.EfectoFallido as exc:
            fallo = {"tipo": efecto.tipo, "resultado": datos(exc.resultado)}
            ledger.marcar(repo, clave, "fallido", "efecto-fallido", fallo, ahora)
            informe.append({"clave": clave, "tipo": efecto.tipo, "resultado": "fallido",
                            "detalle": fallo["resultado"]})
            continue
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            # Sólo los ValueError llevan textos fijos; los demás pueden traer rutas.
            fallo = {"tipo": efecto.tipo, "resultado": {"clase": type(exc).__name__,
                     "detalle": str(exc) if type(exc) is ValueError else ""}}
            ledger.marcar(repo, clave, "fallido", "efecto-fallido", fallo, ahora)
            informe.append({"clave": clave, "tipo": efecto.tipo, "resultado": "fallido",
                            "detalle": fallo["resultado"]})
            continue
        llegada = datos(ejecucion.observacion)
        if not ejecucion.completo:
            # Incierto o admisión perdida: repetir el mismo efecto en otro tick.
            ledger.marcar(repo, clave, "pendiente", "efecto-diferido",
                          {"tipo": efecto.tipo, "observacion": llegada}, ahora)
            informe.append({"clave": clave, "tipo": efecto.tipo, "resultado": "diferido"})
            continue
        nuevo, efectos = paso(estado, ejecucion.observacion)
        ledger.avanzar(estado, nuevo, efectos, ahora, clave,
                       cierre=(clave, "hecho", "efecto-resultado",
                               {"tipo": efecto.tipo, "observacion": llegada}))
        informe.append({"clave": clave, "tipo": efecto.tipo, "resultado": "hecho"})
    return informe


def resumen(estado):
    """Fila FIFO, tren activo, pausa y últimos terminales del estado operativo."""
    activos = [t for t in estado.trenes if not isinstance(t.fase, Terminal)]
    por_recibo = {e.entrega.recibo: e for e in estado.entradas}
    fila = sorted((e for e in estado.entradas if e.tren is None and e.terminal is None
                   and not isinstance(e.admision, Rechazado)),
                  key=lambda e: (e.admitida is None, e.admitida or 0, e.recibida))
    tren = None
    if activos:
        t = activos[0]
        tren = {"id": t.id, "prs": [por_recibo[r].entrega.pr for r in t.recibos],
                "fase": type(t.fase).__name__, "detalle": datos(t.fase)}
    terminales = [{"id": t.id, "prs": [por_recibo[r].entrega.pr for r in t.recibos],
                   "resultado": t.fase.resultado}
                  for t in estado.trenes if isinstance(t.fase, Terminal)][-5:]
    return {"fila": [{"pr": e.entrega.pr, "head": e.entrega.head, "recibo": e.entrega.recibo,
                      "admision": decision_json(e.admision), "admitida": e.admitida}
                     for e in fila],
            "tren": tren, "pausa": datos(estado.pausa), "terminales": terminales}


def efectos_abiertos(ledger, repo):
    historial = ledger.historial_efectos(repo)
    return [dict({"clave": clave, "tipo": efecto.tipo, "estado": estado,
                  "ejecutor": efecto.tipo in EJECUTABLES},
                 **historial.get(clave, {"intentos": 0, "ultimo": None}))
            for clave, estado, efecto in ledger.outbox(repo)]
