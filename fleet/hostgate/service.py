"""Arranque manual sombra; sin instalación, provisión o procesos de cargas."""
import signal
import time
from . import admin
from .darwin_peer import DarwinPeers, DarwinHealth
from .scope import ScopeVerifier
from .store import Busy
from .ipc import serve


class Runtime(ScopeVerifier):
    def __init__(self, config, peers, synthetic=None):
        self.peers, self.synthetic = peers, synthetic
        self.reader = DarwinHealth(peers, config.node.snapshot_host, config.health_snapshot_path)

    def verify(self, identity): return self.peers.verify(identity)
    def physical_boot(self): return self.peers.boot()
    def executor_boot(self, executor): return self.peers.boot()
    def channel_alive(self, binding, *, pending=False): return self.sessions.alive(binding, pending=pending)
    def monotonic(self): return time.monotonic() if self.synthetic is None else self.synthetic.monotonic()
    def wall(self): return time.time() if self.synthetic is None else self.synthetic.wall()
    def health(self): return self.reader.read() if self.synthetic is None else self.synthetic.health()
    def profile_verified(self, profile): return self.synthetic.profile_verified(profile) if self.synthetic else False
    def closure(self, ids, reservation): return self.synthetic.closure(ids, reservation) if self.synthetic else super().closure(ids, reservation)
    def checkpoint(self, name): pass


def run(*, _locator=None, _synthetic=None, _ready=None):
    def interrupted(signum, frame): raise KeyboardInterrupt
    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        locator = _locator or admin.local_authority()
        config = admin.load_config(locator)
        with admin.AuthorityLease(locator, config) as lease, DarwinPeers() as peers:
            if peers.hardware_uuid() != config.hardware_uuid: raise admin.AdminError('hardware cambió')
            return serve(config, lease, peers, Runtime(config, peers, _synthetic), _ready=_ready)
    except Busy: return 4
    except KeyboardInterrupt: return 0
    except (OSError, ValueError): return 6
    finally: signal.signal(signal.SIGTERM, previous)


if __name__ == '__main__': raise SystemExit(run())
