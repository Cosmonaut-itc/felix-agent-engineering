#!/usr/bin/python3 -I
"""Respuesta terminal para los consumidores anteriores al corte a Pueue."""
import sys

MESSAGE = (
    "deskpolicy retirado el 2026-11-12: el desk toma turno con Pueue. "
    "Actualiza tu rama a origin/main y usa el lanzador del repositorio."
)

if __name__ == "__main__":
    print(MESSAGE, file=sys.stderr)
    sys.exit(64)
