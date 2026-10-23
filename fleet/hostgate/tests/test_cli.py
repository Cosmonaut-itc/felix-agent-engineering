import io
import json
import os
import subprocess
import sys
from hostgate.cli import main
from hostgate.tests.test_contract import Contract
from hostgate.tests.support.world import request


class CLI(Contract):
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
            self.assertIn(code, (0, 1))
            self.assertEqual(reply['result'], 'shadow')
        code, reply = self.call(json.dumps(dict(schema=request()['schema'], operationId='state', operation='estado')), 'estado')
        self.assertEqual(code, 0)
        self.assertIn('reservations', reply)
