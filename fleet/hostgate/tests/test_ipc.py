"""IPC privado: procesos Darwin reales; perfiles/cierres sólo de fixture."""
import copy
import hashlib
import json
import os
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


def child(role, root):
    from hostgate import admin
    from hostgate.client import HostgateClient, request_supervisor, SupervisorHandoff
    from hostgate.darwin_peer import DarwinPeers
    from hostgate.identity import from_json
    from hostgate.model import identity_json
    with fixture_policy(root), patch.object(admin, 'local_authority', return_value=Fixture(root).locator):
        if role == 'server':
            from hostgate.service import run
            class Synthetic(World):
                def profile_verified(self, profile): return profile == 'fixture'
            return run(_locator=Fixture(root).locator, _synthetic=Synthetic())
        if role == 'query':
            with HostgateClient.connect() as client: print(canonical(client.call(query())), flush=True)
            return 0
        if role == 'supervisor':
            handoff = SupervisorHandoff(**json.loads(sys.stdin.readline()))
            with HostgateClient.connect(handoff) as client:
                for line in sys.stdin:
                    print(canonical(client.call(json.loads(line))), flush=True)
            return 0
        with request_supervisor(request(physicalHostId='fixture-host', exclusiveResources=['sen-slot:1'])) as requester:
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
        policy = fixture_policy(self.root); self.addCleanup(policy.close)
        self.f = Fixture(self.root)
        self.f.doc['principals'][str(os.getuid())]['roles'].append('requester')
        self.f.doc['executors']['other'] = copy.deepcopy(self.f.doc['executors']['unix'])
        self.f.doc['principals'][str(os.getuid())]['executors'].append('other')
        self.f.provision()
        self.server = self.spawn('server')
        # Readiness uses an inherited pipe, never a probe with a false positive.
        self.assertEqual(self.server.stdout.readline().strip(), 'listening')

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

    def pair(self):
        from hostgate.darwin_peer import DarwinPeers
        from hostgate.model import identity_json
        r = self.spawn('requester'); handoff = r.stdout.readline()
        self.assertIn('offer_id', handoff)
        s = self.spawn('supervisor')
        with DarwinPeers() as peers: expected = identity_json(peers._observe('unix', pid=s.pid).identity)
        s.stdin.write(handoff); s.stdin.flush()
        r.stdin.write(canonical(expected) + '\n'); r.stdin.flush()
        self.assertEqual(r.stdout.readline().strip(), 'approved')
        def call(value):
            s.stdin.write(canonical(value) + '\n'); s.stdin.flush()
            return json.loads(s.stdout.readline())
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
        r2, s2, call2 = self.pair()
        denied = call2(original); self.assertFalse(denied['wouldGrant'])
        self.assertEqual(self.client().call(query())['budget']['retained']['slots'], 1)
        second = self.spawn('server'); out, err = second.communicate(timeout=15)
        self.assertEqual(second.returncode, 4, (out, err))
        self.assertEqual(call(dict(heartbeat, operationId='beat2', seq=2))['exitCode'], 0)

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
        for raw, length in ((b'', 0), (b'', 65537), (b'{', 2), (b'{"a":1,"a":2}', 13), (b'NaN', 3)):
            c = self.client(); c.stream.sendall(struct.pack('!I', length) + raw); c.stream.shutdown(socket.SHUT_WR)
            with self.assertRaises(OSError): receive_response(c.stream, 'query', self.root)
        self.assertEqual(before, self.inventory())


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


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] in ('server', 'query', 'supervisor', 'requester'):
        sys.exit(child(sys.argv[1], Path(sys.argv[2])))
    unittest.main()
