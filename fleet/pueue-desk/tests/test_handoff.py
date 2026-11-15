"""U3.5: relevo de pueued en la Mini. launchd, login, daemon y ps son dobles en /tmp.

La máquina falsa vive en un JSON con reemplazo atómico, así un hijo muerto con
SIGKILL deja exactamente los efectos que alcanzó a hacer. El modelo de login
carga sólo los plists habilitados de LaunchAgents (O-W3).
"""
from contextlib import contextmanager, redirect_stdout
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import signal
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from test_slots import control

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('install_mini_handoff', BASE / 'install-mini.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

DOMAIN = 'gui/501'
BREW, OURS, GUARD = control.BREW_LABEL, control.DAEMON_LABEL, control.GUARD_LABEL
DAEMONS = (BREW, OURS)
STAMP = '2026-11-14T12:00:00+00:00'


def task(ident, status, group='suites'):
    return dict(id=ident, created_at=STAMP, command='true', original_command='true', path='/synthetic',
                envs={}, group=group, dependencies=[], priority=0, label=None, status=status)


def queued(): return {'Queued': dict(enqueued_at=STAMP)}
def running(): return {'Running': dict(enqueued_at=STAMP, start=STAMP)}
def done(result='Success'): return {'Done': dict(enqueued_at=STAMP, start=STAMP, end=STAMP, result=result)}


class Killed(BaseException):
    """In-process death for boundaries outside a fork."""


class Machine:
    """File-backed launchd + pueued + ps. Every effect is one atomic JSON write."""
    def __init__(self, root):
        self.root = root
        self.file = root / 'machine.json'
        self.agents = root / 'LaunchAgents'
        self.data = root / 'data'
        self.state_dir = root / 'state'
        self.pid_file = self.data / 'pueue.pid'
        self.queue_path = self.data / 'state.json'

    def load(self):
        return json.loads(self.file.read_text())

    def save(self, m):
        tmp = self.file.with_suffix('.tmp')
        tmp.write_text(json.dumps(m))
        os.replace(tmp, self.file)

    @contextmanager
    def edit(self):
        m = self.load()
        yield m
        self.save(m)

    # -- launchd -------------------------------------------------------------
    def plist_path(self, label):
        return self.agents / (label + '.plist')

    def enabled(self, m, label):
        return m['overrides'].get(label) != 'disabled'

    def check(self, m, where):
        loaded = [l for l in m['loaded'] if l in DAEMONS]
        armed = [l for l in DAEMONS if self.plist_path(l).exists() and self.enabled(m, l)]
        if len(loaded) > 1:
            m['violations'].append(where + ': dos daemons cargados ' + ','.join(loaded))
        if len(armed) > 1:
            m['violations'].append(where + ': un login cargaría ' + ','.join(armed))

    def launchctl(self, argv):
        assert argv[0] == '/bin/launchctl', argv
        op, rest = argv[1], argv[2:]
        with self.edit() as m:
            m['log'].append([op] + rest)
            label = rest[0][len(DOMAIN) + 1:] if rest and rest[0].startswith(DOMAIN + '/') else None
            rc, out = 0, ''
            if op == 'print':
                if label in m['loaded']:
                    out = label + ' = {\n\tpath = ' + str(self.plist_path(label)) + '\n\tstate = running\n}\n'
                else:
                    rc = 113
            elif op == 'print-disabled':
                out = 'disabled services = {\n' + ''.join(
                    '\t"' + k + '" => ' + v + '\n' for k, v in sorted(m['overrides'].items())) + '}\n'
            elif op in ('disable', 'enable'):
                m['overrides'][label] = op + 'd'
            elif op == 'bootout':
                if label not in m['loaded']:
                    rc = 3
                else:
                    m['loaded'].remove(label)
                    if label in m['pending']:
                        m['pending'].remove(label)
                    if m['daemon'] and m['daemon']['owner'] == label:
                        self.stop(m, graceful=True)
            elif op == 'bootstrap':
                path = Path(rest[1])
                name = plistlib.loads(path.read_bytes())['Label'] if path.exists() else None
                if name is None or not self.enabled(m, name) or name in m['loaded']:
                    rc = 5  # disabled or already loaded: launchd refuses
                else:
                    m['loaded'].append(name)
                    self.launch(m, name)
            else:
                raise AssertionError('launchctl inesperado: ' + op)
            self.check(m, op + ' ' + str(label))
        return subprocess.CompletedProcess(argv, rc, out, '')

    def launch(self, m, label):
        if label == BREW:
            self.stock_start(m, BREW)
        elif label == OURS:
            m['pending'].append(OURS)  # KeepAlive: preparar --exec runs when the lock frees

    def reboot(self, reused=None):
        """Power cycle + login: loaded == enabled plists in LaunchAgents."""
        with self.edit() as m:
            if m['daemon']:
                self.stop(m, graceful=True)
            old = self.pid_file.read_text().strip() if self.pid_file.exists() else None
            if reused and old:
                m['reused'][old] = reused  # the stale pid now names another executable
            m['loaded'], m['pending'] = [], []
            for label in sorted(m['labels']):
                if self.plist_path(label).exists() and self.enabled(m, label):
                    m['loaded'].append(label)
            m['boots'] += 1
            self.check(m, 'login')
            for label in list(m['loaded']):
                self.launch(m, label)

    # -- pueued --------------------------------------------------------------
    def stop(self, m, graceful):
        if graceful:  # stock saves on SIGTERM; the pid file stays (worst case)
            self.queue_path.write_text(json.dumps(m['queue']))
        m['daemon'], m['queue'] = None, None

    def stock_start(self, m, owner):
        if m['daemon']:
            m['violations'].append('segundo pueued iniciado por ' + owner)
            return
        q = json.loads(self.queue_path.read_text())
        for t in q['tasks'].values():
            variant, detail = next(iter(t['status'].items()))
            if variant in ('Running', 'Paused'):
                t['status'] = {'Done': dict(enqueued_at=detail['enqueued_at'], start=detail['start'], end=STAMP, result='Killed')}
            elif variant == 'Locked':
                t['status'] = {'Stashed': dict(enqueue_at=None)}
        for t in q['tasks'].values():
            if 'Queued' in t['status']:
                q['groups'][t['group']]['status'] = 'Paused'
        m['next_pid'] += 1
        m['daemon'] = dict(owner=owner, pid=m['next_pid'])
        m['queue'] = q
        m['starts'].append(owner)
        self.pid_file.write_text(str(m['next_pid']) + '\n')
        self.queue_path.write_text(json.dumps(q))

    def kill_daemon(self):
        """SIGKILL of pueued: no save; KeepAlive restarts the owner's program."""
        with self.edit() as m:
            owner = m['daemon']['owner']
            m['daemon'], m['queue'] = None, None
            if owner in m['loaded']:
                self.launch(m, owner)

    def step(self, boundary=lambda name: None):
        """launchd runs pending preparers; only success execs pueued."""
        m = self.load()
        for label in list(m['pending']):
            engine = self.engine()
            try:
                with control.locked(self.state_dir, create=False):
                    control.prepare_start(engine, self.queue_path, dict(data=self.data, pid_file=self.pid_file),
                                          boundary=lambda phase: boundary('preparar:' + phase))
            except (ValueError, subprocess.CalledProcessError, OSError) as error:
                with self.edit() as m:
                    m['prepare_errors'].append(str(error))
                continue
            with self.edit() as m:
                if label in m['pending']:
                    m['pending'].remove(label)
                    self.stock_start(m, label)
            boundary('exec')

    # -- clients -------------------------------------------------------------
    def ps(self, argv, **kwargs):
        pid = argv[2]
        m = self.load()
        if m['daemon'] and str(m['daemon']['pid']) == pid:
            name = '/opt/homebrew/bin/pueued'
        elif pid in m['reused']:
            name = m['reused'][pid]
        else:
            return subprocess.CompletedProcess(argv, 1, '', '')
        out = ('Sun Nov 15 00:00:%02d 2026 ' % (int(pid) % 60) + name) if 'lstart=' in argv else name
        return subprocess.CompletedProcess(argv, 0, out + '\n', '')

    def engine(self):
        return control.MiniControl(FakeDaemon(self), self.state_dir, clock=lambda: self.load()['now'])

    def launchd(self):
        return control.Launchd(DOMAIN, self.agents, self.launchctl)


class FakeDaemon:
    """pueue client: fails exactly like the CLI when no daemon answers."""
    def __init__(self, machine):
        self.m = machine
        self.calls = []

    def status(self):
        m = self.m.load()
        if not m['daemon']:
            raise subprocess.CalledProcessError(2, ['pueue', 'status'])
        return copy.deepcopy(dict(groups=m['queue']['groups'], tasks=m['queue']['tasks']))

    def parallel(self, group, number):
        self.call('parallel', number, '-g', group)

    def call(self, *args):
        self.calls.append(args)
        with self.m.edit() as m:
            if not m['daemon']:
                raise subprocess.CalledProcessError(2, ['pueue', args[0]])
            m['rpc'].append([str(a) for a in args])
            group = m['queue']['groups'][args[-1]]
            if args[0] == 'pause':
                group['status'] = 'Paused'
            elif args[0] == 'start':
                group['status'] = 'Running'
            elif args[0] == 'parallel':
                group['parallel_tasks'] = int(args[1])
            else:
                raise AssertionError('RPC inesperada')
            self.m.queue_path.write_text(json.dumps(m['queue']))


def fake_run(machine):
    def run(argv, **kwargs):
        if argv[:1] == ['/bin/ps']:
            return machine.ps(argv)
        if argv[:1] == ['/bin/launchctl']:
            return machine.launchctl(argv)
        raise AssertionError('Ningún binario real: ' + str(argv))
    return run


def handoff_for(machine, checkpoint=lambda name: None):
    return control.MiniHandoff(machine.engine(), machine.launchd(), machine.pid_file, checkpoint=checkpoint)


def drive(machine, target, checkpoint=lambda name: None, wait=0, limit=12):
    """Installer + KeepAlive: request, then ticks with the lock released between them."""
    with control.locked(machine.state_dir, create=False):
        result = handoff_for(machine, checkpoint).request(target, wait)
    for _ in range(limit):
        if result in ('done', 'aborted'):
            return result
        machine.step(checkpoint)
        with control.locked(machine.state_dir, create=False):
            result = handoff_for(machine, checkpoint).tick()
    return result


def child(root, target, index):
    """Separate interpreter (macOS SQLite is not fork-safe): SIGKILL after the index-th boundary."""
    machine = Machine(Path(root))
    seen = [0]

    def checkpoint(name):
        seen[0] += 1
        if seen[0] == index:
            os.kill(os.getpid(), signal.SIGKILL)
    with patch.object(control.subprocess, 'run', side_effect=fake_run(machine)):
        drive(machine, target, checkpoint)


class HandoffFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='u35-opus-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.m = Machine(self.root)
        self.guard_errors = []
        for d in (self.m.agents, self.m.data, self.m.state_dir):
            d.mkdir()
        groups = {g: dict(status='Running', parallel_tasks=n) for g, n in control.MINI_PROFILE.items()}
        queue = dict(groups=groups, tasks={'0': task(0, done()), '1': task(1, queued(), 'rapidas')})
        groups['rapidas']['status'] = 'Paused'  # a manual pause that must survive everything
        self.m.queue_path.write_text(json.dumps(queue))
        for label in (BREW, GUARD):
            self.m.plist_path(label).write_bytes(plistlib.dumps(dict(Label=label, ProgramArguments=['/bin/true'])))
        self.m.save(dict(overrides={}, loaded=[], pending=[], daemon=None, queue=None, next_pid=400, reused={},
                         labels=[BREW, OURS, GUARD], log=[], violations=[], starts=[], boots=0, rpc=[],
                         prepare_errors=[], now=1000.0))
        self.patch_run()
        self.m.reboot()  # Homebrew's pueued, like today's Mini
        with control.locked(self.m.state_dir):
            self.m.engine().adopt(self.m.pid_file)
        with self.m.engine().connect(write=True) as db:
            state = self.m.engine().state(db)
            state['reasons']['rapidas'] = ['manual']
            state['startup_version'] = 1
            self.m.engine().store(db, state)
            control.handoff_state(db, migrate=True)
        self.install_files()

    def patch_run(self):
        self.real_run = subprocess.run
        patcher = patch.object(control.subprocess, 'run', side_effect=fake_run(self.m))
        patcher.start()
        self.addCleanup(patcher.stop)

    def install_files(self):
        # What instalar does before the switch: disable, then write the plist.
        self.m.launchd().disable(OURS)
        self.m.plist_path(OURS).write_bytes(plistlib.dumps(dict(Label=OURS, ProgramArguments=['/bin/true'])))

    def handoff(self, checkpoint=lambda name: None):
        return handoff_for(self.m, checkpoint)

    def handoff_row(self):
        with self.m.engine().connect() as db:
            return control.handoff_state(db)

    def policy(self):
        with self.m.engine().connect() as db:
            return self.m.engine().state(db), control.handoff_state(db)

    def drive(self, target, checkpoint=lambda name: None, wait=0, limit=12):
        return drive(self.m, target, checkpoint, wait, limit)

    def recover(self, limit=12):
        """The real guard job alone (it ticks the handoff first), plus launchd's KeepAlive.

        load=1.5 is the hysteresis band: the guard neither pauses nor opens, so
        only recovery and reconciliation are observed.
        """
        row = None
        for _ in range(limit):
            self.m.step()
            try:
                self.guard(load=1.5)
            except (ValueError, subprocess.CalledProcessError) as error:
                self.guard_errors.append(str(error))  # the job exits non-zero; launchd reruns it
            row = self.handoff_row()
            if row is None or row['phase'] == 'idle':
                self.m.step()
                return row['last']['result'] if row and row['last'] else 'idle'
        return row['phase']

    def guard(self, load=0.0):
        # The job builds its own MiniControl; give it the machine's clock.
        clock = (lambda: self.m.load()['now'],) + control.MiniControl.__init__.__defaults__[1:]
        with patch.object(control, 'pressure_mini', return_value=dict(load=load, memory=1)), \
                patch.object(control.MiniControl.__init__, '__defaults__', clock), \
                control.locked(self.m.state_dir, create=False):
            return control.guard(FakeDaemon(self.m), None, self.m.state_dir, host='mini', launchd=self.m.launchd())

    def kill_at(self, target, index):
        """True when the child died by SIGKILL at that boundary (effects durable)."""
        result = self.real_run([sys.executable, '-B', str(Path(__file__).resolve()), 'child', str(self.root), target, str(index)],
                               cwd=str(Path(__file__).resolve().parent), capture_output=True, text=True, timeout=60,
                               env=dict(os.environ, TMPDIR='/tmp', PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(result.stderr, '')
        return result.returncode == -signal.SIGKILL

    def assert_one_daemon(self, owner):
        m = self.m.load()
        self.assertEqual(m['violations'], [])
        self.assertEqual([l for l in m['loaded'] if l in DAEMONS], [owner])
        self.assertEqual(m['daemon']['owner'], owner)
        other = OURS if owner == BREW else BREW
        self.assertEqual(m['overrides'].get(other), 'disabled')
        self.assertNotEqual(m['overrides'].get(owner), 'disabled')

    def assert_queue_kept(self):
        tasks = self.m.load()['queue']['tasks']
        self.assertEqual(sorted(tasks), ['0', '1'])
        self.assertIn('Queued', tasks['1']['status'])
        self.assertEqual(tasks['1']['group'], 'rapidas')
        self.assertEqual(tasks['0']['status'], done())


class HandoffTests(HandoffFixture):
    def boundaries(self, target):
        names = []
        self.assertEqual(self.drive(target, names.append), 'done')
        return names

    def test_install_completes_with_one_daemon_queue_kept_and_groups_closed_until_guard(self):
        names = self.boundaries('install')
        self.assertIn('bootout:' + BREW, names)
        self.assertLess(names.index('disable:' + BREW), names.index('bootout:' + BREW))
        self.assertLess(names.index('bootout:' + BREW), names.index('relevo:start'))
        self.assertLess(names.index('relevo:start'), names.index('enable:' + OURS))
        self.assert_one_daemon(OURS)
        self.assert_queue_kept()
        state, handoff = self.policy()
        self.assertEqual(handoff['phase'], 'idle')
        self.assertEqual(handoff['last']['result'], 'done')
        self.assertEqual(handoff['brew_plist'], str(self.m.plist_path(BREW)))
        self.assertEqual(state['startup']['phase'], 'prepared')
        groups = self.m.load()['queue']['groups']
        self.assertEqual({g: r['status'] for g, r in groups.items()}, dict.fromkeys(control.MINI_PROFILE, 'Paused'))
        self.assertFalse(any('handoff' in r for r in state['reasons'].values()))
        self.assertEqual(state['reasons']['rapidas'], ['manual', 'startup'])
        view = self.m.engine().view()
        self.assertEqual(view['startup_waiting'], dict(suites='cuotas_sensores', default='cuotas_sensores'))
        self.assertEqual(view['startup_holds'], dict(rapidas='pausa manual'))
        self.guard()
        groups = self.m.load()['queue']['groups']
        self.assertEqual(groups['suites']['status'], 'Running')
        self.assertEqual(groups['default']['status'], 'Running')
        self.assertEqual(groups['rapidas']['status'], 'Paused')  # manual stays for Felix
        state, _ = self.policy()
        self.assertEqual(state['startup']['phase'], 'ready')
        with control.locked(self.m.state_dir, create=False), \
                patch.object(control, 'pressure_mini', return_value=dict(load=0.0, memory=1)):
            self.m.engine().manual('rapidas', resume=True)
        self.guard()
        self.assertEqual(self.m.load()['queue']['groups']['rapidas']['status'], 'Running')

    def test_sigkill_at_every_install_boundary_recovers_by_guard_with_and_without_login(self):
        names = self.boundaries('install')
        count = len(names)
        for reboot in (False, True):
            for index in range(1, count + 1):
                with self.subTest(boundary=names[index - 1], index=index, login=reboot):
                    self.tearDown_once()
                    self.setUp()
                    self.assertTrue(self.kill_at('install', index))
                    if reboot:
                        self.m.reboot()
                    self.assertEqual(self.m.load()['violations'], [])
                    result = self.recover()
                    state, handoff = self.policy()
                    self.assertEqual(handoff['phase'], 'idle')
                    if handoff['last'] and handoff['last']['result'] == 'aborted':
                        # A login before the switch restarted Homebrew's daemon: the
                        # switch aborts and the old manager keeps one daemon.
                        self.assertTrue(reboot)
                        self.assertLess(index, names.index('relevo:switch') + 1)
                        self.assert_one_daemon(BREW)
                    else:
                        self.assertIn(result, ('done', 'idle'))
                        self.assert_one_daemon(OURS)
                        self.assertIn(state['startup']['phase'], ('prepared', 'ready'))
                    self.assert_queue_kept()
                    self.assertFalse(any('handoff' in r for r in state['reasons'].values()))
                    self.assertEqual(self.m.load()['violations'], [])

    def tearDown_once(self):
        self.doCleanups()

    def installed(self):
        self.assertEqual(self.drive('install'), 'done')
        self.guard()  # ready; adopts our daemon's generation
        with self.m.edit() as m:
            m['log'], m['starts'] = [], []

    def test_withdraw_returns_to_homebrew_and_removes_our_plist(self):
        self.installed()
        self.assertEqual(self.drive('withdraw'), 'done')
        self.assert_one_daemon(BREW)
        self.assertFalse(self.m.plist_path(OURS).exists())
        self.assert_queue_kept()
        state, handoff = self.policy()
        self.assertEqual(handoff['last'], dict(target='withdraw', result='done', at=1000.0))
        self.assertEqual(state['generation'], self.m.engine().generation(state))
        self.assertFalse(any('handoff' in r for r in state['reasons'].values()))
        self.assertIn('manual', state['reasons']['rapidas'])
        self.guard()
        groups = self.m.load()['queue']['groups']
        self.assertEqual(groups['suites']['status'], 'Running')
        self.assertEqual(groups['rapidas']['status'], 'Paused')
        # The guard reopened its own pauses only; it never reloads our agent.
        self.assertNotIn(OURS, self.m.load()['loaded'])

    def test_sigkill_at_every_withdraw_boundary_recovers_by_guard_with_and_without_login(self):
        self.installed()
        names = self.boundaries('withdraw')
        for reboot in (False, True):
            for index in range(1, len(names) + 1):
                with self.subTest(boundary=names[index - 1], index=index, login=reboot):
                    self.doCleanups()
                    self.setUp()
                    self.installed()
                    self.assertTrue(self.kill_at('withdraw', index))
                    if reboot:
                        self.m.reboot()
                    self.assertEqual(self.m.load()['violations'], [])
                    self.recover()
                    state, handoff = self.policy()
                    self.assertEqual(handoff['phase'], 'idle')
                    if handoff['last']['result'] == 'aborted':
                        self.assertTrue(reboot)
                        self.assert_one_daemon(OURS)
                    else:
                        self.assert_one_daemon(BREW)
                        self.assertFalse(self.m.plist_path(OURS).exists())
                    self.assert_queue_kept()
                    self.assertFalse(any('handoff' in r for r in state['reasons'].values()))

    def test_o_w3_login_between_bootout_and_bootstrap_loads_no_daemon_then_guard_finishes(self):
        names = []
        index = None
        recorder = lambda name: names.append(name)
        self.assertEqual(self.drive('install', recorder), 'done')
        index = names.index('bootout:' + BREW) + 1
        self.doCleanups(); self.setUp()
        self.assertTrue(self.kill_at('install', index))
        self.assertEqual(self.policy()[1]['phase'], 'switch')
        self.m.reboot()
        m = self.m.load()
        self.assertIsNone(m['daemon'])  # brew disabled, ours still disabled: nothing loads
        self.assertEqual([l for l in m['loaded'] if l in DAEMONS], [])
        self.assertEqual(self.recover(), 'done')
        self.assert_one_daemon(OURS)
        self.assertEqual(self.m.load()['starts'], [BREW, OURS])

    def test_source_daemon_lingering_after_bootout_keeps_switch_open(self):
        original = self.m.launchctl

        def lingering(argv):
            result = original(argv)
            if argv[1] == 'bootout' and argv[2].endswith('/' + BREW):
                with self.m.edit() as m:  # unloaded, but its pueued still answers
                    m['daemon'] = dict(owner='orphan', pid=m['next_pid'])
                    m['queue'] = json.loads(self.m.queue_path.read_text())
            return result
        with patch.object(self.m, 'launchctl', side_effect=lingering):
            with control.locked(self.m.state_dir, create=False):
                try:
                    result = self.handoff().request('install')
                except ValueError as error:  # a later fence caught what the switch let through
                    result = 'error: ' + str(error)
                self.assertEqual(result, 'switch')
        m = self.m.load()
        self.assertNotIn(OURS, m['loaded'])
        self.assertNotEqual(m['overrides'].get(OURS), 'enabled')
        with self.m.edit() as m:
            m['daemon'], m['queue'] = None, None  # it finally exits
        self.assertEqual(self.recover(), 'done')
        self.assert_one_daemon(OURS)

    def test_daemon_answering_without_preparation_holds_the_switch(self):
        with control.locked(self.m.state_dir, create=False):
            self.assertEqual(self.handoff().request('install'), 'start')
        with self.m.edit() as m:  # pueued started outside our preparer
            m['pending'].remove(OURS)
            self.m.stock_start(m, OURS)
        with control.locked(self.m.state_dir, create=False), self.assertRaisesRegex(ValueError, 'sin preparación'):
            self.handoff().tick()
        self.assertEqual(self.handoff_row()['phase'], 'start')
        self.assertEqual(self.m.engine().view()['pause_reasons']['suites'], ['startup'])

    def test_preparer_refuses_while_the_other_manager_owns_the_phase(self):
        for phase in ('intent', 'drain', 'switch'):
            with self.subTest(phase=phase):
                with self.m.engine().connect(write=True) as db:
                    value = dict(control.idle_handoff(str(self.m.plist_path(BREW))), phase=phase, target='install',
                                 source=BREW, destination=OURS, source_plist=str(self.m.plist_path(BREW)),
                                 destination_plist=str(self.m.plist_path(OURS)), deadline=1000.0, requested_at=1000.0,
                                 previous_generation=None if phase != 'switch' else 'x')
                    db.execute('UPDATE mini_handoff SET body=? WHERE id=1', (control.canonical(value),))
                with control.locked(self.m.state_dir, create=False), self.assertRaisesRegex(ValueError, 'Relevo en curso'):
                    control.prepare_start(self.m.engine(), self.m.queue_path, dict(data=self.m.data, pid_file=self.m.pid_file))

    def test_two_daemons_rejected_preparer_never_execs_while_pueued_answers_or_pid_lives(self):
        # Someone bootstraps our agent by hand while Homebrew's daemon runs.
        with self.m.edit() as m:
            m['overrides'][OURS] = 'enabled'
            m['loaded'].append(OURS); m['pending'].append(OURS)
        self.m.step()
        m = self.m.load()
        self.assertEqual(m['starts'], [BREW])
        self.assertTrue(any('Daemon responde' in e for e in m['prepare_errors']))
        # The daemon stopped answering but its pid still names pueued: still refused.
        with patch.object(FakeDaemon, 'status', side_effect=subprocess.CalledProcessError(2, ['pueue'])):
            with control.locked(self.m.state_dir, create=False), self.assertRaisesRegex(ValueError, 'PID anterior'):
                control.prepare_start(self.m.engine(), self.m.queue_path, dict(data=self.m.data, pid_file=self.m.pid_file))

    def test_stale_pid_reused_by_other_program_after_reboot_is_offline_on_mini_only(self):
        self.installed()
        old = self.m.pid_file.read_text().strip()
        self.m.reboot(reused='/usr/libexec/trustd')  # Application Support keeps the stale pid file
        self.m.step()
        m = self.m.load()
        self.assertEqual(m['prepare_errors'], [])
        self.assertEqual((m['daemon'] or {}).get('owner'), OURS)
        with self.m.edit() as m:
            m['daemon'], m['queue'] = None, None
        self.m.pid_file.write_text(old + '\n')
        with self.assertRaisesRegex(ValueError, 'PID anterior'):
            control.assert_offline(FakeDaemon(self.m), self.m.pid_file, 'desk')  # desk: any live PID blocks

    def test_maintenance_marker_freezes_handoff_preparer_and_request(self):
        names = []
        with control.locked(self.m.state_dir, create=False):
            self.assertEqual(self.handoff(names.append).request('install'), 'start')
        (self.m.state_dir / 'maintenance').write_text('x')
        before = self.m.load()
        with control.locked(self.m.state_dir, create=False):
            self.assertEqual(self.handoff().tick(), 'maintenance')
        self.m.step()
        after = self.m.load()
        self.assertEqual((after['loaded'], after['daemon'], after['log'][len(before['log']):]),
                         (before['loaded'], before['daemon'], []))
        self.assertTrue(any('mantenimiento' in e for e in after['prepare_errors']))
        (self.m.state_dir / 'maintenance').unlink()
        self.assertEqual(self.recover(), 'done')
        self.assert_one_daemon(OURS)

    def test_busy_task_requires_wait_drains_without_kill_and_deadline_aborts_to_guard(self):
        with self.m.edit() as m:
            m['queue']['tasks']['2'] = task(2, running())
        with control.locked(self.m.state_dir, create=False), self.assertRaisesRegex(ValueError, 'tareas en curso'):
            self.handoff().request('install', 0)
        self.assertEqual(self.policy()[1]['phase'], 'idle')
        with control.locked(self.m.state_dir, create=False):
            self.assertEqual(self.handoff().request('install', 600), 'drain')
        state, handoff = self.policy()
        self.assertEqual(handoff['owned'], ['default', 'suites'])
        self.assertEqual(state['reasons']['suites'], ['handoff'])
        self.assertEqual(self.m.load()['queue']['groups']['suites']['status'], 'Paused')
        self.assertEqual(self.m.engine().view()['pause_reasons']['suites'], ['startup'])  # rack vocabulary only
        self.assertEqual(self.guard(), None)  # guard closed while the handoff is pending
        self.assertEqual(self.m.load()['queue']['groups']['suites']['status'], 'Paused')
        with self.m.edit() as m:
            m['now'] += 700  # deadline passes with the task still running
        self.assertEqual(self.recover(), 'aborted')
        self.assert_one_daemon(BREW)
        self.assertIn('Running', self.m.load()['queue']['tasks']['2']['status'])  # never killed
        state, handoff = self.policy()
        self.assertEqual(state['reasons']['suites'], ['pressure'])
        self.assertEqual(handoff['last']['result'], 'aborted')
        self.guard()
        self.assertEqual(self.m.load()['queue']['groups']['suites']['status'], 'Running')

    def test_task_finishing_inside_the_wait_completes_the_switch(self):
        with self.m.edit() as m:
            m['queue']['tasks']['2'] = task(2, running())
        with control.locked(self.m.state_dir, create=False):
            self.assertEqual(self.handoff().request('install', 600), 'drain')
        with self.m.edit() as m:
            m['queue']['tasks']['2']['status'] = done()
        self.assertEqual(self.recover(), 'done')
        self.assert_one_daemon(OURS)
        self.assertEqual(self.m.load()['queue']['tasks']['2']['status'], done())

    def test_request_gates_reject_without_effects(self):
        cases = {
            'receipt': lambda: self.open_receipt(),
            'startup': lambda: self.set_state(startup=dict(phase='intent')),
            'enabled': lambda: self.m.launchd().enable(OURS),
            'generation': lambda: self.set_state(generation='otra'),
            'maintenance': lambda: (self.m.state_dir / 'maintenance').write_text('x'),
        }
        for name, prepare in cases.items():
            with self.subTest(gate=name):
                self.doCleanups(); self.setUp()
                prepare()
                before = self.m.load()
                with control.locked(self.m.state_dir, create=False), self.assertRaises((ValueError, KeyError)):
                    self.handoff().request('install')
                after = self.m.load()
                self.assertEqual(self.handoff_row()['phase'], 'idle')
                self.assertEqual((after['loaded'], after['daemon']), (before['loaded'], before['daemon']))
                self.assertFalse([e for e in after['log'][len(before['log']):] if e[0] not in ('print', 'print-disabled')])

    def open_receipt(self):
        body = dict(response=dict(status='applying'))
        with self.m.engine().connect(write=True) as db:
            db.execute('INSERT INTO host_actions VALUES (?,?,?)', ('slots-00000001', 'x', json.dumps(body)))

    def set_state(self, **fields):
        with self.m.engine().connect(write=True) as db:
            state = self.m.engine().state(db)
            state.update(fields)
            db.execute('UPDATE policy SET body=? WHERE id=1', (control.canonical(state),))

    def test_unreadable_handoff_row_keeps_every_group_closed_and_no_launchd_effect(self):
        self.guard(load=99)  # the guard pauses suites/default for pressure
        self.assertEqual(self.m.load()['queue']['groups']['suites']['status'], 'Paused')
        with self.m.engine().connect(write=True) as db:
            db.execute("UPDATE mini_handoff SET body='{\"version\":2}' WHERE id=1")
        before = self.m.load()
        self.guard()  # low pressure, but the switch row is unreadable: nothing opens
        after = self.m.load()
        self.assertFalse([e for e in after['log'][len(before['log']):] if e[0] not in ('print', 'print-disabled')])
        self.assertNotIn(['start', '-g', 'suites'], after['rpc'])
        self.assertEqual(after['queue']['groups']['suites']['status'], 'Paused')
        self.assertTrue(self.m.engine().view()['recovery_pending'])
        self.assertIn('relevo_ilegible', (self.m.state_dir / 'guardia.log').read_text())

    def test_pre_u35_authority_has_no_row_and_reasons_handoff_is_mini_only(self):
        with self.m.engine().connect(write=True) as db:
            db.execute('DROP TABLE mini_handoff')
        self.assertEqual(self.recover(), 'idle')  # no row: nothing pending, the guard runs as before
        with self.m.engine().connect() as db:
            self.assertIsNone(control.handoff_state(db))
        state, _ = None, None
        with self.m.engine().connect(write=True) as db:
            state = self.m.engine().state(db)
            state['reasons']['suites'] = ['handoff']
            self.m.engine().store(db, state)
        desk = control.MiniControl(FakeDaemon(self.m), self.m.state_dir, host='desk')
        with self.m.engine().connect() as db, self.assertRaises(ValueError):
            desk.state(db)

    def test_running_task_on_restart_outside_handoff_fails_visibly_not_rerun(self):
        self.installed()
        with self.m.edit() as m:
            m['queue']['tasks']['2'] = task(2, running())
            m['queue']['groups']['suites']['status'] = 'Running'
            self.m.queue_path.write_text(json.dumps(m['queue']))
        self.m.kill_daemon()  # SIGKILL: KeepAlive reruns preparar, then pueued
        self.m.step()
        m = self.m.load()
        self.assertEqual(m['daemon']['owner'], OURS)
        status = m['queue']['tasks']['2']['status']
        self.assertEqual(status['Done']['result'], 'Killed')
        state, _ = self.policy()
        self.assertEqual(state['startup']['interruptions']['2']['message'], control.INTERRUPTED)
        self.assertEqual({g: r['status'] for g, r in m['queue']['groups'].items()}, dict.fromkeys(control.MINI_PROFILE, 'Paused'))
        self.guard()
        self.assertEqual(self.m.load()['queue']['tasks']['2']['status'], status)  # never relaunched


class InstallerTests(HandoffFixture):
    def args(self, command, seco=False, esperar=0):
        return types.SimpleNamespace(command=command, seco=seco, cuotas=False, pid_file=self.m.pid_file, esperar=esperar)

    def run_installer(self, command, seco=False, esperar=0):
        home = self.root / 'home'
        (home / 'Library').mkdir(parents=True, exist_ok=True)
        if not (home / 'Library/LaunchAgents').exists():
            (home / 'Library/LaunchAgents').symlink_to(self.m.agents)
        library = self.root / 'lib'
        out = io.StringIO()
        config = dict(data=self.m.data, pid_file=self.m.pid_file)
        launchd = control.Launchd(DOMAIN, self.m.agents, self.m.launchctl)
        with redirect_stdout(out):
            result = installer.adopted(self.args(command, seco, esperar), home, self.m.state_dir, library, DOMAIN,
                                       control=control, launchd=launchd, pueue=FakeDaemon(self.m), config=config,
                                       sleep=lambda s: self.m.step())
        return result, out.getvalue()

    def fresh(self):
        # Before U3.5: no row, no plist of ours, no override.
        with self.m.engine().connect(write=True) as db:
            db.execute('DROP TABLE mini_handoff')
            state = self.m.engine().state(db)
            state.pop('startup_version')
            db.execute('UPDATE policy SET body=? WHERE id=1', (control.canonical(state),))
        self.m.plist_path(OURS).unlink()
        with self.m.edit() as m:
            m['overrides'].pop(OURS)

    def fingerprint(self):
        files = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*')
                 if p.is_file() and p.name != 'machine.json'}
        m = self.m.load()
        return files, m['loaded'], m['overrides'], m['daemon'], m['rpc']

    def test_seco_validates_with_the_preparer_before_any_effect_and_describes_steps(self):
        self.fresh()
        before = self.fingerprint()
        with patch.object(control, 'start_projection', wraps=control.start_projection) as projection:
            result, out = self.run_installer('instalar', seco=True)
        self.assertEqual(result, 'seco')
        projection.assert_called_once()
        self.assertEqual(self.fingerprint(), before)
        for step in ('preparador', 'disable', 'bootout', 'bootstrap', 'mini_handoff', 'esperar'):
            self.assertIn(step, out)
        self.assertLess(out.index('disable ' + DOMAIN + '/' + OURS), out.index('copiar'))

    def test_seco_and_real_reject_corrupt_queue_profile_or_receipt_without_effects(self):
        for name, damage in (('queue', lambda: self.m.queue_path.write_text('{')),
                             ('profile', lambda: self.set_policy_hash()),
                             ('receipt', lambda: self.open_receipt())):
            for seco in (True, False):
                with self.subTest(case=name, seco=seco):
                    self.doCleanups(); self.setUp(); self.fresh()
                    damage()
                    before = self.fingerprint()
                    with self.assertRaises(ValueError):
                        self.run_installer('instalar', seco=seco)
                    self.assertEqual(self.fingerprint(), before)

    def set_policy_hash(self):
        with self.m.engine().connect(write=True) as db:
            state = self.m.engine().state(db)
            state['policy_hash'] = '0' * 64
            db.execute('UPDATE policy SET body=? WHERE id=1', (control.canonical(state),))

    def open_receipt(self):
        with self.m.engine().connect(write=True) as db:
            db.execute('INSERT INTO host_actions VALUES (?,?,?)',
                       ('slots-00000001', 'x', json.dumps(dict(response=dict(status='partial')))))

    def test_real_install_migrates_additively_disables_before_plist_and_completes(self):
        self.fresh()
        with self.m.engine().connect() as db:
            before_state = self.m.engine().state(db)
        written = []
        original = installer.atomic_bytes

        def checked(target, data):
            if target.name == OURS + '.plist':
                written.append(self.m.load()['overrides'].get(OURS))
            return original(target, data)
        with patch.object(installer, 'atomic_bytes', side_effect=checked):
            try:
                result, out = self.run_installer('instalar')
            finally:
                self.assertEqual(written, ['disabled'])  # login-safe before the file exists
        self.assertEqual(result, 'done')
        self.assert_one_daemon(OURS)
        self.assertIn('completado', out)
        state, handoff = self.policy()
        for key in ('desired', 'overrides', 'revision', 'pid_file', 'policy_hash'):
            self.assertEqual(state[key], before_state[key])
        self.assertEqual(state['startup_version'], 1)
        self.assertEqual(handoff['last']['result'], 'done')
        self.assertTrue((self.root / 'lib/control.py').exists())
        guard = [e for e in self.m.load()['log'] if e[0] in ('bootout', 'bootstrap') and GUARD in ' '.join(e)]
        self.assertEqual([e[0] for e in guard], ['bootout', 'bootstrap'])
        # Repeat: the daemon already belongs to us; only files and the guard refresh.
        result, out = self.run_installer('instalar')
        self.assertEqual(result, 'current')
        self.assertEqual(self.m.load()['starts'], [BREW, OURS])

    def test_installer_death_after_files_then_login_keeps_homebrew_and_retry_completes(self):
        self.fresh()
        with patch.object(control.MiniHandoff, 'request', side_effect=Killed()):
            with self.assertRaises(Killed):
                self.run_installer('instalar')
        self.m.reboot()
        self.assert_one_daemon(BREW)
        with self.assertRaisesRegex(ValueError, 'sin reconciliar'):
            self.run_installer('instalar')  # Homebrew restarted: the guard adopts it first
        self.guard()
        result, _ = self.run_installer('instalar')
        self.assertEqual(result, 'done')
        self.assert_one_daemon(OURS)

    def test_desinstalar_withdraws_keeps_authority_and_is_idempotent(self):
        self.fresh()
        self.run_installer('instalar')
        self.guard()
        result, out = self.run_installer('desinstalar', seco=True)
        self.assertEqual(result, 'seco')
        self.assertIn('Homebrew', out)
        result, _ = self.run_installer('desinstalar')
        self.assertEqual(result, 'done')
        self.assert_one_daemon(BREW)
        self.assertTrue((self.m.state_dir / 'control.sqlite3').exists())
        self.assertTrue(self.m.plist_path(GUARD).exists())
        result, out = self.run_installer('desinstalar')
        self.assertEqual(result, 'current')
        self.assertIn('nada que retirar', out)

    def test_parar_arrancar_maintenance_order_and_restart_through_preparer(self):
        self.fresh()
        self.run_installer('instalar')
        self.guard()
        with self.m.edit() as m:
            m['log'] = []
            m['queue']['tasks']['2'] = task(2, running())
        _, out = self.run_installer('parar', seco=True)
        self.assertIn('maintenance', out)
        self.assertEqual(self.m.load()['log'], [])
        self.run_installer('parar')
        m = self.m.load()
        ops = [(e[0], e[1].rsplit('/', 1)[-1]) for e in m['log'] if e[0] == 'bootout']
        self.assertEqual(ops, [('bootout', GUARD), ('bootout', OURS)])
        self.assertIsNone(m['daemon'])
        self.assertTrue((self.m.state_dir / 'maintenance').exists())
        self.assertEqual(self.policy()[0]['stop_tasks'], {'2': STAMP})
        self.assertEqual(m['overrides'][OURS], 'disabled')
        self.m.reboot()  # still in maintenance: no daemon label loads at login
        self.assertIsNone(self.m.load()['daemon'])
        self.assertNotIn(OURS, self.m.load()['loaded'])
        self.m.step()
        self.assertIsNone(self.m.load()['daemon'])
        self.run_installer('arrancar')
        self.m.step()
        m = self.m.load()
        self.assertEqual(m['daemon']['owner'], OURS)
        self.assertEqual(m['queue']['tasks']['2']['status']['Done']['result'], 'Killed')
        self.assertEqual(self.policy()[0]['startup']['interruptions']['2']['message'], control.INTERRUPTED)


class RoutingTests(unittest.TestCase):
    def test_main_routes_handoff_only_with_relevo_maintenance_or_our_agent(self):
        with tempfile.TemporaryDirectory(prefix='u35-opus-route-', dir='/tmp') as folder:
            home = Path(folder)
            state = home / '.local/state/pao/pueue-mini'
            agents = home / 'Library/LaunchAgents'

            def route(*argv):
                calls = []
                with patch.object(installer, 'adopted', side_effect=lambda args, *rest, **kw: calls.append(args.command)), \
                        patch.object(installer.Path, 'home', return_value=home), \
                        patch.object(sys, 'argv', ['install-mini.py', *argv]), \
                        patch.object(installer.subprocess, 'run', side_effect=AssertionError('sin efectos')):
                    try:
                        installer.main()
                    except SystemExit:
                        return 'error'
                    except AssertionError:
                        return 'legacy'
                return calls[0] if calls else 'legacy'
            self.assertEqual(route('instalar', '--relevo'), 'error')  # no authority yet
            state.mkdir(parents=True); (state / 'control.adopted').write_text('pueue-host-control@1\n')
            self.assertEqual(route('instalar', '--relevo'), 'instalar')
            self.assertEqual(route('desinstalar', '--relevo'), 'desinstalar')
            self.assertEqual(route('parar'), 'parar')
            self.assertEqual(route('desinstalar'), 'error')  # never discards desire, never switches implicitly
            self.assertEqual(route('instalar'), 'legacy')  # plain reinstall keeps today's daemon untouched
            agents.mkdir(parents=True); (agents / (OURS + '.plist')).write_text('x')
            self.assertEqual(route('instalar'), 'instalar')  # our agent owns pueued: upgrades go through preflight
            self.assertEqual(route('instalar', '--esperar', '121'), 'error')


if __name__ == '__main__':
    if sys.argv[1:2] == ['child']:
        child(sys.argv[2], sys.argv[3], int(sys.argv[4]))
        sys.exit(3)  # boundary never reached
    unittest.main()
