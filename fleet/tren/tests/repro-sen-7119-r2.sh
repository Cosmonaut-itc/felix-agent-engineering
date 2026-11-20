#!/usr/bin/env bash
set -eu

python3 -I -B - "$PWD/fleet/tren" <<'PY'
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import mkdtemp
import sys

sys.path.insert(0, sys.argv[1])
sys.path.insert(0, str(Path(sys.argv[1]) / 'tests'))
from comparar_eventos_sombra import comparar
from dominio import (Admitido, Aterrizado, Certificado, Entrega, Entrada,
                     Observacion, Promoviendo, Terminal, VistaPR, datos, paso)
from ledger import Ledger, filas_medicion
from medicion import medir
from sombra import DURACION_SUITE_ESTIMADA, paso_sombra

REPO = 'example/project'
BASE, HEAD, C, ARBOL = (letra * 40 for letra in 'bacd')


def entrega(ledger, pr, cuando, admitida=False):
    d = Entrega(REPO, pr, HEAD, '/apto.json', '/dirigida.json')
    entrada = Entrada(d, cuando, Admitido(), cuando) if admitida else None
    ledger.entregar(d, cuando, entrada)
    return d.recibo


def vista(pr, **cambios):
    return replace(VistaPR(pr, HEAD, apto=True, hallazgos=0, dirigida=True,
                          ci='verde', conflicto=False), **cambios)


def operativo(ledger, obs):
    estado, efectos = paso(ledger.estado(REPO, operativo=True), obs)
    ledger.guardar_paso(estado, efectos, obs.ahora)
    return estado


def simular(ledger, obs):
    estado, efectos = paso_sombra(ledger.estado(REPO, sombra=True, doble_camino=True), obs)
    ledger.sombra(estado, efectos, obs.ahora, doble_camino=True)
    return estado


def informe(ledger, hasta):
    return medir(*filas_medicion(REPO, ledger.directorio), REPO, 0, hasta)


def lotes(inf):
    return [(lote['instante'], lote['prs']) for lote in inf['lotes']]


ledger = Ledger(mkdtemp(prefix='sen-7119-fin-'))
entrega(ledger, 1, 90)
inicial = operativo(ledger, Observacion(100, BASE, (vista(1),)))
certificado = Certificado(C, ARBOL, 'suite-1', 'verde', BASE)
tren = replace(inicial.trenes[0], fase=certificado)
ledger.guardar_paso(replace(inicial, trenes=(tren,)), (), 220)
recibo2 = entrega(ledger, 2, 230)
promocion = operativo(ledger, Observacion(280, BASE, (vista(1), vista(2))))
assert isinstance(promocion.trenes[0].fase, Promoviendo)
assert promocion.entradas[1].admitida == 280
obs = Observacion(340, C, (vista(1, abierto=False, mergeado=True), vista(2)),
                  main=Aterrizado(BASE, C, ARBOL), tren_id=tren.id, intento_promocion=1)
cierre = operativo(ledger, obs)
assert cierre.trenes[0].fase == Terminal('ATERRIZADO') and len(cierre.trenes) == 1
intermedio = informe(ledger, 341)
real = operativo(ledger, replace(obs, ahora=460, main=None, tren_id='', intento_promocion=0))
assert real.trenes[1].recibos == (recibo2,)
print('B1 tras ACK / medir:', lotes(intermedio), 'pendientes:', len(intermedio['pendientes']))
print('B1 tras ACK / esperado:', [(100, [1])], 'pendientes:', 1)
print('B1 tras tick siguiente / medir:', lotes(informe(ledger, 461)))
print('B1 tras tick siguiente / dominio:', [(100, [1]), (460, [2])])
ledger.close()

ledger = Ledger(mkdtemp(prefix='sen-7119-modos-'))
recibo = entrega(ledger, 1, 100, admitida=True)
sombra = simular(ledger, Observacion(100, BASE, (vista(1),)))
assert sombra.trenes[0].recibos == (recibo,)
antes = lotes(informe(ledger, 5000))
activo = operativo(ledger, Observacion(5000, BASE, (vista(1, abierto=False),)))
assert not activo.trenes
print('B2 lote simulado antes de activar:', antes)
print('B2 lote simulado después de activar y observar cierre / medir:', lotes(informe(ledger, 5001)))
print('B2 después de activar / esperado (lote ya cerrado):', antes)
ledger.close()

ledger = Ledger(mkdtemp(prefix='sen-7119-retencion-'))
entrega(ledger, 1, 100, admitida=True)
simular(ledger, Observacion(100, BASE, (vista(1),)))
recibo2 = entrega(ledger, 2, 220, admitida=True)
simular(ledger, Observacion(220, BASE, (vista(1), vista(2))))
libre = 100 + DURACION_SUITE_ESTIMADA
obs = Observacion(340, BASE, (vista(1), vista(2, ci='pendiente')))
simular(ledger, obs)
sombra = simular(ledger, replace(obs, ahora=libre))
entrada2 = next(e for e in sombra.entradas if e.entrega.recibo == recibo2)
print('B3 PR 2 al quedar libre:', type(entrada2.admision).__name__, entrada2.admision.motivo.value)
print('B3 sombra despacha PR 2:', any(recibo2 in t.recibos for t in sombra.trenes), 'esperado: False')
print('B3 medir:', lotes(informe(ledger, libre + 1)), 'esperado:', [(100, [1])])

evento = [['sombra-doble-camino', libre, {'estado': datos(sombra), 'efectos': []}]]
mutado = deepcopy(evento)
estado = mutado[0][2]['estado']
estado['entradas'][0]['tren'] = estado['trenes'][1]['id']
comparar(evento, mutado)
print('Comparador: aceptó un recibo enlazado al lote común equivocado')
ledger.close()
PY
