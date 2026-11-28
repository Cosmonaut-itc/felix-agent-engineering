"""Adelanta el tick cuando el candidato o la suite dejan de esperar."""
import errno
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import time

from cache_github import CacheGitHub
from dominio import Corriendo, Diagnosticando, EsperandoCI, clave
from inscripcion import activacion
from instalar import atomico, etiqueta
from ledger import directorio_estado, efectos_en_ejecucion
from observador import ERRORES_LECTURA, GitHubGit, LimiteGitHub, configuracion


INTERVALO_S = 20
PLAZO_S = 15
TOPE_PATEOS = 3


class LecturaCIFallida(Exception):
    """Error de lectura que el tick ordinario trataría como CI pendiente."""


class GitHubVigia(GitHubGit):
    """Conserva la clasificación de CI y distingue los fallos de lectura."""

    def api(self, endpoint, paginado=False):
        try:
            return super().api(endpoint, paginado)
        except ERRORES_LECTURA:
            raise LecturaCIFallida from None

    def ci(self, repo, head, rama=None):
        try:
            return super().ci(repo, head, rama)
        except ERRORES_LECTURA:
            raise LecturaCIFallida from None


def suites_listas(repo, tren, config):
    from adaptadores import seleccionar
    from operativo import EFECTOS_SUITE

    consultar = getattr(seleccionar(config, None), "salidas_listas", None)
    if consultar is None:
        return []
    return sorted(efecto.clave for efecto in efectos_en_ejecucion(repo)
                  if efecto.tipo in EFECTOS_SUITE and efecto.datos[0] == tren.id
                  and consultar(efecto))


def leer_memoria(ruta, llave):
    try:
        memoria = json.loads(ruta.read_text(encoding="utf-8"))
        if (isinstance(memoria, dict) and memoria.get("clave") == llave and
                type(memoria.get("pateos")) is int and 1 <= memoria["pateos"] <= TOPE_PATEOS and
                type(memoria.get("ultimo")) in (int, float) and math.isfinite(memoria["ultimo"])):
            return memoria["pateos"], memoria["ultimo"]
    except (OSError, ValueError, OverflowError, RecursionError):
        pass
    return 0, 0


def despertar(repo, ejecutar):
    servicio = f"gui/{os.getuid()}/{etiqueta(repo)}"
    consulta = ejecutar(["launchctl", "print", servicio], timeout=3,
                        capture_output=True, text=True)
    if consulta.returncode != 0:
        if consulta.returncode == errno.ESRCH or re.search(
                r"\bcould not find (?:specified )?service\b", consulta.stderr or "", re.IGNORECASE):
            return "servicio-ausente"
        return "lectura-fallida"
    estado = re.search(r"^[ \t]*state[ \t]*=[ \t]*([^\r\n]+)", consulta.stdout or "", re.MULTILINE)
    if estado is None:
        return "lectura-fallida"
    if estado[1].strip() == "running":
        return "tick-vivo"
    pateo = ejecutar(["launchctl", "kickstart", servicio], timeout=3,
                     capture_output=True, text=True)
    return "pateo" if pateo.returncode == 0 else "lectura-fallida"


def vigilar(repo, leer_estado, *, inicio=None, ejecutar_gh=None, launchctl=None):
    limite = (time.monotonic() if inicio is None else inicio) + PLAZO_S
    ejecutar_gh = ejecutar_gh or subprocess.run
    launchctl = launchctl or subprocess.run

    def acotado(ejecutar, argv, *, timeout, **opciones):
        restante = limite - time.monotonic()
        if restante <= 0:
            raise subprocess.TimeoutExpired(argv, timeout)
        return ejecutar(argv, timeout=min(timeout, restante), **opciones)

    salida = {"tren": None, "fase": None}
    try:
        directorio = directorio_estado()
        if activacion(repo) != "activo" or not (directorio / "ledger.sqlite3").exists():
            return None
        estado = leer_estado(repo)
        if estado is None:
            return None
        tren = next((t for t in estado.trenes if isinstance(t.fase, EsperandoCI)), None) or next(
            (t for t in estado.trenes if isinstance(t.fase, (Corriendo, Diagnosticando))), None)
        if tren is None:
            return None

        fase = tren.fase
        salida.update(tren=tren.id, fase=type(fase).__name__)
        config = configuracion(directorio / "config.json", repo)
        if isinstance(fase, EsperandoCI):
            with CacheGitHub(directorio) as cache:
                observador = GitHubVigia(config, activo=True, cache=cache,
                                         ejecutar=lambda argv, **kw: acotado(ejecutar_gh, argv, **kw))
                resultado = observador.ci_candidato(estado)
            if resultado is None or resultado[0].id != tren.id or resultado[1].estado == "pendiente":
                return dict(salida, vigia="sin-señal")
            clase, detalle, solicitud = "ci", resultado[1].estado, fase.solicitud
        else:
            detalle = suites_listas(repo, tren, config)
            if not detalle:
                return dict(salida, vigia="sin-señal")
            clase, solicitud = "suite", fase.ci

        salida["señal"] = clase
        llave = clave(tren.id, salida["fase"], fase.candidato.C, solicitud, clase, detalle)
        memoria = directorio / "vigia" / (hashlib.sha256(repo.encode("utf-8")).hexdigest() + ".json")
        pateos, ultimo = leer_memoria(memoria, llave)
        if pateos >= TOPE_PATEOS:
            return dict(salida, vigia="tope")
        if pateos and time.time() - ultimo < INTERVALO_S:
            return dict(salida, vigia="espera")
        motivo = despertar(repo, lambda argv, **kw: acotado(launchctl, argv, **kw))
        if motivo == "pateo":
            atomico(memoria, json.dumps({"clave": llave, "pateos": pateos + 1,
                                       "ultimo": time.time()}, sort_keys=True).encode("utf-8"))
        return dict(salida, vigia=motivo)
    except LimiteGitHub:
        return dict(salida, vigia="limite-github")
    except (*ERRORES_LECTURA, sqlite3.Error, LecturaCIFallida, IndexError, AttributeError,
            OverflowError, RecursionError):
        return dict(salida, vigia="lectura-fallida")
