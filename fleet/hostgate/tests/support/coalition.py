"""Kernel sustituido para darwin_coalition: procesos, contadores por coalición y barreras."""
import errno
from dataclasses import dataclass, replace
from hostgate.darwin_coalition import Birth, ReadError

BOOT = '11111111-2222-3333-4444-555555555555'
CID, OTHER = 3174, 9


@dataclass
class Proc:
    unique: int
    parent: int = 0
    coalition: int = CID
    uid: int = 501
    ruid: int = 501
    svuid: int = 501
    version: int = 7
    zombie: bool = False
    listed: bool = True


class World:
    """Cada add/exit mueve los contadores como el kernel: started al nacer, exited al salir."""
    def __init__(self):
        self.boot_value, self.procs, self.counters = BOOT, {}, {CID: [0, 0], OTHER: [0, 0]}
        self.errors, self.hooks, self.saturated, self.calls = {}, [], False, []
        self.add(100, 10)  # El dueño S.
        self.owner = Birth(BOOT, 100, 10)

    def add(self, pid, unique, **fields):
        proc = self.procs[pid] = Proc(unique, **fields)
        self.counters[proc.coalition][0] += 1
        return Birth(BOOT, pid, unique)

    def exit(self, pid, zombie=False):
        proc = self.procs[pid]
        self.counters[proc.coalition][1] += 1
        if zombie: self.procs[pid] = replace(proc, zombie=True)
        else: del self.procs[pid]

    def once(self, op, pid, action, nth=1):
        """Barrera de un solo disparo después de la lectura número nth de (op, pid)."""
        seen = [0]
        def hook(o, p):
            if (o, p) == (op, pid):
                seen[0] += 1
                if seen[0] == nth:
                    self.hooks.remove(hook)
                    action()
        self.hooks.append(hook)

    def _read(self, op, pid, value):
        self.calls.append((op, pid))
        if (op, pid) in self.errors: raise ReadError(self.errors[op, pid], op)  # None: tamaño inesperado.
        result = value()
        for hook in list(self.hooks): hook(op, pid)
        return result

    def _proc(self, pid):
        proc = self.procs.get(pid)
        if proc is None or proc.zombie: raise ReadError(errno.ESRCH, 'ESRCH')
        return proc

    def boot(self): return self._read('boot', None, lambda: self.boot_value)
    def usage(self, cid): return self._read('usage', cid, lambda: tuple(self.counters[cid]))
    def listpids(self):
        return self._read('list', None, lambda: ([p for p, x in sorted(self.procs.items()) if x.listed],
                                                 self.saturated))
    def unique(self, pid):
        return self._read('unique', pid, lambda: (self._proc(pid).unique, self._proc(pid).parent,
                                                  self._proc(pid).version))
    def coalition(self, pid): return self._read('coalition', pid, lambda: self._proc(pid).coalition)
    def short(self, pid):
        def value():
            p = self._proc(pid)
            return pid, p.uid, p.ruid, p.svuid, 2
        return self._read('short', pid, value)


class Clock:
    def __init__(self, step=0.0): self.now, self.step = 0.0, step
    def __call__(self):
        self.now += self.step
        return self.now
