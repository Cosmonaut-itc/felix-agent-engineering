"""CC3-P2. H1 documents in a private directory and synthetic Pueue status."""
import hashlib
import io
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_client_actions import a, c, control, stock_engine
import test_slots


REF = '20261118-120000-abcdef'
ACTION = 'cancel-action-0001'
LOCAL = 'aaaa0000-0000-0000-0000-000000000001'
ATTEMPT = '2026-11-18T11:59:59.999999Z'
CREATED = '2026-11-18T12:00:00.000000Z'
ACCEPTED = '2026-11-18T12:00:02.000000Z'
ENQUEUED = '2026-11-18T12:00:03.000000Z'
STARTED = '2026-11-18T12:00:04.000000Z'
BIRTH = 'darwin:123:45'
WORKER = 'remote-run-no-payload@1'


class Fixture:
    def __init__(self, authority, generation):
        self.root = authority / 'runs'
        self.root.mkdir(mode=0o700)
        self.run = self.root / REF
        self.run.mkdir(mode=0o700)
        self.config = authority / 'remote-run-adapter.json'
        self.producer = b'raise RuntimeError("producer must never execute")\n'
        self.copy = self.run / 'pueue.py'
        self.copy.write_bytes(self.producer)
        self.copy.chmod(0o600)
        digest = hashlib.sha256(self.producer).hexdigest()
        self.write(self.config, dict(schema='remote-run-adapter@1', root=str(self.root),
                                     artifacts=[dict(producer=digest, worker=WORKER)]))
        self.stable = dict(generation=hashlib.sha256(generation.encode()).hexdigest(), task_id=77,
                           created_at=CREATED, attempt=ATTEMPT)
        self.stable['fingerprint'] = a.digest(dict(self.stable, ref=REF, launcher='remote-run'))
        self.queued = self.identity('Queued', enqueued_at=ATTEMPT)
        self.stashed = self.identity('Stashed')
        self.running = self.identity('Running', enqueued_at=ENQUEUED, started_at=STARTED)
        self.done = dict(self.running, state='Done')
        self.binding = self.document('binding@2', group='suites', identity=self.queued,
                                     producer=digest, worker=WORKER)
        self.links = dict(binding=a.digest(self.binding), local_attempt=LOCAL)
        self.attempt = self.document('attempt@2', **self.links, identity=self.stable, cause='add')
        self.links['attempt_receipt'] = a.digest(self.attempt)
        self.publish_binding()
        self.action()
        self.select('hold')

    def identity(self, state, **fields):
        return dict(dict(self.stable, state=state, enqueued_at=None, enqueue_at=None, started_at=None), **fields)

    @staticmethod
    def document(schema, **fields):
        return dict(schema='remote-run-client-' + schema, launcher='remote-run', ref=REF, **fields)

    @staticmethod
    def write(path, value):
        path.write_text(a.canonical(value) + '\n')
        path.chmod(0o600)

    def file(self, kind, attempt=False):
        return self.run / ('pueue.client-' + kind + '-' + a.digest(LOCAL if attempt else [REF, ACTION]) + '.json')

    def publish_binding(self):
        self.write(self.run / 'pueue.client.json', self.binding)
        self.write(self.run / 'pueue.client-add.json', self.binding)
        self.links['binding'] = a.digest(self.binding)
        self.attempt['binding'] = self.links['binding']
        self.links['attempt_receipt'] = a.digest(self.attempt)
        self.write(self.file('attempt', True), self.attempt)
        self.write(self.run / 'pueue.client-current.json', self.document('current@1', revision=1, **self.links))

    def select(self, phase, action_id=ACTION):
        self.write(self.run / 'pueue.client-selection.json', dict(schema='remote-run-client-selection@1',
                   request=dict(ref=REF, action_id=action_id), phase=phase))

    def action(self, mode='rack-queued', **changes):
        value = dict(self.document('action@2', **self.links, identity=self.stable,
                                  action_id=ACTION, mode=mode), **changes)
        self.write(self.file('mode'), value)

    def accept(self, action_id=ACTION):
        self.accepted = dict(action_id=action_id, attempt=ATTEMPT, accepted_at=ACCEPTED, origin='rack-panel')
        self.accepted['receipt'] = a.digest(self.accepted)
        self.write(self.file('acceptance'), self.document('acceptance@1', **self.links,
                                                         action_id=ACTION, acceptance=self.accepted))
        return self.accepted

    def worker(self, queued=True, **changes):
        self.started = dict(self.document('worker-start@2', **self.links, identity=self.stable,
                            worker=WORKER, pid=123, birth=BIRTH, action_id=ACTION if queued else None,
                            acceptance_receipt=self.accepted['receipt'] if queued else None), **changes)
        self.write(self.file('worker-start', True), self.started)

    def complete(self, kind='no-payload', **changes):
        value = dict(self.document('completion@1', **self.links, identity=self.stable, action_id=ACTION,
                     acceptance_receipt=self.accepted['receipt'], worker_start=a.digest(self.started),
                     pid=123, birth=BIRTH, kind=kind, exit=130,
                     no_payload=kind == 'no-payload', payload_complete=kind == 'terminated'), **changes)
        value['receipt'] = a.digest(value)
        self.write(self.file('completion'), value)
        return dict(kind=kind, attempt=ATTEMPT, receipt=value['receipt'],
                    acceptance_receipt=self.accepted['receipt'], exit=130)


class RemoteRunTests(unittest.TestCase):
    def setUp(self):
        test_slots.SlotsTests.setUp(self)
        self.fixture = Fixture(self.root, self.gen)
        self.tasks = {'77': dict(id=77, created_at=CREATED, group='suites',
                                status={'Queued': dict(enqueued_at=ATTEMPT)})}
        self.reads, self.effects = 0, []
        self.p.status = self.status
        self.engine.client_birth = control.priority_helper().birth
        life = stock_engine(self.engine, os.getpid())
        life.start()
        self.addCleanup(life.stop)
        self.actions = a.ClientActions(self.engine, rpc=self.rpc, **self.engine_options())
        self.cap = dict(schema=c.SCHEMA, launcher='remote-run', ref=REF, op='capabilities')
        self.hold = self.request('hold', self.fixture.queued)

    def engine_options(self):
        # Baseline can construct both paths and fail on behavior.
        return dict(birth=self.engine.client_birth, exclusion=self.engine.client_exclusion)

    def status(self):
        self.reads += 1
        return dict(tasks=self.tasks, groups=self.p.groups)

    def rpc(self, verb, *args):
        self.effects.append(verb)
        if verb == 'stash':
            self.tasks['77']['status'] = {'Stashed': dict(enqueue_at=None)}
        elif verb == 'start':
            self.tasks['77']['status'] = {'Running': dict(enqueued_at=ENQUEUED, start=STARTED)}
        elif verb == 'remove':
            self.tasks['77'] = dict(id=77, created_at=STARTED, group='default',
                                    status={'Queued': dict(enqueued_at=STARTED)})

    def request(self, op, expected, acceptance=None, mode='rack-queued', proof=None):
        result = dict(schema=c.SCHEMA, launcher='remote-run', ref=REF, op=op, action_id=ACTION,
                      phase=op, mode=mode, expected=expected, acceptance=acceptance, proof=proof)
        if op == 'signal':
            result.update(phase='signal:TERM', signal='TERM')
        return result

    def act(self, request, actions=None):
        body, code = (actions or self.actions).dispatch(a.canonical(request).encode())
        self.assertEqual(code, 0, body)
        return body

    def active(self):
        self.assertEqual(self.act(self.cap)['identity'], self.fixture.queued)

    def selected_start(self):
        self.assertEqual(self.act(self.hold)['status'], 'held')
        accepted = self.fixture.accept()
        self.fixture.select('cancel-no-payload')
        return self.request('cancel-no-payload', self.fixture.stashed, accepted)

    def done(self):
        self.tasks['77']['status'] = {'Done': dict(enqueued_at=ENQUEUED, start=STARTED,
                                                   end='2026-11-18T12:00:05Z', result='Killed')}

    def started(self):
        request = self.selected_start()
        self.assertEqual(self.act(request)['status'], 'started')
        self.fixture.worker()
        return request

    def recoverer(self):
        return a.Reconciler(self.engine, **self.engine_options())

    def unavailable(self, reason, operation, *args):
        with self.assertRaises(a.Unavailable) as error:
            operation(*args)
        self.assertEqual(error.exception.reason, reason)

    def test_observe_h1_and_caja(self):
        self.active()
        self.engine.profile = dict(self.engine.profile, caja=1)
        self.tasks['77']['group'] = 'caja'
        self.fixture.binding['group'] = 'caja'
        self.fixture.publish_binding()
        self.active()
        with self.engine.connect() as db:
            state = self.engine.state(db)
        self.assertEqual(self.actions.observe(self.cap, state, None),
                         dict(group='caja', identity=self.fixture.queued))

    def test_verify_acceptance_and_stock_q(self):
        request = self.selected_start()
        self.assertEqual(self.act(request)['identity'], self.fixture.running)
        self.assertEqual(self.effects, ['stash', 'start'])
        self.assertEqual(self.act(self.cap)['identity'], self.fixture.running)

    def test_acceptance_must_match_original(self):
        request = self.selected_start()
        fake = dict(request['acceptance'], origin=None)
        del fake['receipt']
        fake['receipt'] = a.digest(fake)
        self.assertEqual(self.act(dict(request, acceptance=fake))['reason'], 'identity')
        self.assertEqual(self.effects, ['stash'])

    def test_recover_closes_once_from_live_done(self):
        request = self.started()
        proof = self.fixture.complete()
        self.done()
        reconciler = self.recoverer()
        item, = reconciler.copies(None, advance=False)
        bundle = reconciler.actions.adapter.recover(request, item['state'], item['reservation'],
                                                    item['record']['process'], self.status())
        self.assertEqual(set(bundle), {'observation', 'finish'})
        self.assertEqual(bundle['finish']['proof'], proof)
        self.engine.reconcile_clients()
        finish = self.actions.record(bundle['finish'])['response']
        self.assertEqual((finish['status'], finish['reservation']), ('finished', 'closed'))
        self.assertEqual(self.actions.record(request)['response']['status'], 'started')
        self.assertFalse(self.actions.reservation(request)['open'])
        self.assertEqual(self.effects, ['stash', 'start'])

    def test_completion_cannot_settle_a_queue_phase(self):
        request = self.started()
        self.fixture.complete()
        self.done()
        record = self.actions.record(request)
        record['response'] = self.actions.reply(request, 'unknown', 'rpc_unresolved', held=True)
        reservation = self.actions.reservation(request)
        reservation['uncertain'] = True
        self.actions.save(record, reservation)
        # Remove only the seal of our synthetic emitter. No publication remains
        # that can attribute or settle this phase.
        (self.root / 'client-rpc' / record['process']['dispatch'] / 'seal.json').unlink()
        self.engine.reconcile_clients()
        self.assertEqual(self.actions.record(request)['response']['reason'], 'rpc_unresolved')
        self.assertTrue(self.actions.reservation(request)['open'])

    def test_apply_revalidates_completion(self):
        request = self.started()
        self.fixture.complete()
        self.done()
        reconciler = self.recoverer()
        item, = reconciler.copies(None, advance=False)
        proposal = reconciler.collect(item, self.status())
        self.assertIsNotNone(proposal['finish'])
        self.fixture.file('completion').unlink()
        self.assertTrue(reconciler.apply(item, proposal, None))
        self.assertTrue(self.actions.reservation(request)['open'])

    def test_completion_requires_current_done(self):
        request = self.started()
        proof = self.fixture.complete()
        response = self.act(self.request('finish', self.fixture.done, request['acceptance'], proof=proof))
        self.assertEqual(response['reason'], 'stale_state')
        reconciler = self.recoverer()
        item, = reconciler.copies(None, advance=False)
        proposal, = reconciler.gather([item], None)
        self.assertEqual(proposal.get('observation'), dict(group='suites', identity=self.fixture.running))
        self.assertIsNone(proposal['finish'])
        self.assertTrue(self.actions.reservation(request)['open'])

    def test_completion_from_other_worker_keeps_open(self):
        request = self.started()
        self.fixture.complete(pid=124)
        self.done()
        self.engine.reconcile_clients()
        self.assertTrue(self.actions.reservation(request)['open'])

    def test_running_worker_can_predate_acceptance(self):
        f = self.fixture
        f.action(mode='rack-running')
        f.select('signal:TERM')
        accepted = f.accept()
        f.worker(queued=False)
        self.tasks['77']['status'] = {'Running': dict(enqueued_at=ATTEMPT, start=STARTED)}
        observed = f.identity('Running', enqueued_at=ATTEMPT, started_at=STARTED)
        request = self.request('signal', observed, accepted, mode='rack-running')
        self.assertEqual(self.act(request)['status'], 'signalled')
        f.complete(kind='terminated')
        self.tasks['77']['status'] = {'Done': dict(enqueued_at=ATTEMPT, start=STARTED)}
        self.engine.reconcile_clients()
        self.assertFalse(self.actions.reservation(request)['open'])

    def test_stock_retirement_retains_original_id_when_reused(self):
        f = self.fixture
        f.action(mode='own-legacy')
        hold = dict(self.hold, mode='own-legacy')
        self.assertEqual(self.act(hold)['status'], 'held')
        f.select('retire-held')
        original = f.document('retirement@1', **f.links, identity=f.stable, action_id=ACTION,
                              mode='own-legacy', previous_result=None, stop_exit=130)
        original['receipt'] = a.digest(original)
        f.write(f.file('retirement'), original)
        action = self.request('retire-held', f.stashed, mode='own-legacy')
        response = self.act(action)
        self.assertEqual(response['status'], 'retired')
        self.assertEqual(self.act(self.cap)['identity'], f.identity('Absent'))
        self.engine.reconcile_clients()
        self.assertFalse(self.actions.reservation(action)['open'])

    def test_other_creation_does_not_become_absent(self):
        self.active()
        self.tasks['77']['created_at'] = STARTED
        self.assertEqual(self.act(self.cap)['reason'], 'identity')

    def test_different_group_is_identity(self):
        self.active()
        self.tasks['77']['group'] = 'default'
        self.assertEqual(self.act(self.cap)['reason'], 'identity')

    def test_old_attempt_original_is_identity(self):
        f = self.fixture
        self.active()
        original = dict(f.attempt, local_attempt='another-run-attempt')
        f.write(f.file('attempt', True), original)
        current = f.document('current@1', revision=1, **dict(f.links, attempt_receipt=a.digest(original)))
        f.write(f.run / 'pueue.client-current.json', current)
        self.assertEqual(self.act(self.cap)['reason'], 'identity')

    def test_current_replaced_mid_read_is_identity(self):
        self.active()
        path = self.fixture.run / 'pueue.client-current.json'
        original = self.p.status

        def status():
            self.fixture.write(path, dict(self.fixture.document('current@1', **self.fixture.links), revision=2))
            return original()

        self.p.status = status
        self.assertEqual(self.act(self.cap)['reason'], 'identity')

    def test_different_queue_attempt_is_identity(self):
        self.active()
        self.tasks['77']['status']['Queued']['enqueued_at'] = ENQUEUED
        self.assertEqual(self.act(self.cap)['reason'], 'identity')

    def test_old_action_original_is_identity(self):
        self.active()
        self.fixture.action(action_id='another-action-0001')
        self.assertEqual(self.act(self.hold)['reason'], 'identity')
        self.assertEqual(self.effects, [])

    def test_old_generation_is_startup(self):
        self.active()
        self.gen = 'daemon-2'
        with self.engine.connect(write=True) as db:
            state = self.engine.state(db)
            state['generation'] = self.gen
            self.engine.store(db, state)
        self.assertEqual(self.act(self.hold)['reason'], 'startup')

    def test_daemon_replaced_during_observe_is_startup(self):
        self.active()
        original = self.p.status

        def status():
            self.gen = 'daemon-2'
            return original()

        self.p.status = status
        self.assertEqual(self.act(self.hold)['reason'], 'startup')

    def test_legacy_binding_is_unsupported_before_daemon(self):
        self.active()
        self.fixture.write(self.fixture.run / 'pueue.client.json',
                           dict(schema='remote-run-client-binding@1', identity=None, reason='identity'))
        self.p.status = lambda: self.fail('legacy original reached daemon')
        self.assertEqual(self.act(self.cap)['reason'], 'unsupported')

    def test_unaccredited_producer_is_unsupported(self):
        self.active()
        source = b'unaccredited producer bytes'
        self.fixture.copy.write_bytes(source)
        self.fixture.binding['producer'] = hashlib.sha256(source).hexdigest()
        self.fixture.publish_binding()
        self.assertEqual(self.act(self.cap)['reason'], 'unsupported')

    def test_producer_copy_is_hashed(self):
        self.active()
        self.fixture.copy.write_bytes(b'other producer')
        self.assertEqual(self.act(self.cap)['reason'], 'unsupported')

    def test_unaccredited_worker_is_unsupported(self):
        self.active()
        self.fixture.binding['worker'] = 'not-the-worker@1'
        self.fixture.publish_binding()
        self.assertEqual(self.act(self.cap)['reason'], 'unsupported')

    def test_unknown_worker_original_keeps_open(self):
        request = self.started()
        self.fixture.worker(worker='not-the-worker@1')
        self.fixture.complete()
        self.done()
        self.engine.reconcile_clients()
        self.assertTrue(self.actions.reservation(request)['open'])

    def test_no_allowlist_is_unsupported(self):
        self.active()
        self.fixture.config.unlink()
        self.p.status = lambda: self.fail('absent allowlist reached daemon')
        self.assertFalse(self.actions.supports(self.cap))
        self.assertEqual(self.act(self.cap)['reason'], 'unsupported')

    def test_missing_ref_is_not_found(self):
        self.active()
        self.assertEqual(self.act(dict(self.cap, ref='missing-run-1'))['reason'], 'not_found')

    def test_missing_reserved_directory_keeps_open(self):
        request = self.started()
        self.fixture.run.rename(self.fixture.root / 'saved')
        self.engine.reconcile_clients()
        reserve = self.actions.reservation(request)
        self.assertEqual(reserve['reconcile_reason'], 'evidence_missing')
        self.assertTrue(reserve['open'])

    def test_directory_swap_between_lookup_and_use(self):
        self.active()
        original = self.p.status

        def status():
            run = self.fixture.run
            run.rename(self.fixture.root / 'saved')
            run.mkdir(mode=0o700)
            return original()

        self.p.status = status
        self.assertEqual(self.act(self.cap)['reason'], 'identity')

    def test_symlink_original_is_rejected(self):
        self.active()
        path = self.fixture.run / 'pueue.client.json'
        content = path.with_suffix('.backup')
        path.rename(content)
        path.symlink_to(content.name)
        original = os.open

        def open_file(name, flags, *args, **kwargs):
            if name == path.name:
                self.assertTrue(flags & os.O_NOFOLLOW)
            return original(name, flags, *args, **kwargs)

        with patch.object(os, 'open', open_file):
            self.assertEqual(self.act(self.hold)['reason'], 'identity')

    def test_fifo_is_opened_nonblocking_and_rejected(self):
        self.active()
        path = self.fixture.run / 'pueue.client.json'
        path.unlink()
        os.mkfifo(path, mode=0o600)
        original = os.open

        def open_file(name, flags, *args, **kwargs):
            if name == path.name:
                self.assertTrue(flags & os.O_NONBLOCK)
            return original(name, flags, *args, **kwargs)

        with patch.object(os, 'open', open_file):
            self.assertEqual(self.act(self.hold)['reason'], 'identity')

    def test_special_original_is_rejected(self):
        self.active()
        path = self.fixture.run / 'pueue.client.json'
        path.unlink()
        path.mkdir(mode=0o600)
        original, ino = os.fstat, path.stat().st_ino

        def fstat(fd):
            info = original(fd)
            if info.st_ino != ino:
                return info
            fields = ('st_mode', 'st_uid', 'st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
            return SimpleNamespace(st_nlink=1, **{field: getattr(info, field) for field in fields})

        with patch.object(os, 'fstat', fstat):
            self.assertEqual(self.act(self.hold)['reason'], 'identity')

    def test_hardlink_original_is_rejected(self):
        self.active()
        path = self.fixture.run / 'pueue.client.json'
        os.link(path, path.with_suffix('.backup'))
        self.assertEqual(self.act(self.hold)['reason'], 'identity')

    def test_wrong_file_owner_is_rejected(self):
        self.active()
        original = os.fstat
        ino = (self.fixture.run / 'pueue.client.json').stat().st_ino

        def fstat(fd):
            info = original(fd)
            if info.st_ino != ino:
                return info
            fields = ('st_mode', 'st_nlink', 'st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
            return SimpleNamespace(st_uid=info.st_uid + 1, **{field: getattr(info, field) for field in fields})

        with patch.object(os, 'fstat', fstat):
            self.assertEqual(self.act(self.hold)['reason'], 'identity')

    def test_open_permissions_are_rejected(self):
        self.active()
        (self.fixture.run / 'pueue.client.json').chmod(0o644)
        self.assertEqual(self.act(self.hold)['reason'], 'identity')

    def test_oversized_original_is_rejected(self):
        self.active()
        data = a.canonical(self.fixture.binding) + ' ' * c.MAX_BYTES
        for name in ('pueue.client.json', 'pueue.client-add.json'):
            (self.fixture.run / name).write_text(data)
        original = os.fdopen

        def fdopen(fd, *args, **kwargs):
            stream = original(fd, *args, **kwargs)
            if os.fstat(fd).st_size > c.MAX_BYTES:
                stream.read = lambda *_: self.fail('oversized original reached read')
            return stream

        with patch.object(os, 'fdopen', fdopen):
            self.assertEqual(self.act(self.hold)['reason'], 'evidence_missing')

    def test_anchor_is_required_and_must_match(self):
        self.active()
        self.fixture.write(self.fixture.run / 'pueue.client-add.json', dict(self.fixture.binding, group='caja'))
        self.assertEqual(self.act(self.hold)['reason'], 'identity')

    def test_duplicate_fields_are_rejected(self):
        self.active()
        for name in ('pueue.client.json', 'pueue.client-add.json'):
            path = self.fixture.run / name
            path.write_text('{"ref":' + a.canonical(REF) + ',' + path.read_text()[1:])
        self.assertEqual(self.act(self.hold)['reason'], 'evidence_missing')

    def test_other_launchers_in_public_and_guard_paths(self):
        self.active()
        reads = self.reads
        for launcher in ('jale', 'apotheca', 'vigilante'):
            with self.subTest(launcher=launcher):
                r = dict(self.cap, launcher=launcher)
                output = io.StringIO()
                self.assertEqual(a.main(self.engine, io.BytesIO(a.canonical(r).encode()), output,
                                        **self.engine_options()), 0)
                self.assertIn('"reason":"unsupported"', output.getvalue())
                turn = self.engine.client_reconcile_turn()
                self.assertIsNotNone(turn)
                reconciler, _, _ = turn
                self.unavailable('unsupported', reconciler.actions.adapter.recover, r, {}, None, None, None)
        self.assertEqual(self.reads, reads)

    def test_gather_does_not_read_daemon_for_unaccredited_action(self):
        request = self.started()
        reconciler = self.recoverer()
        item, = reconciler.copies(None, advance=False)
        self.fixture.write(self.fixture.run / 'pueue.client.json',
                           dict(schema='remote-run-client-binding@1', identity=None, reason='identity'))
        self.p.status = lambda: self.fail('unaccredited action reached daemon')
        self.assertEqual(reconciler.gather([item], None), [dict(kind='retain', reason='unsupported')])

    def test_coordinated_profile_does_not_activate(self):
        self.active()
        self.engine.client_exclusion = 'coordinated'
        actions = a.ClientActions(self.engine, **self.engine_options())
        self.p.status = lambda: self.fail('coordinated reached daemon')
        self.assertEqual(self.act(self.cap, actions)['reason'], 'unsupported')
        self.assertEqual(self.act(self.hold, actions)['reason'], 'unsupported')
        self.assertEqual(self.effects, [])


if __name__ == '__main__':
    unittest.main()
