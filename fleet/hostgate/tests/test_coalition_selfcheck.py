"""Selfcheck por boot contra el kernel real, y nada conectado todavía (SEN-5650, 4b)."""
import ctypes
import errno
import importlib
import importlib.util
import os
import re
import select
import signal
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from hostgate import admin
from hostgate.darwin_coalition import ReadError, _Native
from hostgate.scope import ClosureEvidence, authorize_creation, record_started
from hostgate.service import Runtime

FLEET = Path(__file__).resolve().parents[2]


def selfcheck(**seams):
    """El veredicto, o la excepción como valor: en la base falla por aserción."""
    try:
        return importlib.import_module('hostgate.coalition_selfcheck').selfcheck(**seams)
    except Exception as exc:
        return exc


def shape(verdict):
    return type(verdict).__name__, getattr(verdict, 'step', None), getattr(verdict, 'detail', None)


class Faulty:
    """Acceso nativo real con un fallo inyectado sólo sobre las identidades que crea el selfcheck."""
    def __init__(self, fault):
        self.real, self.fault, self.pids, self.births, self.cache = _Native(), fault, {}, {}, {}
        self.flickered = set()

    def created(self, label, pid):
        self.pids[pid] = label
        try:
            self.births[label] = (pid, self.real.unique(pid)[0])
        except ReadError:
            pass  # El intermedio puede haber salido ya; lo recoge el selfcheck.

    def boot(self): return self.real.boot()
    def listpids(self): return self.real.listpids()

    def usage(self, cid):
        if self.fault == 'read-error' and 'blocked' in self.births: raise ReadError(errno.EIO, 'inyectado')
        return self.real.usage(cid)

    def _read(self, name, pid):
        label = self.pids.get(pid)
        if label and self.fault == 'abi-size': raise ReadError(None, 'inyectado')
        if label == 'orphan' and self.fault == 'orphan-missing': raise ReadError(errno.ESRCH, 'inyectado')
        try:
            value = getattr(self.real, name)(pid)
        except ReadError as exc:
            stale = {'zombie-alive': 'zombie', 'own-alive': 'exec'}.get(self.fault)
            if exc.errno == errno.ESRCH and label == stale and (name, pid) in self.cache:
                return self.cache[(name, pid)]  # Lectura vieja: la identidad parece seguir viva.
            raise
        self.cache[(name, pid)] = value
        if self.fault == 'flicker' and name == 'short' and label in ('blocked', 'orphan') \
                and label not in self.flickered:
            self.flickered.add(label)
            return tuple(value[:4]) + (value[4] % 4 + 1,)  # Otro estado válido, una sola vez: proceso que arranca.
        return value

    def unique(self, pid): return self._read('unique', pid)
    def coalition(self, pid): return self._read('coalition', pid)
    def short(self, pid): return self._read('short', pid)

    def alive(self):
        own = []
        for label, (pid, unique) in self.births.items():
            try:
                if self.real.unique(pid)[0] == unique: own.append(label)
            except ReadError as exc:
                if exc.errno != errno.ESRCH: raise
        return own


def exists(pid):
    try:
        return _Native().unique(pid) is not None
    except ReadError as exc:
        if exc.errno == errno.ESRCH: return False
        raise


def exited(pid):
    """NOTE_EXIT con plazo, sin recoger; ESRCH al registrar ya es salida."""
    queue = select.kqueue()
    try:
        queue.control([select.kevent(pid, select.KQ_FILTER_PROC, select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                     select.KQ_NOTE_EXIT)], 1, 10)
    except ProcessLookupError:
        pass
    finally:
        queue.close()


def end(native):
    """Termina y recoge lo que un defecto deja vivo; sin defecto, sólo recoge un hijo que quedó zombi."""
    for label, (pid, _) in native.births.items():
        if label in native.alive(): os.kill(pid, signal.SIGKILL)
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass


@unittest.skipUnless(sys.platform == 'darwin', 'kernel Darwin real')
class Kernel(unittest.TestCase):
    def test_selfcheck_passes_with_observed_boot_and_build(self):
        build = subprocess.run(['/usr/sbin/sysctl', '-n', 'kern.osversion'], stdout=subprocess.PIPE,
                               check=True, timeout=30).stdout.decode().strip()
        verdict = selfcheck()
        self.assertEqual(shape(verdict), ('SelfcheckPassed', None, None), verdict)
        self.assertEqual((verdict.boot, verdict.darwin_build), (_Native().boot(), build))

    def test_each_injected_fault_fails_with_its_reason_and_leaves_nothing_alive(self):
        """Variante: selfcheck_ignores_failure."""
        cases = {'abi-size': ('blocked-child', 'abi-size'),
                 'read-error': ('occupied', 'barrido invalido: read-error'),
                 'orphan-missing': ('orphan', 'orphan ausente'),
                 'zombie-alive': ('zombie', 'zombi legible como vivo'),
                 'own-alive': ('reaped', 'identidad propia viva: exec')}
        for fault, (step, detail) in cases.items():
            with self.subTest(fault=fault):
                native = Faulty(fault)
                verdict = selfcheck(_native=native, _created=native.created)
                self.assertEqual(shape(verdict), ('SelfcheckFailed', step, detail), verdict)
                self.assertEqual(native.alive(), [])

    def test_identity_that_settles_is_retried(self):
        """Variante: root_not_retried."""
        native = Faulty('flicker')
        self.assertEqual(shape(selfcheck(_native=native, _created=native.created)), ('SelfcheckPassed', None, None))
        self.assertEqual((native.flickered, native.alive()), ({'blocked', 'orphan'}, []))

    def test_unexpected_exception_and_build_failure_are_failed_results(self):
        def broken(): raise OSError(errno.EIO, 'sysctl')
        self.assertEqual(shape(selfcheck(_build=broken)), ('SelfcheckFailed', 'boot', 'OSError: [Errno 5] sysctl'))

    def test_native_init_failure_is_a_failed_result(self):
        """Variante: native_init_escapes."""
        for exc in (OSError(errno.EIO, 'dlopen'), AttributeError('proc_pidinfo')):
            with self.subTest(exc=type(exc).__name__), patch.object(ctypes, 'CDLL', side_effect=exc):
                self.assertEqual(shape(selfcheck()), ('SelfcheckFailed', 'abi', '%s: %s' % (type(exc).__name__, exc)))

    def test_failed_exec_fails(self):
        """Variante: exec_status_ignored."""
        execv = os.execv
        def failed(path, argv):
            os.closerange(3, 1024)  # Lo que exec cerraría; sin esto la tubería propia nunca da EOF.
            os.read(0, 1)
            execv('/nonexistent/cat', argv)
        native = Faulty('none')
        with patch.object(os, 'execv', failed):
            verdict = selfcheck(_native=native, _created=native.created)
        self.assertEqual(shape(verdict), ('SelfcheckFailed', 'release', 'exec sin exito: salida 127'))
        self.assertEqual(native.alive(), [])

    def test_failed_wait_in_cleanup_still_ends_every_child(self):
        """Variante: cleanup_stops_on_failure."""
        waitpid, waits, native = os.waitpid, [], Faulty('read-error')
        def wait(pid, flags):  # La primera espera recoge al intermedio; falla la primera de la limpieza.
            waits.append(pid)
            if len(waits) == 2: raise TimeoutError('inyectado')
            return waitpid(pid, flags)
        try:
            with patch.object(os, 'waitpid', wait):
                verdict = selfcheck(_native=native, _created=native.created)
            self.assertEqual(shape(verdict),
                             ('SelfcheckFailed', 'cleanup', 'limpieza incompleta: TimeoutError: inyectado'))
            self.assertEqual(native.alive(), [])
        finally:
            end(native)

    def test_orphan_ends_despite_failed_middle_wait_and_unreadable_identity(self):
        """Variante: orphan_registered_late. Variante: revalidate_in_batch."""
        waitpid, dup2, unique, sid = os.waitpid, os.dup2, _Native.unique, os.getsid(0)
        native, waits, (report, sink) = Faulty('none'), [], os.pipe()
        def stop(fd, target):  # Sólo el huérfano está en otra sesión: avisa su pid y se detiene.
            if os.getsid(0) != sid: os.write(sink, b'%d\n' % os.getpid()); os.kill(os.getpid(), signal.SIGSTOP)
            return dup2(fd, target)
        def wait(pid, flags):  # La espera del intermedio falla cuando ya salió: el huérfano existe.
            waits.append(pid)
            if len(waits) == 1: exited(pid); raise TimeoutError('inyectado')
            return waitpid(pid, flags)
        def unreadable(self, pid):  # El hijo bloqueado, ya recogido, no se puede revalidar.
            if pid in waits[1:] and pid == native.births['blocked'][0]: raise ReadError(errno.EIO, 'inyectado')
            return unique(self, pid)
        try:
            with patch.object(os, 'dup2', stop), patch.object(os, 'waitpid', wait), \
                    patch.object(_Native, 'unique', unreadable):
                verdict = selfcheck(_native=native, _created=native.created)
        finally:
            os.close(sink)
            orphans = [int(pid) for pid in os.read(report, 64).split()]
            os.close(report)
        try:
            self.assertEqual(shape(verdict),
                             ('SelfcheckFailed', 'cleanup', 'limpieza incompleta: ReadError: [Errno 5] inyectado'))
            self.assertEqual((native.alive(), [pid for pid in orphans if exists(pid)]), ([], []))
        finally:  # Con un defecto, el huérfano detenido sigue vivo: la prueba no lo deja.
            for pid in orphans:
                if exists(pid): os.kill(pid, signal.SIGKILL)
            end(native)


class NothingConnected(unittest.TestCase):
    def test_selfcheck_and_accreditation_exist_but_nothing_calls_them(self):
        module = importlib.util.find_spec('hostgate.coalition_selfcheck')
        self.assertTrue(module is not None and callable(getattr(admin, 'accredit', None)))
        calls = re.compile(r'(?<!def )\b(accredit|selfcheck)\(|coalition_selfcheck')
        callers = []
        for path in sorted(FLEET.rglob('*.py')):
            if 'tests' in path.relative_to(FLEET).parts: continue
            for number, line in enumerate(path.read_text(errors='replace').splitlines(), 1):
                if calls.search(line) and path.name != 'coalition_selfcheck.py':
                    callers.append('%s:%d' % (path.relative_to(FLEET), number))
        self.assertEqual(callers, [])
        runtime = Runtime.__new__(Runtime)
        runtime.synthetic = None
        self.assertIs(runtime.profile_verified('darwin-coalition-process@1'), False)
        self.assertEqual(runtime.closure(['p'], {}), ClosureEvidence())
        for call in (lambda: authorize_creation('k', None), lambda: record_started('k', None)):
            with self.assertRaises(NotImplementedError): call()


if __name__ == '__main__':
    unittest.main()
