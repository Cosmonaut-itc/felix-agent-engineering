"""Registro cooperativo: sólo un modo sombra inequívoco relaja el guard."""
import os
from pathlib import Path


def modo(repo):
    ruta = Path(os.environ.get("XDG_CONFIG_HOME") or
                str(Path.home() / ".config")) / "pao-tren/inscritos"
    if not os.path.lexists(ruta):
        return None
    if not ruta.stat().st_mode & 0o444:
        raise OSError("registro ilegible")
    resultado = None
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        campos = linea.split("#", 1)[0].lower().split()
        if campos and campos[0] == repo.lower():
            actual = "sombra" if campos[1:] == ["sombra"] else "activo"
            if actual == "activo":
                return actual
            resultado = actual
    return resultado
