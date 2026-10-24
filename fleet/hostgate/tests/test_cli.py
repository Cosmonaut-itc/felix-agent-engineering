import io
import json
import os
import subprocess
import sys
from hostgate.cli import main
from hostgate.tests.test_contract import Harness
from hostgate.tests.support.world import request


class CLI(Harness):
    def call(self, payload, command='reservar'):
        out, err = io.StringIO(), io.StringIO()
        code = main([command], gate=self.gate, auth=self.auth,
                    stdin=io.StringIO(payload), stdout=out, stderr=err)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        return code, json.loads(lines[0])

    def test_public_cli_harness_and_strict_json(self):
        code, result = self.call(json.dumps(request()))
        self.assertEqual(code, 0)
        self.assertEqual(result['result'], 'shadow')
        for raw in ('{}', 'null', '{"a":1,"a":2}', '{"x":NaN}', '[]', 'x' * 65537,
                    json.dumps(dict(request(), pid=1)), json.dumps(dict(request(), ttl=200)),
                    json.dumps(dict(request(), capacity=200)), json.dumps(dict(request(), stateDir='/tmp'))):
            self.assertEqual(self.call(raw)[0], 2)

    def test_demand_and_nul_validation(self):
        for value in (True, -1, 1.5, float('inf'), None):
            r = request(); r['demand']['cpuMilli'] = value
            self.assertEqual(self.call(json.dumps(r))[0], 2)
        self.assertEqual(self.call(json.dumps(dict(request(), jobId='a\x00b')))[0], 2)
        r = request(); r['demand']['diskBytes']['unknown'] = 1
        self.assertEqual(self.call(json.dumps(r))[0], 1)

    def test_owner_payload_cannot_construct_auth(self):
        proc = subprocess.run([sys.executable, '-B', '-m', 'hostgate', 'reservar'],
                              input=json.dumps(request()), text=True, capture_output=True,
                              timeout=10)
        self.assertEqual(proc.returncode, 5, proc.stderr)
        self.assertFalse(json.loads(proc.stdout)['wouldGrant'])
        self.assertNotIn('granted', proc.stdout)
        self.assertEqual(self.call(json.dumps(dict(request(), auth={'role': 'operator'})))[0], 2)

    def test_cli_all_commands_and_exit_mapping(self):
        r = self.reserve(); key = self.key(r)
        for op, extra in [('latido', {'seq': 1}), ('dudosa', {'reason': 'pause', 'evidenceIds': []}),
                          ('soltar', {'evidenceIds': []})]:
            payload = dict(schema=request()['schema'], operationId=op, operation=op, key=key, **extra)
            code, reply = self.call(json.dumps(payload), op)
            self.assertEqual(code, 1 if op == 'soltar' else 0)
            self.assertEqual(reply['result'], 'shadow')
        code, reply = self.call(json.dumps(dict(schema=request()['schema'], operationId='state', operation='estado')), 'estado')
        self.assertEqual(code, 0)
        self.assertIn('reservations', reply)

    def test_malformed_provider_keeps_json_envelope_and_all_resources(self):
        from dataclasses import replace
        from hostgate.protocol import digest
        from hostgate.scope import ClosureEvidence
        key = self.key(self.reserve(exclusiveResources=['sen-slot:1', 'postgres:5434']))
        valid = ClosureEvidence.fixture(key, self.world.boot)
        for field, value in (('kind', None), ('kind', []), ('complete', 'true'),
                             ('resources_clean', 1), ('issuer', []), ('key', [])):
            proof = replace(valid, **{field: value})
            values = dict(proof.__dict__); values.pop('evidence_digest')
            self.world.closures[('malformed',)] = replace(proof, evidence_digest=digest(values))
            payload = dict(schema=request()['schema'], operationId='malformed-' + field + str(value),
                           operation='soltar', key=key, evidenceIds=['malformed'])
            code, result = self.call(json.dumps(payload), 'soltar')
            self.assertEqual(code, 1)
            self.assertEqual(result['result'], 'shadow')
            self.assertEqual(self.view()['budget']['retained']['slots'], 1)
            self.assertEqual(len(self.view()['resources']), 2)

    def test_heartbeat_sequence_replay_and_different_live_peer(self):
        from dataclasses import replace
        key = self.key(self.reserve())
        def heartbeat(op, seq):
            return self.call(json.dumps(dict(schema=request()['schema'], operationId=op,
                             operation='latido', key=key, seq=seq)), 'latido')
        self.assertEqual(heartbeat('h1', 1)[0], 0)
        self.world.now += 5
        code, replay = heartbeat('h1', 1)
        self.assertEqual(code, 0)
        self.assertTrue(replay['replayed'])
        self.assertEqual(self.view()['reservations'][0]['heartbeatAgeSeconds'], 5)
        self.assertEqual(heartbeat('h2', 1)[0], 1)
        owner = self.auth
        self.auth = replace(owner, role='query')
        self.assertEqual(heartbeat('h3', 2)[0], 5)
        self.auth = self.world.auth(pid=444)
        self.assertEqual(heartbeat('h1', 1)[0], 5)
        self.assertEqual(heartbeat('h4', 2)[0], 5)
        self.auth = owner
        self.assertEqual(heartbeat('h5', 2)[0], 0)
