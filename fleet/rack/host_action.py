"""Closed Mini host-control variant; cancellation keeps its original contract."""
import re

OPS = ('host_capabilities', 'slots', 'undo')
GROUPS = {'suites', 'default', 'rapidas'}
ACTION = re.compile(r'[A-Za-z0-9-]{8,64}')


def vector(value):
    return (type(value) is dict and set(value) == GROUPS
            and all(type(n) is int and 1 <= n <= 8 for n in value.values()))


def validate(request):
    if type(request) is not dict or request.get('op') not in OPS:
        raise ValueError('Operación inválida')
    keys = {'schema', 'op', 'host'}
    if request['op'] != 'host_capabilities':
        keys |= {'action_id', 'dry_run', 'expected', 'change' if request['op'] == 'slots' else 'undo_of'}
        if (type(request.get('action_id')) is not str or not ACTION.fullmatch(request['action_id'])
                or type(request.get('dry_run')) is not bool):
            raise ValueError('Identidad inválida')
        e = request.get('expected')
        if (type(e) is not dict or set(e) != {'revision', 'policy_hash', 'generation', 'mode', 'slots'}
                or type(e['revision']) is not int or e['revision'] < 1
                or type(e['policy_hash']) is not str or len(e['policy_hash']) != 64
                or type(e['generation']) is not str or not 1 <= len(e['generation']) <= 160
                or e['mode'] != 'fijo' or not vector(e['slots'])):
            raise ValueError('Estado inválido')
        if request['op'] == 'slots':
            c = request.get('change')
            if (type(c) is not dict or set(c) != {'group', 'delta'}
                    or type(c['group']) is not str or c['group'] not in GROUPS
                    or type(c['delta']) is not int or c['delta'] not in (-1, 1)):
                raise ValueError('Cambio inválido')
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


def control(value):
    """Read view whitelist; never publish arbitrary controller fields or error text."""
    if type(value) is not dict or value.get('schema') != 'rack-host-result@1':
        return unavailable()
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
            or not vector(value.get('slots')) or not vector(value.get('desired'))
            or type(value.get('revision')) is not int or value['revision'] < 1
            or value.get('cap') != 7 or value.get('mode') != 'fijo'
            or type(value.get('policy_hash')) is not str or len(value['policy_hash']) != 64
            or type(value.get('generation')) is not str or not 1 <= len(value['generation']) <= 160
            or type(value.get('drift')) is not bool or type(value.get('recovery_pending')) is not bool):
        raise ValueError('Capacidad inválida')
    fields = ('revision', 'policy_hash', 'generation', 'mode', 'cap', 'slots', 'desired', 'drift', 'recovery_pending')
    result = dict(schema=value['schema'], status='ok', capabilities={k: caps[k] for k in
                  ('slots', 'undo', 'dry_run', 'mode', 'reorder', 'restart_queue')}, **{k: value[k] for k in fields})
    reasons = value.get('pause_reasons', {})
    if type(reasons) is not dict or any(type(g) is not str or type(r) is not list
            or any(x not in ('pressure', 'manual', 'ambiguous') for x in r) for g, r in reasons.items()):
        raise ValueError('Pausas inválidas')
    result['pause_reasons'] = reasons
    result['reason'] = None if caps['slots'] else 'Los ajustes de lugares están apagados en la Mini.'
    return result


def sanitize(request, response, op):
    if type(response) is not dict or response.get('schema') != 'rack-host-result@1':
        # Old controller's explicit invalid_request is an unsupported variant.
        if type(response) is dict and (response.get('error') or {}).get('code') == 'invalid_request':
            return unavailable()
        raise ValueError('Respuesta inválida')
    if op == 'capabilities':
        return control(response)
    if response.get('action_id') != request['action_id']:
        raise ValueError('ID distinto')
    status = response.get('status')
    if status not in ('applied', 'applying', 'partial', 'unknown', 'conflict', 'rejected', 'dry_run', 'recovery_error', 'unsupported'):
        raise ValueError('Estado inválido')
    answer = dict(schema='rack-host-result@1', action_id=request['action_id'], status=status, error=None)
    error = response.get('error')
    messages = dict(stale_state='Los lugares cambiaron. Actualiza antes de decidir.',
                    busy='Hay un ajuste pendiente; consulta su recibo.',
                    invalid_request='Cada grupo admite 1..8 lugares y el tope es 7.',
                    undo_conflict='Los lugares cambiaron. No se puede deshacer.',
                    idempotency_conflict='Ese ID ya pertenece a otra petición.',
                    recovery_error='Estado de control no disponible. Se conserva la pausa.',
                    disabled='Los ajustes de lugares están apagados en la Mini.',
                    unsupported='Falta actualizar el control de este host.')
    if error is not None:
        code = error.get('code') if type(error) is dict else None
        if code not in messages:
            raise ValueError('Error inválido')
        answer['error'] = dict(code=code, message=messages[code])
    if type(response.get('revision')) is int:
        answer['revision'] = response['revision']
    if 'control' in response:
        answer['control'] = control(response['control'])
    if type(response.get('effects')) is bool:
        answer['effects'] = response['effects']
    payload = response.get('result')
    if payload is not None:
        if (type(payload) is not dict or not vector(payload.get('before')) or not vector(payload.get('desired'))
                or payload.get('observed') is not None and not vector(payload['observed'])):
            raise ValueError('Vector inválido')
        answer['result'] = {k: payload.get(k) for k in ('before', 'desired', 'observed')}
        answer['result'].update(expires_at=None, undo_of=request['action_id'] if status == 'applied' else None)
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
