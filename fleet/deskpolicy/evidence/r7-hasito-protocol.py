"""Exercise the lane-closed client contract using Hasito's real estado.py.

The client below is a protocol fixture for U1; the production adapter belongs to U3.
Usage: python3 -I .../r7-hasito-protocol.py .audit/r7-hasito-estado.py
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path.cwd() / 'fleet'))
from deskpolicy.store import read_json
from deskpolicy.tests.test_r7 import RoundSevenTest

ESTADO = Path(sys.argv.pop(1)).resolve()
assert hashlib.sha256(ESTADO.read_bytes()).hexdigest() == '2efcf5fc350965ec5bb0fee438e941d912d07b15679a2f7dca911090a17db006'
ENV = {k: v for k, v in os.environ.items() if k not in ('MINI_LOCK', 'MINI_COLA')}
ENV['MINI_LOCK_LEGADO'] = ''


def estado(lock, *args):
    result = subprocess.run([sys.executable, '-I', str(ESTADO), '--lock', str(lock), *args],
                            env=ENV, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, (args, result.stdout, result.stderr)
    return result.stdout.strip()


def enqueue(lock, owner):
    return estado(lock, 'encolar', '--owner', owner, '--run-id', 'local-' + owner)


def take(lock, owner, turn=None):
    turn = turn or enqueue(lock, owner)
    estado(lock, 'tomar', '--fifo', '--turno', turn, '--owner', owner,
           '--run-id', 'local-' + owner, '--phase', 'ADMISSION_WAIT', '--espera', '0')
    return turn


class ClientProtocolTest(RoundSevenTest):
    # Only the methods selected explicitly below are run.
    def client_case(self, explicit, writer_dies):
        locks = self.h.directory / 'local-locks'
        locks.mkdir(mode=0o700)
        high = locks / 'desk-3.lock'
        turn3 = take(high, 'H3')
        successor_turn = enqueue(high, 'successor')
        # Run the original dawn/fallback scenario with the lock still owned.
        receipt = self.dawn(writer_dies)
        self.assertEqual(receipt['reason'], 'lane-closed')
        self.engine.register('H3', 'hasito', 'script', 'test', 1, withdraw=True, lane=3)
        estado(high, 'soltar', '--owner', 'H3')
        estado(high, 'retirar', '--owner', 'H3', '--turno', turn3)
        registry = json.loads(high.with_name(high.name + '.turnos.json').read_text())
        self.assertFalse(any(t['owner'] == 'H3' for t in registry['turnos']))
        self.assertEqual(read_json(self.engine.record_path('H3'))['state'], 'withdrawn')
        contract = (Path.cwd() / 'fleet/contracts/desk-politica-v1.md').read_text()
        self.assertIn('carril N cerrado en el modo vigente', contract)
        def continue_client():
            if explicit:
                raise RuntimeError('carril 3 cerrado en el modo vigente')
            # Respect an older local turn on lane 1; lane 2 is the least free.
            lock1 = locks / 'desk.lock'
            older = enqueue(lock1, 'older')
            chosen = None
            for lane in range(1, self.engine.status()['max_lane'] + 1):
                lock = locks / ('desk.lock' if lane == 1 else 'desk-' + str(lane) + '.lock')
                registry = lock.with_name(lock.name + '.turnos.json')
                if not registry.exists() or not json.loads(registry.read_text())['turnos']:
                    chosen = (lane, lock)
                    break
            self.assertEqual(chosen[0], 2)
            lane, lock = chosen
            take(lock, 'H3-new')
            self.lane_pending('H3-new', lane)
            admitted, worker, pipe = self.launch('H3-new', lane)
            self.assertTrue(admitted['execute'], admitted)
            self.assertEqual(admitted['mode'], 'day')
            self.close_actor(worker, pipe)
            estado(lock, 'soltar', '--owner', 'H3-new')
            estado(lock1, 'retirar', '--turno', older, '--owner', 'older')
        error = None
        if explicit:
            with self.assertRaisesRegex(RuntimeError, '^carril 3 cerrado en el modo vigente$') as caught:
                continue_client()
            error = str(caught.exception)
            self.assertFalse(self.engine.record_path('H3-new').exists())
            self.assertFalse((locks / 'desk.lock').exists())
            self.assertFalse((locks / 'desk-2.lock').exists())
        else:
            continue_client()
        # The next local owner of the high lane follows the same rule.
        take(high, 'successor', successor_turn)
        self.lane_pending('successor', 3)
        denied, _, _ = self.launch('successor', 3)
        self.assertEqual(denied['reason'], 'lane-closed')
        self.engine.register('successor', 'hasito', 'script', 'test', 1, withdraw=True, lane=3)
        estado(high, 'soltar', '--owner', 'successor')
        estado(high, 'retirar', '--owner', 'successor', '--turno', successor_turn)
        self.assertEqual(json.loads(high.with_name(high.name + '.turnos.json').read_text())['turnos'], [])
        print('R7 CLIENT', 'explicit' if explicit else 'auto', 'writer-dead' if writer_dies else 'dawn',
              'error', error, 'high FIFO empty; successor released', flush=True)

    def test_auto_dawn(self):
        self.client_case(False, False)

    def test_auto_writer_dead(self):
        self.client_case(False, True)

    def test_explicit_lock_dawn(self):
        self.client_case(True, False)

    def test_explicit_mini_lock_writer_dead(self):
        self.client_case(True, True)


names = ['test_auto_dawn', 'test_auto_writer_dead', 'test_explicit_lock_dawn', 'test_explicit_mini_lock_writer_dead']
result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(ClientProtocolTest(name) for name in names))
raise SystemExit(not result.wasSuccessful())
