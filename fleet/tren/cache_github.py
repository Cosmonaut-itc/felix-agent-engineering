"""Caché descartable de respuestas condicionales de GitHub."""
import os
from pathlib import Path
import sqlite3
from threading import Lock
import time


class CacheGitHub:
    def __init__(self, directorio):
        self.db = None
        self.candado = Lock()
        try:
            ruta = Path(directorio) / "github-cache.sqlite3"
            # Guarda cuerpos de repos privados: sólo el usuario del tren la lee.
            # Antes de abrir: un modo previo de sólo lectura haría fallar O_RDWR.
            for archivo in (ruta, Path(f"{ruta}-wal"), Path(f"{ruta}-shm")):
                if archivo.exists():
                    os.chmod(archivo, 0o600)
            os.close(os.open(ruta, os.O_CREAT | os.O_RDWR, 0o600))
            self.db = sqlite3.connect(ruta, isolation_level=None, check_same_thread=False)
            self.db.execute("PRAGMA busy_timeout=1000")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("""CREATE TABLE IF NOT EXISTS respuestas(
                endpoint TEXT PRIMARY KEY, etag TEXT NOT NULL, cuerpo TEXT NOT NULL,
                siguiente TEXT, usado REAL NOT NULL)""")
            self.db.execute("DELETE FROM respuestas WHERE usado < ?", (time.time() - 7 * 86400,))
        except (OSError, sqlite3.Error):
            self.__exit__()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self.db is not None:
            try:
                self.db.close()
            except sqlite3.Error:
                pass
            self.db = None

    def _sql(self, sql, parametros):
        with self.candado:
            if self.db is not None:
                try:
                    return self.db.execute(sql, parametros).fetchone()
                except sqlite3.Error:
                    pass

    def leer(self, endpoint):
        return self._sql("SELECT etag, cuerpo, siguiente FROM respuestas WHERE endpoint=?",
                         (endpoint,))

    def guardar(self, endpoint, etag, cuerpo, siguiente):
        if etag:
            self._sql("""INSERT OR REPLACE INTO respuestas(endpoint, etag, cuerpo, siguiente, usado)
                         VALUES (?, ?, ?, ?, ?)""", (endpoint, etag, cuerpo, siguiente, time.time()))
        else:
            self._sql("DELETE FROM respuestas WHERE endpoint=?", (endpoint,))

    def tocar(self, endpoint, etag):
        self._sql("UPDATE respuestas SET usado=? WHERE endpoint=? AND etag=?",
                  (time.time(), endpoint, etag))
