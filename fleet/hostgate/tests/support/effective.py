"""Autoridad efectiva privada y procesos propios, sin provisión productiva."""
import json
import os
from pathlib import Path
import socket
import sys
import uuid
from unittest.mock import patch
from hostgate.protocol import canonical
from hostgate.tests.support import coalition
from hostgate.tests.test_admin import Fixture, fixture_policy
from hostgate.tests.test_ipc import FixtureSocket, query
from hostgate.tests.support.world import World, request


class Coalition:
    """Proveedor de coalición del servidor de prueba: DarwinCoalition sobre el kernel sustituido.

    El dueño es el supervisor real que autoriza; observe_root crea la raíz como su hija, con uniqueid pid*10.
    """
    def __init__(self): self.kernel = None

    def _reader(self):
        from hostgate.darwin_coalition import DarwinCoalition
        return DarwinCoalition(_native=self.kernel)

    def certify(self, owner):
        if self.kernel is None:
            self.kernel = coalition.World()
            self.kernel.procs.clear(); self.kernel.counters = {coalition.CID: [0, 0], coalition.OTHER: [0, 0]}
            self.kernel.boot_value = owner.boot
            self.kernel.add(owner.pid, owner.unique)
        return self._reader().certify(owner)

    def observe(self, baseline, root=None): return self._reader().observe(baseline, root=root)

    def observe_root(self, owner, pid):
        if pid not in self.kernel.procs: self.kernel.add(pid, pid * 10, parent=owner.unique)
        return self._reader().observe_root(owner, pid)


class EffectiveFixture(Fixture):
    def __init__(self, root):
        super().__init__(root)
        self.doc.update(mode='effective', storeId='fixture-effective-' + uuid.uuid4().hex)

    def provision(self):
        from hostgate import admin
        self.publish()
        # Primero pasa por el producto: en la base el rechazo es al modo, no import.
        cfg = admin.load_config(self.locator)
        from hostgate.store import store_for
        anchor = {k: self.doc[k] for k in ('hardwareUuid', 'physicalHostId', 'storeId', 'stateDir', 'mode')}
        self.locator.anchor_path.write_text(canonical(dict(anchor, schema='fleet/hostgate-authority@1')))
        self.locator.anchor_path.chmod(0o444)
        self.locator.lock_path.touch(mode=0o440)
        store = store_for(cfg.node)
        store.initialize(cfg.node)
        self.writer = store.connect(writable=True)


def child(role, root):
    from hostgate import admin
    from hostgate.client import HostgateClient, request_supervisor, SupervisorHandoff
    from hostgate.identity import from_json
    from hostgate.scope import ClosureEvidence
    os.chdir(root)
    with fixture_policy(root), patch.object(admin, 'local_authority', return_value=EffectiveFixture(root).locator), \
            patch.object(socket, 'socket', FixtureSocket):
        if role == 'server':
            from hostgate.service import run
            class Synthetic(World):
                def __init__(self):
                    super().__init__()
                    self.coalition = Coalition()
                def closure(self, ids, reservation):
                    return ClosureEvidence.fixture(reservation['key'], reservation['physicalBoot'])
            return run(_locator=EffectiveFixture(root).locator, _synthetic=Synthetic(),
                       _ready=lambda: print('listening', flush=True))
        if role == 'supervisor':
            print('ready', flush=True)
            handoff = SupervisorHandoff(**json.loads(sys.stdin.readline()))
            with HostgateClient.connect(handoff, mode='effective') as client:
                for line in sys.stdin:
                    value = json.loads(line)
                    if '__query_channel__' in value:
                        with HostgateClient.connect(mode='effective') as other:
                            result = other.call(value['__query_channel__'], timeout=5)
                    else: result = client.call(value, timeout=5)
                    print(canonical(result), flush=True)
            return 0
        if role == 'query':
            with HostgateClient.connect(mode='effective') as client:
                print(canonical(client.call(query(), timeout=5)), flush=True)
            return 0
        value = request(os.environ.get('IPC_OPERATION', 'op'), executorId=os.environ.get('IPC_EXECUTOR', 'unix'),
                        physicalHostId='fixture-host', exclusiveResources=['sen-slot:1'])
        with request_supervisor(value, mode='effective') as requester:
            print(canonical(requester.handoff.__dict__), flush=True)
            requester.approve(from_json(json.loads(sys.stdin.readline())))
            print('approved', flush=True)
            sys.stdin.readline()
    return 0


if __name__ == '__main__': raise SystemExit(child(sys.argv[1], Path(sys.argv[2])))
