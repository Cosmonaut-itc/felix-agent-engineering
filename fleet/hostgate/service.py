"""Arranque manual; sin instalación, provisión ni procesos de cargas."""
import sys
import signal
import time
import uuid
from . import admin
from .darwin_peer import DarwinPeers, DarwinHealth
from .scope import ClosureEvidence, ScopeVerifier
from .store import Busy
from .ipc import serve


def _observed_build():
    from .coalition_selfcheck import _darwin_build
    try: return _darwin_build()
    except Exception: return None  # Sin build observado nada se acredita.


class Runtime(ScopeVerifier):
    """Fronteras reales del servicio. En efectivo expone la coalición y acredita perfiles una vez por arranque."""
    def __init__(self, config, peers, synthetic=None, *, _selfcheck=None, _build=None, _coalition=None):
        self.config, self.peers, self.synthetic = config, peers, synthetic
        self.reader = DarwinHealth(peers, config.node.snapshot_host, config.health_snapshot_path)
        effective = config.node.mode == 'effective'
        self.coalition = _coalition if _coalition is not None else getattr(synthetic, 'coalition', None)
        if self.coalition is None and effective and synthetic is None:
            from .darwin_coalition import DarwinCoalition
            self.coalition = DarwinCoalition()
        self.selfcheck_result = self.darwin_build = None
        # El selfcheck crea procesos: sólo corre si algún perfil puede acreditarse con él, antes de servir.
        if effective and synthetic is None and any(p.accreditation is not None for p in config.profiles.values()):
            from . import coalition_selfcheck
            self.darwin_build = (_build or _observed_build)()
            self.selfcheck_result = (_selfcheck or coalition_selfcheck.selfcheck)()

    def verify(self, identity): return self.peers.verify(identity)
    def physical_boot(self): return self.peers.boot()
    def executor_boot(self, executor): return self.peers.boot()
    def channel_alive(self, binding, *, pending=False): return self.sessions.alive(binding, pending=pending)
    def monotonic(self): return time.monotonic() if self.synthetic is None else self.synthetic.monotonic()
    def wall(self): return time.time() if self.synthetic is None else self.synthetic.wall()
    def health(self): return self.reader.read() if self.synthetic is None else self.synthetic.health()
    def profile_verified(self, profile, workload_uid=None):
        """Accredited para este boot y el uid de la carga, o False."""
        if self.synthetic is not None: return self.synthetic.profile_verified(profile, workload_uid=workload_uid)
        spec = self.config.profiles.get(profile)
        if spec is None or self.selfcheck_result is None or workload_uid is None: return False
        verdict = admin.accredit(spec, darwin_build=self.darwin_build, workload_uid=workload_uid, selfcheck=self.selfcheck_result)
        if type(verdict) is not admin.Accredited: return False
        try:  # Forma canónica de 4a: el selfcheck da minúsculas y kern.bootsessionuuid mayúsculas.
            same = str(uuid.UUID(verdict.boot)) == str(uuid.UUID(self.peers.boot()))
        except Exception:
            return False
        return verdict if same else False

    def closure(self, ids, reservation):
        """Sólo la sombra lee pruebas de fuera; la real la emite hostgate dentro de soltar."""
        if self.synthetic is not None and self.config.node.mode == 'shadow': return self.synthetic.closure(ids, reservation)
        return ClosureEvidence()
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
    except (OSError, ValueError) as exc:
        print('hostgate: ' + str(exc), file=sys.stderr); return 6
    finally: signal.signal(signal.SIGTERM, previous)


if __name__ == '__main__': raise SystemExit(run())
