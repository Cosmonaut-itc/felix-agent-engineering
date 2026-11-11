"""SEN-5649 3a: positivos y negativos contra el endpoint efectivo privado."""
import copy
from contextlib import closing
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import patch

from hostgate import admin
from hostgate.client import HostgateClient
from hostgate.protocol import canonical, digest
from hostgate.tests import test_ipc
from hostgate.tests.support.effective import EffectiveFixture
from hostgate.tests.support.world import request
from hostgate.wire import TransportError


class EffectiveHostgate(test_ipc.IPCHarness):
    def setUp(self):
        with patch.object(test_ipc, 'Fixture', EffectiveFixture): super().setUp()
        from hostgate.store import store_for
        self.cfg = admin.load_config(self.f.locator)
        self.store = store_for(self.cfg.node)
        # SEN-5678: mantener WAL y SHM incluso con el SQLite de Apple.
        self.writer = self.f.writer
        self.addCleanup(self.writer.close)
        self.empty = self.store.read()
        self.r, self.s, self.call = self.pair()
        self.original = request(physicalHostId='fixture-host', exclusiveResources=['sen-slot:1'])
        self.grant = self.call(self.original)
        self.assertIs(self.grant['granted'], True, self.grant)
        self.key = self.grant['reservationKey']
        self.assertEqual(self.key['generation'], 1)
        self.assertTrue(self.key['reservationId'].startswith('e_'))

    def spawn(self, role, env=None, root=None, module='hostgate.tests.support.effective'):
        proc = subprocess.Popen([sys.executable, '-B', '-m', module, role, str(root or self.root)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        def cleanup():
            if proc.poll() is None: proc.terminate()
            try: proc.communicate(timeout=8)
            except subprocess.TimeoutExpired: proc.kill(); proc.communicate(timeout=8)
        self.addCleanup(cleanup)
        return proc

    def client(self, mode='effective'):
        with patch.object(admin, 'local_authority', return_value=self.f.locator):
            client = HostgateClient.connect(mode=mode)
        self.addCleanup(client.close)
        return client

    def snapshot(self, root=None):
        # Incluye sidecars, inodes y metadatos: los rechazos no deben abrir un escritor.
        root = root or self.root
        return {str(p.relative_to(root)): (p.stat().st_ino, p.stat().st_mode, p.stat().st_mtime_ns,
                hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
                for p in [root] + list(root.rglob('*'))}

    def test_empty_store_wire_replay_and_budget_rejection(self):
        from hostgate.protocol import validate_result
        self.assertEqual(self.empty, dict(pending=[], reservations=[], generations={}, drained=False))
        self.assertEqual(self.writer.execute('SELECT * FROM metadata').fetchall(),
                         [(self.cfg.node.store_id, 2, 'effective')])
        for field in ('wouldGrant', 'simulationKey', 'simulationState'):
            self.assertNotIn(field, self.grant)
            with self.assertRaises(ValueError):
                validate_result(dict(self.grant, **{field: self.key}), 'effective', self.cfg.node.store_id)
        replay = self.call(self.original)
        self.assertIs(replay['replayed'], True)
        self.assertIs(replay['granted'], False)
        self.assertEqual(replay['originalResult'], self.grant)
        self.assertEqual(replay['reservationKey'], self.key)
        self.assertEqual(replay['currentState'], dict(state='reserved', reasons=[]))
        self.assertEqual(len(self.store.read()['reservations']), 1)
        self.assertEqual(self.store.read()['generations'], {'job': 1})
        self.assertEqual(self.writer.execute("SELECT count(*) FROM operations WHERE operation_id='op'").fetchone()[0], 1)
        _, _, other = self.pair('second', 'other')
        denied = other(dict(self.original, operationId='second', executorId='other'))
        self.assertIs(denied['granted'], False)
        self.assertIn('budget-exceeded', {r['code'] for r in denied['reasons']})
        self.assertNotIn('reservationKey', denied)
        print('EFFECTIVE_EXAMPLES', canonical(dict(grant=self.grant, rejection=denied, replay=replay)), flush=True)
        state = self.client().call(test_ipc.query(), timeout=2)
        self.assertEqual(state['pending'][0]['state'], 'granted')
        self.assertEqual(state['budget']['retained']['slots'], 1)
        for value in (self.grant, replay, denied, state):
            self.assertEqual(value['result'], 'effective')
            self.assertEqual(value['mode'], 'effective')
            self.assertEqual(value['storeId'], self.cfg.node.store_id)
            self.assertNotIn('simulation', canonical(value).lower())

    def test_no_promotion_copy_rename_kind_or_existing_directory(self):
        from hostgate.store import ShadowStore, EffectiveStore, StoreError
        before = self.snapshot()
        with self.assertRaises(StoreError): EffectiveStore.initialize(self.cfg.node)
        self.assertEqual(before, self.snapshot())
        for attack in ('copy', 'rename', 'metadata', 'kind'):
            with self.subTest(attack=attack):
                folder = self.root / attack
                cfg = replace(self.cfg.node, state_dir=folder, store_id='new-' + attack)
                if attack == 'kind':
                    EffectiveStore.initialize(cfg)
                    writer = EffectiveStore(cfg).connect(writable=True)
                    writer.execute("UPDATE metadata SET kind='shadow'")
                else:
                    shadow = replace(cfg, mode='shadow')
                    ShadowStore.initialize(shadow)
                    writer = ShadowStore(shadow).connect(writable=True)
                    if attack == 'metadata': writer.execute("UPDATE metadata SET kind='effective'")
                    if attack == 'copy': shutil.copy2(folder / 'shadow.db', folder / 'effective.db')
                    else: (folder / 'shadow.db').rename(folder / 'effective.db')
                try:
                    # Fija una lectura actual: otros lectores reutilizan el read-mark
                    # de SHM, además del WAL vivo exigido por SEN-5678.
                    writer.execute('BEGIN')
                    writer.execute('SELECT * FROM metadata').fetchall()
                    before = self.snapshot(folder)
                    with self.assertRaisesRegex(StoreError, 'kind' if attack == 'kind' else 'application_id'):
                        with closing(EffectiveStore(cfg).connect()): pass
                    self.assertEqual(before, self.snapshot(folder))
                finally: writer.close()
        self.assertEqual(self.store.read()['reservations'][0]['key'], self.key)

    def test_mode_anchor_and_client_match_exactly(self):
        from hostgate.store import ShadowStore
        with self.assertRaises(TransportError): self.client(mode='shadow')
        self.assertEqual(self.client().call(test_ipc.query())['reservations'][0]['key'], self.key)
        other = self.root / 'shadow-authority'; other.mkdir(mode=0o700)
        f = test_ipc.Fixture(other); f.provision()
        cfg = admin.load_config(f.locator)
        with closing(ShadowStore(cfg.node).connect(writable=True)):
            server = self.spawn('server', root=other, module='hostgate.tests.test_ipc')
            self.assertEqual(self.line(server).strip(), 'listening')
            previous = Path.cwd()
            try:
                os.chdir(other)
                with patch.object(admin, 'local_authority', return_value=f.locator):
                    with HostgateClient.connect(mode='shadow') as client:
                        self.assertEqual(client.call(test_ipc.query())['result'], 'shadow')
                    with self.assertRaises(TransportError): HostgateClient.connect(mode='effective')
            finally: os.chdir(previous)
            server.terminate(); server.communicate(timeout=8)
            f.doc['mode'] = 'effective'; f.publish()
            before = self.snapshot(other)
            with patch('hostgate.store.EffectiveStore.connect', side_effect=AssertionError('SQLite antes del ancla')):
                with self.assertRaises(admin.AdminError):
                    with admin.AuthorityLease(f.locator, admin.load_config(f.locator)): pass
            self.assertEqual(before, self.snapshot(other))
        # También contrasta el hello recibido con una configuración efectiva válida.
        from hostgate import client as api
        receive = api.receive_json
        def wrong_hello(*args, **kwargs):
            value = receive(*args, **kwargs)
            if value.get('kind') == 'hello': value['mode'] = 'shadow'
            return value
        with patch.object(api, 'receive_json', side_effect=wrong_hello):
            with self.assertRaises(TransportError): self.client()
        for poisoned in (dict(self.grant, simulationKey=self.key), dict(self.grant, configRevision='other')):
            client = self.client()
            with patch.object(api, 'receive_response', return_value=poisoned):
                with self.assertRaises(TransportError): client.call(test_ipc.query('poisoned'))
            self.assertEqual(client.stream.fileno(), -1)
        # 4c: el CLI `estado` toma el modo de la configuración validada, no de flags ni de la key.
        from hostgate import cli
        out = io.StringIO()
        with patch.object(admin, 'local_authority', return_value=self.f.locator):
            self.assertEqual(cli.main(['estado'], stdin=io.StringIO(canonical(test_ipc.query())),
                                      stdout=out, stderr=io.StringIO()), 0)
        facts = json.loads(out.getvalue())
        self.assertEqual((facts['mode'], facts['reservations'][0]['key']), ('effective', self.key))
        self.assertEqual(self.store.read()['reservations'][0]['key'], self.key)

    def test_release_seals_unissued_and_stale_keys_do_not_touch_owner(self):
        for key in (dict(self.key, reservationId='s_old'), dict(self.key, storeId='other-store'),
                    dict(self.key, generation=2)):
            before = copy.deepcopy(self.store.read()['reservations'][0])
            result = self.call(dict(test_ipc.query('stale-' + digest(key)), operation='soltar', key=key, evidenceIds=['fixture']))
            self.assertEqual(result['reasons'][0]['code'], 'stale-generation')
            self.assertEqual(self.store.read()['reservations'][0], before)
        # 4c: una reserva sin permiso se sella con never-created propio; la evidencia del harness no cuenta.
        results = [self.call(dict(test_ipc.query(op), operation='soltar', key=self.key, evidenceIds=['fixture']))
                   for op in ('release', 'release-again')]
        for result in results:
            self.assertEqual((result['reasons'], result['state'], result.get('permit')), ([], 'released', results[0].get('permit')))
        raw = json.loads(self.writer.execute('SELECT payload FROM host_view WHERE id=1').fetchone()[0])
        self.assertEqual(raw['reservations'][0]['state'], 'released')
        row = self.store.read()['reservations'][0]
        self.assertEqual((row['state'], row['reasons'], row['key']), ('released', [], self.key))
        self.assertEqual((row['closureEvidence']['kind'], row['closureEvidence']['provenance']),
                         ('never-created', 'darwin-coalition@1'))
        self.assertEqual(self.client().call(test_ipc.query())['budget']['retained']['slots'], 0)
        replay = self.call(self.original)
        self.assertFalse(replay['granted'])
        self.assertEqual((replay['currentState']['state'], replay['currentState']['permit']['state']), ('released', 'sealed'))

    def creation(self, op, operation, **fields):
        return dict(test_ipc.query(op), operation=operation, key=self.key, **fields)

    def test_creation_operations_travel_by_ipc_and_the_wire_is_exact(self):
        """Variantes: unvalidated_wire, success_without_receipt, lenient_current_state, allowed_outside_reserved, query_fields_in_creation, short_novel, state_without_key."""
        from hostgate import cli, client as api
        launch = dict(argvDigest='a' * 64, inputDigest='b' * 64, bootstrapDigest='c' * 64, launcherDigest='e' * 64)
        confirmation = dict(operationId=self.original['operationId'], requestDigest=digest(self.original),
                            attemptId=self.original['attemptId'], source='reply')
        authorize = self.creation('auth', 'authorize_creation', confirmation=confirmation, launch=launch)
        for label, bad in (('campo de mas', dict(authorize, extra=1)), ('sin launch', {k: v for k, v in authorize.items() if k != 'launch'}),
                           ('launch de mas', dict(authorize, launch=dict(launch, suite='x')))):
            with self.subTest(label):
                refused = self.call(dict(bad, operationId='bad-' + label.replace(' ', '-')))
                self.assertEqual((refused['exitCode'], refused['reasons'][0]['code']), (2, 'operation-conflict'))
        self.assertNotIn('creation', self.store.read()['reservations'][0])
        issued = self.call(authorize)
        self.assertEqual((issued['exitCode'], issued.get('permit', {}).get('state')), (0, 'issued'), issued)
        permit = issued['permit']['permitId']
        consume = self.creation('consume', 'consume_creation', permitId=permit, launchDigest=digest(launch))
        before = self.snapshot()
        with patch.object(admin, 'local_authority', return_value=self.f.locator):
            out = io.StringIO()
            self.assertEqual(cli.main(['consume_creation'], stdin=io.StringIO(canonical(consume)), stdout=out,
                                      stderr=io.StringIO()), 5)
        self.assertEqual(json.loads(out.getvalue())['reasons'][0]['code'], 'enforcement-unavailable')
        self.assertEqual(self.snapshot(), before)  # El CLI no emparejado no abre escritor.
        consumed = self.call(consume)
        self.assertIs(consumed['creationAllowed'], True)
        self.assertIs(self.call(consume)['creationAllowed'], False)
        started = self.call(self.creation('start', 'record_started', permitId=permit, root=dict(pid=4242, unique=42420)))
        self.assertEqual((started['state'], started['permit']['state']), ('running', 'registered'), started)
        busy = self.call(dict(test_ipc.query('busy'), operation='soltar', key=self.key, evidenceIds=[]))
        self.assertEqual((busy['state'], busy['scopeStatus']['novel']), ('running', [[4242, 42420]]))
        later = self.call(consume)  # Replay tras el registro: currentState running con su permiso.
        client = self.client()
        def received(result):
            return patch.object(api, 'receive_response', return_value=result)
        with received(consumed):  # Control: el resultado real pasa la validación de su operación.
            self.assertEqual(client.consume_creation('consume', self.key, permit, digest(launch)), consumed)
        thin = dict(consumed, permit={k: v for k, v in consumed['permit'].items() if k != 'baselineDigest'})
        def without(result, field): return {k: v for k, v in result.items() if k != field}
        release = dict(test_ipc.query('busy'), operation='soltar', key=self.key, evidenceIds=[])
        # Control: cada resultado real pasa la validación de su operación por el cliente.
        for call, result in ((lambda c: c.authorize_creation('auth', self.key, confirmation, launch), issued),
                             (lambda c: c.record_started('start', self.key, permit, dict(pid=4242, unique=42420)), started),
                             (lambda c: c.consume_creation('consume', self.key, permit, digest(launch)), later),
                             (lambda c: c.call(release), busy)):
            with received(result):
                self.assertEqual(call(self.client()), result)
        # R1-1 a R1-3 de la review del 4c-3: un solo campo cambia respecto del resultado real.
        current = dict(later, currentState=without(later['currentState'], 'permit'))
        scope_short = dict(busy, scopeStatus=dict(busy['scopeStatus'], novel=[]))
        for label, call, poisoned in (
                ('consumo sin key', lambda c: c.consume_creation('consume', self.key, permit, digest(launch)), without(consumed, 'reservationKey')),
                ('consumo con campo de estado', lambda c: c.consume_creation('consume', self.key, permit, digest(launch)), dict(consumed, reservations=[])),
                ('autorizacion sin permiso', lambda c: c.authorize_creation('auth', self.key, confirmation, launch), without(issued, 'permit')),
                ('registro sin permiso', lambda c: c.record_started('start', self.key, permit, dict(pid=4242, unique=42420)), without(started, 'permit')),
                ('consumo uncertain que habilita', lambda c: c.consume_creation('consume', self.key, permit, digest(launch)), dict(consumed, state='uncertain')),
                ('replay con currentState sin permiso', lambda c: c.consume_creation('consume', self.key, permit, digest(launch)), current),
                ('ocupado sin su miembro', lambda c: c.call(release), scope_short)):
            with self.subTest(label), received(poisoned), self.assertRaises(TransportError):
                call(self.client())
        for label, poisoned in (('campo de mas', dict(consumed, extra=1)), ('campo de otra operacion', dict(consumed, scopeStatus=busy['scopeStatus'])),
                                ('permiso sin baselineDigest', thin), ('replay que habilita', dict(self.call(consume), creationAllowed=True))):
            with self.subTest(label), received(poisoned), self.assertRaises(TransportError):
                self.client().consume_creation('consume', self.key, permit, digest(launch))

    def test_heartbeat_only_original_supervisor_channel(self):
        heartbeat = dict(test_ipc.query('beat'), operation='latido', key=self.key, seq=1)
        self.assertEqual(self.call(heartbeat)['exitCode'], 0)
        _, _, other = self.pair('other-owner', 'other')
        for call in (self.client().call, other):
            before = self.store.read()['reservations'][0]
            self.assertEqual(call(dict(heartbeat, operationId='foreign'))['exitCode'], 5)
            self.assertEqual(self.store.read()['reservations'][0], before)
        # Mismo PID, socket nuevo: no adopta el binding del supervisor.
        from hostgate.darwin_peer import DarwinPeers
        with DarwinPeers() as peers:
            self.assertEqual(peers.observe_process('unix', os.getpid()), peers._observe('unix', pid=os.getpid()).identity)
        self.assertEqual(self.call({'__query_channel__': dict(heartbeat, operationId='same-pid')})['exitCode'], 5)
        self.assertEqual(self.call(dict(heartbeat, operationId='beat2', seq=2))['exitCode'], 0)
        self.assertEqual(self.store.read()['reservations'][0]['seq'], 2)

    def test_effective_validators_reject_future_launch_and_corrupt_receipt(self):
        from hostgate.model import validate_view
        for changes in ({'state': 'running'}, {'state': 'released'}, {'launchPossible': True},
                        {'key': dict(self.key, reservationId='s_promoted')}):
            view = self.store.read(); view['reservations'][0].update(changes)
            with self.assertRaises(ValueError): validate_view(view, self.cfg.node)
        poisoned = dict(self.grant, simulationKey=self.key)
        self.writer.execute('UPDATE operations SET result=? WHERE operation_id=?', (canonical(poisoned), 'op'))
        denied = self.call(self.original)
        self.assertEqual(denied['exitCode'], 6)
        self.assertNotIn('originalResult', denied)
        self.assertEqual(self.store.read()['generations'], {'job': 1})

    def test_client_timeout_is_bounded_and_transmitted(self):
        from hostgate import client as api
        client = self.client()
        send = api.send_json
        deadlines = []
        def capture(stream, value, deadline):
            deadlines.append(deadline - api.time.monotonic())
            return send(stream, value, deadline)
        with patch.object(api, 'send_json', side_effect=capture):
            self.assertEqual(client.call(test_ipc.query(), timeout=0.5)['exitCode'], 0)
        self.assertTrue(0 < deadlines[0] <= 0.5, deadlines)
        for timeout in (0, -1, 301, True, float('inf'), float('nan')):
            client = self.client()
            with self.assertRaises(TransportError): client.call(test_ipc.query(), timeout=timeout)
            self.assertEqual(client.stream.fileno(), -1)
