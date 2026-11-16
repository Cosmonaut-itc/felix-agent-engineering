"""U3.6 en el rack: canal `reorder`/`reorder_undo` contra el control.py real y un Pueue en memoria.

El host es el `MiniControl` de pueue-desk con el doble de `tests/test_reorder.py`
(edición real con `editor_prioridad.py`, reloj y señales falsos). Nunca se
lanza un controlador instalado ni un host compartido.
"""
import safety
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import test_actions

accion = test_actions.accion
host_channel = accion.host_channel
BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('rack_host_reorder', BASE.parent / 'pueue-desk/tests/test_reorder.py')
host_tests = importlib.util.module_from_spec(spec); spec.loader.exec_module(host_tests)
control = host_tests.control
PAUSED = 'No se puede reordenar mientras el grupo está pausado'
# Titles as the panel shows them; synthetic, no real ticket data.
TICKETS = {'SEN-5742': 'SUA: con la nómina dual apagada, usar el salario real y no el fiscal guardado',
           'SEN-5667': 'Corrección de nómina: recalcular un periodo anterior con el salario vigente',
           'SEN-5703': '«La empresa paga por el trabajador»: explicar qué hace cada opción y cuánto cuesta',
           'SEN-4481': 'Cambiar la configuración de nómina después de crear el borrador <b>sin</b> HTML',
           'SEN-5731': 'Aguinaldo: no volver a calcular el aguinaldo de una baja que ya lo cobró'}


class RackReorderFixture(unittest.TestCase):
    def setUp(self):
        self.host_side = host_tests.ReorderFixture('run')
        self.host_side.setUp()
        self.addCleanup(self.host_side.doCleanups)
        self.p = self.host_side.p
        self.host_side.queue([0] * 5)
        self.tmp = tempfile.TemporaryDirectory(prefix='u36-opus-rack-', dir='/tmp'); self.addCleanup(self.tmp.cleanup)
        self.host = dict(id='mini', transport='local', actions=dict(transport='local', reason=None,
                         controllers={'remote-run': accion.CONTROLLERS['remote-run']}))
        self.wires = []
        self.actions = accion.Actions([self.host], Path(self.tmp.name) / 'rack', self.payload, self.executor,
                                      clock=lambda: 1000)
        self.snapshot()

    @property
    def engine(self):
        return self.host_side.engine

    def executor(self, host, target, wire, timeout):
        self.assertEqual(target, 'host-control'); self.assertLessEqual(timeout, 10)
        self.wires.append(copy.deepcopy(wire))
        with control.locked(self.engine.directory):
            return self.engine.handle(wire)

    def snapshot(self):
        """What serve.py publishes: the host view through the rack whitelist."""
        self.view = host_channel.control(self.engine.view(), 'mini')

    def payload(self):
        return dict(now=1000, hosts=[dict(id='mini', fresh=True, age=0, snapshot=dict(control=self.view))])

    def page(self, now=1000):
        """The rack.json the browser reads: the Mini with its real view and the fake Pueue's rows."""
        names = sorted(TICKETS)
        tasks = [dict(id=t['id'], group=t['group'], state='Queued', name=('contract', 'types', 'e2e', 'gates', 'lint')[t['id'] % 5],
                      ticket=names[t['id'] % 5], project='sen-checkin', enqueued=now - 60 * (12 - t['id']), start=None, end=None,
                      result=None, scope=None, repo=None, sha=None, branch='codex/sen-%d' % t['id'], subject=None,
                      priority=t['priority'], control=dict(launcher='remote-run', ref='20261116-10000%d-abcdef' % t['id'],
                      expected=dict(task_id=t['id'], state='Queued'), reason=None))
                 for t in sorted(self.p.tasks.values(), key=lambda t: t['id'])]
        running = [dict(tasks[0], id=90 + k, state='Running', start=now - 300 * (k + 1), enqueued=now - 900, control=None,
                        ticket='SEN-60%d' % k, branch=None) for k in range(2)]
        groups = [dict(name=g, slots=n, status=self.p.groups[g]['status']) for g, n in control.MINI_PROFILE.items()]
        snapshot = dict(schema='rack-host@1', host='mini', taken=now, groups=groups, tasks=running + tasks, control=self.view,
                        cpu=dict(cores=10, load=[6, 6, 6]), mem=dict(total=16 * 1024**3, available=6 * 1024**3, swap_total=0, swap_used=0))
        return dict(schema='rack@1', now=now, tickets={k: dict(title=v, project='Jale') for k, v in TICKETS.items()},
                    hosts=[dict(id='mini', name='Mac Mini', fresh=True, age=0, error=None, snapshot=snapshot, estimates={},
                                actions=dict(reason=None))])

    def receipts(self, order):
        """Real host receipts through the rack: the reorder and its undo, as the browser receives them."""
        code, applied = self.actions.post(self.request(order))
        assert applied['status'] == 'applied', applied
        undo_code, undone = self.actions.post(self.undo(applied))
        assert undone['status'] == 'applied', undone
        return dict(applied=dict(applied, served_at=1001), undone=dict(undone, served_at=1002))

    def request(self, order, ident='reorder-00000001', group='suites', dry_run=False):
        info = self.view['reorder'][group]
        return dict(schema='rack-action@1', op='reorder', host='mini', action_id=ident, dry_run=dry_run,
                    expected=dict(group=group, generation=self.view['generation'], queue_etag=info['queue_etag'],
                                  queue=copy.deepcopy(info['queue'])), change=dict(order=order))

    def undo(self, applied, ident='undo-00000001'):
        r = applied['result']
        return dict(schema='rack-action@1', op='reorder_undo', host='mini', action_id=ident, dry_run=False,
                    expected=dict(group=r['group'], generation=self.view['generation'], queue_etag=r['after_etag']),
                    undo_of=r['undo_of'])


class ChannelTests(RackReorderFixture):
    def test_drag_applies_and_undo_restores_exact_priorities(self):
        code, answer = self.actions.post(self.request([1, 5, 2, 3, 4]))
        self.assertEqual((code, answer['status'], answer.get('effects')), (200, 'applied', True))
        self.assertEqual(self.p.order(), [1, 5, 2, 3, 4])
        r = answer['result']
        self.assertEqual(r['undo_of'], 'reorder-00000001'); self.assertIs(r['undo'], True)
        self.assertEqual(r['after'], [[1, 5], [2, 3], [3, 2], [4, 1], [5, 4]])
        self.assertEqual(set(r), {'group', 'order', 'writes', 'estimate_ms', 'held', 'undo', 'undo_of',
                                  'before', 'after', 'after_etag', 'undo_seconds'})
        self.assertEqual(self.actions.get('reorder-00000001')[1], answer)
        self.assertEqual([w['op'] for w in self.wires], ['reorder'])
        code, undone = self.actions.post(self.undo(answer))
        self.assertEqual((code, undone['status']), (200, 'applied'))
        self.assertEqual(self.p.priorities(), {i: 0 for i in range(1, 6)})
        self.assertIsNone((undone.get('result') or {}).get('undo_of', 'ausente'))  # an undo is never undone from the toast

    def test_undo_conflict_has_fixed_text_and_no_effect(self):
        answer = self.actions.post(self.request([1, 5, 2, 3, 4]))[1]
        self.p.add(host_tests.make_task(9, 0))
        before = self.p.priorities()
        code, undone = self.actions.post(self.undo(answer))
        self.assertEqual((code, undone['status'], undone.get('effects')), (409, 'conflict', False))
        self.assertEqual(undone['error'], dict(code='undo_conflict', message='La fila cambió; no se puede deshacer'))
        self.assertEqual(self.p.priorities(), before)

    def test_stale_queue_is_terminal_conflict_without_pause(self):
        request = self.request([5, 4, 3, 2, 1])
        self.p.add(host_tests.make_task(9, 0))
        code, answer = self.actions.post(request)
        self.assertEqual((code, answer['status'], (answer.get('error') or {}).get('code')), (409, 'conflict', 'stale_queue'))
        self.assertEqual(self.p.calls, [])
        self.assertNotIn('reorder', answer)  # the host's fresh eligibility is not relayed

    def test_lost_answer_is_unknown_then_settled_by_status(self):
        def lose(host, target, wire, timeout):
            self.executor(host, target, wire, timeout); raise OSError('lost answer')
        self.actions.executor = lose
        request = self.request([1, 5, 2, 3, 4])
        self.assertEqual(self.actions.post(request)[1]['status'], 'unknown')
        self.actions.executor = self.executor
        code, answer = self.actions.get(request['action_id'])
        self.assertEqual((code, answer['status']), (200, 'applied'))
        self.assertEqual([w['op'] for w in self.wires], ['reorder', 'status'])
        self.assertEqual(self.p.calls.count(('pause', '--wait', '-g', 'suites')), 1)

    def test_old_host_is_translated_to_unsupported_without_effects(self):
        def old(host, target, wire, timeout):
            self.wires.append(wire)
            return dict(schema='rack-host-result@1', action_id=wire.get('action_id'), status='recovery_error',
                        error=dict(code='recovery_error', message='Estado de control ilegible'))
        self.actions.executor = old
        code, answer = self.actions.post(self.request([1, 5, 2, 3, 4]))
        self.assertEqual((code, answer['status'], answer.get('effects'), (answer.get('error') or {}).get('code')),
                         (409, 'unsupported', False, 'unsupported'))

    def test_current_host_uncertain_reorder_stays_unknown(self):
        self.actions.executor = lambda host, target, wire, timeout: dict(
            schema='rack-host-result@1', action_id=wire['action_id'], status='unknown',
            error=dict(code='recovery_error', message='x'))
        self.assertEqual(self.actions.post(self.request([1, 5, 2, 3, 4]))[1]['status'], 'unknown')

    def test_reorder_and_slots_block_each_other_on_the_host(self):
        self.actions.executor = lambda host, target, wire, timeout: dict(
            schema='rack-host-result@1', action_id=wire['action_id'], status='unknown')
        self.actions.post(self.request([1, 5, 2, 3, 4]))
        slots = dict(schema='rack-action@1', op='slots', host='mini', action_id='slots-00000001', dry_run=False,
                     expected={k: self.view[k] for k in ('revision', 'generation', 'policy_hash', 'mode', 'slots')},
                     change=dict(group='suites', delta=-1))
        self.assertEqual(self.actions.post(slots)[0], 409)

    def test_poll_identity_keeps_the_mini_budget(self):
        request = self.request([1, 5, 2, 3, 4])
        self.actions.respond('post', request)
        self.assertEqual(self.actions.identities.get(request['action_id'], {}).get('host'), 'mini')


class EligibilityTests(RackReorderFixture):
    def test_without_capability_or_with_closed_group_nothing_reaches_the_host(self):
        request = self.request([1, 5, 2, 3, 4])
        self.view = dict(self.view, capabilities=dict(self.view['capabilities'], reorder=False))
        code, answer = self.actions.post(dict(request, action_id='reorder-00000002'))
        self.assertEqual((code, answer['status']), (409, 'rejected'))
        self.snapshot()
        self.view['reorder']['suites'] = dict(enabled=False, code='paused', reason=PAUSED + ' · pausa manual')
        code, answer = self.actions.post(dict(request, action_id='reorder-00000003'))
        self.assertEqual((code, answer['status'], (answer.get('error') or {}).get('message')), (409, 'rejected', PAUSED + ' · pausa manual'))
        self.assertEqual(self.wires, [])

    def test_paused_group_shows_its_owner_including_recovery(self):
        for reason, label in (('manual', 'pausa manual'), ('reorder', 'en pausa recuperando…'),
                              ('pressure', 'pausa por presión')):
            with self.engine.connect(write=True) as db:
                state = self.engine.state(db); state['reasons']['suites'] = [reason]; self.engine.store(db, state)
            self.p.groups['suites']['status'] = 'Paused'
            try:
                self.snapshot()
            except ValueError as error:
                self.fail('a %r pause broke the whole panel: %s' % (reason, error))
            self.assertEqual(self.view['pause_reasons']['suites'], [reason])
            self.assertEqual(self.view['reorder']['suites'], dict(enabled=False, code='paused', reason=PAUSED + ' · ' + label))

    def test_dry_run_reaches_host_without_receipt(self):
        code, answer = self.actions.post(self.request([1, 5, 2, 3, 4], dry_run=True))
        self.assertEqual((code, answer['status'], answer['result']['writes']), (200, 'dry_run', 5))
        self.assertIs(answer['result']['undo'], False); self.assertIsNone(answer['result']['undo_of'])
        with self.actions.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
        self.assertEqual(self.p.calls, [])


class WhitelistTests(RackReorderFixture):
    def test_request_variant_is_closed(self):
        request = self.request([1, 5, 2, 3, 4])
        self.assertEqual(accion.validate(request), request)
        bad = []
        for path, value in ((('host',), 'desk'), (('extra',), 1), (('expected', 'group'), 'hasito'),
                            (('expected', 'queue_etag'), 'A' * 64), (('expected', 'generation'), ''),
                            (('change', 'order'), [1, 5, 2, 3]), (('change', 'order'), [1, 5, 2, 3, 3]),
                            (('change', 'order'), [1, 5, 2, 3, True]), (('dry_run',), 0)):
            changed = copy.deepcopy(request); target = changed
            for key in path[:-1]: target = target[key]
            target[path[-1]] = value; bad.append(changed)
        for row in ([1, 'z' * 32, 0], [1, '0' * 32, 2**31], [1, '0' * 32], [-1, '0' * 32, 0]):
            changed = copy.deepcopy(request); changed['expected']['queue'][0] = row; bad.append(changed)
        long = copy.deepcopy(request)
        long['expected']['queue'] = [[i, '0' * 32, 0] for i in range(65)]; long['change']['order'] = list(range(65))
        bad.append(long)
        undo = dict(schema='rack-action@1', op='reorder_undo', host='mini', action_id='undo-00000001', dry_run=False,
                    expected=dict(group='suites', generation='g', queue_etag='a' * 64), undo_of='reorder-00000001')
        self.assertEqual(accion.validate(undo), undo)
        bad += [dict(undo, undo_of='x'), dict(undo, expected=dict(undo['expected'], queue=[]))]
        for value in bad:
            with self.assertRaises(ValueError, msg=value): accion.validate(value)

    def test_view_never_forwards_host_text_and_desk_cannot_claim_reorder(self):
        view = self.engine.view()
        for item in (dict(enabled=False, code='paused', reason=PAUSED + ' · /Users/x/secret'),
                     dict(enabled=False, code='other', reason='x'),
                     dict(view['reorder']['suites'], label='SEN-1'),
                     dict(view['reorder']['suites'], max_writes=65)):
            changed = copy.deepcopy(view); changed['reorder']['suites'] = item
            with self.assertRaises(ValueError): host_channel.control(changed, 'mini')
        # Other codes never carry host text: the rack writes its own.
        changed = copy.deepcopy(view); changed['reorder']['suites'] = dict(enabled=False, code='held_tasks', reason='cmd=rm -rf')
        self.assertEqual(host_channel.control(changed, 'mini')['reorder']['suites']['reason'],
                         control.REORDER_MESSAGES['held_tasks'])
        changed = copy.deepcopy(view); changed['reorder']['hasito'] = changed['reorder']['suites']
        with self.assertRaises(ValueError): host_channel.control(changed, 'mini')
        old = {k: v for k, v in view.items() if k != 'reorder'}
        self.assertIs(host_channel.control(old, 'mini')['capabilities']['reorder'], False)
        self.assertIs(view['capabilities']['reorder'], True)

    def test_host_and_rack_messages_agree(self):
        for code, message in control.REORDER_MESSAGES.items():
            self.assertEqual(host_channel.REORDER_MESSAGES[code], message)
        self.assertEqual(set(control.PAUSE_LABELS.values()), set(host_channel.PAUSE_LABELS))
        self.assertEqual(set(control.PAUSE_LABELS), set(host_channel.PAUSE_REASONS))

    def test_receipt_payload_is_rebuilt_from_ids_and_numbers(self):
        answer = self.actions.post(self.request([1, 5, 2, 3, 4]))[1]
        self.assertTrue(all(type(x) is int for row in answer['result']['after'] for x in row))
        request = self.request([1, 5, 2, 3, 4], ident='reorder-00000009')
        base = dict(schema='rack-host-result@1', action_id=request['action_id'], status='applied', error=None,
                    effects=True, result=dict(group='suites', order=[1, 5, 2, 3, 4], writes=4, estimate_ms=470,
                    held=None, undo=True, before=[[i, 0] for i in range(1, 6)],
                    after=[[1, 5], [2, 3], [3, 2], [4, 1], [5, 4]], after_etag='b' * 64, undo_seconds=60))
        self.assertEqual(host_channel.sanitize(request, copy.deepcopy(base), 'reorder')['result'].get('undo_of'), request['action_id'])
        for key, value in (('group', 'rapidas'), ('after', [[1, 'x']]), ('order', [1, 1]), ('held', 'manual'),
                           ('after_etag', None), ('before', [[1, 0], [2, 0], [3, 0], [4, 0], [6, 0]])):
            changed = copy.deepcopy(base); changed['result'][key] = value
            with self.assertRaises(ValueError, msg=key): host_channel.sanitize(request, changed, 'reorder')
        changed = copy.deepcopy(base); changed['result']['command'] = 'secret'
        self.assertNotIn('command', host_channel.sanitize(request, changed, 'reorder')['result'])


class UITests(RackReorderFixture):
    """Production JS in a synthetic DOM; the drag itself is checked in a real browser (README)."""
    def run_ui(self, scenario, **extra):
        if not shutil.which('node'):
            self.skipTest('node no disponible')
        source = (BASE / 'index.html').read_text().split('<script>', 1)[1].split('</script>', 1)[0]
        result = subprocess.run(['node', str(BASE / 'tests/reorder-ui-check.js')], text=True, capture_output=True, timeout=20,
                                input=json.dumps(dict(js=source, scenario=scenario, **extra), ensure_ascii=False))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('UI reorder %s OK' % scenario, result.stdout)

    def scenario(self, name):
        page = self.page()
        side = RackReorderFixture('run'); side.setUp(); self.addCleanup(side.doCleanups)
        conflict = dict(schema='rack-action@1', action_id='x' * 32, status='conflict', effects=False, served_at=1003,
                        error=dict(code='undo_conflict', message='La fila cambió; no se puede deshacer'))
        self.run_ui(name, fixture=page, receipts=side.receipts([1, 5, 2, 3, 4]), conflict=conflict)

    def test_drag_five_to_two_shows_progress_highlight_and_undo_toast(self):
        self.scenario('applied')

    def test_undo_request_is_bound_to_the_receipt(self):
        self.scenario('undo')

    def test_unknown_shows_no_success(self):
        self.scenario('unknown')

    def test_undo_conflict_text(self):
        self.scenario('undo-conflict')

    def test_too_long_is_rejected_before_any_request(self):
        self.scenario('too-long')

    def test_keyboard_alt_keeps_focus_target(self):
        self.scenario('keyboard')

    def test_paused_group_shows_reason_and_is_not_draggable(self):
        with self.engine.connect(write=True) as db:
            state = self.engine.state(db); state['reasons']['suites'] = ['manual']; self.engine.store(db, state)
        self.p.groups['suites']['status'] = 'Paused'
        self.snapshot()
        self.run_ui('paused', fixture=self.page())

    def test_desk_and_actions_off_never_offer_reorder(self):
        self.run_ui('off', fixture=self.page())


if __name__ == '__main__':
    unittest.main()
