"""P07/P08/P09/P13: controles nativos y fallos de una sola observación."""
import ctypes as C
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
from hostgate import darwin_peer as d
from hostgate.admission import Hostgate
from hostgate.salud.lector_mini import evaluar
from hostgate.tests.support.world import World, config, FIXTURES

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
        self.watch_object = Watch()
    def read(self, name, value):
        self.calls.append(name)
        if self.bad == len(self.calls): raise PermissionError('EPERM ' + name)
        if self.after: self.after(len(self.calls), name)
        return value
    def boot(self): return self.read('U', U)
    def boottime(self): return self.read('T', T)
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
        native = Native()
        peers = d.DarwinPeers(_native=native)
        self.addCleanup(peers.close)
        native.calls.clear()
        a, b = socket.socketpair()
        self.addCleanup(a.close); self.addCleanup(b.close)
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
            for bad in range(1, 12):
                with self.subTest(role=role, failed_read=bad, source=ORDER[bad - 1]):
                    n, p, s = self.fresh(); n.bad = bad
                    with self.assertRaises(d.PeerError): p.capture(s, 'unix')
            for gap in range(1, 11):
                with self.subTest(role=role, gap=(ORDER[gap - 1], ORDER[gap])):
                    n, p, s = self.fresh()
                    def change(index, name):
                        if index == gap:
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

    def test_challenge_rejects_buffered_proof_and_bounded_frames(self):
        import struct
        for raw in (struct.pack('!I', 0), struct.pack('!I', 65537), b'\0\0',
                    struct.pack('!I', 2) + b'{}',
                    struct.pack('!I', 8) + b'{"x":NaN}',
                    struct.pack('!I', 32) + b'{"kind":"proof","nonce":"stale"}'):
            n, p, s = self.fresh()
            # socketpair peer queda registrado por fresh; duplicado sólo para escribir fixture.
            other = socket.socket(fileno=os.dup(s.fileno()))
            other.close()
            with patch.object(p, '_recv', return_value=raw):
                with self.assertRaises(d.PeerError): p._challenge(s, 0)


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
 s.connect(sys.argv[1]); peer=p.authenticate(s,'unix')
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
                        stream.sendall(b'Q'); self.assertEqual(child.wait(timeout=5), 0)
                        self.assertFalse(peers.alive(stream, peer))
                        self.assertNotEqual(peers.verify(peer.identity).status, 'same')
                finally:
                    if child.poll() is None: child.kill()
                    child.wait(timeout=5)

    def test_inherited_connection_observes_parent_not_exec_child(self):
        with tempfile.TemporaryDirectory() as root:
            endpoint = str(Path(root) / 'p.sock')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener, d.DarwinPeers() as peers:
                listener.bind(endpoint); listener.listen(1)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as inherited:
                    inherited.connect(endpoint)
                    code = 'import socket,sys; s=socket.socket(fileno=int(sys.argv[1])); s.sendall(b"X")'
                    child = subprocess.Popen([sys.executable, '-B', '-c', code, str(inherited.fileno())],
                                             pass_fds=(inherited.fileno(),))
                    try:
                        with listener.accept()[0] as stream:
                            stream.settimeout(5); self.assertEqual(stream.recv(1), b'X')
                            peer = peers.capture(stream, 'unix')
                            self.assertEqual(peer.identity.pid, os.getpid())
                            self.assertNotEqual(peer.identity.pid, child.pid)
                        self.assertEqual(child.wait(timeout=5), 0)
                    finally:
                        if child.poll() is None: child.kill()
                        child.wait(timeout=5)


class Health(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'sample.json'
        self.n = Native(); self.p = d.DarwinPeers(_native=self.n); self.addCleanup(self.p.close)
        self.sample = json.loads((FIXTURES / 'health/mini.json').read_text())
        self.sample['host'] = 'mini-prueba'
        self.sample['boot'] = dict(bootId='mini-prueba/2026-10-23T07:00:12.550262Z',
                                   lastBootUtc='2026-10-23T07:00:12.550262Z', error=None)
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

    def test_each_health_read_fault_and_boot_change_latches_closed(self):
        for index in range(1, 5):
            for fault in ('error', 'uuid', 'time'):
                with self.subTest(read=index, fault=fault):
                    self.n.calls.clear()
                    if fault == 'error': self.n.bad = index
                    method = 'boot' if fault == 'uuid' else 'boottime'
                    original = getattr(self.n, method)
                    def altered():
                        value = original()
                        if len(self.n.calls) >= index:
                            return 'aaaaaaaa-2222-3333-4444-555555555555' if fault == 'uuid' else (T[0] + 1, T[1])
                        return value
                    with patch.object(self.n, method, side_effect=altered) if fault != 'error' else patch.object(self.n, 'bad', index):
                        self.assertIsNone(self.health.read()[2])
                    self.n.bad = None
        self.path.write_text('{bad')
        self.assertTrue(self.gate._health())


if __name__ == '__main__': unittest.main()
