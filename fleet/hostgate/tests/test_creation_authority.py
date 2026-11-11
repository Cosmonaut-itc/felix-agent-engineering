"""SEN-5650 4c: la autoridad de creación por los métodos de Hostgate, con el kernel de coalición sustituido."""
from dataclasses import replace
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from hostgate import scope
from hostgate.admission import Hostgate
from hostgate.darwin_coalition import DarwinCoalition
from hostgate.model import budget_view
from hostgate.protocol import digest
from hostgate.store import EffectiveStore, ShadowStore
from hostgate.tests.support import coalition
from hostgate.tests.support.world import World, config, request

LAUNCH = dict(argvDigest='a' * 64, inputDigest='b' * 64, bootstrapDigest='c' * 64)
ROOT = dict(pid=300, unique=30)


class Authority(unittest.TestCase):
    """Hostgate efectivo en proceso: store efectivo real y coalición del dueño pid 100, uniqueid 10."""
    mode = 'effective'

    def setUp(self):
        from hostgate.tests.test_admin import fixture_policy
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.world = World()
        self.world.boot = coalition.BOOT
        self.kernel = coalition.World()
        self.world.coalition = DarwinCoalition(_native=self.kernel)
        with fixture_policy(root):
            self.config = replace(config(root / 'state'), mode=self.mode, store_id=self.mode + '-store')
        (EffectiveStore if self.mode == 'effective' else ShadowStore).initialize(self.config)
        self.gate = Hostgate(self.config, self.world)
        self.auth = self.world.auth(start=(1, 2, 10, 7))  # startToken Darwin: (seg, useg, uniqueid, versión).
        self.key = self.reserve('op', 'job')

    def reserve(self, op, job):
        granted = self.gate.reservar(request(op, job), self.auth)
        self.assertTrue(granted['granted'], granted['reasons'])
        return granted['reservationKey']

    @staticmethod
    def confirmation(grant='op', job='job'):
        return dict(operationId=grant, requestDigest=digest(request(grant, job)), attemptId='a1', source='reply')

    def authorize(self, op, key=None, gate=None, **conf):
        return (gate or self.gate).authorize_creation(op, key or self.key, self.confirmation(**conf), LAUNCH, self.auth)

    def consume(self, op, permit_id, key=None, gate=None):
        return (gate or self.gate).consume_creation(op, key or self.key, permit_id, digest(LAUNCH), self.auth)

    def start(self, op, permit_id, root=ROOT, key=None):
        return self.gate.record_started(op, key or self.key, permit_id, root, self.auth)

    def release(self, op, key=None, auth=None):
        return self.gate.soltar(op, key or self.key, [], auth or self.auth)

    def running(self):
        """reservar -> authorize -> consume -> record_started con la raíz 300 viva, hija del dueño."""
        permit = self.authorize('auth-1')['permit']['permitId']
        self.assertTrue(self.consume('consume-1', permit)['creationAllowed'])
        self.kernel.add(ROOT['pid'], ROOT['unique'], parent=10)
        started = self.start('start-1', permit)
        self.assertEqual((started['exitCode'], started['state']), (0, 'running'), started['reasons'])
        return permit, started

    def row(self, key=None):
        key = key or self.key
        return next(r for r in self.gate.store.read()['reservations'] if r['key'] == key)

    def available_slots(self):
        return budget_view(self.config, self.gate.store.read()['reservations'])['available']['slots']


class HappyPath(Authority):
    def test_full_cycle_releases_with_real_correlated_evidence_once(self):
        """Variante: add_credit."""
        permit, started = self.running()
        self.assertTrue(started['permit']['scopeReceipt'].startswith('scp_'))
        self.assertEqual(self.available_slots(), 1)
        self.kernel.exit(ROOT['pid'])
        closed = self.release('release-1')
        self.assertEqual((closed['exitCode'], closed['state'], closed['permit']['state']), (0, 'released', 'closed'),
                         closed['reasons'])
        row = self.row()
        self.assertEqual((row['state'], row['creation']['state'], row['reasons']), ('released', 'closed', []))
        evidence = scope.evidence_from_json(row['closureEvidence'])
        self.assertEqual((evidence.kind, evidence.provenance, evidence.issuer),
                         ('unix-scope-empty', 'darwin-coalition@1', 'hostgate:effective-store:1'))
        self.assertEqual(evidence.observation.permit_id, permit)
        self.assertEqual(row['creation']['closedBy'], evidence.evidence_digest)
        self.assertEqual(scope.closure_reasons(evidence, row, mode='effective'), [])
        self.assertEqual(self.available_slots(), 2)
        other = self.reserve('op-2', 'job-2')
        again = self.release('release-2')
        self.assertEqual((again['exitCode'], again['state'], again['permit']), (0, 'released', closed['permit']))
        self.assertEqual(self.row()['closureEvidence'], row['closureEvidence'])
        self.assertEqual(self.available_slots(), 1)
        self.assertEqual(self.row(other)['state'], 'reserved')


class SinglePermit(Authority):
    def test_second_authorize_consume_and_root_are_rejected(self):
        """Variantes: regenerate_permit, refresh_baseline."""
        original = self.authorize('auth-1')
        permit, baseline = original['permit']['permitId'], self.row()['creation']['baseline']
        self.kernel.add(200, 20, parent=1)  # Otra línea base si alguien la volviera a tomar.
        second = self.authorize('auth-2')
        self.assertEqual((second['exitCode'], [r['code'] for r in second['reasons']]), (2, ['operation-conflict']))
        self.assertEqual(second['permit'], original['permit'])
        consumed = self.consume('consume-1', permit)
        self.assertIs(consumed['creationAllowed'], True)
        results = [second, consumed]
        for op, candidate in (('consume-2', permit), ('consume-3', 'pmt_' + '0' * 32)):
            again = self.consume(op, candidate)
            self.assertEqual((again['exitCode'], again['creationAllowed']), (2, False), again)
            results.append(again)
        self.kernel.add(ROOT['pid'], ROOT['unique'], parent=10)
        started = self.start('start-1', permit)
        self.kernel.add(301, 31, parent=10)
        other_root = self.start('start-2', permit, dict(pid=301, unique=31))
        self.assertEqual((other_root['exitCode'], other_root['state']), (2, 'uncertain'))
        results += [started, other_root]
        self.assertEqual(self.row()['creation']['permitId'], permit)
        self.assertEqual(self.row()['creation']['baseline'], baseline)
        self.assertEqual([r for r in results if r.get('creationAllowed')], [consumed])

    def test_replays_return_the_original_permit_and_never_allow_creation(self):
        """Variante: replay_allows_spawn."""
        original = self.authorize('auth-1')
        permit, baseline = original['permit']['permitId'], self.row()['creation']['baseline']
        self.kernel.add(200, 20, parent=1)
        replayed = self.authorize('auth-1')
        self.assertEqual((replayed['replayed'], replayed['granted']), (True, False))
        self.assertEqual(replayed['originalResult'], original)
        self.assertEqual(replayed['currentState']['permit'], original['permit'])
        self.assertEqual(self.row()['creation']['baseline'], baseline)
        consumed = self.consume('consume-1', permit)
        again = self.consume('consume-1', permit)
        self.assertIs(again['originalResult']['creationAllowed'], True)
        self.assertIs(again['creationAllowed'], False)
        self.assertEqual(again['currentState']['permit']['state'], 'consumed')
        self.kernel.add(ROOT['pid'], ROOT['unique'], parent=10)
        started = self.start('start-1', permit)
        restarted = self.start('start-1', permit)
        self.assertEqual(restarted['originalResult'], started)
        self.assertEqual(restarted['currentState']['permit']['scopeReceipt'], started['permit']['scopeReceipt'])
        self.assertEqual((self.row()['creation']['permitId'], self.row()['creation']['baseline']), (permit, baseline))
        self.assertIs(consumed['creationAllowed'], True)


class Seal(Authority):
    def test_unissued_and_issued_seal_never_created_and_consumed_retains(self):
        """Variante: seal_consumed."""
        sealed = self.release('release-a')
        self.assertEqual((sealed['exitCode'], sealed['state'], set(sealed.get('permit', ()))),
                         (0, 'released', {'state', 'closedBy'}))
        self.assertEqual(scope.evidence_from_json(self.row()['closureEvidence']).kind, 'never-created')
        issued_key = self.reserve('op-b', 'job-b')
        permit = self.authorize('auth-b', issued_key, grant='op-b', job='job-b')['permit']['permitId']
        sealed = self.release('release-b', issued_key)
        self.assertEqual((sealed['state'], sealed['permit']['state'], sealed['permit']['permitId']),
                         ('released', 'sealed', permit))
        late = self.consume('consume-b', permit, issued_key)
        self.assertEqual((late['exitCode'], late['creationAllowed'], late['state']), (2, False, 'released'))
        consumed_key = self.reserve('op-c', 'job-c')
        permit = self.authorize('auth-c', consumed_key, grant='op-c', job='job-c')['permit']['permitId']
        self.consume('consume-c', permit, consumed_key)
        kept = self.release('release-c', consumed_key)
        self.assertEqual((kept['exitCode'], kept['state'], kept['permit']['state']), (1, 'reserved', 'consumed'))
        row = self.row(consumed_key)
        self.assertEqual((row['state'], row['creation']['state'], row['launchPossible']), ('reserved', 'consumed', True))
        self.assertNotIn('closureEvidence', row)
        self.assertEqual(self.available_slots(), 1)


class Busy(Authority):
    def test_occupied_and_retry_answer_busy_without_durable_reasons(self):
        """Variantes: durable_busy, release_on_occupied."""
        self.running()
        busy = self.release('release-1')
        self.assertEqual((busy['exitCode'], busy['state'], busy.get('scopeStatus')),
                         (1, 'running', dict(status='occupied', novel=[[300, 30]], novelCount=1)))
        self.assertTrue(busy['reasons'][0]['retryable'])
        self.assertEqual((self.row()['state'], self.row()['reasons']), ('running', []))
        replayed = self.release('release-1')
        self.assertEqual((replayed['originalResult'], replayed['currentState']['state']), (busy, 'running'))
        self.kernel.saturated = True
        saturated = self.release('release-2')
        self.assertEqual(saturated['scopeStatus'], dict(status='scan-retry', reason='list-saturated'))
        self.kernel.saturated = False
        self.world.coalition = DarwinCoalition(_native=self.kernel, _clock=coalition.Clock(step=0.06))
        late = self.release('release-3')
        self.assertEqual(late['scopeStatus'], dict(status='scan-retry', reason='deadline'))  # SCAN_LIMIT de 4a.
        self.assertEqual((self.row()['state'], self.row()['reasons']), ('running', []))
        self.world.coalition = DarwinCoalition(_native=self.kernel)
        self.kernel.exit(ROOT['pid'])
        self.assertEqual(self.release('release-4')['state'], 'released')

    def test_invalid_scan_leaves_uncertain_and_closes_other_ledgers(self):
        """Variante: ninguna; Invalid no es busy y deja duda durable."""
        self.running()
        self.kernel.exit(ROOT['pid'])
        self.kernel.boot_value = '99999999-2222-3333-4444-555555555555'
        invalid = self.release('release-1')
        self.assertEqual((invalid['state'], invalid['scopeStatus']), ('uncertain', dict(status='invalid', reason='boot-changed')))
        self.assertEqual(self.row()['state'], 'uncertain')
        self.assertEqual(self.available_slots(), 1)
        other = self.gate.reservar(request('op-2', 'job-2'), self.auth)
        self.assertFalse(other['granted'])
        self.assertIn('reservation-uncertain', [r['code'] for r in other['reasons']])


class Veto(Authority):
    def test_elevated_new_member_adds_durable_doubt(self):
        """Variante: ignore_elevated."""
        self.running()
        self.kernel.add(301, 31, parent=30, uid=0, svuid=0)  # euid 0 con ruid 501: elevado.
        vetoed = self.release('release-1')
        self.assertEqual((vetoed['state'], vetoed['scopeStatus']['novel']), ('uncertain', [[300, 30], [301, 31]]))
        row = self.row()
        self.assertEqual((row['state'], row['creation']['state']), ('uncertain', 'registered'))
        self.assertIn('reservation-uncertain', [r['code'] for r in row['reasons']])

    def test_owner_child_already_in_the_baseline_refuses_authorize_with_doubt(self):
        """Variante: allow_prior_child. Design §2: un hijo propio ya presente al autorizar invalida el orden."""
        self.kernel.add(200, 20, parent=10)
        refused = self.authorize('auth-1')
        self.assertEqual([r['code'] for r in refused['reasons']][:1], ['identity-unverifiable'], refused)
        row = self.row()
        self.assertEqual((row['state'], type(scope.creation_from_row(row))), ('uncertain', scope.Unissued))


class Fencing(Authority):
    def assertUntouched(self, result, code, before):
        self.assertEqual([r['code'] for r in result['reasons']][:1], [code], result)
        self.assertNotEqual(result['exitCode'], 0)
        self.assertEqual(self.row().get('creation'), before)

    def test_foreign_owner_and_channel_never_read_the_permit(self):
        """Variante: foreign_permit_read."""
        permit = self.authorize('auth-1')['permit']['permitId']
        before = self.row()['creation']
        intruder = self.world.auth(pid=200, start=(1, 2, 20, 7))
        channel = replace(self.auth, channel_binding='other-channel')
        with patch.object(scope, 'closure_reasons', wraps=scope.closure_reasons) as spy:
            for auth in (intruder, channel):
                for result in (self.gate.consume_creation('x-c', self.key, permit, digest(LAUNCH), auth),
                               self.gate.soltar('x-s', self.key, [], auth)):
                    self.assertUntouched(result, 'identity-unverifiable', before)
                    self.assertEqual(result['exitCode'], 5)
            self.assertEqual(spy.call_count, 0)

    def test_profile_revision_and_epoch_reject_without_touching_the_permit(self):
        """Variante: ninguna; el fencing de perfil y epoch deja la duda y el permiso intacto."""
        other = self.reserve('op-b', 'job-b')
        permit = self.authorize('auth-1')['permit']['permitId']
        other_permit = self.authorize('auth-b', other, grant='op-b', job='job-b')['permit']['permitId']
        before, other_before = self.row()['creation'], self.row(other)['creation']
        self.world.profile_revision = 'prf_other'
        self.assertUntouched(self.consume('consume-1', permit), 'profile-unverified', before)
        self.assertEqual(self.row()['state'], 'uncertain')
        self.world.profile_revision = 'prf_fixture'
        restarted = Hostgate(self.config, self.world)  # Otro proceso: otro epoch del emisor.
        for result in (self.consume('consume-b', other_permit, other, gate=restarted),
                       restarted.soltar('release-b', other, [], self.auth)):
            self.assertEqual([r['code'] for r in result['reasons']][:1], ['reservation-uncertain'], result)
            self.assertEqual(self.row(other)['creation'], other_before)
        self.assertEqual(self.row(other)['state'], 'uncertain')

    def test_boot_change_rejects_authorize(self):
        """Variante: ninguna; el boot del host fija la autoridad."""
        self.world.boot = '99999999-2222-3333-4444-555555555555'
        self.assertUntouched(self.authorize('auth-1'), 'boot-unverified', None)

    def test_old_generation_only_reads_its_own_closure(self):
        """Variante: lookup_latest_job."""
        sealed = self.release('release-1')
        newer = self.reserve('op-2', 'job')
        self.assertEqual(newer['generation'], 2)
        stale = self.authorize('auth-old')
        self.assertEqual(([r['code'] for r in stale['reasons']][:1], stale['reservationKey']), (['stale-generation'], self.key))
        reread = self.release('release-2')
        self.assertEqual((reread['exitCode'], reread['reservationKey'], reread['permit']), (0, self.key, sealed['permit']))
        row = self.row(newer)
        self.assertEqual((row['state'], 'creation' in row), ('reserved', False))
        self.assertEqual(self.available_slots(), 1)

    def test_unknown_key_is_rejected(self):
        """Variante: lookup_latest_job."""
        for i, key in enumerate((dict(self.key, generation=7), dict(self.key, reservationId='e_' + '0' * 32))):
            result = self.authorize('auth-%d' % i, key)
            self.assertEqual([r['code'] for r in result['reasons']], ['stale-generation'], result)
        self.assertNotIn('creation', self.row())


class Shadow(Authority):
    mode = 'shadow'

    def reserve(self, op, job):
        granted = self.gate.reservar(request(op, job), self.auth)
        self.assertTrue(granted['wouldGrant'], granted['reasons'])
        return granted['simulationKey']

    def test_shadow_rejects_the_three_operations_and_keeps_soltar(self):
        """Variante: ninguna; en sombra la autoridad no existe."""
        view = self.gate.store.read()
        for result in (self.authorize('auth-1'), self.consume('consume-1', 'pmt_1'), self.start('start-1', 'pmt_1')):
            self.assertEqual((result['exitCode'], result['reasons'][0]['code']), (5, 'enforcement-unavailable'))
            self.assertNotIn('permit', result)
        self.assertEqual(self.gate.store.read(), view)
        kept = self.release('release-1')
        self.assertEqual((kept['simulationState'], kept['reasons'][0]['code']), ('uncertain', 'unix-scope-unconfirmed'))


if __name__ == '__main__': unittest.main()
