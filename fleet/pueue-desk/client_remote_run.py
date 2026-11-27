"""CC3-P2: bounded reads of Hasito H1 originals, using the controller's CC0."""
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import hashlib
import os
from pathlib import Path
import re
import stat

# client_actions loads this fixed companion and supplies c and Unavailable.
CONFIG = 'remote-run-adapter.json'
WORKER = 'remote-run-no-payload@1'
GROUPS = ('suites', 'hasito', 'caja', 'otros', 'rapidas')
STABLE = ('generation', 'task_id', 'created_at', 'fingerprint', 'attempt')
LINKS = ('binding', 'attempt_receipt', 'local_attempt')
MODES = ('own', 'own-legacy', 'rack-queued', 'rack-running')
RETIRE = ('retire-held', 'retire-interrupted')
PHASES = ('hold', 'cancel-no-payload', 'signal:TERM', 'signal:KILL', *RETIRE, 'finish')
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
ARTIFACT_LIMIT = 1024 * 1024
STAMP = re.compile(r'([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})'
                   r'(?:\.([0-9]{1,9}))?(Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])')


def match(condition):
    if not condition:
        raise Unavailable('identity')


def stable(identity):
    return {key: identity[key] for key in STABLE}


def timestamp(value):
    parts = STAMP.fullmatch(value) if type(value) is str else None
    match(parts is not None)
    fraction = (parts[2] or '').ljust(6, '0')[:6]
    stamp = datetime.fromisoformat(parts[1] + '.' + fraction + parts[3].replace('Z', '+00:00'))
    return stamp.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def inode(info):
    return info.st_dev, info.st_ino


@contextmanager
def directory(path, *, parent=None, private=False):
    fd = os.open(path, DIR_FLAGS, dir_fd=parent)
    try:
        info = os.fstat(fd)
        match(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
              and (stat.S_IMODE(info.st_mode) == 0o700 if private else not info.st_mode & 0o022))
        yield fd
        try:
            current = os.stat(path, dir_fd=parent, follow_symlinks=False)
        except OSError:
            raise Unavailable('identity') from None
        match(inode(current) == inode(info))
    finally:
        os.close(fd)


def read_file(path, *, parent=None, limit, private=True):
    fd = os.open(path, FILE_FLAGS, dir_fd=parent)
    try:
        info = os.fstat(fd)
        match(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
              and (stat.S_IMODE(info.st_mode) == 0o600 if private else not info.st_mode & 0o022))
        if info.st_size > limit:
            raise Unavailable('evidence_missing')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(limit + 1)
        after = os.fstat(fd)
        try:
            current = os.stat(path, dir_fd=parent, follow_symlinks=False)
        except OSError:
            raise Unavailable('identity') from None
        match(inode(current) == inode(info) and after.st_nlink == 1
              and after.st_mtime_ns == info.st_mtime_ns and after.st_ctime_ns == info.st_ctime_ns)
        if len(raw) > limit:
            raise Unavailable('evidence_missing')
        return raw
    finally:
        os.close(fd)


def installed(engine, record, reservation):
    """The private authority directory supplies one local root and allowlist."""
    try:
        raw = read_file(engine.directory / CONFIG, limit=c.MAX_BYTES)
        settings = c.decode(raw)
        c.keys(settings, 'schema root artifacts')
        c.require(settings['schema'] == 'remote-run-adapter@1' and type(settings['root']) is str,
                  'remote_run_installation')
        root = Path(settings['root'])
        c.require(root.is_absolute() and '..' not in root.parts, 'remote_run_root')
        c.require(type(settings['artifacts']) is list and settings['artifacts'], 'remote_run_artifacts')
        artifacts = set()
        for artifact in settings['artifacts']:
            c.keys(artifact, 'producer worker')
            c.digest(artifact['producer'])
            c.require(artifact['worker'] == WORKER, 'remote_run_worker')
            artifacts.add((artifact['producer'], artifact['worker']))
        return RemoteRun(engine, root, artifacts, record, reservation)
    except (OSError, ValueError, Unavailable):
        raise Unavailable('unsupported') from None


class Originals:
    """One pinned run, one immutable attempt, and the exact files it names."""

    def __init__(self, run, ref):
        self.run, self.ref = run, ref

    def read(self, name):
        return read_file(name, parent=self.run, limit=c.MAX_BYTES)

    def original(self, raw, schema, fields):
        value = c.decode(raw)
        c.keys(value, 'schema launcher ref ' + fields)
        c.require(value['schema'] == 'remote-run-client-' + schema, 'remote_run_schema')
        c.token(value['ref'])
        match(value['launcher'] == 'remote-run' and value['ref'] == self.ref)
        return value

    def binding(self, artifacts, reservation):
        try:
            raw = self.read('pueue.client.json')
        except FileNotFoundError:
            raise Unavailable('unsupported' if reservation is None else 'evidence_missing') from None
        envelope = c.decode(raw)
        c.require(type(envelope) is dict, 'remote_run_binding')
        if envelope.get('schema') == 'remote-run-client-binding@1':
            raise Unavailable('unsupported')
        value = self.original(raw, 'binding@2', 'group identity worker producer')
        match(raw == self.read('pueue.client-add.json'))
        identity = c.identity(value['identity'])
        c.digest(value['producer'])
        if (value['producer'], value['worker']) not in artifacts:
            raise Unavailable('unsupported')
        try:
            source = read_file('pueue.py', parent=self.run, limit=ARTIFACT_LIMIT, private=False)
        except FileNotFoundError:
            raise Unavailable('unsupported') from None
        if hashlib.sha256(source).hexdigest() != value['producer']:
            raise Unavailable('unsupported')
        fingerprint = dict(stable(identity), ref=self.ref, launcher='remote-run')
        del fingerprint['fingerprint']
        match(identity['fingerprint'] == c.rpc_digest(fingerprint))
        c.require(value['group'] in GROUPS, 'remote_run_group')
        return value

    def attempt(self, binding):
        self.current = self.read('pueue.client-current.json')
        value = self.original(self.current, 'current@1', 'revision binding local_attempt attempt_receipt')
        c.token(value['local_attempt'])
        c.digest(value['binding'])
        c.digest(value['attempt_receipt'])
        match(type(value['revision']) is int and value['revision'] == 1
              and value['binding'] == c.rpc_digest(binding))
        self.links = {key: value[key] for key in LINKS}
        local = self.links['local_attempt']
        name = 'pueue.client-attempt-' + c.rpc_digest(local) + '.json'
        origin = self.original(self.read(name), 'attempt@2', 'local_attempt binding identity cause')
        self.identity = stable(binding['identity'])
        match(origin['cause'] == 'add' and origin['binding'] == self.links['binding']
              and origin['local_attempt'] == local and origin['identity'] == self.identity
              and type(origin['identity']['task_id']) is int
              and c.rpc_digest(origin) == self.links['attempt_receipt'])

    def selection(self):
        value = c.decode(self.read('pueue.client-selection.json'))
        c.keys(value, 'schema request phase')
        c.require(value['schema'] == 'remote-run-client-selection@1' and value['phase'] in PHASES,
                  'remote_run_selection')
        c.keys(value['request'], 'ref action_id')
        c.token(value['request']['ref'])
        c.token(value['request']['action_id'])
        match(value['request']['ref'] == self.ref)
        return value

    def action_original(self, action_id, kind, schema, fields):
        c.token(action_id)
        name = 'pueue.client-' + kind + '-' + c.rpc_digest([self.ref, action_id]) + '.json'
        value = self.original(self.read(name), schema, 'action_id binding attempt_receipt local_attempt ' + fields)
        match(value['action_id'] == action_id and all(value[key] == self.links[key] for key in LINKS))
        if 'identity' in value:
            match(value['identity'] == self.identity and type(value['identity']['task_id']) is int)
        return value

    def action(self, request):
        value = self.action_original(request['action_id'], 'mode', 'action@2', 'mode identity')
        match(value['mode'] in MODES and value['mode'] == request['mode'])
        return value

    def acceptance(self, action_id):
        value = self.action_original(action_id, 'acceptance', 'acceptance@1', 'acceptance')['acceptance']
        c.acceptance(value, action_id, self.identity['attempt'])
        c.require(value['receipt'] == self.receipt(value), 'remote_run_acceptance')
        return value

    def worker(self):
        name = 'pueue.client-worker-start-' + c.rpc_digest(self.links['local_attempt']) + '.json'
        value = self.original(self.read(name), 'worker-start@2',
                              'binding attempt_receipt local_attempt identity pid birth worker action_id acceptance_receipt')
        if value['worker'] != WORKER:
            raise Unavailable('unsupported')
        match(all(value[key] == self.links[key] for key in LINKS)
              and value['identity'] == self.identity and type(value['identity']['task_id']) is int)
        c.rpc_pid(value['pid'])
        c.rpc_birth(value['birth'])
        c.require((value['action_id'] is None) == (value['acceptance_receipt'] is None), 'remote_run_worker')
        if value['action_id'] is not None:
            c.token(value['action_id'])
            c.digest(value['acceptance_receipt'])
        return value

    @staticmethod
    def receipt(value):
        return c.rpc_digest({key: item for key, item in value.items() if key != 'receipt'})

    def completion(self, action_id, accepted, kind):
        worker = self.worker()
        winner = (action_id, accepted['receipt'])
        pair = (worker['action_id'], worker['acceptance_receipt'])
        match(pair == winner or kind == 'terminated' and pair == (None, None))
        value = self.action_original(action_id, 'completion', 'completion@1',
            'identity acceptance_receipt worker_start pid birth kind exit no_payload payload_complete receipt')
        match(value['acceptance_receipt'] == accepted['receipt'] and value['worker_start'] == c.rpc_digest(worker)
              and value['pid'] == worker['pid'] and value['birth'] == worker['birth'] and value['kind'] == kind)
        c.require(type(value['pid']) is int and type(value['exit']) is int and value['exit'] == 130
                  and type(value['no_payload']) is bool and type(value['payload_complete']) is bool
                  and (value['no_payload'] if kind == 'no-payload' else value['payload_complete'])
                  and value['receipt'] == self.receipt(value), 'remote_run_completion')
        return dict(kind=value['kind'], attempt=self.identity['attempt'], receipt=value['receipt'],
                    acceptance_receipt=value['acceptance_receipt'], exit=value['exit'])

    def retirement(self, request):
        value = self.action_original(request['action_id'], 'retirement', 'retirement@1',
                                     'mode identity previous_result stop_exit receipt')
        match(value['mode'] == request['mode'] and value['mode'] in ('own', 'own-legacy'))
        # H1 never publishes a previous_result on this path.
        c.require(value['previous_result'] is None and type(value['stop_exit']) is int
                  and 0 <= value['stop_exit'] <= 255 and value['receipt'] == self.receipt(value),
                  'remote_run_retirement')
        return dict(kind='interrupted', attempt=self.identity['attempt'], receipt=value['receipt'],
                    acceptance_receipt=None, exit=value['stop_exit'])


class RemoteRun:
    def __init__(self, engine, root, artifacts, record, reservation):
        self.engine, self.root, self.artifacts = engine, root, artifacts
        self.record, self.reservation = record, reservation

    @contextmanager
    def evidence(self, request, reservation):
        ref = request['ref']
        c.token(ref)
        try:
            with directory(self.root) as root:
                opened = False
                try:
                    with directory(ref, parent=root, private=True) as run:
                        opened = True
                        originals = Originals(run, ref)
                        binding = originals.binding(self.artifacts, reservation)
                        if binding['group'] not in self.engine.profile:
                            raise Unavailable('unsupported')
                        originals.attempt(binding)
                        if reservation is not None:
                            match(binding['group'] == reservation['group']
                                  and originals.identity == stable(reservation['identity']))
                        yield binding['group'], originals
                        match(originals.current == originals.read('pueue.client-current.json'))
                except FileNotFoundError:
                    reason = 'not_found' if not opened and reservation is None else 'evidence_missing'
                    raise Unavailable(reason) from None
        except FileNotFoundError:
            raise Unavailable('unsupported' if reservation is None else 'evidence_missing') from None
        except OSError as error:
            reason = 'identity' if error.errno in (errno.ELOOP, errno.ENOTDIR) else 'evidence_missing'
            raise Unavailable(reason) from None
        except (ValueError, TypeError, KeyError, OverflowError):
            raise Unavailable('evidence_missing') from None

    def generation(self, originals, state):
        raw = state['generation']
        if (originals.identity['generation'] != hashlib.sha256(raw.encode()).hexdigest()
                or state.get('startup', {}).get('phase') not in (None, 'ready')
                or state.get('mini_handoff', {}).get('phase') not in (None, 'done', 'withdrawn', 'aborted')):
            raise Unavailable('startup')

    def selected(self, originals, request):
        selection = originals.selection()
        match(selection['request']['action_id'] == request['action_id'])
        return selection['phase']

    def selected_reservation(self, originals):
        try:
            selection = originals.selection()
        except FileNotFoundError:
            raise Unavailable('identity') from None
        selected = dict(selection['request'], launcher='remote-run', phase=selection['phase'])
        return selected, self.reservation(selected)

    def phase_record(self, originals, request, reservation):
        if reservation['uncertain']:
            raise Unavailable('rpc_unresolved')
        recorded = self.record(dict(request, phase=reservation['phase']))
        if recorded is None:
            raise Unavailable('evidence_missing')
        attribution = recorded.get('attribution')
        c.keys(attribution, 'dispatch seal_receipt exclusion')
        c.digest(attribution['seal_receipt'])
        c.require(attribution['exclusion'] == 'filtered'
                  and attribution['dispatch'] == recorded['process']['dispatch'], 'remote_run_attribution')
        match(recorded['request']['mode'] == request['mode']
              and stable(recorded['request']['expected']) == originals.identity)
        return recorded

    def refreshed_start(self, request, originals, current, reservation, posterior):
        if posterior and request['op'] == 'cancel-no-payload':
            match(reservation is not None and reservation.get('hold_identity') == request['expected'])
            accepted = originals.acceptance(request['action_id'])
            match(accepted == request['acceptance'] == reservation['acceptance'])
            c.start_after_acceptance(current, accepted)
            return
        if request['op'] == 'capabilities':
            selected, reservation = self.selected_reservation(originals)
            if reservation is not None:
                request = dict(selected, launcher='remote-run', mode=reservation['mode'])
        match(reservation is not None and reservation['phase'] == 'cancel-no-payload'
              and stable(reservation['identity']) == originals.identity)
        recorded = self.phase_record(originals, request, reservation)
        match(recorded['response']['status'] == 'started'
              and all(recorded['response']['identity'][key] == current[key]
                      for key in ('enqueued_at', 'started_at')))

    def project(self, request, originals, group, daemon, reservation, posterior):
        identity = originals.identity
        tasks, key = daemon['tasks'], str(identity['task_id'])
        absent = dict(identity, state='Absent', enqueued_at=None, enqueue_at=None, started_at=None)
        if key not in tasks:
            return absent
        task = tasks[key]
        c.integer(task['id'])
        match(task['id'] == identity['task_id'])
        created = timestamp(task['created_at'])
        if created != identity['created_at']:
            # Only the stock verdict following Success can use a reused ID as Q.
            if posterior and request['op'] in RETIRE:
                return absent
            if request['op'] == 'capabilities':
                _, reservation = self.selected_reservation(originals)
            if (reservation is not None and reservation['identity'] == absent
                    and not reservation['uncertain']):
                return absent  # The journal already attributed the removal.
            raise Unavailable('identity')
        match(task['group'] == group)
        status = task['status']
        match(type(status) is dict and len(status) == 1)
        name, data = next(iter(status.items()))
        enqueued = scheduled = started = None
        if name == 'Stashed':
            scheduled = timestamp(data['enqueue_at']) if data['enqueue_at'] is not None else None
        if name in ('Queued', 'Running', 'Paused', 'Done'):
            enqueued = timestamp(data['enqueued_at'])
        if name in ('Running', 'Paused', 'Done'):
            started = timestamp(data['start'])
        current = dict(identity, state=name, enqueued_at=enqueued, enqueue_at=scheduled, started_at=started)
        if enqueued is not None and enqueued != identity['attempt']:
            match(name in ('Running', 'Done'))
            self.refreshed_start(request, originals, current, reservation, posterior)
        return c.identity(current)

    def observe(self, request, state, reservation, *, posterior=False):
        with self.evidence(request, reservation) as (group, originals):
            self.generation(originals, state)
            daemon = self.engine.pueue.status()
            try:
                generation = self.engine.generation(state)
            except (OSError, ValueError):
                raise Unavailable('startup') from None
            if generation != state['generation']:
                raise Unavailable('startup')
            current = self.project(request, originals, group, daemon, reservation, posterior)
            return dict(group=group, identity=current)

    def finish(self, request, originals, current, reservation):
        if reservation is None:
            raise Unavailable('evidence_missing')
        recorded = self.phase_record(originals, request, reservation)
        op, after = recorded['request']['op'], recorded['response']
        if after['status'] == 'retired' and op in RETIRE and after['identity'] == current:
            accepted, proof = None, originals.retirement(request)
        else:
            kind = {'started': ('cancel-no-payload', 'no-payload'),
                    'signalled': ('signal', 'terminated')}.get(after['status'])
            if kind is None or op != kind[0] or current['state'] != 'Done':
                raise Unavailable('evidence_missing')
            match(all(after['identity'][key] == current[key] for key in ('enqueued_at', 'started_at')))
            accepted = originals.acceptance(request['action_id'])
            match(accepted == recorded['request']['acceptance'] == reservation['acceptance'])
            proof = originals.completion(request['action_id'], accepted, kind[1])
            if kind[1] == 'no-payload':
                c.start_after_acceptance(current, accepted)
        return c.validate_request(dict(schema=c.SCHEMA, launcher='remote-run', op='finish', phase='finish',
            ref=request['ref'], action_id=request['action_id'], mode=request['mode'], expected=current,
            acceptance=accepted, proof=proof))

    def verify(self, request, observation, reservation):
        with self.evidence(request, reservation) as (group, originals):
            current = observation['identity']
            match(group == observation['group'] and stable(current) == originals.identity
                  and current == request['expected'])
            selected = self.selected(originals, request)
            originals.action(request)
            op = request['op']
            if op == 'finish':
                if reservation is None:
                    raise Unavailable('evidence_missing')
                match(selected in (reservation['phase'], 'finish'))
                match(self.finish(request, originals, current, reservation) == request)
            else:
                match(selected == request['phase'])
                if op in RETIRE:
                    originals.retirement(request)
                if op in ('signal', 'cancel-no-payload'):
                    match(originals.acceptance(request['action_id']) == request['acceptance'])
                if op == 'signal':
                    worker = originals.worker()
                    match((worker['action_id'], worker['acceptance_receipt']) in
                          ((None, None), (request['action_id'], request['acceptance']['receipt'])))

    def recover(self, request, state, reservation, process, daemon):
        with self.evidence(request, reservation) as (group, originals):
            self.generation(originals, state)
            if callable(daemon):
                daemon = daemon()
            if daemon is None:
                raise Unavailable('startup')
            current = self.project(request, originals, group, daemon, reservation, False)
            finish = None
            if request['op'] in ('cancel-no-payload', 'signal', *RETIRE) and current['state'] in ('Done', 'Absent'):
                selected = self.selected(originals, request)
                match(selected in (request['phase'], 'finish'))
                originals.action(request)
                finish = self.finish(request, originals, current, reservation)
            return dict(observation=dict(group=group, identity=current), finish=finish)
