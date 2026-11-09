"""Única entrada de consumidor: locator fijo, servidor nativo y sesión persistente."""
import os
import select
import socket
import time
from dataclasses import dataclass
from . import admin
from .darwin_peer import DarwinPeers
from .model import identity_json
from .ipc import message, check
from .protocol import validate
from .wire import send_json, receive_json, receive_response, TransportError, ResponseLimitError


@dataclass(frozen=True)
class SupervisorHandoff:
    offer_id: str
    requester: object
    request_digest: str
    config_revision: str


class HostgateClient:
    @classmethod
    def _open(cls, executor=None):
        self = cls(); self.stream, self.peers = socket.socket(socket.AF_UNIX), DarwinPeers()
        try:
            locator = admin.local_authority(); config = admin.load_config(locator)
            self.spool_dir, self.deadline = locator.socket_path.parent, time.monotonic() + 5
            if executor is None:
                permitted = config.principals.get(str(os.geteuid()), {}).get('executors', ())
                executor = next((e for e in config.node.executors if e in permitted), None)
                if executor is None: raise TransportError('ningún ejecutor permitido para uid local')
            self.stream.settimeout(5); self.stream.connect(str(locator.socket_path))
            send_json(self.stream, message('open', executorId=executor), self.deadline)
            self.peer = self.peers.authenticate(self.stream, executor, _deadline=self.deadline)
            hello = check(receive_json(self.stream, self.deadline), 'hello', 'physicalHostId storeId configRevision mode identity')
            if (self.peer.identity.uid != config.service_uid or self.peer.egid != config.service_gid or
                    hello != message('hello', physicalHostId=config.node.host_id, storeId=config.node.store_id,
                                     configRevision=config.node.revision, mode='shadow', identity=identity_json(self.peer.identity)) or
                    not self.peers.alive(self.stream, self.peer)):
                raise TransportError('servidor contradice autoridad administrativa/kernel')
            return self
        except BaseException as exc:
            self.close()
            if isinstance(exc, KeyboardInterrupt): raise TransportError('sesión cancelada; resultado desconocido') from exc
            raise

    @classmethod
    def connect(cls, handoff=None):
        self = cls._open(None if handoff is None else handoff.requester['executorId'])
        try:
            if handoff is None: send_json(self.stream, message('query'), self.deadline)
            else:
                send_json(self.stream, message('attach', **handoff.__dict__), self.deadline)
                remote = check(receive_json(self.stream, self.deadline), 'requester', 'identity')
                if remote['identity'] != handoff.requester: raise TransportError('solicitante ajeno')
                send_json(self.stream, message('approve', identity=remote['identity']), self.deadline)
            check(receive_json(self.stream, self.deadline), 'ready')
            return self
        except BaseException as exc:
            self.close()
            if isinstance(exc, KeyboardInterrupt): raise TransportError('sesión cancelada; resultado desconocido') from exc
            raise

    def call(self, request, *, max_response_bytes=256 * 1024**2):
        try:
            if not self.peers.alive(self.stream, self.peer): raise TransportError('servidor/canal perdió identidad')
            if select.select([self.stream], [], [], 0)[0]: raise TransportError('tráfico no solicitado')
            deadline = time.monotonic() + 300
            send_json(self.stream, request, deadline)
            result = receive_response(self.stream, request['operationId'], self.spool_dir, max_response_bytes, _deadline=deadline)
            if select.select([self.stream], [], [], 0)[0]: self.close()
            return result
        except BaseException as exc:
            self.close()
            if isinstance(exc, (OSError, ValueError, TypeError, KeyError, MemoryError, KeyboardInterrupt)) and not isinstance(exc, TransportError):
                raise TransportError('operación sin respuesta íntegra; resultado desconocido') from exc
            raise

    def close(self):
        self.stream.close(); self.peers.close()

    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class RequesterSession:
    def __init__(self, client, handoff): self.client, self.handoff = client, handoff

    def approve(self, original_supervisor):
        try:
            remote = check(receive_json(self.client.stream, self.client.deadline), 'supervisor', 'identity')
            if remote['identity'] != identity_json(original_supervisor): raise TransportError('supervisor no es el original')
            send_json(self.client.stream, message('approve', identity=remote['identity']), self.client.deadline)
            check(receive_json(self.client.stream, self.client.deadline), 'ready')
        except BaseException as exc:
            self.close()
            if isinstance(exc, KeyboardInterrupt): raise TransportError('sesión cancelada; resultado desconocido') from exc
            raise

    def close(self): self.client.close()
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


def request_supervisor(request):
    validate(request, 'reservar')
    client = HostgateClient._open(request['executorId'])
    try:
        send_json(client.stream, message('offer', request=request), client.deadline)
        offer = check(receive_json(client.stream, client.deadline), 'offer', 'offer_id requester request_digest config_revision')
        return RequesterSession(client, SupervisorHandoff(**{k: v for k, v in offer.items() if k not in ('schema', 'kind')}))
    except BaseException as exc:
        client.close()
        if isinstance(exc, KeyboardInterrupt): raise TransportError('sesión cancelada; resultado desconocido') from exc
        raise
