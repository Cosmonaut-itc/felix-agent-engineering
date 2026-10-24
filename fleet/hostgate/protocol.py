"""Wire fleet/host-reservation@1: datos estrictos, nunca autoridad."""
import hashlib
import json

SCHEMA = 'fleet/host-reservation@1'
MAX_BYTES = 65536
CODES = frozenset(('waiting-capacity pending-expired requester-dead anchor-dead drained '
    'health-invalid health-threshold health-reader-pending boot-unverified default-shell-unverified '
    'executor-incompatible profile-unverified budget-exceeded resource-busy resource-cleanup-unconfirmed '
    'reservation-uncertain unix-scope-unconfirmed windows-tree-unconfirmed identity-unverifiable '
    'stale-generation operation-conflict store-unavailable busy enforcement-unavailable').split())


def reason(code, detail='', retryable=False):
    if code not in CODES:
        raise ValueError('motivo desconocido')
    return dict(code=code, detail=detail, retryable=retryable)


def strict_json(text):
    if len(text.encode('utf-8')) > MAX_BYTES:
        raise ValueError('JSON excede 64 KiB')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('clave duplicada')
            result[key] = value
        return result
    def invalid(value):
        raise ValueError('numero no finito')
    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)
    except (RecursionError, OverflowError) as exc:
        raise ValueError('JSON demasiado profundo') from exc
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False, ensure_ascii=True)


def digest(value):
    return hashlib.sha256(canonical(value).encode('ascii')).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ValueError('identificador invalido')
    return value


def integer(value):
    if type(value) is not int or value < 0:
        raise ValueError('se requiere entero no negativo')
    return value


def demand(value):
    if not isinstance(value, dict) or set(value) != {'cpuMilli', 'memoryBytes', 'diskBytes', 'slots'}:
        raise ValueError('demanda invalida')
    for k in ('cpuMilli', 'memoryBytes', 'slots'):
        integer(value[k])
    if not isinstance(value['diskBytes'], dict):
        raise ValueError('diskBytes invalido')
    for name, amount in value['diskBytes'].items():
        identifier(name); integer(amount)
    if not any(value[k] for k in ('cpuMilli', 'memoryBytes', 'slots')) and not any(value['diskBytes'].values()):
        raise ValueError('demanda vacia')
    return value


def key(value):
    if not isinstance(value, dict) or set(value) != {'storeId', 'reservationId', 'generation'}:
        raise ValueError('key invalida')
    identifier(value['storeId']); identifier(value['reservationId'])
    if integer(value['generation']) == 0:
        raise ValueError('generacion invalida')
    return value


def validate(value, operation=None):
    if not isinstance(value, dict):
        raise ValueError('solicitud no es objeto')
    base = {'schema', 'operationId', 'operation'}
    op = value.get('operation')
    if value.get('schema') != SCHEMA or (operation is not None and op != operation):
        raise ValueError('schema u operacion invalida')
    fields = {
        'reservar': {'jobId', 'attemptId', 'executorId', 'physicalHostId', 'demand', 'exclusiveResources', 'profileId'},
        'latido': {'key', 'seq'}, 'dudosa': {'key', 'reason', 'evidenceIds'},
        'soltar': {'key', 'evidenceIds'}, 'estado': set()}
    if op not in fields or set(value) != base | fields[op]:
        raise ValueError('campos no permitidos o ausentes')
    identifier(value['operationId'])
    if op == 'reservar':
        for name in ('jobId', 'attemptId', 'executorId', 'physicalHostId', 'profileId'):
            identifier(value[name])
        demand(value['demand'])
        resources = value['exclusiveResources']
        if not isinstance(resources, list) or len(resources) > 128:
            raise ValueError('recursos invalidos')
        for resource in resources: identifier(resource)
        if len(resources) != len(set(resources)):
            raise ValueError('recurso duplicado')
    if 'key' in fields[op]: key(value['key'])
    if op == 'latido': integer(value['seq'])
    if op == 'dudosa': identifier(value['reason'])
    if 'evidenceIds' in fields[op]:
        if not isinstance(value['evidenceIds'], list) or len(value['evidenceIds']) > 128:
            raise ValueError('evidencia invalida')
        for evidence in value['evidenceIds']: identifier(evidence)
    canonical(value)
    return value


def reply(op_id, revision, reasons=(), would_grant=False, exit_code=0, **fields):
    return dict(schema=SCHEMA, operationId=op_id, result='shadow', wouldGrant=would_grant,
                reasons=list(reasons), configRevision=revision, exitCode=exit_code, **fields)
