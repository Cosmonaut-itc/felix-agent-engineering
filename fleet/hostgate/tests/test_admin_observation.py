"""R1-C1: native success must include the observed security data."""
import ctypes as C
import errno
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from hostgate import admin
from hostgate.tests import test_admin as fixtures


def native_acl_regression(library):
    fault = C.CDLL(library, use_errno=True)
    fault.failing_fstatx.argtypes = [C.c_int, C.c_void_p, C.c_void_p]
    fault.failing_fstatx.restype = fault.get_failures.restype = C.c_int
    native = admin._call
    with tempfile.TemporaryDirectory() as td, fixtures.fixture_policy(Path(td).resolve()):
        fixture = fixtures.Fixture(Path(td).resolve()); fixture.provision()
        path = fixture.locator.config_path
        subprocess.run(['/bin/chmod', '-N', str(path)], check=True)
        assert admin.load_config(fixture.locator).node.revision.startswith('cfg_')
        print('NO_ACL_ACCEPTED', flush=True)
        subprocess.run(['/bin/chmod', '+a', 'everyone allow write,delete', str(path)], check=True)
        fd = os.open(path, os.O_RDONLY)
        try:
            acl = C.c_void_p(admin._read_acl(fd))
            try:
                for _ in range(16):
                    first, entry = C.c_void_p(), C.c_void_p()
                    assert native('acl_create_entry', C.c_int, [C.c_void_p, C.c_void_p], C.byref(acl), C.byref(entry)) == 0
                    assert native('acl_get_entry', C.c_int, [C.c_void_p, C.c_int, C.c_void_p], acl, 0, C.byref(first)) == 0
                    assert native('acl_copy_entry', C.c_int, [C.c_void_p, C.c_void_p], entry, first) == 0
                assert native('acl_set_fd_np', C.c_int, [C.c_int, C.c_void_p, C.c_int], fd, acl, 0x100) == 0
            finally:
                native('acl_free', C.c_int, [C.c_void_p], acl)
            listing = subprocess.check_output(['/bin/ls', '-le', str(path)], text=True)
            assert listing.count('allow write,delete') == 17, listing
            print('REAL_ACE_COUNT 17', flush=True)
            try: admin.load_config(fixture.locator)
            except admin.AdminError as exc: print('CONTROL_REJECTED', exc, flush=True)
            else: raise AssertionError('unsafe ACL control was accepted')
            before = path.read_bytes(), path.stat(), listing
            observed = []
            def injected(name, result, types, *args):
                if name == 'fstatx_np' and os.fstat(args[0]).st_ino == before[1].st_ino:
                    rc = fault.failing_fstatx(*args)
                    observed.append((rc, C.get_errno(), fault.get_failures()))
                    print('FSTATX_RESULT rc=%s errno=%s failures=%s' % observed[-1], flush=True)
                    return rc
                return native(name, result, types, *args)
            rejected = False
            with patch.object(admin, '_call', side_effect=injected):
                try: admin.load_config(fixture.locator)
                except admin.AdminError as exc:
                    rejected = True; print('FAULT_REJECTED', exc, flush=True)
                else: print('FAULT_ACCEPTED_UNSAFE_ACL', flush=True)
            assert observed and all(rc == 0 and err == errno.ENOMEM and n > 0 for rc, err, n in observed), observed
            assert before == (path.read_bytes(), path.stat(), subprocess.check_output(['/bin/ls', '-le', str(path)], text=True))
            print('UNCHANGED True', flush=True)
            assert rejected, 'ACL insegura aceptada tras fallo nativo de observacion'
        finally:
            os.close(fd)


class NativeObservations(unittest.TestCase):
    def test_real_fstatx_realloc_failure(self):
        with tempfile.TemporaryDirectory() as td:
            library = str(Path(td).resolve() / 'fault.dylib')
            subprocess.run(['/usr/bin/clang', '-dynamiclib', '-o', library,
                            str(Path(__file__).with_name('support') / 'acl_realloc_fault.c')], check=True)
            child = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), library],
                                   env=dict(os.environ, DYLD_INSERT_LIBRARIES=library),
                                   capture_output=True, text=True, timeout=30)
            print(child.stdout, end='', flush=True)
            self.assertEqual(child.returncode, 0, child.stdout + child.stderr)

    def test_native_success_without_outputs_is_rejected(self):
        with tempfile.TemporaryDirectory() as td, fixtures.fixture_policy(Path(td).resolve()):
            fixture = fixtures.Fixture(Path(td).resolve()); fixture.provision()
            native = admin._call
            for missing in ('fstatx_np', 'filesec_query_property', 'filesec_get_property',
                            'fstatfs64', 'sysctlbyname', 'gethostuuid'):
                with self.subTest(missing=missing):
                    def empty(name, result, types, *args):
                        return 0 if name == missing else native(name, result, types, *args)
                    before = fixture.locator.config_path.read_bytes()
                    with patch.object(admin, '_call', side_effect=empty):
                        with self.assertRaises(admin.AdminError): admin.load_config(fixture.locator)
                    self.assertEqual(fixture.locator.config_path.read_bytes(), before)

    def test_filesec_metadata_must_match_same_fd(self):
        with tempfile.TemporaryDirectory() as td, fixtures.fixture_policy(Path(td).resolve()):
            fixture = fixtures.Fixture(Path(td).resolve()); fixture.provision()
            native = admin._call
            for prop, ctype in ((1, C.c_uint32), (2, C.c_uint32), (4, C.c_uint16)):
                for missing in (False, True):
                    with self.subTest(property=prop, missing=missing):
                        def incoherent(name, result, types, *args):
                            if name == 'filesec_get_property' and args[1] == prop:
                                if missing: return 0
                                rc = native(name, result, types, *args)
                                C.cast(args[2], C.POINTER(ctype)).contents.value ^= 1
                                return rc
                            return native(name, result, types, *args)
                        with patch.object(admin, '_call', side_effect=incoherent):
                            with self.assertRaises(admin.AdminError): admin.load_config(fixture.locator)

    def test_acl_entry_outputs_and_stale_errno(self):
        with tempfile.TemporaryDirectory() as td, fixtures.fixture_policy(Path(td).resolve()):
            fixture = fixtures.Fixture(Path(td).resolve()); fixture.provision()
            native = admin._call
            def stale_errno(name, result, types, *args):
                rc = native(name, result, types, *args)
                if rc == 0: C.set_errno(errno.ENOMEM)
                return rc
            with patch.object(admin, '_call', side_effect=stale_errno):
                self.assertTrue(admin.load_config(fixture.locator).node.revision.startswith('cfg_'))
            path = fixture.locator.config_path
            subprocess.run(['/bin/chmod', '+a', 'everyone allow read', str(path)], check=True)
            self.assertTrue(admin.load_config(fixture.locator).node.revision.startswith('cfg_'))
            for missing in ('acl_get_entry', 'acl_get_tag_type', 'acl_get_permset_mask_np'):
                with self.subTest(missing=missing):
                    def empty(name, result, types, *args):
                        return 0 if name == missing else native(name, result, types, *args)
                    with patch.object(admin, '_call', side_effect=empty):
                        with self.assertRaises(admin.AdminError): admin.load_config(fixture.locator)


if __name__ == '__main__':
    native_acl_regression(sys.argv[1])
