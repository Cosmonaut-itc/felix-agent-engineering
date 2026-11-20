"""Compara los campos ajenos a la formación de lotes en los injertos históricos."""
import json
from pathlib import Path
import sys
from unittest import TestCase


def conservar_lotes_comunes(tipo, payload, comunes):
    estado = payload["estado"]
    estado["trenes"] = [t for t in estado["trenes"] if tuple(t["recibos"]) in comunes]
    ids = {t["id"] for t in estado["trenes"]}
    for entrada in estado["entradas"]:
        if tipo == "sombra-doble-camino" or entrada["tren"] not in ids:
            del entrada["tren"]
    if tipo == "sombra-doble-camino":
        # El instante de despacho forma parte del id simulado.
        for tren in estado["trenes"]:
            del tren["id"]
    # El efecto de armado se emite en el tick de despacho de cada regla.
    payload["efectos"] = [e for e in payload["efectos"] if e["tipo"] != "armar-candidato"]


def comparar(anterior, actual):
    prueba = TestCase()
    prueba.assertEqual([e[:2] for e in anterior], [e[:2] for e in actual])
    for (tipo, _, a), (_, _, b) in zip(anterior, actual):
        if tipo != "admision-entrega":
            comunes = ({tuple(t["recibos"]) for t in a["estado"]["trenes"]}
                       & {tuple(t["recibos"]) for t in b["estado"]["trenes"]})
            conservar_lotes_comunes(tipo, a, comunes)
            conservar_lotes_comunes(tipo, b, comunes)
        prueba.assertEqual(a, b)


if __name__ == "__main__":
    comparar(*(json.loads(Path(ruta).read_text()) for ruta in sys.argv[1:]))
