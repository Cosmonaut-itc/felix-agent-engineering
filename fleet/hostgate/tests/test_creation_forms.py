"""SEN-5650 4b: formas del permiso de creación, evidencia real y modo, sobre vistas y filas plantadas.
Las filas se escriben a mano en su forma durable: la prueba fija el formato, no lo deriva del producto."""
import copy
import hashlib
import sqlite3
from dataclasses import asdict, replace
import itertools
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from hostgate import scope
from hostgate.admission import Hostgate
from hostgate.darwin_coalition import Baseline, Birth, Member
from hostgate.model import validate_view
from hostgate.protocol import canonical, digest
from hostgate.scope import ClosureEvidence
from hostgate.store import EffectiveStore, ShadowStore, StoreError, UnrecordedResult
from hostgate.tests.support.coalition import BOOT, CID, OTHER
from hostgate.tests.support.world import World, config, request

BASE = Baseline.build(BOOT, CID, (Member(Birth(BOOT, 100, 10), 501, 501, 501, 1, 7),
                                  Member(Birth(BOOT, 200, 20), 501, 501, 501, 1, 7)), (5, 3))
DROP = object()


def root_doc(**changes):
    return dict(dict(boot=BOOT, pid=300, unique=30, uid=501, ruid=501, svuid=501, parentUnique=10, pidversion=7,
                     coalition=CID), **changes)


def permit_doc(row, state, closed_by=None, **changes):
    value = dict(protocol='darwin-coalition-creation@1', state=state, permitId='permit-1', owner=row['owner'],
                 channelBinding=row['channelBinding'], boot=BOOT, issuerEpoch=1, confirmationDigest='c' * 64,
                 launchDigest='d' * 64, profileRevision='prf_1', coalitionId=CID, baseline=BASE.to_json(),
                 baselineDigest=BASE.digest, baselineCounters=list(BASE.counters))
    if state in ('registered', 'closed'): value.update(root=root_doc(), scopeReceipt='receipt-1')
    if state in ('closed', 'sealed'): value['closedBy'] = closed_by or 'f' * 64
    value.update(changes)
    return {k: v for k, v in value.items() if v is not DROP}


def evidence_doc(row, kind='unix-scope-empty', issued=True, **observed):
    scoped = kind == 'unix-scope-empty'
    obs = dict(provider_revision='darwin-coalition@1', issuer_epoch=1, profile_revision='prf_1', permit_id='permit-1',
               confirmation_digest='c' * 64, launch_digest='d' * 64, coalition_id=CID, baseline_digest=BASE.digest,
               root=root_doc(), boot=BOOT, scan_before=[9, 7], scan_after=[9, 7], member_count=2, live_task_count=2,
               new_members=[], baseline_survivors_digest='e' * 64, root_terminal_basis='note-exit',
               resource_policy='process-only')
    if not scoped:
        obs.update(root=None, scan_before=None, scan_after=None, member_count=None, live_task_count=None,
                   baseline_survivors_digest=None, root_terminal_basis=None)
    if not issued:
        obs.update(profile_revision=None, permit_id=None, confirmation_digest=None, launch_digest=None,
                   coalition_id=None, baseline_digest=None)
    obs.update(observed)
    values = dict(kind=kind, issuer='hostgate:%s:1' % row['key']['storeId'], key=row['key'], boot=BOOT,
                  scope=row['scope'], provenance='darwin-coalition@1', complete=True, creators_sealed=True,
                  root_terminal=scoped, scope_empty=scoped, lineage_complete=scoped, profile_verified=scoped,
                  resources_clean=True, permit_revoked=True, no_external_effects=True, observation=obs)
    return redigest(values)


def redigest(values):
    values = {k: v for k, v in values.items() if k != 'evidence_digest'}
    return dict(values, evidence_digest=digest(values))


class Planted(unittest.TestCase):
    """Una reserva efectiva real de `reservar`, luego filas plantadas por el store efectivo."""
    mode = 'effective'

    def setUp(self):
        from hostgate.tests.test_admin import fixture_policy
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.world = World()
        self.world.boot = BOOT
        with fixture_policy(root):
            self.config = replace(config(root / 'state'), mode=self.mode, store_id=self.mode + '-store')
        (EffectiveStore if self.mode == 'effective' else ShadowStore).initialize(self.config)
        self.gate = Hostgate(self.config, self.world)
        self.auth = self.world.auth()
        self.reply = self.gate.reservar(request(), self.auth)
        self.view = self.gate.store.read()
        self.row = self.view['reservations'][0]
        self.counter = itertools.count()

    def with_row(self, **changes):
        view = copy.deepcopy(self.view)
        row = view['reservations'][0]
        row.update(changes)
        for name in [k for k, v in row.items() if v is DROP]: del row[name]
        return view

    def accepted(self, view, config=None):
        try:
            validate_view(view, config or self.config)
            return True
        except (ValueError, TypeError, KeyError, AttributeError):
            return False

    def roundtrip(self, view):
        """Escribe por el store y relee; devuelve la vista releída o la excepción."""
        def apply(current, original):
            current.clear(); current.update(copy.deepcopy(view))
            return UnrecordedResult({})
        try:
            self.gate.store.mutate('plant', 'plant-%d' % next(self.counter), '0' * 64, apply, self.world)
            return self.gate.store.read()
        except StoreError as exc:
            return exc

    def legal(self, view):
        self.assertTrue(self.accepted(view), view['reservations'][0].get('creation'))
        self.assertEqual(self.roundtrip(view), view)

    def illegal(self, label, view):
        with self.subTest(label):
            self.assertFalse(self.accepted(view), label)

    def released(self, state, proof, issued=True, **changes):
        if state == 'sealed' and not issued:
            creation = dict(protocol='darwin-coalition-creation@1', state='sealed', closedBy=proof['evidence_digest'])
        else:
            creation = permit_doc(self.row, state, closed_by=proof['evidence_digest'])
        return self.with_row(**dict(dict(state='released', creation=creation, closureEvidence=proof,
                                         launchPossible=state in ('consumed', 'registered', 'closed')), **changes))

    def running(self):
        return dict(self.row, state='running', launchPossible=True, creation=permit_doc(self.row, 'registered'))

    def reasons(self, proof, row, mode):
        """closure_reasons sobre la forma durable; una interfaz ausente no es una lista de motivos."""
        try:
            return scope.closure_reasons(scope.evidence_from_json(proof), row, mode=mode)
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
            return 'interfaz ausente: %s' % type(exc).__name__

    def retains(self, label, reasons):
        with self.subTest(label):
            self.assertIsInstance(reasons, list, label)
            self.assertTrue(reasons and all(r['code'] in ('unix-scope-unconfirmed', 'resource-cleanup-unconfirmed')
                                            for r in reasons), (label, reasons))


class Forms(Planted):
    def test_reserved_holds_unissued_issued_or_consumed(self):
        row = self.row
        self.assertNotIn('creation', row)
        self.legal(self.with_row(launchPossible=False))
        self.legal(self.with_row(creation=permit_doc(row, 'issued')))
        self.legal(self.with_row(creation=permit_doc(row, 'consumed'), launchPossible=True))
        for label, changes in (
                ('consumed sin launchPossible', dict(creation=permit_doc(row, 'consumed'))),
                ('issued con launchPossible', dict(creation=permit_doc(row, 'issued'), launchPossible=True)),
                ('sin permiso con launchPossible', dict(launchPossible=True)),
                ('unissued explicito', dict(creation=dict(protocol='darwin-coalition-creation@1', state='unissued'))),
                ('campo de mas en la fila', dict(extra=1))):
            self.illegal(label, self.with_row(**changes))
        for label, changes in (  # Un solo campo del permiso emitido, alterado.
                ('campo de mas', dict(extra=1)), ('campo de menos', dict(launchDigest=DROP)),
                ('protocolo ajeno', dict(protocol='other@1')), ('digest de linea base', dict(baselineDigest='0' * 64)),
                ('contadores de linea base', dict(baselineCounters=[6, 4])), ('epoch booleano', dict(issuerEpoch=True)),
                ('epoch cero', dict(issuerEpoch=0)), ('coalicion del registro', dict(coalitionId=OTHER)),
                ('coalicion decimal', dict(coalitionId=float(CID))), ('confirmacion corta', dict(confirmationDigest='c' * 63)),
                ('lanzamiento no hexadecimal', dict(launchDigest='g' * 64)),
                ('linea base no canonica', dict(baseline=BASE.to_json() + ' ')),
                ('otro dueno', dict(owner=dict(row['owner'], pid=999))), ('otro canal', dict(channelBinding='other-channel'))):
            self.illegal(label, self.with_row(creation=permit_doc(row, 'issued', **changes)))

    def test_running_requires_registered_permit(self):
        """Variante: running_without_root."""
        row, base = self.row, dict(state='running', launchPossible=True)
        self.legal(self.with_row(creation=permit_doc(row, 'registered'), **base))
        self.illegal('consumed sin raiz', self.with_row(creation=permit_doc(row, 'consumed'), **base))
        self.illegal('sin launchPossible', self.with_row(creation=permit_doc(row, 'registered'), **dict(base, launchPossible=False)))
        for label, changes in (
                ('sin raiz', dict(root=DROP)), ('sin recibo', dict(scopeReceipt=DROP)), ('sin linea base', dict(baseline=DROP)),
                ('raiz con campo de mas', dict(root=root_doc(extra=1))), ('raiz con pid booleano', dict(root=root_doc(pid=True))),
                ('raiz con pid cero', dict(root=root_doc(pid=0))), ('raiz con uid desbordado', dict(root=root_doc(uid=2 ** 32)))):
            self.illegal(label, self.with_row(creation=permit_doc(row, 'registered', **changes), **base))

    def test_root_outside_baseline_same_coalition(self):
        """Variante: root_in_baseline."""
        running = dict(state='running', launchPossible=True)
        reused = permit_doc(self.row, 'registered', root=root_doc(pid=200, unique=21))
        self.legal(self.with_row(creation=reused, **running))  # Mismo pid, otro nacimiento: no es de la línea base.
        for label, root in (('raiz de la linea base', root_doc(pid=200, unique=20)),
                            ('raiz de otra coalicion', root_doc(coalition=OTHER)),
                            ('raiz de otro boot', root_doc(boot='99999999-2222-3333-4444-555555555555'))):
            self.illegal(label, self.with_row(creation=permit_doc(self.row, 'registered', root=root), **running))

    def test_uncertain_keeps_its_permit_prefix(self):
        for state, launched in (('issued', False), ('consumed', True), ('registered', True)):
            self.legal(self.with_row(state='uncertain', launchPossible=launched, creation=permit_doc(self.row, state)))
            self.illegal(state + ' con campo de mas', self.with_row(
                state='uncertain', launchPossible=launched, creation=permit_doc(self.row, state, extra=1)))
        self.legal(self.with_row(state='uncertain'))

    def test_each_state_admits_only_its_permits(self):
        legal = dict(reserved='unissued issued consumed', running='registered', uncertain='unissued issued consumed registered')
        bare = dict(protocol='darwin-coalition-creation@1', state='sealed', closedBy='f' * 64)
        for state, variant in itertools.product(legal, 'unissued issued consumed registered closed sealed bare'.split()):
            creation = DROP if variant == 'unissued' else bare if variant == 'bare' else permit_doc(self.row, variant)
            view = self.with_row(state=state, creation=creation, launchPossible=variant in ('consumed', 'registered', 'closed'))
            with self.subTest(state=state, variant=variant): self.assertEqual(self.accepted(view), variant in legal[state].split())

    def test_released_requires_correlated_proof(self):
        """Variante: released_without_proof."""
        row, scoped = self.row, evidence_doc(self.row)
        never, unused = evidence_doc(row, 'never-created'), evidence_doc(row, 'never-created', issued=False)
        self.legal(self.released('closed', scoped))
        for proof, issued in ((never, True), (unused, False)): self.legal(self.released('sealed', proof, issued=issued))
        alien = redigest(dict(scoped, key=dict(row['key'], reservationId='e_other'), scope='e_other'))
        for label, view in (
                ('sin evidencia', self.released('closed', scoped, closureEvidence=DROP)),
                ('evidencia de otra reserva', self.released('closed', alien)),
                ('closedBy de otra prueba', self.with_row(state='released', launchPossible=True, closureEvidence=scoped,
                                                         creation=permit_doc(row, 'closed')))) + tuple(
                ('permiso %s' % state, self.released(state, never if state == 'issued' else scoped))
                for state in ('issued', 'consumed', 'registered')) + (
                ('sin permiso', self.released('sealed', unused, issued=False, creation=DROP, launchPossible=False)),
                ('closed con never-created', self.released('closed', never)),
                ('prueba sin un campo', self.released('sealed', {k: v for k, v in never.items() if k != 'root_terminal'})),
                ('sealed con unix-scope-empty', self.released('sealed', scoped)),
                ('sellado sin emitir con linea base en la prueba', self.released('sealed', never, issued=False))):
            self.illegal(label, view)

    def test_sealed_has_no_root_and_closed_has_one(self):
        """Variante: sealed_with_root."""
        never, unused = evidence_doc(self.row, 'never-created'), evidence_doc(self.row, 'never-created', issued=False)
        for proof, issued in ((never, True), (unused, False)): self.legal(self.released('sealed', proof, issued=issued))
        # Cada ilegal difiere de su legal sólo en la raíz (y el recibo, en el último).
        sealed = permit_doc(self.row, 'sealed', closed_by=never['evidence_digest'], root=root_doc())
        self.illegal('sealed con raiz', self.released('sealed', never, creation=sealed))
        bare = dict(protocol='darwin-coalition-creation@1', state='sealed', closedBy=unused['evidence_digest'], root=root_doc())
        self.illegal('sellado sin emitir con raiz', self.released('sealed', unused, issued=False, creation=bare))
        self.illegal('sealed con raiz y recibo', self.released('sealed', never, creation=dict(sealed, scopeReceipt='receipt-1')))
        scoped = evidence_doc(self.row)
        self.illegal('closed sin raiz', self.released('closed', scoped, creation=permit_doc(
            self.row, 'closed', closed_by=scoped['evidence_digest'], root=DROP, scopeReceipt=DROP)))
        for state, proof in (('closed', scoped), ('sealed', never)):
            self.illegal(state + ' con campo de mas', self.released(state, proof, creation=permit_doc(
                self.row, state, closed_by=proof['evidence_digest'], extra=1)))

    def test_effective_accepts_only_real_evidence(self):
        """Variante: accept_synthetic."""
        self.legal(self.released('closed', evidence_doc(self.row)))
        synthetic = asdict(ClosureEvidence.fixture(self.row['key'], BOOT))
        self.illegal('simulation en effective', self.released('closed', synthetic))
        self.illegal('simulation sellada en effective', self.released('sealed', asdict(ClosureEvidence.fixture(
            self.row['key'], BOOT, 'never-created')), issued=False))
        self.retains('simulation en effective', self.reasons(synthetic, self.running(), 'effective'))

    def test_mode_comes_from_configuration_not_prefix(self):
        """Variante: mode_from_prefix."""
        running, real = self.running(), evidence_doc(self.row)
        self.assertEqual(self.reasons(real, running, 'effective'), [])
        self.retains('real con modo shadow', self.reasons(real, running, 'shadow'))
        self.retains('modo desconocido', self.reasons(real, running, 'enforcing'))
        with self.subTest('fila con modo ajeno'), self.assertRaises(ValueError): scope.validate_reservation(self.row, 'enforcing')
        prefixed = dict(running, key=dict(running['key'], reservationId='s_1'), scope='s_1')
        synthetic = asdict(ClosureEvidence.fixture(prefixed['key'], BOOT))
        self.retains('simulation con prefijo s_', self.reasons(synthetic, prefixed, 'effective'))

    def test_real_evidence_correlates_field_by_field(self):
        """Variante: trust_supervisor_clean."""
        running, real = self.running(), evidence_doc(self.row)
        self.assertEqual(self.reasons(real, running, 'effective'), [])
        obs, store, other = real['observation'], self.row['key']['storeId'], '99999999-2222-3333-4444-555555555555'
        for label, values in (
                ('key', dict(real, key=dict(real['key'], generation=2))),
                ('generacion booleana', dict(real, key=dict(real['key'], generation=True))),
                ('store', dict(real, issuer='hostgate:other-store:1')),
                ('boot', dict(real, boot=other, observation=dict(obs, boot=other))),
                ('epoch del permiso', dict(real, issuer='hostgate:%s:2' % store, observation=dict(obs, issuer_epoch=2))),
                ('emisor sin epoch', dict(real, issuer='hostgate:%s' % store)),
                ('procedencia', dict(real, provenance='simulation')),
                ('sin observacion', {k: v for k, v in real.items() if k != 'observation'})):
            self.retains(label, self.reasons(redigest(values), running, 'effective'))
        near = dict(scan_before=[9, 8], scan_after=[9, 8])  # Un superviviente: legal con member_count y live_task_count en 1.
        for label, changes in (  # Sólo cambia lo que el caso nombra en la observación; re-firmada, retiene.
                ('revision del perfil', dict(profile_revision='prf_2')), ('permiso', dict(permit_id='permit-2')),
                ('confirmacion', dict(confirmation_digest='0' * 64)), ('lanzamiento', dict(launch_digest='0' * 64)),
                ('coalicion', dict(coalition_id=OTHER)), ('digest de la linea base', dict(baseline_digest='0' * 64)),
                ('raiz', dict(root=root_doc(unique=31))), ('miembro nuevo', dict(new_members=[root_doc()])),
                ('proveedor', dict(provider_revision='other@1')), ('epoch booleano', dict(issuer_epoch=True)),
                ('politica vacia', dict(resource_policy='')), ('politica sin texto', dict(resource_policy=True)),
                ('base terminal vacia', dict(root_terminal_basis='')), ('base terminal sin texto', dict(root_terminal_basis=True)),
                ('supervivientes sin digest', dict(baseline_survivors_digest='e' * 63)),
                ('contadores movidos', dict(scan_after=[10, 8])), ('barrido anterior decimal', dict(scan_before=[9.0, 7.0])),
                ('barrido posterior decimal', dict(scan_after=[9.0, 7.0])),
                ('barrido de tres', dict(scan_before=[9, 7, 1], scan_after=[9, 7, 1])),
                ('barrido invertido', dict(scan_before=[7, 9], scan_after=[7, 9], member_count=-2, live_task_count=-2)),
                ('barrido anterior a la linea base', dict(scan_before=[4, 2], scan_after=[4, 2])),
                ('cardinalidad', dict(member_count=1)), ('vivos sin barrido', dict(member_count=1, live_task_count=1)),
                ('miembros decimales', dict(member_count=2.0)), ('vivos decimales', dict(live_task_count=2.0)),
                ('miembros booleanos', dict(near, member_count=True, live_task_count=1)),
                ('vivos booleanos', dict(near, member_count=1, live_task_count=True)),
                ('mas vivos que la linea base', dict(scan_before=[9, 6], scan_after=[9, 6], member_count=3, live_task_count=3))):
            self.retains(label, self.reasons(redigest(dict(real, observation=dict(obs, **changes))), running, 'effective'))
        self.assertEqual(self.reasons(evidence_doc(self.row, **dict(near, member_count=1, live_task_count=1)), running, 'effective'), [])
        self.retains('digest viejo', self.reasons(dict(real, observation=dict(obs, permit_id='permit-2')), running, 'effective'))
        self.illegal('liberada con permiso ajeno', self.released('closed', redigest(
            dict(real, observation=dict(obs, permit_id='permit-2')))))
        never, issued, deep = evidence_doc(self.row, 'never-created'), dict(running, creation=permit_doc(self.row, 'issued')), []
        for _ in range(2000): deep = [deep]
        self.assertEqual(self.reasons(never, issued, 'effective'), [])
        for name, bad in dict(scan_before=[9, 7], scan_after=[9, 7], member_count=0, live_task_count=0,
                              baseline_survivors_digest='e' * 64, root_terminal_basis='note-exit').items():
            self.retains('sobra ' + name, self.reasons(evidence_doc(self.row, 'never-created', **{name: bad}), issued, 'effective'))
        self.retains('boot observado', self.reasons(evidence_doc(self.row, 'never-created', False, boot=other), self.row, 'effective'))
        for label, proof, state, changes in (
                ('never-created con consumed', never, 'consumed', {}), ('never-created con registered', never, 'registered', {}),
                ('permiso de otro dueno', real, 'registered', dict(owner=dict(self.row['owner'], pid=999))),
                ('permiso sin fondo', real, 'registered', dict(owner=dict(self.row['owner'], deep=deep)))):
            self.retains(label, self.reasons(proof, dict(running, creation=permit_doc(self.row, state, **changes)), 'effective'))
        with self.subTest('otro boot'), self.assertRaises(ValueError): scope.creation_from_row(dict(running, physicalBoot=other))
        fake, key = asdict(ClosureEvidence.fixture(running['key'], BOOT)), running['key']
        for mode, proof in (('effective', real), ('shadow', fake)):  # Una fila sin validar retiene; no lanza.
            for i, bad in enumerate((dict(key, generation=float('nan')), dict(key, reservationId=None), None, {})):
                self.retains('fila ilegible %d en %s' % (i, mode), self.reasons(proof, dict(running, key=bad), mode))

    def test_fixture_synthetic_evidence_no_longer_releases(self):
        key = self.reply['reservationKey']
        self.world.closures[('proof',)] = ClosureEvidence.fixture(key, BOOT)
        result = self.gate.soltar('release', key, ['proof'], self.auth)
        self.assertEqual(([r['code'] for r in result['reasons']], result['state']), (['enforcement-unavailable'], 'uncertain'))
        stored = self.gate.store.read()['reservations'][0]
        self.assertEqual((stored['state'], stored['launchPossible']), ('uncertain', False))
        self.assertNotIn('closureEvidence', stored)
        fixture = asdict(ClosureEvidence.fixture(key, BOOT))
        self.illegal('fixture liberando', self.released('closed', fixture))
        self.legal(self.released('closed', evidence_doc(self.row)))


class Store(Planted):
    def raw(self):
        conn = sqlite3.connect(str(self.gate.store.path))
        try: return conn.execute('SELECT payload FROM host_view WHERE id=1').fetchone()[0], conn.execute(
            'SELECT count(*) FROM audit').fetchone()[0]
        finally: conn.close()

    def safe_read(self):
        try: return self.gate.store.read()
        except StoreError as exc: return exc

    def test_illegal_row_is_never_written_and_fails_to_load(self):
        before = self.raw()
        rejected = self.roundtrip(self.with_row(state='running', launchPossible=True))
        self.assertIsInstance(rejected, StoreError)
        self.assertEqual(self.raw(), before)
        self.assertEqual(self.safe_read(), self.view)
        illegal = self.with_row(creation=permit_doc(self.row, 'sealed'))
        writer = self.gate.store.connect(writable=True)
        try:
            writer.execute('UPDATE host_view SET payload=?, checksum=? WHERE id=1', (canonical(illegal), digest(illegal)))
            self.assertIsInstance(self.safe_read(), StoreError)
            writer.execute('UPDATE host_view SET payload=?, checksum=? WHERE id=1', (canonical(self.view), digest(self.view)))
        finally: writer.close()
        self.assertEqual(self.safe_read(), self.view)

    def test_effective_v1_is_rejected_and_left_intact(self):
        """Variante: reset_store."""
        from hostgate.tests.test_admin import fixture_policy
        folder = self.config.state_dir.parent / 'v1'
        with fixture_policy(folder.parent):
            v1 = replace(self.config, state_dir=folder, store_id='v1-store')
        with patch.object(EffectiveStore, 'schema_version', 1): EffectiveStore.initialize(v1)
        def snapshot():
            return {p.name: (p.stat().st_ino, p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
                    for p in folder.iterdir()}
        before = snapshot()
        for writable in (False, True):
            with self.subTest(writable=writable):
                with self.assertRaisesRegex(StoreError, 'schema'):
                    EffectiveStore(v1).connect(writable=writable).close()
                self.assertEqual(snapshot(), before)
        denied = Hostgate(v1, self.world).reservar(request('v1'), self.auth)
        self.assertEqual((denied['exitCode'], [r['code'] for r in denied['reasons']]), (6, ['store-unavailable']))
        self.assertEqual(snapshot(), before)
        self.assertLessEqual(set(snapshot()), set(before))  # El SQLite de Apple deja -wal/-shm desde initialize.
        with open(str(self.gate.store.path), 'rb') as current: header = current.read(72)
        self.assertEqual(int.from_bytes(header[60:64], 'big'), 2)
        writer = self.gate.store.connect(writable=True)
        try: self.assertEqual(writer.execute('SELECT schema_version, kind FROM metadata').fetchall(), [(2, 'effective')])
        finally: writer.close()


class ShadowForms(Planted):
    mode = 'shadow'

    def test_shadow_rows_carry_no_real_permit(self):
        """Variante: real_permit_in_shadow."""
        self.assertTrue(self.accepted(self.view))
        self.illegal('permiso real en sombra', self.with_row(creation=permit_doc(self.row, 'issued')))
        self.assertTrue(self.reply['wouldGrant'], self.reply)
        key = self.reply['simulationKey']
        self.world.closures[('proof',)] = ClosureEvidence.fixture(key, BOOT)
        self.assertEqual(self.gate.soltar('release', key, ['proof'], self.auth)['reasons'], [])
        self.assertTrue(self.accepted(self.gate.store.read()))
        today = asdict(ClosureEvidence.fixture(key, BOOT))
        today.pop('observation', None)  # Forma escrita antes de 4b.
        self.assertEqual(self.gate.store.read()['reservations'][0]['closureEvidence'], today)
        self.assertTrue(self.accepted(self.with_row(state='released', closureEvidence=today)))
        real = evidence_doc(self.row)
        self.illegal('real en shadow', self.with_row(state='released', closureEvidence=real))
        mixed = redigest(dict(today, observation=real['observation']))
        self.illegal('simulation con observacion', self.with_row(state='released', closureEvidence=mixed))
        self.illegal('emisor sin texto', self.with_row(state='released', closureEvidence=redigest(dict(today, issuer=True))))


if __name__ == '__main__': unittest.main()
