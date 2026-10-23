import copy
import json
from pathlib import Path

from hostgate.tests.support.cases import parametrize, suite

from hostgate.salud.lector_desk import _ts, evaluar

REAL = json.loads((Path(__file__).parents[1] / 'fixtures/health/desk.json').read_text())
BOOT = REAL['boot']['bootId']
AHORA = _ts(REAL['ts']) + 30


def observacion(**campos):
    base = dict(schema='desktop-vigilante/powershell-count@1', value=None, unit='count', status='unknown',
                error=None, source='Process.GetProcessesByName+OpenProcess', minAgeSeconds=300, warnCount=1,
                scope='monitor-account', coverage='unavailable', observedLowerBound=0, unresolvedCandidates=None,
                startedUtc='2026-10-23T22:33:07.1000000Z', completedUtc='2026-10-23T22:33:07.4000000Z', errors=[])
    base.update(campos)
    return base


VARIANTES = {
    'complete0': observacion(value=0, status='ok', coverage='complete', unresolvedCandidates=0),
    'completewarn': observacion(value=3, status='warn', coverage='complete', observedLowerBound=3,
                                unresolvedCandidates=0),
    'partialunknown': observacion(coverage='partial', observedLowerBound=2, unresolvedCandidates=4,
                                  errors=[dict(stage='open', code='access-denied', win32=5, count=4)]),
    'unavailable': observacion(error='capture-failed', unresolvedCandidates=None,
                               errors=[dict(stage='enumerate', code='capture-failed', win32=None, count=1)]),
}


def con_observacion(nombre):
    muestra = copy.deepcopy(REAL)
    muestra['observations'] = {'powerShellLongLived': VARIANTES[nombre]}
    return muestra


def test_snapshot_real_sin_extension_es_sano():
    assert 'observations' not in REAL
    assert evaluar(REAL, AHORA, BOOT) == []


@parametrize('nombre', sorted(VARIANTES))
def test_extension_no_cambia_la_admision(nombre):
    muestra = con_observacion(nombre)
    sin_extension = {k: v for k, v in muestra.items() if k != 'observations'}
    assert sin_extension == REAL
    assert evaluar(muestra, AHORA, BOOT) == evaluar(REAL, AHORA, BOOT) == []


@parametrize('nombre', sorted(VARIANTES))
def test_extension_no_tapa_un_desk_no_sano(nombre):
    muestra = con_observacion(nombre)
    muestra['metrics']['commitRatio']['value'] = 0.9
    muestra['metrics']['commitBytes']['value'] = 0.9 * muestra['metrics']['commitLimitBytes']['value']
    base = copy.deepcopy(muestra)
    del base['observations']
    assert evaluar(muestra, AHORA, BOOT) == evaluar(base, AHORA, BOOT) == ['commitRatio']


@parametrize('cambio, motivo', [
    (lambda m: m['errors'].append({'stage': 'metric', 'code': 'x'}), 'errors'),
    (lambda m: m.update(status='warn'), 'status'),
    (lambda m: m['persist'].update(jsonl='failed'), 'persist'),
    (lambda m: m['boot'].update(bootId='FELIX-DESKTOP/otro'), 'boot'),
    (lambda m: m['volumes']['C:'].update(freeBytes=5 * 1024 ** 3), 'C: libre'),
    (lambda m: m['metrics']['cpuRootPct'].update(value=None), 'cpuRootPct incompleta'),
    (lambda m: m['metrics']['eLatencyMs'].update(value=80), 'eLatencyMs'),
    (lambda m: m.update(schema='desktop-vigilante/muestra@2'), 'schema'),
])
def test_controles_negativos(cambio, motivo):
    muestra = copy.deepcopy(REAL)
    cambio(muestra)
    assert motivo in evaluar(muestra, AHORA, BOOT)


def test_vigencia_fuera_de_ventana():
    assert 'vigencia' in evaluar(REAL, AHORA + 200, BOOT)
    assert 'vigencia' in evaluar(REAL, AHORA - 100, BOOT)


def test_elatency_idle_valido_es_sano():
    muestra = copy.deepcopy(REAL)
    muestra['metrics']['eLatencyMs'].update(status='idle', value=None, error=None, transfers=0)
    assert evaluar(muestra, AHORA, BOOT) == []


@parametrize('ts', ['2026-99-99T00:00:00Z', '2026-02-30T00:00:00Z', '2026-10-23T25:00:00Z',
                                '2026-10-23T22:33:08', 20261023, None])
def test_fecha_malformada_es_no_sano(ts):
    muestra = copy.deepcopy(REAL)
    muestra['ts'] = ts
    assert 'vigencia' in evaluar(muestra, AHORA, BOOT)


@parametrize('ruta, valor, motivo', [
    (('metrics', 'cpuRootPct', 'value'), -1, 'cpuRootPct incompleta'),
    (('metrics', 'cpuRootPct', 'value'), 150, 'cpuRootPct incompleta'),
    (('metrics', 'cpuRootPct', 'value'), True, 'cpuRootPct incompleta'),
    (('metrics', 'commitBytes', 'value'), -1, 'commitBytes incompleta'),
    (('metrics', 'commitRatio', 'value'), 1.5, 'commitRatio incompleta'),
    (('metrics', 'commitLimitBytes', 'value'), 0, 'commit incoherente'),
    (('metrics', 'eLatencyMs', 'value'), -0.5, 'eLatencyMs incompleta'),
    (('volumes', 'C:', 'freeBytes'), 999111520257, 'volumen C: incompleto'),
    (('volumes', 'E:', 'totalBytes'), 0, 'volumen E: incompleto'),
    (('volumes', 'C:', 'freeBytes'), -1, 'volumen C: incompleto'),
])
def test_rangos_imposibles_con_status_ok_son_no_sano(ruta, valor, motivo):
    muestra = copy.deepcopy(REAL)
    destino = muestra
    for clave in ruta[:-1]:
        destino = destino[clave]
    destino[ruta[-1]] = valor
    assert motivo in evaluar(muestra, AHORA, BOOT)


def test_commit_usado_mayor_que_limite_es_incoherente():
    muestra = copy.deepcopy(REAL)
    m = muestra['metrics']
    m['commitBytes']['value'] = m['commitLimitBytes']['value'] + 1
    m['commitRatio']['value'] = m['commitBytes']['value'] / m['commitLimitBytes']['value']
    assert m['commitRatio']['value'] > 1
    assert evaluar(muestra, AHORA, BOOT) != []


BASURA = [None, 1, -1, 1.5, float('nan'), True, '', 'x', [], [1], {}, {'value': 1}]


def _rutas(objeto, prefijo=()):
    if isinstance(objeto, dict):
        for clave, valor in objeto.items():
            yield prefijo + (clave,)
            yield from _rutas(valor, prefijo + (clave,))


@parametrize('ruta', list(_rutas(REAL)))
def test_cualquier_campo_malformado_nunca_lanza(ruta):
    for basura in BASURA:
        muestra = copy.deepcopy(REAL)
        destino = muestra
        for clave in ruta[:-1]:
            destino = destino[clave]
        destino[ruta[-1]] = basura
        motivos = evaluar(muestra, AHORA, BOOT)
        assert isinstance(motivos, list)


@parametrize('ruta', ['metrics', 'volumes', 'boot', 'guestRuntime', 'persist', 'errors', 'status',
                                  'schema', 'host', 'ts'])
def test_campo_superior_malformado_es_no_sano(ruta):
    for basura in BASURA:
        if type(basura) is type(REAL[ruta]) and basura == REAL[ruta]:
            continue
        muestra = copy.deepcopy(REAL)
        muestra[ruta] = basura
        assert evaluar(muestra, AHORA, BOOT) != [], (ruta, basura)


@parametrize('muestra', [None, [], 'x', 1])
def test_snapshot_no_objeto(muestra):
    assert evaluar(muestra, AHORA, BOOT) == ['snapshot no es objeto']


def load_tests(loader, tests, pattern):
    return suite(globals())
