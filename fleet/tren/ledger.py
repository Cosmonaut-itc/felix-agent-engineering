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
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    @contextmanager
    def transaccion(self):
        self.db.execute("BEGIN IMMEDIATE")
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

    def estado(self, repo, sombra=False):
        estado = inicial(repo)
        if sombra:
            row = self.db.execute("SELECT payload FROM events WHERE repo=? AND tipo='sombra' ORDER BY seq DESC LIMIT 1",
                                  (repo,)).fetchone()
            if row:
                estado = desde_datos(json.loads(row[0])["estado"])
        recibos = {e.entrega.recibo for e in estado.entradas}
        nuevas = tuple(desde_datos(json.loads(r[0])) for r in self.db.execute(
            "SELECT payload FROM entries WHERE repo=? ORDER BY rowid", (repo,)))
        if sombra:
            iniciales = {e.entrega.recibo: e for e in (
                desde_datos(json.loads(r[0])) for r in self.db.execute(
                    "SELECT payload FROM events WHERE repo=? AND tipo='admision-entrega' ORDER BY seq",
                    (repo,)))}
            nuevas = tuple(iniciales.get(e.entrega.recibo, e) for e in nuevas)
        return replace(estado, entradas=estado.entradas + tuple(
            e for e in nuevas if e.entrega.recibo not in recibos))

    def sombra(self, estado, efectos, ahora):
        # Only the event is committed. No operational trains/effects/outbox rows.
        # Its tagged snapshot feeds the next shadow tick, preserving the 60m clock.
        plan = {"estado": datos(estado), "efectos": datos(efectos)}
        with self.transaccion():
            self.evento(estado.repo, "sombra", ahora, plan)
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
