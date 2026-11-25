"""Synthetic Pueue 4.0.4 daemon for CC3-P1 process tests: frames only, no tasks.

argv: socket secret folder mode. Modes: success, failure, version (greets with
0.32.0), stall-greeting (waits for folder/go before frame 2), stall-reply
(waits for folder/go before frame 4), close (closes after frame 3). It records
markers and each frame 3 in hex in folder; it never parses the request.
"""
import os
from pathlib import Path
import socket
import struct
import sys
import time


def receive(connection, size):
    data = b''
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise EOFError()
        data += chunk
    return data


def frame(payload):
    return struct.pack('>Q', len(payload)) + payload


def text(value):
    data = value.encode()
    return bytes([0x60 | len(data)]) + data


def wait_for(path):
    while not path.exists():
        time.sleep(.005)


def serve(connection, secret, folder, mode):
    length = struct.unpack('>Q', receive(connection, 8))[0]
    if receive(connection, length) != secret:
        return
    (folder / 'secret-read').write_text('1')
    if mode == 'stall-greeting':
        wait_for(folder / 'go')
    connection.sendall(frame(b'0.32.0' if mode == 'version' else b'0.31.1'))
    try:
        length = struct.unpack('>Q', receive(connection, 8))[0]
        request = receive(connection, length)
    except EOFError:
        return
    with (folder / 'requests').open('a') as stream:
        stream.write(request.hex() + '\n')
    (folder / 'got-request').write_text('1')
    if mode == 'close':
        return
    if mode == 'stall-reply':
        wait_for(folder / 'go')
    key = 'Failure' if mode == 'failure' else 'Success'
    connection.sendall(frame(b'\xa1' + text(key) + text('synthetic')))
    try:
        while connection.recv(1):  # Like pueued: the client closes, not us.
            pass
    except OSError:
        pass


def main(socket_path, secret_path, folder, mode):
    folder = Path(folder)
    secret = Path(secret_path).read_bytes()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(socket_path)
    server.listen(4)
    (folder / 'ready').write_text(str(os.getpid()))
    while True:
        connection, _ = server.accept()
        with connection:
            try:
                serve(connection, secret, folder, mode)
            except (EOFError, OSError):
                pass


if __name__ == '__main__':
    main(*sys.argv[1:5])
