"""Contrato de publicación del tren, compartido por el receptor y el rack."""
import json
from pathlib import Path
import re

MAX_DOCUMENT = 256 * 1024
MAX_STATE = 2 * MAX_DOCUMENT
FRESH_SECONDS = 660
DEFAULT_CONFIG = Path(__file__).resolve().with_name('rack.json')
REPO = r'[A-Za-z0-9_.-]{1,64}/[A-Za-z0-9_.-]{1,64}'
MODE = r'[a-z0-9-]{1,32}'
RESULTS = ('ATERRIZADO', 'DEVUELTO', 'ABSUELTO', 'ABORTADO', 'PARTIDO', 'REARMAR', 'SIMULADO')
# Vocabulario público de Motivo. El rack no depende del código del proyector.
MOTIVOS = (
    'custodia', 'head-cambiado', 'base-no-main', 'pr-no-abierto', 'apto-no-vigente',
    'hallazgos-abiertos', 'dirigida-no-limpia', 'ci-no-verde', 'conflicto',
    'observacion-incompleta', 'falta-verificado', 'alto-riesgo-excluido-del-piloto',
    'merge-ready',
)
CONTROL = re.compile(r'[\x00-\x1f\x7f-\x9f\ud800-\udfff]')
ABSOLUTE_PATH = re.compile(r'(?<![\w./-])(?:[/\\]|[a-zA-Z]:[/\\])')
# Digit-only runs up to 15 long are dates or counters; longer ones are hashes too.
LONG_HEX = re.compile(r'(?i)(?<![0-9a-f])(?=[0-9a-f]*[a-f]|[0-9]{16})([0-9a-f]{7})[0-9a-f]+(?![0-9a-f])')


def _require(condition, field):
    if not condition:
        raise ValueError('Tren inválido: ' + field)


def _keys(row, fields):
    _require(type(row) is dict and set(row) == set(fields.split()), fields)


def _matches(value, pattern, field):
    _require(type(value) is str and re.fullmatch(pattern, value) is not None, field)
    return value


def _choice(value, choices, field):
    _require(value in choices, field)
    return value


def _int(value, minimum, maximum=None):
    _require(type(value) is int and value >= minimum and (maximum is None or value <= maximum),
             'entero')
    return value


def _date(value, minimum, maximum):
    # El intervalo también descarta infinitos y NaN, sin convertir enteros a float.
    _require(type(value) in (int, float) and minimum <= value <= maximum, 'fecha')
    return value


def _rows(value, minimum, maximum):
    _require(type(value) is list and minimum <= len(value) <= maximum, 'lista')
    return value


def _repo(value):
    _matches(value, REPO, 'repo')
    _require(all(part not in ('.', '..') for part in value.split('/')), 'repo')
    return value


def load_repos(path):
    """Lee la configuración opcional del servidor y del receptor."""
    try:
        data = decode(path.read_bytes())
    except FileNotFoundError:
        return ()
    _require(type(data) is dict, 'configuración')
    config = data.get('tren', {})
    _require(type(config) is dict, 'tren')
    repos = tuple(_repo(repo) for repo in _rows(config.get('repos', []), 0, 4))
    _require(len(repos) == len({repo.lower() for repo in repos}), 'tren.repos repetidos')
    return repos


def decode(raw):
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            _require(key not in obj, 'clave repetida')
            obj[key] = value
        return obj

    return json.loads(raw.decode('utf-8'), object_pairs_hook=unique)


def public_text(text):
    """Acorta secuencias hexadecimales en texto libre, fuera de ids de lote y refs."""
    return LONG_HEX.sub(r'\1', text)


def public_title(text):
    if type(text) is str and text.strip() and not CONTROL.search(text) and not ABSOLUTE_PATH.search(text):
        return public_text(text)


def _unit(value):
    _require(type(value) is str and 0 < len(value) <= 300 and '..' not in value
             and '\\' not in value and not CONTROL.search(value)
             and not ABSOLUTE_PATH.search(value), 'unidad')
    return value


def _place(row):
    _keys(row, 'tipo vm pueue_id ref')
    kind = _choice(row['tipo'], ('mini', 'desk', 'boat', 'desconocido'), 'tipo')
    vm = row['vm']
    if vm is not None:
        _require(kind == 'boat', 'vm fuera de Boat')
        _matches(vm, r'[a-z0-9][a-z0-9-]{0,62}', 'vm')
    return dict(tipo=kind, vm=vm,
                pueue_id=None if row['pueue_id'] is None else _int(row['pueue_id'], 0),
                ref=None if row['ref'] is None else _matches(
                    row['ref'], r'job-[A-Za-z0-9]{4,32}|[0-9]{8}-[0-9]{6}-[0-9a-f]{6}', 'ref'))


def _member(row):
    _keys(row, 'pr head')
    return dict(pr=_int(row['pr'], 1, 10**7), head=_matches(row['head'], r'[0-9a-f]{7}', 'head'))


def _return(row, repo, pr):
    _keys(row, 'check unidad comentario_url')
    url = _matches(row['comentario_url'],
                   rf'https://github\.com/(?ai:{re.escape(repo)})/pull/{pr}#issuecomment-[0-9]+',
                   'comentario_url')
    return dict(check=_matches(row['check'], MODE, 'check'), unidad=_unit(row['unidad']),
                comentario_url=url)


def _finished_member(row, repo):
    _keys(row, 'pr resultado devolucion')
    pr = _int(row['pr'], 1, 10**7)
    return dict(pr=pr, resultado=_choice(row['resultado'], RESULTS + ('MERGEADO-FUERA', 'CADUCADO'), 'resultado'),
                devolucion=None if row['devolucion'] is None else _return(row['devolucion'], repo, pr))


def _problem(row):
    _keys(row, 'stage message')
    return dict(stage=_choice(row['stage'], ('ledger', 'fases', 'suite', 'boat', 'inscripcion'), 'stage'),
                message=_matches(row['message'], r'Lectura fallida: [A-Za-z]{1,40}', 'message'))


def valid_tren(data, repos, now):
    """Reconstruye el documento o lanza ValueError si una fila rompe el contrato."""
    _keys(data, 'schema taken repos errors')
    _require(data['schema'] == 'tren-host@1', 'schema')
    taken = _date(data['taken'], now - 86400, now + 300)

    def date(value):
        return _date(value, taken - 30 * 86400, taken + 300)

    def optional_date(value):
        return None if value is None else date(value)

    def pause(row):
        _keys(row, 'motivo pr desde')
        return dict(motivo=_matches(row['motivo'], r'[a-z0-9][a-z0-9-]{0,47}', 'motivo'),
                    pr=None if row['pr'] is None else _int(row['pr'], 1), desde=date(row['desde']))

    def suite(row):
        _keys(row, 'modo intento estado inicio fin resultado fallas lugar')
        return dict(modo=_matches(row['modo'], MODE, 'modo'), intento=_int(row['intento'], 0, 99),
                    estado=_choice(row['estado'], ('en-fila', 'corriendo', 'terminada', 'pendiente-75'), 'estado'),
                    inicio=optional_date(row['inicio']), fin=optional_date(row['fin']),
                    resultado=_choice(row['resultado'], ('verde', 'rojo', None), 'resultado'),
                    fallas=None if row['fallas'] is None else _int(row['fallas'], 0, 10**5),
                    lugar=_place(row['lugar']))

    def trial(row):
        _keys(row, 'modo unidad prs hechos inicio lugar')
        return dict(modo=_matches(row['modo'], MODE, 'modo'), unidad=_unit(row['unidad']),
                    prs=[_int(pr, 1, 10**7) for pr in _rows(row['prs'], 1, 5)],
                    hechos=_int(row['hechos'], 0), inicio=optional_date(row['inicio']), lugar=_place(row['lugar']))

    def batch(row):
        _keys(row, 'id fase fase_desde cierre miembros suite ensayo')
        return dict(id=_matches(row['id'], r'[0-9a-f]{12}', 'id'),
                    fase=_choice(row['fase'], ('lote', 'ci', 'suite', 'atribucion', 'aterrizando', 'otra'), 'fase'),
                    fase_desde=date(row['fase_desde']), cierre=date(row['cierre']),
                    miembros=[_member(member) for member in _rows(row['miembros'], 1, 5)],
                    suite=[suite(run) for run in _rows(row['suite'], 0, 8)],
                    ensayo=None if row['ensayo'] is None else trial(row['ensayo']))

    def waiting(row):
        _keys(row, 'pr head etapa motivo desde')
        return dict(pr=_int(row['pr'], 1, 10**7), head=_matches(row['head'], r'[0-9a-f]{7}', 'head'),
                    etapa=_choice(row['etapa'], ('entregado', 'admitido', 'retenido', 'caducado'), 'etapa'),
                    motivo=_choice(row['motivo'], MOTIVOS + (None,), 'motivo'), desde=date(row['desde']))

    def recent(row, repo):
        _keys(row, 'id resultado cierre fin miembros')
        return dict(id=_matches(row['id'], r'[0-9a-f]{12}', 'id'),
                    resultado=_choice(row['resultado'], RESULTS, 'resultado'),
                    cierre=date(row['cierre']), fin=_date(row['fin'], taken - 12 * 3600, taken + 300),
                    miembros=[_finished_member(member, repo) for member in _rows(row['miembros'], 1, 5)])

    configured = {repo.lower(): repo for repo in repos}
    clean = []
    seen = set()
    for row in _rows(data['repos'], 1, 4):
        _keys(row, 'repo modo pausa lote espera recientes')
        name = _repo(row['repo']).lower()
        _require(name in configured and name not in seen, 'repo no permitido o repetido')
        seen.add(name)
        repo = configured[name]
        clean.append(dict(repo=repo, modo=_choice(row['modo'], ('activo', 'sombra'), 'modo'),
                          pausa=None if row['pausa'] is None else pause(row['pausa']),
                          lote=None if row['lote'] is None else batch(row['lote']),
                          espera=[waiting(pr) for pr in _rows(row['espera'], 0, 50)],
                          recientes=[recent(item, repo) for item in _rows(row['recientes'], 0, 10)]))
    return dict(schema='tren-host@1', taken=taken, repos=clean,
                errors=[_problem(row) for row in _rows(data['errors'], 0, 16)])


def read_received(path, repos, now):
    with path.open('rb') as stream:
        raw = stream.read(MAX_STATE + 1)
    _require(len(raw) <= MAX_STATE, 'tamaño de tren.json')
    data = decode(raw)
    _require(type(data) is dict and 'received_at' in data, 'received_at')
    received_at = _date(data.pop('received_at'), 0, now + 300)
    return received_at, valid_tren(data, repos, received_at)
