"""Sesiones efímeras: identidad nativa, dos aprobaciones y canal original."""
import os
import secrets
import select
import socket
import stat
import time
from dataclasses import dataclass
from . import admin
from .model import AuthContext, identity_json
from .protocol import digest, validate, reply, reason
from .store import Busy
from .wire import receive_json, send_json, send_response, TransportError

SCHEMA = 'fleet/hostgate-ipc@1'


def message(kind, **fields): return dict(schema=SCHEMA, kind=kind, **fields)


def check(value, kind, fields=''):
    if (not isinstance(value, dict) or value.get('schema') != SCHEMA or value.get('kind') != kind or
            set(value) != {'schema', 'kind'} | set(fields.split())):
        raise TransportError('handshake inválido')
    return value


@dataclass(frozen=True)
class BoundSession:
    supervisor: object
    requester: object
    binding: str
    request_digest: str
    executor_id: str
    reservation_operation_id: str
    config_revision: str


class Sessions:
    def __init__(self, config, peers):
        self.config, self.peers, self.connections, self.bindings, self.offers = config, peers, {}, {}, {}

    def allowed(self, connection, role):
        identity = connection['peer'].identity
        policy = self.config.principals.get(str(identity.uid), {})
        return role in policy.get('roles', ()) and identity.executor_id in policy.get('executors', ())

    def live(self, connection):
        return connection in self.connections.values() and self.peers.alive(connection['stream'], connection['peer'])

    def alive(self, binding, *, pending=False):
        pair = self.bindings.get(binding)
        return bool(pair and self.live(pair[1]) and (not pending or self.live(pair[0])))

    def bind(self, requester, supervisor, request):
        if not self.live(requester) or not self.live(supervisor): raise TransportError('pairing sin canales vivos')
        session = BoundSession(supervisor['peer'], requester['peer'], secrets.token_hex(32), digest(request),
                               request['executorId'], request['operationId'], self.config.node.revision)
        self.bindings[session.binding] = (requester, supervisor)
        supervisor.update(state='active', session=session)
        requester['state'] = 'requester'
        return session

    def context(self, connection, request):
        session = connection.get('session')
        if not self.live(connection): raise TransportError('peer/canal cambiado')
        if session is None:
            if request['operation'] != 'estado': raise PermissionError('consulta sin supervisor')
            return AuthContext('uid:' + str(connection['peer'].identity.uid), 'query', connection['peer'].identity, None, None)
        if session.config_revision != self.config.node.revision or not self.alive(session.binding): raise TransportError('sesión inválida')
        if request['operation'] == 'reservar' and (request['executorId'] != session.executor_id or
                request['operationId'] != session.reservation_operation_id or digest(request) != session.request_digest):
            raise ValueError('reserva distinta de la aprobada')
        return AuthContext('uid:' + str(session.supervisor.identity.uid), 'supervisor',
                           session.supervisor.identity, session.requester.identity, session.binding)

    def close_connection(self, stream):
        connection = self.connections.pop(stream, None)
        for key, offer in list(self.offers.items()):
            if offer['requester'] is connection or offer.get('supervisor') is connection: self.offers.pop(key)
        stream.close()
        # alive retira el watch por descriptor cerrado, conservando el binding muerto.
        if connection: self.peers.alive(stream, connection['peer'])

    def close(self):
        for stream in list(self.connections): self.close_connection(stream)


def serve(config, lease, peers, world, *, _ready=None):
    from .admission import Hostgate
    sessions = Sessions(config, peers); world.sessions = sessions
    gate, listener, owned = Hostgate(config.node, world), socket.socket(socket.AF_UNIX), None
    path = lease.locator.socket_path
    def send(connection, value): send_json(connection['stream'], value, connection['deadline'])
    def dispatch(connection, request):
        operation_id = request.get('operationId') if isinstance(request, dict) else None
        try:
            validate(request); lease.check()
            auth = sessions.context(connection, request)
            op = request['operation']
            if op == 'reservar': return gate.reservar(request, auth)
            if op == 'latido': return gate.latido(operation_id, request['key'], request['seq'], auth)
            if op == 'dudosa': return gate.dudosa(operation_id, request['key'], request['reason'], request['evidenceIds'], auth)
            if op == 'soltar': return gate.soltar(operation_id, request['key'], request['evidenceIds'], auth)
            result = gate.estado({}, auth); result['operationId'] = operation_id; return result
        except Busy: code, detail = 4, 'busy'
        except PermissionError: code, detail = 5, 'identity-unverifiable'
        except admin.AdminError: code, detail = 6, 'store-unavailable'
        except (ValueError, TypeError, KeyError): code, detail = 2, 'operation-conflict'
        except Exception: code, detail = 6, 'store-unavailable'
        return reply(operation_id, config.node.revision, [reason(detail)], exit_code=code)
    try:
        lease.check()
        with admin._Paths(config) as paths:
            paths.open(path.parent, 'state'); paths.check()
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != config.service_uid or info.st_nlink != 1:
                    raise admin.AdminError('socket residual inseguro')
                path.unlink()
            listener.bind(str(path)); path.chmod(0o600); owned = path.lstat().st_ino
            listener.listen(64); listener.settimeout(0.2)
            if _ready: _ready()
            while True:
                readable = select.select([listener] + list(sessions.connections), [], [], 0.2)[0]
                for stream in readable:
                    try:
                        if stream is listener:
                            stream, _ = listener.accept()
                            if len(sessions.connections) >= 64: stream.close(); continue
                            deadline = time.monotonic() + 5
                            opening = check(receive_json(stream, deadline), 'open', 'executorId')
                            executor = opening['executorId']
                            if executor not in config.node.executors: raise TransportError('ejecutor desconocido')
                            peer = peers.authenticate(stream, executor, _deadline=deadline)
                            connection = dict(stream=stream, peer=peer, state='intent', deadline=deadline)
                            sessions.connections[stream] = connection
                            own = peers._observe(executor, pid=os.getpid()).identity
                            send(connection, message('hello', physicalHostId=config.node.host_id, storeId=config.node.store_id,
                                 configRevision=config.node.revision, mode='shadow', identity=identity_json(own)))
                            continue
                        connection = sessions.connections[stream]
                        state = connection['state']
                        deadline = connection['deadline'] if state != 'active' else time.monotonic() + 300
                        value = receive_json(stream, deadline)
                        paths.check()
                        if state == 'active':
                            send_response(stream, dispatch(connection, value), _deadline=deadline); continue
                        if not sessions.live(connection): raise TransportError('peer no verificable')
                        lease.check()
                        if state == 'intent':
                            kind = value.get('kind') if isinstance(value, dict) else None
                            if kind == 'query':
                                check(value, kind)
                                if not sessions.allowed(connection, 'query'): raise TransportError('ACL query')
                                connection['state'] = 'active'; send(connection, message('ready'))
                            elif kind == 'offer':
                                check(value, kind, 'request'); request = validate(value['request'], 'reservar')
                                if (not sessions.allowed(connection, 'requester') or request['executorId'] != connection['peer'].identity.executor_id or
                                        request['physicalHostId'] != config.node.host_id): raise TransportError('oferta no autorizada')
                                offer_id = secrets.token_hex(32)
                                sessions.offers[offer_id] = dict(requester=connection, request=request)
                                connection.update(state='offered', offer_id=offer_id)
                                send(connection, message('offer', offer_id=offer_id, requester=identity_json(connection['peer'].identity),
                                     request_digest=digest(request), config_revision=config.node.revision))
                            elif kind == 'attach':
                                check(value, kind, 'offer_id requester request_digest config_revision')
                                offer = sessions.offers[value['offer_id']]; r = offer['requester']; request = offer['request']
                                if (offer.get('supervisor') or not sessions.live(r) or not sessions.allowed(connection, 'supervisor') or
                                        r['peer'].identity.uid != connection['peer'].identity.uid or r['peer'].identity.executor_id != connection['peer'].identity.executor_id or
                                        value != message('attach', offer_id=value['offer_id'], requester=identity_json(r['peer'].identity),
                                                         request_digest=digest(request), config_revision=config.node.revision)):
                                    raise TransportError('attach no corresponde')
                                offer['supervisor'] = connection; connection.update(state='attached', offer_id=value['offer_id'])
                                send(r, message('supervisor', identity=identity_json(connection['peer'].identity)))
                                send(connection, message('requester', identity=identity_json(r['peer'].identity)))
                            else: raise TransportError('intención inválida')
                        elif state in ('offered', 'attached'):
                            offer = sessions.offers[connection['offer_id']]
                            other = offer.get('supervisor') if state == 'offered' else offer['requester']
                            check(value, 'approve', 'identity')
                            if other is None or value['identity'] != identity_json(other['peer'].identity): raise TransportError('aprobación ajena')
                            connection['approved'] = True
                            if other.get('approved'):
                                sessions.bind(offer['requester'], offer['supervisor'], offer['request'])
                                send(offer['supervisor'], message('ready')); send(offer['requester'], message('ready'))
                                sessions.offers.pop(connection['offer_id'])
                        else: raise TransportError('tráfico no solicitado')
                    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
                        if stream is listener: raise
                        sessions.close_connection(stream)
                for stream, connection in list(sessions.connections.items()):
                    if connection['state'] not in ('active', 'requester') and time.monotonic() >= connection['deadline']:
                        sessions.close_connection(stream)
    finally:
        sessions.close(); listener.close()
        if owned is not None and path.exists() and path.lstat().st_ino == owned: path.unlink()
