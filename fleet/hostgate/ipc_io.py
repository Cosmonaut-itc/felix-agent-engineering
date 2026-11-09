"""Buffers por conexión; ninguna operación de socket espera a otro peer."""
from collections import deque
import struct
import time
from .protocol import canonical, strict_json
from .wire import response_frames, TransportError


INCOMPLETE = object()


class Channel:
    def __init__(self, stream):
        self.stream = stream
        stream.setblocking(False)
        self.input = bytearray()
        self.size = None
        self.output = deque()
        self.pending = b''
        self.deadline = time.monotonic() + 5
        self.progress = time.monotonic()
        self.auth = self.challenge = None
        self.connection = None

    def queue(self, value, *, response=False):
        if response:
            frames = response_frames(value)
        else:
            raw = canonical(value).encode('ascii')
            if not 1 <= len(raw) <= 65536: raise TransportError('entrada excede 64 KiB')
            frames = iter((raw,))
        self.output.append(frames)

    def read(self):
        if self.deadline is None: self.deadline = time.monotonic() + 300
        needed = 4 if self.size is None else self.size
        try: raw = self.stream.recv(needed - len(self.input))
        except BlockingIOError: return INCOMPLETE
        if not raw: raise TransportError('frame interrumpido')
        self.progress = time.monotonic(); self.input.extend(raw)
        if len(self.input) != needed: return INCOMPLETE
        if self.size is None:
            self.size, = struct.unpack('!I', self.input); self.input.clear()
            if not 1 <= self.size <= 65536: raise TransportError('longitud de entrada inválida')
            return INCOMPLETE
        value = strict_json(self.input.decode('utf-8'))
        self.input.clear(); self.size = None
        return value

    def write(self):
        if not self.pending:
            while self.output:
                try: raw = next(self.output[0])
                except StopIteration: self.output.popleft(); continue
                self.pending = memoryview(struct.pack('!I', len(raw)) + raw)
                break
            if not self.pending:
                if self.connection and self.connection['state'] in ('active', 'requester'): self.deadline = None
                return
        try: count = self.stream.send(self.pending)
        except BlockingIOError: return
        if not count: raise TransportError('envío interrumpido')
        self.pending = self.pending[count:]; self.progress = time.monotonic()

    def expired(self, now):
        if self.deadline is None: return False
        return now >= self.deadline or (self.connection and self.connection['state'] == 'active' and now - self.progress >= 30)

    def close(self):
        if self.auth is not None: self.auth.close()
        if self.challenge is not None: self.challenge.close()
        for frames in self.output: frames.close() if hasattr(frames, 'close') else None
