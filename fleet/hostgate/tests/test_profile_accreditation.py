"""SEN-5650 4b: acreditación del perfil en la configuración del operador, por load_config real."""
import copy
from dataclasses import FrozenInstanceError
import os
from pathlib import Path
import tempfile
import unittest
from hostgate import admin
from hostgate.protocol import digest
from hostgate.scope import ClosureEvidence
from hostgate.tests.test_admin import Fixture, fixture_policy

BOOT = '11111111-2222-3333-4444-555555555555'
BUILD = '25G120'
NAME = 'darwin-coalition-process@1'


def block(**changes):
    value = dict(minimumDemand=dict(cpuMilli=500, memoryBytes=500, diskBytes={'data': 500}, slots=1),
                 scopeMechanism='darwin-coalition@1', darwinBuild=BUILD, bootstrapDigest='b' * 64,
                 workloadUid=os.getuid(), launcherDigest='a' * 64,
                 accreditation=dict(campaignRunId='20261110-campaign', sha='1' * 40, darwinBuild=BUILD))
    value.update(changes)
    return {k: v for k, v in value.items() if v is not None}


class Profiles(unittest.TestCase):
    """Configuración privada y veredictos de acreditación; sin pruebas propias."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        policy = fixture_policy(root)
        self.addCleanup(policy.close)
        self.f = Fixture(root)
        self.f.provision()
        self.today = copy.deepcopy(self.f.doc)

    def load(self, **profiles):
        self.f.doc = copy.deepcopy(self.today)
        self.f.doc['profiles'].update(profiles)
        self.f.publish()
        try:
            return admin.load_config(self.f.locator)
        except admin.AdminError as exc:
            return exc

    def passed(self, build=BUILD):
        kind = getattr(admin, 'SelfcheckPassed', None)
        return kind and kind(BOOT, build)

    def verdict(self, profile_id, spec=None, build=BUILD, uid=None, selfcheck='passed'):
        cfg = self.load(**({profile_id: spec} if spec else {}))
        if not hasattr(cfg, 'profiles') or not hasattr(admin, 'accredit'): return 'interfaz ausente: %r' % (cfg,)
        selfcheck = self.passed() if selfcheck == 'passed' else selfcheck
        return admin.accredit(cfg.profiles[profile_id], darwin_build=build,
                              workload_uid=os.getuid() if uid is None else uid, selfcheck=selfcheck)

    def unverified(self, label, verdict, detail):
        with self.subTest(label):
            self.assertIsInstance(verdict, dict, label)
            self.assertEqual(verdict['code'], 'profile-unverified')
            self.assertTrue(verdict['detail']['detail'].startswith(detail), verdict)


class Accreditation(Profiles):
    def test_today_config_loads_unaccredited_with_same_digest(self):
        cfg = self.load()
        self.assertNotIsInstance(cfg, admin.AdminError)
        self.assertEqual(cfg.content_digest, digest(self.today))
        self.assertEqual(cfg.node.revision, 'cfg_' + digest(self.today))
        profile = getattr(cfg, 'profiles', {}).get('fixture')
        self.assertIsNotNone(profile)
        self.assertEqual((profile.profile_id, profile.accreditation), ('fixture', None))
        self.assertEqual(profile.revision, 'prf_' + digest(dict(profileId='fixture', spec=self.today['profiles']['fixture'])))
        self.unverified('sin bloque', self.verdict('fixture'), 'perfil sin bloque')

    def test_block_is_strict_frozen_and_revisioned(self):
        cfg = self.load(**{NAME: block()})
        self.assertNotIsInstance(cfg, admin.AdminError, cfg)
        profile = cfg.profiles[NAME]
        self.assertEqual((profile.accreditation.darwin_build, profile.accreditation.workload_uid,
                          profile.accreditation.campaign_sha), (BUILD, os.getuid(), '1' * 40))
        with self.assertRaises(FrozenInstanceError): profile.accreditation.darwin_build = 'other'
        with self.assertRaises(TypeError): cfg.profiles['other'] = profile
        self.assertNotEqual(self.load(**{NAME: block(launcherDigest='c' * 64)}).profiles[NAME].revision, profile.revision)
        campaign = block()['accreditation']
        for label, spec in [('sin ' + name, block(**{name: None})) for name in block() if name != 'minimumDemand'] + [
                ('campo de mas', block(extra=1)),
                ('campana con campo de mas', block(accreditation=dict(campaign, extra=1))),
                ('campana sin sha', block(accreditation={k: v for k, v in campaign.items() if k != 'sha'})),
                ('mecanismo desconocido', block(scopeMechanism='process-group@1')),
                ('digest corto', block(bootstrapDigest='b' * 63)),
                ('sha no hexadecimal', block(accreditation=dict(campaign, sha='z' * 40))),
                ('uid booleano', block(workloadUid=True)),
                ('campana de otro build', block(accreditation=dict(campaign, darwinBuild='25G999')))]:
            with self.subTest(label):
                self.assertIsInstance(self.load(**{NAME: spec}), admin.AdminError, label)

    def test_profile_name_never_accredits(self):
        """Variante: profile_id_is_certificate."""
        self.unverified('nombre sin bloque', self.verdict(NAME, dict(minimumDemand=block()['minimumDemand'])),
                        'perfil sin bloque')
        self.unverified('nombre con bloque de otro host', self.verdict(NAME, block(), build='25H001'), 'build')
        verdict = self.verdict('fixture-named', block())  # Otro nombre, mismo bloque: acredita el bloque.
        self.assertEqual((type(verdict).__name__, getattr(verdict, 'profile_id', None), getattr(verdict, 'boot', None)),
                         ('Accredited', 'fixture-named', BOOT))

    def test_each_mismatch_is_profile_unverified(self):
        failed = getattr(admin, 'SelfcheckFailed', lambda *a: None)('zombie', 'zombi contado como vivo')
        for label, kwargs, detail in (
                ('build distinto', dict(build='25H001'), 'build'),
                ('uid distinto', dict(uid=os.getuid() + 1), 'uid'),
                ('selfcheck fallido', dict(selfcheck=failed), 'selfcheck fallido: zombie'),
                ('selfcheck ausente', dict(selfcheck=None), 'selfcheck ausente'),
                ('selfcheck booleano', dict(selfcheck=True), 'selfcheck ausente'),
                ('selfcheck de otro build', dict(selfcheck=self.passed('25H001')), 'build')):
            self.unverified(label, self.verdict(NAME, block(), **kwargs), detail)
        self.unverified('uid booleano igual al configurado', self.verdict(NAME, block(workloadUid=1), uid=True), 'uid')
        verdict = self.verdict(NAME, block())
        self.assertEqual((type(verdict).__name__, getattr(verdict, 'boot', None), getattr(verdict, 'darwin_build', None)),
                         ('Accredited', BOOT, BUILD))


class Peers:
    """Boot del host como lo leería sysctl; health_boot basta para DarwinHealth."""
    def __init__(self, boot): self.value = boot
    def boot(self): return self.value
    def health_boot(self): return self.value, (1, 0)


class RuntimeAccreditation(Profiles):
    """service.Runtime: selfcheck una vez antes de servir y accredit con build, uid y boot canónico."""
    LOWER = 'abcdef01-2222-3333-4444-5555555555ab'  # Con letras: el selfcheck da minúsculas y sysctl mayúsculas.

    def runtime(self, mode='effective', spec=None, selfcheck='passed', build=BUILD, boot=LOWER.upper()):
        from hostgate.service import Runtime
        self.f.doc = copy.deepcopy(self.today)
        self.f.doc.update(mode=mode, storeId=mode + '-runtime-store')
        if spec is not None: self.f.doc['profiles'][NAME] = spec
        self.f.publish()
        calls = []
        def check():
            calls.append(1)
            return admin.SelfcheckPassed(self.LOWER, BUILD) if selfcheck == 'passed' else selfcheck
        coalition = object()
        runtime = Runtime(admin.load_config(self.f.locator), Peers(boot), _selfcheck=check, _build=lambda: build,
                          _coalition=coalition)
        self.assertIs(runtime.coalition, coalition)
        return runtime, calls

    def test_no_block_or_shadow_never_runs_selfcheck(self):
        """Variante: selfcheck_in_shadow."""
        for label, mode, spec in (('efectivo sin bloque', 'effective', None), ('sombra con bloque', 'shadow', block())):
            with self.subTest(label):
                runtime, calls = self.runtime(mode, spec)
                self.assertEqual(calls, [])
                self.assertIs(runtime.profile_verified(NAME, workload_uid=os.getuid()), False)
                self.assertEqual(runtime.closure(['p'], {}), ClosureEvidence())

    def test_failed_selfcheck_build_uid_or_boot_leave_unverified(self):
        """Variante: ninguna; cada discrepancia deja el perfil sin acreditar."""
        failed = admin.SelfcheckFailed('zombie', 'zombi contado como vivo')
        for label, kwargs, uid in (('selfcheck fallido', dict(selfcheck=failed), os.getuid()),
                                   ('build distinto', dict(build='25H001'), os.getuid()),
                                   ('uid distinto', {}, os.getuid() + 1), ('sin uid', {}, None),
                                   ('otro boot', dict(boot='99999999-2222-3333-4444-555555555555'), os.getuid())):
            with self.subTest(label):
                runtime, calls = self.runtime(spec=block(), **kwargs)
                self.assertEqual(calls, [1])
                self.assertIs(runtime.profile_verified(NAME, workload_uid=uid), False)

    def test_lowercase_selfcheck_boot_matches_uppercase_sysctl(self):
        """Variante: boot_raw_compare."""
        runtime, calls = self.runtime(spec=block())
        verdicts = [runtime.profile_verified(NAME, workload_uid=os.getuid()) for _ in range(3)]
        self.assertEqual(calls, [1])
        self.assertEqual({(type(v).__name__, getattr(v, 'boot', None), getattr(v, 'revision', None)) for v in verdicts},
                         {('Accredited', self.LOWER, runtime.config.profiles[NAME].revision)})
        self.assertEqual(runtime.closure(['p'], {}), ClosureEvidence())


if __name__ == '__main__':
    unittest.main()
