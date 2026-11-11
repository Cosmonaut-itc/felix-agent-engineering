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


def _hex64(value):
    if not isinstance(value, str) or len(value) != 64 or set(value) - set('0123456789abcdef'):
        raise ValueError('digest invalido')


def _exact(value, names):
    if not isinstance(value, dict) or set(value) != set(names.split()):
        raise ValueError('campos no permitidos o ausentes')
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
        'soltar': {'key', 'evidenceIds'}, 'estado': set(), 'authorize_creation': {'key', 'confirmation', 'launch'},
        'consume_creation': {'key', 'permitId', 'launchDigest'}, 'record_started': {'key', 'permitId', 'root'}}
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
    if op == 'authorize_creation':
        confirmation = _exact(value['confirmation'], 'operationId requestDigest attemptId source')
        identifier(confirmation['operationId']); identifier(confirmation['attemptId']); _hex64(confirmation['requestDigest'])
        if confirmation['source'] not in ('reply', 'same-channel-replay'): raise ValueError('origen de confirmacion')
        for name in _exact(value['launch'], 'argvDigest inputDigest bootstrapDigest launcherDigest').values(): _hex64(name)
    if op in ('consume_creation', 'record_started'): identifier(value['permitId'])
    if op == 'consume_creation': _hex64(value['launchDigest'])
    if op == 'record_started':
        root = _exact(value['root'], 'pid unique')
        if integer(root['pid']) == 0 or integer(root['unique']) == 0: raise ValueError('raiz invalida')
    if 'evidenceIds' in fields[op]:
        if not isinstance(value['evidenceIds'], list) or len(value['evidenceIds']) > 128:
            raise ValueError('evidencia invalida')
        for evidence in value['evidenceIds']: identifier(evidence)
    canonical(value)
    return value


def reply(op_id, revision, reasons=(), would_grant=False, exit_code=0, *, mode='shadow', store_id=None, **fields):
    if mode == 'shadow':
        return dict(schema=SCHEMA, operationId=op_id, result='shadow', wouldGrant=would_grant,
                    reasons=list(reasons), configRevision=revision, exitCode=exit_code, **fields)
    common = dict(schema=SCHEMA, operationId=op_id, reasons=list(reasons),
                  configRevision=revision, exitCode=exit_code)
    if mode != 'effective': raise ValueError('modo desconocido')
    identifier(store_id)
    result = dict(common, result='effective', mode=mode, storeId=store_id, granted=would_grant, **fields)
    return validate_result(result, mode, store_id)


def validate_result(result, mode='shadow', store_id=None, operation=None):
    """Valida la variante del wire; correlación con el ledger vive en el store."""
    if mode not in ('shadow', 'effective'): raise ValueError('modo desconocido')
    grant_field = 'wouldGrant' if mode == 'shadow' else 'granted'
    key_field = 'simulationKey' if mode == 'shadow' else 'reservationKey'
    if (not isinstance(result, dict) or result['schema'] != SCHEMA or result['result'] != mode
            or type(result[grant_field]) is not bool or type(result['exitCode']) is not int
            or result['exitCode'] not in range(7) or not isinstance(result['reasons'], list)):
        raise ValueError('resultado de operacion')
    def reasons(values):
        for item in values:
            if item['code'] not in CODES or type(item['retryable']) is not bool or 'detail' not in item:
                raise ValueError('motivos de operacion')
    reasons(result['reasons'])
    if key_field in result: key(result[key_field])
    if result[grant_field] and key_field not in result: raise ValueError('admision sin reserva')
    if mode == 'effective':
        if {'wouldGrant', 'simulationKey', 'simulationState'} & set(result):
            raise ValueError('campos de simulacion en resultado efectivo')
        identifier(store_id)
        identifier(result['configRevision'])
        if result['mode'] != mode or result['storeId'] != store_id: raise ValueError('modo/storeId')
        if operation is not None and {'state', 'permit'} & set(result) and key_field not in result:
            raise ValueError('estado sin reserva')
        if key_field in result:
            if result[key_field]['storeId'] != store_id or not result[key_field]['reservationId'].startswith('e_'):
                raise ValueError('key efectiva invalida')
            _state(result, strict=operation is not None)
        # Sin operación (store, reply) valen los campos de cualquiera; con ella, sólo los suyos.
        fields = _COMMON | _EFFECTIVE | (_OPERATION_FIELDS.get(operation, frozenset()) if operation else _ANY)
        if set(result) - (fields - _QUERY if operation in _CREATION else fields):
            raise ValueError('campos de resultado no permitidos')
        if operation in _CREATION and result['exitCode'] == 0 and 'replayed' not in result:
            permit_state, field = _CREATION[operation]
            if result.get('permit', {}).get('state') != permit_state or (field and field not in result):
                raise ValueError('exito sin recibo integro')
        if 'heartbeatTTL' in result and (type(result['heartbeatTTL']) not in (int, float) or result['heartbeatTTL'] <= 0):
            raise ValueError('TTL')
        if 'scopeStatus' in result: _scope(result['scopeStatus'])
        # creationAllowed ausente equivale a falso; verdadero sólo en el consumo original.
        if 'creationAllowed' in result:
            allowed = result['creationAllowed']
            if type(allowed) is not bool or (allowed and ('replayed' in result or result['exitCode'] != 0
                                                          or result.get('state') != 'reserved'
                                                          or result['permit']['state'] != 'consumed')):
                raise ValueError('creationAllowed sin consumo original')
        if 'replayed' in result:
            if result['replayed'] is not True or result['granted']: raise ValueError('replay no admite')
            original = validate_result(result['originalResult'], mode, store_id, operation)
            if 'replayed' in original or original['operationId'] != result['operationId']:
                raise ValueError('historico de replay')
            current = result['currentState']
            if current is not None:
                if set(current) - {'permit'} != {'state', 'reasons'}: raise ValueError('estado de replay')
                _state(current, strict=operation is not None)
                reasons(current['reasons'])
    canonical(result)
    return result


_COMMON = frozenset('schema operationId reasons configRevision exitCode result mode storeId granted'.split())
_EFFECTIVE = frozenset('pendingId reservationKey state permit replayed originalResult currentState pending '
                       'reservations budget resources drained degraded physicalHostId'.split())
_OPERATION_FIELDS = {'authorize_creation': frozenset(['heartbeatTTL']), 'consume_creation': frozenset(['creationAllowed']),
                     'record_started': frozenset(), 'soltar': frozenset(['scopeStatus'])}
_ANY = frozenset().union(*_OPERATION_FIELDS.values())
_QUERY = frozenset('pendingId pending reservations budget resources drained degraded physicalHostId'.split())
# Éxito de cada operación de creación: estado del permiso y campo propio obligatorio.
_CREATION = {'authorize_creation': ('issued', 'heartbeatTTL'), 'consume_creation': ('consumed', 'creationAllowed'),
             'record_started': ('registered', None)}
_PERMIT_STATES = {'reserved': ('issued', 'consumed'), 'running': ('registered',),
                  'uncertain': ('issued', 'consumed', 'registered'), 'released': ('closed', 'sealed')}
_PERMIT_EXTRA = {'issued': '', 'consumed': '', 'registered': 'scopeReceipt', 'closed': 'scopeReceipt closedBy',
                 'sealed': 'closedBy'}


def _state(value, strict=False):
    """state efectivo y, si hay permiso, su variante pública compatible. strict: running y released lo llevan."""
    if value.get('state') not in _PERMIT_STATES: raise ValueError('estado efectivo')
    if 'permit' not in value:
        if strict and value['state'] in ('running', 'released'): raise ValueError('estado sin permiso')
        return
    permit = value['permit']
    state = permit.get('state') if isinstance(permit, dict) else None
    if state not in _PERMIT_STATES[value['state']]: raise ValueError('permiso incompatible con el estado')
    if not (state == 'sealed' and set(permit) == {'state', 'closedBy'}):
        _exact(permit, 'state permitId issuerEpoch profileRevision baselineDigest ' + _PERMIT_EXTRA[state])
        identifier(permit['permitId']); identifier(permit['profileRevision']); _hex64(permit['baselineDigest'])
        if integer(permit['issuerEpoch']) == 0: raise ValueError('epoch')
        if 'scopeReceipt' in permit: identifier(permit['scopeReceipt'])
    if 'closedBy' in permit: _hex64(permit['closedBy'])


def _scope(value):
    if not isinstance(value, dict) or value.get('status') not in ('occupied', 'scan-retry', 'invalid'):
        raise ValueError('estado del ambito')
    if value['status'] != 'occupied':
        identifier(_exact(value, 'status reason')['reason']); return
    _exact(value, 'status novel novelCount')
    if not isinstance(value['novel'], list) or not 0 < integer(value['novelCount']) or len(value['novel']) != min(64, value['novelCount']):
        raise ValueError('novedades')
    for pair in value['novel']:
        if not isinstance(pair, list) or len(pair) != 2 or not all(integer(x) > 0 for x in pair): raise ValueError('novedad')
