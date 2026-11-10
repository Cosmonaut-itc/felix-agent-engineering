"""SEN-5650 4a: lector de coalición. Reglas con kernel sustituido; kernel real en la
coalición compartida de remote-run; kernel real en coalición propia (HOSTGATE_OWN_COALITION=1).

Cada docstring «Variante: x» nombra la variante defectuosa que esa prueba debe matar."""
import collections
import ctypes as C
import errno
import json
import os
import select
import signal
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from hostgate import darwin_coalition as dc
from hostgate.darwin_coalition import (Baseline, Birth, Complete, CoalitionUnavailable, DarwinCoalition,
                                       Invalid, Member, Occupied, ReadError, Retry, RootRejected)
from hostgate.protocol import canonical
from hostgate.tests.support.coalition import BOOT, CID, OTHER, Clock, World

FLEET = Path(__file__).resolve().parents[2]


class Rules(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.add(200, 20)  # P: miembro ajeno de la línea base.
        self.gate = DarwinCoalition(_native=self.w, _clock=Clock())
        self.base = self.gate.certify(self.w.owner)

    def observe(self): return self.gate.observe(self.base)

    def assertComplete(self, vanished=0):
        obs = self.observe()
        self.assertIsInstance(obs, Complete)
        self.assertEqual(obs.vanished, vanished)
        return obs

    def assertNovel(self, *births):
        obs = self.observe()
        self.assertIsInstance(obs, Occupied)
        self.assertEqual([m.birth for m in obs.novel], list(births))
        return obs

    def test_certify_is_exact_and_owner_is_member(self):
        self.w.add(500, 50, coalition=OTHER)
        base = self.gate.certify(self.w.owner)
        self.assertEqual(base.births, {self.w.owner, Birth(BOOT, 200, 20)})
        self.assertEqual((base.boot, base.coalition, base.counters), (BOOT, CID, (2, 0)))
        self.assertEqual(self.assertComplete().members, 2)

    def test_new_member_occupies(self):
        """Variante: omit_new_birth."""
        self.assertComplete()
        child = self.w.add(300, 30, parent=10)
        obs = self.assertNovel(child)
        self.assertEqual(obs.novel[0], Member(child, 501, 501, 501, 10, 7))
        self.assertFalse(obs.novel[0].elevated)

    def test_observe_never_refreshes_baseline(self):
        """Variante: refresh_baseline."""
        text = self.base.to_json()
        child = self.w.add(300, 30)
        self.assertNovel(child)
        self.assertNovel(child)
        self.assertEqual(self.base.to_json(), text)
        self.w.exit(300)
        self.assertComplete()

    def test_child_of_baseline_member_retains(self):
        """Variante: subtract_foreign_descendants."""
        self.assertComplete()
        child = self.w.add(300, 30, parent=20)
        self.assertNovel(child)
        self.w.exit(200)  # El hijo queda huérfano de un miembro ajeno: sigue reteniendo.
        self.assertNovel(child)

    def test_reused_pid_is_novelty(self):
        """Variante: pid_only_identity."""
        self.assertComplete()
        self.w.exit(200)
        self.assertComplete()
        self.assertNovel(self.w.add(200, 99))

    def test_counters_change_during_scan(self):
        """Variante: compare_live_count_only. C12: P ya leído hace fork y sale; el recuento vivo no cambia."""
        self.assertComplete()
        def fork_and_exit():
            self.w.add(300, 30, parent=20)
            self.w.exit(200)
        self.w.once('unique', 200, fork_and_exit, nth=2)
        self.assertEqual(self.observe(), Retry(dc.COUNTERS_CHANGED))
        self.assertNovel(Birth(BOOT, 300, 30))

    def test_exit_during_scan_changes_counters(self):
        """Variante: compare_started_only. Sólo cambia tasks_exited durante el barrido."""
        self.w.add(300, 30)
        self.assertNovel(Birth(BOOT, 300, 30))
        self.w.once('list', None, lambda: self.w.exit(300, zombie=True))
        self.assertEqual(self.observe(), Retry(dc.COUNTERS_CHANGED))
        self.assertComplete(vanished=1)

    def test_esrch_without_concordance(self):
        """Variante: esrch_means_empty."""
        self.w.add(300, 30)
        self.w.exit(300, zombie=True)
        self.assertComplete(vanished=1)
        self.w.add(400, 40)
        self.w.errors['unique', 400] = errno.ESRCH  # Vivo según los contadores, ilegible.
        self.assertEqual(self.observe(), Retry(dc.CARDINALITY))

    def test_cardinality_mismatch_retries(self):
        """Variante: skip_member_count."""
        self.assertComplete()
        self.w.add(300, 30, listed=False)
        self.assertEqual(self.observe(), Retry(dc.CARDINALITY))
        self.w.procs[300].listed = True
        self.w.counters[CID][1] += 1  # Más miembros legibles que tareas vivas.
        self.assertEqual(self.observe(), Retry(dc.CARDINALITY))

    def test_saturated_list_retries(self):
        """Variante: ignore_truncation. La lista saturada omite un hijo vivo con contadores estables."""
        self.assertComplete()
        self.w.add(300, 30, listed=False)
        self.w.saturated = True
        self.assertEqual(self.observe(), Retry(dc.SATURATED))

    def test_identity_change_between_reads_retries(self):
        self.w.add(300, 30)
        self.assertNovel(Birth(BOOT, 300, 30))
        def reuse():
            self.w.exit(300)
            self.w.add(300, 31)
        self.w.once('coalition', 300, reuse)
        self.assertEqual(self.observe(), Retry(dc.IDENTITY_CHANGED))

    def test_non_esrch_error_is_invalid(self):
        """Variante: unknown_abi_is_empty."""
        self.w.add(500, 50, coalition=OTHER)
        self.assertComplete()
        for (op, pid), code, expected in ((('coalition', 500), errno.EPERM, Invalid(dc.READ_ERROR, errno.EPERM)),
                                          (('short', 200), None, Invalid(dc.ABI_SIZE)),
                                          (('usage', CID), errno.ESRCH, Invalid(dc.READ_ERROR, errno.ESRCH)),
                                          (('list', None), errno.ENOMEM, Invalid(dc.READ_ERROR, errno.ENOMEM))):
            with self.subTest(op=op):
                self.w.errors = {(op, pid): code}
                self.assertEqual(self.observe(), expected)

    def test_elevated_member_is_reported(self):
        plain = self.w.add(300, 30)
        self.assertFalse(self.assertNovel(plain).novel[0].elevated)
        setuid, saved = self.w.add(301, 31, uid=0), self.w.add(302, 32, svuid=0)
        obs = self.assertNovel(plain, setuid, saved)
        self.assertEqual([m.elevated for m in obs.novel], [False, True, True])

    def test_boot_coalition_and_counters_lost_are_invalid(self):
        self.assertComplete()
        self.w.boot_value = '99999999-2222-3333-4444-555555555555'
        self.assertEqual(self.observe(), Invalid(dc.BOOT_CHANGED))
        self.w.boot_value = BOOT
        self.w.once('list', None, lambda: setattr(self.w, 'boot_value', '99999999-2222-3333-4444-555555555555'))
        self.assertEqual(self.observe(), Invalid(dc.BOOT_CHANGED))
        self.w.boot_value = BOOT
        self.w.procs[200].coalition = OTHER
        self.assertEqual(self.observe(), Invalid(dc.COALITION_CHANGED))
        self.w.procs[200].coalition = CID
        self.w.counters[CID] = [1, 0]
        del self.w.procs[200]
        self.assertEqual(self.observe(), Invalid(dc.COUNTERS_INCOHERENT))
        self.w.counters[CID] = [1, 2]
        self.assertEqual(self.observe(), Invalid(dc.COUNTERS_INCOHERENT))

    def test_certify_rejects_lost_owner_null_coalition_and_unproven_scan(self):
        cases = (('boot', lambda w: setattr(w, 'boot_value', '99999999-2222-3333-4444-555555555555'),
                  Invalid(dc.BOOT_CHANGED)),
                 ('null', lambda w: setattr(w.procs[100], 'coalition', 0), Invalid(dc.COALITION_NULL)),
                 ('owner', lambda w: setattr(w.procs[100], 'unique', 11), Invalid(dc.OWNER_MISMATCH)),
                 ('moved', lambda w: w.once('list', None, lambda: setattr(w.procs[100], 'coalition', OTHER)),
                  Invalid(dc.COALITION_CHANGED)),
                 ('saturated', lambda w: setattr(w, 'saturated', True), Retry(dc.SATURATED)))
        for name, damage, expected in cases:
            with self.subTest(name):
                w = World()
                DarwinCoalition(_native=w).certify(w.owner)
                damage(w)
                with self.assertRaises(CoalitionUnavailable) as caught:
                    DarwinCoalition(_native=w).certify(w.owner)
                self.assertEqual(caught.exception.observation, expected)

    def test_deadline_retries(self):
        self.assertIsInstance(DarwinCoalition(_native=self.w, _clock=Clock(0.001)).observe(self.base), Complete)
        slow = DarwinCoalition(_native=self.w, _clock=Clock(0.03))
        self.assertEqual(slow.observe(self.base), Retry(dc.DEADLINE))

    def test_baseline_roundtrip_and_every_alteration_fails(self):
        text = self.base.to_json()
        self.assertEqual(Baseline.from_json(text), self.base)
        def edit(change, rehash=True):
            value = json.loads(text)
            change(value)
            if rehash:
                value['digest'] = dc.digest({k: v for k, v in value.items() if k != 'digest'})
            return canonical(value)
        self.assertEqual(Baseline.from_json(edit(lambda v: None)), self.base)
        altered = [text.replace(',', ', ', 1), text.replace('}', '} ', 1), text[:-1] + ',"x":1}',
                   text.replace('"v":1', '"v":1,"v":1'), edit(lambda v: v['members'][0].update(uid=0), rehash=False),
                   edit(lambda v: v['members'].pop()), edit(lambda v: v['members'].reverse()),
                   edit(lambda v: v['members'].append(v['members'][0])), edit(lambda v: v.update(v=2)),
                   edit(lambda v: v.update(v=True)), edit(lambda v: v.update(coalition=0)),
                   edit(lambda v: v.update(boot=BOOT.replace('5', 'A'))), edit(lambda v: v.update(counters=[2, 0, 0])),
                   edit(lambda v: v.update(counters=[2, 1])), edit(lambda v: v['members'][0].update(pid=True)),
                   edit(lambda v: v['members'][0].update(uid='501')), edit(lambda v: v['members'][0].update(x=1)),
                   edit(lambda v: v['members'][0].update(unique=0)), edit(lambda v: v.update(members=[]))]
        for index, bad in enumerate(altered):
            with self.subTest(index=index), self.assertRaises(ValueError):
                Baseline.from_json(bad)
        for field, value in (('coalition', 0), ('digest', '0' * 64), ('counters', (3, 0)), ('members', ())):
            with self.subTest(field=field), self.assertRaises(ValueError):
                replace(self.base, **{field: value})

    def test_baseline_over_limit_is_rejected_not_trimmed(self):
        for extra, ok in ((dc.MAX_MEMBERS - 1, True), (dc.MAX_MEMBERS, False)):
            with self.subTest(members=extra + 1):
                w = World()
                for i in range(extra):
                    w.add(1000 + i, 1000 + i)
                if ok:
                    self.assertEqual(len(DarwinCoalition(_native=w).certify(w.owner).members), dc.MAX_MEMBERS)
                    continue
                with self.assertRaises(CoalitionUnavailable) as caught:
                    DarwinCoalition(_native=w).certify(w.owner)
                self.assertEqual(caught.exception.observation, Invalid(dc.TOO_LARGE))

    def test_observe_root_rules(self):
        child = self.w.add(300, 30, parent=10)
        root = self.gate.observe_root(self.w.owner, 300)
        self.assertEqual((root.birth, root.coalition, root.parent_unique), (child, CID, 10))
        self.w.add(301, 31, parent=30)
        self.w.add(302, 32, parent=10, coalition=OTHER)
        self.w.add(303, 33, parent=10)
        self.w.exit(303, zombie=True)
        cases = [(301, None, dc.NOT_DIRECT_CHILD), (302, None, dc.NOT_IN_COALITION), (303, None, dc.READ_ERROR),
                 (300, lambda: self.w.once('short', 300, lambda: setattr(self.w.procs[300], 'unique', 39)),
                  dc.IDENTITY_CHANGED),
                 (300, lambda: self.w.once('short', 300, lambda: setattr(self.w, 'boot_value', 'x'), nth=2),
                  dc.BOOT_CHANGED)]
        for pid, setup, reason in cases:
            with self.subTest(pid=pid, reason=reason):
                self.w.boot_value, self.w.procs[300].unique = BOOT, 30
                if setup: setup()
                with self.assertRaises(RootRejected) as caught:
                    self.gate.observe_root(self.w.owner, pid)
                self.assertEqual(caught.exception.args[0], reason)
        self.w.boot_value = BOOT
        with self.assertRaises(RootRejected) as caught:
            self.gate.observe_root(Birth(BOOT, 100, 11), 300)
        self.assertEqual(caught.exception.args[0], dc.OWNER_MISMATCH)

    def test_runtime_and_fleet_do_not_import_the_reader(self):
        code = ('import sys\nimport hostgate.service, hostgate.cli, hostgate.client, hostgate.admin\n'
                'import hostgate.admission, hostgate.scope, hostgate.ipc, hostgate.store\n'
                'import fleet.cli, fleet.host_reservation\n'
                'print(sorted(m for m in sys.modules if m.startswith(("hostgate", "fleet"))))')
        done = subprocess.run([sys.executable, '-B', '-c', code], env=dict(os.environ, PYTHONPATH=str(FLEET)),
                              cwd=str(FLEET), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        loaded = done.stdout.decode()
        self.assertIn("'hostgate.service'", loaded)
        self.assertNotIn('darwin_coalition', loaded)
        mine = {Path(dc.__file__).resolve(), Path(__file__).resolve(), FLEET / 'hostgate/tests/support/coalition.py'}
        # 4b: las formas del permiso releen Baseline/RootIdentity con import diferido, sus pruebas las plantan.
        mine |= {FLEET / 'hostgate/scope.py', FLEET / 'hostgate/tests/test_creation_forms.py'}
        users = [str(p) for p in FLEET.rglob('*.py')
                 if p.resolve() not in mine and 'darwin_coalition' in p.read_text(errors='replace')]
        self.assertEqual(users, [])


class Own:
    """Procesos que la prueba creó, por identidad exacta. Sólo a ellos se termina, revalidados."""
    def __init__(self, native, boot):
        self.native, self.boot, self.births, self.children, self.popens = native, boot, [], [], []

    def birth(self, pid):
        birth = Birth(self.boot, pid, self.native.unique(pid)[0])
        self.births.append(birth)
        return birth

    def alive(self, birth):
        try:
            return self.native.unique(birth.pid)[0] == birth.unique
        except ReadError as exc:
            if exc.errno != errno.ESRCH: raise
            return False

    def terminate(self, birth):
        """SIGKILL a la identidad exacta y espera NOTE_EXIT; no recoge (eso es del padre)."""
        queue = select.kqueue()
        try:
            try:
                queue.control([select.kevent(birth.pid, select.KQ_FILTER_PROC,
                                             select.KQ_EV_ADD | select.KQ_EV_ONESHOT, select.KQ_NOTE_EXIT)], 0)
            except ProcessLookupError:
                return
            if not self.alive(birth): return
            os.kill(birth.pid, signal.SIGKILL)
            if not queue.control(None, 1, 10): raise AssertionError('sin NOTE_EXIT de %r' % (birth,))
        finally:
            queue.close()

    def reap(self, pid):
        self.children.remove(pid)
        os.waitpid(pid, 0)

    def close(self):
        for birth in self.births: self.terminate(birth)
        for pid in list(self.children): self.reap(pid)
        for popen in self.popens:
            if popen.poll() is None: popen.kill()
            popen.wait()
            for stream in (popen.stdin, popen.stdout, popen.stderr):
                if stream: stream.close()
        return [b for b in self.births if self.alive(b)]


DAEMON = ('import os,sys\nif os.fork(): os._exit(0)\nos.setsid()\nif os.fork(): os._exit(0)\n'
          'os.write(1,b"%d\\n"%os.getpid())\nd=os.open("/dev/null",os.O_RDWR)\n'
          '[os.dup2(d,f) for f in (0,1,2)]\nos.execve("/bin/sleep",["sleep","60"],{})')
FORK_AFTER_LIST = ('import os\nos.write(1,b"ready\\n")\nos.read(0,1)\nc=os.fork()\n'
                   'if c: os.write(1,b"%d\\n"%c); os._exit(0)\nos.execve("/bin/sleep",["sleep","60"],{})')
NESTED = ('import os,subprocess\np=subprocess.Popen(["/bin/sleep","60"])\nos.write(1,b"%d\\n"%p.pid)\n'
          'os.read(0,1)\np.kill(); p.wait()')


class Kernel(unittest.TestCase):
    ATTEMPTS = 500

    def setUp(self):
        self.gate = DarwinCoalition()
        self.native = self.gate._native
        boot = self.native.boot()
        self.owner = Birth(boot, os.getpid(), self.native.unique(os.getpid())[0])
        self.own = Own(self.native, boot)
        self.addCleanup(lambda: self.assertEqual(self.own.close(), []))
        self.retries = collections.Counter()

    def certify(self):
        for _ in range(self.ATTEMPTS):
            try:
                return self.gate.certify(self.owner)
            except CoalitionUnavailable as exc:
                self.assertIsInstance(exc.observation, Retry)
                self.retries[exc.observation.reason] += 1
        self.fail('certify sin barrido aceptado: %r' % self.retries)

    def settle(self, baseline):
        """Primera observación aceptada. Invalid falla; Retry reintenta sin dormir."""
        for _ in range(self.ATTEMPTS):
            obs = self.gate.observe(baseline)
            self.assertNotIsInstance(obs, Invalid)
            if isinstance(obs, (Complete, Occupied)): return obs
            self.retries[obs.reason] += 1
        self.fail('sin observación aceptada: %r' % self.retries)

    def novel(self, obs):
        return {m.birth for m in obs.novel} if isinstance(obs, Occupied) else set()

    def report(self, label, obs, births):
        """Diagnóstico de novedad identificable: cada novedad se clasifica contra lo creado aquí."""
        novel = self.novel(obs)
        print('KERNEL', label, json.dumps({'kind': obs.kind, 'propias': len(novel & set(births)),
                                           'ajenas': len(novel - set(births)), 'retries': self.retries}),
              file=sys.stderr, flush=True)

    def fork(self, then):
        pid = os.fork()
        if pid == 0:
            try:
                then()
            finally:
                os._exit(127)
        self.own.children.append(pid)
        return pid

    def popen(self, code, **kw):
        popen = subprocess.Popen([sys.executable, '-B', '-c', code], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, **kw)
        self.own.popens.append(popen)
        return popen


@unittest.skipUnless(sys.platform == 'darwin', 'kernel Darwin real')
class SharedCoalition(Kernel):
    def test_certify_returns_whole_baseline_with_test_process(self):
        base = self.certify()
        self.assertIn(self.owner, base.births)
        self.assertEqual(base.coalition, self.native.coalition(os.getpid()))
        self.assertEqual(len(base.members), base.counters[0] - base.counters[1])
        self.assertEqual(Baseline.from_json(base.to_json()), base)

    def test_fork_spawn_exec_and_daemon_are_novel_until_terminated(self):
        base = self.certify()
        r, w = os.pipe()
        self.addCleanup(os.close, w)
        forked = self.fork(lambda: (os.close(w), os.read(r, 1)))
        os.close(r)
        spawned = os.posix_spawn('/bin/sleep', ['sleep', '60'], {})
        self.own.children.append(spawned)
        execd = self.fork(lambda: os.execv('/bin/sleep', ['sleep', '60']))
        direct = [self.own.birth(pid) for pid in (forked, spawned, execd)]
        intermediate = self.popen(DAEMON)
        daemon = self.own.birth(int(intermediate.stdout.readline()))
        self.assertEqual(intermediate.wait(), 0)
        for birth in direct:
            self.assertEqual(self.native.unique(birth.pid)[1], self.owner.unique)
        self.assertEqual(self.native.coalition(daemon.pid), base.coalition)
        obs = self.settle(base)
        self.report('creados', obs, direct + [daemon])
        self.assertLessEqual(set(direct + [daemon]), self.novel(obs))
        for birth in direct + [daemon]: self.own.terminate(birth)
        for pid in (forked, spawned, execd): self.own.reap(pid)
        obs = self.settle(base)
        self.report('terminados', obs, direct + [daemon])
        self.assertFalse(set(direct + [daemon]) & self.novel(obs))

    def test_c12_fork_behind_listing_forces_retry(self):
        base = self.certify()
        parent = self.popen(FORK_AFTER_LIST)
        self.assertEqual(parent.stdout.readline(), b'ready\n')
        born = []

        class Barrier(dc._Native):
            def listpids(inner):
                pids, saturated = dc._Native.listpids(inner)
                if not born:
                    self.assertIn(parent.pid, pids)
                    parent.stdin.write(b'x'); parent.stdin.flush()
                    born.append(self.own.birth(int(parent.stdout.readline())))
                    self.assertEqual(parent.wait(), 0)
                    self.assertNotIn(born[0].pid, pids)
                return pids, saturated
        raced = DarwinCoalition(_native=Barrier(), _limit=10.0).observe(base)
        self.assertEqual(self.native.coalition(born[0].pid), base.coalition)
        self.assertIsInstance(raced, Retry)
        self.assertIn(raced.reason, (dc.COUNTERS_CHANGED, dc.CARDINALITY))
        obs = self.settle(base)
        self.report('c12', obs, born)
        self.assertIn(born[0], self.novel(obs))

    def test_own_zombie_is_not_a_live_member(self):
        base = self.certify()
        r, w = os.pipe()
        zombie_pid = self.fork(lambda: (os.close(w), os.read(r, 1)))
        os.close(r)
        zombie = self.own.birth(zombie_pid)
        queue = select.kqueue()
        self.addCleanup(queue.close)
        queue.control([select.kevent(zombie_pid, select.KQ_FILTER_PROC,
                                     select.KQ_EV_ADD | select.KQ_EV_ONESHOT, select.KQ_NOTE_EXIT)], 0)
        os.close(w)  # EOF: el hijo sale y queda zombi hasta waitpid.
        self.assertTrue(queue.control(None, 1, 10))
        self.assertIn(zombie_pid, self.native.listpids()[0])
        with self.assertRaises(ReadError) as caught:
            self.native.unique(zombie_pid)
        self.assertEqual(caught.exception.errno, errno.ESRCH)
        obs = self.settle(base)
        self.report('zombi', obs, [zombie])
        self.assertNotIn(zombie, self.novel(obs))
        self.own.reap(zombie_pid)

    def test_observe_root_accepts_direct_child_only(self):
        child = subprocess.Popen(['/bin/sleep', '60'])
        self.own.popens.append(child)
        direct = self.own.birth(child.pid)
        nested = self.popen(NESTED)
        grandchild = self.own.birth(int(nested.stdout.readline()))
        root = self.gate.observe_root(self.owner, child.pid)
        self.assertEqual((root.birth, root.parent_unique), (direct, self.owner.unique))
        self.assertEqual(root.coalition, self.native.coalition(os.getpid()))
        with self.assertRaises(RootRejected) as caught:
            self.gate.observe_root(self.owner, grandchild.pid)
        self.assertEqual(caught.exception.args[0], dc.NOT_DIRECT_CHILD)
        with self.assertRaises(RootRejected) as caught:
            self.gate.observe_root(self.owner, 1)
        self.assertEqual(caught.exception.args[0], dc.NOT_IN_COALITION)

    def test_spawn_into_another_coalition_is_eperm(self):
        """Premisa del perfil (K6): un proceso sin privilegios no elige coalición."""
        libc = C.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        setcoal = libc.posix_spawnattr_setcoalition_np
        setcoal.argtypes = [C.c_void_p, C.c_uint64, C.c_int, C.c_int]
        for label, target in (('launchd', self.native.coalition(1)), ('propia', self.native.coalition(os.getpid()))):
            with self.subTest(label):
                attr, pid = C.c_void_p(), C.c_int(0)
                self.assertEqual(libc.posix_spawnattr_init(C.byref(attr)), 0)
                try:
                    self.assertEqual(setcoal(C.byref(attr), target, 0, 0), 0)
                    argv, envp = (C.c_char_p * 2)(b'/usr/bin/true', None), (C.c_char_p * 1)(None)
                    rc = libc.posix_spawn(C.byref(pid), b'/usr/bin/true', None, C.byref(attr), argv, envp)
                finally:
                    libc.posix_spawnattr_destroy(C.byref(attr))
                if rc == 0 and pid.value > 0:
                    os.waitpid(pid.value, 0)
                self.assertEqual(rc, errno.EPERM)


@unittest.skipUnless(sys.platform == 'darwin' and os.environ.get('HOSTGATE_OWN_COALITION') == '1',
                     'sólo en una conexión ssh propia, no multiplexada')
class OwnCoalition(Kernel):
    SCRIPT = ('( /bin/sleep 60 </dev/null >/dev/null 2>&1 & echo $! ); '
              '/bin/sleep 60 </dev/null >/dev/null 2>&1 & echo $!; wait')

    def test_shell_child_and_orphan_occupy_then_complete(self):
        base = self.certify()
        self.assertLessEqual(len(base.members), 8, base.members)
        self.assertTrue(all(m.birth.unique <= self.owner.unique for m in base.members), base.members)
        shell = subprocess.Popen(['/bin/sh', '-c', self.SCRIPT], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
        self.own.popens.append(shell)
        orphan = int(shell.stdout.readline())
        child = int(shell.stdout.readline())
        births = [self.own.birth(pid) for pid in (shell.pid, child, orphan)]
        occupied = self.settle(base)
        self.assertIsInstance(occupied, Occupied)
        self.assertEqual(self.novel(occupied), set(births))
        for birth in births[::-1]: self.own.terminate(birth)
        shell.wait()
        complete = self.settle(base)
        self.assertIsInstance(complete, Complete)
        print('OWN_COALITION_OK', json.dumps({'coalition': base.coalition, 'baseline': len(base.members),
                                              'occupied': len(occupied.novel), 'complete': complete.members,
                                              'retries': self.retries}), flush=True)


if __name__ == '__main__':
    unittest.main()
