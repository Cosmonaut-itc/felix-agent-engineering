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
from .wire import TransportError
from .ipc_io import Channel, INCOMPLETE

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
            if connection is not None and (offer['requester'] is connection or offer.get('supervisor') is connection): self.offers.pop(key)
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
    channels = {}
    def send(connection, value): channels[connection['stream']].queue(value)
    def close(stream):
        channels.pop(stream).close()
        sessions.close_connection(stream)
    def dispatch(connection, request):
        operation_id = request.get('operationId') if isinstance(request, dict) else None
        called_core = False
        try:
            validate(request); lease.check()
            auth = sessions.context(connection, request)
            called_core = True; op = request['operation']
            if op == 'reservar': return gate.reservar(request, auth)
            if op == 'latido': return gate.latido(operation_id, request['key'], request['seq'], auth)
            if op == 'dudosa': return gate.dudosa(operation_id, request['key'], request['reason'], request['evidenceIds'], auth)
            if op == 'soltar': return gate.soltar(operation_id, request['key'], request['evidenceIds'], auth)
            if op == 'authorize_creation':
                return gate.authorize_creation(operation_id, request['key'], request['confirmation'], request['launch'], auth)
            if op == 'consume_creation':
                return gate.consume_creation(operation_id, request['key'], request['permitId'], request['launchDigest'], auth)
            if op == 'record_started':
                return gate.record_started(operation_id, request['key'], request['permitId'], request['root'], auth)
            result = gate.estado({}, auth); result['operationId'] = operation_id; return result
        except Busy: code, detail = 4, 'busy'
        except PermissionError: code, detail = (6, 'store-unavailable') if called_core else (5, 'identity-unverifiable')
        except admin.AdminError: code, detail = 6, 'store-unavailable'
        except (ValueError, TypeError, KeyError): code, detail = (6, 'store-unavailable') if called_core else (2, 'operation-conflict')
        except Exception: code, detail = 6, 'store-unavailable'
        return reply(operation_id, config.node.revision, [reason(detail)], exit_code=code,
                     mode=config.node.mode, store_id=config.node.store_id)
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
            listener.listen(64); listener.setblocking(False)
            if _ready: _ready()
            while True:
                readable, writable, _ = select.select(
                    [listener] + [s for s, c in channels.items() if not c.output and not c.pending],
                    [s for s, c in channels.items() if c.output or c.pending], [], 0.2)
                for stream in readable + writable:
                    try:
                        if stream is listener:
                            stream, _ = listener.accept()
                            if len(channels) >= 64: stream.close(); continue
                            channels[stream] = Channel(stream)
                            continue
                        channel = channels[stream]
                        if stream in writable:
                            channel.write(); continue
                        value = channel.read()
                        if value is INCOMPLETE: continue
                        if channel.connection is None:
                            if channel.auth is None:
                                opening = check(value, 'open', 'executorId')
                                executor = opening['executorId']
                                if executor not in config.node.executors: raise TransportError('ejecutor desconocido')
                                channel.executor = executor
                                channel.auth = peers.authentication_steps(stream, executor, channel.deadline)
                                next(channel.auth)
                                channel.challenge = peers.challenge_steps()
                                channel.queue(next(channel.challenge))
                                continue
                            try:
                                channel.queue(channel.challenge.send(value)); continue
                            except StopIteration:
                                try: next(channel.auth)
                                except StopIteration as done: peer = done.value
                            connection = dict(stream=stream, peer=peer, state='intent', deadline=channel.deadline)
                            channel.connection = connection
                            sessions.connections[stream] = connection
                            own = peers._observe(channel.executor, pid=os.getpid()).identity
                            send(connection, message('hello', physicalHostId=config.node.host_id, storeId=config.node.store_id,
                                 configRevision=config.node.revision, mode=config.node.mode, identity=identity_json(own)))
                            continue
                        connection = channel.connection
                        state = connection['state']
                        paths.check()
                        if state == 'active':
                            channel.queue(dispatch(connection, value), response=True); continue
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
                        close(stream)
                for stream, channel in list(channels.items()):
                    if channel.expired(time.monotonic()): close(stream)
    finally:
        for stream in list(channels): close(stream)
        listener.close()
        if owned is not None and path.exists() and path.lstat().st_ino == owned: path.unlink()
