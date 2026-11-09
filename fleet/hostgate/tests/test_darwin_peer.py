"""P07/P08/P09/P13: controles nativos y fallos de una sola observación."""
import ctypes as C
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from hostgate import darwin_peer as d
from hostgate.admission import Hostgate
from hostgate.model import AuthContext
from hostgate.store import ShadowStore
from hostgate.salud.lector_mini import evaluar
from hostgate.tests.support.world import World, config, request

U = '11111111-2222-3333-4444-555555555555'
T = (1792738812, 550262)
ORDER = ['U', 'cred', 'pid', 'token', 'unique', 'BSD', 'unique', 'token', 'pid', 'cred', 'U']
ROLES = ('H<-R', 'H<-S', 'cliente<-H')


class Watch:
    def __init__(self): self.exited = self.closed = False
    def check(self):
        if self.closed or self.exited: raise d.PeerError('NOTE_EXIT')
    def close(self): self.closed = True


class Native:
    """Una barrera después de cada lectura, sin sustituir el peer entero."""
    def __init__(self):
        self.calls, self.after, self.bad, self.generation = [], None, None, 0
        self.uuid, self.time = U, T
        self.watch_object = Watch()
    def read(self, name, value):
        self.calls.append(name)
        if self.bad == len(self.calls): raise PermissionError('EPERM ' + name)
        if self.after: self.after(len(self.calls), name)
        return value
    def boot(self): return self.read('U', self.uuid)
    def boottime(self): return self.read('T', self.time)
    def peercred(self, s): return self.read('cred', (501, 20))
    def peerpid(self, s): return self.read('pid', 123)
    def token(self, s): return self.read('token', (123, 501, 20, 7 + self.generation))
    def unique(self, pid): return self.read('unique', (b'x' * 16, 41 + self.generation, 7 + self.generation))
    def bsd(self, pid):
        return self.read('BSD', SimpleNamespace(pid=123, uid=501, gid=20, ruid=999, rgid=999,
                                               status=2, start_sec=100, start_usec=2))
    def watch(self, pid):
        self.read('register', None)
        return self.watch_object


class PeerMatrix(unittest.TestCase):
    def fresh(self):
        self.doCleanups()  # Cada barrera cierra sus sockets antes de la siguiente.
        native = Native()
        peers = d.DarwinPeers(_native=native)
        self.addCleanup(peers.close)
        native.calls.clear()
        a, b = socket.socketpair()
        self.addCleanup(a.close); self.addCleanup(b.close)
        native.remote = b
        return native, peers, a

    def test_positive_capture_verify_and_all_observer_roles(self):
        for role in ROLES:
            with self.subTest(role=role):
                n, p, s = self.fresh()
                peer = p.capture(s, 'unix')
                self.assertEqual(n.calls, ORDER)
                self.assertEqual(peer.identity.start_token, (100, 2, 41, 7))
                self.assertEqual(peer.identity.physical_boot, U)
                self.assertEqual(peer.egid, 20)  # ruid/rgid diferentes son legítimos.
                self.assertEqual(p.verify(peer.identity).status, 'same')
                with patch.object(p, '_challenge'):
                    self.assertEqual(p.authenticate(s, 'unix'), peer)
                self.assertTrue(p.alive(s, peer))

    def test_each_source_disagrees_alone(self):
        variants = [('peercred', (502, 20)), ('peercred', (501, 21)), ('peerpid', 124),
                    ('token', (124, 501, 20, 7)), ('token', (123, 502, 20, 7)),
                    ('token', (123, 501, 21, 7)), ('token', (123, 501, 20, 8)),
                    ('unique', (b'x' * 16, 41, 8))]
        for field, value in [('pid', 124), ('uid', 502), ('gid', 21), ('status', 5),
                             ('status', 0), ('start_sec', 0), ('start_usec', 1000000)]:
            bsd = Native().bsd(123); setattr(bsd, field, value)
            variants.append(('bsd', bsd))
        for role in ROLES:
            for source, value in variants:
                with self.subTest(role=role, source=source, value=value):
                    n, p, s = self.fresh()
                    with patch.object(n, source, return_value=value), self.assertRaises(d.PeerError):
                        p.capture(s, 'unix')

    def test_every_read_error_and_every_native_gap(self):
        for role in ROLES:
            for phase in (0, 12, 23):  # captura inicial, tras NOTE_EXIT y tras reto.
                for bad in range(1, 12):
                    with self.subTest(role=role, phase=phase, failed_read=bad, source=ORDER[bad - 1]):
                        n, p, s = self.fresh(); n.bad = phase + bad
                        with patch.object(p, '_challenge'), self.assertRaises(d.PeerError): p.authenticate(s, 'unix')
                for gap in range(1, 11):
                    with self.subTest(role=role, phase=phase, gap=(ORDER[gap - 1], ORDER[gap])):
                        n, p, s = self.fresh()
                        def change(index, name):
                            if index == phase + gap:
                                n.generation += 1
                                n.watch_object.exited = True
                        n.after = change
                        with patch.object(p, '_challenge'), self.assertRaises(d.PeerError):
                            p.authenticate(s, 'unix')
                        self.assertFalse(p.alive(s, p.capture(s, 'unix')))

    def test_uuid_and_one_sided_reads_change(self):
        for index in range(1, 12):
            n, p, s = self.fresh()
            source = ORDER[index - 1]
            original = {'U': n.boot, 'cred': n.peercred, 'pid': n.peerpid,
                        'token': n.token, 'unique': n.unique, 'BSD': n.bsd}[source]
            def altered(*args):
                value = original(*args)
                if len(n.calls) == index:
                    if source == 'U': return 'aaaaaaaa-2222-3333-4444-555555555555'
                    if source == 'cred': return (502, 20)
                    if source == 'pid': return 124
                    if source == 'token': return (123, 501, 20, 8)
                    if source == 'unique': return (b'y' * 16, 42, 8)
                    value.uid = 502
                return value
            method = {'U': 'boot', 'cred': 'peercred', 'pid': 'peerpid', 'token': 'token',
                      'unique': 'unique', 'BSD': 'bsd'}[source]
            with self.subTest(read=index), patch.object(n, method, side_effect=altered):
                with self.assertRaises(d.PeerError): p.capture(s, 'unix')

    def test_register_challenge_and_dispatch_windows(self):
        for role in ROLES:
            for gap in ('before-register', 'register', 'after-register', 'before-challenge',
                        'during-challenge', 'after-challenge', 'dispatch'):
                with self.subTest(role=role, gap=gap):
                    n, p, s = self.fresh()
                    def change(index, name):
                        if ((gap == 'before-register' and index == 11) or
                            (gap == 'register' and name == 'register') or
                            (gap == 'after-register' and index == 12) or
                            (gap == 'before-challenge' and index == 23)):
                            n.generation += 1; n.watch_object.exited = True
                    n.after = change
                    def challenge(*args):
                        if gap in ('during-challenge', 'after-challenge'):
                            n.generation += 1; n.watch_object.exited = True
                    with patch.object(p, '_challenge', side_effect=challenge):
                        if gap == 'dispatch':
                            peer = p.authenticate(s, 'unix')
                            n.generation += 1; n.watch_object.exited = True
                            self.assertFalse(p.alive(s, peer))
                        else:
                            with self.assertRaises(d.PeerError): p.authenticate(s, 'unix')
            n, p, s = self.fresh()
            with patch.object(n, 'watch', side_effect=ProcessLookupError('ESRCH')):
                with self.assertRaises(d.PeerError): p.authenticate(s, 'unix')

    def test_same_pid_new_birth_never_verifies_or_adopts_fd(self):
        for field, value in [('start_token', (100, 2, 42, 7)), ('start_token', (101, 2, 41, 7)),
                             ('start_token', (100, 2, 41, 8))]:
            n, p, s = self.fresh(); peer = p.capture(s, 'unix')
            self.assertEqual(p.verify(replace(peer.identity, **{field: value})).status,
                             'confirmed_absent_or_different')
        with patch.object(p, '_challenge'): p.authenticate(s, 'unix')
        s.close()
        a, b = socket.socketpair(); self.addCleanup(a.close); self.addCleanup(b.close)
        self.assertFalse(p.alive(a, peer)); self.assertFalse(p.alive(s, peer))
        self.assertTrue(n.watch_object.closed)

    def test_eof_while_process_is_live_closes_only_known_watch(self):
        n, p, s = self.fresh()
        with patch.object(p, '_challenge'): peer = p.authenticate(s, 'unix')
        self.assertEqual(p.verify(peer.identity).status, 'same')
        n.remote.close()
        self.assertFalse(n.watch_object.exited)
        self.assertFalse(p.alive(s, peer)); self.assertTrue(n.watch_object.closed)

    def test_total_deadline_and_cancellation_close_uncommitted_watch(self):
        n, p, s = self.fresh(); s.settimeout(2)
        with patch.object(p, '_challenge'), patch.object(d.time, 'monotonic', side_effect=(100, 106)):
            with self.assertRaises(d.PeerError): p.authenticate(s, 'unix')
        self.assertTrue(n.watch_object.closed); self.assertEqual(s.gettimeout(), 2)
        n, p, s = self.fresh()
        with patch.object(p, '_challenge', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            p.authenticate(s, 'unix')
        self.assertTrue(n.watch_object.closed); self.assertFalse(p._streams)

    def test_challenge_rejects_buffered_proof_and_bounded_frames(self):
        import struct
        def frame(raw): return struct.pack('!I', len(raw)) + raw
        for raw in (struct.pack('!I', 0), struct.pack('!I', 65537), b'\0\0', frame(b'{}'),
                    frame(b'{"x":NaN}'), frame(b'\xef\xbb\xbf{}'), frame(b'{"x":1,"x":2}'),
                    frame(b'{"kind":"proof","nonce":"stale"}'),
                    frame(b'{"kind":"challenge","nonce":"' + b'0' * 64 + b'"}') +
                    frame(b'{"kind":"proof","nonce":"' + b'0' * 64 + b'"}')):
            with self.subTest(raw=raw):
                n, p, s = self.fresh()
                n.remote.sendall(raw); n.remote.shutdown(socket.SHUT_WR)
                with self.assertRaises(d.PeerError): p.authenticate(s, 'unix')
                self.assertFalse(p._streams); self.assertTrue(n.watch_object.closed)



@unittest.skipUnless(sys.platform == 'darwin', 'ABI exclusivo de Darwin')
class NativeProcesses(unittest.TestCase):
    def test_own_connections_symmetric_challenge_note_exit_and_eof(self):
        with tempfile.TemporaryDirectory() as root:
            endpoint = str(Path(root) / 'p.sock')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener, d.DarwinPeers() as peers:
                listener.bind(endpoint); listener.listen(1); listener.settimeout(5)
                code = '''import os,socket,sys
from hostgate.darwin_peer import DarwinPeers
with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s, DarwinPeers() as p:
 s.connect(sys.argv[1])
 original=p._frame
 def maximum(stream,deadline,message=None):
  if message is not None and message['kind']=='challenge':
   import json,struct
   raw=json.dumps(message).encode().ljust(65536,b' ')
   stream.sendall(struct.pack('!I',len(raw))+raw)
  else: return original(stream,deadline,message)
 p._frame=maximum
 peer=p.authenticate(s,'unix')
 assert peer.identity.pid == int(sys.argv[2])
 assert p.verify(peer.identity).status == 'same'
 s.sendall(b'K'); assert s.recv(1)==b'Q'
'''
                child = subprocess.Popen([sys.executable, '-B', '-c', code, endpoint, str(os.getpid())])
                try:
                    with listener.accept()[0] as stream:
                        peer = peers.authenticate(stream, 'unix')
                        self.assertEqual(peer.identity.pid, child.pid)
                        self.assertEqual(len(peer.identity.start_token), 4)
                        self.assertEqual(peers.verify(peer.identity).status, 'same')
                        self.assertEqual(stream.recv(1), b'K'); self.assertTrue(peers.alive(stream, peer))
                        watch = peers._streams[stream][1]
                        stream.sendall(b'Q'); self.assertEqual(child.wait(timeout=5), 0)
                        with self.assertRaises(d.PeerError): watch.check()
                        self.assertEqual(stream.recv(1), b'')
                        self.assertFalse(peers.alive(stream, peer))
                        self.assertNotEqual(peers.verify(peer.identity).status, 'same')
                finally:
                    if child.poll() is None: child.kill()
                    child.wait(timeout=5)

    def test_inherited_writer_cannot_replace_authenticated_parent(self):
        with tempfile.TemporaryDirectory() as root:
            endpoint = str(Path(root) / 'p.sock')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener, d.DarwinPeers() as peers:
                listener.bind(endpoint); listener.listen(1); listener.settimeout(5)
                code = '''import socket,subprocess,sys
from hostgate.darwin_peer import DarwinPeers
with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s, DarwinPeers() as p:
 s.connect(sys.argv[1]); p.authenticate(s,'unix'); s.sendall(b'K'); assert s.recv(1)==b'Q'
 inner="import socket,sys; s=socket.socket(fileno=int(sys.argv[1])); s.sendall(b'X'); assert s.recv(1)==b'D'"
 child=subprocess.Popen([sys.executable,'-B','-c',inner,str(s.fileno())],pass_fds=(s.fileno(),))
 try: assert child.wait(timeout=5)==0
 finally:
  if child.poll() is None: child.kill()
  child.wait(timeout=5)
'''
                child = subprocess.Popen([sys.executable, '-B', '-c', code, endpoint])
                try:
                    with listener.accept()[0] as stream:
                        stream.settimeout(5); peer = peers.authenticate(stream, 'unix')
                        self.assertEqual(stream.recv(1), b'K'); self.assertEqual(peer.identity.pid, child.pid)
                        stream.sendall(b'Q'); self.assertEqual(stream.recv(1), b'X')
                        self.assertIsNone(child.poll())  # S original todavía vive.
                        self.assertNotEqual(peers.capture(stream, 'unix'), peer)
                        self.assertFalse(peers.alive(stream, peer))
                        stream.sendall(b'D'); self.assertEqual(child.wait(timeout=5), 0)
                finally:
                    if child.poll() is None: child.kill()
                    child.wait(timeout=5)

    def test_exact_native_sizes_return_codes_and_timeval(self):
        n = d._Native()
        self.assertEqual([C.sizeof(t) for t in (d._Audit, d._Unique, d.DarwinBSDInfo)], [32, 56, 136])
        self.assertGreater(n.unique(os.getpid())[1], 0)
        value = SimpleNamespace(uuid=b'x' * 16, unique=42, version=-1)
        with patch.object(n, '_proc', return_value=value): self.assertEqual(n.unique(123)[2], 0xffffffff)
        with d.DarwinPeers() as p: self.assertEqual(p.health_boot(), (p.boot(), n.boottime()))
        with patch.object(n.libc, 'sysctlbyname', return_value=-1):
            for fn in (n.boot, n.boottime):
                with self.assertRaises(d.PeerError): fn()
        for length in (0, 1, 15):
            with patch.object(n, '_sysctl', return_value=length):
                for fn in (n.boot, n.boottime):
                    with self.assertRaises(d.PeerError): fn()
        for sec, usec in ((0, 0), (1, -1), (1, 1000000)):
            def clock(name, value): value.sec, value.usec = sec, usec; return C.sizeof(value)
            with patch.object(n, '_sysctl', side_effect=clock), self.assertRaises(d.PeerError): n.boottime()
        for size in (0, 1, 55, 135):
            with patch.object(n, 'proc', return_value=size):
                for fn in (n.unique, n.bsd):
                    with self.assertRaises(d.PeerError): fn(os.getpid())
        stream = SimpleNamespace(getsockopt=lambda *args: b'x', fileno=lambda: 0)
        for fn in (n.peerpid, n.token):
            with self.assertRaises(d.PeerError): fn(stream)
        with patch.object(n.libc, 'getpeereid', return_value=-1), self.assertRaises(d.PeerError): n.peercred(stream)



class Health(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'sample.json'
        self.n = Native(); self.p = d.DarwinPeers(_native=self.n); self.addCleanup(self.p.close)
        producer = Path(__file__).resolve().parents[3] / 'scripts/hosts/mini-vigilante/muestra.py'
        spec = importlib.util.spec_from_file_location('producer_pr2b', producer)
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        raw = {'LocalHostName': 'mini-prueba', 'kern.boottime': '{ sec = 1792738812, usec = 550262 }',
               'kern.memorystatus_level': '77', 'kern.memorystatus_vm_pressure_level': '1',
               'vm.swapusage': 'total = 1024.00M used = 0.00M free = 1024.00M',
               'vm.loadavg': '{ 2.79 3.1 4.0 }', 'hw.ncpu': '10'}
        self.sample = m.capturar(m.leer_umbrales(producer.with_name('umbrales.json')),
            comando=lambda argv: raw[argv[-1]],
            reloj=lambda: datetime(2026, 10, 24, 0, 40, tzinfo=timezone.utc),
            statvfs=lambda path: SimpleNamespace(f_blocks=500 * 1024**3, f_bavail=100 * 1024**3, f_frsize=1))
        self.path.write_text(json.dumps(self.sample))
        self.health = d.DarwinHealth(self.p, 'mini-prueba', self.path)
        self.world = World(); self.world.health = self.health.read
        self.cfg = replace(config(Path(self.tmp.name) / 'state'), snapshot_host='mini-prueba')
        self.gate = Hostgate(self.cfg, self.world)
        self.n.calls.clear()

    def test_real_reader_and_core_accept_exact_integer_witness(self):
        kind, sample, witness = self.world.health()
        self.assertEqual(self.n.calls, ['U', 'T', 'T', 'U'])
        self.assertEqual(witness, self.sample['boot']['bootId'])
        self.assertEqual(evaluar(sample, self.world.wall(), witness), [])
        self.assertEqual(self.gate._health(), [])
        self.assertEqual(self.p.boot(), U); self.assertNotEqual(witness, U)

    def test_fresh_snapshot_other_boot_is_not_independent_witness(self):
        self.sample['boot'] = dict(bootId='mini-prueba/2026-10-22T00:00:00.000000Z',
                                   lastBootUtc='2026-10-22T00:00:00.000000Z', error=None)
        self.path.write_text(json.dumps(self.sample))
        self.assertIn('boot', evaluar(self.sample, self.world.wall(), self.health.read()[2]))
        self.assertTrue(self.gate._health())

    def test_bad_boot_never_grants_or_releases_retained_capacity(self):
        from hostgate.identity import Identity
        def boot():
            try: return self.p.boot()
            except d.PeerError: return None
        self.world.physical_boot = boot
        self.world.executor_boot = lambda executor: boot()
        self.world.verify = self.p.verify
        identity = Identity('unix', 501, 123, (100, 2, 41, 7), U, U)
        auth = AuthContext('fixture', 'supervisor', identity, identity, 'fixture-channel')
        ShadowStore.initialize(self.cfg)
        first = self.gate.reservar(request(exclusiveResources=['sen-slot:1']), auth)
        self.assertTrue(first['wouldGrant'])
        before = self.gate.store.read()['reservations'][0]
        self.sample['boot']['bootId'] = 'mini-prueba/2026-10-22T00:00:00.000000Z'
        self.sample['boot']['lastBootUtc'] = '2026-10-22T00:00:00.000000Z'
        self.path.write_text(json.dumps(self.sample))
        self.assertFalse(self.gate.reservar(request('next', 'next'), auth)['wouldGrant'])
        self.n.uuid = 'aaaaaaaa-2222-3333-4444-555555555555'
        self.assertFalse(self.gate.reservar(request('boot-change', 'boot-change'), auth)['wouldGrant'])
        after = self.gate.store.read()['reservations'][0]
        self.assertEqual(after['demand'], before['demand'])
        self.assertEqual(after['exclusiveResources'], before['exclusiveResources'])
        projected = self.gate._observe(self.gate.store.read())['reservations'][0]
        self.assertEqual(projected['effectiveState'], 'uncertain')

    def test_initial_uuid_bracket_and_time_mismatch_with_parse_failure(self):
        for fault in range(1, 4):
            n = Native(); p = d.DarwinPeers(_native=n); self.addCleanup(p.close)
            n.calls.clear(); n.bad = fault
            with self.assertRaises(OSError): d.DarwinHealth(p, 'mini-prueba', self.path)
        for gap in (1, 2):
            n = Native(); p = d.DarwinPeers(_native=n); self.addCleanup(p.close); n.calls.clear()
            def change(index, name):
                if index == gap: n.uuid = 'aaaaaaaa-2222-3333-4444-555555555555'
            n.after = change
            with self.assertRaises(d.PeerError): d.DarwinHealth(p, 'mini-prueba', self.path)
        self.n.time = (T[0] + 1, T[1]); self.path.write_text('{bad')
        self.assertIsNone(self.health.read()[2])
        self.n.time = T; self.path.write_text(json.dumps(self.sample))
        self.assertIsNone(self.health.read()[2])

    def test_each_health_read_fault_and_boot_change_latches_closed(self):
        original = Path.read_bytes
        for fault in ('error', 'uuid', 'time'):
            for index in range(1, 6 if fault == 'error' else 4 if fault == 'time' else 5):
                with self.subTest(read_or_gap=index, fault=fault):
                    n = Native(); p = d.DarwinPeers(_native=n); self.addCleanup(p.close)
                    h = d.DarwinHealth(p, 'mini-prueba', self.path); n.calls.clear()
                    def read_snapshot(path): return n.read('snapshot', original(path))
                    def after(count, name):
                        if count == index:
                            if fault == 'uuid': n.uuid = 'aaaaaaaa-2222-3333-4444-555555555555'
                            if fault == 'time': n.time = (T[0] + 1, T[1])
                    n.after = after
                    if fault == 'error': n.bad = index
                    with patch.object(Path, 'read_bytes', read_snapshot):
                        self.assertIsNone(h.read()[2])
                    if fault != 'error':
                        n.after = None; n.uuid, n.time = U, T
                        self.assertIsNone(h.read()[2])  # No aprende otro boot ni se reabre.
        for raw in ('{bad', '{"x":NaN}', '{"x":1,"x":2}'):
            self.path.write_text(raw)
            self.assertTrue(self.gate._health())


if __name__ == '__main__': unittest.main()
