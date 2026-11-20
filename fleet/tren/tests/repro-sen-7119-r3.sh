#!/usr/bin/env bash
set -eu

python3 -I -B - "$PWD/fleet/tren" <<'PY'
from dataclasses import replace
from tempfile import mkdtemp
import sys

sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[1] + '/tests')
from dominio import Admitido, Entrada, paso
from ledger import Ledger, filas_medicion
from medicion import medir
from sombra import paso_sombra
from test_tren import REPO, entrega, observacion, vista


def custodiar(ledger, pr, instante, sombra=False):
    solicitud = entrega(pr)
    muestra = Entrada(solicitud, instante, Admitido(), instante) if sombra else None
    return ledger.entregar(solicitud, instante, muestra)


def operativo(ledger, obs):
    estado, efectos = paso(ledger.estado(REPO, operativo=True), obs)
    ledger.guardar_paso(estado, efectos, obs.ahora)
    return estado


def simular(ledger, obs):
    estado, efectos = paso_sombra(ledger.estado(REPO, sombra=True, doble_camino=True), obs)
    ledger.sombra(estado, efectos, obs.ahora, doble_camino=True)
    return estado


def informe(ledger, hasta, desde=0):
    return medir(*filas_medicion(REPO, ledger.directorio), REPO, desde, hasta)


def lotes(resultado):
    return [(lote['fuente'], int(lote['instante']), lote['prs']) for lote in resultado['lotes']]


ledger = Ledger(mkdtemp(prefix='sen-7119-r3-activar-'))
recibos = [custodiar(ledger, pr, instante, sombra=True)
           for pr, instante in enumerate((100, 220, 340), 1)]
simulado = simular(ledger, observacion(3, 4300))
assert [t.recibos for t in simulado.trenes] == [(recibos[0],), tuple(recibos[1:])]
activo = operativo(ledger, observacion(3, 4420))
assert activo.trenes[0].recibos == tuple(recibos)
resultado = informe(ledger, 4421)
assert lotes(resultado) == [('sombra', 100, [1]), ('sombra', 4180, [2, 3]),
                            ('real', 4420, [1, 2, 3])]
ahorro = resultado['suites_ahorradas_estimadas']
assert len(resultado['entregas']) == 3 and not resultado['pendientes']
print(f'C1 lotes: {lotes(resultado)}')
print(f'C1 entregas: 3; suites ahorradas: {ahorro}; máximo para 3 recibos únicos: 2')
ledger.close()

ledger = Ledger(mkdtemp(prefix='sen-7119-r3-rollback-'))
custodiar(ledger, 1, 90)
operativo(ledger, observacion(ahora=100))
recibo2 = custodiar(ledger, 2, 110)
recibo3 = custodiar(ledger, 3, 120)
obs = observacion(3, 220)
opciones_solo = {'sensible': True, 'autorizado': True}
operativo(ledger, replace(obs, prs=(vista(1), vista(2, ci='pendiente', **opciones_solo), vista(3))))
obs = replace(obs, ahora=340, prs=(vista(1), vista(2, **opciones_solo), vista(3)))
activo = operativo(ledger, obs)
assert [(e.entrega.pr, e.admitida, e.solo) for e in activo.entradas[1:]] == [
    (2, 340, True), (3, 220, False)]
assert informe(ledger, 341)['pendientes'] == [recibo3, recibo2]
legado, efectos = paso(ledger.estado(REPO, sombra=True), obs)
ledger.sombra(legado, efectos, 400)
assert informe(ledger, 401) == {**informe(ledger, 341), 'hasta': 401}
simular(ledger, replace(obs, ahora=460))
post_rollback = lotes(informe(ledger, 461))
esperado = [('real', 100, [1]), ('sombra', 460, [3])]
print(f'C2 lotes tras volver a sombra: {post_rollback}; FIFO esperado: {esperado}')
print('Control: evento sombra legado dentro del tramo activo conserva lotes y pendientes')
ledger.close()

ledger = Ledger(mkdtemp(prefix='sen-7119-r3-cohorte-'))
for pr, instante in enumerate((100, 200, 220, 300, 4181), 1):
    custodiar(ledger, pr, instante, sombra=True)
resultado = informe(ledger, 4181, desde=220)
assert [e['pr'] for e in resultado['entregas']] == [3, 4]
assert lotes(resultado) == [('sombra', 4180, [3, 4])]
assert resultado['suites_ahorradas_estimadas'] == resultado['distribucion']['2'] == 1
antes = informe(ledger, 4180, desde=220)
assert not antes['lotes'] and antes['pendientes'] == [entrega(3).recibo, entrega(4).recibo]
print('Control: la historia antes de desde conserva ocupación; cohorte y corte excluyen el borde superior')
ledger.close()

ledger = Ledger(mkdtemp(prefix='sen-7119-r3-u1-'))
custodiar(ledger, 1, 90)
legado, efectos = paso(ledger.estado(REPO, sombra=True), observacion(ahora=100))
ledger.sombra(legado, efectos, 100)
entregado, = informe(ledger, 101)['entregas']
assert entregado['admision'] == {'estado': 'retenido', 'motivo': 'custodia'}
assert entregado['fuente'] == 'sin-observacion-al-entregar' and entregado['admitida'] == 100
print('Control U1:', entregado['admision'], entregado['fuente'], 'primera admisión:', entregado['admitida'])
ledger.close()

sys.exit(ahorro > 2 or post_rollback != esperado)
PY
