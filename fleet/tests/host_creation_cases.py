"""Real 4c receipts for Fleet host rounds: an in-process effective Hostgate, never hand-written replies.

The Fleet job comes from the public CLI (HostHarness); the round, its grant and every creation
step are those the authority of fleet/hostgate answers. Tests drive the ledger through
fleet.storage, as the 4e supervisor will.
"""
import contextlib
from dataclasses import replace
import json
import os
import sqlite3

from test_host_reservation_cli import HostHarness
from fleet import native, storage
from fleet.host_reservation import DEMAND, SCHEMA
from fleet.model import Closure, HostIntent, Step, dump
from fleet.storage import Ledger

LAUNCH = dict(argvDigest="a" * 64, inputDigest="b" * 64, bootstrapDigest="c" * 64, launcherDigest="e" * 64)
ROOT = dict(pid=300, unique=30)


@contextlib.contextmanager
def default_umask():
    previous = os.umask(0o022)  # the hostgate store checks the modes of the default umask
    try:
        yield
    finally:
        os.umask(previous)


class Authority:
    """Effective Hostgate, effective store and the coalition of owner pid 100, uniqueid 10.

    The coalition is hostgate's own test provider over the substituted kernel; its kernel
    exists from the first authorize on, and tests add and end the root there.
    """

    def __init__(self, root):
        from hostgate.admission import Hostgate
        from hostgate.store import EffectiveStore
        from hostgate.tests.support import coalition
        from hostgate.tests.support.effective import Coalition
        from hostgate.tests.support.world import World, config
        from hostgate.tests.test_admin import fixture_policy
        root.mkdir(mode=0o700)
        self.provider, self.world = Coalition(), World()
        self.world.boot, self.world.coalition = coalition.BOOT, self.provider
        with default_umask():
            with fixture_policy(root):
                self.config = replace(config(root / "state"), mode="effective", store_id="effective-store")
            EffectiveStore.initialize(self.config)
        self.gate = Hostgate(self.config, self.world)
        self.auth = self.world.auth(pid=100, start=(1, 2, 10, 7))

    def send(self, request):
        op, key, name = request["operationId"], request.get("key"), request["operation"]
        with default_umask():
            if name == "reservar":
                return self.gate.reservar(request, self.auth)
            if name == "authorize_creation":
                return self.gate.authorize_creation(op, key, request["confirmation"], request["launch"], self.auth)
            if name == "consume_creation":
                return self.gate.consume_creation(op, key, request["permitId"], request["launchDigest"], self.auth)
            if name == "record_started":
                return self.gate.record_started(op, key, request["permitId"], request["root"], self.auth)
            return self.gate.soltar(op, key, request["evidenceIds"], self.auth)

    @property
    def kernel(self):
        return self.provider.kernel

    def estado(self):
        with default_umask():
            return self.gate.estado({}, self.auth)


class CreationHarness(HostHarness):
    """One enqueue-only host job; its round 1 reserves against the in-process authority."""

    def setUp(self):
        super().setUp()
        code, job = self.submit(enqueue_only=True)
        self.assertEqual((code, job.get("accepted")), (0, True), job)
        self.job, self.attempt_id = job["job"], job["attempt"]
        self.authority = Authority(self.root / "g")
        self.me = native.identity(os.getpid())

    @contextlib.contextmanager
    def db(self):
        with Ledger(self.state, create=True, initialize=False) as db:
            yield db

    def intent(self, ordinal=1):
        node = self.authority.config
        op = storage.host_operation_id(self.attempt_id, ordinal)
        request = {"schema": SCHEMA, "operationId": op, "operation": "reservar", "jobId": self.job,
                   "attemptId": self.attempt_id, "executorId": "unix", "physicalHostId": node.host_id,
                   "demand": DEMAND, "exclusiveResources": [], "profileId": "fixture"}
        return HostIntent(self.attempt_id, ordinal, op, request, node.host_id, "unix", node.store_id,
                          node.revision, self.me)

    def current(self, db=None):
        if db is None:
            with self.db() as db:
                return storage.host_rounds(db, self.attempt_id)[-1]
        return storage.host_rounds(db, self.attempt_id)[-1]

    def grant(self, claim=True):
        """reservar answered by the authority; confirmed and claimed, or held claim_unavailable."""
        intent = self.intent()
        with self.db() as db:
            with db.transaction():
                storage.put_host_intent(db, intent)
            with db.transaction():
                self.assertTrue(storage.mark_host_unknown(db, intent, self.me))
            receipt = self.authority.send(intent.request)
            self.assertTrue(receipt["granted"], receipt)
            with db.transaction():
                if not claim:
                    db.execute("UPDATE control SET drained=1")
                storage.confirm_host_and_claim(db, intent, receipt["reservationKey"], receipt, "reply",
                                               self.me, self.me)
                if not claim:
                    db.execute("UPDATE control SET drained=0")
            return self.current(db)

    def request(self, current, step, seq=1, **fields):
        return dict(schema=SCHEMA, operationId=storage.creation_operation_id(self.attempt_id, 1, step, seq),
                    operation=storage.OPERATIONS[step], key=current.key, **fields)

    def requests(self, current, permit=None, seq=1):
        """The wire request of each step, as 4e will send it."""
        from hostgate.protocol import digest
        return {"authorize": self.request(current, "authorize", confirmation=storage.creation_confirmation(
                    current.intent, current.source), launch=LAUNCH),
                "consume": permit and self.request(current, "consume", permitId=permit, launchDigest=digest(LAUNCH)),
                "start": permit and self.request(current, "start", permitId=permit, root=ROOT),
                "release": self.request(current, "release", seq, evidenceIds=[])}

    def walk(self, current):
        """Every original step of a creation that ends closed: requests, receipts and estado's closure."""
        authorize = self.requests(current)["authorize"]
        issued = self.authority.send(authorize)
        steps = self.requests(current, issued["permit"]["permitId"])
        consumed = self.authority.send(steps["consume"])
        self.authority.kernel.add(ROOT["pid"], ROOT["unique"], parent=10)
        started = self.authority.send(steps["start"])
        busy = self.authority.send(steps["release"])
        self.authority.kernel.exit(ROOT["pid"])
        final = self.requests(current, issued["permit"]["permitId"], seq=2)["release"]
        closed = self.authority.send(final)
        self.assertEqual((busy["exitCode"], closed["exitCode"], closed["state"]), (1, 0, "released"), closed)
        return dict(authorize=Step(authorize, issued), consume=Step(steps["consume"], consumed),
                    start=Step(steps["start"], started), busy=Step(steps["release"], busy),
                    release=Step(final, closed), closure=self.closure(current))

    def closure(self, current):
        estado = self.authority.estado()
        row = next(r for r in estado["reservations"] if r["key"] == current.key)
        return Closure(row["permit"], row["closureEvidence"])

    def plant(self, state, creation, reason=None, ordinal=1, **columns):
        """Rewrite round `ordinal` offline; SQLite closed before any reader starts."""
        conn = sqlite3.connect(str(self.state / "state.db"))
        try:
            with conn:
                conn.execute("UPDATE host_rounds SET state=?,reason=?,creation_json=? WHERE attempt=? AND ordinal=?",
                             (state, reason, None if creation is None else storage.creation_json(creation),
                              self.attempt_id, ordinal))
                for name, value in columns.items():
                    conn.execute("UPDATE host_rounds SET {}=? WHERE attempt=? AND ordinal=?".format(name),
                                 (value, self.attempt_id, ordinal))
        finally:
            conn.close()
        for path in self.state.glob("state.db*"):
            path.chmod(0o600)

    def second_round(self):
        """Insert round 2 of the same attempt, prepared; False when the open index refuses it."""
        intent = self.intent(2)
        conn = sqlite3.connect(str(self.state / "state.db"))
        try:
            with conn:
                conn.execute("INSERT INTO host_rounds (attempt,ordinal,operation_id,request_json,request_sha256,"
                             "physical_host_id,executor_id,store_id,config_revision,requester_identity,state) "
                             "SELECT attempt,2,?,?,?,physical_host_id,executor_id,store_id,config_revision,"
                             "requester_identity,'prepared' FROM host_rounds WHERE ordinal=1",
                             (intent.operation_id, dump(intent.request), storage.request_sha256(intent.request)))
            return True
        except sqlite3.IntegrityError:
            return False
        finally:
            conn.close()

    def observed(self):
        return self.status(self.job)

    def round_view(self):
        return self.observed()["reservation"]

    def raw(self):
        rows = self.ledger("SELECT state,creation_json FROM host_rounds WHERE attempt=? ORDER BY ordinal",
                           (self.attempt_id,))
        return [(state, json.loads(raw) if raw else None) for state, raw in rows]

