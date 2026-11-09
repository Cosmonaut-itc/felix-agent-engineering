"""IPC privado: procesos Darwin reales; perfiles/cierres sólo de fixture."""
import copy
import hashlib
import json
import os
import select
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from hostgate.protocol import canonical
from hostgate.tests.test_admin import Fixture, fixture_policy
from hostgate.tests.support.world import World, request


def query(op='query'):
    return dict(schema='fleet/host-reservation@1', operationId=op, operation='estado')


class FixtureSocket(socket.socket):
    """Darwin limita sun_path a 104 bytes; cwd está fijado por este lanzador."""
    def bind(self, address): return super().bind(Path(address).name if isinstance(address, str) else address)
    def connect(self, address): return super().connect(Path(address).name if isinstance(address, str) else address)


def child(role, root):
    from hostgate import admin
    from hostgate.client import HostgateClient, request_supervisor, SupervisorHandoff
    from hostgate.darwin_peer import DarwinPeers
    from hostgate.identity import from_json
    from hostgate.model import identity_json
    os.chdir(root)
    with fixture_policy(root), patch.object(admin, 'local_authority', return_value=Fixture(root).locator), patch.object(socket, 'socket', FixtureSocket):
        if role == 'server':
            from hostgate.service import run
            class Synthetic(World):
                def profile_verified(self, profile): return profile == 'fixture'
            return run(_locator=Fixture(root).locator, _synthetic=Synthetic(), _ready=lambda: print('listening', flush=True))
        if role == 'query':
            with HostgateClient.connect() as client: print(canonical(client.call(query())), flush=True)
            return 0
        if role == 'supervisor':
            print('ready', flush=True)
            handoff = SupervisorHandoff(**json.loads(sys.stdin.readline()))
            with HostgateClient.connect(handoff) as client:
                for line in sys.stdin:
                    print(canonical(client.call(json.loads(line))), flush=True)
            return 0
        with request_supervisor(request(os.environ.get('IPC_OPERATION', 'op'), executorId=os.environ.get('IPC_EXECUTOR', 'unix'),
                                        physicalHostId='fixture-host', exclusiveResources=['sen-slot:1'])) as requester:
            print(canonical(requester.handoff.__dict__), flush=True)
            expected = from_json(json.loads(sys.stdin.readline()))
            requester.approve(expected)
            print('approved', flush=True)
            sys.stdin.readline()
    return 0


class IPC(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        previous = Path.cwd(); os.chdir(self.root); self.addCleanup(os.chdir, previous)
        sockets = patch.object(socket, 'socket', FixtureSocket); sockets.start(); self.addCleanup(sockets.stop)
        policy = fixture_policy(self.root); self.addCleanup(policy.close)
        self.f = Fixture(self.root)
        self.f.doc['principals'][str(os.getuid())]['roles'].append('requester')
        self.f.doc['executors']['other'] = copy.deepcopy(self.f.doc['executors']['unix'])
        self.f.doc['principals'][str(os.getuid())]['executors'].append('other')
        self.f.provision()
        self.server = self.spawn('server')
        # Readiness uses an inherited pipe, never a probe with a false positive.
        self.assertEqual(self.line(self.server).strip(), 'listening')

    def spawn(self, role, env=None):
        proc = subprocess.Popen([sys.executable, '-B', '-m', 'hostgate.tests.test_ipc', role, str(self.root)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=env)
        def cleanup():
            if proc.poll() is None: proc.terminate()
            try: proc.communicate(timeout=8)
            except subprocess.TimeoutExpired: proc.kill(); proc.communicate(timeout=8)
        self.addCleanup(cleanup)
        return proc

    def client(self):
        from hostgate import admin
        from hostgate.client import HostgateClient
        with patch.object(admin, 'local_authority', return_value=self.f.locator):
            client = HostgateClient.connect()
        self.addCleanup(client.close)
        return client

    def line(self, proc):
        self.assertTrue(select.select([proc.stdout], [], [], 12)[0], 'hijo sin respuesta dentro del deadline')
        value = proc.stdout.readline()
        if not value: self.fail('hijo terminó: ' + proc.stderr.read())
        return value

    def pair(self, op='op', executor='unix', before_attach=None):
        from hostgate.darwin_peer import DarwinPeers
        from hostgate.model import identity_json
        r = self.spawn('requester', dict(os.environ, IPC_OPERATION=op, IPC_EXECUTOR=executor)); handoff = self.line(r)
        self.assertIn('offer_id', handoff)
        if before_attach: before_attach()
        s = self.spawn('supervisor'); self.assertEqual(self.line(s).strip(), 'ready')
        with DarwinPeers() as peers: expected = identity_json(peers._observe(executor, pid=s.pid).identity)
        s.stdin.write(handoff); s.stdin.flush()
        r.stdin.write(canonical(expected) + '\n'); r.stdin.flush()
        self.assertEqual(self.line(r).strip(), 'approved')
        def call(value):
            s.stdin.write(canonical(value) + '\n'); s.stdin.flush()
            return json.loads(self.line(s))
        return r, s, call

    def inventory(self):
        return {p.name: (p.stat().st_size, p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
                for p in (self.root / 'A').glob('*') if not p.name.endswith('-shm')}

    def test_common_authority_capacity_pairing_replay_and_second_reserve(self):
        from hostgate import admin
        from hostgate.store import ShadowStore
        results = []
        for number in (1, 2):
            env = dict(os.environ, HOME=str(self.root / str(number)), FLEET_STATE_DIR=str(self.root / ('state' + str(number))),
                       XDG_STATE_HOME=str(self.root / ('xdg' + str(number))), HOSTGATE_CONFIG='fake', HOSTGATE_SOCKET='fake')
            p = self.spawn('query', env); out, err = p.communicate(timeout=15)
            self.assertEqual(p.returncode, 0, err); results.append(json.loads(out))
        self.assertEqual(results[0], results[1]); self.assertEqual(results[0]['physicalHostId'], 'fixture-host')
        r, s, call = self.pair()
        original = request(physicalHostId='fixture-host', exclusiveResources=['sen-slot:1'])
        granted = call(original); self.assertTrue(granted['wouldGrant'], granted)
        self.assertTrue(call(original)['replayed'])
        store = ShadowStore(admin.load_config(self.f.locator).node)
        before = store.read()
        for changes in ({'operationId': 'new'}, {'executorId': 'other'}, {'jobId': 'other'}, {'attemptId': 'other'},
                        {'physicalHostId': 'other'}, {'profileId': 'other'}, {'exclusiveResources': []},
                        {'demand': dict(original['demand'], cpuMilli=1)}, {'budget': 999}, {'operation': 'estado'}):
            self.assertEqual(call(dict(original, **changes))['exitCode'], 2)
            self.assertEqual(store.read(), before)
        r.stdin.close(); r.stdin = None; r.wait(timeout=8)
        heartbeat = dict(query('beat'), operation='latido', key=granted['simulationKey'], seq=1)
        self.assertEqual(call(heartbeat)['exitCode'], 0)
        self.assertEqual(self.client().call(heartbeat)['exitCode'], 5)
        r2, s2, call2 = self.pair('second', 'other')
        denied = call2(dict(original, operationId='second', executorId='other')); self.assertFalse(denied['wouldGrant']); self.assertIn('budget-exceeded', {x['code'] for x in denied['reasons']})
        self.assertEqual(self.client().call(query())['budget']['retained']['slots'], 1)
        second = self.spawn('server'); out, err = second.communicate(timeout=15)
        self.assertEqual(second.returncode, 4, (out, err))
        self.assertEqual(call(dict(heartbeat, operationId='beat2', seq=2))['exitCode'], 0)

    def test_pairing_rejects_wrong_supervisor_approval_and_dead_requester(self):
        r, s, call = self.pair(); self.assertTrue(call(request(physicalHostId='fixture-host', exclusiveResources=['sen-slot:1']))['wouldGrant'])
        before = self.inventory()
        for dead in (False, True):
            r = self.spawn('requester'); handoff = self.line(r)
            if dead: r.terminate(); r.communicate(timeout=8)
            s = self.spawn('supervisor'); self.assertEqual(self.line(s).strip(), 'ready'); s.stdin.write(handoff); s.stdin.flush()
            if not dead:
                from hostgate.darwin_peer import DarwinPeers
                from hostgate.model import identity_json
                with DarwinPeers() as peers: wrong = identity_json(peers._observe('unix', pid=os.getpid()).identity)
                r.stdin.write(canonical(wrong) + '\n'); r.stdin.flush()
                out, err = r.communicate(timeout=8); self.assertNotEqual(r.returncode, 0)
            out, err = s.communicate(timeout=8)
            self.assertNotEqual(s.returncode, 0); self.assertEqual(out, '')
        self.assertEqual(self.inventory(), before)

    def test_unregistered_connection_failure_keeps_pending_offers(self):
        from hostgate.ipc import message
        from hostgate.wire import send_json
        def rejected_open():
            stray = socket.socket(socket.AF_UNIX); self.addCleanup(stray.close)
            stray.connect(str(self.f.locator.socket_path))
            send_json(stray, message('open', executorId='desconocido'))
            self.assertTrue(select.select([stray], [], [], 12)[0], 'servidor no cerró el canal no registrado')
            self.assertEqual(stray.recv(1), b'')
        r, s, call = self.pair(before_attach=rejected_open)
        self.assertTrue(call(request(physicalHostId='fixture-host', exclusiveResources=['sen-slot:1']))['wouldGrant'])

    def test_large_core_snapshot_survives_transport_without_ledger_write(self):
        from types import SimpleNamespace
        from hostgate import admin
        from hostgate.admission import Hostgate
        from hostgate.darwin_peer import DarwinPeers
        from hostgate.model import AuthContext
        from hostgate.scope import ClosureEvidence
        from hostgate.service import Runtime
        self.server.terminate(); self.server.communicate(timeout=8)
        cfg = admin.load_config(self.f.locator); world = World(); gate = Hostgate(cfg.node, world)
        auth = world.auth(principal='uid:' + str(os.getuid()))
        for number in range(80):
            granted = gate.reservar(request('history-' + str(number), physicalHostId='fixture-host'), auth)
            self.assertTrue(granted['wouldGrant'], granted)
            key = granted['simulationKey']; world.closures[('proof',)] = ClosureEvidence.fixture(key, world.boot)
            self.assertEqual(gate.soltar('release-' + str(number), key, ['proof'], auth)['exitCode'], 0)
        self.assertTrue(gate.reservar(request('held', physicalHostId='fixture-host', exclusiveResources=['sen-slot:1']), auth)['wouldGrant'])
        with DarwinPeers() as peers:
            runtime = Runtime(cfg, peers, World()); runtime.sessions = SimpleNamespace(alive=lambda *args, **kw: False)
            own = peers._observe('unix', pid=os.getpid()).identity
            expected = Hostgate(cfg.node, runtime).estado({}, AuthContext('uid:' + str(os.getuid()), 'query', own, None, None))
        expected['operationId'] = 'query'
        self.assertGreater(len(canonical(expected).encode('ascii')), 100 * 1024)
        self.server = self.spawn('server'); self.assertEqual(self.line(self.server).strip(), 'listening')
        before = self.inventory()
        p = self.spawn('query'); out, err = p.communicate(timeout=20)
        self.assertEqual(p.returncode, 0, err); self.assertEqual(json.loads(out), expected)
        print('P14 bytes', len(out), 'sidecars', sorted(x.name for x in (self.root / 'A').iterdir()), flush=True)
        self.assertEqual(self.inventory(), before, 'SEN-5625: consulta alteró ledger/WAL durable')

    def test_input_limits_and_no_ledger_changes(self):
        from hostgate.wire import send_json, receive_response
        client = self.client(); before = self.inventory()
        for extra in ('budget', 'ncpu', 'acl', 'stateDir', 'mode', 'configRevision', 'pid', 'auth'):
            self.assertEqual(client.call(dict(query(), **{extra: 1}))['exitCode'], 2)
        self.assertEqual(before, self.inventory())
        client = self.client()
        raw = canonical(query('maximum')).encode() + b' ' * (65536 - len(canonical(query('maximum'))))
        client.stream.sendall(struct.pack('!I', len(raw)) + raw)
        self.assertEqual(receive_response(client.stream, 'maximum', self.root)['exitCode'], 0)
        for raw, length in ((b'', 0), (b'', 65537), (b'{', 2), (b'{"a":1,"a":2}', 13), (b'NaN', 3), (b'\xef\xbb\xbf{}', 5), ('é'.encode(), 2)):
            c = self.client(); c.stream.sendall(struct.pack('!I', length) + raw); c.stream.shutdown(socket.SHUT_WR)
            with self.assertRaises(OSError): receive_response(c.stream, 'query', self.root)
        self.assertEqual(before, self.inventory())

    def test_r1_cancel_after_commit(self):
        from hostgate import admin, wire
        from hostgate.client import HostgateClient, SupervisorHandoff
        from hostgate.darwin_peer import DarwinPeers
        from hostgate.model import identity_json
        r = self.spawn('requester')
        handoff = SupervisorHandoff(**json.loads(self.line(r)))
        with DarwinPeers() as peers:
            own = identity_json(peers._observe('unix', pid=os.getpid()).identity)
        r.stdin.write(canonical(own) + '\n'); r.stdin.flush()
        with patch.object(admin, 'local_authority', return_value=self.f.locator):
            client = HostgateClient.connect(handoff)
        self.addCleanup(client.close)
        self.assertEqual(self.line(r).strip(), 'approved')
        def cancel_before_end(stream, operation_id, spool_dir, maximum, *, _deadline):
            size, = struct.unpack('!I', wire._read(stream, 4, _deadline))
            kind = wire._read(stream, 1, _deadline)
            self.assertEqual(kind, b'\x01')
            data = wire._read(stream, size - 1, _deadline)
            self.assertTrue(json.loads(data)['wouldGrant'])
            raise KeyboardInterrupt('cancel before END')
        caught = None
        with patch('hostgate.client.receive_response', side_effect=cancel_before_end):
            try:
                client.call(request(physicalHostId='fixture-host', exclusiveResources=['sen-slot:1']))
            except BaseException as exc:
                caught = exc
        state = self.client().call(query('after-cancel'))
        print('R1_CANCEL exception={} outcome_unknown={} fd={} retained={} reservations={}'.format(
            type(caught).__name__, getattr(caught, 'outcome_unknown', None), client.stream.fileno(),
            state['budget']['retained']['slots'], len(state['reservations'])), flush=True)
        self.assertEqual(client.stream.fileno(), -1)
        self.assertEqual(state['budget']['retained']['slots'], 1)
        self.assertEqual(len(state['reservations']), 1)
        self.assertIsInstance(caught, wire.TransportError)
        self.assertIs(caught.outcome_unknown, True)

    def test_r1_query_only_second_executor(self):
        from hostgate import admin, wire
        from hostgate.client import HostgateClient
        from hostgate.ipc import check, message
        self.server.terminate(); self.server.communicate(timeout=8)
        self.f.doc['principals'][str(os.getuid())]['executors'] = ['unix']
        self.f.publish()
        cfg = admin.load_config(self.f.locator)
        self.server = self.spawn('server')
        self.assertEqual(self.line(self.server).strip(), 'listening')
        failure, default_result = None, None
        with patch.object(admin, 'local_authority', return_value=self.f.locator):
            try:
                with HostgateClient.connect() as client:
                    default_result = client.call(query('default'))
            except Exception as exc:
                failure = type(exc).__name__ + ': ' + str(exc)
            control = HostgateClient._open('unix')
        self.addCleanup(control.close)
        wire.send_json(control.stream, message('query'), control.deadline)
        check(wire.receive_json(control.stream, control.deadline), 'ready')
        positive = control.call(query('positive'))
        print('R1_EXECUTOR catalog={} permitted={} default={} explicit_unix_exit={}'.format(
            cfg.node.executors, cfg.principals[str(os.getuid())]['executors'], failure,
            positive['exitCode']), flush=True)
        self.assertEqual(positive['exitCode'], 0)
        self.assertIsNotNone(default_result, 'authorized query must choose an allowed executor')
        self.assertEqual(default_result['exitCode'], 0)

    def test_r1_query_without_permitted_executor(self):
        from hostgate import admin
        from hostgate.client import HostgateClient
        from hostgate.wire import TransportError
        self.server.terminate(); self.server.communicate(timeout=8)
        self.f.doc['principals'][str(os.geteuid())]['executors'] = []
        self.f.publish()
        cfg = admin.load_config(self.f.locator)
        self.assertEqual(cfg.principals[str(os.geteuid())]['executors'], ())
        self.server = self.spawn('server')
        self.assertEqual(self.line(self.server).strip(), 'listening')
        with patch.object(admin, 'local_authority', return_value=self.f.locator):
            with self.assertRaisesRegex(TransportError, 'ningún ejecutor permitido') as caught:
                HostgateClient.connect()
        print('R1_NO_EXECUTOR', str(caught.exception), flush=True)


class Framing(unittest.TestCase):
    def test_complete_fragmented_response_and_interruptions(self):
        from hostgate.wire import receive_response, send_response, TransportError, ResponseLimitError
        from hostgate.protocol import reply
        value = reply('large', 'r', rows=['é' * 100] * 200)
        with tempfile.TemporaryDirectory() as root:
            class Capture:
                def __init__(self): self.data = bytearray()
                def settimeout(self, timeout): pass
                def sendall(self, data): self.data.extend(data)
            output = Capture(); send_response(output, value)
            raw = bytes(output.data); self.assertGreater(len(raw), 65536)
            class Stream:
                def __init__(self, data): self.data = data
                def settimeout(self, timeout): pass
                def recv(self, size): result, self.data = self.data[:size], self.data[size:]; return result
            self.assertEqual(receive_response(Stream(raw), 'large', Path(root)), value)
            for cut in (1, 4, 6, 65540, len(raw) - 41, len(raw) - 1):
                with self.subTest(cut=cut), self.assertRaises(TransportError):
                    receive_response(Stream(raw[:cut]), 'large', Path(root))
            for altered in (raw[:-1] + bytes([raw[-1] ^ 1]), raw[:-40] + struct.pack('!Q', 1) + raw[-32:],
                            struct.pack('!I', 1) + b'\x01', struct.pack('!I', 2) + b'\x09x'):
                with self.assertRaises(TransportError): receive_response(Stream(altered), 'large', Path(root))
            with self.assertRaises(ResponseLimitError): receive_response(Stream(raw), 'large', Path(root), 20)
            self.assertEqual(receive_response(Stream(raw), 'large', Path(root), 1000000), value)
            for invalid, maximum in ((dict(value, rows=[float('nan')]), 2**64 - 1), (value, 65536)):
                output = Capture()
                with self.assertRaises(TransportError): send_response(output, invalid, _maximum=maximum)
                with self.assertRaises(TransportError): receive_response(Stream(bytes(output.data)), 'large', Path(root))


class ClientHandshake(unittest.TestCase):
    def test_server_facts_must_match_kernel_and_config_before_intent(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from hostgate import admin
        from hostgate.client import HostgateClient
        from hostgate.identity import Identity
        from hostgate.ipc import message
        from hostgate.model import identity_json
        from hostgate.wire import TransportError
        identity = Identity('unix', 501, 123, (1, 2, 3, 4), 'boot', 'boot')
        cfg = SimpleNamespace(principals={str(os.geteuid()): {'executors': ('unix',)}}, service_uid=501, service_gid=20, node=SimpleNamespace(executors=['unix'], host_id='host', store_id='store', revision='cfg'))
        hello = message('hello', physicalHostId='host', storeId='store', configRevision='cfg', mode='shadow', identity=identity_json(identity))
        variants = [hello] + [dict(hello, **{key: value}) for key, value in
            [('physicalHostId', 'fake'), ('storeId', 'fake'), ('configRevision', 'fake'), ('mode', 'enforcing'),
             ('identity', dict(hello['identity'], pid=124))]]
        for response, uid in [(v, 501) for v in variants] + [(hello, 502)]:
            stream, peers = Mock(), Mock()
            peers.authenticate.return_value = SimpleNamespace(identity=identity, egid=20)
            peers.alive.return_value = True; cfg.service_uid = uid
            with patch.object(admin, 'load_config', return_value=cfg), patch('hostgate.client.socket.socket', return_value=stream), \
                    patch('hostgate.client.DarwinPeers', return_value=peers), patch('hostgate.client.receive_json', return_value=response):
                if response == hello and uid == 501: HostgateClient._open().close()
                else:
                    with self.assertRaises(TransportError): HostgateClient._open()
                self.assertEqual(stream.sendall.call_count, 1)  # sólo open; ninguna intención/aprobación/RPC
                peers.close.assert_called_once()


class ClientCancellation(unittest.TestCase):
    def test_r1_pairing_cancellation_closes_and_reports_unknown(self):
        from unittest.mock import Mock
        from hostgate import client as api
        from hostgate.wire import TransportError
        for entry in ('connect', 'request_supervisor', 'approve'):
            with self.subTest(entry=entry):
                client = Mock()
                with patch.object(api.HostgateClient, '_open', return_value=client), \
                        patch.object(api, 'send_json'), \
                        patch.object(api, 'receive_json', side_effect=KeyboardInterrupt('cancel handshake')):
                    try:
                        if entry == 'connect': api.HostgateClient.connect()
                        elif entry == 'request_supervisor': api.request_supervisor(request())
                        else: api.RequesterSession(client, None).approve(None)
                    except BaseException as exc:
                        caught = exc
                    else: self.fail('cancelled handshake returned a session')
                client.close.assert_called_once()
                self.assertIsInstance(caught, TransportError)
                self.assertIs(caught.outcome_unknown, True)

    def test_r1_open_cancellation_closes_and_reports_unknown(self):
        from unittest.mock import Mock
        from hostgate import client as api
        from hostgate.wire import TransportError
        stream, peers = Mock(), Mock()
        with patch.object(api.socket, 'socket', return_value=stream), \
                patch.object(api, 'DarwinPeers', return_value=peers), \
                patch.object(api.admin, 'load_config', side_effect=KeyboardInterrupt('cancel open')):
            try: api.HostgateClient._open()
            except BaseException as exc: caught = exc
            else: self.fail('cancelled open returned a session')
        stream.close.assert_called_once(); peers.close.assert_called_once()
        self.assertIsInstance(caught, TransportError)
        self.assertIs(caught.outcome_unknown, True)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] in ('server', 'query', 'supervisor', 'requester'):
        sys.exit(child(sys.argv[1], Path(sys.argv[2])))
    unittest.main()
