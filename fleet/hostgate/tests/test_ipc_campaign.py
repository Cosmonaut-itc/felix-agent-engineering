"""Campaña integrada: seams privadas de fallos; endpoint/cliente/core reales."""
from contextlib import ExitStack, contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import select
import sqlite3
import struct
import sys
from unittest.mock import patch

from hostgate import admin, darwin_peer as native, ipc_io, wire
from hostgate.client import HostgateClient, SupervisorHandoff, request_supervisor
from hostgate.model import identity_json
from hostgate.protocol import canonical
from hostgate.tests.test_ipc import query
from hostgate.tests.test_ipc_progress import Progress, server as progress_server
from hostgate.tests.support.world import request


@contextmanager
def faults(root):
    """Falla una lectura nativa, nunca reemplaza el peer completo ni el endpoint."""
    original = native.DarwinPeers._observe
    watch = native._Native.watch
    challenge = native.DarwinPeers.challenge_steps
    counters, targets = {}, {}
    def control():
        path = root / '.fault'
        return json.loads(path.read_text()) if path.exists() else {}
    def selected():
        value = control()
        return value if value.get('side') == os.getpid() else {}
    def observe(peers, executor, stream=None, pid=None):
        value = selected()
        actual_pid = peers._native.peerpid(stream) if stream is not None else pid
        targets[id(peers)] = actual_pid
        if actual_pid != value.get('pid'): return original(peers, executor, stream, pid)
        key = (id(peers), value['nonce'])
        phase = counters.get(key, 0); counters[key] = phase + 1
        index = 0
        def wrap(fn):
            def read(*args):
                nonlocal index
                index += 1
                failed = value['index'] + int(value['kind'] == 'gap')
                if value['kind'] in ('read', 'gap') and phase == value['phase'] and index == failed: raise native.PeerError('injected single native read/gap')
                result = fn(*args)
                if value['kind'] == 'divergence' and phase == value['phase'] and index == value['index']:
                    component = value['component']
                    if index == 11: return '00000000-0000-0000-0000-000000000001'
                    if index == 3: return result + 1
                    if index == 6: setattr(result, component, getattr(result, component) + 1); return result
                    result = list(result); result[component] += 1; return tuple(result)
                return result
            return read
        with ExitStack() as stack:
            for name in ('boot', 'peercred', 'peerpid', 'token', 'unique', 'bsd'):
                stack.enter_context(patch.object(peers._native, name, side_effect=wrap(getattr(peers._native, name))))
            return original(peers, executor, stream, pid)
    def register(backend, pid):
        value = selected()
        if value.get('kind') == 'register' and value.get('pid') == pid: raise native.PeerError('injected NOTE_EXIT registration')
        return watch(backend, pid)
    def exchange(peers):
        value = selected()
        if value.get('kind') == 'challenge' and value.get('pid') == targets.get(id(peers)): raise native.PeerError('injected before challenge')
        yield from challenge(peers)
    with patch.object(native.DarwinPeers, '_observe', observe), patch.object(native._Native, 'watch', register), patch.object(native.DarwinPeers, 'challenge_steps', exchange):
        yield


def altered_frames(root, value):
    mode = (root / '.wire').read_text() if (root / '.wire').exists() else ''
    if not mode:
        yield from wire.response_frames(value); return
    if mode in ('overflow', 'serialization-error'):
        yield from wire.response_frames(dict(value, x=float('nan')) if mode == 'serialization-error' else value, _maximum=1 if mode == 'overflow' else 2**64 - 1); return
    raw = canonical(value).encode('utf-8')
    if mode == 'invalid-json': raw = b'{'
    if mode == 'nonfinite': raw = b'{"schema":"fleet/host-reservation@1","operationId":"query","result":"shadow","x":1e999}'
    if mode == 'duplicate-key': raw = b'{"x":1,"x":2}'
    if mode == 'utf8': raw = json.dumps(dict(value, text='é' * 32768), ensure_ascii=False, separators=(',', ':')).encode()
    if mode == 'counter-extreme':
        yield b'\x01' + raw
        yield b'\x02' + struct.pack('!Q', 2**64 - 1) + hashlib.sha256(raw).digest(); return
    for offset in range(0, len(raw), 65536): yield b'\x01' + raw[offset:offset + 65536]
    if mode == 'cut': raise wire.TransportError('cut after DATA before END')
    end = b'\x02' + struct.pack('!Q', len(raw)) + hashlib.sha256(raw).digest()
    yield end
    if mode in ('duplicate-end', 'extra'): yield end if mode == 'duplicate-end' else b'\x01x'


def server(root):
    def checkpoint(world, name):
        cut = (root / '.cut').read_text() if (root / '.cut').exists() else ''
        if cut == 'native-revalidate' and name == 'before_revalidate':
            pid = next(iter(world.sessions.bindings.values()))[1]['peer'].identity.pid
            (root / '.fault').write_text(json.dumps(dict(side=os.getpid(), pid=pid, kind='read', phase=0, index=1, nonce='revalidate')))
        if cut == name: raise OSError('injected ' + name)
    from hostgate.service import Runtime
    with faults(root), patch.object(Runtime, 'checkpoint', checkpoint), patch.object(ipc_io, 'response_frames', lambda value: altered_frames(root, value)):
        return progress_server(root)


class Campaign(Progress):
    server_module = 'hostgate.tests.test_ipc_campaign'

    def bound_client(self):
        r = self.spawn('requester'); handoff = SupervisorHandoff(**json.loads(self.line(r)))
        with native.DarwinPeers() as peers: own = identity_json(peers._observe('unix', pid=os.getpid()).identity)
        r.stdin.write(canonical(own) + '\n'); r.stdin.flush()
        with patch.object(admin, 'local_authority', return_value=self.f.locator): client = HostgateClient.connect(handoff)
        self.addCleanup(client.close); self.assertEqual(self.line(r).strip(), 'approved')
        return client

    def sql(self):
        with sqlite3.connect(str(self.root / 'A' / 'shadow.db')) as conn:
            return {name: conn.execute('SELECT * FROM ' + name).fetchall() for name in ('operations', 'audit')}

    def test_native_reads_gaps_and_windows_through_three_observers(self):
        counter = 0
        def attempt(role):
            with patch.object(admin, 'local_authority', return_value=self.f.locator), faults(self.root):
                if role == 'H<-R':
                    with request_supervisor(request(physicalHostId='fixture-host')): pass
                elif role == 'client<-H':
                    with HostgateClient.connect() as client: self.assertEqual(client.call(query())['exitCode'], 0)
                else:
                    r = self.spawn('requester')
                    try:
                        handoff = SupervisorHandoff(**json.loads(self.line(r)))
                        with native.DarwinPeers() as peers: own = identity_json(peers._observe('unix', pid=os.getpid()).identity)
                        r.stdin.write(canonical(own) + '\n'); r.stdin.flush()
                        with HostgateClient.connect(handoff): self.assertEqual(self.line(r).strip(), 'approved')
                    finally:
                        if r.poll() is None: r.terminate()
                        r.communicate(timeout=8)
        cases = [(kind, phase, index) for phase in range(3) for kind, limit in (('read', 11), ('gap', 10)) for index in range(1, limit + 1)]
        cases += [('register', 0, 99), ('challenge', 0, 99), ('read', 3, 1)]
        variants = [(2, 0), (2, 1), (3, 0)] + [(4, c) for c in range(4)] + [(5, 1), (5, 2)] + [(6, c) for c in ('pid', 'uid', 'gid', 'start_sec', 'start_usec')] + [(11, 0)]
        cases = [(k, p, i, None) for k, p, i in cases] + [('divergence', 0, i, c) for i, c in variants]
        for role in ('H<-R', 'H<-S', 'client<-H'):
            for kind, phase, index, component in cases:
                with self.subTest(role=role, kind=kind, phase=phase, index=index, component=component):
                    attempt(role); before = (self.ledger(), self.sql())
                    counter += 1
                    value = dict(side=os.getpid() if role == 'client<-H' else self.server.pid,
                                 pid=self.server.pid if role == 'client<-H' else os.getpid(),
                                 kind=kind, phase=phase, index=index, nonce=counter, component=component)
                    (self.root / '.fault').write_text(json.dumps(value))
                    try:
                        with self.assertRaises(OSError): attempt(role)
                    finally: (self.root / '.fault').unlink()
                    if kind == 'divergence' and index == 11 and role != 'client<-H':
                        self.server.terminate(); self.server.communicate(timeout=8)
                        self.server = self.spawn('server'); self.assertEqual(self.line(self.server).strip(), 'listening')
                    self.assertEqual((self.ledger(), self.sql()), before)
                    self.assertEqual(self.snapshot()['budget']['retained']['slots'], 0)
                    print('NATIVE role={} kind={} capture={} index={} component={} positive=ok rejected ledger/operations/audit=unchanged'.format(role, kind, phase, index, component), flush=True)

    def test_commit_cuts_replay_and_restart_cannot_adopt_binding(self):
        _, _, call = self.pair()
        original = request(physicalHostId='fixture-host', exclusiveResources=['sen-slot:1'])
        for cut, retained in (('before_write', 0), ('before_commit', 0), ('after_commit', 1)):
            (self.root / '.cut').write_text(cut)
            response = call(original)
            (self.root / '.cut').unlink()
            self.assertEqual(response['exitCode'], 3)
            self.assertEqual(self.snapshot()['budget']['retained']['slots'], retained)
        replay = call(original)
        self.assertTrue(replay['replayed']); self.assertTrue(replay['originalResult']['wouldGrant'])
        self.assertFalse(replay['wouldGrant'])
        before = self.sql()
        for field, value in (('operationId', 'new'), ('executorId', 'other'), ('jobId', 'new'), ('attemptId', 'new'),
                             ('physicalHostId', 'other'), ('profileId', 'other'), ('demand', dict(original['demand'], slots=0)),
                             ('exclusiveResources', []), ('operation', 'estado'), ('budget', 999), ('auth', 1)):
            self.assertEqual(call(dict(original, **{field: value}))['exitCode'], 2)
            self.assertEqual(self.sql(), before)
        key = replay['simulationKey']
        self.server.terminate(); self.server.communicate(timeout=8)
        self.server = self.spawn('server'); self.assertEqual(self.line(self.server).strip(), 'listening')
        state = self.snapshot()
        self.assertEqual(state['reservations'][0]['key'], key)
        self.assertEqual(state['reservations'][0]['effectiveState'], 'uncertain')
        self.assertEqual(state['budget']['retained']['slots'], 1)
        self.assertEqual(self.client().call(dict(query('adopt'), operation='latido', key=key, seq=1))['exitCode'], 5)
        _, _, second = self.pair('second')
        self.assertFalse(second(request('second', physicalHostId='fixture-host', exclusiveResources=['sen-slot:1']))['wouldGrant'])

    def test_anchor_reopen_b_and_reduced_a(self):
        from hostgate.tests.test_admin import Fixture
        _, _, call = self.pair()
        grant = call(request(physicalHostId='fixture-host', exclusiveResources=['sen-slot:1']))
        self.assertTrue(grant['wouldGrant'])
        self.server.terminate(); self.server.communicate(timeout=8)
        config_a = copy.deepcopy(self.f.doc)
        # B inicializado válidamente, sin cambiar el ancla estable de A.
        b = self.root / 'B'; b.mkdir(mode=0o700)
        fixture_b = Fixture(b); fixture_b.doc['stateDir'] = str(b / 'store'); fixture_b.doc['storeId'] = 'fixture-store-B'; fixture_b.provision()
        clone = self.root / 'C'; clone.mkdir(mode=0o700)
        fixture_c = Fixture(clone); fixture_c.provision()
        for doc in (fixture_b.doc, fixture_c.doc):
            self.f.doc = dict(config_a, stateDir=doc['stateDir'], storeId=doc['storeId']); self.f.publish()
            before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.rglob('*') if p.is_file()}
            failed = self.spawn('server'); out, err = failed.communicate(timeout=8)
            self.assertEqual(failed.returncode, 6, (out, err)); self.assertEqual(out, '')
            self.assertEqual({str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.rglob('*') if p.is_file()}, before)
        self.f.doc = config_a; self.f.doc['budget']['slots'] = 0; self.f.doc['operatorRevision'] += 1; self.f.publish()
        self.server = self.spawn('server'); self.assertEqual(self.line(self.server).strip(), 'listening')
        view = self.snapshot()
        self.assertEqual(view['configRevision'], admin.load_config(self.f.locator).node.revision)
        self.assertEqual(view['reservations'][0]['key'], grant['simulationKey'])
        self.assertEqual(view['reservations'][0]['exclusiveResources'], ['sen-slot:1'])
        self.assertEqual(view['budget']['retained']['slots'], 1); self.assertEqual(view['budget']['available']['slots'], -1)


    def test_response_faults_and_complete_result_required(self):
        before = (self.ledger(), self.sql())
        spool_factory, read_frame = wire.tempfile.SpooledTemporaryFile, wire._read
        def full_spool(*args, **kwargs):
            spool = spool_factory(*args, **kwargs)
            spool.write = lambda raw: (_ for _ in ()).throw(OSError('ENOSPC on spool write'))
            return spool
        for mode in ('invalid-json', 'nonfinite', 'duplicate-key', 'counter-extreme', 'cut', 'duplicate-end', 'extra', 'utf8', 'spool-full', 'timeout', 'overflow', 'serialization-error'):
            with self.subTest(mode=mode):
                with self.client() as client:
                    self.assertEqual(client.call(query())['exitCode'], 0)  # mismo canal, sin fallo
                    if mode not in ('spool-full', 'timeout', 'overflow', 'serialization-error'): (self.root / '.wire').write_text(mode)
                    try:
                        if mode == 'utf8':
                            self.assertEqual(client.call(query())['text'], 'é' * 32768)
                        elif mode in ('duplicate-end', 'extra'):
                            self.assertEqual(client.call(query())['exitCode'], 0)
                            if client.stream.fileno() >= 0: self.assertTrue(select.select([client.stream], [], [], 2)[0])
                            with self.assertRaises(wire.TransportError): client.call(query('next'))
                        else:
                            with ExitStack() as stack:
                                if mode == 'spool-full': stack.enter_context(patch.object(wire.tempfile, 'SpooledTemporaryFile', side_effect=full_spool))
                                if mode == 'timeout':
                                    reads = []
                                    def timed_read(*args):
                                        reads.append(1)
                                        if len(reads) == 4: raise socket.timeout('injected after DATA before END')
                                        return read_frame(*args)
                                    stack.enter_context(patch.object(wire, '_read', side_effect=timed_read))
                                with self.assertRaises(wire.TransportError) as caught: client.call(query())
                            self.assertTrue(caught.exception.outcome_unknown)
                            self.assertEqual(client.stream.fileno(), -1)
                    finally:
                        if (self.root / '.wire').exists(): (self.root / '.wire').unlink()
                self.assertEqual((self.ledger(), self.sql()), before)
                self.assertEqual(self.snapshot()['exitCode'], 0)

    def test_utf8_input_exact_byte_limit(self):
        before = (self.ledger(), self.sql())
        value = query('é')
        raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
        for size in (65535, 65536, 65537):
            with self.client() as client:
                framed = raw + b' ' * (size - len(raw))
                if size <= 65536:
                    client.stream.sendall(struct.pack('!I', size) + framed)
                    self.assertEqual(wire.receive_response(client.stream, 'é', self.root)['exitCode'], 0)
                else:
                    with self.assertRaises(OSError):
                        client.stream.sendall(struct.pack('!I', size) + framed)
                        wire.receive_response(client.stream, 'é', self.root)
                self.assertEqual((self.ledger(), self.sql()), before)

    def test_confirmed_mutation_with_socket_cut_retains_one_debit(self):
        client = self.bound_client()
        (self.root / '.wire').write_text('cut')
        try:
            with self.assertRaises(wire.TransportError) as caught:
                client.call(request(physicalHostId='fixture-host', exclusiveResources=['sen-slot:1']))
        finally: (self.root / '.wire').unlink()
        self.assertTrue(caught.exception.outcome_unknown); self.assertEqual(client.stream.fileno(), -1)
        view = self.snapshot()
        self.assertEqual(len(view['reservations']), 1); self.assertEqual(view['budget']['retained']['slots'], 1)
        self.assertEqual(view['reservations'][0]['effectiveState'], 'uncertain')
        self.assertEqual(view['reservations'][0]['exclusiveResources'], ['sen-slot:1'])


    def test_native_failure_during_core_revalidation(self):
        client = self.bound_client()
        (self.root / '.cut').write_text('native-revalidate')
        try:
            denied = client.call(request(physicalHostId='fixture-host', exclusiveResources=['sen-slot:1']))
        finally:
            (self.root / '.cut').unlink()
            if (self.root / '.fault').exists(): (self.root / '.fault').unlink()
        self.assertFalse(denied['wouldGrant'])
        self.assertEqual(self.snapshot()['budget']['retained']['slots'], 0)
        _, _, positive = self.pair('after-fault')
        self.assertTrue(positive(request('after-fault', physicalHostId='fixture-host', exclusiveResources=['sen-slot:1']))['wouldGrant'])

    def test_native_change_after_grant_retains_binding_and_budget(self):
        client = self.bound_client()
        grant = client.call(request(physicalHostId='fixture-host', exclusiveResources=['sen-slot:1']))
        self.assertTrue(grant['wouldGrant'])
        before = self.ledger()['reservations'][0]
        (self.root / '.fault').write_text(json.dumps(dict(side=self.server.pid, pid=os.getpid(), kind='divergence', phase=0, index=5, component=1, nonce='held')))
        try:
            denied = client.call(dict(query('changed'), operation='latido', key=grant['simulationKey'], seq=1))
            self.assertEqual(denied['exitCode'], 6); self.assertFalse(denied['wouldGrant'])
        finally: (self.root / '.fault').unlink()
        view = self.snapshot(); row = self.ledger()['reservations'][0]
        for field in ('key', 'owner', 'channelBinding', 'demand', 'exclusiveResources', 'seq'): self.assertEqual(row[field], before[field])
        self.assertEqual(view['reservations'][0]['effectiveState'], 'uncertain')
        self.assertEqual(view['budget']['retained']['slots'], 1)
        self.assertEqual(self.client().call(dict(query('adopt'), operation='latido', key=grant['simulationKey'], seq=1))['exitCode'], 5)

    def test_acl_handoff_and_same_pid_other_channel(self):
        from hostgate.ipc import message
        original = request(physicalHostId='fixture-host', exclusiveResources=['sen-slot:1'])
        client = self.bound_client(); grant = client.call(original); self.assertTrue(grant['wouldGrant'])
        before = (self.ledger(), self.sql())
        with patch.object(admin, 'local_authority', return_value=self.f.locator):
            for field in ('uid', 'pid', 'startToken', 'role'):
                stray = HostgateClient._open(); self.addCleanup(stray.close)
                wire.send_json(stray.stream, message('query', **{field: 'forged'}))
                with self.assertRaises(OSError): wire.receive_json(stray.stream)
                self.assertEqual((self.ledger(), self.sql()), before)
            for field in ('offer_id', 'requester', 'request_digest', 'config_revision'):
                with request_supervisor(original) as offered:
                    forged = dict(offered.handoff.__dict__)
                    forged[field] = dict(forged[field], uid=os.getuid() + 1) if field == 'requester' else 'forged'
                    with self.assertRaises(OSError): HostgateClient.connect(SupervisorHandoff(**forged))
                self.assertEqual((self.ledger(), self.sql()), before)
        other = self.bound_client()  # mismo nacimiento S, conexión/binding distintos
        self.assertEqual(other.call(original)['exitCode'], 5)
        self.assertEqual(other.call(dict(query('foreign'), operation='latido', key=grant['simulationKey'], seq=1))['exitCode'], 5)
        self.assertEqual(self.ledger()['reservations'][0]['seq'], 0)
        self.assertEqual(client.call(dict(query('owner'), operation='latido', key=grant['simulationKey'], seq=1))['exitCode'], 0)
        self.assertEqual(self.snapshot()['budget']['retained']['slots'], 1)
        self.server.terminate(); self.server.communicate(timeout=8)
        self.f.doc['principals'][str(os.getuid())]['roles'].remove('query'); self.f.publish()
        self.server = self.spawn('server'); self.assertEqual(self.line(self.server).strip(), 'listening')
        before = (self.ledger(), self.sql())
        with self.assertRaises(OSError): self.client()
        self.assertEqual((self.ledger(), self.sql()), before)
        self.bound_client()  # control de la ACL: pairing permitido en la misma revisión


if __name__ == '__main__': raise SystemExit(server(Path(sys.argv[2])))
