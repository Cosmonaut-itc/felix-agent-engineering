"""CLI JSON; el endpoint autenticado pertenece al servicio futuro."""
import json
import sys
from .protocol import MAX_BYTES, reply, strict_json, validate, reason
from .store import StoreError, Busy

OPERATIONS = ('reservar', 'latido', 'dudosa', 'soltar', 'estado')


def main(argv=None, *, gate=None, auth=None, stdin=None, stdout=None, stderr=None):
    args = list(sys.argv[1:] if argv is None else argv)
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    operation_id = None
    revision = gate.config.revision if gate is not None else None
    try:
        if len(args) != 1 or args[0] not in OPERATIONS:
            raise ValueError('uso: python3 -B -m hostgate {reservar|latido|dudosa|soltar|estado}')
        raw = stdin.read(MAX_BYTES + 1)
        request = validate(strict_json(raw), args[0])
        operation_id = request['operationId']
        if gate is None or auth is None:
            result = reply(operation_id, revision,
                           [reason('enforcement-unavailable', 'endpoint autenticado no implementado')], exit_code=5)
        elif args[0] == 'reservar': result = gate.reservar(request, auth)
        elif args[0] == 'latido': result = gate.latido(operation_id, request['key'], request['seq'], auth)
        elif args[0] == 'dudosa': result = gate.dudosa(operation_id, request['key'], request['reason'], request['evidenceIds'], auth)
        elif args[0] == 'soltar': result = gate.soltar(operation_id, request['key'], request['evidenceIds'], auth)
        else:
            result = gate.estado({}, auth)
            result['operationId'] = operation_id
    except KeyboardInterrupt:
        result = reply(operation_id, revision, [reason('reservation-uncertain', 'interrupcion sin prueba de fin')], exit_code=130)
    except Busy:
        result = reply(operation_id, revision, [reason('busy', 'escritor ocupado', True)], exit_code=4)
    except (StoreError, OSError):
        result = reply(operation_id, revision, [reason('store-unavailable', 'estado/entorno inutilizable')], exit_code=6)
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        print('solicitud invalida', file=stderr)
        result = reply(operation_id, revision, [reason('operation-conflict', 'schema/uso invalido')], exit_code=2)
    print(json.dumps(result, ensure_ascii=True, allow_nan=False, separators=(',', ':')), file=stdout, flush=True)
    return result['exitCode']
