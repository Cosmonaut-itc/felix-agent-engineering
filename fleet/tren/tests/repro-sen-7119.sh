#!/usr/bin/env bash
set -eu

python3 -I -B - "$PWD/fleet/tren" <<'PY'
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from tempfile import mkdtemp

ruta = Path(sys.argv[1])
sys.path.insert(0, str(ruta))
from dominio import (Admitido, Candidato, Corriendo, Entrega, Entrada, Estado, Observacion,
                     Terminal, VistaPR, datos, paso)
from medicion import medir
from sombra import paso_sombra

repo = "example/project"
base, head, candidato_sha = "b" * 40, "a" * 40, "c" * 40


def entrega(pr, recibida):
    return Entrada(Entrega(repo, pr, head, "/apto.json", "/dirigida.json"), recibida)


def vista(pr, head=head):
    return VistaPR(pr, head, apto=True, hallazgos=0, dirigida=True,
                   ci="verde", conflicto=False)


def lotes(estado):
    prs = {e.entrega.recibo: e.entrega.pr for e in estado.entradas}
    return [[prs[r] for r in t.recibos] for t in estado.trenes]


def registrar(sombra, ahora, eventos):
    eventos.append(dict(seq=len(eventos), repo=repo, tipo="sombra-doble-camino",
                        instante=ahora, payload=json.dumps({"estado": datos(sombra)})))


def escenario(caduca):
    activo = sombra = Estado(repo, (entrega(1, 90),))
    eventos = []
    vistas = [vista(1)]
    obs = Observacion(100, base, tuple(vistas))
    activo, _ = paso(activo, obs)
    sombra, _ = paso_sombra(sombra, obs)
    registrar(sombra, obs.ahora, eventos)
    tren = activo.trenes[0]
    candidato = Candidato(base, ((1, head, candidato_sha),), candidato_sha, "d" * 40)
    activo = replace(activo, trenes=(replace(tren, fase=Corriendo(candidato, "suite-1")),))
    for pr, ahora in ((2, 220), (3, 340), (4, 460)):
        nueva = entrega(pr, ahora - 10)
        activo = replace(activo, entradas=activo.entradas + (nueva,))
        sombra = replace(sombra, entradas=sombra.entradas + (nueva,))
        vistas.append(vista(pr))
        if caduca and pr == 4:
            vistas[2] = vista(3, "e" * 40)
        obs = Observacion(ahora, base, tuple(vistas))
        activo, _ = paso(activo, obs)
        sombra, _ = paso_sombra(sombra, obs)
        registrar(sombra, ahora, eventos)

    print("En la suite activa, lotes (dominio / sombra):", lotes(activo), "/", lotes(sombra))
    activo = replace(activo, trenes=(replace(tren, fase=Terminal("ATERRIZADO")),))
    obs = Observacion(580, candidato_sha, tuple(vistas))
    activo, _ = paso(activo, obs)
    sombra, _ = paso_sombra(sombra, obs)
    registrar(sombra, obs.ahora, eventos)
    solicitudes = [dict(repo=repo, recibida=e.recibida, payload=json.dumps(datos(e.entrega)))
                   for e in activo.entradas]
    informe = medir(solicitudes, eventos, repo, 0, 600)
    print("Caduca PR 3:", caduca)
    print("Dominio al quedar libre:", lotes(activo))
    print("Sombra al quedar libre:", lotes(sombra))
    print("medir lotes:", [t["prs"] for t in informe["lotes"]],
          "pendientes:", informe["pendientes"],
          "suites_ahorradas_estimadas:", informe["suites_ahorradas_estimadas"])
    esperado = [[1], [2, 4]] if caduca else [[1], [2, 3, 4]]
    print("Esperado sombra y medir:", esperado, "suites_ahorradas_estimadas:",
          sum(len(lote) - 1 for lote in esperado))
    assert lotes(activo) == esperado


escenario(False)
escenario(True)

# Reproduce un evento válido de tick en seco con un lote que cierra con
# ambas reglas. Cambia sólo la base de la fase para probar el comparador.
entradas = tuple(replace(entrega(pr, 50 + pr), admitida=90, admision=Admitido())
                 for pr in (1, 2, 3))
original, efectos = paso(Estado(repo, entradas),
                         Observacion(100, base, tuple(vista(pr) for pr in (1, 2, 3))))
evento = [["sombra", 100, {"estado": datos(original),
                           "efectos": datos(tuple(e for e in efectos
                                                  if e.tipo != "avisar-en-tren"))}]]
archivos = Path(mkdtemp(prefix="pao-review-7119-"))
anterior, mutado = archivos / "anterior.json", archivos / "mutado.json"
anterior.write_text(json.dumps(evento))
fase = evento[0][2]["estado"]["trenes"][0]["fase"]
print("Mismo lote, B original:", fase["B"])
fase["B"] = "f" * 40
print("Mismo lote, B mutada:", fase["B"])
mutado.write_text(json.dumps(evento))
raiz_comparador = ruta / "tests" / "comparar_eventos_sombra.py"
cmp = subprocess.run(["cmp", "-s", str(anterior), str(mutado)], check=False)
nuevo = subprocess.run([sys.executable, "-I", "-B", str(raiz_comparador),
                        str(anterior), str(mutado)], check=False)
print("cmp (comparación anterior):", cmp.returncode,
      "comparador nuevo:", nuevo.returncode, "esperado comparador nuevo: distinto de 0")
PY
