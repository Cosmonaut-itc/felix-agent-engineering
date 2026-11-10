"""Selfcheck por boot del lector de coalición contra el kernel real (SEN-5650, 4b).

Desconectado: nada fuera de las pruebas lo llama; conectarlo es de 4c. Corre sin
privilegios y sin launchd, afirma sólo sobre las identidades que crea y no
duerme para ganar carreras: reintenta un número acotado de barridos. Todo
proceso que crea termina con él, también cuando falla.
"""
import ctypes as C
import errno
import os
import select
import signal
import time
from .admin import SelfcheckFailed, SelfcheckPassed
from .darwin_coalition import (Birth, Complete, CoalitionUnavailable, DarwinCoalition, Invalid, NOT_DIRECT_CHILD,
                               NOT_IN_COALITION, Occupied, ReadError, RootRejected, _Native)

ATTEMPTS = 500
WAIT = 10.0  # Plazo de cada espera de NOTE_EXIT, en segundos.


class _Failed(Exception):
    def __init__(self, detail):
        super().__init__(detail)
        self.detail = detail


def _darwin_build():
    libc = C.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    value, size = C.create_string_buffer(64), C.c_size_t(64)
    if libc.sysctlbyname(b'kern.osversion', value, C.byref(size), None, C.c_size_t(0)) != 0 or size.value < 2:
        raise _Failed('kern.osversion ilegible')
    return value.value.decode('ascii')


def _read_error(exc):
    return 'abi-size' if exc.errno is None else 'read-error errno %d' % exc.errno


def _gone(native, birth):
    """Sin identidad viva: ESRCH (también un zombi) o el pid ya es de otro."""
    try:
        return native.unique(birth.pid)[0] != birth.unique
    except ReadError as exc:
        if exc.errno == errno.ESRCH: return True
        raise


def _novel(obs):
    return {m.birth for m in obs.novel} if isinstance(obs, Occupied) else set()


def _await_exit(pids):
    """NOTE_EXIT de cada pid con plazo; no recoge. ESRCH al registrar ya es salida."""
    queue = select.kqueue()
    try:
        waiting = 0
        for pid in pids:
            try:
                queue.control([select.kevent(pid, select.KQ_FILTER_PROC, select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                             select.KQ_NOTE_EXIT)], 0)
                waiting += 1
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + WAIT
        while waiting:
            left = deadline - time.monotonic()
            events = queue.control(None, waiting, left) if left > 0 else []
            if not events: raise _Failed('plazo agotado esperando salida')
            waiting -= len(events)
    finally:
        queue.close()


class _Run:
    def __init__(self, native, created):
        self.native, self.created = native, created
        self.gate = DarwinCoalition(_native=native)
        self.children, self.births, self.fds = [], {}, []
        self.pending = []  # Pids propios sin identidad leída: sólo se esperan, nunca se terminan.
        self.step = 'boot'

    def pipe(self):
        r, w = os.pipe()  # No heredables: exec los cierra en los hijos.
        self.fds += [r, w]
        return r, w

    def close(self, fd):
        if fd in self.fds:
            self.fds.remove(fd)
            os.close(fd)

    def fork(self, label, child):
        pid = os.fork()
        if pid == 0:
            try:
                child()
            finally:
                os._exit(127)
        self.children.append(pid)
        if self.created: self.created(label, pid)
        return pid

    def own(self, label, pid):
        try:
            unique = self.native.unique(pid)
        except ReadError as exc:
            if exc.errno == errno.ESRCH: raise _Failed('%s ausente' % label)
            raise
        self.births[label] = Birth(self.boot, pid, unique[0])
        return unique

    def reap(self, pid):
        self.children.remove(pid)
        os.waitpid(pid, 0)

    def settle(self, baseline, accept):
        """Barridos hasta que accept da por buena una observación; Invalid falla."""
        last = None
        for _ in range(ATTEMPTS):
            obs = self.gate.observe(baseline)
            if isinstance(obs, Invalid): raise _Failed('barrido invalido: %s' % obs.reason)
            if isinstance(obs, (Complete, Occupied)) and accept(obs): return obs
            last = obs
        raise _Failed('reintentos agotados: %s' % getattr(last, 'reason', getattr(last, 'kind', None)))

    def run(self):
        n = self.native
        self.boot = n.boot()
        build = self.build()
        self.step = 'privileges'
        if os.geteuid() == 0 or os.getuid() != os.geteuid() or os.getgid() != os.getegid():
            raise _Failed('uid o gid elevado')
        self.step = 'abi'
        me = os.getpid()
        unique = n.unique(me)
        spid, uid, ruid, svuid, _ = n.short(me)
        started, exited = n.usage(n.coalition(me))
        if (spid, uid, ruid, svuid) != (me, os.getuid(), os.getuid(), os.getuid()) or started < exited:
            raise _Failed('lectura propia incoherente')
        owner = Birth(self.boot, me, unique[0])
        self.step = 'certify'
        baseline = self.certify(owner)
        self.step = 'blocked-child'
        r, w_blocked = self.pipe()
        blocked = self.fork('blocked', lambda: (os.close(w_blocked), os.read(r, 1)))
        self.close(r)
        self.own('blocked', blocked)
        root = self.gate.observe_root(owner, blocked)
        if root.birth != self.births['blocked'] or root.coalition != baseline.coalition or root.birth in baseline.births:
            raise _Failed('raiz no es el hijo creado')
        self.step = 'fork-exec'
        r, w_exec = self.pipe()
        execd = self.fork('exec', lambda: (os.dup2(r, 0), os.execv('/bin/cat', ['cat'])))
        self.close(r)
        before = self.own('exec', execd)
        self.step = 'orphan'
        r, w_orphan = self.pipe()
        report_r, report_w = self.pipe()

        def detach():
            os.setsid()
            pid = os.fork()
            if pid == 0:
                try:
                    os.dup2(r, 0)
                    os.execv('/bin/cat', ['cat'])
                finally:
                    os._exit(127)
            os.write(report_w, b'%d\n' % pid)
        middle = self.fork('middle', detach)
        self.close(r)
        self.close(report_w)
        self.reap(middle)
        text = os.read(report_r, 32)
        self.close(report_r)
        if not text.endswith(b'\n'): raise _Failed('huerfano sin pid')
        orphan = int(text)
        if self.created: self.created('orphan', orphan)
        self.pending.append(orphan)
        self.own('orphan', orphan)
        self.pending.remove(orphan)
        try:
            self.gate.observe_root(owner, orphan)
            raise _Failed('huerfano aceptado como raiz')
        except RootRejected as exc:
            if str(exc) != NOT_DIRECT_CHILD: raise _Failed('huerfano rechazado por %s' % exc)
        if n.coalition(orphan) != baseline.coalition: raise _Failed('huerfano fuera de la coalicion')
        self.step = 'zombie'
        r, w = self.pipe()
        zombie = self.fork('zombie', lambda: (os.close(w), os.read(r, 1)))
        self.close(r)
        zombie_birth = Birth(self.boot, zombie, self.own('zombie', zombie)[0])
        del self.births['zombie']
        queue = select.kqueue()
        try:
            queue.control([select.kevent(zombie, select.KQ_FILTER_PROC, select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                         select.KQ_NOTE_EXIT)], 0)
            self.close(w)
            if not queue.control(None, 1, WAIT): raise _Failed('zombi sin NOTE_EXIT')
        finally:
            queue.close()
        if zombie not in n.listpids()[0]: raise _Failed('zombi ya recogido')
        if not _gone(n, zombie_birth): raise _Failed('zombi legible como vivo')
        self.step = 'other-coalition'
        other = n.coalition(1)
        if not other or other == baseline.coalition: raise _Failed('coalicion de launchd no distinguible')
        try:
            self.gate.observe_root(owner, 1)
            raise _Failed('launchd aceptado como raiz')
        except RootRejected as exc:
            if str(exc) != NOT_IN_COALITION: raise _Failed('launchd rechazado por %s' % exc)
        self.step = 'occupied'
        own = set(self.births.values())
        obs = self.settle(baseline, lambda obs: own <= _novel(obs))
        if zombie_birth in _novel(obs): raise _Failed('zombi contado como vivo')
        if n.unique(execd)[0] != before[0]: raise _Failed('exec cambio la identidad')
        self.step = 'release'
        for fd in (w_blocked, w_exec, w_orphan): self.close(fd)
        _await_exit([blocked, execd, orphan])
        for pid in (blocked, execd, zombie): self.reap(pid)
        self.step = 'reaped'
        self.settle(baseline, lambda obs: not own & _novel(obs))
        alive = sorted(label for label, birth in self.births.items() if not _gone(n, birth))
        if alive: raise _Failed('identidad propia viva: %s' % ', '.join(alive))
        return SelfcheckPassed(self.boot, build)

    def certify(self, owner):
        last = None
        for _ in range(ATTEMPTS):
            try:
                return self.gate.certify(owner)
            except CoalitionUnavailable as exc:
                if isinstance(exc.observation, Invalid): raise _Failed('certify: %s' % exc.observation.reason)
                last = exc.observation.reason
        raise _Failed('certify: reintentos agotados: %s' % last)

    def cleanup(self):
        """Cierra tuberías, recoge a los hijos y termina sólo identidades propias revalidadas."""
        for fd in list(self.fds): self.close(fd)
        for pid in list(self.children):
            try:
                os.kill(pid, signal.SIGKILL)  # Hijo sin recoger: su pid no puede reciclarse.
            except ProcessLookupError:
                pass
            self.reap(pid)
        try:
            real = _Native()  # Revalida con el kernel, no con el acceso inyectado.
            awaited = list(self.pending)
            for birth in self.births.values():
                if not _gone(real, birth):
                    os.kill(birth.pid, signal.SIGKILL)
                    awaited.append(birth.pid)
            _await_exit(awaited)
            alive = sorted(label for label, birth in self.births.items() if not _gone(real, birth))
        except (OSError, _Failed) as exc:
            return 'limpieza sin revalidar: %s' % exc
        return 'identidad propia viva: %s' % ', '.join(alive) if alive else None


def selfcheck(*, _native=None, _build=None, _created=None):
    """SelfcheckPassed(boot, build) o SelfcheckFailed(step, detail); nunca un booleano."""
    try:
        run = _Run(_native if _native is not None else _Native(), _created)
    except ReadError as exc:
        return SelfcheckFailed('abi', _read_error(exc))
    run.build = _build or _darwin_build
    try:
        result = run.run()
    except _Failed as exc:
        result = SelfcheckFailed(run.step, exc.detail)
    except ReadError as exc:
        result = SelfcheckFailed(run.step, _read_error(exc))
    except RootRejected as exc:
        result = SelfcheckFailed(run.step, 'raiz rechazada: %s' % exc)
    except Exception as exc:
        result = SelfcheckFailed(run.step, '%s: %s' % (type(exc).__name__, exc))
    finally:
        leftover = run.cleanup()
    return SelfcheckFailed('cleanup', leftover) if leftover else result
