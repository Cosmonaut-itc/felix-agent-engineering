#!/usr/bin/env python3
"""Prepara un LaunchAgent por repo para tick --seco; cargar es explícito."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import subprocess
import tempfile
from typing import Iterator, Optional
import uuid


def repositorio(valor: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", valor):
        raise argparse.ArgumentTypeError("repo debe ser owner/repo")
    return valor


def etiqueta(repo: str) -> str:
    # El hash evita colisiones entre nombres con puntos, guiones o barras.
    return "local.pao.tren." + hashlib.sha256(repo.encode("utf-8")).hexdigest()


def entorno(home: Path) -> dict[str, str]:
    detectadas = set()
    for programa in ("python3", "gh", "git"):
        ruta = shutil.which(programa)
        if ruta is None:
            raise ValueError("falta en PATH: " + programa)
        detectadas.add(str(Path(ruta).absolute().parent))
    # Mantener la precedencia original: el directorio de python3 también puede
    # contener otros gh/git y no debe adelantarse a los ejecutables detectados.
    rutas = [str(Path(p).absolute()) for p in os.get_exec_path()
             if str(Path(p).absolute()) in detectadas]
    rutas.extend(str(p) for p in (home / ".local/bin", Path("/opt/homebrew/bin"),
                  Path("/usr/local/bin"), Path("/usr/bin"), Path("/bin"),
                  Path("/usr/sbin"), Path("/sbin")))
    resultado = {"HOME": str(home), "PATH": os.pathsep.join(dict.fromkeys(rutas))}
    for variable, defecto in (("XDG_STATE_HOME", ".local/state"),
                              ("XDG_CONFIG_HOME", ".config"),
                              ("XDG_DATA_HOME", ".local/share"),
                              ("XDG_CACHE_HOME", ".cache")):
        ruta = Path(os.environ.get(variable) or home / defecto)
        if not ruta.is_absolute():
            raise ValueError(variable + " debe ser absoluto")
        resultado[variable] = str(ruta)
    return resultado


def renderizar(repo: str, home: Path) -> dict:
    variables = entorno(home)
    logs = Path(variables["XDG_STATE_HOME"]) / "pao-tren/launchd" / etiqueta(repo)
    return {
        "Label": etiqueta(repo),
        "ProgramArguments": [str(home / ".local/bin/pao-tren"),
                             "tick", "--repo", repo, "--seco"],
        "StartInterval": 120,
        "RunAtLoad": True,
        "StandardOutPath": str(logs / "stdout.log"),
        "StandardErrorPath": str(logs / "stderr.log"),
        "EnvironmentVariables": variables,
    }


def sello() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f") + "-" + uuid.uuid4().hex


def atomico(ruta: Path, contenido: bytes) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporal = tempfile.mkstemp(prefix=ruta.name + ".", suffix=".tmp",
                                           dir=str(ruta.parent))
    # Si falla la escritura se conserva el temporal para inspección; no se borra nada.
    with os.fdopen(descriptor, "wb") as salida:
        salida.write(contenido)
        salida.flush()
        os.fsync(salida.fileno())
    os.replace(temporal, ruta)


@contextmanager
def bloquear(estado: Path, label: str) -> Iterator[None]:
    estado.mkdir(parents=True, exist_ok=True)
    with (estado / (label + ".instalacion.lock")).open("a") as candado:
        fcntl.flock(candado, fcntl.LOCK_EX)
        yield


def ejecutar(argv: list[str], *, comprobar: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(argv, check=comprobar, capture_output=True, text=True, timeout=30)


def detener(servicio: str) -> bool:
    cargado = ejecutar(["launchctl", "print", servicio], comprobar=False).returncode == 0
    if cargado:
        ejecutar(["launchctl", "bootout", servicio])
    return cargado


def instalar(repo: str, retirar: bool = False, cargar: bool = False) -> None:
    home = Path.home()
    label = etiqueta(repo)
    ruta = home / "Library/LaunchAgents" / (label + ".plist")
    estado = Path(os.environ.get("XDG_STATE_HOME") or home / ".local/state") / "pao-tren"
    if not estado.is_absolute():
        raise ValueError("XDG_STATE_HOME debe ser absoluto")
    dominio = "gui/" + str(os.getuid())
    servicio = dominio + "/" + label
    bootstrap = ["launchctl", "bootstrap", dominio, str(ruta)]
    with bloquear(estado, label):
        if retirar:
            detener(servicio)
            if ruta.exists():
                destino = ruta.with_name(ruta.name + ".retirado-" + sello())
                ruta.rename(destino)
                print("retirado: " + str(destino))
            return
        unidad = renderizar(repo, home)
        binario = Path(unidad["ProgramArguments"][0])
        if not binario.is_file() or not os.access(binario, os.X_OK):
            raise ValueError("instala primero pao-tren con bin/pao sync --no-plugins --no-schedule")
        contenido = plistlib.dumps(unidad, sort_keys=True)
        cambiado = not ruta.exists() or ruta.read_bytes() != contenido
        Path(unidad["StandardOutPath"]).parent.mkdir(parents=True, exist_ok=True)
        if cambiado:
            if ruta.exists():
                respaldo = ruta.with_name(ruta.name + ".bak-" + sello())
                shutil.copy2(ruta, respaldo)
                print("respaldo: " + str(respaldo))
            atomico(ruta, contenido)
        print("plist: " + str(ruta))
        print(shlex.join(bootstrap))
        if cargar:
            # El servicio puede seguir con un plist anterior preparado sin --cargar.
            detener(servicio)
            ejecutar(bootstrap)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=repositorio)
    accion = parser.add_mutually_exclusive_group()
    accion.add_argument("--retirar", action="store_true", help="bootout y renombrar, sin borrar")
    accion.add_argument("--cargar", action="store_true", help="ejecutar también launchctl bootstrap")
    args = parser.parse_args(argv)
    try:
        instalar(args.repo, args.retirar, args.cargar)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(2, "error de instalación: " + str(exc) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
