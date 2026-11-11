"""CLI JSON: consulta por IPC; mutaciones sólo en sesión de supervisor."""
import json
import sys
from .protocol import MAX_BYTES, reply, strict_json, validate, reason
from .store import Busy

OPERATIONS = ('reservar', 'latido', 'dudosa', 'soltar', 'estado', 'authorize_creation', 'consume_creation', 'record_started')


def main(argv=None, *, gate=None, auth=None, stdin=None, stdout=None, stderr=None):
    args = list(sys.argv[1:] if argv is None else argv)
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    operation_id = None
    revision = gate.config.revision if gate is not None else None
    try:
        if len(args) != 1 or args[0] not in OPERATIONS:
            raise ValueError('uso: python3 -B -m hostgate {%s}' % '|'.join(OPERATIONS))
        raw = stdin.read(MAX_BYTES + 1)
        request = validate(strict_json(raw), args[0])
        operation_id = request['operationId']
    except KeyboardInterrupt:
        result = reply(operation_id, revision, [reason('reservation-uncertain', 'interrupcion sin prueba de fin')], exit_code=130)
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
        print('solicitud invalida', file=stderr)
        result = reply(operation_id, revision, [reason('operation-conflict', 'schema/uso invalido')], exit_code=2)
    except (OSError, AttributeError):
        result = reply(operation_id, revision, [reason('store-unavailable', 'entrada no disponible')], exit_code=6)
    else:
        try:
            if gate is None and args[0] == 'estado':
                from .client import HostgateClient
                # El modo sale de la configuración validada, nunca de flags ni de la key.
                with HostgateClient.connect(mode=None) as client: result = client.call(request)
            elif gate is None or auth is None:
                result = reply(operation_id, revision,
                               [reason('enforcement-unavailable', 'mutación requiere sesión persistente de supervisor')], exit_code=5)
            elif args[0] == 'reservar': result = gate.reservar(request, auth)
            elif args[0] == 'latido': result = gate.latido(operation_id, request['key'], request['seq'], auth)
            elif args[0] == 'dudosa': result = gate.dudosa(operation_id, request['key'], request['reason'], request['evidenceIds'], auth)
            elif args[0] == 'soltar': result = gate.soltar(operation_id, request['key'], request['evidenceIds'], auth)
            elif args[0] == 'authorize_creation':
                result = gate.authorize_creation(operation_id, request['key'], request['confirmation'], request['launch'], auth)
            elif args[0] == 'consume_creation':
                result = gate.consume_creation(operation_id, request['key'], request['permitId'], request['launchDigest'], auth)
            elif args[0] == 'record_started':
                result = gate.record_started(operation_id, request['key'], request['permitId'], request['root'], auth)
            else:
                result = gate.estado({}, auth)
                result['operationId'] = operation_id
        except OSError:
            print('transporte no disponible; resultado desconocido', file=stderr)
            if gate is None and args[0] == 'estado': return 6
            result = reply(operation_id, revision, [reason('store-unavailable')], exit_code=6)
        except Busy:
            result = reply(operation_id, revision, [reason('busy', 'escritor ocupado', True)], exit_code=4)
        except Exception:
            result = reply(operation_id, revision, [reason('store-unavailable', 'estado/entorno inutilizable')], exit_code=6)
        except KeyboardInterrupt:
            if gate is None and args[0] == 'estado':
                print('transporte cancelado; resultado desconocido', file=stderr)
                return 6
            result = reply(operation_id, revision, [reason('reservation-uncertain', 'interrupcion sin prueba de fin')], exit_code=130)
    print(json.dumps(result, ensure_ascii=True, allow_nan=False, separators=(',', ':')), file=stdout, flush=True)
    return result['exitCode']
