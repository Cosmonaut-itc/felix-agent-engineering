"""Fixtures administrativas propias; nunca provisionan la autoridad instalada."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from hostgate.protocol import canonical, demand, validate


def document(root):
    import ctypes
    import uuid
    raw = (ctypes.c_ubyte * 16)()
    timeout = (ctypes.c_long * 2)(5, 0)
    assert ctypes.CDLL('/usr/lib/libSystem.B.dylib').gethostuuid(raw, timeout) == 0
    return dict(schema='fleet/hostgate-node@1', operatorRevision=1,
                        hardwareUuid=str(uuid.UUID(bytes=bytes(raw))), physicalHostId='fixture-host',
                        serviceUid=os.getuid(), serviceGid=os.getgid(), storeId='fixture-store-A',
                        stateDir=str(root / 'A'), mode='shadow', ncpu=int(subprocess.check_output(['/usr/sbin/sysctl', '-n', 'hw.ncpu'])),
                        budget=dict(cpuMilli=1000, memoryBytes=1000, diskBytes={'data': 1000}, slots=1),
                        volumes={'data': str(root)},
                        executors={'unix': dict(platform='darwin', scopeKind='unix', allowedUids=[os.getuid()])},
                        exclusiveResources=['sen-slot:1'],
                        principals={str(os.getuid()): dict(roles=['supervisor', 'query'], executors=['unix'])},
                        profiles={'fixture': dict(minimumDemand=dict(cpuMilli=500, memoryBytes=500,
                                                                    diskBytes={'data': 500}, slots=1))},
                        pendingTtlSec=20, heartbeatTtlSec=10, snapshotHost='Mini-Fixture',
                        healthSnapshotPath=str(root / 'health.json'), drained=False)



class Fixture:
    def __init__(self, root):
        from hostgate import admin
        self.root = root
        self.locator = admin.AuthorityLocator(*(root / p for p in
                                                ('node.json', 'authority.json', 'authority.lock', 'control.sock')))
        self.doc = document(root)

    def publish(self):
        self.locator.config_path.write_text(canonical(self.doc))
        self.locator.config_path.chmod(0o644)

    def provision(self):
        from hostgate import admin
        from hostgate.store import ShadowStore
        self.publish()
        anchor = {k: self.doc[k] for k in ('hardwareUuid', 'physicalHostId', 'storeId', 'stateDir', 'mode')}
        self.locator.anchor_path.write_text(canonical(dict(anchor, schema='fleet/hostgate-authority@1')))
        self.locator.anchor_path.chmod(0o444)
        self.locator.lock_path.touch(mode=0o440)
        ShadowStore.initialize(admin.load_config(self.locator).node)


def fixture_policy(root):
    """Seams privadas: propietario de fixture y ruta dentro de la copia remote-run.

    NodeConfig prohíbe checkouts y remote-run en producción. Sólo este harness
    sustituye esa comprobación; el cargador sigue comprobando cada fd/ACL/FS.
    """
    from contextlib import ExitStack
    from hostgate import admin
    stack = ExitStack()
    stack.enter_context(patch.object(admin, '_ADMIN_UID', os.getuid()))
    stack.enter_context(patch.object(admin, '_ADMIN_GID', os.getgid()))
    from hostgate.model import _validate_state_path
    def path_check(path):
        if root in path.parents and path.is_absolute(): return
        _validate_state_path(path)
    stack.enter_context(patch('hostgate.model._validate_state_path', side_effect=path_check))
    return stack


class Administration(unittest.TestCase):
    def setUp(self):
        from hostgate import admin
        self.admin = admin
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        policy = fixture_policy(self.root)
        self.addCleanup(policy.close)
        self.f = Fixture(self.root)
        self.f.provision()

    def load(self):
        return self.admin.load_config(self.f.locator)

    def inventory(self):
        return {str(p.relative_to(self.root)): (p.lstat().st_mode, p.lstat().st_ino,
                p.stat().st_size, p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
                for p in self.root.rglob('*') if p.is_file() and not p.is_symlink()}

    def rejected(self):
        before = self.inventory()
        with self.assertRaises(self.admin.AdminError):
            cfg = self.load()
            with self.admin.AuthorityLease(self.f.locator, cfg): pass
        self.assertEqual(self.inventory(), before)
        self.assertFalse(self.f.locator.socket_path.exists())

    def test_load_revision_immutable_maps_and_existing_serializers(self):
        cfg = self.load()
        self.assertEqual(cfg.node.revision, 'cfg_' + hashlib.sha256(canonical(self.f.doc).encode('ascii')).hexdigest())
        demand(dict(cfg.node.budget))
        self.assertEqual(json.loads(canonical(dict(cfg.node.budget))), self.f.doc['budget'])
        self.assertIs(copy.deepcopy(cfg.node.budget), cfg.node.budget)
        for target in (cfg.node.budget, cfg.node.budget['diskBytes'], cfg.principals[str(os.getuid())],
                       cfg.node.profile_minima['fixture']['diskBytes'], cfg.node.acl):
            with self.assertRaises(TypeError): target['x'] = 1
        with self.admin.AuthorityLease(self.f.locator, cfg) as lease: lease.check()

    def test_policy_content_digest_and_live_revision_guard(self):
        cfg = self.load()
        with self.admin.AuthorityLease(self.f.locator, cfg) as lease:
            self.f.doc['budget']['cpuMilli'] = 999
            self.f.publish()
            self.assertNotEqual(self.load().node.revision, cfg.node.revision)
            with self.assertRaises(self.admin.AdminError): lease.check()

    def test_p02_fixed_locator_and_wire_stays_closed(self):
        original = self.admin.local_authority()
        with patch.dict(os.environ, {k: str(self.root) for k in
                        ('HOME', 'XDG_STATE_HOME', 'FLEET_STATE_DIR', 'HOSTGATE_CONFIG', 'TMPDIR')}):
            self.assertEqual(self.admin.local_authority(), original)
        for field in ('budget', 'ncpu', 'acl', 'stateDir', 'mode', 'configRevision'):
            with self.assertRaises(ValueError):
                validate(dict(schema='fleet/host-reservation@1', operationId='q', operation='estado', **{field: 1}))

    def test_loaded_minima_unknown_volume_and_resource_use_existing_core(self):
        from hostgate.admission import Hostgate
        from hostgate.tests.support.world import World, request
        cfg = self.load(); world = World(); auth = world.auth(principal='uid:' + str(os.getuid()))
        gate = Hostgate(cfg.node, world)
        for changes in ({'demand': dict(cpuMilli=1, memoryBytes=1, diskBytes={'unknown': 1}, slots=0)},
                        {'exclusiveResources': ['unknown']}):
            denied = gate.reservar(request('bad-' + str(len(gate.store.read()['pending'])),
                                          physicalHostId='fixture-host', **changes), auth)
            self.assertFalse(denied['wouldGrant']); self.assertEqual(gate.store.read()['reservations'], [])
        result = gate.reservar(request('minimum', physicalHostId='fixture-host',
                              demand=dict(cpuMilli=1, memoryBytes=1, diskBytes={'data': 1}, slots=0)), auth)
        self.assertTrue(result['wouldGrant'], result)
        self.assertEqual(gate.store.read()['reservations'][0]['demand'], self.f.doc['profiles']['fixture']['minimumDemand'])

    def test_strict_document_and_catalogs(self):
        original = copy.deepcopy(self.f.doc)
        changes = [('extra', 1), ('mode', 'enforcing'), ('ncpu', True), ('budget', dict(original['budget'], slots=True)),
                   ('hardwareUuid', '00000000-0000-0000-0000-000000000001'), ('serviceUid', os.getuid() + 1),
                   ('volumes', {}), ('stateDir', str(self.root / '..' / 'other')),
                   ('executors', {'unix': dict(platform='windows', scopeKind='unix', allowedUids=[os.getuid()])}),
                   ('principals', {str(os.getuid()): dict(roles=['operator'], executors=['unix'])})]
        for field, value in changes:
            with self.subTest(field=field, value=value):
                self.f.doc = dict(original, **{field: value}); self.f.publish(); self.rejected()
        self.f.doc = original
        for raw in ('{"schema":1,"schema":2}', '{"x":NaN}', '{"x":Infinity}', ' ' * 65537):
            self.f.locator.config_path.write_text(raw); self.rejected()

    def test_real_permissions_links_acl_and_failed_observations(self):
        path = self.f.locator.config_path
        path.chmod(0o664); self.rejected(); path.chmod(0o644)
        self.root.chmod(0o720); self.rejected(); self.root.chmod(0o700)
        other = self.root / 'hardlink'; os.link(str(path), str(other))
        self.rejected(); other.unlink()
        path.rename(other); path.symlink_to(other)
        self.rejected(); path.unlink(); other.rename(path)
        alias = self.root / 'alias'; alias.symlink_to(self.root, target_is_directory=True)
        with patch.object(self.f, 'locator', self.admin.AuthorityLocator(alias / 'node.json',
                          self.f.locator.anchor_path, self.f.locator.lock_path, self.f.locator.socket_path)):
            self.rejected()
        alias.unlink()
        subprocess.run(['/bin/chmod', '+a', 'everyone allow write,delete', str(path)], check=True)
        try: self.rejected()
        finally: subprocess.run(['/bin/chmod', '-N', str(path)], check=True)
        for name, value in (('_ADMIN_UID', os.getuid() + 1),):
            with patch.object(self.admin, name, value): self.rejected()
        for name in ('_check_filesystem', '_check_acl'):
            with patch.object(self.admin, name, side_effect=OSError('observation unavailable/remote FS')): self.rejected()
        native = self.admin._call
        def remote(name, result, types, *args):
            if name == 'fstatfs64': return 0  # buffer vacío: no MNT_LOCAL ni APFS
            return native(name, result, types, *args)
        with patch.object(self.admin, '_call', side_effect=remote): self.rejected()

    def test_anchor_other_store_and_reduced_budget_preserve_debits(self):
        from hostgate.admission import Hostgate
        from hostgate.store import ShadowStore
        from hostgate.tests.support.world import World, request
        cfg = self.load(); world = World(); auth = world.auth(principal='uid:' + str(os.getuid()))
        gate = Hostgate(cfg.node, world)
        with self.admin.AuthorityLease(self.f.locator, cfg):
            granted = gate.reservar(request(physicalHostId='fixture-host', exclusiveResources=['sen-slot:1']), auth)
            self.assertTrue(granted['wouldGrant'], granted)
        held = gate.store.read()
        original = copy.deepcopy(self.f.doc)
        for store_id in ('fixture-store-B', 'fixture-store-A'):
            self.f.doc = dict(original, storeId=store_id, stateDir=str(self.root / ('B-' + store_id)))
            self.f.publish(); ShadowStore.initialize(self.load().node)
            with patch.object(ShadowStore, 'connect', side_effect=AssertionError('B must not be opened')):
                self.rejected()
        self.f.doc = dict(original, physicalHostId='another-host'); self.f.publish(); self.rejected()
        self.f.doc = dict(original, operatorRevision=2, budget=dict(original['budget'], cpuMilli=100))
        self.f.publish(); reduced = self.load()
        with self.admin.AuthorityLease(self.f.locator, reduced):
            self.assertEqual(gate.store.read(), held)
            world.channel = False
            result = Hostgate(reduced.node, world).estado({}, auth)
            self.assertEqual(result['budget']['available']['cpuMilli'], -900)
            self.assertEqual(result['budget']['retained']['slots'], 1)
            self.assertEqual(result['resources'][0]['name'], 'sen-slot:1')
            self.assertEqual(result['reservations'][0]['effectiveState'], 'uncertain')
        self.f.locator.anchor_path.unlink(); self.rejected()
        self.f.locator.anchor_path.write_text('{}'); self.f.locator.anchor_path.chmod(0o444); self.rejected()

    def test_lease_competition_replacement_and_missing_ledger(self):
        from hostgate.store import Busy
        cfg = self.load()
        with self.admin.AuthorityLease(self.f.locator, cfg) as first:
            before = self.inventory()
            with self.assertRaises(Busy):
                with self.admin.AuthorityLease(self.f.locator, cfg): pass
            self.assertEqual(before, self.inventory()); first.check()
            probe = """
import sys
from pathlib import Path
from hostgate.tests.test_admin import Fixture, fixture_policy
from hostgate import admin
from hostgate.store import Busy
with fixture_policy(Path(sys.argv[1])):
    f = Fixture(Path(sys.argv[1]))
    try:
        with admin.AuthorityLease(f.locator, admin.load_config(f.locator)): pass
    except Busy: sys.exit(4)
"""
            child = subprocess.run([sys.executable, '-B', '-c', probe, str(self.root)], capture_output=True, timeout=20)
            self.assertEqual(child.returncode, 4, child.stderr)
            self.assertEqual(before, self.inventory()); first.check()
            lock = self.f.locator.lock_path
            lock.rename(self.root / 'old-lock'); lock.touch(mode=0o440)
            with self.assertRaises(self.admin.AdminError): first.check()
        with self.admin.AuthorityLease(self.f.locator, cfg) as lease:
            db = cfg.node.state_dir / 'shadow.db'
            db.rename(db.with_name('old.db')); db.write_bytes(db.with_name('old.db').read_bytes()); db.chmod(0o600)
            with self.assertRaises(self.admin.AdminError): lease.check()
        db.unlink(); self.rejected()

    def test_live_config_anchor_and_directory_replacement_are_detected(self):
        for path in (self.f.locator.config_path, self.f.locator.anchor_path, self.root / 'A'):
            with self.subTest(path=path):
                with self.admin.AuthorityLease(self.f.locator, self.load()) as lease:
                    moved = path.with_name(path.name + '-old'); path.rename(moved)
                    try:
                        path.symlink_to(moved, target_is_directory=moved.is_dir())
                        with self.assertRaises(self.admin.AdminError): lease.check()
                    finally: path.unlink(); moved.rename(path)


def comparison(base):
    """El booleano de matriz elige base/candidato, nunca una excepción de import."""
    root = Path(tempfile.mkdtemp()).resolve()
    try:
        if base:
            # Documento canónico fijo: no depende del cargador ni de código nuevo.
            doc = document(root)
            (root / 'node.json').write_text(canonical(doc))
            request = dict(schema='fleet/host-reservation@1', operationId='comparison', operation='estado')
            command = [sys.executable, '-B', '-m', 'hostgate', 'estado']
            proc = subprocess.run(command, input=json.dumps(request), text=True, capture_output=True, timeout=15)
            print('COMMAND', command, 'STDIN', request, 'STDOUT', proc.stdout, 'RC', proc.returncode, flush=True)
            facts = json.loads(proc.stdout); revision = facts['configRevision']
            assert proc.returncode == 5 and facts['reasons'][0]['code'] == 'enforcement-unavailable'
        else:
            from hostgate import admin
            with fixture_policy(root):
                f = Fixture(root); f.provision(); doc = json.loads(f.locator.config_path.read_text())
                cfg = admin.load_config(f.locator)
                with admin.AuthorityLease(f.locator, cfg):
                    revision = cfg.node.revision
                    facts = dict(physicalHostId=cfg.node.host_id, storeId=cfg.node.store_id, capacity=dict(cfg.node.budget))
        expected = 'cfg_' + hashlib.sha256(canonical(doc).encode('ascii')).hexdigest()
        print('REVISION_OPERANDS', revision, expected, flush=True)
        assert revision == expected, (revision, expected)
        assert facts['physicalHostId'] == 'fixture-host' and facts['storeId'] == 'fixture-store-A'
        assert facts['capacity']['slots'] == 1
    finally:
        import shutil
        shutil.rmtree(root)
