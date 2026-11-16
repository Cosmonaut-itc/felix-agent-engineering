"""Closed host-control variant; cancellation keeps its original contract."""
import re
import hashlib
import json

REORDER_OPS = ('reorder', 'reorder_undo')
OPS = ('host_capabilities', 'slots', 'mode', 'undo') + REORDER_OPS
GROUPS = {'suites', 'default', 'rapidas'}
DESK_GROUPS = {'hasito', 'jale', 'jale-verify', 'otros', 'pesadas', 'rapidas'}
ACTION = re.compile(r'[A-Za-z0-9-]{8,64}')
HEX64 = re.compile('[0-9a-f]{64}')
TOKEN = re.compile('[0-9a-f]{32}')
REORDER_MAX = 64
# The rack never forwards host text: each code maps to its own fixed message.
REORDER_MESSAGES = dict(
    stale_queue='La fila cambió. Actualiza antes de decidir.',
    same_order='Ese orden ya está vigente.',
    too_long='Fila demasiado larga para reordenar de una vez',
    paused='No se puede reordenar mientras el grupo está pausado',
    busy='Hay un ajuste pendiente; consulta su recibo.',
    held_tasks='Hay tareas apartadas, en edición o pausadas en este grupo; no se reordena.',
    edit_unsafe='Esta fila no se puede editar con seguridad (alias o formato de Pueue); no se pausó nada.',
    not_ready='El control de este host se está recuperando. Vuelve a intentarlo.',
    reorder_disabled='El reordenamiento está apagado en este host.',
    interrupted='El reordenamiento se interrumpió; la fila quedó como estaba.',
    queue_changed='La fila cambió durante el reordenamiento; se restauró lo que seguía en fila.',
    reorder_unverified='No se pudo verificar la fila; el grupo queda en pausa. Revisar y resolver con control.py reanudar.')
PAUSE_LABELS = ('pausa por presión', 'pausa manual', 'pausa de origen desconocido',
                'retenido al reiniciar', 'relevo del daemon', 'en pausa recuperando…')
PAUSE_REASONS = ('pressure', 'manual', 'ambiguous', 'startup', 'handoff', 'reorder')


def vector(value, host='mini'):
    return (type(value) is dict and set(value) == (DESK_GROUPS if host == 'desk' else GROUPS)
            and all(type(n) is int and 1 <= n <= 8 for n in value.values()))


def int32(value):
    return type(value) is int and -(2**31) <= value < 2**31


def queue(value):
    """Served queue: [id, opaque token, priority] rows, unique ids, scheduler order."""
    return (type(value) is list and 2 <= len(value) <= REORDER_MAX
            and all(type(row) is list and len(row) == 3 and type(row[0]) is int and 0 <= row[0] < 2**64
                    and type(row[1]) is str and TOKEN.fullmatch(row[1]) and int32(row[2]) for row in value)
            and len({row[0] for row in value}) == len(value))


def validate_reorder(request):
    keys = {'schema', 'op', 'host', 'action_id', 'dry_run', 'expected',
            'change' if request['op'] == 'reorder' else 'undo_of'}
    e = request.get('expected')
    fields = {'group', 'generation', 'queue_etag'} | ({'queue'} if request['op'] == 'reorder' else set())
    if (type(request.get('action_id')) is not str or not ACTION.fullmatch(request['action_id'])
            or type(request.get('dry_run')) is not bool):
        raise ValueError('Identidad inválida')
    if (type(e) is not dict or set(e) != fields or e['group'] not in GROUPS
            or type(e['generation']) is not str or not 1 <= len(e['generation']) <= 160
            or type(e['queue_etag']) is not str or not HEX64.fullmatch(e['queue_etag'])):
        raise ValueError('Estado inválido')
    if request['op'] == 'reorder':
        c = request.get('change')
        if (not queue(e['queue']) or type(c) is not dict or set(c) != {'order'} or type(c['order']) is not list
                or any(type(i) is not int for i in c['order']) or sorted(c['order']) != sorted(r[0] for r in e['queue'])):
            raise ValueError('Orden inválido')
    elif type(request.get('undo_of')) is not str or not ACTION.fullmatch(request['undo_of']):
        raise ValueError('Recibo inválido')
    if set(request) != keys or request['schema'] != 'rack-action@1' or request.get('host') != 'mini':
        raise ValueError('Petición inválida')
    return request


def validate(request):
    if type(request) is not dict or request.get('op') not in OPS:
        raise ValueError('Operación inválida')
    if request['op'] in REORDER_OPS:
        return validate_reorder(request)
    keys = {'schema', 'op', 'host'}
    if request['op'] != 'host_capabilities':
        keys |= {'action_id', 'dry_run', 'expected', 'change' if request['op'] in ('slots', 'mode') else 'undo_of'}
        if (type(request.get('action_id')) is not str or not ACTION.fullmatch(request['action_id'])
                or type(request.get('dry_run')) is not bool):
            raise ValueError('Identidad inválida')
        e = request.get('expected')
        if (type(e) is not dict or set(e) != {'revision', 'policy_hash', 'generation', 'mode', 'slots'}
                or type(e['revision']) is not int or e['revision'] < 1
                or type(e['policy_hash']) is not str or len(e['policy_hash']) != 64
                or type(e['generation']) is not str or not 1 <= len(e['generation']) <= 160
                or e['mode'] not in (('dia', 'noche') if request.get('host') == 'desk' else ('fijo',))
                or not vector(e['slots'], request.get('host'))):
            raise ValueError('Estado inválido')
        if request['op'] == 'slots':
            c = request.get('change')
            if (type(c) is not dict or set(c) != {'group', 'delta'}
                    or type(c['group']) is not str or c['group'] not in (DESK_GROUPS if request.get('host') == 'desk' else GROUPS)
                    or type(c['delta']) is not int or c['delta'] not in (-1, 1)):
                raise ValueError('Cambio inválido')
        elif request['op'] == 'mode':
            c = request.get('change')
            if type(c) is not dict or set(c) != {'mode'} or c['mode'] not in ('dia', 'noche'):
                raise ValueError('Modo inválido')
        elif type(request.get('undo_of')) is not str or not ACTION.fullmatch(request['undo_of']):
            raise ValueError('Recibo inválido')
    if (set(request) != keys or request['schema'] != 'rack-action@1'
            or type(request['host']) is not str or not re.fullmatch(r'[A-Za-z0-9_-]+', request['host'])):
        raise ValueError('Petición inválida')
    return request


def unavailable():
    return dict(schema='rack-host-result@1', status='unsupported', capabilities=dict(
        slots=False, undo=False, dry_run=False, mode=False, reorder=False, restart_queue=False),
        error=dict(code='unsupported', message='Falta actualizar el control de este host.'))


def reorder_reason(code, message):
    """Fixed text per code; only `paused` carries the owners, from a closed label set."""
    base = REORDER_MESSAGES[code]
    if code != 'paused' or message == base:
        return base
    labels = message[len(base) + 3:].split(', ') if type(message) is str and message.startswith(base + ' · ') else None
    if not labels or any(x not in PAUSE_LABELS for x in labels):
        raise ValueError('Motivo inválido')
    return base + ' · ' + ', '.join(labels)


def reorder_view(value):
    """Per-group reorder eligibility of the Mini, rebuilt field by field."""
    if type(value) is not dict or not set(value) <= GROUPS:
        raise ValueError('Reordenar inválido')
    result = {}
    for group, item in value.items():
        if type(item) is not dict or type(item.get('enabled')) is not bool:
            raise ValueError('Reordenar inválido')
        if item['enabled']:
            if (set(item) != {'enabled', 'code', 'reason', 'queue_etag', 'queue', 'max_writes'}
                    or item['code'] is not None or item['reason'] is not None
                    or type(item['queue_etag']) is not str or not HEX64.fullmatch(item['queue_etag'])
                    or not queue(item['queue']) or type(item['max_writes']) is not int
                    or not 0 <= item['max_writes'] <= REORDER_MAX):
                raise ValueError('Reordenar inválido')
            result[group] = dict(enabled=True, code=None, reason=None, queue_etag=item['queue_etag'],
                                 queue=[list(r) for r in item['queue']], max_writes=item['max_writes'])
        else:
            code = item.get('code')
            if set(item) != {'enabled', 'code', 'reason'} or code is not None and code not in REORDER_MESSAGES:
                raise ValueError('Reordenar inválido')
            if (code is None) != (item['reason'] is None):
                raise ValueError('Reordenar inválido')
            result[group] = dict(enabled=False, code=code, reason=reorder_reason(code, item['reason']) if code else None)
    return result


def mode_fields(value):
    result = {}
    hour = r'(?:[01][0-9]|2[0-3]):[0-5][0-9]'
    other = 'noche' if value['mode'] == 'dia' else 'día'
    for key, pattern in (('mode_note', r'(?:Automático · cambia|Adelantado · vuelve) a ' + other + ' a las ' + hour),
                         ('mode_detail', r'Ajustes manuales hasta las ' + hour),
                         ('slots_note', r'Lugares ajustados · hasta las ' + hour)):
        item = value.get(key)
        if item is not None and (type(item) is not str or not re.fullmatch(pattern, item)):
            raise ValueError('Nota de horario inválida')
        if key in value:
            result[key] = item
    pending = value.get('mode_pending')
    expected = dict(dia='Aplicando horario de día…', noche='Aplicando horario de noche…')
    if pending is not None:
        if pending != expected:
            raise ValueError('Estado de horario inválido')
        result['mode_pending'] = expected
    for key, expected in (('mode_partial', 'Horario aplicado parcialmente · revisando…'),
                          ('mode_unknown', 'Cambio sin confirmar · consultando…')):
        if key in value:
            if value[key] != expected:
                raise ValueError('Estado de horario inválido')
            result[key] = expected
    return result


def control(value, host='mini'):
    """Read view whitelist; never publish arbitrary controller fields or error text."""
    if type(value) is not dict or value.get('schema') != 'rack-host-result@1':
        return unavailable()
    if value.get('status') == 'busy' and (value.get('error') or {}).get('code') == 'lock_busy':
        answer = unavailable()
        answer.update(status='busy', error=dict(code='lock_busy',
                      message='El control está ocupado. Vuelve a intentarlo.'))
        return answer
    if value.get('status') == 'recovery_error':
        answer = unavailable()
        answer.update(status='recovery_error', error=dict(code='recovery_error',
                      message='Estado de control no disponible. Se conserva la pausa.'))
        return answer
    if value.get('status') != 'ok':
        return unavailable()
    caps = value.get('capabilities')
    if (type(caps) is not dict or any(type(caps.get(k)) is not bool for k in
            ('slots', 'undo', 'dry_run', 'mode', 'reorder', 'restart_queue'))
            or not vector(value.get('slots'), host) or not vector(value.get('desired'), host)
            or type(value.get('revision')) is not int or value['revision'] < 1
            or (value.get('mode'), value.get('cap')) not in ((('dia', 14), ('noche', 18)) if host == 'desk' else (('fijo', 7),))
            or type(value.get('policy_hash')) is not str or len(value['policy_hash']) != 64
            or type(value.get('generation')) is not str or not 1 <= len(value['generation']) <= 160
            or type(value.get('drift')) is not bool or type(value.get('recovery_pending')) is not bool):
        raise ValueError('Capacidad inválida')
    fields = ('revision', 'policy_hash', 'generation', 'mode', 'cap', 'slots', 'desired', 'drift', 'recovery_pending')
    result = dict(schema=value['schema'], status='ok', capabilities={k: caps[k] for k in
                  ('slots', 'undo', 'dry_run', 'mode', 'reorder', 'restart_queue')}, **{k: value[k] for k in fields})
    # Reordering exists only on the Mini; a desk claiming it is not trusted.
    result['capabilities']['reorder'] = caps['reorder'] and host == 'mini' and type(value.get('reorder')) is dict
    if host == 'mini' and 'reorder' in value:
        result['reorder'] = reorder_view(value['reorder'])
    reasons = value.get('pause_reasons', {})
    if type(reasons) is not dict or any(type(g) is not str or type(r) is not list
            or any(x not in PAUSE_REASONS for x in r) for g, r in reasons.items()):
        raise ValueError('Pausas inválidas')
    result['pause_reasons'] = reasons
    holds = value.get('startup_holds', {})
    if (type(holds) is not dict or any(type(g) is not str or type(r) is not str or not 1 <= len(r) <= 300 for g,r in holds.items())):
        raise ValueError('Retención de arranque inválida')
    result['startup_holds'] = holds
    waiting = value.get('startup_waiting', {})
    if (type(waiting) is not dict or any(type(g) is not str or code != 'cuotas_sensores'
            or g in holds for g,code in waiting.items())):
        raise ValueError('Espera de arranque inválida')
    result['startup_waiting'] = waiting
    if host == 'desk':
        foreign = value.get('foreign_work', 0)
        if type(foreign) is not int or foreign < 0:
            raise ValueError('Trabajo ajeno inválido')
        result['foreign_work'] = foreign
        event, next_event, expires = value.get('schedule_event'), value.get('next_event'), value.get('expires_at')
        if (type(event) not in (int, float) or type(next_event) not in (int, float)
                or not 0 < event < next_event or next_event - event not in (9*3600, 15*3600)
                or expires is not None and (type(expires) not in (int, float) or expires != next_event)):
            raise ValueError('Evento inválido')
        result.update(schedule_event=event, next_event=next_event, expires_at=expires, **mode_fields(value))
        if caps['mode'] and result.get('mode_note') is None:
            raise ValueError('Nota de horario ausente')
        tables = value.get('tables')
        if tables is not None or caps['mode']:
            if (type(tables) is not dict or set(tables) != {'dia', 'noche'}
                    or any(not vector(tables[m], 'desk') or sum(tables[m].values()) != cap
                           for m, cap in (('dia', 14), ('noche', 18)))):
                raise ValueError('Tablas inválidas')
            result['tables'] = tables
    if host == 'mini' and 'slots_note' in value:
        if value['slots_note'] not in (None, 'Lugares ajustados · hasta otro cambio manual'):
            raise ValueError('Nota de lugares inválida')
        result['slots_note'] = value['slots_note']
    result['reason'] = None if caps['slots'] else 'Los ajustes de lugares están apagados en este host.'
    return result


def sanitize(request, response, op):
    if type(response) is not dict or response.get('schema') != 'rack-host-result@1':
        # Old controller's explicit invalid_request is an unsupported variant.
        if type(response) is dict and (response.get('error') or {}).get('code') == 'invalid_request':
            return unavailable()
        raise ValueError('Respuesta inválida')
    if op == 'capabilities':
        return control(response, request['host'])
    if response.get('action_id') != request['action_id']:
        raise ValueError('ID distinto')
    status = response.get('status')
    if op in REORDER_OPS and status == 'recovery_error':
        # A current host answers an uncertain real reorder with `unknown`; only a
        # controller without reordering rejects the op this way, before any effect.
        answer = dict(schema='rack-host-result@1', action_id=request['action_id'], status='unsupported',
                      effects=False, error=dict(code='unsupported', message='Falta actualizar el control de este host.'))
        return answer
    if status not in ('applied', 'applying', 'partial', 'unknown', 'conflict', 'rejected', 'dry_run', 'recovery_error', 'unsupported', 'aborted'):
        raise ValueError('Estado inválido')
    answer = dict(schema='rack-host-result@1', action_id=request['action_id'], status=status, error=None)
    if op == 'status' and status in ('conflict', 'rejected', 'aborted'):
        wire = {k: v for k, v in request.items() if k not in ('schema', 'host')}
        wire['schema'] = 'rack-host-control@1'
        body = json.dumps(dict(host=request['host'], request=wire), sort_keys=True, separators=(',', ':'), ensure_ascii=True)
        digest = hashlib.sha256(body.encode()).hexdigest()
        # An ordinary desk click may recover the schedule before rejecting its
        # vector. It is terminal only with its bound receipt and explicit event.
        event = response.get('schedule_recovery')
        scheduled = (request['host'] == 'desk' and request['op'] in ('slots', 'mode') and request['dry_run'] is False
                     and status == 'conflict' and (response.get('error') or {}).get('code') == 'stale_state'
                     and response.get('effects') is True and type(event) in (int, float))
        if scheduled:
            view = control(response.get('control'), 'desk')
            scheduled = view['status'] == 'ok' and event == view['schedule_event']
        if response.get('receipt_digest') != digest or status != 'aborted' and response.get('effects') is not False and not scheduled:
            raise ValueError('Rechazo sin recibo vinculado y sin efectos')
        answer['receipt_verified'] = True
    error = response.get('error')
    messages = dict(same_mode='Ese horario ya está vigente.', stale_state='Los lugares cambiaron. Actualiza antes de decidir.',
                    busy='Hay un ajuste pendiente; consulta su recibo.',
                    lock_busy='El control está ocupado. Vuelve a intentarlo.',
                    invalid_request='Cada grupo admite 1..8 lugares y se respeta el tope del modo.',
                    undo_conflict='El horario o los lugares cambiaron; no se puede deshacer este ajuste',
                    expired='El ajuste venció en el cambio de horario.',
                    idempotency_conflict='Ese ID ya pertenece a otra petición.',
                    recovery_error='Estado de control no disponible. Se conserva la pausa.',
                    disabled='Los ajustes de lugares están apagados en este host.',
                    foreign_work='Hay trabajo fuera del reparto. Resuélvelo antes de añadir lugares.',
                    unsupported='Falta actualizar el control de este host.')
    reorder = request['op'] in REORDER_OPS
    if reorder:
        messages.update(REORDER_MESSAGES, undo_conflict='La fila cambió; no se puede deshacer')
    if error is not None:
        code = error.get('code') if type(error) is dict else None
        if code not in messages:
            raise ValueError('Error inválido')
        answer['error'] = dict(code=code, message=reorder_reason(code, error.get('message'))
                               if reorder and code in REORDER_MESSAGES else messages[code])
    if type(response.get('revision')) is int:
        answer['revision'] = response['revision']
    if 'control' in response:
        answer['control'] = control(response['control'], request['host'])
    if type(response.get('effects')) is bool:
        answer['effects'] = response['effects']
    payload = response.get('result')
    if reorder:
        if payload is not None:
            answer['result'] = reorder_result(request, status, payload)
        return answer
    if payload is not None:
        if (type(payload) is not dict or not vector(payload.get('before'), request['host']) or not vector(payload.get('desired'), request['host'])
                or payload.get('observed') is not None and not vector(payload['observed'], request['host'])):
            raise ValueError('Vector inválido')
        answer['result'] = {k: payload.get(k) for k in ('before', 'desired', 'observed')}
        expires = payload.get('expires_at')
        if expires is not None and (request['host'] != 'desk' or type(expires) not in (int, float) or not 0 < expires < 10**11):
            raise ValueError('Vencimiento inválido')
        answer['result'].update(expires_at=expires, undo_of=request['action_id'] if status == 'applied' else None)
        if request['host'] == 'desk' and 'mode' in payload:
            if (payload['mode'], payload.get('cap')) not in (('dia', 14), ('noche', 18)):
                raise ValueError('Modo inválido')
            event, nxt = payload.get('schedule_event'), payload.get('next_event')
            if (type(event) not in (int, float) or type(nxt) not in (int, float)
                    or not 0 < event < nxt or nxt - event not in (9*3600, 15*3600)):
                raise ValueError('Evento inválido')
            answer['result'].update(mode=payload['mode'], cap=payload['cap'],
                                    schedule_event=event, next_event=nxt, **mode_fields(payload))
        if request['host'] == 'mini' and 'slots_note' in payload:
            if payload['slots_note'] not in (None, 'Lugares ajustados · hasta otro cambio manual'):
                raise ValueError('Nota de lugares inválida')
            answer['result']['slots_note'] = payload['slots_note']
        changed = [g for g in payload['desired'] if payload['before'][g] != payload['desired'][g]]
        observed = payload.get('observed')
        answer['result']['applied'] = [g for g in changed if observed is not None and observed[g] == payload['desired'][g]]
        answer['result']['unconfirmed'] = [g for g in changed if observed is None or observed[g] != payload['desired'][g]]
        if status == 'dry_run':
            before, target = payload['before'], payload['desired']
            plan = [(g, target[g]) for g in sorted(target) if target[g] < before[g]] + [
                   (g, target[g]) for g in sorted(target) if target[g] > before[g]]
            answer['result'].update(decreases=[dict(group=g, slots=n) for g, n in plan if n < before[g]],
                                    increases=[dict(group=g, slots=n) for g, n in plan if n > before[g]],
                                    rpc=[['parallel', str(n), '-g', g] for g, n in plan])
    return answer


def reorder_result(request, status, payload):
    """Ids, priorities and counts only; the undo handle exists only for an applied reorder."""
    group = request['expected']['group']
    order = payload.get('order') if type(payload) is dict else None
    if (type(payload) is not dict or payload.get('group') != group or type(order) is not list
            or not 2 <= len(order) <= REORDER_MAX or any(type(i) is not int or i < 0 for i in order)
            or len(set(order)) != len(order)
            or type(payload.get('writes')) is not int or not 0 <= payload['writes'] <= len(order)
            or type(payload.get('estimate_ms')) is not int or not 0 <= payload['estimate_ms'] <= 10**6
            or payload.get('held') not in (None, 'ambiguous', 'pressure')
            or payload.get('undo') not in (None, True, False)):
        raise ValueError('Resultado inválido')
    result = dict(group=group, order=list(order), writes=payload['writes'], estimate_ms=payload['estimate_ms'],
                  held=payload.get('held'), undo=payload.get('undo') is True, undo_of=None)
    if status == 'applied':
        rows = [payload.get('before'), payload.get('after')]
        if (not all(type(r) is list and all(type(x) is list and len(x) == 2 and type(x[0]) is int and int32(x[1])
                                            for x in r) and sorted(x[0] for x in r) == sorted(order) for r in rows)
                or type(payload.get('after_etag')) is not str or not HEX64.fullmatch(payload['after_etag'])
                or type(payload.get('undo_seconds')) is not int or not 0 < payload['undo_seconds'] <= 3600):
            raise ValueError('Resultado inválido')
        result.update(before=[list(x) for x in rows[0]], after=[list(x) for x in rows[1]],
                      after_etag=payload['after_etag'], undo_seconds=payload['undo_seconds'])
        if result['undo'] and request['op'] == 'reorder':
            result['undo_of'] = request['action_id']
    else:
        result['undo'] = False
    return result
