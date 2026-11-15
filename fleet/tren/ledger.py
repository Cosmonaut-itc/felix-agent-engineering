"""Ledger local, transacciones atómicas y simulación de sombra separada."""
from contextlib import closing, contextmanager
from dataclasses import replace
import fcntl
import json
import os
from pathlib import Path
import sqlite3

from dominio import Entrada, clave, datos, desde_datos, inicial


def directorio_estado():
    return Path(os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")) / "pao-tren"


def codificar(valor):
    return json.dumps(datos(valor), sort_keys=True, ensure_ascii=True)


class Ledger:
    VERSION = 1

    def __init__(self, directorio=None):
        self.directorio = Path(directorio) if directorio is not None else directorio_estado()
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
                # U15, también aditiva: decisiones de juicio y pausa manual.
                # `pendiente` hasta el COMMIT del paso que la consume.
                self.db.execute("CREATE TABLE IF NOT EXISTS decisions ("
                                "id TEXT PRIMARY KEY, repo TEXT NOT NULL, "
                                "payload TEXT NOT NULL, estado TEXT NOT NULL, "
                                "resultado TEXT)")
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
    def candado(self, repo):
        # Append/open preserves the inode; never retire lock files.
        with (self.directorio / (clave("repo", repo) + ".lock")).open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
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
            iniciales = {e.entrega.recibo: e for e in (
                desde_datos(json.loads(r[0])) for r in self.db.execute(
                    "SELECT payload FROM events WHERE repo=? AND tipo='admision-entrega' ORDER BY seq",
                    (repo,)))}
            nuevas = tuple(iniciales.get(e.entrega.recibo, e) for e in nuevas)
        return replace(estado, entradas=estado.entradas + tuple(
            e for e in nuevas if e.entrega.recibo not in recibos))

    def registrar_decision(self, decision):
        """Una sola pendiente por repo; el llamador mantiene el candado del repo."""
        with self.transaccion():
            if self.decision_pendiente(decision.repo) is not None:
                raise ValueError("decisión pendiente")
            self.db.execute("INSERT INTO decisions VALUES (?, ?, ?, 'pendiente', NULL)",
                            (decision.id, decision.repo, codificar(decision)))
            self.evento(decision.repo, "decision", decision.instante, datos(decision))

    def decision_pendiente(self, repo):
        row = self.db.execute("SELECT payload FROM decisions WHERE repo=? AND "
                              "estado='pendiente' ORDER BY rowid LIMIT 1", (repo,)).fetchone()
        return desde_datos(json.loads(row[0])) if row else None

    def decisiones(self, repo):
        return [{"decision": json.loads(r["payload"]), "estado": r["estado"],
                 "resultado": r["resultado"]} for r in self.db.execute(
            "SELECT payload, estado, resultado FROM decisions WHERE repo=? ORDER BY rowid",
            (repo,))]

    def guardar_paso(self, estado, efectos, ahora, consumido=None, decision=None,
                     rechazo=""):
        """Snapshot, proyecciones, outbox, ACK y decisión consumida: un solo COMMIT."""
        with self.transaccion():
            if decision is not None:
                cambio = self.db.execute(
                    "UPDATE decisions SET estado=?, resultado=? WHERE id=? AND estado='pendiente'",
                    ("rechazada" if rechazo else "aplicada", rechazo or None, decision.id))
                if cambio.rowcount != 1:
                    raise ValueError("decisión ya consumida")
            # entries es custodia inmutable: la sombra también la importa.
            # La continuación operativa completa vive sólo en su evento.
            for tren in estado.trenes:
                self.db.execute("INSERT INTO trains VALUES (?, ?, ?) ON CONFLICT(id) "
                                "DO UPDATE SET payload=excluded.payload",
                                (tren.id, estado.repo, codificar(tren)))
            for efecto in efectos:
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
            plan = {"estado": datos(estado), "efectos": datos(efectos), "consumido": consumido}
            if decision is not None:
                plan["decision"] = {"id": decision.id, "rechazo": rechazo or None}
            self.evento(estado.repo, "operativo", ahora, plan)

    def outbox(self, repo):
        return [dict(r) for r in self.db.execute(
            "SELECT e.clave, e.payload, o.estado, r.payload AS resultado FROM effects e "
            "JOIN outbox o ON o.clave=e.clave LEFT JOIN effect_results r ON r.clave=e.clave "
            "WHERE e.repo=? AND o.estado!='completo' ORDER BY e.rowid", (repo,))]

    def iniciar_efecto(self, repo, efecto, ahora):
        # Durable antes del IO: tras una muerte se reconcilia antes del paso.
        with self.transaccion():
            self.db.execute("UPDATE outbox SET estado='ejecutando' WHERE clave=?", (efecto.clave,))
            self.evento(repo, "efecto-iniciado", ahora, {"clave": efecto.clave})

    def registrar_efecto(self, repo, efecto, ahora, resultado, completo=False,
                         sin_consumo=False):
        """Feedback cierra directamente: no produce observación para el reductor."""
        with self.transaccion():
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
        # Its tagged snapshot feeds the next shadow tick, preserving the 60m clock.
        plan = {"estado": datos(estado), "efectos": datos(efectos)}
        with self.transaccion():
            self.evento(estado.repo, "sombra-doble-camino" if doble_camino else "sombra",
                        ahora, plan)
        return plan

    def ultimos_eventos(self, repo):
        return [dict(r) for r in self.db.execute(
            "SELECT seq, tipo, instante, payload FROM events WHERE repo=? ORDER BY seq DESC LIMIT 10", (repo,))]


def filas_medicion(repo, directorio=None):
    """SQLite mode=ro: sin mkdir, migraciones, candado ni pragmas de escritura."""
    raiz = Path(directorio) if directorio is not None else directorio_estado()
    ruta = (raiz / "ledger.sqlite3").resolve()
    with closing(sqlite3.connect(ruta.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        if db.execute("PRAGMA user_version").fetchone()[0] != Ledger.VERSION:
            raise ValueError("versión de ledger no soportada")
        requests = [dict(r) for r in db.execute(
            "SELECT * FROM requests WHERE repo=? ORDER BY recibida, recibo", (repo,))]
        events = [dict(r) for r in db.execute(
            "SELECT * FROM events WHERE repo=? ORDER BY seq", (repo,))]
    return requests, events
