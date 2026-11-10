"""SEN-5650 4b: formas del permiso de creación, evidencia real y modo, sobre vistas y filas plantadas.

Las filas se escriben a mano en su forma durable: la prueba fija el formato, no lo deriva del producto.
"""
import copy
from dataclasses import asdict
import itertools
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from hostgate.admission import Hostgate
from hostgate.darwin_coalition import Baseline, Birth, Member
from hostgate.model import validate_view
from hostgate.protocol import digest
from hostgate.scope import ClosureEvidence
from hostgate.store import EffectiveStore, ShadowStore, StoreError, UnrecordedResult
from hostgate.tests.support.coalition import BOOT, CID, OTHER
from hostgate.tests.support.world import World, config, request

BASE = Baseline.build(BOOT, CID, (Member(Birth(BOOT, 100, 10), 501, 501, 501, 1, 7),
                                  Member(Birth(BOOT, 200, 20), 501, 501, 501, 1, 7)), (5, 3))
DROP = object()


def root_doc(**changes):
    value = dict(boot=BOOT, pid=300, unique=30, uid=501, ruid=501, svuid=501, parentUnique=10, pidversion=7,
                 coalition=CID)
    value.update(changes)
    return value


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
        from hostgate import scope
        try:
            return scope.closure_reasons(scope.evidence_from_json(proof), row, mode=mode)
        except (AttributeError, TypeError, RecursionError) as exc:
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
                ('registered en reserved', dict(creation=permit_doc(row, 'registered'), launchPossible=True)),
                ('campo de mas', dict(creation=permit_doc(row, 'issued', extra=1))),
                ('campo de menos', dict(creation=permit_doc(row, 'issued', launchDigest=DROP))),
                ('protocolo ajeno', dict(creation=permit_doc(row, 'issued', protocol='other@1'))),
                ('digest de linea base', dict(creation=permit_doc(row, 'issued', baselineDigest='0' * 64))),
                ('contadores de linea base', dict(creation=permit_doc(row, 'issued', baselineCounters=[6, 4]))),
                ('epoch booleano', dict(creation=permit_doc(row, 'issued', issuerEpoch=True))),
                ('coalicion del registro', dict(creation=permit_doc(row, 'issued', coalitionId=OTHER))),
                ('linea base no canonica', dict(creation=permit_doc(row, 'issued', baseline=BASE.to_json() + ' '))),
                ('otro dueno', dict(creation=permit_doc(row, 'issued', owner=dict(row['owner'], pid=999)))),
                ('otro canal', dict(creation=permit_doc(row, 'issued', channelBinding='other-channel'))),
                ('campo de mas en la fila', dict(extra=1))):
            self.illegal(label, self.with_row(**changes))

    def test_running_requires_registered_permit(self):
        """Variante: running_without_root."""
        row = self.row
        self.legal(self.with_row(state='running', launchPossible=True, creation=permit_doc(row, 'registered')))
        for label, changes in (
                ('sin permiso', dict()),
                ('consumed sin raiz', dict(creation=permit_doc(row, 'consumed'))),
                ('sin raiz', dict(creation=permit_doc(row, 'registered', root=DROP))),
                ('sin recibo', dict(creation=permit_doc(row, 'registered', scopeReceipt=DROP))),
                ('sin linea base', dict(creation=permit_doc(row, 'registered', baseline=DROP))),
                ('raiz con campo de mas', dict(creation=permit_doc(row, 'registered', root=root_doc(extra=1)))),
                ('raiz con pid booleano', dict(creation=permit_doc(row, 'registered', root=root_doc(pid=True)))),
                ('closed en running', dict(creation=permit_doc(row, 'closed'))),
                ('registered sin launchPossible', dict(creation=permit_doc(row, 'registered'), launchPossible=False))):
            self.illegal(label, self.with_row(**dict(dict(state='running', launchPossible=True), **changes)))

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
        row = self.row
        for state, launched in (('issued', False), ('consumed', True), ('registered', True)):
            self.legal(self.with_row(state='uncertain', launchPossible=launched, creation=permit_doc(row, state)))
            self.illegal(state + ' con campo de mas', self.with_row(
                state='uncertain', launchPossible=launched, creation=permit_doc(row, state, extra=1)))
        self.legal(self.with_row(state='uncertain'))
        for state, launched in (('closed', True), ('sealed', False)):
            self.illegal(state, self.with_row(state='uncertain', launchPossible=launched,
                                              creation=permit_doc(row, state)))


    def test_released_requires_correlated_proof(self):
        """Variante: released_without_proof."""
        row, scoped = self.row, evidence_doc(self.row)
        never, unused = evidence_doc(row, 'never-created'), evidence_doc(row, 'never-created', issued=False)
        self.legal(self.released('closed', scoped))
        self.legal(self.released('sealed', never))
        self.legal(self.released('sealed', unused, issued=False))
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
                ('sealed con unix-scope-empty', self.released('sealed', scoped)),
                ('sellado sin emitir con linea base en la prueba', self.released('sealed', never, issued=False))):
            self.illegal(label, view)

    def test_sealed_has_no_root_and_closed_has_one(self):
        """Variante: sealed_with_root."""
        never, unused = evidence_doc(self.row, 'never-created'), evidence_doc(self.row, 'never-created', issued=False)
        self.legal(self.released('sealed', never))
        self.legal(self.released('sealed', unused, issued=False))
        # Cada ilegal difiere de su legal sólo en la raíz (y el recibo, en el último).
        sealed = permit_doc(self.row, 'sealed', closed_by=never['evidence_digest'], root=root_doc())
        self.illegal('sealed con raiz', self.released('sealed', never, creation=sealed))
        bare = dict(protocol='darwin-coalition-creation@1', state='sealed', closedBy=unused['evidence_digest'],
                    root=root_doc())
        self.illegal('sellado sin emitir con raiz', self.released('sealed', unused, issued=False, creation=bare))
        self.illegal('sealed con raiz y recibo', self.released('sealed', never, creation=dict(
            sealed, scopeReceipt='receipt-1')))
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
        prefixed = dict(running, key=dict(running['key'], reservationId='s_1'), scope='s_1')
        synthetic = asdict(ClosureEvidence.fixture(prefixed['key'], BOOT))
        self.retains('simulation con prefijo s_', self.reasons(synthetic, prefixed, 'effective'))

    def test_real_evidence_correlates_field_by_field(self):
        """Variante: trust_supervisor_clean."""
        running, real = self.running(), evidence_doc(self.row)
        self.assertEqual(self.reasons(real, running, 'effective'), [])
        obs = real['observation']
        other = '99999999-2222-3333-4444-555555555555'
        for label, values in (
                ('key', dict(real, key=dict(real['key'], generation=2))),
                ('generacion booleana', dict(real, key=dict(real['key'], generation=True))),
                ('store', dict(real, issuer='hostgate:other-store:1')),
                ('boot', dict(real, boot=other, observation=dict(obs, boot=other))),
                ('revision del perfil', dict(real, observation=dict(obs, profile_revision='prf_2'))),
                ('permiso', dict(real, observation=dict(obs, permit_id='permit-2'))),
                ('confirmacion', dict(real, observation=dict(obs, confirmation_digest='0' * 64))),
                ('lanzamiento', dict(real, observation=dict(obs, launch_digest='0' * 64))),
                ('coalicion', dict(real, observation=dict(obs, coalition_id=OTHER))),
                ('digest de la linea base', dict(real, observation=dict(obs, baseline_digest='0' * 64))),
                ('raiz', dict(real, observation=dict(obs, root=root_doc(unique=31)))),
                ('miembro nuevo', dict(real, observation=dict(obs, new_members=[root_doc()]))),
                ('contadores movidos', dict(real, observation=dict(obs, scan_after=[10, 7]))),
                ('barrido posterior decimal', dict(real, observation=dict(obs, scan_after=[9.0, 7.0]))),
                ('cardinalidad', dict(real, observation=dict(obs, member_count=4))),
                ('mas vivos que la linea base', dict(real, observation=dict(
                    obs, scan_before=[9, 6], scan_after=[9, 6], member_count=3, live_task_count=3))),
                ('epoch del permiso', dict(real, issuer='hostgate:%s:2' % self.row['key']['storeId'],
                                           observation=dict(obs, issuer_epoch=2))),
                ('proveedor', dict(real, observation=dict(obs, provider_revision='other@1'))),
                ('barrido anterior a la linea base', dict(real, observation=dict(obs, scan_before=[4, 2],
                                                                                 scan_after=[4, 2]))),
                ('emisor sin epoch', dict(real, issuer='hostgate:%s' % self.row['key']['storeId'])),
                ('procedencia', dict(real, provenance='simulation')),
                ('sin observacion', {k: v for k, v in real.items() if k != 'observation'})):
            self.retains(label, self.reasons(redigest(values), running, 'effective'))
        self.retains('digest viejo', self.reasons(dict(real, observation=dict(obs, permit_id='permit-2')),
                                                  running, 'effective'))
        self.illegal('liberada con permiso ajeno', self.released('closed', redigest(
            dict(real, observation=dict(obs, permit_id='permit-2')))))
        never, deep = evidence_doc(self.row, 'never-created'), []
        for _ in range(2000): deep = [deep]
        self.assertEqual(self.reasons(never, dict(running, creation=permit_doc(self.row, 'issued')), 'effective'), [])
        for label, proof, state, changes in (
                ('never-created con consumed', never, 'consumed', {}),
                ('never-created con registered', never, 'registered', {}),
                ('permiso de otro dueno', real, 'registered', dict(owner=dict(self.row['owner'], pid=999))),
                ('permiso sin fondo', real, 'registered', dict(owner=dict(self.row['owner'], deep=deep)))):
            self.retains(label, self.reasons(proof, dict(running, creation=permit_doc(self.row, state, **changes)),
                                             'effective'))

    def test_fixture_synthetic_evidence_no_longer_releases(self):
        key = self.reply['reservationKey']
        self.world.closures[('proof',)] = ClosureEvidence.fixture(key, BOOT)
        result = self.gate.soltar('release', key, ['proof'], self.auth)
        self.assertEqual(([r['code'] for r in result['reasons']], result['state']),
                         (['enforcement-unavailable'], 'uncertain'))
        stored = self.gate.store.read()['reservations'][0]
        self.assertEqual((stored['state'], stored['launchPossible']), ('uncertain', False))
        self.assertNotIn('closureEvidence', stored)
        fixture = asdict(ClosureEvidence.fixture(key, BOOT))
        self.illegal('fixture liberando', self.released('closed', fixture))
        self.legal(self.released('closed', evidence_doc(self.row)))


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


if __name__ == '__main__':
    unittest.main()
