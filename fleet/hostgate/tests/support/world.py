"""Fronteras simuladas compartidas; ninguna acredita una plataforma real."""
import copy
import json
from pathlib import Path
from hostgate.identity import Identity, Observation
from hostgate.model import AuthContext, NodeConfig
from hostgate.scope import ClosureEvidence
from hostgate.salud.lector_desk import _ts

FIXTURES = Path(__file__).parents[2] / 'fixtures'


class World:
    def __init__(self):
        self.now = 100.0
        self.utc = 1792792830.0
        self.boot = 'physical-1'
        self.executor_boots = {'unix': 'executor-1', 'other': 'executor-1'}
        self.processes = {}
        self.channel = True
        self.transport_calls = []
        self.profiles = {'fixture'}
        self.closures = {}
        self.fault = None
        self.hook = None
        self.sample = json.loads((FIXTURES / 'health/mini.json').read_text())
        self.sample_boot = self.sample['boot']['bootId']
        self.utc = _ts(self.sample['ts']) + 30
        self.health_kind = 'mini'
        self.default_shell = False
        self.witness = False

    def monotonic(self):
        return self.now

    def wall(self):
        return self.utc

    def physical_boot(self):
        return self.boot

    def executor_boot(self, executor):
        return self.executor_boots.get(executor)

    def verify(self, identity):
        actual = self.processes.get(identity.pid, 'absent')
        if actual == 'EPERM':
            return Observation('unverifiable')
        if actual == 'absent' or actual != identity:
            return Observation('confirmed_absent_or_different')
        return Observation('same', actual)

    def health(self):
        return self.health_kind, copy.deepcopy(self.sample), self.sample_boot

    def profile_verified(self, profile):
        return profile in self.profiles

    def channel_alive(self, binding):
        return self.channel and binding == 'fixture-channel'

    def closure(self, evidence_ids, reservation):
        return self.closures.get(tuple(evidence_ids), ClosureEvidence())

    def checkpoint(self, name):
        if self.hook:
            self.hook(name)
        if self.fault == name:
            raise OSError('injected ' + name)

    def auth(self, pid=100, principal='fixture', role='supervisor', start=(1, 2)):
        peer = Identity('unix', 501, pid, start, 'executor-1', self.boot)
        anchor = Identity('unix', 501, pid + 1000, (1, 1), 'executor-1', self.boot)
        self.processes[pid] = peer
        self.processes[anchor.pid] = anchor
        return AuthContext(principal, role, peer, anchor, 'fixture-channel')

    def transport(self, *args):
        self.transport_calls.append(args)
        raise AssertionError('sombra no conecta')


def config(path):
    return NodeConfig('node', 'shadow-store', ('unix', 'other'),
                      {'cpuMilli': 2000, 'memoryBytes': 2000,
                       'diskBytes': {'data': 2000}, 'slots': 2},
                      ('postgres:5434', 'postgres:5435', 'sen-slot:1', 'sen-slot:2',
                       'sen-test-db-1', 'sen-test-db-2'), Path(path).resolve(),
                      20.0, 10.0, 'r1', {'fixture': ('supervisor', 'query', 'operator')},
                      {'fixture': {'cpuMilli': 1000, 'memoryBytes': 1000,
                                   'diskBytes': {'data': 1000}, 'slots': 1}}, 'Mini-Fixture', 10)


def request(op='op', job='job', **changes):
    result = dict(schema='fleet/host-reservation@1', operationId=op, operation='reservar',
                  jobId=job, attemptId='a1', executorId='unix', physicalHostId='node',
                  demand=dict(cpuMilli=1000, memoryBytes=1000, diskBytes={'data': 1000}, slots=1),
                  exclusiveResources=[], profileId='fixture')
    result.update(changes)
    return result
