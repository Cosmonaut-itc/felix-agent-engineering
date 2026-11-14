"""U3.4: motivos de arranque del desk en canal, snapshot, servidor y panel; sin hosts reales."""
import safety  # fail closed before any subprocess
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec); spec.loader.exec_module(loaded)
    return loaded


control = module('startup_control', BASE.parent / 'pueue-desk/control.py')
snapshot = module('startup_snapshot', BASE / 'snapshot.py')
serve = module('startup_serve', BASE / 'serve.py')
host_action = module('startup_host_action', BASE / 'host_action.py')
NOW = 1794672000.0  # 2026-11-14T16:00:00Z, Día
STAMP = '2026-11-14T15:00:00-06:00'


class FakePueue:
    def __init__(self):
        self.groups = {g: dict(status='Running', parallel_tasks=n) for g, n in control.desk_profile(NOW).items()}
        self.groups['default'] = dict(status='Paused', parallel_tasks=1)

    def status(self):
        return dict(tasks={}, groups=copy.deepcopy(self.groups))


def desk_view(root, **changes):
    """A real desk controller view, then the startup fields a restart leaves."""
    engine = control.MiniControl(FakePueue(), root / 'host', clock=lambda: NOW, generation=lambda: 'gen-1', host='desk')
    with control.locked(engine.directory):
        engine.adopt(root / 'pid')
    view = engine.view()
    view.update(changes)
    return view


class StartupChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='rack-u34o-', dir='/tmp'); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def retained(self):
        return desk_view(self.root, pause_reasons=dict(pesadas=['ambiguous', 'startup'], jale=['startup']),
                         startup_held=dict(pesadas='log_existente'), startup_docker=False,
                         interrupted=[dict(id=3, created_at=STAMP)])

    def test_channel_publishes_held_and_docker_and_never_the_interrupted_list(self):
        view = self.retained()  # adopt once; a second adoption would raise ValueError by itself
        result = host_action.control(copy.deepcopy(view), 'desk')
        self.assertEqual(result['startup_held'], dict(pesadas='log_existente'))
        self.assertIs(result['startup_docker'], False)
        self.assertEqual(result['pause_reasons']['jale'], ['startup'])
        self.assertNotIn('interrupted', result)
        legacy = desk_view(self.root / 'old'); legacy.pop('startup_held'); legacy.pop('startup_docker')
        self.assertEqual(host_action.control(legacy, 'desk')['startup_held'], {})  # U3.3 controller
        for bad in (dict(startup_held=dict(jale='log_existente')),          # retained without ambiguous
                    dict(startup_held=dict(pesadas='inventado')),
                    dict(startup_docker=0),
                    dict(pause_reasons=dict(jale=['reinicio']))):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                host_action.control(dict(copy.deepcopy(view), **bad), 'desk')

    def test_snapshot_marks_only_listed_killed_rows_and_drops_the_list(self):
        controller = self.root / '.local/lib/pao-pueue-desk/control.py'
        controller.parent.mkdir(parents=True); controller.write_text('raise AssertionError("injected")')
        done = lambda result: {'Done': dict(enqueued_at=STAMP, start=STAMP, end='2026-11-14T15:59:00Z', result=result)}
        tasks = {'3': dict(id=3, created_at=STAMP, group='pesadas', status=done('Killed'), command='echo', path='/tmp'),
                 '4': dict(id=4, created_at=STAMP, group='jale', status=done('Killed'), command='echo', path='/tmp'),
                 '5': dict(id=5, created_at='2026-11-13T01:00:00Z', group='jale', status=done('Killed'), command='echo', path='/tmp'),
                 '6': dict(id=6, created_at=STAMP, group='jale', status=done('Success'), command='echo', path='/tmp')}
        listed = [dict(id=3, created_at=STAMP), dict(id=5, created_at=STAMP), dict(id=6, created_at=STAMP)]
        pueue = self.root / 'pueue'
        pueue.write_text('#!/usr/bin/python3\nprint(' + repr(json.dumps(dict(groups={'jale': dict(status='Paused', parallel_tasks=5)}, tasks=tasks))) + ')\n')
        pueue.chmod(0o755)
        with patch.object(snapshot, 'read_control', return_value=self.retained() | {'interrupted': listed}), \
             patch.object(snapshot, 'linux_capacity', return_value=None), patch.object(snapshot, 'desk_guard', return_value=None), \
             patch.object(snapshot, 'collect_usage', return_value=None):
            data = snapshot.collect('linux', 'desk', pueue=str(pueue), home=self.root, now=NOW)
        marked = {t['id'] for t in data['tasks'] if t.get('interrupted')}
        self.assertEqual(marked, {3})  # 4 not listed, 5 another created_at, 6 not Killed
        self.assertNotIn('interrupted', data['control'])
        data['tasks'] = [t for t in data['tasks']]
        valid = serve.validate_snapshot(json.loads(json.dumps(data)), 'desk')
        self.assertTrue(next(t for t in valid['tasks'] if t['id'] == 3)['interrupted'])

    def test_server_rejects_interrupted_marks_on_other_rows(self):
        base = dict(schema='rack-host@1', host='desk', os='linux', taken=NOW, cpu=None, mem=None, disk=None, guard=None,
                    groups=[dict(name='jale', status='Paused', slots=5)], errors=[])
        row = dict(id=1, group='jale', state='Done', name=None, scope=None, project=None, ticket=None, repo=None,
                   sha=None, branch=None, subject=None, priority=0, result='Killed', enqueued=None, start=None, end=None)
        self.assertTrue(serve.validate_snapshot(dict(base, tasks=[dict(row, interrupted=True)]), 'desk')['tasks'][0]['interrupted'])
        for bad in (dict(interrupted=1), dict(interrupted=True, result='Success'), dict(interrupted=True, state='Running')):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                serve.validate_snapshot(dict(base, tasks=[dict(row, **bad)]), 'desk')

    def fixture(self, control_view, tasks, groups=None):
        groups = groups or [dict(name=g, slots=n, status='Running') for g, n in control.desk_profile(NOW).items()]
        return dict(now=NOW, tickets={}, hosts=[dict(id='desk', name='Felix DESKTOP', fresh=True, age=0, estimates={},
                    actions=dict(reason=None), snapshot=dict(control=host_action.control(control_view, 'desk'),
                    groups=groups, tasks=tasks, cpu=dict(cores=16, load=[1, 1, 1]),
                    mem=dict(total=32 * 1024**3, available=20 * 1024**3, swap_total=0, swap_used=0)))])

    def test_production_ui_retained_group_interrupted_task_and_normal_desk(self):
        row = lambda **f: dict(dict(id=3, group='pesadas', state='Done', name='suite-sen-1-sol', scope=None, project=None,
                                     ticket='SEN-1', repo=None, sha=None, branch=None, subject='Contract completa', priority=0,
                                     result='Killed', enqueued=NOW - 900, start=NOW - 800, end=NOW - 60), **f)
        profile = control.desk_profile(NOW)
        paused = [dict(name=g, slots=n, status='Paused' if g in ('pesadas', 'jale') else 'Running') for g, n in profile.items()]
        restarted = self.fixture(self.retained(), [row(interrupted=True), row(id=4, group='jale', ticket=None)],
                                 paused + [dict(name='default', slots=1, status='Paused')])
        normal = self.fixture(desk_view(self.root / 'normal'), [])
        cut_default = self.fixture(desk_view(self.root / 'dflt', pause_reasons=dict(default=['ambiguous']),
                                             startup_held=dict(default='fuera_del_reparto')),
                                   [row(id=7, group='default', ticket=None, interrupted=True)],
                                   [dict(name=g, slots=n, status='Running') for g, n in profile.items()]
                                   + [dict(name='default', slots=1, status='Paused')])
        source = (BASE / 'index.html').read_text().split('<script>', 1)[1].split('</script>', 1)[0]
        result = subprocess.run(['node', str(BASE / 'tests/startup-ui-check.js')], text=True, capture_output=True, timeout=10,
                                input=json.dumps(dict(js=source, restarted=restarted, normal=normal, cut_default=cut_default)))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('UI startup OK', result.stdout)


if __name__ == '__main__':
    unittest.main()
