"""CC1. Durable client phases; only this coordinator sends client mutations.

The installation supplies engine, adapter and binary. No client field selects
any of them. Adapters are bounded local evidence reads, never launcher calls
or result locks. CC2 collects retained actions outside the host lock.
"""
from contextlib import nullcontext
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
import tempfile

# Loading through compile also works with the controller's isolated interpreter
# and cannot create bytecode during a read-only capabilities request.
c = types.ModuleType('client_contract')
_contract = Path(__file__).with_name('client_contract.py')
exec(compile(_contract.read_bytes(), str(_contract), 'exec'), c.__dict__)


EMITTER = Path(__file__).with_name('client_rpc.py')
# CC3-P1: each daemon read of the CC1 turn uses half of CC2's .4 s batch.
READ_SECONDS = .2
SEAL_SECONDS = 1.0  # Bounds only the seal wait, never the whole turn.
STATUS = {'hold': 'held', 'cancel-no-payload': 'started', 'signal': 'signalled',
          'signal-manual': 'signalled', 'retire-held': 'retired', 'retire-interrupted': 'retired'}
_RPC = None


def rpc_module():
    global _RPC
    if _RPC is None:
        module = types.ModuleType('client_rpc')
        module.__file__ = str(EMITTER)
        exec(compile(EMITTER.read_bytes(), str(EMITTER), 'exec'), module.__dict__)
        module.c = c  # One contract: its Invalid is the one this module catches.
        _RPC = module
    return _RPC


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

    def __init__(self, engine, adapter=None, rpc=None, birth=None, exclusion=None):
        self.engine = engine
        self.adapter = adapter if adapter is not None else InstalledAdapters()
        self.rpc = rpc  # Test seam: a synthetic conversation inside the real emit().
        self.birth = birth if birth is not None else getattr(engine, 'client_birth', None)
        # CC7's installation fixes the profile; the wire never selects it.
        profile = exclusion if exclusion is not None else getattr(engine, 'client_exclusion', 'coordinated')
        self.exclusion = profile if profile in c.EXCLUSION_PROFILES else 'coordinated'

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

    def bounded(self, read, *args):
        """P1: a daemon read of the turn never waits without its own deadline."""
        pueue = getattr(self.engine, 'pueue', None)
        if pueue is None:
            return read(*args)
        saved = getattr(pueue, 'timeout', None), getattr(pueue, 'deadline', None)
        pueue.timeout, pueue.deadline = READ_SECONDS, time.monotonic() + READ_SECONDS
        try:
            return read(*args)
        finally:
            pueue.timeout, pueue.deadline = saved

    def observe(self, r, state, reservation):
        # Without CC7's `filtered` profile a remote-run effect is never attributable:
        # its mutating phases, and its per-run capability, answer unsupported.
        if r['launcher'] == 'remote-run' and r['op'] != 'finish' and self.exclusion != 'filtered':
            raise Unavailable('unsupported')
        observation = self.bounded(self.adapter.observe, r, state, reservation)
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
                if self.bounded(self.engine.pueue.status)['groups'][group]['status'] != 'Running':
                    return reject('manual', 'deferred')
            if r['op'] == 'finish':
                previous.update(open=False, uncertain=False, identity=current)
                record['response'] = self.reply(r, 'finished', identity=current)
                self.save(record, previous)
                return record['response']
            if self.rpc is None and (self.birth is None or not EMITTER.is_file()):
                return reject('unsupported')  # Stock Pueue has no raw path.
            life = self.daemon_life(state, current['generation'])  # D of the prior read P.
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
            self.deliver(record, group, current, life, args, state)
            self.boundary('rpc_returned')
            # Neither the CLI code nor the edge confirm an effect: only rpc_verdict
            # over the originals, P, a Q read after the seal, and `filtered`.
            verdict, after, post = self.stock_verdict(
                record, lambda receipt: self.posterior(r, group, reservation, receipt))
            self.settle(record, reservation, verdict, after, post)
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

    def daemon_life(self, state, generation):
        """Life D: native PID and birth of the daemon named by the authority's pid_file."""
        try:
            pid = int(Path(state['pid_file']).read_text().strip())
            born = self.birth(pid) if self.birth is not None else None
            return c.rpc_life(dict(pid=pid, birth=born, generation=generation))
        except (OSError, ValueError):
            raise Unavailable('startup') from None

    def alive(self, pid, born):
        try:
            return self.birth is None or self.birth(pid) == born
        except (OSError, ValueError):
            return True  # An unreadable identity never proves an end of life.

    @staticmethod
    def stock(process):
        """A phase delivered by the CC3 emitter. Older children keep their rule."""
        return type(process) is dict and 'dispatch' in process

    def deliver(self, record, group, current, life, args, state):
        """Plan, gated identity, dispatch, then at most SEAL_SECONDS for the seal.

        The emitter waits on stdin until its identity is FULL in the journal. It
        is never killed nor reaped: a kill cannot exclude a request the daemon
        already read, and it would destroy the only witness. It does not inherit
        control.lock. stdout/stderr, argv and env never enter the journal.
        """
        r = record['request']
        rpc = rpc_module()
        child = None
        if self.rpc is None:
            endpoint = self.engine.rpc_endpoint(state)
            child = subprocess.Popen([sys.executable, '-I', '-S', str(EMITTER), str(self.engine.directory),
                                      *map(str, endpoint)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
                                     **self.engine.pueue.context)
        pid = child.pid if child is not None else os.getpid()
        try:
            born = self.birth(pid)
            if born is None:
                raise Unavailable('rpc_unresolved')
            dispatch = os.urandom(32).hex()
            plan = c.validate_rpc_plan(dict(schema=c.RPC_PLAN, dispatch=dispatch, request=r, group=group,
                                            verb=c.RPC_VERBS[r['phase']], task_id=current['task_id'],
                                            daemon=life, pid=pid, birth=born))
            folder = self.engine.directory / 'client-rpc'
            folder.mkdir(mode=0o700, exist_ok=True)
            folder = folder / dispatch
            folder.mkdir(mode=0o700)
            rpc.publish(folder, 'plan.json', plan, c.RPC_PLAN_MAX_BYTES)
            record['process'] = dict(pid=pid, birth=born, dispatch=dispatch,
                                     plan_digest=c.rpc_digest(plan), state='gated')
            record['pre'] = dict(life=life, identity=current)
            self.save(record)
            self.boundary('gated')
            if child is None:
                self.injected(dispatch, (pid, born), args)
            else:
                child.stdin.write(dispatch.encode('ascii'))
                child.stdin.close()
            record['process']['state'] = 'sent'
            self.save(record)
            if child is not None:
                deadline = time.monotonic() + SEAL_SECONDS
                while (time.monotonic() < deadline and not (folder / 'seal.json').exists()
                       and self.alive(pid, born)):
                    time.sleep(.005)
        finally:
            if child is not None and not child.stdin.closed:
                child.stdin.close()  # EOF before the dispatch: the gate never opens.

    def injected(self, dispatch, own, args):
        """Test seam: the real claim/seal protocol with a synthetic conversation.

        The double may return a seal outcome; an exception is a local abort
        after frame 3, which leaves no seal, exactly like the emitter.
        """
        rpc = rpc_module()

        def talk(plan):
            try:
                outcome = self.rpc(*args)
            except (OSError, subprocess.SubprocessError):
                raise rpc.Abort() from None
            result = dict(peer={k: plan['daemon'][k] for k in ('pid', 'birth')},
                          outcome='replied', reply='success', reason=None)
            if type(outcome) is dict:
                result.update(outcome)
            return result
        rpc.emit(self.engine.directory, dispatch, own, talk)

    def originals(self, process):
        folder = self.engine.directory / 'client-rpc' / process['dispatch']
        found = {}
        for name, parse, limit in (('seal', c.parse_rpc_seal, c.RPC_MAX_BYTES),
                                   ('claim', c.parse_rpc_claim, c.RPC_MAX_BYTES),
                                   ('plan', c.parse_rpc_plan, c.RPC_PLAN_MAX_BYTES)):
            try:
                with (folder / (name + '.json')).open('rb') as stream:
                    found[name] = parse(stream.read(limit + 1))  # Invalid: retained by the caller.
            except FileNotFoundError:
                found[name] = None
        return found

    def stock_verdict(self, record, posterior=None, post=None):
        """(verdict, E, Q) for a stock phase; shared by CC1, collect and apply.

        Lives are read before the originals: a dead emitter already published
        whatever it sealed. Q is read only after a success seal and names it.
        """
        r, process = record['request'], record['process']
        try:
            emitter_alive = self.alive(process['pid'], process['birth'])
            found = self.originals(process)
            plan, claim, seal = found['plan'], found['claim'], found['seal']
            daemon = plan['daemon'] if plan is not None else record['pre']['life']
            life_alive = self.alive(daemon['pid'], daemon['birth'])
            if (posterior is not None and seal is not None and seal['outcome'] == 'replied'
                    and seal['reply'] == 'success'):
                try:
                    post = posterior(seal['receipt'])
                except (Unavailable, OSError, ValueError, KeyError, TypeError, sqlite3.Error,
                        subprocess.SubprocessError):
                    post = None  # A success without Q stays unknown and retriable.
            verdict = c.rpc_verdict(record, plan, claim, seal, emitter_alive, life_alive,
                                    record['pre'], post, self.exclusion)
        except (c.Invalid, KeyError, TypeError):
            return (False, None, 'evidence_missing'), None, None
        after = None
        if verdict == (True, True, None):
            after = c.rpc_effect_identity(r['op'], record['pre']['identity'], post['identity'])
        return verdict, after, post

    def posterior(self, r, group, reservation, receipt):
        """Q of CC1: after the seal, in the same life D, with its own read deadline."""
        state = self.authority()
        before = self.daemon_life(state, r['expected']['generation'])
        observed = self.observe(r, state, reservation)
        if observed['group'] != group:
            raise Unavailable('identity')
        life = self.daemon_life(state, observed['identity']['generation'])
        if {k: life[k] for k in ('pid', 'birth')} != {k: before[k] for k in ('pid', 'birth')}:
            raise Unavailable('startup')
        return dict(life=life, identity=observed['identity'], seal=receipt)

    def settle(self, record, reservation, verdict, after, post):
        """Phase receipt and reservation from rpc_verdict, never from rc or the edge."""
        r = record['request']
        settled, effect, reason = verdict
        if effect is True:
            c.transition(r['expected'], after, r['op'])
            record['response'] = self.reply(r, STATUS[r['op']], identity=after, held=True)
            if r['op'] == 'hold':
                reservation['hold_identity'] = after
            reservation.update(identity=after, uncertain=False)
            record['attribution'] = dict(dispatch=record['process']['dispatch'], seal_receipt=post['seal'],
                                         exclusion=self.exclusion)
        elif effect is False:
            # R− and N2: the initial hold closes; every other phase keeps its reservation.
            close = r['op'] == 'hold' and reservation.get('hold_identity') is None and reservation['acceptance'] is None
            status = 'rejected' if close or reason == 'stale_state' else 'deferred'
            record['response'] = self.reply(r, status, reason, held=not close)
            reservation.update(open=not close, uncertain=False)
        else:
            record['response'] = self.reply(r, 'unknown', reason, held=True)
            if settled:
                reservation['uncertain'] = False  # S3/S4/success without Q: no message in flight.


class Reconciler:
    """CC2: bounded copy / collect / compare. Never sends a Pueue mutation.

    recover is a fixed adapter's evidence-only interface. It returns observation,
    settled (a positive exclusion of late RPCs, NOT child death), effect_applied
    (durable launcher evidence bound to the phase key and child's PID/birth), and optional
    finish (a full CC0 request authenticated by verify). The collector runs in
    a disposable process, outside control.lock; no result lock can stall guard.
    """
    limit = 8
    seconds = .4
    offline = None
    boundary = staticmethod(lambda phase: None)

    def __init__(self, engine, adapter=None, birth=None, exclusion=None):
        self.engine = engine
        self.actions = ClientActions(engine, adapter=adapter, birth=birth, exclusion=exclusion)
        self.birth = birth

    def lock(self):
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
            return None  # Absence is compared too; a restart invalidates the copy.

    def daemon_stamp(self, state, offline):
        # Native PID/birth/file identity only: safe in the caller's protected
        # turn. The collector authenticates mini_generation outside the lock.
        try:
            path = Path(state['pid_file'])
            before = path.stat()
            pid = int(path.read_text().strip())
            born = self.birth(pid) if self.birth is not None else None
            after = path.stat()
            signature = lambda st: [st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size]
            if born is None or signature(before) != signature(after):
                raise ValueError('client_generation')
            return [pid, born, signature(after)]
        except (OSError, ValueError):
            return None

    def apply_generation(self, state, item, offline):
        if 'daemon_stamp' not in item:
            return self.current_generation(state, offline)
        # A fresh native check for EACH action: the daemon can die between two
        # saves even while control.lock excludes every coordinated writer.
        if self.daemon_stamp(state, offline) != item['daemon_stamp']:
            return None
        return item['generation']

    def copies(self, offline, protected=False, advance=True):
        with (nullcontext() if protected else self.lock()), self.engine.connect(write=True) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='client_reservations'").fetchone():
                return []
            self.actions.index(db)
            keys = list(db.execute('SELECT launcher,ref,action_id FROM client_open ORDER BY checked LIMIT ?', (self.limit,)))
            if not keys:
                return []
            state = self.engine.state(db)
            native = self.engine.host == 'mini' and self.engine.generation_source is None
            stamp = self.daemon_stamp(state, offline) if native else None
            generation = (state['generation'] if stamp is not None else None) if native else self.current_generation(state, offline)
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
                if native:
                    result[-1]['daemon_stamp'] = stamp
                if advance:
                    db.execute('UPDATE client_open SET checked=? WHERE launcher=? AND ref=? AND action_id=?', (sequence,) + key)
            return result

    @staticmethod
    def effect_matches(proof, record):
        # Shape and binding are checked again under the apply lock. This is an
        # internal fixed-adapter receipt, never a client-supplied affirmation.
        if type(proof) is not dict or set(proof) != {'launcher', 'ref', 'action_id', 'phase', 'pid', 'birth'}:
            return False
        r, process = record['request'], record.get('process') or {}
        return (all(proof[k] == r[k] for k in ('launcher', 'ref', 'action_id', 'phase'))
                and type(proof['pid']) is int and proof['pid'] > 0
                and proof['pid'] == process.get('pid')
                and type(proof['birth']) is str and bool(proof['birth'])
                and proof['birth'] == process.get('birth'))

    @staticmethod
    def journal_proposal(item):
        record = item['record']
        if item['rejected_unsent']:
            return dict(kind='rejected_not_sent')
        if record['response']['status'] == 'unknown' and record.get('process') is None:
            # CC1 commits PID/birth before opening the RPC gate.
            return dict(kind='not_sent')
        return None

    def collect(self, item, daemon):
        record, reservation = item['record'], item['reservation']
        r, process = record['request'], record.get('process')
        journal = self.journal_proposal(item)
        if journal is not None:
            return journal
        stock = self.actions.stock(process)
        if stock and record['response']['status'] == 'unknown':
            return self.collect_stock(item)
        if item['generation'] != item['state']['generation']:
            raise Unavailable('startup')
        if 'daemon_stamp' in item:
            # ps may wait: authenticate only in the bounded collector, pinning
            # it to the copied native life. Apply checks that life per action.
            if (self.daemon_stamp(item['state'], None) != item['daemon_stamp']
                    or self.current_generation(item['state'], None) != item['generation']
                    or self.daemon_stamp(item['state'], None) != item['daemon_stamp']):
                raise Unavailable('startup')
        # A settled stock phase already has its seal: a live emitter is irrelevant.
        if process and process.get('pid') is not None and not stock:
            if self.birth is None or self.birth(process['pid']) == process['birth']:
                raise Unavailable('rpc_unresolved')
        evidence = self.actions.adapter.recover(r, item['state'], reservation, process, daemon)
        if daemon is None:
            raise Unavailable('startup')
        c.keys(evidence, 'observation settled finish' + (' effect_applied' if 'effect_applied' in evidence else ''))
        observed = evidence['observation']
        c.keys(observed, 'group identity')
        c.identity(observed['identity'])
        if (observed['group'] != reservation['group'] or
                not self.actions.same_attempt(reservation['identity'], observed['identity'])):
            raise Unavailable('identity')
        if stock:
            # P1: a queue phase settles only by S1-S4; the adapter's flag is ignored.
            evidence = dict(evidence, settled=True)
        if evidence['settled'] is not True:
            raise Unavailable('rpc_unresolved')
        # Excluding a late message does not attribute an external Stashed to
        # our hold. Older adapters without effect evidence must retain unknown.
        if record['response']['status'] == 'unknown' and not self.effect_matches(evidence.get('effect_applied'), record):
            raise Unavailable('evidence_missing')
        finish = evidence['finish']
        if finish is not None:
            c.validate_request(finish)
            if (finish['op'] != 'finish' or self.actions.key(finish)[:3] != self.actions.key(r)[:3]
                    or finish['mode'] != reservation['mode'] or finish['expected'] != observed['identity']
                    or reservation['acceptance'] is not None and finish['acceptance'] != reservation['acceptance']):
                raise Unavailable('identity')
            self.actions.adapter.verify(finish, observed, reservation)
        return dict(kind='evidence', observation=observed, finish=finish, effect_applied=evidence.get('effect_applied'))

    def collect_stock(self, item):
        """P1: settled and effect of an uncertain stock phase come from rpc_verdict.

        Outside control.lock: lives, originals, then a fresh Q only after a
        success seal, so Q names the seal it followed. The adapter supplies only
        Q's observation. A restart does not block S4: rpc_verdict pins life D.
        apply recomputes the verdict under the lock with this Q.
        """
        record, reservation = item['record'], item['reservation']
        r = record['request']

        def posterior(receipt):
            if self.offline or type(self.actions.adapter) is InstalledAdapters:
                raise Unavailable('unsupported')
            daemon = self.engine.pueue.status()  # After the seal, inside the batch deadline.
            evidence = self.actions.adapter.recover(r, item['state'], reservation, record['process'], daemon)
            observed = evidence['observation']
            c.keys(observed, 'group identity')
            c.identity(observed['identity'])
            if observed['group'] != reservation['group']:  # An absence has no group: the reservation's.
                raise Unavailable('identity')
            life = self.actions.daemon_life(item['state'], observed['identity']['generation'])
            return dict(life=life, identity=observed['identity'], seal=receipt)

        verdict, after, post = self.actions.stock_verdict(record, posterior)
        if verdict[1] is True and not self.actions.same_attempt(reservation['identity'], after):
            raise Unavailable('identity')
        return dict(kind='stock', post=post)

    def gather(self, copies, offline):
        proposals = []
        self.offline = offline
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

    def apply(self, item, proposal, offline, protected=False):
        with (nullcontext() if protected else self.lock()):
            old, before = item['record'], item['reservation']
            r = old['request']
            current = self.actions.record(r)
            reservation = self.actions.reservation(r)
            with self.engine.connect() as db:
                state = self.engine.state(db)
            generation = self.apply_generation(state, item, offline)
            if (state['generation'] != item['state']['generation'] or generation != item['generation']
                    or current['revision'] != old['revision']
                    or current['digest'] != old['digest']
                    or current.get('process') != old.get('process')
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
            elif proposal['kind'] == 'stock':
                # The verdict is recomputed here over fresh originals and lives,
                # without any daemon read; only the collector's Q travels.
                if not self.actions.stock(current.get('process')) or current['response']['status'] != 'unknown':
                    return False
                verdict, after, post = self.actions.stock_verdict(current, post=proposal.get('post'))
                self.actions.settle(current, reservation, verdict, after, post)
                reason = verdict[2] or reason
            elif proposal['kind'] == 'evidence':
                observed, finish = proposal['observation'], proposal['finish']
                after = observed['identity']
                if current['response']['status'] == 'unknown':
                    if not self.effect_matches(proposal.get('effect_applied'), current):
                        raise Unavailable('evidence_missing')
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
            self.advance(r)
            return True

    def advance(self, r):
        with self.engine.connect(write=True) as db:
            key = self.actions.key(r)[:3]
            sequence = db.execute('SELECT COALESCE(MAX(checked),0)+1 FROM client_open').fetchone()[0]
            db.execute('UPDATE client_open SET checked=? WHERE launcher=? AND ref=? AND action_id=?', (sequence,) + key)

    @staticmethod
    def binding(item):
        record, reservation = item['record'], item['reservation']
        return dict(key=ClientActions.key(record['request']), process=record.get('process'),
                    generation=item['generation'], authority=item['state']['generation'],
                    revision=record['revision'], digest=record['digest'],
                    reservation_revision=reservation['revision'], reservation_digest=reservation['digest'],
                    phase=reservation['phase'], daemon_stamp=item.get('daemon_stamp'))

    def evidence_path(self, item):
        # Fixed internal path, derived only from the validated action key.
        return self.engine.directory / 'client-evidence' / (digest(self.binding(item)) + '.json')

    def cache(self, item, proposal):
        # Disposable evidence, never authority. Atomic publication occurs AFTER
        # collection and outside control.lock. A crash only causes recollection.
        path = self.evidence_path(item)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        body = canonical(dict(binding=self.binding(item), proposal=proposal)).encode()
        if len(body) > c.MAX_BYTES * self.limit:
            return
        fd, name = tempfile.mkstemp(prefix='.collect-', dir=str(path.parent))
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(body)
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def cached(self, item):
        try:
            with self.evidence_path(item).open('rb') as stream:
                raw = stream.read(c.MAX_BYTES * self.limit + 1)
            if len(raw) > c.MAX_BYTES * self.limit:
                return None
            value = json.loads(raw)
            # JSON normalizes the tuple in binding.key to a list.
            if value['binding'] != json.loads(canonical(self.binding(item))):
                return None
            proposal = value['proposal']
            return proposal if proposal.get('kind') in ('evidence', 'stock', 'retain') else None
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def protected_turn(self, offline=None):
        """Caller still owns its original lock and has finished its own duties.

        Do not advance the rotating cursor until a review is applied: an action
        copied in tick N remains in the reached batch in N+1, even with more
        than limit open actions and every independent acquisition colliding.
        Reviewing cached evidence never replaces collection for an open action;
        refresh its copy after applying so the next proposal binds to new state.
        Native copies read only PID/birth/file identity, not ps generation.
        No adapter, process wait or collection occurs here.
        """
        copies = self.copies(offline, protected=True, advance=False)
        pending = []
        for item in copies:
            proposal = self.journal_proposal(item)
            if proposal is None:
                proposal = self.cached(item)
            if proposal is None:
                pending.append(item)
                continue
            try:
                reviewed = self.apply(item, proposal, offline, protected=True)
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error,
                    subprocess.SubprocessError, Unavailable):
                reviewed = False
            if reviewed:
                r = item['record']['request']
                item['reservation'] = self.actions.reservation(r)
                if not item['reservation']['open']:
                    continue
                item['record'] = self.actions.record(r)
            pending.append(item)  # Every still-open action collects again outside the turn.
        return pending

    def run(self, offline=None, copies=None):
        try:
            copies = self.copies(offline) if copies is None else copies
            if not copies:
                return 0
            self.boundary('copied')
            proposals = self.bounded_gather(copies, offline)
            self.boundary('collected')
            for item, proposal in zip(copies, proposals):
                self.cache(item, proposal)
            return sum(self.apply(item, proposal, offline) for item, proposal in zip(copies, proposals))
        except (BlockingIOError, OSError, ValueError, KeyError, TypeError, sqlite3.Error,
                subprocess.SubprocessError, Unavailable):
            return 0  # Failure preserves every reservation, including at startup.


def main(engine, stdin, stdout, birth=None, exclusion=None):
    raw = stdin.read(c.MAX_BYTES + 1)
    result, code = ClientActions(engine, birth=birth, exclusion=exclusion).dispatch(raw)
    stdout.write(canonical(result) + '\n')
    return code
