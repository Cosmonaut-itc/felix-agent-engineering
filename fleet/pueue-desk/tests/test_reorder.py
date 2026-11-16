"""U3.6: reordenar y deshacer en la Mini contra un Pueue en memoria.

El doble de `pueue edit` bloquea la tarea, escribe el TOML de una sola
EditableTask, ejecuta el editor real (`editor_prioridad.py`) con el `$EDITOR`
que arma control.py y aplica el resultado como el daemon 4.0.4. Reloj, PIDs,
señales, presión y sistema de archivos son falsos o privados en /tmp.
"""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


control = load('control_reorder', BASE / 'control.py')
editor = load('editor_reorder', BASE / 'editor_prioridad.py')
LOW = dict(load=0, memory=1)
HIGH = dict(load=4, memory=1)


def outcome(result):
    """(status, code) without raising: a mutant must fail by assertion, never by TypeError."""
    return result.get('status'), (result.get('error') or {}).get('code')


def code(result):
    return outcome(result)[1]


def message(result):
    return (result.get('error') or {}).get('message')


class Crash(BaseException):
    """control.py muere en una frontera: ningún except ordinario lo atrapa."""


def toml_string(value):
    """Lo que elige el serializador de Pueue para una cadena, según su contenido."""
    printable = all(c == '\n' or 32 <= ord(c) != 127 for c in value)
    if '\n' in value and "'''" not in value and printable:
        return "'''\n" + value + "'''"
    if '\n' not in value and ('"' in value or '\\' in value) and "'" not in value and printable:
        return "'" + value + "'"
    out = []
    for c in value:
        named = {'\\': '\\\\', '"': '\\"', '\b': '\\b', '\t': '\\t', '\f': '\\f', '\r': '\\r'}
        if c in named:
            out.append(named[c])
        elif c == '\n':
            out.append('\n' if '\n' in value else '\\n')
        elif ord(c) < 32 or ord(c) == 127:
            out.append('\\u%04X' % ord(c))
        else:
            out.append(c)
    body = ''.join(out)
    return '"""\n' + body + '"""' if '\n' in value else '"' + body + '"'


def emit(task):
    lines = ['[%d]' % task['id'], 'id = %d' % task['id'], 'command = ' + toml_string(task['original_command']),
             'path = ' + toml_string(task['path'])]
    if task['label'] is not None:
        lines.append('label = ' + toml_string(task['label']))
    lines.append('priority = %d' % task['priority'])
    return '\n'.join(lines) + '\n'


def make_task(ident, priority=0, group='suites', command=None, label=None, status=None):
    command = command or 'sleep %d' % (30 + ident)
    return dict(id=ident, created_at='2026-11-16T10:00:%02d.123456Z' % (ident % 60), original_command=command,
                command=command, path='/tmp/fila-privada', envs={'SECRETO_PRUEBA': 'valor-%d' % ident}, group=group,
                dependencies=[], priority=priority, label=label,
                status=status or {'Queued': {'enqueued_at': '2026-11-16T10:01:%02d.5Z' % (ident % 60)}})


class FakeEdit:
    def __init__(self, fake, ident, env):
        self.fake, self.ident, self.env = fake, str(ident), env
        fake.spawned += 1
        self.pid = 41000 + fake.spawned
        self.returncode = None
        fake.alive[self.pid] = fake.test.now
        fake.spawns.append(int(ident))

    def finish(self, code):
        self.returncode = code
        self.fake.alive.pop(self.pid, None)
        return None, None

    def communicate(self, data, timeout=None):
        fake = self.fake
        fake.timeouts.append(('edit', timeout))
        if data != b'go\n':
            return self.finish(1)
        try:
            fake.point('edit_gate')
        except Crash:
            if fake.hard:
                fake.alive.pop(self.pid, None)  # sh lee EOF y sale sin ejecutar Pueue
            raise
        task = fake.tasks.get(self.ident)
        if task is None or control.task_state(task)[0] not in ('Queued', 'Stashed'):
            return self.finish(1)  # EditRequest rechazado por el daemon
        previous = task['status']
        task['status'] = {'Locked': {'previous_status': previous}}
        fake.point('edit_locked')
        folder = Path(tempfile.mkdtemp(dir=self.env['TMPDIR']))
        path = folder / 'tasks.toml'
        path.write_text(fake.emit(task))
        result = subprocess.run(['/bin/sh', '-c', self.env['EDITOR'] + ' ' + shlex.quote(str(path))], env=self.env)
        fake.editor_runs.append(result.returncode)
        if result.returncode != 0:
            task['status'] = previous  # EditRestore
            shutil.rmtree(folder)
            return self.finish(1)
        edited = editor.parse(path.read_text())
        shutil.rmtree(folder)
        words = edited['command'].split(' ', 1)
        expanded = (fake.aliases[words[0]] + (' ' + words[1] if len(words) > 1 else '')
                    if words[0] in fake.aliases else edited['command'])
        task.update(status=previous, original_command=edited['command'], command=expanded, path=edited['path'],
                    label=edited['label'], priority=edited['priority'])
        fake.edits.append((int(self.ident), edited['priority']))
        fake.point('edit_done')
        return self.finish(0)

    def kill(self):
        if not self.fake.hard:
            self.fake.alive.pop(self.pid, None)

    def wait(self):
        if self.returncode is None:
            self.returncode = -9
        return self.returncode


class FakePueue:
    def __init__(self, test):
        self.test = test
        self.groups = {g: dict(parallel_tasks=n, status='Running') for g, n in control.MINI_PROFILE.items()}
        self.tasks = {}
        self.calls, self.events, self.timeouts, self.spawns, self.edits, self.kills, self.editor_runs = [], [], [], [], [], [], []
        self.alive, self.spawned, self.hard, self.immortal = {}, 0, False, False
        self.crash, self.hooks, self.aliases = None, {}, {}
        self.timeout, self.context = None, {}
        self.emit = emit

    def add(self, *tasks):
        for task in tasks:
            self.tasks[str(task['id'])] = task

    def point(self, event):
        self.events.append(event)
        hook = self.hooks.get((event, self.events.count(event)))
        if hook:
            hook()
        if self.crash == (event, self.events.count(event)):
            self.crash = None
            raise Crash(event)

    def status(self):
        self.point('status')
        return dict(groups=copy.deepcopy(self.groups), tasks=copy.deepcopy(self.tasks))

    def parallel(self, group, number):
        self.calls.append(('parallel', group, number))
        self.groups[group]['parallel_tasks'] = number

    def call(self, *args):
        self.calls.append(args)
        self.timeouts.append((args[0], self.timeout))
        if args[:2] == ('pause', '--wait') and args[2] == '-g':
            self.groups[args[3]]['status'] = 'Paused'
        elif args[:2] == ('start', '-g'):
            self.groups[args[2]]['status'] = 'Running'
        elif args[0] == 'enqueue' and len(args) == 2:
            task = self.tasks[args[1]]
            assert control.task_state(task)[0] in ('Locked', 'Stashed')
            task['status'] = {'Queued': {'enqueued_at': '2026-11-16T11:00:00Z'}}
        else:
            raise AssertionError('Sin RPC real: %r' % (args,))
        self.point(args[0])
        return ''

    def edit_process(self, ident, env):
        return FakeEdit(self, ident, env)

    def signal(self, pid, sig):
        self.kills.append((pid, sig))
        if not self.immortal:
            self.alive.pop(pid, None)

    def priorities(self, group='suites'):
        return {int(k): t['priority'] for k, t in self.tasks.items() if t['group'] == group}

    def order(self, group='suites'):
        return [t['id'] for t in control.queued_rows(dict(tasks=self.tasks), group)]


class ReorderFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='u36-opus-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.top = Path(self.tmp.name)
        self.root = self.top / 'pueue-mini'
        self.root.mkdir()
        self.data = self.top / 'pueue-data'
        (self.data / 'task_logs').mkdir(parents=True)
        (self.data / 'state.json').write_text('{}')
        self.now, self.mono, self.gen = 1_800_000_000.0, 100.0, 'daemon-1'
        self.p = FakePueue(self)
        self.pressure = patch.object(control, 'pressure_mini', return_value=dict(LOW))
        self.sensor = self.pressure.start()
        self.addCleanup(self.pressure.stop)
        self.engine = self.fresh()
        with control.locked(self.root):
            self.engine.adopt()
        self.p.calls.clear()

    def fresh(self):
        """A new control.py process over the same authority (guard tick, POST or reanudar)."""
        engine = control.MiniControl(self.p, self.root, clock=lambda: self.now, generation=lambda: self.gen)
        engine.monotonic, engine.wall = (lambda: self.mono), (lambda: self.now)
        engine.sleep = lambda seconds: setattr(self, 'mono', self.mono + seconds)
        engine.process_start, engine.kill = self.p.alive.get, self.p.signal
        engine.pueue_data, engine.pueue_config = self.data, self.top / 'pueue.yml'
        engine.alias_default = self.top / 'pueue_aliases.yml'
        return engine

    def queue(self, priorities, group='suites', start=1):
        for k, prio in enumerate(priorities):
            self.p.add(make_task(start + k, prio, group))

    def request(self, order, ident='reorder-00000001', group='suites', dry_run=False):
        view = self.engine.view()
        info = view['reorder'][group]
        return dict(schema=control.HOST_SCHEMA, op='reorder', action_id=ident, dry_run=dry_run,
                    expected=dict(group=group, generation=view['generation'], queue_etag=info.get('queue_etag', 'a' * 64),
                                  queue=info.get('queue', [[1, '0' * 32, 0], [2, '0' * 32, 0]])),
                    change=dict(order=order))

    def undo(self, of, ident='undo-0000000001', group='suites'):
        view = self.engine.view()
        return dict(schema=control.HOST_SCHEMA, op='reorder_undo', action_id=ident, dry_run=False,
                    expected=dict(group=group, generation=view['generation'],
                                  queue_etag=view['reorder'][group].get('queue_etag', 'a' * 64)), undo_of=of)

    def receipt(self, ident):
        return self.engine.handle(dict(schema=control.HOST_SCHEMA, op='status', action_id=ident))

    def state(self):
        with self.engine.connect() as db:
            return self.engine.state(db)

    def fingerprint(self):
        return {str(p.relative_to(self.top)): p.read_bytes() for p in self.top.rglob('*') if p.is_file()}

    def crash(self, event, nth=1, hard=False, order=(1, 5, 2, 3, 4), ident='reorder-00000001'):
        request = self.request(list(order), ident)
        self.p.events.clear()
        self.p.crash, self.p.hard = (event, nth), hard
        with self.assertRaises(Crash):
            self.engine.handle(request)
        self.p.hard = False
        self.p.calls.clear()
        return request


class NormalSequenceTests(ReorderFixture):
    def test_drag_five_to_two_applies_receipt_and_reopens_through_admission(self):
        self.queue([0] * 5)
        before = copy.deepcopy(self.p.tasks)
        result = self.engine.handle(self.request([1, 5, 2, 3, 4]))
        self.assertEqual(result['status'], 'applied')
        self.assertIsNone(result['error'])
        self.assertTrue(result['effects'])
        self.assertEqual(result['result']['before'], [[i, 0] for i in range(1, 6)])
        self.assertEqual(result['result']['after'], [[1, 5], [2, 3], [3, 2], [4, 1], [5, 4]])
        self.assertIs(result['result']['undo'], True)
        self.assertIsNone(result['result']['held'])
        self.assertEqual(self.p.order(), [1, 5, 2, 3, 4])
        # One pause, one start: no ids, no restart, no remove/add.
        self.assertEqual(self.p.calls, [('pause', '--wait', '-g', 'suites'), ('start', '-g', 'suites')])
        self.assertEqual(self.p.spawns, [1, 2, 3, 4, 5])
        for ident, task in self.p.tasks.items():
            self.assertEqual(dict(task, priority=0), before[ident])  # same id, created_at, enqueued_at, command
        self.assertEqual(self.state()['reasons'].get('suites', []), [])
        self.assertEqual(self.p.groups['suites']['status'], 'Running')
        self.assertEqual(self.receipt('reorder-00000001'), dict(result, receipt_digest=result['receipt_digest']))

    def test_every_rpc_has_one_second_deadline_and_pause_precedes_any_edit(self):
        self.queue([0] * 3)
        self.engine.handle(self.request([3, 1, 2]))
        self.assertEqual({t for _, t in self.p.timeouts}, {control.REORDER_RPC_SECONDS})
        self.assertLess(self.p.events.index('pause'), self.p.events.index('edit_gate'))
        self.assertIsNone(self.p.timeout)

    def test_unchanged_targets_are_not_written(self):
        self.queue([3, 2, 1])
        self.engine.handle(self.request([1, 3, 2]))
        self.assertEqual(self.p.spawns, [2, 3])
        self.assertEqual(self.p.priorities(), {1: 3, 2: 1, 3: 2})

    def test_durable_files_keep_only_ids_priorities_and_hashes(self):
        self.queue([0] * 4)
        self.p.tasks['2']['label'] = 'etiqueta-visible'
        self.engine.handle(self.request([4, 3, 2, 1]))
        work = self.root / 'reorder-edit'
        self.assertEqual(os.stat(work).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(work / 'tmp').st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(work / 'plan.json').st_mode & 0o777, 0o600)
        self.assertEqual(list((work / 'tmp').iterdir()), [])
        blob = b''.join(p.read_bytes() for p in self.root.rglob('*') if p.is_file())
        for secret in (b'sleep 3', b'/tmp/fila-privada', b'SECRETO_PRUEBA', b'valor-', b'etiqueta-visible'):
            self.assertNotIn(secret, blob)

    def test_same_id_returns_same_receipt_and_new_body_conflicts(self):
        self.queue([0] * 3)
        request = self.request([3, 1, 2])
        first = self.engine.handle(request)
        calls = list(self.p.calls)
        self.assertEqual(self.engine.handle(request), first)
        self.assertEqual(self.p.calls, calls)
        other = dict(request, change=dict(order=[2, 3, 1]))
        self.assertEqual(code(self.engine.handle(other)), 'idempotency_conflict')
        self.assertEqual(self.p.calls, calls)

    def test_rejected_id_stays_bound(self):
        self.queue([0] * 3)
        request = self.request([1, 2, 3])
        self.assertEqual(code(self.engine.handle(request)), 'same_order')
        self.assertEqual(code(self.engine.handle(dict(request, change=dict(order=[3, 2, 1])))),
                         'idempotency_conflict')
        self.assertEqual(self.p.calls, [])

    def test_dry_run_and_capabilities_have_no_effects(self):
        self.queue([0] * 5)
        files, tasks = self.fingerprint(), copy.deepcopy(self.p.tasks)
        dry = self.engine.handle(self.request([5, 4, 3, 2, 1], dry_run=True))
        self.assertEqual(dry['status'], 'dry_run')
        self.assertEqual(dry['result']['writes'], 5)
        caps = self.engine.handle(dict(schema=control.HOST_SCHEMA, op='capabilities'))
        self.assertTrue(caps['capabilities']['reorder'])
        self.assertEqual(self.fingerprint(), files)
        self.assertEqual(self.p.tasks, tasks)
        self.assertEqual(self.p.calls, [])
        self.assertEqual(self.p.spawns, [])

    def test_view_serves_opaque_tokens_and_etag_over_whole_row(self):
        self.queue([0] * 3)
        info = self.engine.view()['reorder']['suites']
        self.assertTrue(info['enabled'])
        self.assertEqual([row[0] for row in info['queue']], [1, 2, 3])
        self.assertTrue(all(len(row[1]) == control.TOKEN_LENGTH for row in info['queue']))
        self.assertNotIn('sleep', json.dumps(info))
        etag = info['queue_etag']
        for change in (lambda: self.p.tasks['2'].update(priority=1),
                       lambda: self.p.tasks['3']['status']['Queued'].update(enqueued_at='2026-11-16T12:00:00Z'),
                       lambda: self.p.tasks['1'].update(command='sleep 99')):
            snapshot = copy.deepcopy(self.p.tasks)
            change()
            # command differs from original_command: identity changes and the row is no longer editable
            info = self.engine.view()['reorder']['suites']
            self.assertNotEqual(info.get('queue_etag'), etag)
            self.p.tasks = snapshot
        self.gen = 'daemon-2'
        self.assertNotEqual(control.queue_etag('daemon-2', 'suites', control.queued_rows(dict(tasks=self.p.tasks), 'suites')),
                            etag)

    def test_desk_profile_groups_are_never_accepted_on_the_mini(self):
        self.queue([0] * 3)
        request = self.request([3, 2, 1])
        request['expected']['group'] = 'jale'
        with self.assertRaises(ValueError):
            control.validate_host_request(request, 'mini')


class ConflictTests(ReorderFixture):
    def test_stale_expected_queue_is_conflict_without_pause(self):
        self.queue([0] * 3)
        request = self.request([3, 2, 1])
        self.p.add(make_task(4))
        result = self.engine.handle(request)
        self.assertEqual(outcome(result), ('conflict', 'stale_queue'))
        self.assertEqual(self.p.calls, [])

    def test_started_added_or_cancelled_before_comparison_edits_nothing(self):
        cases = dict(started=lambda: self.p.tasks['2'].update(status={'Running': {
                         'enqueued_at': '2026-11-16T10:01:02Z', 'start': '2026-11-16T10:05:00Z'}}),
                     added=lambda: self.p.add(make_task(9)),
                     cancelled=lambda: self.p.tasks.pop('3'))
        for name, change in cases.items():
            with self.subTest(name=name):
                self.p.tasks.clear(); self.p.calls.clear(); self.p.spawns.clear(); self.p.events.clear()
                self.queue([0] * 4)
                request = self.request([4, 3, 2, 1], ident='conflict-' + name)
                self.p.events.clear()
                self.p.hooks = {('pause', 1): change}
                result = self.engine.handle(request)
                self.p.hooks = {}
                self.assertEqual(outcome(result), ('conflict', 'stale_queue'))
                self.assertEqual(self.p.spawns, [])
                self.assertEqual(self.p.calls, [('pause', '--wait', '-g', 'suites'), ('start', '-g', 'suites')])
                self.assertEqual(self.state()['reasons'].get('suites', []), [])

    def test_add_after_comparison_keeps_its_id_stays_out_and_invalidates_undo(self):
        self.queue([0] * 4)
        request = self.request([4, 3, 2, 1])
        self.p.events.clear()
        self.p.hooks = {('edit_done', 1): lambda: self.p.add(make_task(9))}
        result = self.engine.handle(request)
        self.assertEqual(result['status'], 'applied')
        self.assertIs(result['result']['undo'], False)
        self.assertEqual(self.p.tasks['9']['priority'], 0)
        self.assertNotIn(9, self.p.spawns)
        undo = self.engine.handle(self.undo('reorder-00000001'))
        self.assertEqual(outcome(undo), ('conflict', 'undo_conflict'))
        self.assertEqual(message(undo), 'La fila cambió; no se puede deshacer')
        self.assertEqual(self.p.spawns, [1, 2, 3, 4])

    def test_foreign_edit_during_action_holds_group_without_success(self):
        self.queue([0] * 4)
        request = self.request([4, 3, 2, 1])
        self.p.events.clear()
        self.p.hooks = {('edit_done', 1): lambda: self.p.tasks['3'].update(original_command='otro', command='otro')}
        result = self.engine.handle(request)
        self.assertEqual(outcome(result), ('aborted', 'reorder_unverified'))
        self.assertFalse(result['result']['undo'])
        self.assertEqual(self.p.spawns, [1, 2, 3])  # the helper refused task 3; nothing else is touched
        self.assertIn(editor.EXIT_IDENTITY, self.p.editor_runs)
        self.assertEqual((self.p.tasks['1']['priority'], self.p.tasks['2']['priority']), (1, 2))
        self.assertEqual(self.p.groups['suites']['status'], 'Paused')
        self.assertEqual(self.state()['reasons'].get('suites', []), ['ambiguous'])
        self.assertNotIn(('start', '-g', 'suites'), self.p.calls)
        self.assertIn('pausa de origen desconocido', self.engine.view()['reorder']['suites']['reason'])


class PausedAndUnsafeTests(ReorderFixture):
    def paused(self, reason):
        startup = dict(phase='ready', statuses={}, groups={}, holds={}, interruptions={},
                       source_hash='0' * 64, target_hash='0' * 64)
        with self.engine.connect(write=True) as db:
            state = self.engine.state(db)
            state['reasons']['suites'] = [reason] if reason else []
            if reason == 'startup':
                state.update(startup=startup, startup_version=1)
            self.engine.store(db, state)
        self.p.groups['suites']['status'] = 'Paused'

    def test_paused_group_is_rejected_with_its_reason(self):
        self.queue([0] * 3)
        request = self.request([3, 2, 1])
        for reason, label in (('pressure', 'pausa por presión'), ('manual', 'pausa manual'),
                              ('ambiguous', 'pausa de origen desconocido'), ('startup', 'retenido al reiniciar'),
                              (None, 'pausa de origen desconocido')):
            with self.subTest(reason=reason):
                self.paused(reason)
                info = self.engine.view()['reorder']['suites']
                self.assertFalse(info['enabled'])
                self.assertEqual(info['reason'], 'No se puede reordenar mientras el grupo está pausado · ' + label)
                result = self.engine.handle(dict(request, action_id='paused-' + (reason or 'none')))
                self.assertEqual(outcome(result), ('rejected', 'paused'))
                self.assertIn(label, message(result))
                self.assertEqual(self.p.calls, [])

    def test_manual_fence_counts_as_manual_pause(self):
        self.queue([0] * 3)
        request = self.request([3, 2, 1])
        self.engine.pause_fence('suites').write_text('')
        self.assertIn('pausa manual', self.engine.view()['reorder']['suites']['reason'])
        self.assertEqual(code(self.engine.handle(request)), 'paused')
        self.assertEqual(self.p.calls, [])

    def test_held_tasks_block_reorder(self):
        for status in ({'Stashed': {'enqueue_at': None}},
                       {'Locked': {'previous_status': {'Queued': {'enqueued_at': '2026-11-16T10:00:00Z'}}}},
                       {'Paused': {'enqueued_at': '2026-11-16T10:00:00Z', 'start': '2026-11-16T10:00:01Z'}}):
            with self.subTest(status=next(iter(status))):
                self.p.tasks.clear()
                self.queue([0] * 3)
                request = self.request([3, 2, 1], ident='held-' + next(iter(status)).lower())
                self.p.add(make_task(7, status=status))
                self.assertEqual(self.engine.view()['reorder']['suites']['code'], 'held_tasks')
                self.assertEqual(code(self.engine.handle(request)), 'held_tasks')
                self.assertEqual(self.p.calls, [])

    def test_edit_unsafe_is_rejected_before_any_pause(self):
        def alias():
            (self.top / 'pueue_aliases.yml').write_text('sleep: "sleep 1;"\n')
        def expanded():
            self.p.tasks['2']['command'] = 'expandido 1'
        def files_mode():
            (self.top / 'pueue.yml').write_text('client:\n  edit_mode: files\n')
        def odd_config():
            (self.top / 'pueue.yml').write_text('client: {edit_mode: toml}\n')
        def control_char():
            self.p.tasks['3']['label'] = 'con\x1bescape'
        def no_editor():
            self.engine.editor = self.top / 'no-existe.py'
        def other_alias_file():
            (self.top / 'pueue.yml').write_text('shared:\n  alias_file: %s\n' % (self.top / 'otros.yml'))
            (self.top / 'otros.yml').write_text('sleep: dormir\n')
        for name, setup in dict(alias=alias, expanded=expanded, files_mode=files_mode, odd_config=odd_config,
                                control_char=control_char, no_editor=no_editor, other_alias_file=other_alias_file).items():
            with self.subTest(name=name):
                self.p.tasks.clear()
                for f in ('pueue_aliases.yml', 'pueue.yml', 'otros.yml'):
                    if (self.top / f).exists():
                        (self.top / f).unlink()
                self.engine.editor = BASE / control.EDITOR_NAME
                self.queue([0] * 3)
                request = self.request([3, 2, 1], ident='unsafe-' + name.replace('_', '-'))
                setup()
                self.assertEqual(self.engine.view()['reorder']['suites']['code'], 'edit_unsafe')
                result = self.engine.handle(request)
                self.assertEqual(outcome(result), ('rejected', 'edit_unsafe'))
                self.assertEqual(self.p.calls, [])
                self.assertEqual(self.p.spawns, [])

    def test_unrelated_alias_and_toml_mode_stay_safe(self):
        (self.top / 'pueue_aliases.yml').write_text('# alias\nls: "ls -la"\n')
        (self.top / 'pueue.yml').write_text('---\nclient:\n  edit_mode: toml\nshared:\n  pueue_directory: ~/x\n')
        self.queue([0] * 3)
        self.assertTrue(self.engine.view()['reorder']['suites']['enabled'])

    def test_disabled_host_and_busy_receipt(self):
        self.queue([0] * 3)
        request = self.request([3, 2, 1])
        (self.root / 'slots.disabled').write_text('')
        view = self.engine.view()
        self.assertFalse(view['capabilities']['reorder'])
        self.assertEqual(view['reorder']['suites']['code'], 'reorder_disabled')
        self.assertEqual(code(self.engine.handle(request)), 'reorder_disabled')
        self.assertEqual(self.p.calls, [])

    def test_single_row_has_nothing_to_reorder(self):
        self.queue([0])
        info = self.engine.view()['reorder']['suites']
        self.assertEqual((info['enabled'], info['code']), (False, None))


class LimitTests(ReorderFixture):
    def test_more_than_64_pending_is_too_long_and_rejected_by_validation(self):
        self.queue([0] * 65)
        self.assertEqual(self.engine.view()['reorder']['suites']['code'], 'too_long')
        self.assertEqual(control.REORDER_MESSAGES['too_long'], 'Fila demasiado larga para reordenar de una vez')
        rows = [[i, 'a' * 32, 0] for i in range(1, 66)]
        request = dict(schema=control.HOST_SCHEMA, op='reorder', action_id='long-00000001', dry_run=False,
                       expected=dict(group='suites', generation='g', queue_etag='a' * 64, queue=rows),
                       change=dict(order=list(range(65, 0, -1))))
        with self.assertRaises(ValueError):
            control.validate_host_request(request, 'mini')

    def test_64_rows_fit_request_and_three_full_groups_fit_the_view(self):
        big = 2**64 - 100
        rows = [[big + i, 'f' * 32, -(2**31)] for i in range(64)]
        request = dict(schema=control.HOST_SCHEMA, op='reorder', action_id='x' * 64, dry_run=False,
                       expected=dict(group='suites', generation='g' * 160, queue_etag='a' * 64, queue=rows),
                       change=dict(order=[r[0] for r in reversed(rows)]))
        control.validate_host_request(request, 'mini')
        self.assertLessEqual(len(json.dumps(request).encode()), 16384)
        for k, group in enumerate(('suites', 'default', 'rapidas')):
            self.queue([-(2**31)] * 64, group=group, start=10**18 + 100 * k)
        view = self.engine.view()
        self.assertTrue(all(view['reorder'][g]['enabled'] for g in ('suites', 'default', 'rapidas')))
        self.assertLessEqual(len(json.dumps(view).encode()), 16384)

    def test_plan_over_pause_budget_is_rejected_before_pausing(self):
        self.queue([0] * 64)
        with (self.data / 'state.json').open('wb') as stream:
            stream.truncate(64 * 2**20)  # long history: the estimate exceeds 3 s
        info = self.engine.view()['reorder']['suites']
        self.assertLess(info['max_writes'], 64)
        result = self.engine.handle(self.request(list(range(64, 0, -1))))
        self.assertEqual(outcome(result), ('rejected', 'too_long'))
        self.assertEqual(self.p.calls, [])

    def test_budget_expiry_mid_action_aborts_and_restores(self):
        self.queue([0] * 5)
        request = self.request([5, 4, 3, 2, 1])
        self.p.events.clear()
        self.p.hooks = {('edit_done', 2): lambda: setattr(self, 'mono', self.mono + control.REORDER_PAUSE_SECONDS)}
        result = self.engine.handle(request)
        self.assertEqual(outcome(result), ('aborted', 'interrupted'))
        self.assertEqual(self.p.priorities(), {i: 0 for i in range(1, 6)})
        self.assertEqual(self.p.groups['suites']['status'], 'Running')
        self.assertEqual(self.state()['reasons'].get('suites', []), [])


class PressureTests(ReorderFixture):
    def test_new_pressure_keeps_order_and_hands_pause_to_guard(self):
        self.queue([0] * 3)
        request = self.request([3, 2, 1])
        self.p.events.clear()
        self.p.hooks = {('pause', 1): lambda: setattr(self.sensor, 'return_value', dict(HIGH))}
        result = self.engine.handle(request)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual((result['result']['held'], result['result']['undo']), ('pressure', False))
        self.assertEqual(self.p.order(), [3, 2, 1])
        self.assertEqual(self.state()['reasons'].get('suites', []), ['pressure'])
        self.assertNotIn(('start', '-g', 'suites'), self.p.calls)
        self.sensor.return_value = dict(LOW)
        with control.locked(self.root):
            self.fresh().guard(dict(LOW))
        self.assertEqual(self.p.groups['suites']['status'], 'Running')
        self.assertEqual(self.state()['reasons'].get('suites', []), [])

    def test_accepted_action_survives_actions_disabled_and_maintenance(self):
        self.queue([0] * 3)
        request = self.request([3, 2, 1])
        self.p.events.clear()
        (self.top / 'rack').mkdir()
        self.p.hooks = {('pause', 1): lambda: (self.top / 'rack/actions.disabled').write_text('')}
        result = self.engine.handle(request)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(self.p.order(), [3, 2, 1])
        self.assertEqual(self.p.groups['suites']['status'], 'Running')
        (self.top / 'rack/actions.disabled').unlink()
        self.p.events.clear()
        request = self.request([1, 2, 3], ident='reorder-00000002')
        self.p.events.clear()
        self.p.hooks = {('pause', 1): lambda: (self.root / 'maintenance').write_text('')}
        result = self.engine.handle(request)
        self.assertEqual((result['status'], result['result']['held']), ('applied', 'ambiguous'))
        self.assertEqual(self.p.order(), [1, 2, 3])
        self.assertEqual(self.p.groups['suites']['status'], 'Paused')  # reopening needs fresh admission


class UndoTests(ReorderFixture):
    def test_exact_undo_with_negatives_and_ties(self):
        self.queue([5, -3, -3, 0, 7, -(2**31)])
        before = self.p.priorities()
        order = self.p.order()
        self.assertEqual(order, [5, 1, 4, 2, 3, 6])
        applied = self.engine.handle(self.request([2, 5, 1, 4, 3, 6]))
        self.assertEqual(applied['status'], 'applied')
        self.assertIs(applied['result']['undo'], True)
        self.now += 7
        undone = self.engine.handle(self.undo('reorder-00000001'))
        self.assertEqual(undone['status'], 'applied')
        self.assertIs(undone['result']['undo'], False)  # undo of an undo does not exist
        self.assertEqual(self.p.priorities(), before)
        self.assertEqual(self.p.order(), order)
        again = self.engine.handle(self.undo('reorder-00000001', ident='undo-0000000002'))
        self.assertEqual(code(again), 'undo_conflict')

    def test_undo_conflicts_have_no_effects(self):
        def added():
            self.p.add(make_task(9))
        def external_priority():
            self.p.tasks['1']['priority'] = 40
        def late():
            self.now += control.REORDER_UNDO_SECONDS + 1
        def restarted():
            self.gen = 'daemon-2'
        def later_action():
            # Back to the very same priorities: only "latest receipt of the group" tells them apart.
            self.assertEqual(self.engine.handle(self.request([1, 2, 3], ident='reorder-00000098'))['status'], 'applied')
            self.assertEqual(self.engine.handle(self.request([3, 2, 1], ident='reorder-00000099'))['status'], 'applied')
        for name, change in dict(added=added, external_priority=external_priority, late=late, restarted=restarted,
                                 later_action=later_action).items():
            with self.subTest(name=name):
                self.setUp()
                self.queue([0] * 3)
                self.assertEqual(self.engine.handle(self.request([3, 2, 1]))['status'], 'applied')
                change()
                tasks, calls, files = copy.deepcopy(self.p.tasks), list(self.p.calls), self.fingerprint()
                result = self.engine.handle(self.undo('reorder-00000001'))
                self.assertEqual(outcome(result), ('conflict', 'undo_conflict'))
                self.assertFalse(result['effects'])
                self.assertEqual((self.p.tasks, self.p.calls), (tasks, calls))
                changed = {k for k in set(files) | set(self.fingerprint()) if files.get(k) != self.fingerprint().get(k)}
                self.assertTrue(changed <= {'pueue-mini/control.sqlite3'})  # only the bound rejection

    def test_undo_of_unknown_or_slots_receipt_conflicts(self):
        self.queue([0] * 3)
        self.assertEqual(code(self.engine.handle(self.undo('no-existe-0001', ident='undo-unknown-01'))),
                         'undo_conflict')
        view = self.engine.view()
        slots = dict(schema=control.HOST_SCHEMA, op='slots', action_id='slots-00000001', dry_run=False,
                     expected={k: view[k] for k in ('revision', 'generation', 'policy_hash', 'mode', 'slots')},
                     change=dict(group='suites', delta=-1))
        self.assertEqual(self.engine.handle(slots)['status'], 'applied')
        self.p.calls.clear()
        self.assertEqual(code(self.engine.handle(self.undo('slots-00000001'))), 'undo_conflict')
        self.assertEqual(self.p.calls, [])

    def test_slots_undo_never_replays_a_reorder_receipt(self):
        self.queue([0] * 3)
        self.engine.handle(self.request([3, 2, 1]))
        view = self.engine.view()
        undo = dict(schema=control.HOST_SCHEMA, op='undo', action_id='undo-slots-0001', dry_run=False,
                    expected={k: view[k] for k in ('revision', 'generation', 'policy_hash', 'mode', 'slots')},
                    undo_of='reorder-00000001')
        calls = list(self.p.calls)
        self.assertEqual(code(self.engine.handle(undo)), 'undo_conflict')
        self.assertEqual(self.p.calls, calls)


# event, nth, hard, expected status, code, final priorities ('before' or 'target')
FRONTIERS = [
    ('pause', 1, False, 'aborted', 'interrupted', 'before'),           # prepared, group closed
    ('status', 2, False, 'aborted', 'interrupted', 'before'),          # paused, before the comparison
    ('edit_gate', 1, True, 'aborted', 'interrupted', 'before'),        # PID durable, gate never opened
    ('edit_locked', 1, False, 'aborted', 'interrupted', 'before'),     # client killed while Locked
    ('edit_locked', 1, True, 'aborted', 'interrupted', 'before'),      # orphan editor still alive
    ('edit_done', 1, False, 'aborted', 'interrupted', 'before'),       # partial: rolled back
    ('edit_done', 3, True, 'aborted', 'interrupted', 'before'),
    ('edit_done', 5, False, 'applied', None, 'target'),                # every write done, unverified
    ('status', 3, False, 'applied', None, 'target'),                   # verified read lost
    ('status', 4, False, 'applied', None, 'target'),                   # edited, before release
    ('start', 1, False, 'applied', None, 'target'),                    # reopened, receipt not written
]
TARGET = {1: 5, 2: 3, 3: 2, 4: 1, 5: 4}


class FrontierTests(ReorderFixture):
    def settle(self, recoverer):
        engine = self.fresh()
        with control.locked(self.root):
            if recoverer == 'guard':
                engine.guard(dict(LOW))
            elif recoverer == 'reanudar':
                engine.manual('suites', resume=True)
            else:
                engine.handle(self.request([2, 1], ident='next-00000001', group='rapidas'))

    def test_crash_at_each_frontier_is_settled_by_the_single_recoverer(self):
        for recoverer in ('guard', 'post', 'reanudar'):
            for event, nth, hard, status, expected_code, final in FRONTIERS:
                with self.subTest(recoverer=recoverer, event=event, nth=nth, hard=hard):
                    self.setUp()
                    self.queue([0] * 5)
                    self.queue([0] * 2, group='rapidas', start=20)
                    self.crash(event, nth, hard)
                    state = self.state()
                    self.assertIn('reorder', state['reasons'].get('suites', []))  # no gap between pause and phase
                    info = self.engine.view()['reorder']['suites']
                    self.assertIn('en pausa recuperando…', info['reason'])
                    self.p.calls.clear()
                    self.settle(recoverer)
                    receipt = self.receipt('reorder-00000001')
                    self.assertEqual(outcome(receipt), (status, expected_code))
                    expected = TARGET if final == 'target' else {i: 0 for i in range(1, 6)}
                    self.assertEqual(self.p.priorities(), expected)
                    self.assertNotIn('reorder', self.state()['reasons'].get('suites', []))
                    self.assertEqual(self.p.groups['suites']['status'], 'Running')
                    self.assertTrue(all(c[1] == str(self.p.spawns[-1]) or c[1] in map(str, range(1, 6))
                                        for c in self.p.calls if c[0] == 'enqueue'))
                    # `reanudar` admits once more after the recoverer; a start on a Running group is a no-op.
                    starts = (0 if event == 'start' else 1) + (recoverer == 'reanudar')
                    self.assertEqual(self.p.calls.count(('start', '-g', 'suites')), starts)
                    self.assertTrue(all(control.task_state(t)[0] == 'Queued' for t in self.p.tasks.values()))
                    self.assertEqual(self.p.alive, {})

    def test_orphan_editor_is_killed_only_by_pid_and_start_and_never_edited_twice(self):
        self.queue([0] * 5)
        self.crash('edit_locked', 1, hard=True)
        (pid, start), = self.p.alive.items()
        spawns = list(self.p.spawns)
        with control.locked(self.root):
            self.fresh().recover_reorder()
        self.assertEqual(self.p.kills, [(pid, signal.SIGKILL)])
        self.assertEqual(self.p.spawns[:len(spawns)], spawns)
        self.assertEqual(self.p.calls[0], ('enqueue', '1'))  # after the kill, before any new edit
        self.assertGreaterEqual(self.mono, 100 + control.REORDER_REAP_SECONDS)

    def test_reused_pid_is_not_killed(self):
        self.queue([0] * 5)
        self.crash('edit_locked', 1, hard=True)
        (pid, start), = self.p.alive.items()
        self.p.alive[pid] = start + 3600  # another process now owns that PID
        with control.locked(self.root):
            self.fresh().recover_reorder()
        self.assertEqual(self.p.kills, [])
        self.assertEqual(self.receipt('reorder-00000001')['status'], 'aborted')

    def test_immortal_orphan_keeps_group_closed_and_never_edits_again(self):
        self.queue([0] * 5)
        self.crash('edit_locked', 1, hard=True)
        self.p.immortal = True
        spawns = list(self.p.spawns)
        with self.assertRaisesRegex(ValueError, 'sigue vivo'), control.locked(self.root):
            self.fresh().guard(dict(LOW))  # the tick fails loudly after its protective close
        self.assertEqual(self.p.spawns, spawns)
        self.assertEqual(len(self.p.kills), 1)
        self.assertEqual(self.receipt('reorder-00000001')['status'], 'applying')
        self.assertEqual(self.p.groups['suites']['status'], 'Paused')
        self.assertIn('reorder', self.state()['reasons'].get('suites', []))
        self.assertNotIn('enqueue', [c[0] for c in self.p.calls])

    def test_crash_during_rollback_is_settled_by_the_next_tick(self):
        self.queue([0] * 5)
        self.crash('edit_done', 2)
        self.p.events.clear()
        self.p.crash = ('edit_locked', 1)  # the first rollback edit dies too
        with self.assertRaises(Crash):
            with control.locked(self.root):
                self.fresh().guard(dict(LOW))
        with control.locked(self.root):
            self.fresh().guard(dict(LOW))
        self.assertEqual(self.receipt('reorder-00000001')['status'], 'aborted')
        self.assertEqual(self.p.priorities(), {i: 0 for i in range(1, 6)})
        self.assertEqual(self.p.groups['suites']['status'], 'Running')

    def test_guard_recovers_even_when_actions_are_disabled(self):
        self.queue([0] * 5)
        self.crash('edit_done', 5)
        (self.top / 'rack').mkdir()
        (self.top / 'rack/actions.disabled').write_text('')
        (self.root / 'slots.disabled').write_text('')
        with control.locked(self.root):
            self.fresh().guard(dict(LOW))
        self.assertEqual(self.receipt('reorder-00000001')['status'], 'applied')
        self.assertEqual(self.p.priorities(), TARGET)

    def test_enqueue_recovery_is_persisted_when_the_group_stays_closed(self):
        self.queue([0] * 5)
        self.crash('edit_locked', 1)
        self.sensor.return_value = dict(HIGH)
        with control.locked(self.root):
            self.fresh().recover_reorder()
        self.assertEqual(self.p.calls[0], ('enqueue', '1'))
        self.assertEqual(self.p.calls[-1], ('pause', '--wait', '-g', 'suites'))
        self.assertEqual(self.state()['reasons'].get('suites', []), ['pressure'])

    def test_stock_restore_stashed_after_restart_is_enqueued_but_foreign_stashed_is_not(self):
        self.queue([0] * 5)
        self.crash('edit_locked', 1)
        self.p.tasks['1']['status'] = {'Stashed': {'enqueue_at': None}}  # stock restore turned Locked into Stashed
        self.p.add(make_task(8, status={'Stashed': {'enqueue_at': None}}))
        self.gen = 'daemon-2'
        with control.locked(self.root):
            self.fresh().recover_reorder()
        self.assertIn(('enqueue', '1'), self.p.calls)
        self.assertNotIn(('enqueue', '8'), self.p.calls)
        self.assertEqual(self.receipt('reorder-00000001')['status'], 'aborted')

    def test_stashed_without_restart_is_not_ours(self):
        self.queue([0] * 5)
        self.crash('edit_locked', 1)
        self.p.tasks['1']['status'] = {'Stashed': {'enqueue_at': None}}  # someone stashed it meanwhile
        with control.locked(self.root):
            self.fresh().recover_reorder()
        self.assertNotIn('enqueue', [c[0] for c in self.p.calls])
        receipt = self.receipt('reorder-00000001')
        self.assertEqual(outcome(receipt), ('aborted', 'reorder_unverified'))
        self.assertEqual(self.state()['reasons'].get('suites', []), ['ambiguous'])

    def test_locked_with_log_is_never_enqueued(self):
        self.queue([0] * 5)
        self.crash('edit_locked', 1)
        (self.data / 'task_logs/1.log').write_text('')
        with control.locked(self.root):
            self.fresh().recover_reorder()
        self.assertNotIn('enqueue', [c[0] for c in self.p.calls])
        self.assertEqual(code(self.receipt('reorder-00000001')), 'reorder_unverified')


class StartupTests(ReorderFixture):
    def image(self):
        return dict(groups=copy.deepcopy(self.p.groups), tasks=copy.deepcopy(self.p.tasks))

    def test_preparer_restores_own_locked_and_holds_foreign(self):
        self.queue([0] * 5)
        self.crash('edit_locked', 1, hard=True)
        self.p.add(make_task(8, status={'Locked': {'previous_status': {'Queued': {'enqueued_at': '2026-11-16T10:00:00Z'}}}}))
        self.p.add(make_task(9, group='rapidas', status={'Stashed': {'enqueue_at': None}}))
        with self.engine.connect() as db:
            owned = control.reorder_journal(db)
            state = self.engine.state(db)
        self.assertEqual(set(owned), {'1'})
        queue = self.image()
        new, image = control.startup_plan(queue, state, self.now, self.data, control.MINI_PROFILE, owned)
        self.assertEqual(image['tasks']['1']['status'], queue['tasks']['1']['status']['Locked']['previous_status'])
        self.assertIn('1', new['startup']['statuses'])
        self.assertIn('Locked', image['tasks']['8']['status'])
        self.assertIn('ambiguous', new['reasons'].get('suites', []))
        self.assertEqual(image['tasks']['9']['status'], {'Stashed': {'enqueue_at': None}})
        self.assertNotIn('ambiguous', new['reasons'].get('rapidas', []))
        control.validate_queue(image)
        _, image = control.startup_plan(queue, state, self.now, self.data, control.MINI_PROFILE,
                                        dict(owned, **{'8': 'f' * 64}))  # journaled id, other identity
        self.assertIn('Locked', image['tasks']['8']['status'])

    def test_preparer_without_foreign_lock_adds_no_hold_and_recoverer_finishes_after_restart(self):
        self.queue([0] * 5)
        self.crash('edit_done', 1)
        self.p.calls.clear()
        self.p.events.clear()
        self.p.crash = ('edit_locked', 1)
        self.p.hard = True
        with self.assertRaises(Crash):
            with control.locked(self.root):
                self.fresh().recover_reorder()  # dies on its rollback edit: task 1 Locked, orphan alive
        self.p.hard = False
        with self.engine.connect() as db:
            owned = control.reorder_journal(db)
            state = self.engine.state(db)
        self.assertEqual(owned, {'1': control.task_identity(self.p.tasks['1'])})
        self.p.alive.clear()  # the daemon restart takes the client with it
        new, image = control.startup_plan(self.image(), state, self.now, self.data, control.MINI_PROFILE, owned)
        self.assertNotIn('ambiguous', new['reasons'].get('suites', []))
        new['startup']['phase'] = 'ready'
        new['startup_version'] = 1
        self.p.tasks, self.p.groups = image['tasks'], image['groups']
        self.gen = new['generation'] = 'daemon-2'
        with self.engine.connect(write=True) as db:
            self.engine.store(db, new)
        with control.locked(self.root):
            self.fresh().recover_reorder()
        self.assertEqual(self.receipt('reorder-00000001')['status'], 'aborted')
        self.assertEqual(self.p.priorities(), {i: 0 for i in range(1, 6)})
        self.assertNotIn('enqueue', [c[0] for c in self.p.calls])
        self.assertNotIn(('start', '-g', 'suites'), self.p.calls)  # startup still owns the pause
        self.assertEqual(self.state()['reasons'].get('suites', []), ['startup'])

    def test_start_inputs_reads_the_journal_without_writing(self):
        self.queue([0] * 5)
        self.crash('edit_locked', 1)
        with self.engine.connect() as db:
            self.assertEqual(list(control.reorder_journal(db)), ['1'])
        files = self.fingerprint()
        with self.engine.connect() as db:
            control.reorder_journal(db)
        self.assertEqual(self.fingerprint(), files)


class HelperTests(unittest.TestCase):
    def table(self, command='sleep 1', path='/tmp', label=None, priority=0, ident=3):
        return emit(dict(id=ident, original_command=command, path=path, label=label, priority=priority))

    def test_multiline_command_with_priority_line_is_not_the_key(self):
        for command in ('echo a\npriority = 99\nfin', 'echo "a"\npriority = 99\n', "x'''\npriority = 1\n"):
            with self.subTest(command=command):
                text = self.table(command=command, priority=-7)
                task = editor.parse(text)
                self.assertEqual((task['command'], task['priority']), (command, -7))
                plan = dict(id=3, before=-7, target=12, fingerprint=editor.fingerprint(command, '/tmp', None))
                try:
                    result = editor.rewrite(text, plan)
                except editor.Unrecognized as error:
                    self.fail('reescritura rechazada: %s' % error)
                self.assertEqual(editor.parse(result)['priority'], 12)
                self.assertEqual(editor.parse(result)['command'], command)
                self.assertEqual(result.count('priority = 99'), text.count('priority = 99'))

    def test_literal_and_basic_strings_unicode_and_escapes(self):
        for command in ('echo ñandú 🚀', 'printf "%s\\n" \'x\'', 'a\\b', 'tab\tdentro', 'esc\x1b', 'comilla"y\'apóstrofo'):
            with self.subTest(command=command):
                text = self.table(command=command, label='etiqueta ü')
                task = editor.parse(text)
                self.assertEqual((task['command'], task['label']), (command, 'etiqueta ü'))
        text = '[3]\nid = 3\ncommand = "\\u00f1\\U0001F680"\npath = \'/tmp\'\npriority = 0\n'
        self.assertEqual(editor.parse(text)['command'], 'ñ🚀')

    def test_null_label_is_absent_and_fingerprinted_as_none(self):
        text = self.table(label=None)
        self.assertNotIn('label', text)
        task = editor.parse(text)
        self.assertIsNone(task['label'])
        self.assertNotEqual(editor.fingerprint('sleep 1', '/tmp', None), editor.fingerprint('sleep 1', '/tmp', ''))

    def test_unrecognized_formats_are_rejected(self):
        good = self.table(label='x')
        cases = dict(
            spaces=good.replace('priority = 0', 'priority  = 0'),
            indented=good.replace('priority = 0', ' priority = 0'),
            trailing=good.replace('priority = 0', 'priority = 0 '),
            comment=good.replace('priority = 0', 'priority = 0 # c'),
            crlf=good.replace('\n', '\r\n'),
            tab=good.replace('priority = 0', 'priority =\t0'),
            extra=good + 'envs = 1\n',
            repeated=good + 'priority = 1\n',
            missing=good.replace('priority = 0\n', ''),
            disordered=good.replace('path = "/tmp"\nlabel = "x"', 'label = "x"\npath = "/tmp"'),
            header=good.replace('[3]', '[4]'),
            array=good.replace('[3]', '[[3]]'),
            quoted_key=good.replace('priority =', '"priority" ='),
            float=good.replace('priority = 0', 'priority = 0.5'),
            plus=good.replace('priority = 0', 'priority = +5'),
            leading_zero=good.replace('priority = 0', 'priority = 05'),
            hex=good.replace('priority = 0', 'priority = 0x5'),
            underscore=good.replace('priority = 0', 'priority = 1_0'),
            overflow=good.replace('priority = 0', 'priority = 2147483648'),
            inline=good.replace('label = "x"', 'label = {a = 1}'),
            string_priority=good.replace('priority = 0', 'priority = "0"'),
            integer_label=good.replace('label = "x"', 'label = 1'),
            two_tables=good + '[4]\nid = 4\n',
            line_continuation='[3]\nid = 3\ncommand = """a\\\nb"""\npath = "/tmp"\npriority = 0\n',
            bad_escape=good.replace('"sleep 1"', '"sl\\eep"'),
            surrogate=good.replace('"sleep 1"', '"\\uD800"'),
            open_string=good.replace('"sleep 1"', '"sleep 1'),
            six_quotes=good.replace('"sleep 1"', '"""a""""""'),
            leading_blank='\n' + good)
        for name, text in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(editor.Unrecognized):
                    editor.parse(text)

    def test_rewrite_checks_plan_and_identity(self):
        text = self.table(priority=4)
        fp = editor.fingerprint('sleep 1', '/tmp', None)
        with self.assertRaises(LookupError):
            editor.rewrite(text, dict(id=3, before=5, target=1, fingerprint=fp))
        with self.assertRaises(LookupError):
            editor.rewrite(text, dict(id=9, before=4, target=1, fingerprint=fp))
        with self.assertRaises(PermissionError):
            editor.rewrite(text, dict(id=3, before=4, target=1, fingerprint=editor.fingerprint('sleep 2', '/tmp', None)))
        self.assertEqual(editor.rewrite(text, dict(id=3, before=4, target=-(2**31), fingerprint=fp)),
                         text.replace('priority = 4', 'priority = -2147483648'))

    def test_main_requires_private_plan_and_own_tasks_file(self):
        with tempfile.TemporaryDirectory(prefix='u36-opus-ed-', dir='/tmp') as tmp:
            work = Path(tmp) / 'work'
            (work / 'tmp/x').mkdir(parents=True, mode=0o700)
            os.chmod(work, 0o700)
            plan = work / 'plan.json'
            fp = editor.fingerprint('sleep 1', '/tmp', None)
            plan.write_text(json.dumps(dict(version=1, id=3, before=0, target=2, fingerprint=fp)))
            target = work / 'tmp/x/tasks.toml'
            original = self.table()
            target.write_text(original)
            os.chmod(plan, 0o644)
            self.assertEqual(editor.main(['e', str(plan), str(target)]), editor.EXIT_PLAN)
            os.chmod(plan, 0o600)
            os.chmod(work, 0o755)
            self.assertEqual(editor.main(['e', str(plan), str(target)]), editor.EXIT_PLAN)
            os.chmod(work, 0o700)
            outside = Path(tmp) / 'tasks.toml'
            outside.write_text(original)
            self.assertEqual(editor.main(['e', str(plan), str(outside)]), editor.EXIT_PLAN)
            link = work / 'tmp/x/link'
            link.mkdir()
            (link / 'tasks.toml').symlink_to(target)  # resolves inside the private directory
            self.assertEqual(editor.main(['e', str(plan), str(link / 'tasks.toml')]), editor.EXIT_PLAN)
            self.assertEqual(outside.read_text(), original)
            self.assertEqual(target.read_text(), original)
            target.write_text(original.replace('[3]', '[[3]]'))
            self.assertEqual(editor.main(['e', str(plan), str(target)]), editor.EXIT_FORMAT)
            target.write_text(self.table(command='otro'))
            self.assertEqual(editor.main(['e', str(plan), str(target)]), editor.EXIT_IDENTITY)
            target.write_text(original)
            self.assertEqual(editor.main(['e', str(plan), str(target)]), 0)
            self.assertEqual(target.read_text(), original.replace('priority = 0', 'priority = 2'))
            self.assertEqual(editor.main(['e', str(plan)]), editor.EXIT_PLAN)
            plan.write_text(json.dumps(dict(version=1, id=3, before=0, target=2, fingerprint=fp, cmd='x')))
            self.assertEqual(editor.main(['e', str(plan), str(target)]), editor.EXIT_PLAN)


class DeskTests(unittest.TestCase):
    def test_desk_stays_without_reorder(self):
        from test_desk_slots import DeskFixture

        class Probe(DeskFixture):
            def runTest(self):
                pass
        probe = Probe()
        probe.setUp()
        try:
            view = probe.engine.view()
            self.assertFalse(view['capabilities']['reorder'])
            self.assertNotIn('reorder', view)
            request = dict(schema=control.HOST_SCHEMA, op='reorder', action_id='desk-reorder-01', dry_run=False,
                           expected=dict(group='jale', generation=view['generation'], queue_etag='a' * 64,
                                         queue=[[1, 'a' * 32, 0], [2, 'b' * 32, 0]]), change=dict(order=[2, 1]))
            result = probe.engine.handle(request)
            self.assertEqual(outcome(result), ('unsupported', 'unsupported'))
            self.assertEqual(probe.p.calls, [])
        finally:
            for cleanup in reversed(probe._cleanups):
                cleanup[0](*cleanup[1], **cleanup[2])


if __name__ == '__main__':
    unittest.main()
