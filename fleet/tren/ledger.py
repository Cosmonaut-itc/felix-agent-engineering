"""Ledger local, transacciones atómicas y simulación de sombra separada."""
from collections import Counter
from contextlib import closing, contextmanager
from dataclasses import replace
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time

from dominio import Entrada, clave, datos, desde_datos, inicial


EVENTO_ABSOLUCION = "intermitente-absuelto"
VENTANA_ABSOLUCION_S = 7 * 86400


def directorio_estado():
    return Path(os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")) / "pao-tren"


def codificar(valor):
    return json.dumps(datos(valor), sort_keys=True, ensure_ascii=True)


@contextmanager
def candado_programa(exclusivo):
    """Candado entre instalar (exclusivo) y los ticks (compartido).

    Nunca espera: rinde falso, sin tomar nada, si el candado está ocupado.
    """
    raiz = directorio_estado()
    if not exclusivo and not raiz.is_dir():
        # Sin directorio de estado no hay ledger, y nadie instala sin ledger.
        yield True
        return
    # Append/open preserves the inode; never retire lock files.
    with (raiz / "programa.lock").open("a") as handle:
        try:
            fcntl.flock(handle, (fcntl.LOCK_EX if exclusivo else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class CandadoOcupado(Exception):
    """Un tick conservó el candado del repositorio hasta vencer el plazo; nada se tomó."""


INTERVALO_CANDADO_S = 0.5


def tomar_con_plazo(handle, plazo, al_esperar):
    limite = time.monotonic() + plazo
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            restante = limite - time.monotonic()
            if restante <= 0:
                raise CandadoOcupado() from None
            if al_esperar is not None:
                al_esperar()
                al_esperar = None
            time.sleep(min(INTERVALO_CANDADO_S, restante))


class Ledger:
    VERSION = 1

    def __init__(self, directorio=None):
        self.directorio = Path(directorio) if directorio is not None else directorio_estado()
        self.fd_candado = None
        self.directorio.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(str(self.directorio / "ledger.sqlite3"),
                                  isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA synchronous=FULL")
        try:
            with self.transaccion():
                version = self.db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, self.VERSION):
                    raise ValueError("versión de ledger no soportada")
                if version == 0:
                    # No executescript: it would implicitly commit the migration.
                    for sql in (
                        "CREATE TABLE requests (recibo TEXT PRIMARY KEY, repo TEXT NOT NULL, pr INTEGER NOT NULL, head TEXT NOT NULL, payload TEXT NOT NULL, recibida REAL NOT NULL)",
                        "CREATE TABLE entries (recibo TEXT PRIMARY KEY REFERENCES requests(recibo), repo TEXT NOT NULL, payload TEXT NOT NULL)",
                        "CREATE TABLE trains (id TEXT PRIMARY KEY, repo TEXT NOT NULL, payload TEXT NOT NULL)",
                        "CREATE TABLE effects (clave TEXT PRIMARY KEY, repo TEXT NOT NULL, payload TEXT NOT NULL)",
                        "CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT NOT NULL, tipo TEXT NOT NULL, instante REAL NOT NULL, payload TEXT NOT NULL)",
                        "CREATE TABLE outbox (clave TEXT PRIMARY KEY REFERENCES effects(clave), estado TEXT NOT NULL)",
                        "CREATE INDEX events_repo ON events(repo, seq)",
                    ):
                        self.db.execute(sql)
                    self.db.execute("PRAGMA user_version=1")
                # Extensión aditiva del esquema 1: los eventos/snapshots antiguos
                # siguen leyéndose sin transformar la historia de sombra.
                self.db.execute("CREATE TABLE IF NOT EXISTS effect_results ("
                                "clave TEXT PRIMARY KEY REFERENCES effects(clave), "
                                "payload TEXT NOT NULL)")
                self.db.execute("CREATE TABLE IF NOT EXISTS decisions ("
                                "id TEXT PRIMARY KEY, repo TEXT NOT NULL, payload TEXT NOT NULL, "
                                "estado TEXT NOT NULL, resultado TEXT)")
                self.db.execute("CREATE TABLE IF NOT EXISTS snapshots_operador ("
                                "ruta TEXT PRIMARY KEY, repo TEXT NOT NULL, checkout TEXT NOT NULL)")
                self.db.execute("CREATE TABLE IF NOT EXISTS snapshots_reservados (ruta TEXT PRIMARY KEY)")
                self.db.execute("CREATE TABLE IF NOT EXISTS snapshots_usados ("
                                "ruta TEXT NOT NULL REFERENCES snapshots_operador(ruta) ON DELETE CASCADE, "
                                "tren TEXT NOT NULL, PRIMARY KEY(ruta, tren))")
                self.db.execute("CREATE TABLE IF NOT EXISTS snapshots_lanzamientos ("
                                "ruta TEXT NOT NULL REFERENCES snapshots_operador(ruta) ON DELETE CASCADE, "
                                "tren TEXT NOT NULL, recibo TEXT NOT NULL, PRIMARY KEY(ruta, tren, recibo))")
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    @contextmanager
    def transaccion(self, escritura=True):
        self.db.execute("BEGIN IMMEDIATE" if escritura else "BEGIN")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    @contextmanager
    def candado(self, repo, plazo=None, al_esperar=None):
        """Sin plazo espera lo que haga falta; con plazo lanza CandadoOcupado al vencerlo.

        al_esperar corre una vez, sólo si el candado está tomado al llegar.
        """
        # Append/open preserves the inode; never retire lock files.
        with (self.directorio / (clave("repo", repo) + ".lock")).open("a") as handle:
            if plazo is None:
                fcntl.flock(handle, fcntl.LOCK_EX)
            else:
                tomar_con_plazo(handle, plazo, al_esperar)
            self.fd_candado = handle.fileno()
            try:
                yield
            finally:
                self.fd_candado = None
                fcntl.flock(handle, fcntl.LOCK_UN)

    def entregar(self, entrega, ahora, sombra_entrega=None):
        with self.transaccion():
            existente = self.db.execute("SELECT payload FROM requests WHERE recibo=?",
                                       (entrega.recibo,)).fetchone()
            if existente:
                if existente[0] != codificar(entrega):
                    raise ValueError("entrega existente con referencias distintas")
                return entrega.recibo
            self.db.execute("INSERT INTO requests VALUES (?, ?, ?, ?, ?, ?)",
                            (entrega.recibo, entrega.repo, entrega.pr, entrega.head,
                             codificar(entrega), ahora))
            self.db.execute("INSERT INTO entries VALUES (?, ?, ?)",
                            (entrega.recibo, entrega.repo, codificar(Entrada(entrega, ahora))))
            self.evento(entrega.repo, "custodia", ahora, {"recibo": entrega.recibo})
            if sombra_entrega is not None:
                self.evento(entrega.repo, "admision-entrega", ahora, datos(sombra_entrega))
        return entrega.recibo

    def evento(self, repo, tipo, ahora, payload):
        self.db.execute("INSERT INTO events(repo, tipo, instante, payload) VALUES (?, ?, ?, ?)",
                        (repo, tipo, ahora, json.dumps(payload, sort_keys=True)))

    def absoluciones_recientes(self, repo, ahora):
        filas = self.db.execute(
            "SELECT payload FROM events WHERE repo=? AND tipo=? AND instante BETWEEN ? AND ?",
            (repo, EVENTO_ABSOLUCION, ahora - VENTANA_ABSOLUCION_S, ahora))
        conteo = Counter(json.loads(fila[0])["unidad"] for fila in filas)
        return tuple(sorted(conteo.items()))

    def estado(self, repo, sombra=False, doble_camino=False, operativo=False):
        estado = inicial(repo)
        if not sombra:
            row = self.db.execute(
                "SELECT payload FROM events WHERE repo=? AND tipo='operativo' ORDER BY seq DESC LIMIT 1",
                (repo,)).fetchone()
            if row:
                estado = desde_datos(json.loads(row[0])["estado"])
        if sombra:
            tipo = "sombra-doble-camino" if doble_camino else "sombra"
            for row in self.db.execute(
                    "SELECT payload FROM events WHERE repo=? AND tipo=? ORDER BY seq DESC",
                    (repo, tipo)):
                estado = desde_datos(json.loads(row[0])["estado"])
                break
        recibos = {e.entrega.recibo for e in estado.entradas}
        nuevas = tuple(desde_datos(json.loads(r[0])) for r in self.db.execute(
            "SELECT payload FROM entries WHERE repo=? ORDER BY rowid", (repo,)))
        if sombra or operativo:
            iniciales = {e.entrega.recibo: e for e in map(desde_datos, admisiones_entrega(self.db, repo))}
            nuevas = tuple(iniciales.get(e.entrega.recibo, e) for e in nuevas)
        return replace(estado, entradas=estado.entradas + tuple(
            e for e in nuevas if e.entrega.recibo not in recibos))

    def guardar_paso(self, estado, efectos, ahora, consumido=None, fallo_operador=None, *,
                     prelanzadas=(), suites_sin_leer=()):
        """Snapshot, proyecciones, outbox y ACK del resultado: un solo COMMIT."""
        with self.transaccion():
            for aviso in prelanzadas:
                self.evento(estado.repo, "suite-prelanzada-sin-leer", ahora, aviso)
            for aviso in suites_sin_leer:
                self.evento(estado.repo, "suite-sin-leer", ahora, aviso)
            # entries es custodia inmutable: la sombra también la importa.
            # La continuación operativa completa vive sólo en su evento.
            for tren in estado.trenes:
                self.db.execute("INSERT INTO trains VALUES (?, ?, ?) ON CONFLICT(id) "
                                "DO UPDATE SET payload=excluded.payload",
                                (tren.id, estado.repo, codificar(tren)))
            for efecto in efectos:
                if efecto.tipo == "registrar-absolucion":
                    self.evento(estado.repo, EVENTO_ABSOLUCION, ahora, json.loads(efecto.datos[1]))
                    continue
                if efecto.tipo == "registrar-relanzamiento-automatico":
                    tren_id, numero, limite, detalle, evidencia = efecto.datos
                    self.evento(estado.repo, "relanzamiento-automatico", ahora, {
                        "tren": tren_id, "numero": int(numero), "limite": int(limite),
                        "detalle": detalle, "evidencia": evidencia})
                    continue
                if efecto.tipo == "resolver-juicio":
                    identificador, resultado, motivo = efecto.datos
                    actualizado = self.db.execute(
                        "UPDATE decisions SET estado=?, resultado=? "
                        "WHERE id=? AND repo=? AND estado='pendiente'",
                        (resultado, json.dumps({"motivo": motivo, "instante": ahora}),
                         identificador, estado.repo))
                    if actualizado.rowcount != 1:
                        raise ValueError("decisión no pendiente")
                    self.evento(estado.repo, "juicio-resuelto", ahora,
                                {"id": identificador, "resultado": resultado, "motivo": motivo})
                    continue
                previo = self.db.execute("SELECT repo, payload FROM effects WHERE clave=?",
                                         (efecto.clave,)).fetchone()
                if previo and tuple(previo) != (estado.repo, codificar(efecto)):
                    raise ValueError("colisión de clave de efecto")
                self.db.execute("INSERT OR IGNORE INTO effects VALUES (?, ?, ?)",
                                (efecto.clave, estado.repo, codificar(efecto)))
                self.db.execute("INSERT OR IGNORE INTO outbox VALUES (?, 'pendiente')",
                                (efecto.clave,))
            if consumido is not None:
                self.db.execute("UPDATE outbox SET estado='completo' WHERE clave=? AND estado='resultado'",
                                (consumido,))
            if fallo_operador is not None:
                efecto, detalle = fallo_operador
                self._guardar_resultado(estado.repo, efecto, ahora,
                    {"error": {"codigo": "checkout-operador", "detalle": detalle}})
            # Una nueva generación no puede consumir ni recuperar IO de la
            # anterior. El feedback conserva el orden y se drena igualmente.
            from dominio import Terminal
            from feedback import FEEDBACK
            terminales = {t.id for t in estado.trenes if isinstance(t.fase, Terminal)}
            cancelados = []
            for fila in self.outbox(estado.repo):
                efecto = desde_datos(json.loads(fila["payload"]))
                if (efecto.tipo not in FEEDBACK and efecto.datos and
                        efecto.datos[0] in terminales):
                    self.db.execute("UPDATE outbox SET estado='cancelado' WHERE clave=?", (efecto.clave,))
                    cancelados.append(efecto.clave)
            if cancelados:
                self.evento(estado.repo, "efectos-cancelados", ahora, {"claves": cancelados})
            self.evento(estado.repo, "operativo", ahora,
                        {"estado": datos(estado), "efectos": datos(efectos), "consumido": consumido})

    def terminales_hasta(self, repo, instante):
        from dominio import Terminal
        fila = self.db.execute("SELECT payload FROM events WHERE repo=? AND tipo='operativo' "
                               "AND instante<=? ORDER BY seq DESC LIMIT 1", (repo, instante)).fetchone()
        if fila is None:
            return set()
        estado = desde_datos(json.loads(fila[0])["estado"])
        return {tren.id for tren in estado.trenes if isinstance(tren.fase, Terminal)}

    def efecto_iniciado(self, repo, clave, evidencia=None):
        """El efecto pasó por iniciar_efecto en algún tick: su IO pudo ocurrir.

        Un efecto fallido pudo no llegar a su IO (falla antes de lanzar). Con
        `evidencia`, sólo cuenta si esa lectura sin red confirma que arrancó.
        """
        fila = self.db.execute(
            "SELECT o.estado FROM effects e JOIN outbox o ON o.clave=e.clave "
            "WHERE e.repo=? AND e.clave=? AND o.estado!='pendiente'", (repo, clave)).fetchone()
        if fila is None:
            return False
        return fila[0] != "fallido" or evidencia is None or evidencia()

    def primer_inicio(self, repo, clave):
        """Secuencia del primer efecto-iniciado de la clave, o None."""
        return self.db.execute(
            "SELECT MIN(seq) FROM events WHERE repo=? AND tipo='efecto-iniciado' "
            "AND json_extract(payload, '$.clave')=?", (repo, clave)).fetchone()[0]

    def outbox(self, repo):
        return [dict(r) for r in self.db.execute(
            "SELECT e.clave, e.payload, o.estado, r.payload AS resultado FROM effects e "
            "JOIN outbox o ON o.clave=e.clave LEFT JOIN effect_results r ON r.clave=e.clave "
            "WHERE e.repo=? AND o.estado NOT IN ('completo', 'cancelado') ORDER BY e.rowid", (repo,))]

    def pausa_id(self, repo, estado=None):
        """Identidad de la aparición de una pausa, aun si se repite su texto.

        No añade campos al contrato de Estado/Pausa ni a eventos de sombra.
        Una sucesión de ticks pausados conserva la primera secuencia.
        """
        from juicios import tren_pausado
        estado = estado or self.estado(repo, operativo=True)
        if estado.pausa is None:
            return ""
        def firma(e):
            tren = tren_pausado(e)
            return (e.pausa, tren.id if tren else None)
        actual = firma(estado)
        identificador = clave("pausa-inicial", repo, datos(estado.pausa))
        for fila in self.db.execute(
                "SELECT seq, payload FROM events WHERE repo=? AND tipo='operativo' ORDER BY seq DESC",
                (repo,)):
            anterior = desde_datos(json.loads(fila["payload"])["estado"])
            if firma(anterior) != actual:
                break
            identificador = clave("pausa", repo, fila["seq"])
        return identificador

    def registrar_decision(self, decision):
        """El llamador conserva el flock; sólo datos, ningún efecto externo."""
        with self.transaccion():
            self.db.execute("INSERT INTO decisions VALUES (?, ?, ?, 'pendiente', NULL)",
                            (decision.id, decision.repo, codificar(decision)))
            self.evento(decision.repo, "juicio-registrado", decision.instante, datos(decision))

    def decisiones(self, repo):
        return [{"decision": json.loads(r["payload"]), "estado": r["estado"],
                 "resultado": json.loads(r["resultado"]) if r["resultado"] else None}
                for r in self.db.execute("SELECT * FROM decisions WHERE repo=? ORDER BY rowid", (repo,))]

    def siguiente_decision(self, repo):
        fila = self.db.execute("SELECT payload FROM decisions WHERE repo=? AND estado='pendiente' "
                               "ORDER BY rowid LIMIT 1", (repo,)).fetchone()
        return desde_datos(json.loads(fila[0])) if fila else None

    def iniciar_efecto(self, repo, efecto, ahora, *, limpiar_resultado=False):
        # Durable antes del IO: tras una muerte se reconcilia antes del paso.
        with self.transaccion():
            self.db.execute("UPDATE outbox SET estado='ejecutando' WHERE clave=?", (efecto.clave,))
            if limpiar_resultado:
                self.db.execute("DELETE FROM effect_results WHERE clave=?", (efecto.clave,))
            self.evento(repo, "efecto-iniciado", ahora, {"clave": efecto.clave})

    def registrar_efecto(self, repo, efecto, ahora, resultado, completo=False,
                         sin_consumo=False):
        """Feedback cierra directamente: no produce observación para el reductor."""
        with self.transaccion():
            self._guardar_resultado(repo, efecto, ahora, resultado, completo, sin_consumo)

    def _guardar_resultado(self, repo, efecto, ahora, resultado, completo=False,
                           sin_consumo=False):
        self.db.execute("INSERT INTO effect_results VALUES (?, ?) ON CONFLICT(clave) "
                        "DO UPDATE SET payload=excluded.payload",
                        (efecto.clave, json.dumps(resultado, sort_keys=True)))
        self.db.execute("UPDATE outbox SET estado=? WHERE clave=?",
                        (("completo" if sin_consumo else "resultado")
                         if completo else "fallido", efecto.clave))
        self.evento(repo, "efecto-resultado" if completo else "efecto-fallido", ahora,
                    {"clave": efecto.clave, "resultado": resultado})

    def sombra(self, estado, efectos, ahora, doble_camino=False):
        # Only the event is committed. No operational trains/effects/outbox rows.
        # Its tagged snapshot feeds the next shadow tick and preserves admissions.
        plan = {"estado": datos(estado), "efectos": datos(efectos)}
        with self.transaccion():
            self.evento(estado.repo, "sombra-doble-camino" if doble_camino else "sombra",
                        ahora, plan)
        return plan

    def ultimos_eventos(self, repo):
        return [dict(r) for r in self.db.execute(
            "SELECT seq, tipo, instante, payload FROM events WHERE repo=? ORDER BY seq DESC LIMIT 10", (repo,))]


def admisiones_entrega(db, repo):
    for raw, in db.execute(
            "SELECT payload FROM events WHERE repo=? AND tipo='admision-entrega' ORDER BY seq", (repo,)):
        yield json.loads(raw)


@contextmanager
def lectura_medicion(directorio=None, *, timeout=5.0):
    """SQLite mode=ro: sin mkdir, migraciones, candado ni pragmas de escritura."""
    raiz = Path(directorio) if directorio is not None else directorio_estado()
    ruta = (raiz / "ledger.sqlite3").resolve()
    with closing(sqlite3.connect(ruta.as_uri() + "?mode=ro", uri=True, timeout=timeout)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        if db.execute("PRAGMA user_version").fetchone()[0] != Ledger.VERSION:
            raise ValueError("versión de ledger no soportada")
        yield db


def filas_medicion(repo, directorio=None):
    with lectura_medicion(directorio) as db:
        requests = [dict(r) for r in db.execute(
            "SELECT * FROM requests WHERE repo=? ORDER BY recibida, recibo", (repo,))]
        events = [dict(r) for r in db.execute(
            "SELECT * FROM events WHERE repo=? ORDER BY seq", (repo,))]
    return requests, events


def filas_fases(repo, directorio=None):
    with lectura_medicion(directorio) as db:
        events = [dict(r) for r in db.execute(
            "SELECT * FROM events WHERE repo=? AND tipo IN "
            "('operativo', 'efecto-iniciado', 'efecto-resultado') ORDER BY seq", (repo,))]
        effects = [dict(r) for r in db.execute(
            "SELECT * FROM effects WHERE repo=?", (repo,))]
    return events, effects
