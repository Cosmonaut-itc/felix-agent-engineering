"""CC1. Durable client phases; only this coordinator sends client mutations.

The installation supplies engine, adapter and binary. No client field selects
any of them. Adapters are bounded local evidence reads, never launcher calls
or result locks. CC2's Reconciler recovers retained/unknown reservations.
"""
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import types

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

    def evidence(self, reservation):
        raise Unavailable('unsupported')


def reservations(engine):
    """Read-only seam for host writers; caller holds the real control.lock.

    Missing table is the additive pre-CC1 format, not a missing authority.
    Corrupt/unreadable tables propagate: host operations must fail closed.
    No clock, expiry, launcher callback or result.lock participates here.
    """
    with engine.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='client_reservations'").fetchone():
            return []
        values = [json.loads(row[0]) for row in db.execute('SELECT body FROM client_reservations')]
    return [valid_reservation(value) for value in values]


def valid_reservation(value):
    if (type(value) is not dict or type(value.get('version')) is not int or value['version'] != 1 or
            type(value.get('open')) is not bool or type(value.get('uncertain')) is not bool or
            type(value.get('group')) is not str or not value['group'] or
            value.get('retained') not in (None,) + c.REASONS):
        raise ValueError('client_journal')
    c.identity(value['identity'])
    return value


def blocked(engine, group, opening=False):
    for item in reservations(engine):
        if item['group'] == group and item['open']:
            # A known cancellation hold must not prevent its own safe admission.
            if not opening or item['uncertain']:
                return True
    return False


def protected_ids(engine):
    return {str(r['identity']['task_id']) for r in reservations(engine) if r['open']}


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
        return next((v for v in reservations(self.engine) if tuple(v[k] for k in
                    ('launcher', 'ref', 'action_id')) == self.key(r)[:3]), None)

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
            db.execute('INSERT OR REPLACE INTO client_actions VALUES (?,?,?,?,?)', self.key(record['request']) + (canonical(record),))
            if reservation is not None:
                db.execute('INSERT OR REPLACE INTO client_reservations VALUES (?,?,?,?)', self.key(record['request'])[:3] + (canonical(reservation),))
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
        # Only a receipt without effect and without RPC is evaluated again.
        if record and (query or record['response']['status'] not in c.REEVALUATED or record['digest'] != digest(r)):
            result = (self.reply(r, 'rejected', 'idempotency_conflict', held=record['response']['reservation'] == 'held')
                      if record['digest'] != digest(r) else record['response'])
        elif query:
            result = self.reply(r, 'rejected', 'not_found')
        else:
            result = self.execute(r, record)
        return c.validate_response(dict(result, op='status', action_op=r['op']) if query else result, request)

    def execute(self, r, prior=None):
        previous = self.reservation(r)
        held = bool(previous and previous['open'])
        # Revisions stay monotonic across a deferred re-evaluation (no ABA for CC2).
        record = dict(request=r, digest=digest(r), revision=prior['revision'] if prior else 0,
                      response=None, process=None)
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
                if not previous['open'] and not (previous.get('released') and r['op'] == 'hold'):
                    return reject('stale_state')
                if previous['uncertain']:
                    return reject('rpc_unresolved', 'deferred')
                if previous['acceptance'] is not None and previous['acceptance'] != r['acceptance']:
                    return reject('identity')
            if r['op'] in ('cancel-no-payload', 'retire-held', 'finish') and not held:
                return reject('evidence_missing')
            if r['op'] in ('cancel-no-payload', 'retire-held') and previous.get('hold_identity') != current:
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
                # Synthetic transport: durable dispatch mark, never provably unsent.
                record['process'] = dict(state='direct')
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


ERRORS = (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError)
NO_EFFECT = ('deferred', 'rejected')
# Ops whose effect a fixed adapter can observe in the task identity alone.
OBSERVABLE = {'hold': 'held', 'cancel-no-payload': 'started',
              'retire-held': 'retired', 'retire-interrupted': 'retired'}
LOCK_WAIT = 1.0


class Busy(Exception):
    pass


def retained(engine, group):
    """Closed reason of the first retained open reservation of group, or None."""
    reasons = [v.get('retained') for v in reservations(engine) if v['group'] == group and v['open']]
    return next((r for r in ('rpc_unresolved', 'unsupported') if r in reasons),
                next((r for r in reasons if r), None))


class Reconciler:
    """CC2: autonomous recovery of client actions without runner/waiter/HTTP.

    copy (control.lock) -> collect (no lock) -> compare and decide (control.lock).
    Collect only calls the fixed adapter, the child birth identity and the
    daemon generation. With control.lock held nothing calls a launcher, waits
    for result.lock, recurses into client, waits for a signal grace or sends a
    Pueue RPC. No reservation expires; closing needs journal/daemon/adapter proof.
    """
    LIMIT = 32
    boundary = staticmethod(lambda phase: None)

    def __init__(self, engine, adapter=None, birth=None, budget=5.0, clock=time.monotonic):
        self.engine, self.birth, self.budget, self.clock = engine, birth, budget, clock
        self.actions = ClientActions(engine, adapter)

    @contextmanager
    def locked(self):
        # Bounded wait on the installed inode: a slow client never stalls the tick.
        with (self.engine.directory / 'control.lock').open('rb') as handle:
            limit = time.monotonic() + LOCK_WAIT
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= limit:
                        raise Busy()
                    time.sleep(.02)
            yield

    def run(self):
        report = dict(abiertas=0, cerradas=0, avanzadas=0, retenidas=0, cambiadas=0, pendientes=0, ocupado=False)
        deadline = self.clock() + self.budget
        try:
            with self.locked():
                snapshots = self.copy()
            report['abiertas'] = len(snapshots)
            self.boundary('copied')
            proposals = []
            for snap in snapshots:
                if self.clock() >= deadline:
                    report['pendientes'] += 1  # next tick; nothing decided without evidence
                    continue
                proposals.append((snap, self.collect(snap)))
            self.boundary('collected')
            if proposals:
                with self.locked():
                    for snap, evidence in proposals:
                        self.apply(snap, evidence, report)
        except Busy:
            report['ocupado'] = True
        return report

    def snapshot(self, key, generation):
        launcher, ref, action_id = key
        with self.engine.connect() as db:
            row = db.execute('SELECT body FROM client_reservations WHERE launcher=? AND ref=? AND action_id=?', key).fetchone()
            rows = db.execute('SELECT phase FROM client_actions WHERE launcher=? AND ref=? AND action_id=? '
                              'ORDER BY phase', key).fetchall()
        if row is None:
            return None
        value = valid_reservation(json.loads(row[0]))
        phases = {}
        for (phase,) in rows:
            record = self.actions.record(dict(launcher=launcher, ref=ref, action_id=action_id, phase=phase))
            phases[phase] = (record['revision'], record['digest'], record['response']['status'])
        record = self.actions.record(dict(value, phase=value.get('phase')))
        return dict(key=key, generation=generation, reservation=value, record=record, phases=phases)

    def authority(self):
        with self.engine.connect() as db:
            return self.engine.state(db)

    def copy(self):
        state = self.authority()
        with self.engine.connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='client_reservations'").fetchone():
                return []
        keys = sorted((v['launcher'], v['ref'], v['action_id']) for v in reservations(self.engine) if v['open'])
        if not keys:
            return []
        start = int(self.engine.clock() // 60) % len(keys)  # rotation: no starvation under LIMIT
        keys = (keys[start:] + keys[:start])[:self.LIMIT]
        result = []
        for key in keys:
            snap = self.snapshot(key, state['generation'])
            snap['authority'] = state
            result.append(copy.deepcopy(snap))
        return result

    def collect(self, snap):
        """Outside control.lock. Bounded local reads; any failure is just no evidence."""
        evidence = dict(child=None, live=None, observation=None, finish=None, reason=None)
        R, P = snap['reservation'], snap['record']
        if P is None:
            return evidence
        proc = P.get('process') or {}
        if R['uncertain'] and proc.get('pid') is not None and 'dead_generation' not in proc and self.birth:
            try:
                before = self.engine.generation(snap['authority'])
                born = self.birth(proc['pid'])
                after = self.engine.generation(snap['authority'])
                evidence.update(child='alive' if born == proc['birth'] else 'dead',
                                live=before if before == after else None)
            except ERRORS:
                pass
        excluded = R['uncertain'] and proc.get('dead_generation') not in (None, snap['generation'])
        if excluded or not R['uncertain'] and not self.unused(snap):
            try:
                observed = self.actions.observe(P['request'], snap['authority'], R)
                evidence['observation'] = observed
                if not R['uncertain']:
                    published = self.actions.adapter.evidence(R)
                    c.keys(published, 'acceptance proof')
                    if published['proof'] is None:
                        raise Unavailable('evidence_missing')
                    finish = c.validate_request(dict(
                        schema=c.SCHEMA, op='finish', launcher=R['launcher'], ref=R['ref'],
                        action_id=R['action_id'], phase='finish', mode=R['mode'],
                        expected=observed['identity'], acceptance=published['acceptance'],
                        proof=published['proof']))
                    self.actions.adapter.verify(finish, observed, R)
                    evidence['finish'] = finish
            except Unavailable as error:
                evidence['reason'] = error.reason
            except ERRORS:
                evidence['reason'] = 'evidence_missing'
        return evidence

    @staticmethod
    def unused(snap, besides=None):
        """True when no phase of this action had (or may have had) an effect."""
        return all(status in NO_EFFECT for phase, (_, _, status) in snap['phases'].items() if phase != besides)

    def same(self, before, now):
        # The three versions: journal revisions, request digests, authority generation.
        revisions = (now['reservation']['revision'] == before['reservation']['revision'] and
                     [v[0] for v in now['phases'].values()] == [v[0] for v in before['phases'].values()])
        digests = (now['reservation'].get('digest') == before['reservation'].get('digest') and
                   [v[1] for v in now['phases'].values()] == [v[1] for v in before['phases'].values()])
        generation = now['generation'] == before['generation']
        return revisions and digests and generation and list(now['phases']) == list(before['phases'])

    def apply(self, snap, evidence, report):
        try:
            now = self.snapshot(snap['key'], self.authority()['generation'])
        except ERRORS:
            report['cambiadas'] += 1
            return
        if now is None or not now['reservation']['open'] or not self.same(snap, now):
            report['cambiadas'] += 1  # discard; the next pass rereads from scratch
            return
        R, P = now['reservation'], now['record']
        outcome = self.decide(now, R, P, evidence)
        report[outcome] += 1

    def keep(self, R, reason):
        if R.get('retained') != reason:
            R['retained'] = reason
            R['revision'] = R.get('revision', 0) + 1
            with self.engine.connect(write=True) as db:
                db.execute('UPDATE client_reservations SET body=? WHERE launcher=? AND ref=? AND action_id=?',
                           (canonical(R), R['launcher'], R['ref'], R['action_id']))
        return 'retenidas'

    def decide(self, now, R, P, evidence):
        if P is None or P['digest'] != R.get('digest'):
            return self.keep(R, 'evidence_missing')
        r, proc = P['request'], P.get('process')
        if R['uncertain']:
            if P['response']['status'] != 'unknown':
                return self.keep(R, 'evidence_missing')
            if proc is None:
                # No dispatch mark: the coordinator ended before any child/RPC.
                R['uncertain'] = False
                if self.unused(now, besides=r['phase']):
                    R.update(open=False, released=True)
                P['response'] = self.actions.reply(r, 'deferred', 'host_action', held=R['open'])
                self.actions.save(P, R)
                return 'avanzadas' if R['open'] else 'cerradas'
            dead = proc.get('dead_generation')
            if dead is None or dead == now['generation']:
                if dead is None and evidence['child'] == 'dead' and evidence['live'] == now['generation']:
                    # Only a durable fact: the child died under this daemon generation.
                    # Exit alone never excludes a late message the daemon still holds.
                    proc['dead_generation'] = now['generation']
                    self.actions.save(P)
                return self.keep(R, 'rpc_unresolved')
            # Child dead before a supervised daemon change: no late message exists.
            # The effect itself still needs proof; without it the action stays
            # unknown and uncertain (no opening), only the reason changes.
            current = (evidence['observation'] or {}).get('identity')
            if (current is None or r['op'] not in OBSERVABLE or
                    not self.actions.same_attempt(r['expected'], current)):
                if proc.get('state') != 'excluded':
                    proc['state'] = 'excluded'
                    self.actions.save(P)
                return self.keep(R, evidence['reason'] or 'evidence_missing')
            if current == r['expected']:
                # No late message and no effect: a fixed receipt, never a retry.
                response = self.actions.reply(r, 'rejected', 'stale_state', held=True)
            else:
                try:
                    c.transition(r['expected'], current, r['op'])  # only our op's edge
                    response = self.actions.reply(r, OBSERVABLE[r['op']], identity=current, held=True)
                except c.Invalid:
                    return self.keep(R, 'identity')
                R['identity'] = current
                if r['op'] == 'hold':
                    R['hold_identity'] = current  # attributed: our single message, excluded late
            proc['state'] = 'excluded'
            R['uncertain'] = False
            if response['status'] == 'rejected' and self.unused(now, besides=r['phase']):
                R.update(open=False, released=True)
                response = self.actions.reply(r, 'rejected', 'stale_state')
            P['response'] = response
            self.actions.save(P, R)
            return 'avanzadas' if R['open'] else 'cerradas'
        if self.unused(now):
            # Every phase was rejected/deferred: this action never held anything.
            R.update(open=False, released=True)
            self.actions.save(P, R)
            return 'cerradas'
        finish = evidence['finish']
        if finish is None:
            return self.keep(R, evidence['reason'] or 'evidence_missing')
        current = finish['expected']
        prior = now['phases'].get('finish')
        if prior is not None and prior[2] not in c.REEVALUATED:
            return self.keep(R, 'idempotency_conflict')
        if (R['mode'] != finish['mode'] or not self.actions.same_attempt(R['identity'], current) or
                R['acceptance'] is not None and R['acceptance'] != finish['acceptance']):
            return self.keep(R, 'identity')  # first acceptance and attempt stay fixed
        record = dict(request=finish, digest=digest(finish), revision=prior[0] if prior else 0,
                      process=None, response=self.actions.reply(finish, 'finished', identity=current))
        R.update(open=False, uncertain=False, identity=current)
        self.actions.save(record, R)
        return 'cerradas'


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
