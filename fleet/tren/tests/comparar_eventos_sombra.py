"""Compara los campos ajenos a la formación de lotes en los injertos históricos."""
import json
from pathlib import Path
import sys
from unittest import TestCase


def eventos_sin_lotes(ruta):
    eventos = json.loads(Path(ruta).read_text())
    for tipo, _, payload in eventos:
        if tipo == "admision-entrega":
            continue
        estado = payload["estado"]
        del estado["trenes"]
        for entrada in estado["entradas"]:
            del entrada["tren"]
        payload["efectos"] = [e for e in payload["efectos"] if e["tipo"] != "armar-candidato"]
    return eventos


TestCase().assertEqual(eventos_sin_lotes(sys.argv[1]), eventos_sin_lotes(sys.argv[2]))
