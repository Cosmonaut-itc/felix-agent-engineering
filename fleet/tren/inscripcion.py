"""Registro cooperativo: sólo un modo sombra inequívoco relaja el guard."""
import os
from pathlib import Path
import re


def modo(repo):
    ruta = Path(os.environ.get("XDG_CONFIG_HOME") or
                str(Path.home() / ".config")) / "pao-tren/inscritos"
    if not os.path.lexists(ruta):
        return None
    if not ruta.stat().st_mode & 0o444:
        raise OSError("registro ilegible")
    resultado = None
    with ruta.open(encoding="utf-8") as source:
        for linea in source:
            # Igual que merge-guard: sólo CR/LF separan registros. Una línea
            # ambigua activa todos sus tokens owner/repo, incluso tras #.
            if any(c in linea for c in "\v\f\x1c\x1d\x1e\x85\u2028\u2029"):
                repos = {c for c in linea.lower().split()
                         if re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", c)}
                if repo.lower() in repos:
                    return "activo"
                continue
            campos = linea.split("#", 1)[0].lower().split()
            if campos and campos[0] == repo.lower():
                actual = "sombra" if campos[1:] == ["sombra"] else "activo"
                if actual == "activo":
                    return actual
                resultado = actual
    return resultado


def activacion(repo):
    """Permiso de escritura: sólo inscripciones explícitas sin contradicciones.

    modo() sigue cerrando el guard ante dudas. Esa política no concede permiso
    al tick: aquí las dudas devuelven «ambiguo» y nunca «activo».
    """
    ruta = Path(os.environ.get("XDG_CONFIG_HOME") or
                str(Path.home() / ".config")) / "pao-tren/inscritos"
    if not os.path.lexists(ruta):
        return None
    if not ruta.stat().st_mode & 0o444:
        raise OSError("registro ilegible")
    modos = set()
    with ruta.open(encoding="utf-8") as source:
        for linea in source:
            if any(c in linea for c in "\v\f\x1c\x1d\x1e\x85\u2028\u2029"):
                if repo.lower() in linea.lower().split():
                    return "ambiguo"
                continue
            campos = linea.split("#", 1)[0].lower().split()
            if campos and campos[0] == repo.lower():
                if campos[1:] in ([], ["activo"]):
                    modos.add("activo")
                elif campos[1:] == ["sombra"]:
                    modos.add("sombra")
                else:
                    return "ambiguo"
    return next(iter(modos)) if len(modos) == 1 else "ambiguo" if modos else None
