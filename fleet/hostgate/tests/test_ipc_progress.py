"""Regresión CX-2, derivada de review-r3-controls: R/S/H nativos en Mini."""
import json
import os
from pathlib import Path
import select
import socket
import struct
import subprocess
import sys
import time
from unittest.mock import patch

from hostgate.protocol import canonical
from hostgate.tests.test_admin import Fixture, fixture_policy
from hostgate.tests.test_ipc import IPC, FixtureSocket, query
from hostgate.tests.support.world import World, request


def server(root):
    from hostgate.service import run
    from hostgate.admission import Hostgate
    original_estado = Hostgate.estado
    def observed_estado(gate, *args, **kwargs):
        result = original_estado(gate, *args, **kwargs)
        (root / '.queried').touch()
        return result
    os.chdir(root)
    class ClockWorld(World):
        def monotonic(self): return time.monotonic()
        def profile_verified(self, profile): return profile == 'fixture'
    with fixture_policy(root), patch.object(socket, 'socket', FixtureSocket), patch.object(Hostgate, 'estado', observed_estado):
        return run(_locator=Fixture(root).locator, _synthetic=ClockWorld(),
                   _ready=lambda: print('listening', flush=True))


class Progress(IPC):
    server_module = 'hostgate.tests.test_ipc_progress'
    def spawn(self, role, env=None):
        if role != 'server': return super().spawn(role, env)
        proc = subprocess.Popen([sys.executable, '-B', '-m', self.server_module, 'server', str(self.root)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        def cleanup():
            if proc.poll() is None: proc.terminate()
            try: proc.communicate(timeout=8)
            except subprocess.TimeoutExpired: proc.kill(); proc.communicate(timeout=8)
        self.addCleanup(cleanup)
        return proc

    def snapshot(self):
        with self.client() as client: return client.call(query())

    def ledger(self):
        from hostgate import admin
        from hostgate.store import ShadowStore
        return ShadowStore(admin.load_config(self.f.locator).node).read()

    def history(self):
        from hostgate import admin
        from hostgate.admission import Hostgate
        from hostgate.scope import ClosureEvidence
        self.server.terminate(); self.server.communicate(timeout=8)
        world = World(); gate = Hostgate(admin.load_config(self.f.locator).node, world)
        auth = world.auth(principal='uid:' + str(os.getuid()))
        for n in range(80):
            grant = gate.reservar(request('history-' + str(n), physicalHostId='fixture-host'), auth)
            self.assertTrue(grant['wouldGrant'], grant)
            world.closures[('proof',)] = ClosureEvidence.fixture(grant['simulationKey'], world.boot)
            self.assertEqual(gate.soltar('release-' + str(n), grant['simulationKey'], ['proof'], auth)['exitCode'], 0)
        self.server = self.spawn('server'); self.assertEqual(self.line(self.server).strip(), 'listening')

    def stalled(self, mode):
        from hostgate.ipc import message
        from hostgate.wire import send_json
        if mode == 'reader': self.history()
        _, supervisor, call = self.pair()
        grant = call(request(physicalHostId='fixture-host', exclusiveResources=['sen-slot:1']))
        self.assertTrue(grant['wouldGrant'], grant)
        original = self.ledger()['reservations'][-1]
        seq, latencies, states = 0, [], []
        def beat():
            nonlocal seq
            seq += 1
            value = dict(query('beat-' + str(seq)), operation='latido', key=grant['simulationKey'], seq=seq)
            start = time.monotonic()
            supervisor.stdin.write(canonical(value) + '\n'); supervisor.stdin.flush()
            # 2 s es el requisito, no un timeout productivo modificado.
            ready = select.select([supervisor.stdout], [], [], 2)[0]
            if not ready:
                latencies.append(time.monotonic() - start)
                return False
            response = json.loads(supervisor.stdout.readline())
            latencies.append(time.monotonic() - start); states.append(response['simulationState'])
            self.assertEqual(response['exitCode'], 0)
            self.assertEqual(response['simulationKey'], grant['simulationKey'])
            return True
        self.assertTrue(beat(), 'control sin Q'); self.assertEqual(states[-1], 'reserved')
        if mode == 'handshake':
            slow = socket.socket(socket.AF_UNIX); self.addCleanup(slow.close)
            slow.connect(str(self.f.locator.socket_path))
            send_json(slow, message('open', executorId='unix'))
            self.assertTrue(select.select([slow], [], [], 2)[0], 'H inició el reto')
        else:
            slow_client = self.client(); slow = slow_client.stream
            if mode == 'input': slow.sendall(struct.pack('!I', 64) + b'{')
            else:
                slow.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
                size = len(canonical(self.snapshot()).encode())
                buffers = slow.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF) + slow.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)
                self.assertGreater(size, buffers * 2, 'respuesta real mayor que los buffers Unix')
                (self.root / '.queried').unlink()
                send_json(slow, query('stopped-reader'))
                deadline = time.monotonic() + 2
                while not (self.root / '.queried').exists() and time.monotonic() < deadline: time.sleep(0.01)
                self.assertTrue((self.root / '.queried').exists(), 'Q llegó al core antes del bloqueo de envío')
                print('CX2 reader responseBytes={} socketBuffers={}'.format(size, buffers), flush=True)
        start = time.monotonic(); next_beat = start; pending = False
        while time.monotonic() - start < 15:
            if not pending: pending = not beat()
            # Mantener Q detenido >=15 s también en el rojo, sin acumular solicitudes.
            next_beat += 1
            time.sleep(max(0, min(next_beat, start + 15) - time.monotonic()))
        held = time.monotonic() - start
        slow.close()
        if pending:
            delayed = json.loads(self.line(supervisor))
            states.append(delayed['simulationState'])
        self.assertTrue(beat(), 'control después de cerrar Q')
        snapshot = self.snapshot(); held_rows = [r for r in snapshot['reservations'] if r['effectiveState'] != 'released']
        self.assertEqual(len(held_rows), 1)
        row = held_rows[0]
        durable = self.ledger()['reservations'][-1]
        for field in ('key', 'owner', 'channelBinding', 'demand', 'exclusiveResources'):
            self.assertEqual(durable[field], original[field])
        self.assertEqual(durable['seq'], seq)
        self.assertEqual(snapshot['budget']['retained']['slots'], 1)
        self.assertEqual(snapshot['budget']['available']['slots'], 0)
        self.assertEqual(row['exclusiveResources'], ['sen-slot:1'])
        print('CX2 mode={} ttl=10 held={:.3f}s maxHeartbeat={:.3f}s state={} seq={} debit=1 exclusive=retained owner/key/binding=same'.format(
            mode, held, max(latencies), row['effectiveState'], seq), flush=True)
        self.assertLess(max(latencies), 2, 'Q no debe bloquear el latido sano')
        self.assertEqual(set(states), {'reserved'})
        self.assertEqual(row['effectiveState'], 'reserved')

    def test_partial_input_progress(self): self.stalled('input')
    def test_stopped_reader_progress(self): self.stalled('reader')
    def test_incomplete_handshake_progress(self): self.stalled('handshake')

    def test_real_supervisor_loss_and_expiry_retain_authority(self):
        _, supervisor, call = self.pair()
        grant = call(request(physicalHostId='fixture-host', exclusiveResources=['sen-slot:1']))
        self.assertTrue(grant['wouldGrant'], grant)
        self.assertEqual(call(dict(query('control'), operation='latido', key=grant['simulationKey'], seq=1))['simulationState'], 'reserved')
        time.sleep(10.1)
        expired = self.snapshot()
        self.assertEqual(expired['reservations'][0]['effectiveState'], 'uncertain')
        self.assertEqual(expired['budget']['retained']['slots'], 1)
        self.assertEqual(call(dict(query('late'), operation='latido', key=grant['simulationKey'], seq=2))['simulationState'], 'uncertain')
        supervisor.terminate(); supervisor.communicate(timeout=8)
        other = self.client()
        self.assertEqual(other.call(dict(query('adopt'), operation='latido', key=grant['simulationKey'], seq=3))['exitCode'], 5)
        _, _, second = self.pair('second')
        denied = second(request('second', physicalHostId='fixture-host', exclusiveResources=['sen-slot:1']))
        self.assertFalse(denied['wouldGrant'])
        self.assertIn('budget-exceeded', {r['code'] for r in denied['reasons']})
        self.assertEqual(self.snapshot()['budget']['retained']['slots'], 1)


if __name__ == '__main__':
    raise SystemExit(server(Path(sys.argv[2])))
