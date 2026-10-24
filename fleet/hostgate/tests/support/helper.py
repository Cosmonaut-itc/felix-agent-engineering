"""Padres propios conservan el único escritor hasta su salida normal."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from hostgate.admission import Hostgate
from hostgate.tests.support.world import SharedWorld, config, request


def wait(path, seconds=45):
    deadline = time.monotonic() + seconds
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not path.exists(): raise RuntimeError('barrera propia vencida: ' + str(path))


def main():
    mode, directory, world_path, store_path, job = sys.argv[1:]
    directory = Path(directory)
    if mode.startswith('parent-'):
        read_fd, write_fd = os.pipe()
        subprocess.Popen([sys.executable, '-B', '-m', 'hostgate.tests.support.helper',
                          mode[7:], str(directory), world_path, store_path, job, str(read_fd)],
                         pass_fds=(read_fd,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(read_fd)
        sys.stdin.buffer.read()
        # No se cierra anticipadamente el escritor: su vida termina con el padre.
        os._exit(0)


def child():
    mode, directory, world_path, store_path, job, fd = sys.argv[1:]
    directory = Path(directory)
    world = SharedWorld(world_path)
    auth = world.real_auth(os.getpid(), os.getppid(), 'channel-' + job)
    (directory / 'ready.json').write_text(json.dumps(dict(pid=os.getpid(), parent=os.getppid())))
    wait(directory / 'prepared')
    if mode == 'linked':
        while os.read(int(fd), 1): pass
    else:
        wait(directory / 'retry')
        gate = Hostgate(config(Path(store_path)), world)
        replies = [gate.reservar(request('jale-' + job, job), auth),
                   gate.reservar(request('retry-' + job, job), auth)]
        (directory / 'retries.json').write_text(json.dumps(replies))
        wait(directory / 'finish')
    (directory / 'ended').write_text(mode)


if __name__ == '__main__':
    if sys.argv[1].startswith('parent-'): main()
    else: child()
