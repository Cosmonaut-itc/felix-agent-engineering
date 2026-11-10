"""Re-run the three Opus FIFO cases with the local-first protocol and real estado.py.

Usage: python3 -I .../r6-hasito-protocol.py /path/to/Hasito/scripts/mini/estado.py
The snapshot used in r6 has SHA256 2efcf5fc350965ec5bb0fee438e941d912d07b15679a2f7dca911090a17db006.
"""
import hashlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path.cwd() / 'fleet'))
from deskpolicy.store import process, read_json
from deskpolicy.tests.test_policy import PolicyTest

ESTADO = Path(sys.argv[1]).resolve()
assert hashlib.sha256(ESTADO.read_bytes()).hexdigest() == '2efcf5fc350965ec5bb0fee438e941d912d07b15679a2f7dca911090a17db006'
ENV = {k: v for k, v in os.environ.items() if k not in ('MINI_LOCK', 'MINI_COLA')}
ENV['MINI_LOCK_LEGADO'] = ''


def command(lock, *args):
    return [sys.executable, '-I', str(ESTADO), '--lock', str(lock), *args]


def estado(lock, *args):
    result = subprocess.run(command(lock, *args), env=ENV, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, (args, result.stdout, result.stderr)
    return result.stdout.strip()


def take(lock, owner):
    turn = estado(lock, 'encolar', '--owner', owner, '--run-id', 'local-' + owner)
    estado(lock, 'tomar', '--fifo', '--turno', turn, '--owner', owner,
           '--run-id', 'local-' + owner, '--phase', 'ADMISSION_WAIT', '--espera', '0')
    return turn


def pending(case, name, lane):
    kwargs = {'lane': lane} if 'lane' in inspect.signature(case.engine.register).parameters else {}
    case.engine.register(name, 'hasito', 'script', 'test', 1, **kwargs)


def owner_retries(variant):
    case = PolicyTest()
    case.setUp()
    waiter = None
    try:
        case.h.now = case.h.now.replace(hour=16)
        directory = case.h.directory / 'local-locks'
        directory.mkdir(mode=0o700)
        lock = directory / 'desk.lock'
        take(lock, 'B')
        pending(case, 'B1', 1)
        if variant == 'sync-timeout':
            reader, writer = os.pipe()
            try:
                args = ['ejecutar', '--request-id', 'B1', '--run-id', 'run-B1', '--lane', '1',
                        '--sync-ready-fd', str(reader), '--sync-timeout-s', '.1', '--', '/bin/true']
                assert case.run_cli(args)['rc'] == 75
            finally:
                os.close(reader)
                os.close(writer)
        else:
            case.h.advance(121)
            case.publish()
        turn = estado(lock, 'encolar', '--owner', 'A', '--run-id', 'local-A')
        waiter = subprocess.Popen(command(lock, 'tomar', '--fifo', '--turno', turn,
            '--owner', 'A', '--run-id', 'local-A', '--phase', 'ADMISSION_WAIT', '--espera', '30'),
            env=ENV, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert not case.engine.record_path('A').exists()
        pending(case, 'B2', 1)
        receipt, worker, pipe = case.launch('B2')
        assert receipt['execute'], receipt
        case.close_actor(worker, pipe)
        assert waiter.poll() is None, 'successor acquired the lock while B still owns it'
        registry = json.loads(lock.with_name(lock.name + '.turnos.json').read_text())
        assert [(t['owner'], t['estado']) for t in registry['turnos']] == [('B', 'activo'), ('A', 'pendiente')]
        estado(lock, 'soltar', '--owner', 'B')
        stdout, stderr = waiter.communicate(timeout=30)
        assert waiter.returncode == 0, (stdout, stderr)
        pending(case, 'A', 1)
        next_receipt, next_worker, next_pipe = case.launch('A')
        assert next_receipt['execute'], next_receipt
        case.close_actor(next_worker, next_pipe)
        estado(lock, 'soltar', '--owner', 'A')
        print('HASITO REAL', variant, 'B2 admitted; A local-only until lock release; A then admitted', flush=True)
    finally:
        if waiter is not None and waiter.poll() is None:
            waiter.kill()
            waiter.communicate(timeout=30)
        case.tearDown()


def busy_lane():
    case = PolicyTest()
    case.setUp()
    try:
        case.h.now = case.h.now.replace(hour=16)
        directory = case.h.directory / 'local-locks'
        directory.mkdir(mode=0o700)
        lock1, lock2 = directory / 'desk.lock', directory / 'desk-2.lock'
        pending(case, 'X', 1)
        receipt, worker, pipe = case.launch('X')
        assert receipt['execute'], receipt
        pipe.send(('child', [0, 1]))
        assert pipe.poll(60)
        child = pipe.recv()
        case.extra_pids.append((child, process(child)['start']))
        worker.kill()
        worker.join(60)
        take(lock1, 'A')
        pending(case, 'A', 1)
        denied, _, _ = case.launch('A')
        assert denied['reason'] == 'resource-busy', denied
        take(lock2, 'B')
        pending(case, 'B', 2)
        admitted, other, connection = case.launch('B', 2)
        print('HASITO REAL HOL', denied['reason'], admitted['state'], admitted['reason'], flush=True)
        assert admitted['execute'], admitted
        case.close_actor(other, connection)
        estado(lock1, 'soltar', '--owner', 'A')
        estado(lock2, 'soltar', '--owner', 'B')
    finally:
        case.tearDown()


owner_retries('sync-timeout')
owner_retries('suspension')
busy_lane()
print('HASITO REAL ALL THREE PASS', flush=True)
