import io
import json
import sqlite3
from dataclasses import replace
from unittest.mock import patch

from hostgate.cli import main
from hostgate.protocol import reason
from hostgate.store import WriteUncertain
from hostgate.tests.support.world import request
from hostgate.tests.test_contract import Harness


class ProviderFailure(Harness):
    def serialization_case(self, route, kind, source, stage=None):
        self.setUp()
        granted = self.reserve('reserve-a', 'job-a',
            exclusiveResources=['sen-slot:1', 'postgres:5434'])
        self.assertTrue(granted['wouldGrant'])
        other = self.world.auth(pid=222)
        payload = request('invalid-b', 'job-b')
        key = dict(granted['simulationKey'], reservationId='missing')
        if route == 'pending-conflict':
            self.gate.prepare(payload, other)
        if route == 'foreign':
            key = granted['simulationKey']
        before = self.gate.store.read()
        invalid = b'physical-1' if kind == 'bytes' else float(kind)
        healthy = other
        if source == 'context':
            other = replace(other, anchor=replace(other.anchor, physical_boot=invalid))
        self.world.processes[self.auth.peer.pid] = 'EPERM'
        observed = []
        project = self.gate._persist_effective
        reply = self.gate._reply
        contaminated = []

        def capture(view):
            projection = project(view)
            observed.append(projection['reservations'][0]['effectiveState'])
            return projection

        def provider_reply(*args, **kwargs):
            result = reply(*args, **kwargs)
            if source == 'result' and not contaminated:
                # Sólo el resultado (no la vista); el siguiente reply es el del fallo.
                result['providerDetail'] = invalid
                contaminated.append(True)
            return result

        self.world.fault = stage
        with patch.object(self.gate, '_persist_effective', side_effect=capture), \
                patch.object(self.gate, '_reply', side_effect=provider_reply):
            if route == 'prepare':
                expected_error = ((WriteUncertain if stage == 'after_commit' else OSError)
                                  if stage else (TypeError if kind == 'bytes' else ValueError))
                with self.assertRaises(expected_error):
                    self.gate.prepare(payload, other)
                failed = None
            else:
                if route == 'reservar':
                    failed = self.gate.reservar(payload, other)
                elif route == 'soltar':
                    failed = self.gate.soltar('invalid-b', key, [], other)
                elif route == 'dudosa':
                    failed = self.gate.dudosa('invalid-b', key, 'fixture', [], other)
                else:
                    failed = self.gate.latido('invalid-b', key, 1, other)
                self.assertEqual(failed['exitCode'], 3 if stage else 6,
                                 'SEN3952_R1_CONFIRMACION' if stage else 'SEN3952_R1_PROVEEDOR')
        self.world.fault = None
        stored = self.gate.store.read()
        operations, audit = self.records('invalid-b', preparation=route == 'prepare')
        self.assertTrue(observed)
        self.assertEqual(set(observed), {'uncertain'})
        self.assertEqual(operations, 0)
        if stage == 'before_commit':
            self.assertEqual(stored, before)
            self.assertEqual(audit, [])
            return

        self.world.processes[self.auth.peer.pid] = self.auth.peer
        follower = self.gate.reservar(request('follower-c', 'job-c'), healthy)
        self.assertFalse(follower['wouldGrant'], 'SEN3952_R1_SEGUIDOR_SIN_RECONCILIACION')
        self.assertEqual(self.codes(follower), {'reservation-uncertain'})
        expected = dict(before['reservations'][0], state='uncertain', reasons=[
            reason('identity-unverifiable', 'supervisor no verificable o ausente')])
        self.assertEqual(stored, dict(before, reservations=[expected]))
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]['exitCode'], 6)
        self.assertFalse(audit[0]['wouldGrant'])
        self.assertEqual(self.codes(audit[0]), {'store-unavailable'})
        if failed is not None and not stage:
            self.assertEqual(audit, [failed])
        recovered = self.gate.estado({}, healthy)
        self.assertEqual(recovered['budget']['retained'], expected['demand'])
        self.assertEqual(recovered['budget']['retainedByState'], {'uncertain': 1})
        self.assertEqual(recovered['resources'], [
            dict(name=name, holder=granted['simulationKey'], state='uncertain')
            for name in expected['exclusiveResources']])
        # El id de B sigue disponible con un proveedor sano.
        retry = self.gate.latido('invalid-b', key, 1, healthy)
        self.assertNotIn('replayed', retry)
        self.assertEqual(retry['exitCode'], 5 if route == 'foreign' else
                         2 if route == 'pending-conflict' else 1)

    def test_sen3952_r1_context_keeps_doubt_after_owner_recovers(self):
        for route in ('latido', 'soltar', 'dudosa', 'reservar', 'prepare'):
            for kind in ('nan', 'inf', 'bytes'):
                with self.subTest(route=route, value=kind):
                    self.serialization_case(route, kind, 'context')

    def test_sen3952_r1_result_keeps_doubt_after_owner_recovers(self):
        for route in ('latido', 'soltar', 'dudosa', 'pending-conflict', 'foreign'):
            for kind in ('nan', 'inf', 'bytes'):
                with self.subTest(route=route, value=kind):
                    self.serialization_case(route, kind, 'result')

    def test_sen3952_r1_confirmation_failure_is_uncertain(self):
        for source, routes in (('context', ('latido', 'soltar', 'dudosa', 'reservar', 'prepare')),
                               ('result', ('latido', 'pending-conflict', 'foreign'))):
            for route in routes:
                for kind in ('nan', 'inf', 'bytes'):
                    for stage in ('before_commit', 'after_commit'):
                        with self.subTest(source=source, route=route, value=kind, stage=stage):
                            self.serialization_case(route, kind, source, stage)

    def invalid_provider(self, kind):
        # La primera lectura proyecta A válidamente; la segunda contamina sólo B.
        attribute = 'physical_boot' if kind == 'bytes' else 'monotonic'
        valid = self.world.boot if kind == 'bytes' else self.world.now
        invalid = b'physical-1' if kind == 'bytes' else float(kind)
        reads = []

        def provider():
            reads.append(None)
            return invalid if len(reads) == 2 else valid

        return patch.object(self.world, attribute, side_effect=provider)

    def call(self, payload, auth, msg=None):
        output = io.StringIO()
        code = main([payload['operation']], gate=self.gate, auth=auth,
                    stdin=io.StringIO(json.dumps(payload)), stdout=output, stderr=io.StringIO())
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1, msg)
        result = json.loads(lines[0])
        self.assertEqual(result['exitCode'], code, msg)
        return code, result

    def records(self, operation_id, preparation=False):
        principal = self.auth.principal + ('\x00preparation' if preparation else '')
        with sqlite3.connect(str(self.gate.store.path)) as conn:
            operations = conn.execute(
                'SELECT count(*) FROM operations WHERE principal=? AND operation_id=?',
                (principal, operation_id)).fetchone()[0]
            audit = conn.execute(
                'SELECT payload FROM audit WHERE principal=? AND operation_id=? ORDER BY seq',
                (principal, operation_id)).fetchall()
        return operations, [json.loads(row[0]) for row in audit]

    def test_sen3900_profile_failure_keeps_doubt_after_owner_recovers(self):
        granted = self.reserve('reserve-a', 'job-a',
                               exclusiveResources=['sen-slot:1', 'postgres:5434'])
        self.assertTrue(granted['wouldGrant'])
        before = self.gate.store.read()
        other = self.world.auth(pid=222)
        failed_request = request('reserve-b', 'job-b',
                                 exclusiveResources=['sen-slot:2', 'postgres:5435'])
        self.world.processes[self.auth.peer.pid] = 'EPERM'
        with patch.object(self.world, 'profile_verified', side_effect=PermissionError('profile')) as provider:
            code, failed = self.call(failed_request, other)
        provider.assert_called_once_with('fixture')
        self.assertEqual(code, 6)
        self.assertFalse(failed['wouldGrant'])
        self.assertEqual(self.codes(failed), {'store-unavailable'})
        stored = self.gate.store.read()
        records = self.records('reserve-b')

        self.world.processes[self.auth.peer.pid] = self.auth.peer
        recovered = self.gate.estado({}, other)
        follower_request = request('follower', 'follower',
                                   exclusiveResources=['sen-slot:2', 'postgres:5435'])
        follower_code, follower = self.call(follower_request, other)
        self.assertFalse(follower['wouldGrant'], 'SEN3900_SEGUIDOR_SIN_RECONCILIACION')
        self.assertEqual(follower_code, 1)
        self.assertEqual(self.codes(follower), {'reservation-uncertain'})

        expected = dict(before['reservations'][0], state='uncertain', reasons=[
            reason('identity-unverifiable', 'supervisor no verificable o ausente')])
        self.assertEqual(stored, dict(before, reservations=[expected]))
        self.assertEqual(records, (0, [failed]))
        retained = dict(cpuMilli=1000, memoryBytes=1000, diskBytes={'data': 1000}, slots=1)
        for observed in (recovered, self.gate.estado({}, other)):
            self.assertEqual(observed['budget'], dict(
                capacity=dict(self.config.budget), retained=retained, available=retained,
                retainedByState={'uncertain': 1}))
            self.assertEqual(observed['resources'], [
                dict(name=name, holder=granted['simulationKey'], state='uncertain')
                for name in ('sen-slot:1', 'postgres:5434')])
            self.assertEqual(len(observed['reservations']), 1)
            self.assertEqual(observed['reservations'][0]['state'], 'uncertain')
            self.assertEqual(observed['reservations'][0]['effectiveState'], 'uncertain')

        retry_code, retried = self.call(failed_request, other)
        self.assertEqual(retry_code, 1)
        self.assertEqual(self.codes(retried), {'reservation-uncertain'})
        self.assertNotIn('replayed', retried)
        self.assertEqual(self.records('reserve-b'), (1, [failed, retried]))
        self.assertEqual(self.gate.store.read()['reservations'], [expected])

    def test_sen3952_unserializable_provider_keeps_doubt_after_owner_recovers(self):
        for route in ('reservar', 'prepare'):
            for kind in ('nan', 'inf', 'bytes'):
                with self.subTest(route=route, value=kind):
                    self.setUp()
                    granted = self.reserve('reserve-a', 'job-a',
                        exclusiveResources=['sen-slot:1', 'postgres:5434'])
                    self.assertTrue(granted['wouldGrant'])
                    before = self.gate.store.read()
                    other = self.world.auth(pid=222)
                    payload = request('invalid-b', 'job-b',
                        exclusiveResources=['sen-slot:2', 'postgres:5435'])
                    self.world.processes[self.auth.peer.pid] = 'EPERM'
                    with self.invalid_provider(kind) as provider:
                        if route == 'prepare':
                            with self.assertRaises(TypeError if kind == 'bytes' else ValueError) as caught:
                                self.gate.prepare(payload, other)
                            self.assertIn('JSON', str(caught.exception))
                        else:
                            code, failed = self.call(payload, other)
                            self.assertEqual(code, 6)
                            self.assertFalse(failed['wouldGrant'])
                            self.assertEqual(self.codes(failed), {'store-unavailable'})
                    self.assertGreaterEqual(provider.call_count, 2)
                    stored = self.gate.store.read()
                    operations, audit = self.records('invalid-b', preparation=route == 'prepare')

                    self.world.processes[self.auth.peer.pid] = self.auth.peer
                    recovered = self.gate.estado({}, other)
                    follower_code, follower = self.call(request('follower-c', 'job-c',
                        exclusiveResources=['sen-slot:2', 'postgres:5435']), other)
                    self.assertFalse(follower['wouldGrant'], 'SEN3952_SEGUIDOR_SIN_RECONCILIACION')
                    self.assertEqual(follower_code, 1)
                    self.assertEqual(self.codes(follower), {'reservation-uncertain'})
                    expected = dict(before['reservations'][0], state='uncertain', reasons=[
                        reason('identity-unverifiable', 'supervisor no verificable o ausente')])
                    self.assertEqual(stored, dict(before, reservations=[expected]))
                    self.assertEqual(operations, 0)
                    self.assertEqual(len(audit), 1)
                    self.assertEqual(audit[0]['operationId'], 'invalid-b')
                    self.assertEqual(audit[0]['exitCode'], 6)
                    self.assertFalse(audit[0]['wouldGrant'])
                    self.assertEqual(self.codes(audit[0]), {'store-unavailable'})
                    if route == 'reservar':
                        self.assertEqual(audit, [failed])
                    else:
                        self.assertEqual(self.records('invalid-b'), (0, []))
                    retained = before['reservations'][0]['demand']
                    for observed in (recovered, self.gate.estado({}, other)):
                        self.assertEqual(observed['budget'], dict(
                            capacity=dict(self.config.budget), retained=retained, available=retained,
                            retainedByState={'uncertain': 1}))
                        self.assertEqual(observed['resources'], [
                            dict(name=name, holder=granted['simulationKey'], state='uncertain')
                            for name in ('sen-slot:1', 'postgres:5434')])
                        self.assertEqual(len(observed['reservations']), 1)
                        self.assertEqual(observed['reservations'][0]['state'], 'uncertain')
                    if route == 'prepare':
                        pending_id = self.gate.prepare(payload, other)
                        self.assertEqual(self.records('invalid-b', preparation=True),
                                         (1, audit + [{'pendingId': pending_id}]))
                    else:
                        retry_code, retried = self.call(payload, other)
                        self.assertEqual(retry_code, 1)
                        self.assertEqual(self.codes(retried), {'reservation-uncertain'})
                        self.assertNotIn('replayed', retried)
                        self.assertEqual(self.records('invalid-b'), (1, audit + [retried]))
                    self.assertEqual(self.gate.store.read()['reservations'], [expected])

    def test_sen3952_unserializable_provider_confirmation_failure_is_uncertain(self):
        for route in ('reservar', 'prepare'):
            for kind in ('nan', 'inf', 'bytes'):
                for stage in ('before_commit', 'after_commit'):
                    with self.subTest(route=route, value=kind, stage=stage):
                        self.setUp()
                        self.reserve('reserve-a', 'job-a',
                                     exclusiveResources=['sen-slot:1', 'postgres:5434'])
                        before = self.gate.store.read()
                        other = self.world.auth(pid=222)
                        self.world.processes[self.auth.peer.pid] = 'EPERM'
                        self.world.fault = stage
                        with self.invalid_provider(kind):
                            if route == 'prepare':
                                with self.assertRaises(WriteUncertain if stage == 'after_commit' else OSError):
                                    self.gate.prepare(request('invalid-b', 'job-b'), other)
                            else:
                                code, failed = self.call(request('invalid-b', 'job-b'), other)
                                self.assertEqual(code, 3, 'SEN3952_CONFIRMACION_INCIERTA')
                                self.assertFalse(failed['wouldGrant'])
                        self.world.fault = None
                        operations, audit = self.records('invalid-b', preparation=route == 'prepare')
                        self.assertEqual(operations, 0)
                        stored = self.gate.store.read()
                        if stage == 'before_commit':
                            self.assertEqual(stored, before)
                            self.assertEqual(audit, [])
                        else:
                            expected = dict(before['reservations'][0], state='uncertain', reasons=[
                                reason('identity-unverifiable', 'supervisor no verificable o ausente')])
                            self.assertEqual(stored, dict(before, reservations=[expected]))
                            self.assertEqual(len(audit), 1)
                            self.assertEqual(audit[0]['exitCode'], 6)
                            self.assertFalse(audit[0]['wouldGrant'])

    def test_sen3900_closure_failure_keeps_projection_without_releasing(self):
        self.reserve('reserve-a', 'job-a', exclusiveResources=['sen-slot:1'])
        other = self.world.auth(pid=222)
        key = self.key(self.reserve('reserve-b', 'job-b', auth=other,
                                    exclusiveResources=['sen-slot:2', 'postgres:5435']))
        before = self.gate.store.read()
        self.world.processes[self.auth.peer.pid] = 'EPERM'
        payload = dict(schema=request()['schema'], operationId='failed-release',
                       operation='soltar', key=key, evidenceIds=['proof'])
        observe = self.gate._observe

        def reordered(view):
            projection = observe(view)
            for kind in ('pending', 'reservations'):
                projection[kind].reverse()
            return projection

        with patch.object(self.gate, '_observe', side_effect=reordered), \
                patch.object(self.world, 'closure', side_effect=RuntimeError('closure')):
            code, failed = self.call(payload, other)
        self.assertEqual(code, 6)
        stored = self.gate.store.read()
        self.assertEqual(stored['reservations'][0]['state'], 'uncertain',
                         'SEN3900_CIERRE_CONSERVA_DUDA')
        self.assertEqual(stored['reservations'][1], before['reservations'][1])
        self.assertEqual(stored['pending'], before['pending'])
        self.assertEqual(stored['generations'], before['generations'])
        self.assertEqual(self.records('failed-release'), (0, [failed]))
        self.world.processes[self.auth.peer.pid] = self.auth.peer
        observed = self.gate.estado({}, other)
        self.assertEqual(observed['budget']['retained'], dict(self.config.budget))
        self.assertEqual(observed['budget']['retainedByState'], {'uncertain': 1, 'reserved': 1})
        self.assertEqual({r['name'] for r in observed['resources']},
                         {'sen-slot:1', 'sen-slot:2', 'postgres:5435'})

    def test_sen3900_late_identity_failure_keeps_revalidated_doubt_and_drain(self):
        self.reserve('reserve-a', 'job-a', exclusiveResources=['sen-slot:1'])
        other = self.world.auth(pid=222)
        absent_id = self.gate.prepare(request('prior-preparation', 'prior-job'), self.world.auth(pid=333))
        before = self.gate.store.read()
        decision = self.gate._decision
        verify = self.world.verify
        peer_checks = []

        def profile(profile_id):
            self.world.processes[self.auth.peer.pid] = 'EPERM'
            self.world.boot = 'physical-2'
            return True

        def identity(peer):
            if peer == other.peer:
                peer_checks.append(peer)
                if len(peer_checks) == 3:
                    raise RuntimeError('identity after revalidation')
            return verify(peer)

        def reordered_decision(payload, view, pending, *project):
            view['pending'] = [pending] + [p for p in view['pending']
                                          if p['id'] not in (pending['id'], absent_id)]
            return decision(payload, view, pending, *project)

        with patch.object(self.gate, '_decision', side_effect=reordered_decision), \
                patch.object(self.world, 'profile_verified', side_effect=profile), \
                patch.object(self.world, 'verify', side_effect=identity):
            code, failed = self.call(request('late-failure', 'job-b'), other)
        self.assertEqual(len(peer_checks), 3)
        self.assertEqual(code, 6)
        stored = self.gate.store.read()
        self.assertTrue(stored['drained'], 'SEN3900_REVALIDACION_CONSERVA_DRENADO')
        self.assertEqual(stored['reservations'][0]['state'], 'uncertain')
        self.assertEqual(self.codes(stored['reservations'][0]),
                         {'identity-unverifiable', 'boot-unverified'})
        self.assertEqual(stored['reservations'][0]['demand'], before['reservations'][0]['demand'])
        self.assertEqual(stored['reservations'][0]['exclusiveResources'], ['sen-slot:1'])
        self.assertEqual(stored['pending'], before['pending'])
        self.assertEqual(stored['generations'], before['generations'])
        self.assertEqual(self.records('late-failure'), (0, [failed]))

    def test_sen3900_projection_write_and_commit_failures_remain_uncertain(self):
        for stage in ('write', 'audit', 'before_commit', 'commit', 'after_commit', 'close'):
            self.setUp()
            self.reserve('reserve-a', 'job-a', exclusiveResources=['sen-slot:1'])
            other = self.world.auth(pid=222)
            before = self.gate.store.read()
            self.world.processes[self.auth.peer.pid] = 'EPERM'
            store = self.gate.store
            connect = store.connect
            op = 'failed-' + stage
            self.assertEqual(before['reservations'][0]['state'], 'reserved', stage)

            class Connection:
                def __init__(self):
                    self.conn = connect(writable=True)

                def execute(self, sql, *args):
                    result = self.conn.execute(sql, *args)
                    if ((stage == 'write' and sql.startswith('UPDATE host_view'))
                            or (stage == 'audit' and sql.startswith('INSERT INTO audit'))):
                        raise sqlite3.OperationalError('disk I/O error')
                    return result

                def commit(self):
                    self.conn.commit()
                    if stage == 'commit':
                        raise PermissionError('commit ACK lost')

                def rollback(self):
                    self.conn.rollback()

                def close(self):
                    self.conn.close()
                    if stage == 'close':
                        raise RuntimeError('close after commit')

            def checkpoint(name):
                if name == stage and stage in ('before_commit', 'after_commit'):
                    raise OSError('commit boundary')

            with patch.object(store, 'connect', side_effect=lambda **kwargs: Connection()), \
                    patch.object(self.world, 'checkpoint', side_effect=checkpoint), \
                    patch.object(self.world, 'profile_verified', side_effect=OSError('profile')):
                code, failed = self.call(request(op, 'job-b'), other, msg=stage)
            self.assertEqual(code, 3, 'SEN3900_CONFIRMACION_INCIERTA: ' + stage)
            self.assertFalse(failed['wouldGrant'], stage)
            operations, audit = self.records(op)
            self.assertEqual(operations, 0, stage)
            stored = store.read()
            if stage in ('write', 'audit', 'before_commit'):
                self.assertEqual(stored, before, stage)
                self.assertEqual(audit, [], stage)
            else:
                self.assertEqual(stored['reservations'][0]['state'], 'uncertain', stage)
                self.assertEqual(stored['reservations'][0]['demand'], before['reservations'][0]['demand'], stage)
                self.assertEqual(stored['pending'], before['pending'], stage)
                self.assertEqual(len(audit), 1, stage)
                self.assertEqual(audit[0]['exitCode'], 6, stage)
                self.assertFalse(audit[0]['wouldGrant'], stage)

    def test_sen3900_prepare_conflict_is_audited_without_replacing_original(self):
        payload = request('prepare-conflict', 'job-a')
        pending_id = self.gate.prepare(payload, self.auth)
        before = self.gate.store.read()
        with sqlite3.connect(str(self.gate.store.path)) as conn:
            original = conn.execute('SELECT * FROM operations').fetchall()

        with self.assertRaisesRegex(ValueError, '^operation-conflict$'):
            self.gate.prepare(dict(payload, attemptId='a2'), self.auth)

        operations, audit = self.records(payload['operationId'], preparation=True)
        self.assertEqual([(entry['exitCode'], self.codes(entry), entry['wouldGrant'])
                          for entry in audit[1:]], [(2, {'operation-conflict'}, False)],
                         'SEN3900_PREPARE_CONFLICTO_CLASIFICADO')
        self.assertEqual(operations, 1)
        self.assertEqual(audit[0], {'pendingId': pending_id})
        self.assertEqual(audit[1]['operationId'], payload['operationId'])
        self.assertEqual(self.gate.store.read(), before)
        self.assertEqual([pending['id'] for pending in before['pending']], [pending_id])
        with sqlite3.connect(str(self.gate.store.path)) as conn:
            self.assertEqual(conn.execute('SELECT * FROM operations').fetchall(), original)

    def test_sen3900_prepare_failure_keeps_doubt_after_owner_recovers(self):
        granted = self.reserve('reserve-a', 'job-a',
                               exclusiveResources=['sen-slot:1', 'postgres:5434'])
        self.assertTrue(granted['wouldGrant'])
        before = self.gate.store.read()
        other = self.world.auth(pid=222)
        payload = request('prepare-b', 'job-b',
                          exclusiveResources=['sen-slot:2', 'postgres:5435'])
        self.world.processes[self.auth.peer.pid] = 'EPERM'
        failure = RuntimeError('prepare clock')
        with patch.object(self.world, 'monotonic', side_effect=[self.world.now, failure]) as clock:
            with self.assertRaisesRegex(RuntimeError, '^prepare clock$') as caught:
                self.gate.prepare(payload, other)
        self.assertIs(caught.exception, failure)
        self.assertEqual(clock.call_count, 2)
        stored = self.gate.store.read()
        records = self.records('prepare-b', preparation=True)

        self.world.processes[self.auth.peer.pid] = self.auth.peer
        follower_code, follower = self.call(request('follower', 'follower',
            exclusiveResources=['sen-slot:2', 'postgres:5435']), other)
        self.assertFalse(follower['wouldGrant'], 'SEN3900_PREPARE_SIN_RECONCILIACION')
        self.assertEqual(follower_code, 1)
        self.assertEqual(self.codes(follower), {'reservation-uncertain'})
        expected = dict(before['reservations'][0], state='uncertain', reasons=[
            reason('identity-unverifiable', 'supervisor no verificable o ausente')])
        self.assertEqual(stored, dict(before, reservations=[expected]))
        operations, audit = records
        self.assertEqual(operations, 0)
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]['operationId'], 'prepare-b')
        self.assertEqual(audit[0]['exitCode'], 6)
        self.assertFalse(audit[0]['wouldGrant'])
        self.assertEqual(self.codes(audit[0]), {'store-unavailable'})
        self.assertEqual(self.records('prepare-b'), (0, []))
        observed = self.gate.estado({}, other)
        self.assertEqual(observed['budget']['retainedByState'], {'uncertain': 1})
        self.assertEqual(observed['budget']['retained'], before['reservations'][0]['demand'])
        self.assertEqual(observed['resources'], [
            dict(name=name, holder=granted['simulationKey'], state='uncertain')
            for name in ('sen-slot:1', 'postgres:5434')])
        pending_id = self.gate.prepare(payload, other)
        self.assertEqual(self.gate.store.read()['pending'][-1]['id'], pending_id)
        self.assertEqual(self.records('prepare-b', preparation=True), (1, [audit[0], {'pendingId': pending_id}]))
