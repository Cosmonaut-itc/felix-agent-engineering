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
    """Puerta del tick activo: 'activo' sólo con una inscripción explícita e inequívoca.

    modo() resuelve la duda hacia 'activo' porque allí niega merges. Aquí la
    misma duda concedería escrituras en main, así que la duda cierra la puerta:
    devuelve None (no inscrito), 'sombra' o 'ambiguo' en vez de 'activo'.
    """
    actual = modo(repo)
    if actual != "activo":
        return actual
    ruta = Path(os.environ.get("XDG_CONFIG_HOME") or
                str(Path.home() / ".config")) / "pao-tren/inscritos"
    explicita = False
    with ruta.open(encoding="utf-8") as source:
        for linea in source:
            if any(c in linea for c in "\v\f\x1c\x1d\x1e\x85\u2028\u2029"):
                if repo.lower() in linea.lower():
                    return "ambiguo"
                continue
            campos = linea.split("#", 1)[0].lower().split()
            if campos and campos[0] == repo.lower():
                if campos[1:]:
                    return "ambiguo"
                explicita = True
    return "activo" if explicita else "ambiguo"
