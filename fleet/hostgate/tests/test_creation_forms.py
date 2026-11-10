"""SEN-5650 4b: formas del permiso de creación, evidencia real y modo, sobre vistas y filas plantadas.

Las filas se escriben a mano en su forma durable: la prueba fija el formato, no lo deriva del producto.
"""
import copy
import itertools
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from hostgate.admission import Hostgate
from hostgate.darwin_coalition import Baseline, Birth, Member
from hostgate.model import validate_view
from hostgate.protocol import digest
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
    obs = dict(provider_revision='darwin-coalition@1', issuer_epoch=2, profile_revision='prf_1', permit_id='permit-1',
               confirmation_digest='c' * 64, launch_digest='d' * 64, coalition_id=CID, baseline_digest=BASE.digest,
               root=root_doc(), boot=BOOT, scan_before=[9, 6], scan_after=[9, 6], member_count=3, live_task_count=3,
               new_members=[], baseline_survivors_digest='e' * 64, root_terminal_basis='note-exit',
               resource_policy='process-only')
    if not scoped:
        obs.update(root=None, scan_before=None, scan_after=None, member_count=None, live_task_count=None,
                   baseline_survivors_digest=None, root_terminal_basis=None)
    if not issued:
        obs.update(profile_revision=None, permit_id=None, confirmation_digest=None, launch_digest=None,
                   coalition_id=None, baseline_digest=None)
    obs.update(observed)
    values = dict(kind=kind, issuer='hostgate:%s:2' % row['key']['storeId'], key=row['key'], boot=BOOT,
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
        self.legal(self.with_row(state='uncertain'))
        for state, launched in (('closed', True), ('sealed', False)):
            self.illegal(state, self.with_row(state='uncertain', launchPossible=launched,
                                              creation=permit_doc(row, state)))


class ShadowForms(Planted):
    mode = 'shadow'

    def test_shadow_rows_carry_no_real_permit(self):
        """Variante: real_permit_in_shadow."""
        self.assertTrue(self.accepted(self.view))
        self.illegal('permiso real en sombra', self.with_row(creation=permit_doc(self.row, 'issued')))
        self.assertTrue(self.reply['wouldGrant'], self.reply)


if __name__ == '__main__':
    unittest.main()
