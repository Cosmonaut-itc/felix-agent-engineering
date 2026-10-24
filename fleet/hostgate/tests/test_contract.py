import copy
import json
import tempfile
import unittest
from pathlib import Path
from hostgate.store import ShadowStore, StoreError
from hostgate.admission import Hostgate
from hostgate.model import AuthContext
from hostgate.scope import ClosureEvidence, authorize_creation, record_started
from hostgate.tests.support.world import World, config, request, FIXTURES


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.world = World()
        self.config = config(Path(self.tmp.name) / 'shadow')
        ShadowStore.initialize(self.config)
        self.gate = Hostgate(self.config, self.world)
        self.auth = self.world.auth()

    def reserve(self, op='op', job='job', auth=None, **changes):
        return self.gate.reservar(request(op, job, **changes), auth or self.auth)

    def view(self):
        return self.gate.estado({}, self.auth)

    def key(self, reply):
        return reply['simulationKey']

    def release(self, key, op='release'):
        self.world.closures[('proof',)] = ClosureEvidence.fixture(key, self.world.boot)
        return self.gate.soltar(op, key, ['proof'], self.auth)

    def codes(self, reply):
        return {x['code'] for x in reply['reasons']}


class Contract(Harness):
    def test_b_g1_windows_proof_cannot_release_unix(self):
        from dataclasses import replace
        from hostgate.protocol import digest
        key = self.key(self.reserve(exclusiveResources=['sen-slot:1', 'postgres:5434']))
        proof = replace(ClosureEvidence.fixture(key, self.world.boot, 'windows-tree-empty'),
                        profile_verified=False, lineage_complete=False)
        values = dict(proof.__dict__); values.pop('evidence_digest')
        proof = replace(proof, evidence_digest=digest(values))
        self.world.closures[('cross-platform',)] = proof
        result = self.gate.soltar('cross-platform', key, ['cross-platform'], self.auth)
        self.assertEqual(result['exitCode'], 1)
        self.assertIn('unix-scope-unconfirmed', self.codes(result))
        self.assertEqual(self.view()['budget']['retained']['slots'], 1)
        self.assertEqual({r['name'] for r in self.view()['resources']}, {'sen-slot:1', 'postgres:5434'})

    def test_b_g2_replay_requires_exact_peer_and_channel(self):
        from dataclasses import replace
        key = self.key(self.reserve())
        original = self.gate.latido('heartbeat', key, 1, self.auth)
        before = self.view()['reservations'][0]['heartbeat']
        other = self.world.auth(pid=222)
        self.world.now += 1
        for auth in (other, replace(self.auth, channel_binding='other-channel')):
            with self.subTest(auth=auth):
                self.world.channel_alive = lambda binding: True
                result = self.gate.latido('heartbeat', key, 1, auth)
                self.assertEqual(result['exitCode'], 5)
                self.assertNotIn('originalResult', result)
        legitimate = self.gate.latido('heartbeat', key, 1, self.auth)
        self.assertEqual(legitimate['originalResult'], original)
        self.assertFalse(legitimate['wouldGrant'])
        self.assertEqual(legitimate['currentState']['state'], 'reserved')
        self.assertEqual(self.view()['reservations'][0]['heartbeat'], before)

    def test_a_i1_foreign_peer_cannot_consume_prepared_operation(self):
        from dataclasses import replace
        self.gate.prepare(request(), self.auth)
        other = self.world.auth(pid=222)
        self.world.channel_alive = lambda binding: True
        for auth in (other, replace(self.auth, channel_binding='foreign-channel')):
            denied = self.reserve(auth=auth)
            self.assertEqual(denied['exitCode'], 5)
            self.assertNotIn('originalResult', denied)
            self.assertEqual(self.view()['reservations'], [])
        legitimate = self.reserve()
        self.assertEqual(legitimate['exitCode'], 0)
        self.assertTrue(legitimate['wouldGrant'])
        replay = self.reserve()
        self.assertEqual(replay['originalResult'], legitimate)
        self.assertFalse(replay['wouldGrant'])

    def test_a_i1_foreign_mutations_leave_ids_available_to_owner(self):
        from dataclasses import replace
        key = self.key(self.reserve())
        other = self.world.auth(pid=222)
        self.world.channel_alive = lambda binding: True
        self.world.now += 2
        before = self.gate.store.read()['reservations'][0]['heartbeat']
        for auth in (other, replace(self.auth, channel_binding='foreign-channel')):
            for call in (lambda: self.gate.latido('new-heartbeat', key, 1, auth),
                         lambda: self.gate.soltar('new-release', key, ['proof'], auth)):
                denied = call()
                self.assertEqual(denied['exitCode'], 5)
                self.assertNotIn('originalResult', denied)
                row = self.gate.store.read()['reservations'][0]
                self.assertEqual((row['state'], row['seq'], row['heartbeat']), ('reserved', 0, before))
        heartbeat = self.gate.latido('new-heartbeat', key, 1, self.auth)
        self.assertEqual(heartbeat['exitCode'], 0)
        row = self.gate.store.read()['reservations'][0]
        self.assertEqual((row['seq'], row['heartbeat']), (1, self.world.now))
        self.assertGreater(row['heartbeat'], before)
        self.world.closures[('proof',)] = ClosureEvidence.fixture(key, self.world.boot)
        self.assertEqual(self.gate.soltar('new-release', key, ['proof'], self.auth)['exitCode'], 0)
        self.assertEqual(self.view()['budget']['retained']['slots'], 0)

    def test_reserve_replay_and_query_role_same_peer_are_not_authority(self):
        from dataclasses import replace
        original = self.reserve()
        for auth in (self.world.auth(pid=222), replace(self.auth, role='query')):
            denied = self.reserve(auth=auth)
            self.assertEqual(denied['exitCode'], 5)
            self.assertNotIn('originalResult', denied)
        query = replace(self.auth, role='query')
        self.assertEqual(self.gate.latido('query-heartbeat', self.key(original), 1, query)['exitCode'], 5)

    def test_b_g3_revalidate_after_health_io(self):
        from dataclasses import replace
        initial_health = self.world.health
        for fault in ('death', 'reused', 'anchor', 'eof', 'boot', 'executor-boot', 'deadline'):
            with self.subTest(fault=fault):
                self.world.boot = 'physical-1'
                self.world.executor_boots['unix'] = 'executor-1'
                self.world.channel = True
                self.world.now = 100
                self.auth = self.world.auth()
                def health():
                    sample = initial_health()
                    if fault == 'death': self.world.processes[self.auth.peer.pid] = 'absent'
                    if fault == 'reused': self.world.processes[self.auth.peer.pid] = replace(self.auth.peer, start_token=(9, 9))
                    if fault == 'anchor': self.world.processes[self.auth.anchor.pid] = 'absent'
                    if fault == 'eof': self.world.channel = False
                    if fault == 'boot': self.world.boot = 'physical-2'
                    if fault == 'executor-boot': self.world.executor_boots['unix'] = 'executor-2'
                    if fault == 'deadline': self.world.now += 21
                    return sample
                self.world.health = health
                result = self.reserve('during-health-' + fault, fault)
                self.assertFalse(result['wouldGrant'])
                self.assertEqual(self.gate.store.read()['reservations'], [])
        self.world.health = initial_health

    def test_shadow_only_and_future_interfaces_closed(self):
        r = self.reserve()
        self.assertEqual((r['result'], r['wouldGrant']), ('shadow', True))
        self.assertNotIn('reservationId', r)
        for fn in (authorize_creation, record_started):
            with self.assertRaises(NotImplementedError):
                fn(None, None)

    def test_budget_dimensions_and_exclusives(self):
        self.reserve(exclusiveResources=['postgres:5434'])
        r = self.reserve('two', 'two', exclusiveResources=['postgres:5434'])
        self.assertIn('resource-busy', self.codes(r))
        self.reserve('three', 'three')
        r = self.reserve('four', 'four')
        details = str(r['reasons'])
        for dimension in ('cpuMilli', 'memoryBytes', 'diskBytes:data', 'slots'):
            self.assertIn(dimension, details)
        self.assertIn('waiting-capacity', self.codes(r))

    def test_minimum_and_unknown_resource(self):
        r = self.reserve(demand=dict(cpuMilli=0, memoryBytes=0, diskBytes={'data': 0}, slots=1))
        self.assertEqual(self.view()['budget']['retained']['cpuMilli'], 1000)
        self.assertFalse(self.reserve('x', 'x', exclusiveResources=['unknown'])['wouldGrant'])

    def test_replay_authentication_conflict_and_historical_state(self):
        r = self.reserve()
        self.assertEqual(self.key(r), self.key(self.reserve()))
        changed = request(); changed['attemptId'] = 'different'
        self.assertIn('operation-conflict', self.codes(self.gate.reservar(changed, self.auth)))
        bad = AuthContext('intruder', 'supervisor', self.auth.peer, self.auth.anchor, 'fixture-channel')
        self.assertEqual(self.gate.reservar(request(), bad)['exitCode'], 5)
        self.release(self.key(r))
        replay = self.reserve()
        self.assertEqual(replay['originalResult']['wouldGrant'], True)
        self.assertEqual(replay['currentState']['state'], 'released')
        self.assertFalse(replay['wouldGrant'])

    def test_rejection_stable_after_health_recovers(self):
        self.world.profiles.clear()
        self.assertFalse(self.reserve()['wouldGrant'])
        self.world.profiles.add('fixture')
        self.assertFalse(self.reserve()['wouldGrant'])
        self.assertTrue(self.reserve('new')['wouldGrant'])

    def test_old_generation_never_alters_new(self):
        one = self.reserve(); old = self.key(one)
        self.release(old)
        two = self.reserve('two'); new = self.key(two)
        self.assertEqual(new['generation'], old['generation'] + 1)
        forged = dict(new, generation=old['generation'])
        self.assertIn('stale-generation', self.codes(self.gate.latido('late', forged, 4, self.auth)))
        self.gate.dudosa('old-doubt', old, 'late', [], self.auth)
        self.assertEqual(self.view()['reservations'][-1]['effectiveState'], 'reserved')

    def test_ttl_owner_lost_boot_and_heartbeat_cannot_release(self):
        key = self.key(self.reserve())
        self.world.now += 16
        v = self.view()
        self.assertEqual(v['reservations'][0]['effectiveState'], 'uncertain')
        self.assertEqual(v['budget']['retained']['slots'], 1)
        self.gate.latido('fresh', key, 1, self.auth)
        self.assertEqual(self.view()['reservations'][0]['effectiveState'], 'uncertain')
        self.assertFalse(self.reserve('next', 'next')['wouldGrant'])
        self.world.boot = 'new-physical'
        self.assertTrue(self.view()['drained'])
        self.assertEqual(self.view()['budget']['retained']['slots'], 1)

    def test_absent_reused_and_unverifiable_peer_is_unauthorized(self):
        for value in ('absent', 'EPERM', copy.copy(self.auth.peer)):
            with self.subTest(value=value):
                self.world.processes[self.auth.peer.pid] = value
                if value == self.auth.peer:
                    from dataclasses import replace
                    self.world.processes[self.auth.peer.pid] = replace(value, start_token=(1, 3))
                result = self.reserve('identity-' + str(value))
                self.assertFalse(result['wouldGrant'])
                self.assertEqual(result['exitCode'], 5)
                self.assertEqual(self.codes(result), {'identity-unverifiable'})

    def test_commit_revalidation_pid_anchor_channel_heartbeat(self):
        for fault in ('pid', 'anchor', 'channel', 'heartbeat'):
            with self.subTest(fault=fault):
                self.world.channel = True
                self.auth = self.world.auth()
                self.world.now = 100
                def hook(name):
                    if name == 'before_revalidate':
                        if fault == 'pid': self.world.processes[self.auth.peer.pid] = 'absent'
                        if fault == 'anchor': self.world.processes[self.auth.anchor.pid] = 'absent'
                        if fault == 'channel': self.world.channel = False
                        if fault == 'heartbeat': self.world.now += 30
                self.world.hook = hook
                self.assertFalse(self.reserve(fault, fault)['wouldGrant'])
        self.world.hook = None

    def test_death_after_revalidation_retains_commit(self):
        def hook(name):
            if name == 'before_commit':
                self.world.processes[self.auth.peer.pid] = 'absent'
        self.world.hook = hook
        r = self.reserve()
        self.assertTrue(r['wouldGrant'])
        query_auth = self.world.auth(pid=333, role='query')
        view = self.gate.estado({}, query_auth)
        self.assertEqual(view['reservations'][0]['effectiveState'], 'uncertain')
        self.assertEqual(view['budget']['retained']['slots'], 1)

    def test_scope_and_cleanup_all_or_nothing(self):
        from dataclasses import replace
        from hostgate.protocol import digest
        resources = ['sen-slot:1', 'postgres:5434', 'sen-test-db-1']
        key = self.key(self.reserve(exclusiveResources=resources))
        valid = ClosureEvidence.fixture(key, self.world.boot)
        for field in ('complete', 'creators_sealed', 'root_terminal', 'scope_empty',
                      'lineage_complete', 'profile_verified', 'resources_clean', 'scope'):
            with self.subTest(field=field):
                proof = replace(valid, **{field: 'foreign-scope' if field == 'scope' else False})
                values = dict(proof.__dict__); values.pop('evidence_digest')
                self.world.closures[(field,)] = replace(proof, evidence_digest=digest(values))
                result = self.gate.soltar('close-' + field, key, [field], self.auth)
                self.assertEqual(result['exitCode'], 1)
                self.assertIn('resource-cleanup-unconfirmed' if field == 'resources_clean'
                              else 'unix-scope-unconfirmed', self.codes(result))
                self.assertEqual(self.view()['budget']['retained']['slots'], 1)
                self.assertEqual({r['name'] for r in self.view()['resources']}, set(resources))
        self.release(key)
        self.assertEqual(self.view()['budget']['retained']['slots'], 0)
        self.gate.soltar('repeat-release', key, [], self.auth)
        self.assertEqual(self.view()['budget']['retained']['slots'], 0)

    def test_windows_fixture_only_positive_evidence_fenced(self):
        auth = self.world.auth(pid=222, executor='windows')
        for kind in ('windows-tree-empty', 'never-created'):
            key = self.key(self.reserve(kind, kind, auth=auth, executorId='windows'))
            proof = ClosureEvidence.fixture(key, self.world.boot, kind=kind)
            self.world.closures[(kind,)] = proof
            result = self.gate.soltar('close-' + kind, key, [kind], auth)
            self.assertEqual(result['simulationState'], 'released')
        key = self.key(self.reserve('third', 'third', auth=auth, executorId='windows'))
        for kind in ('unix-scope-empty', 'unconfirmed'):
            self.world.closures[(kind,)] = ClosureEvidence.fixture(key, self.world.boot, kind)
            self.world.now += 15
            self.assertEqual(self.gate._health(), [])
            result = self.gate.soltar('foreign-' + kind, key, [kind], auth)
            self.assertEqual(result['exitCode'], 1)
            self.assertIn('windows-tree-unconfirmed', self.codes(result))
            self.assertEqual(self.view()['budget']['retained']['slots'], 1)
        self.world.closures[('old',)] = proof
        self.gate.soltar('bad-proof', key, ['old'], auth)
        self.assertEqual(self.view()['budget']['retained']['slots'], 1)

    def test_desk_no_transport_even_degraded(self):
        from hostgate.salud.lector_desk import _ts
        self.world.health_kind = 'desk'
        self.world.sample = json.loads((FIXTURES / 'health/desk.json').read_text())
        self.world.utc = _ts(self.world.sample['ts']) + 30
        self.world.sample_boot = self.world.sample['boot']['bootId']
        self.world.witness = True
        self.world.default_shell = False
        denied = self.reserve('desk-shell')
        self.assertFalse(denied['wouldGrant'])
        self.assertEqual(self.codes(denied), {'default-shell-unverified'})
        self.world.default_shell = True
        self.assertTrue(self.reserve('desk-synthetic-gates')['wouldGrant'])
        self.release(self.key(self.reserve('desk-synthetic-gates')))
        self.world.witness = False
        for shell in (False, True):
            self.world.default_shell = shell
            denied = self.reserve('desk-no-witness-' + str(shell))
            self.assertFalse(denied['wouldGrant'])
            self.assertIn('boot-unverified', self.codes(denied))
        self.assertEqual(self.world.transport_calls, [])

    def test_unknown_health_reason_is_barrier(self):
        from unittest.mock import patch
        with patch('hostgate.admission.lector_mini.evaluar', return_value=['new-reader-reason']):
            result = self.reserve()
        self.assertFalse(result['wouldGrant'])
        self.assertEqual(result['reasons'], [dict(code='health-invalid', detail='new-reader-reason', retryable=False)])

    def test_jale_real_helpers_bound_to_three_preparations(self):
        import os
        import subprocess
        import sys
        from hostgate.tests.support.helper import wait
        from hostgate.tests.support.world import SharedWorld, publish_world
        for filename, possible in [(name, variant) for name in ('jale-2254z.json', 'jale-2254z-linked.json')
                                  for variant in (False, True)]:
            fixture = json.loads((FIXTURES / filename).read_text())
            root = Path(self.tmp.name).resolve() / (filename + str(possible))
            root.mkdir()
            state_path = root / 'world.json'
            state = dict(now=100, boot='physical-1',
                         executorBoots={'unix': 'executor-1', 'other': 'executor-1', 'windows': 'executor-1'},
                         realProcesses=True, channelParents={})
            publish_world(state_path, state)
            world = SharedWorld(state_path)
            cfg = config(root / 'shadow')
            ShadowStore.initialize(cfg)
            gate = Hostgate(cfg, world)
            query = world.real_auth(os.getpid(), os.getpid(), 'query')
            parents, helpers = [], []
            try:
                for turn in fixture['turns']:
                    job = filename + '-' + str(turn)
                    directory = root / str(turn); directory.mkdir()
                    parent = subprocess.Popen([sys.executable, '-B', '-m', 'hostgate.tests.support.helper',
                              'parent-' + ('linked' if fixture['linked'] else 'legacy'), str(directory),
                              str(state_path), str(cfg.state_dir), job], stdin=subprocess.PIPE)
                    parents.append(parent)
                    wait(directory / 'ready.json')
                    ids = json.loads((directory / 'ready.json').read_text())
                    auth = world.real_auth(ids['pid'], parent.pid, 'channel-' + job)
                    from hostgate.model import identity_json
                    state['channelParents']['channel-' + job] = identity_json(auth.anchor)
                    publish_world(state_path, state)
                    req = request('jale-' + job, job)
                    if possible and turn == fixture['turns'][0]:
                        committed = gate.reservar(req, auth)
                        self.assertTrue(committed['wouldGrant'])
                        pending = committed['pendingId']
                    else:
                        pending = gate.prepare(req, auth)
                    helpers.append((directory, pending, auth, job))
                    (directory / 'prepared').touch()
                view = gate.estado({}, query)
                self.assertEqual(len(view['reservations']), int(possible))
                self.assertEqual(view['budget']['retained']['slots'], int(possible))
                self.assertEqual(sum(p['effectiveState'] == 'validating' for p in view['pending']), 3 - int(possible))
                for parent in parents:
                    parent.stdin.close()
                    self.assertEqual(parent.wait(timeout=45), 0)
                for directory, pending, auth, job in helpers:
                    if fixture['linked']:
                        wait(directory / 'ended')
                    else:
                        self.assertEqual(world.verify(auth.peer).status, 'same')
                        (directory / 'retry').touch()
                        wait(directory / 'retries.json')
                        replies = json.loads((directory / 'retries.json').read_text())
                        self.assertTrue(all(not r['wouldGrant'] for r in replies))
                        self.assertTrue(all(r['exitCode'] == 5 and self.codes(r) == {'identity-unverifiable'}
                                            and 'originalResult' not in r for r in replies))
                    view = gate.estado({}, query)
                    row = next(p for p in view['pending'] if p['id'] == pending)
                    if possible and job.endswith('-' + str(fixture['turns'][0])):
                        self.assertEqual(row['effectiveState'], 'simulated')
                    else:
                        self.assertEqual(row['effectiveState'], 'expired')
                        self.assertIn('anchor-dead', self.codes(row))
                    self.assertEqual(row['requester']['pid'], auth.peer.pid)
                    self.assertEqual(row['anchor']['pid'], auth.anchor.pid)
                follower = gate.reservar(request('follower-' + filename, 'follower-' + filename), query)
                self.assertEqual(follower['wouldGrant'], not possible)
                persisted = gate.store.read()
                self.assertTrue(all(p['state'] in ('expired', 'granted') for p in persisted['pending'] if p['jobId'].startswith(filename)))
                original_ids = {pending for _, pending, _, _ in helpers}
                self.assertEqual(sum(p['state'] == 'expired' for p in persisted['pending'] if p['id'] in original_ids), 3 - int(possible))
                if possible:
                    self.assertIn('reservation-uncertain', self.codes(follower))
                    view = gate.estado({}, query)
                    self.assertEqual(view['budget']['retained']['slots'], 1)
                    self.assertEqual(view['budget']['available']['slots'], 1)
                    self.assertEqual(view['reservations'][0]['effectiveState'], 'uncertain')
                    continue
                key = follower['simulationKey']
                world.closures[('proof',)] = ClosureEvidence.fixture(key, world.physical_boot())
                self.assertEqual(gate.soltar('release-' + filename, key, ['proof'], query)['exitCode'], 0)
            finally:
                for parent in parents:
                    if not parent.stdin.closed: parent.stdin.close()
                    parent.wait(timeout=45)
                for directory, _, auth, _ in helpers:
                    (directory / 'retry').touch(); (directory / 'finish').touch()
                    wait(directory / 'ended')
                    from hostgate.identity import ProcessTable
                    import time
                    deadline = time.monotonic() + 45
                    table = ProcessTable('unix', host='Mini-Fixture')
                    while table.identity(auth.peer.pid).status == 'same' and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertEqual(table.identity(auth.peer.pid).status, 'confirmed_absent_or_different')

    def test_jale_independent_causes_and_possible_creation(self):
        from dataclasses import replace
        for cause, expected in (('anchor', 'anchor-dead'), ('start', 'requester-dead'),
                                ('ttl', 'pending-expired'), ('eof', 'requester-dead')):
            auth = self.world.auth(pid=200 + len(self.gate.store.read()['pending']))
            req = request('prepare-' + cause, cause)
            pending = self.gate.prepare(req, auth)
            if cause == 'anchor': self.world.processes[auth.anchor.pid] = 'absent'
            if cause == 'start': self.world.processes[auth.peer.pid] = replace(auth.peer, start_token=(8, 8))
            if cause == 'ttl': self.world.now += 10
            if cause == 'eof': self.world.channel = False
            row = next(p for p in self.view()['pending'] if p['id'] == pending)
            self.assertEqual({r['code'] for r in row['reasons']}, {expected})
            self.assertEqual(self.view()['budget']['retained']['slots'], 0)
            self.assertFalse(self.gate.reservar(req, auth)['wouldGrant'])
            self.assertFalse(self.gate.reservar(dict(req, operationId='retry-' + cause), auth)['wouldGrant'])
            self.world.channel = True
        self.assertTrue(self.reserve('could-create', 'could-create')['wouldGrant'])
        self.world.processes[self.auth.peer.pid] = 'absent'
        self.auth = self.world.auth(pid=777)
        view = self.view()
        self.assertEqual(view['budget']['available']['slots'], 1)
        self.assertEqual(view['reservations'][0]['effectiveState'], 'uncertain')
        self.assertIn('reservation-uncertain', self.codes(self.reserve('blocked', 'blocked')))
        self.assertEqual(self.view()['budget']['retained']['slots'], 1)

    def test_channel_eof_withdraws_without_reservation(self):
        self.gate.prepare(request(), self.auth)
        self.world.channel = False
        self.assertEqual(self.view()['pending'][0]['effectiveState'], 'withdrawn')
        self.assertFalse(self.reserve()['wouldGrant'])

    def test_executor_restart_and_clock_regression(self):
        self.reserve()
        self.world.executor_boots['unix'] = 'restarted'
        self.assertEqual(self.view()['reservations'][0]['effectiveState'], 'uncertain')
        self.world.now = 0
        self.assertEqual(self.view()['budget']['retained']['slots'], 1)

    def test_preparation_cannot_be_adopted_by_another_peer(self):
        self.gate.prepare(request(), self.auth)
        other = self.world.auth(pid=222)
        r = self.reserve(auth=other)
        self.assertFalse(r['wouldGrant'])
        self.assertEqual(r['exitCode'], 5)
        self.assertEqual(self.view()['reservations'], [])

    def test_dead_peer_cannot_replay_authenticated_operation(self):
        self.reserve()
        self.world.processes[self.auth.peer.pid] = 'absent'
        r = self.reserve()
        self.assertEqual(r['exitCode'], 5)
        self.assertNotIn('originalResult', r)

    def test_positive_closure_evidence_is_durable(self):
        key = self.key(self.reserve())
        self.release(key)
        evidence = self.view()['reservations'][0]['closureEvidence']
        self.assertEqual(evidence['key'], key)
        self.assertEqual(evidence['provenance'], 'simulation')
        self.assertTrue(evidence['evidence_digest'])

    def test_malformed_closure_provider_fails_closed(self):
        key = self.key(self.reserve())
        self.world.closures[('broken',)] = None
        reply = self.gate.soltar('broken-provider', key, ['broken'], self.auth)
        self.assertFalse(reply['wouldGrant'])
        self.assertEqual(self.view()['budget']['retained']['slots'], 1)

    def test_supervisor_lost_with_descendant_alive_retains_all(self):
        key = self.key(self.reserve(exclusiveResources=['sen-slot:1']))
        child = self.world.auth(pid=444).peer
        query_auth = self.world.auth(pid=555, role='query')
        self.world.processes[self.auth.peer.pid] = 'absent'
        view = self.gate.estado({}, query_auth)
        self.assertEqual(self.world.verify(child).status, 'same')
        self.assertEqual(view['reservations'][0]['effectiveState'], 'uncertain')
        self.assertEqual(view['budget']['retained']['slots'], 1)
        self.assertEqual(view['resources'][0]['holder'], key)

    def test_explicit_dudosa_retains_after_publication_failure(self):
        from unittest.mock import patch
        from hostgate.owner_record import publish_owner
        key = self.key(self.reserve(exclusiveResources=['postgres:5434']))
        record = dict(schema='fleet/host-owner@1', runId='r', jobId='job', attemptId='a1',
                      key=key, physicalHostId='node', executorId='unix', boot=self.world.boot,
                      pid=self.auth.peer.pid, start=list(self.auth.peer.start_token), started=1,
                      ended=None, root=None, recordSeq=1, scope={'status': 'unconfirmed'})
        path = Path(self.tmp.name).resolve() / 'owner.json'
        with patch('hostgate.owner_record.os.fsync', side_effect=[None, OSError(5, 'directory I/O')]):
            with self.assertRaises(OSError): publish_owner(record, path)
        self.gate.dudosa('publication-failure', key, 'owner-publication-uncertain', [], self.auth)
        self.assertEqual(self.view()['reservations'][0]['effectiveState'], 'uncertain')
        self.assertEqual(self.view()['budget']['retained']['slots'], 1)
        self.assertEqual(self.view()['resources'][0]['holder'], key)

    def test_desk_huge_integer_is_invalid_at_adapter_boundary(self):
        from hostgate.salud.lector_desk import _ts
        self.world.health_kind = 'desk'
        self.world.sample = json.loads((FIXTURES / 'health/desk.json').read_text())
        self.world.utc = _ts(self.world.sample['ts']) + 30
        self.world.sample_boot = self.world.sample['boot']['bootId']
        self.world.default_shell = self.world.witness = True
        self.assertEqual(self.gate._health(), [])
        self.world.sample['metrics']['cpuRootPct']['value'] = 10**400
        result = self.reserve()
        self.assertEqual(result['exitCode'], 1)
        self.assertIn('health-invalid', self.codes(result))
        self.assertEqual(self.view()['reservations'], [])

    def test_boot_change_after_revalidation_keeps_ledger_usable(self):
        from unittest.mock import patch
        validate_identity = self.gate._identity_reasons
        def revalidate(pending):
            reasons = validate_identity(pending)
            self.assertEqual(reasons, [])
            self.world.boot = 'physical-2'
            self.world.executor_boots['unix'] = 'executor-2'
            return reasons
        with patch.object(self.gate, '_identity_reasons', side_effect=revalidate):
            result = self.reserve()
        self.assertTrue(result['wouldGrant'])
        stored = self.gate.store.read()
        row = stored['reservations'][0]
        self.assertEqual(row['physicalBoot'], stored['pending'][0]['physicalBoot'])
        self.assertEqual(row['executorBoot'], stored['pending'][0]['executorBoot'])
        view = self.view()
        self.assertEqual(view['reservations'][0]['effectiveState'], 'uncertain')
        self.assertTrue(view['drained'])
        self.assertEqual(view['budget']['retained']['slots'], 1)
        self.assertEqual(self.gate.dudosa('retain', row['key'], 'boot changed', [], self.auth)['exitCode'], 0)
        self.assertEqual(self.gate.store.read()['reservations'][0]['state'], 'uncertain')

    def test_observation_skips_terminal_history_without_erasing_it(self):
        from unittest.mock import patch
        for i in range(4):
            self.release(self.key(self.reserve('history-' + str(i), 'history-' + str(i))), 'release-' + str(i))
        self.world.profiles.clear()
        self.assertEqual(self.reserve('rejected', 'rejected')['exitCode'], 1)
        self.world.profiles.add('fixture')
        self.gate.prepare(request('withdraws', 'withdraws'), self.auth)
        def close_channel(name):
            if name == 'before_write': self.world.channel = False
        self.world.hook = close_channel
        self.gate.prepare(request('expires', 'expires'), self.auth)
        self.world.hook = None
        self.world.channel = True
        self.world.now += 21
        self.assertTrue(self.reserve('active', 'active')['wouldGrant'])
        self.gate.prepare(request('validating', 'validating'), self.auth)
        before = self.gate.store.read()
        with patch.object(self.world, 'verify', wraps=self.world.verify) as verify, \
                patch.object(self.world, 'channel_alive', wraps=self.world.channel_alive) as channel:
            view = self.view()
        self.assertEqual(verify.call_count, 4)
        self.assertEqual(channel.call_count, 2)
        self.assertEqual(len(view['pending']), 9)
        self.assertEqual(len(view['reservations']), 5)
        self.assertEqual(self.gate.store.read(), before)
        self.assertTrue(any(p['state'] == 'expired' for p in before['pending']))
        self.assertTrue(any(p['state'] == 'withdrawn' for p in before['pending']))
        replay = self.reserve('rejected', 'rejected')
        self.assertEqual(replay['originalResult']['exitCode'], 1)

    def test_closure_fencing_fields_and_never_created_each_fail_alone(self):
        from dataclasses import replace
        from hostgate.protocol import digest
        resources = ['sen-slot:1', 'postgres:5434']
        key = self.key(self.reserve(exclusiveResources=resources))
        for kind in ('unix-scope-empty', 'never-created'):
            valid = ClosureEvidence.fixture(key, self.world.boot, kind)
            changes = [('boot', 'foreign-boot'), ('issuer', ''), ('provenance', 'untrusted')]
            if kind == 'never-created':
                changes += [('permit_revoked', False), ('no_external_effects', False)]
            for field, value in changes:
                with self.subTest(kind=kind, field=field):
                    values = dict(valid.__dict__)
                    values.pop('evidence_digest')
                    values[field] = value
                    proof = ClosureEvidence(**values, evidence_digest=digest(values))
                    evidence_id = kind + '-' + field
                    self.world.closures[(evidence_id,)] = proof
                    denied = self.gate.soltar(evidence_id, key, [evidence_id], self.auth)
                    self.assertEqual(denied['exitCode'], 1)
                    self.assertIn('unix-scope-unconfirmed', self.codes(denied))
                    self.assertEqual(self.view()['budget']['retained']['slots'], 1)
                    self.assertEqual({r['name'] for r in self.view()['resources']}, set(resources))
        valid = ClosureEvidence.fixture(key, self.world.boot)
        self.world.closures[('bad-digest',)] = replace(valid, issuer='other-harness')
        self.assertEqual(self.gate.soltar('bad-digest', key, ['bad-digest'], self.auth)['exitCode'], 1)
        self.assertEqual(self.view()['budget']['retained']['slots'], 1)
        self.assertEqual(self.release(key)['exitCode'], 0)

    def test_pending_deadline_expires_before_heartbeat(self):
        from dataclasses import replace
        self.config = replace(self.config, heartbeat_ttl=100, pending_ttl=20)
        self.gate = Hostgate(self.config, self.world)
        pending = self.gate.prepare(request(), self.auth)
        self.world.now += 20
        row = next(p for p in self.view()['pending'] if p['id'] == pending)
        self.assertLess(self.world.now - row['heartbeat'], self.config.heartbeat_ttl)
        self.assertEqual(row['effectiveState'], 'expired')
        self.assertEqual(self.codes(row), {'pending-expired'})
        denied = self.reserve()
        self.assertFalse(denied['wouldGrant'])
        self.assertEqual(self.codes(denied), {'pending-expired'})
        self.assertEqual(self.gate.store.read()['pending'][0]['state'], 'expired')
        self.assertEqual(self.view()['budget']['retained']['slots'], 0)
