"""CC1. Durable client phases; only this coordinator sends client mutations.

The installation supplies engine, adapter and binary. No client field selects
any of them. Adapters are bounded local evidence reads, never launcher calls
or result locks. CC2 collects retained actions outside the host lock.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import types
import time
import select

# Loading through compile also works with the controller's isolated interpreter
# and cannot create bytecode during a read-only capabilities request.
c = types.ModuleType('client_contract')
_contract = Path(__file__).with_name('client_contract.py')
exec(compile(_contract.read_bytes(), str(_contract), 'exec'), c.__dict__)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Unavailable(Exception):
    def __init__(self, reason='unsupported'):
        if reason not in c.REASONS:
            reason = 'evidence_missing'
        self.reason = reason


class InstalledAdapters:
    """Fail closed for formats whose ownership requires command inspection.

    remote-run's owns_task, Jale's observe, apotheca's task_record and the
    vigilante's manual classifier require command fields. Neither a group,
    cwd, label nor an unverified manifest alone replaces that check. These
    installed formats therefore cannot authenticate CC0 evidence without
    forbidden reads. Future launcher deliveries must supply an accredited
    fixed adapter; the wire cannot install one or assert its own receipts.
    """
    def observe(self, request, state, reservation):
        raise Unavailable('unsupported')

    def verify(self, request, observation, reservation):
        raise Unavailable('unsupported')

    def recover(self, request, state, reservation, process, daemon):
        raise Unavailable('unsupported')


def reservations(engine, open_only=False):
    """Read-only seam for host writers; caller holds the real control.lock.

    Missing table is the additive pre-CC1 format, not a missing authority.
    Corrupt/unreadable tables propagate: host operations must fail closed.
    No clock, expiry, launcher callback or result.lock participates here.
    """
    with engine.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='client_reservations'").fetchone():
            return []
        indexed = db.execute("SELECT 1 FROM sqlite_master WHERE name='client_open'").fetchone()
        sql = ('SELECT r.body FROM client_reservations r JOIN client_open o USING(launcher,ref,action_id)'
               if open_only and indexed else 'SELECT body FROM client_reservations')
        values = [json.loads(row[0]) for row in db.execute(sql)]
    for value in values:
        validate_reservation(value)
    return values


def validate_reservation(value):
    if (type(value) is not dict or type(value.get('version')) is not int or value['version'] != 1 or
            type(value.get('open')) is not bool or type(value.get('uncertain')) is not bool or
            type(value.get('group')) is not str or not value['group']):
        raise ValueError('client_journal')
    c.identity(value['identity'])
    if type(value.get('revision')) is not int or value['revision'] < 1:
        raise ValueError('client_journal')
    return value


def index_pending(engine):
    with engine.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='client_reservations'").fetchone():
            return False
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='client_scan'").fetchone():
            return True
        return db.execute('SELECT done FROM client_scan WHERE id=1').fetchone() != (1,)


def blocked(engine, group, opening=False):
    if index_pending(engine):
        return True  # A partial migration cannot certify any group free.
    for item in reservations(engine, open_only=True):
        if item['group'] == group and item['open']:
            # A known cancellation hold must not prevent its own safe admission.
            if not opening or item['uncertain']:
                return True
    return False


def protected_ids(engine):
    if index_pending(engine):
        raise ValueError('client_index_pending')
    return {str(r['identity']['task_id']) for r in reservations(engine, open_only=True) if r['open']}


class ClientActions:
    """Public entry acquires control.lock; private methods require that lock.

    An adapter returns {group, identity}. verify authenticates the acceptance
    and proof against fixed durable launcher evidence, including worker/no-load
    compatibility and absence of a previously published normal result for a
    retirement. It raises Unavailable on any missing/inconclusive evidence.
    It must never wait for a launcher lock, run code, or call back to client.
    """
    boundary = staticmethod(lambda phase: None)

    def __init__(self, engine, adapter=None, rpc=None, birth=None):
        self.engine = engine
        self.adapter = adapter if adapter is not None else InstalledAdapters()
        self.rpc = rpc
        self.birth = birth

    def dispatch(self, raw):
        try:
            request = c.parse(raw)
        except c.Invalid:
            return c.invalid_request_response(), 2
        try:
            response = self.handle(request)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError):
            # No daemon text, paths, environment or exception strings escape.
            # If a durable intent exists, expose it; never downgrade to deferred.
            response = self.failure(request)
        return c.validate_response(response, request), 0

    def reply(self, r, status, reason=None, identity=None, held=False):
        if identity is None and r['acceptance'] is not None:
            identity = r['expected']
        value = {k: r[k] for k in ('schema', 'op', 'launcher', 'ref', 'action_id', 'phase', 'mode')}
        value.update(status=status, effects=None if status == 'unknown' else status in
                     ('held', 'started', 'signalled', 'retired'),
                     reservation='closed' if status == 'finished' else 'held' if held else 'none',
                     identity=identity, acceptance=r['acceptance'],
                     proof=r['proof'] if status == 'finished' else None, reason=reason)
        return c.validate_response(value, r)

    def failure(self, request):
        if request['op'] == 'capabilities':
            return self.capability(request, identity_allowed=False)
        r = c.action_projection(request) if request['op'] == 'status' else request
        try:
            record = self.record(r)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            record = None
        if record and record['response']['reservation'] == 'held':
            result = self.reply(r, 'unknown', 'rpc_unresolved', held=True)
        else:
            result = self.reply(r, 'rejected', 'evidence_missing')
        return dict(result, op='status', action_op=r['op']) if request['op'] == 'status' else result

    def handle(self, request):
        c.validate_request(request)
        # The inode is installed by adoption. Never create/replace the lock here.
        with (self.engine.directory / 'control.lock').open('rb') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if request['op'] == 'capabilities':
                    return self.capability(request, identity_allowed=False)
                r = c.action_projection(request) if request['op'] == 'status' else request
                prior = self.record(r)
                if prior:
                    result = (prior['response'] if prior['digest'] == digest(r) else
                              self.reply(r, 'rejected', 'idempotency_conflict', held=prior['response']['reservation'] == 'held'))
                else:
                    reservation = self.reservation(r)
                    result = self.reply(r, 'deferred', 'lock_busy', held=r['op'] in ('cancel-no-payload', 'finish') or
                                        bool(reservation and reservation['open']))
                return dict(result, op='status', action_op=r['op']) if request['op'] == 'status' else result
            return self._handle(request)

    def authority(self):
        with self.engine.connect() as db:
            state = self.engine.state(db)
        if self.engine.disabled() or self.engine.generation(state) != state['generation']:
            raise Unavailable('startup')
        return state

    def capability(self, request, identity_allowed=True):
        result = dict(schema=c.SCHEMA, op='capabilities', version=1, ops=list(c.OPS))
        if 'launcher' in request:
            identity, reason = None, 'unsupported'
            try:
                if not identity_allowed:
                    raise Unavailable('unsupported')
                observed = self.observe(request, self.authority(), None)
                identity, reason = observed['identity'], None
            except (Unavailable, OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError):
                pass
            result.update(launcher=request['launcher'], ref=request['ref'], identity=identity, reason=reason)
        return c.validate_response(result, request)

    @staticmethod
    def key(r):
        return tuple(r[k] for k in ('launcher', 'ref', 'action_id', 'phase'))

    def record(self, r):
        with self.engine.connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='client_actions'").fetchone():
                return None
            row = db.execute('SELECT body FROM client_actions WHERE launcher=? AND ref=? AND action_id=? AND phase=?', self.key(r)).fetchone()
        if not row:
            return None
        record = json.loads(row[0])
        if (type(record) is not dict or type(record.get('version')) is not int or
                record['version'] != 1 or type(record.get('revision')) is not int):
            raise ValueError('client_journal')
        c.validate_request(record['request'])
        c.validate_response(record['response'], record['request'])
        if record['digest'] != digest(record['request']):
            raise ValueError('client_journal')
        return record

    def reservation(self, r):
        with self.engine.connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='client_reservations'").fetchone():
                return None
            row = db.execute('SELECT body FROM client_reservations WHERE launcher=? AND ref=? AND action_id=?', self.key(r)[:3]).fetchone()
        return validate_reservation(json.loads(row[0])) if row else None

    @staticmethod
    def index(db):
        db.execute('CREATE TABLE IF NOT EXISTS client_open (launcher TEXT, ref TEXT, action_id TEXT, checked INTEGER NOT NULL, PRIMARY KEY(launcher,ref,action_id))')
        db.execute('CREATE INDEX IF NOT EXISTS client_open_checked ON client_open(checked)')
        db.execute('CREATE TABLE IF NOT EXISTS client_scan (id INTEGER PRIMARY KEY CHECK(id=1), cursor INTEGER NOT NULL, done INTEGER NOT NULL)')
        db.execute('INSERT OR IGNORE INTO client_scan VALUES (1,0,0)')
        cursor, done = db.execute('SELECT cursor,done FROM client_scan WHERE id=1').fetchone()
        if not done:
            # Migrate a bounded slice; no historical scan on a guard tick.
            rows = list(db.execute('SELECT rowid,body FROM client_reservations WHERE rowid>? ORDER BY rowid LIMIT 8', (cursor,)))
            for rowid, body in rows:
                value = validate_reservation(json.loads(body))
                if value['open']:
                    db.execute('INSERT OR IGNORE INTO client_open VALUES (?,?,?,0)', tuple(value[k] for k in ('launcher','ref','action_id')))
                cursor = rowid
            db.execute('UPDATE client_scan SET cursor=?,done=? WHERE id=1', (cursor, int(len(rows) < 8)))


    def save(self, record, reservation=None):
        # SQLite FULL + fullfsync commits intent/reservation atomically before
        # the gate is released. No transaction spans any Pueue RPC.
        record['revision'] = record.get('revision', 0) + 1
        record['version'] = 1
        if reservation is not None:
            reservation['version'] = 1
            reservation['revision'] = reservation.get('revision', 0) + 1
            reservation['phase'] = record['request']['phase']
            reservation['digest'] = record['digest']
        with self.engine.connect(write=True) as db:
            db.execute('CREATE TABLE IF NOT EXISTS client_actions (launcher TEXT, ref TEXT, action_id TEXT, phase TEXT, body TEXT NOT NULL, PRIMARY KEY(launcher,ref,action_id,phase))')
            db.execute('CREATE TABLE IF NOT EXISTS client_reservations (launcher TEXT, ref TEXT, action_id TEXT, body TEXT NOT NULL, PRIMARY KEY(launcher,ref,action_id))')
            self.index(db)
            db.execute('INSERT OR REPLACE INTO client_actions VALUES (?,?,?,?,?)', self.key(record['request']) + (canonical(record),))
            if reservation is not None:
                db.execute('INSERT OR REPLACE INTO client_reservations VALUES (?,?,?,?)', self.key(record['request'])[:3] + (canonical(reservation),))
                key = self.key(record['request'])[:3]
                if reservation['open']:
                    db.execute('INSERT OR IGNORE INTO client_open VALUES (?,?,?,0)', key)
                else:
                    db.execute('DELETE FROM client_open WHERE launcher=? AND ref=? AND action_id=?', key)
        self.boundary('saved')

    def observe(self, r, state, reservation):
        observation = self.adapter.observe(r, state, reservation)
        c.keys(observation, 'group identity')
        c.identity(observation['identity'])
        if observation['group'] not in self.engine.profile:
            raise Unavailable('unsupported')
        return observation

    def host_busy(self, group):
        with self.engine.connect() as db:
            for row in db.execute('SELECT body FROM host_actions'):
                action = json.loads(row[0])
                if (action.get('kind') == 'reorder' and action['group'] == group and action['phase'] != 'terminal'
                        or action.get('kind') != 'reorder' and action['response']['status'] in ('applying', 'partial', 'unknown')):
                    return True
        return False

    @staticmethod
    def same_attempt(before, after):
        return all(before[k] == after[k] for k in ('generation', 'task_id', 'created_at', 'fingerprint', 'attempt'))

    def _handle(self, request):
        if request['op'] == 'capabilities':
            return self.capability(request)
        query = request['op'] == 'status'
        r = c.action_projection(request) if query else request
        record = self.record(r)
        if record:
            if record['digest'] != digest(r):
                result = self.reply(r, 'rejected', 'idempotency_conflict', held=record['response']['reservation'] == 'held')
            elif not query and record['response']['status'] in c.REEVALUATED:
                result = self.execute(r, prior=record)
            else:
                result = record['response']
        elif query:
            result = self.reply(r, 'rejected', 'not_found')
        else:
            result = self.execute(r)
        return c.validate_response(dict(result, op='status', action_op=r['op']) if query else result, request)

    def execute(self, r, prior=None):
        previous = self.reservation(r)
        held = bool(previous and previous['open'])
        record = dict(request=r, digest=digest(r), revision=prior['revision'] if prior else 0, response=None, process=None)
        def reject(reason, status='rejected', identity=None):
            record['response'] = self.reply(r, status, reason, identity, held)
            self.save(record)
            return record['response']
        try:
            state = self.authority()
            observed = self.observe(r, state, previous)
            group, current = observed['group'], observed['identity']
            if current['state'] == 'Locked':
                return reject('locked')
            if not self.same_attempt(r['expected'], current):
                return reject('identity')
            if current != r['expected']:
                return reject('stale_state')
            if previous:
                if (previous['group'] != group or previous['mode'] != r['mode'] or
                        not self.same_attempt(previous['identity'], current)):
                    return reject('identity')
                if not previous['open']:
                    return reject('stale_state')
                if r['op'] in ('cancel-no-payload', 'retire-held') and previous.get('hold_identity') != current:
                    return reject('evidence_missing')
                if previous['uncertain']:
                    return reject('rpc_unresolved', 'deferred')
                if previous['acceptance'] is not None and previous['acceptance'] != r['acceptance']:
                    return reject('identity')
            if r['op'] in ('cancel-no-payload', 'retire-held', 'finish') and not held:
                return reject('evidence_missing')
            if self.host_busy(group):
                return reject('host_action', 'deferred')
            if any(v['group'] == group and v['open'] and
                   tuple(v[k] for k in ('launcher', 'ref', 'action_id')) != self.key(r)[:3]
                   for v in reservations(self.engine)):
                return reject('host_action', 'deferred')
            # An adapter verifies local durable acceptance, worker compatibility,
            # proof and interrupted-result policy; request digests alone do not.
            self.adapter.verify(r, observed, previous)
            if r['op'] == 'cancel-no-payload':
                reasons = set(state['reasons'].get(group, []))
                fence = self.engine.fence_reason(group)
                if fence:
                    reasons.add(fence)
                if state.get('startup', {}).get('phase') not in (None, 'ready'):
                    reasons.add('startup')
                if reasons:
                    return reject(next((v for v in ('manual', 'pressure', 'startup') if v in reasons), 'host_action'), 'deferred')
                if self.engine.pueue.status()['groups'][group]['status'] != 'Running':
                    return reject('manual', 'deferred')
            if r['op'] == 'finish':
                previous.update(open=False, uncertain=False, identity=current)
                record['response'] = self.reply(r, 'finished', identity=current)
                self.save(record, previous)
                return record['response']
            reservation = dict(launcher=r['launcher'], ref=r['ref'], action_id=r['action_id'],
                               group=group, mode=r['mode'], identity=current, acceptance=r['acceptance'],
                               hold_identity=previous.get('hold_identity') if previous else None,
                               open=True, uncertain=True, revision=previous.get('revision', 0) if previous else 0)
            # Unknown is durable before spawn, before gate release and every RPC.
            record['response'] = self.reply(r, 'unknown', 'rpc_unresolved', identity=current, held=True)
            self.save(record, reservation)
            self.boundary('reserved')
            # Re-read after durable reservation. Locked/Running is never adopted.
            latest = self.observe(r, self.authority(), reservation)
            if latest != observed:
                record['response'] = self.reply(r, 'rejected', 'locked' if latest['identity']['state'] == 'Locked' else 'stale_state', held=True)
                reservation['uncertain'] = False  # No child has been launched.
                self.save(record, reservation)
                return record['response']
            self.adapter.verify(r, latest, reservation)
            args = {'hold': ('stash', current['task_id']),
                    'cancel-no-payload': ('start', current['task_id']),
                    'signal': ('kill', '-s', 'SIG' + r.get('signal', 'TERM'), current['task_id']),
                    'signal-manual': ('kill', '-s', 'SIG' + r.get('signal', 'TERM'), current['task_id']),
                    'retire-held': ('remove', current['task_id']),
                    'retire-interrupted': ('remove', current['task_id'])}[r['op']]
            self.boundary('before_rpc')
            if self.rpc is not None:
                record['process'] = dict(state='injected')  # Test seam cannot certify non-delivery.
                self.save(record)
                self.rpc(*args)
            else:
                self.send(record, *args)
            self.boundary('rpc_returned')
            after = self.observe(r, self.authority(), reservation)['identity']
            if r['op'] == 'hold' and self.same_attempt(current, after) and after['state'] in ('Running', 'Paused'):
                record['response'] = self.reply(r, 'rejected', 'stale_state', held=True)
            else:
                c.transition(current, after, r['op'])
                status = {'hold': 'held', 'cancel-no-payload': 'started', 'signal': 'signalled',
                          'signal-manual': 'signalled', 'retire-held': 'retired', 'retire-interrupted': 'retired'}[r['op']]
                record['response'] = self.reply(r, status, identity=after, held=True)
                if status == 'held':
                    reservation['hold_identity'] = after
            reservation.update(identity=after, uncertain=False)
            self.save(record, reservation)
            return record['response']
        except Unavailable as error:
            if record['response'] is None:
                return reject(error.reason)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError):
            if record['response'] is None:
                return reject('evidence_missing')
        # Any failure after the durable intent retains exactly that reservation.
        # Even two identical observations cannot rule out a late daemon effect.
        saved = self.record(r)
        return saved['response'] if saved else self.reply(r, 'unknown', 'rpc_unresolved', held=True)

    def send(self, record, *args):
        """Identified private child, gated until its identity is fsynced.

        A timeout kills/reaps our own child but does NOT prove the daemon has no
        pending effect. The action stays unknown; CC2 must exclude late messages.
        stdout/stderr, argv and env never enter the action journal.
        """
        pueue = self.engine.pueue
        if self.birth is None or not Path(pueue.binary).is_absolute():
            raise Unavailable('unsupported')
        child = subprocess.Popen([sys.executable, '-I', '-S', str(Path(__file__).resolve()),
                                  '--rpc-gate', pueue.binary, *map(str, args)],
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, close_fds=True, **pueue.context)
        try:
            born = self.birth(child.pid)
            if born is None:
                raise Unavailable('rpc_unresolved')
            record['process'] = dict(pid=child.pid, birth=born, state='gated')
            self.save(record)
            child.stdin.write(b'1')
            child.stdin.close()
            record['process']['state'] = 'sent'
            self.save(record)
            code = child.wait(timeout=1.0)
            record['process']['state'] = 'reaped'
            self.save(record)
            if code != 0:
                raise Unavailable('rpc_unresolved')
        finally:
            if child.stdin and not child.stdin.closed:
                child.stdin.close()
            if child.poll() is None:
                child.kill()
            child.wait()


class Reconciler:
    """CC2: bounded copy / collect / compare. Never sends a Pueue mutation.

    recover is a fixed adapter's evidence-only interface. It returns observation,
    settled (a positive exclusion of late RPCs, NOT child death), optional effect
    and optional finish (a full CC0 request authenticated by verify). effect is the
    launcher's durable record that THIS phase ran in THIS child: exactly
    EFFECT_FIELDS, compared under control.lock with the copied key and child
    pid/birth. It is never derived from the daemon, the child or the request.
    The collector runs in a disposable process, outside control.lock; no result
    lock can stall guard. Callers run it before their own critical section.
    """
    limit = 8
    seconds = .4
    boundary = staticmethod(lambda phase: None)
    EFFECT_FIELDS = ('launcher', 'ref', 'action_id', 'phase', 'pid', 'birth')

    def __init__(self, engine, adapter=None, birth=None):
        self.engine = engine
        self.actions = ClientActions(engine, adapter=adapter)
        self.birth = birth

    def lock(self):
        # Never waits: a busy lock skips this pass; the index revisits it later.
        handle = (self.engine.directory / 'control.lock').open('rb')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            handle.close()
            raise
        return handle

    def current_generation(self, state, offline):
        try:
            return self.engine.generation(state)
        except (OSError, ValueError, subprocess.SubprocessError):
            if not offline:
                raise
            return None  # Absence is compared too; a restart invalidates the copy.

    def copies(self, offline):
        with self.lock(), self.engine.connect(write=True) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='client_reservations'").fetchone():
                return []
            self.actions.index(db)
            state = self.engine.state(db)
            generation = self.current_generation(state, offline)
            keys = list(db.execute('SELECT launcher,ref,action_id FROM client_open ORDER BY checked LIMIT ?', (self.limit,)))
            sequence = db.execute('SELECT COALESCE(MAX(checked),0)+1 FROM client_open').fetchone()[0]
            result = []
            for key in keys:
                row = db.execute('SELECT body FROM client_reservations WHERE launcher=? AND ref=? AND action_id=?', key).fetchone()
                reservation = validate_reservation(json.loads(row[0]))
                r = dict(zip(('launcher','ref','action_id'), key), phase=reservation['phase'])
                record = self.actions.record(r)
                if not record or not reservation['open']:
                    raise ValueError('client_journal')
                if record['digest'] != reservation['digest'] or self.actions.key(record['request']) != self.actions.key(r):
                    raise ValueError('client_journal')
                phases = [json.loads(row[0]) for row in db.execute(
                    'SELECT body FROM client_actions WHERE launcher=? AND ref=? AND action_id=?', key)]
                rejected_unsent = (record['request']['op'] == 'hold' and record['process'] is None
                    and record['response']['status'] == 'rejected' and record['response']['reason'] in ('locked', 'stale_state')
                    and reservation['uncertain'] is False and reservation.get('hold_identity') is None
                    and reservation['acceptance'] is None and all(
                        phase['process'] is None and phase['response']['effects'] is False
                        and phase['response']['status'] in ('rejected',) + c.REEVALUATED for phase in phases))
                result.append(dict(record=record, reservation=reservation, state=state, generation=generation,
                                   rejected_unsent=rejected_unsent))
                db.execute('UPDATE client_open SET checked=? WHERE launcher=? AND ref=? AND action_id=?', (sequence,) + key)
            return result

    def collect(self, item, daemon):
        record, reservation = item['record'], item['reservation']
        r, process = record['request'], record.get('process')
        if item['rejected_unsent']:
            return dict(kind='rejected_not_sent')
        if record['response']['status'] == 'unknown' and process is None:
            # CC1 fsyncs a child's identity before opening its stdin gate. A
            # child spawned before that commit sees EOF after the owner dies.
            return dict(kind='not_sent')
        if item['generation'] is not None and item['generation'] != item['state']['generation']:
            raise Unavailable('startup')
        if process and process.get('pid') is not None:
            if self.birth is None or self.birth(process['pid']) == process['birth']:
                raise Unavailable('rpc_unresolved')
        evidence = self.actions.adapter.recover(r, item['state'], reservation, process, daemon)
        if daemon is None:
            raise Unavailable('startup')
        c.keys(evidence, 'observation settled finish' + (' effect' if 'effect' in evidence else ''))
        observed = evidence['observation']
        c.keys(observed, 'group identity')
        c.identity(observed['identity'])
        if (observed['group'] != reservation['group'] or
                not self.actions.same_attempt(reservation['identity'], observed['identity'])):
            raise Unavailable('identity')
        if evidence['settled'] is not True:
            raise Unavailable('rpc_unresolved')
        finish = evidence['finish']
        if finish is not None:
            c.validate_request(finish)
            if (finish['op'] != 'finish' or self.actions.key(finish)[:3] != self.actions.key(r)[:3]
                    or finish['mode'] != reservation['mode'] or finish['expected'] != observed['identity']
                    or reservation['acceptance'] is not None and finish['acceptance'] != reservation['acceptance']):
                raise Unavailable('identity')
            self.actions.adapter.verify(finish, observed, reservation)
        # Excluding a late message does not attribute an external Stashed to our
        # hold: apply compares this launcher proof with the copy under the lock.
        return dict(kind='evidence', observation=observed, finish=finish, effect=evidence.get('effect'))

    def gather(self, copies, offline):
        proposals = []
        try:
            self.engine.pueue.timeout = self.seconds / 2
            self.engine.pueue.deadline = time.monotonic() + self.seconds / 2
            daemon = (None if type(self.actions.adapter) is InstalledAdapters else
                      json.loads(Path(offline).read_text()) if offline else self.engine.pueue.status())
        except (OSError, ValueError, subprocess.SubprocessError):
            daemon = None
        for item in copies:
            try:
                proposal = self.collect(item, daemon)
            except Unavailable as error:
                proposal = dict(kind='retain', reason=error.reason)
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError, AttributeError):
                proposal = dict(kind='retain', reason='evidence_missing')
            proposals.append(proposal)
        return proposals

    def bounded_gather(self, copies, offline):
        # One child and one deadline for the batch, never one timeout per action.
        # No DB connection or host lock is inherited across this fork.
        read_fd, write_fd = os.pipe()
        try:
            pid = os.fork()
        except OSError:
            os.close(read_fd); os.close(write_fd)
            raise
        if pid == 0:
            os.close(read_fd)
            try:
                os.setsid()  # Only our collector and its read-only daemon CLI.
                body = canonical(self.gather(copies, offline)).encode()
                # Bound output independently of an adapter's implementation.
                if len(body) <= c.MAX_BYTES * self.limit:
                    view = memoryview(body)
                    while view:
                        view = view[os.write(write_fd, view):]
            finally:
                os.close(write_fd)
                os._exit(0)
        os.close(write_fd)
        deadline, raw = time.monotonic() + self.seconds, b''
        try:
            while len(raw) <= c.MAX_BYTES * self.limit:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([read_fd], [], [], remaining)[0]:
                    return [dict(kind='retain', reason='evidence_missing') for _ in copies]
                chunk = os.read(read_fd, 65536)
                if not chunk:
                    proposals = json.loads(raw)
                    if type(proposals) is list and len(proposals) == len(copies):
                        return proposals
                    break
                raw += chunk
            return [dict(kind='retain', reason='evidence_missing') for _ in copies]
        finally:
            os.close(read_fd)
            # This is our unreaped child, so its PID cannot have been reused.
            # Clean its private group too if status was interrupted during spawn.
            try:
                if os.getpgid(pid) == pid:
                    os.killpg(pid, 9)
                else:
                    os.kill(pid, 9)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)

    def observe(self, offline):
        with self.engine.connect() as db:
            state = self.engine.state(db)
        return state, self.current_generation(state, offline)

    @classmethod
    def bound_effect(cls, record):
        """The only effect proof accepted: the copied action phase and child."""
        process = record.get('process') or {}
        if type(process.get('pid')) is not int or type(process.get('birth')) is not str:
            return None  # no identified child, nothing a launcher record can bind to
        return dict(zip(cls.EFFECT_FIELDS, ClientActions.key(record['request']) + (process['pid'], process['birth'])))

    def apply(self, item, proposal, offline, snapshot=None):
        """One batch: the caller holds self.lock() and passes one observation."""
        if snapshot is None:
            with self.lock():
                return self.apply(item, proposal, offline, self.observe(offline))
        old, before = item['record'], item['reservation']
        r = old['request']
        current = self.actions.record(r)
        reservation = self.actions.reservation(r)
        state, generation = snapshot
        if (state['generation'] != item['state']['generation'] or generation != item['generation']
                or current['revision'] != old['revision']
                or current['digest'] != old['digest']
                or reservation['revision'] != before['revision']
                or reservation['digest'] != before['digest'] or reservation['phase'] != before['phase']
                or not reservation['open']):
            return False  # Discard; the rotating index rereads on a later tick.
        if current['response']['status'] == 'finished':
            return False
        reason = proposal.get('reason', 'evidence_missing')
        if reason not in c.REASONS:
            reason = 'evidence_missing'
        if proposal['kind'] == 'rejected_not_sent':
            # Only the reservation changes: the rejected phase's receipt,
            # digest and revision remain the idempotent historical result.
            reservation.update(open=False, uncertain=False, revision=reservation['revision'] + 1)
            with self.engine.connect(write=True) as db:
                key = self.actions.key(r)[:3]
                db.execute('UPDATE client_reservations SET body=? WHERE launcher=? AND ref=? AND action_id=?',
                           (canonical(reservation),) + key)
                db.execute('DELETE FROM client_open WHERE launcher=? AND ref=? AND action_id=?', key)
            return True
        if proposal['kind'] == 'not_sent':
            close = r['op'] == 'hold' and reservation.get('hold_identity') is None and reservation['acceptance'] is None
            reservation.update(open=not close, uncertain=False)
            current['response'] = self.actions.reply(r, 'rejected' if close else 'deferred',
                                                     'stale_state' if close else 'evidence_missing', held=not close)
            reason = 'evidence_missing'
        elif (proposal['kind'] == 'evidence' and current['response']['status'] == 'unknown'
              and (self.bound_effect(old) is None or proposal.get('effect') != self.bound_effect(old))):
            reason = 'evidence_missing'  # settled alone never attributes the observed effect
        elif proposal['kind'] == 'evidence':
            observed, finish = proposal['observation'], proposal['finish']
            after = observed['identity']
            if current['response']['status'] == 'unknown':
                c.transition(r['expected'], after, r['op'])
                status = dict(hold='held', **{'cancel-no-payload':'started', 'signal':'signalled',
                              'signal-manual':'signalled', 'retire-held':'retired', 'retire-interrupted':'retired'})[r['op']]
                current['response'] = self.actions.reply(r, status, identity=after, held=True)
                if status == 'held':
                    reservation['hold_identity'] = after
                reservation.update(uncertain=False, identity=after)
            if finish is not None:
                # A terminal is appended, never substituted for an effect receipt.
                # It is validated with CC0 again in the authority process.
                c.validate_request(finish)
                prior = self.actions.record(finish)
                if prior and prior['digest'] != digest(finish):
                    raise Unavailable('idempotency_conflict')
                if prior and prior['response']['status'] not in c.REEVALUATED + ('unknown', 'pending'):
                    return False
                terminal = dict(request=finish, digest=digest(finish), revision=prior['revision'] if prior else 0,
                                process=None, response=self.actions.reply(finish, 'finished', identity=after))
                # Persist an acknowledged uncertain phase before appending finish;
                # both commits remain under control.lock. A crash retains the hold.
                self.actions.save(current, reservation)
                reservation.update(open=False, uncertain=False, identity=after, acceptance=finish['acceptance'])
                self.actions.save(terminal, reservation)
                return True
        reservation['reconcile_reason'] = reason
        if current['response']['status'] == 'unknown':
            current['response'] = self.actions.reply(r, 'unknown', reason, held=True)
        if current != old or reservation != before:
            self.actions.save(current, reservation)
        return True

    def run(self, offline=None):
        try:
            copies = self.copies(offline)
            if not copies:
                return 0
            self.boundary('copied')
            proposals = self.bounded_gather(copies, offline)
            self.boundary('collected')
            with self.lock():  # one nonblocking entry and one generation read per batch
                snapshot = self.observe(offline)
                return sum(self.apply(item, proposal, offline, snapshot) for item, proposal in zip(copies, proposals))
        except (BlockingIOError, OSError, ValueError, KeyError, TypeError, sqlite3.Error,
                subprocess.SubprocessError, Unavailable):
            return 0  # Failure preserves every reservation, including at startup.


def main(engine, stdin, stdout, birth=None):
    raw = stdin.read(c.MAX_BYTES + 1)
    result, code = ClientActions(engine, birth=birth).dispatch(raw)
    stdout.write(canonical(result) + '\n')
    return code


if __name__ == '__main__':
    # Internal child only: args come from the closed operation map above.
    if len(sys.argv) < 4 or sys.argv[1] != '--rpc-gate' or sys.stdin.buffer.read(1) != b'1':
        sys.exit(2)
    os.execv(sys.argv[2], sys.argv[2:])
