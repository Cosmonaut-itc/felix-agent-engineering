"""Durable host rounds planted offline; assertions use the public status CLI."""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import unittest

from test_host_reservation_cli import HostHarness
from fleet import native
from hostgate import protocol


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class HostRounds(HostHarness):
    def setUp(self):
        super().setUp()
        code, job = self.submit(enqueue_only=True)
        self.assertEqual((code, job.get("accepted")), (0, True), job)
        self.job, self.attempt = job["job"], job["attempt"]
        op = "fleet:{}:1".format(self.attempt)
        identity = canonical(native.identity(os.getpid()))
        request = dict(schema=protocol.SCHEMA, operation="reservar", operationId=op,
                       jobId=self.job, attemptId=self.attempt, executorId="executor-test",
                       physicalHostId="host-test", profileId="fixture", exclusiveResources=[],
                       demand=dict(cpuMilli=0, memoryBytes=0, diskBytes={}, slots=1))
        self.request = request
        self.key = dict(storeId="store-test", reservationId="e_test", generation=1)
        self.grant = protocol.reply(op, "cfg_test", would_grant=True, mode="effective",
                                    store_id="store-test", state="reserved", reservationKey=self.key)
        why = protocol.reason("budget-exceeded", "fixture", True)
        self.denial = protocol.reply(op, "cfg_test", [why], exit_code=1, mode="effective",
                                     store_id="store-test")
        def replay(original, current):
            return dict(original, replayed=True, originalResult=original,
                        currentState=current, granted=False)
        self.shapes = {
            "prepared": ("prepared", None, None, None, None, None),
            "outcome_unknown": ("outcome_unknown", identity, None, None, None, None),
            "confirmed_reply": ("confirmed", identity, self.key, self.grant, "reply", None),
            "confirmed_replay": ("confirmed", identity, self.key,
                                 replay(self.grant, dict(state="reserved", reasons=[])), "same-channel-replay", None),
            "rejected": ("rejected", identity, None, self.denial, None, None),
            "held_key_receipt": ("held", identity, self.key, self.grant, "reply", "claim_unavailable"),
            "held_uncertain_replay": ("held", identity, self.key,
                                      replay(self.grant, dict(state="uncertain", reasons=[why])),
                                      "same-channel-replay", "replay_not_reserved"),
            "held_denial_replay": ("held", identity, None, replay(self.denial, None), None, "replay_not_reserved"),
            "held_without_receipt": ("held", identity, None, None, None, "supervisor_lost"),
            "retired": ("retired", None, None, None, None, None),
        }
        self.base = dict(attempt=self.attempt, ordinal=1, operation_id=op, request_json=canonical(request),
                         request_sha256=hashlib.sha256(canonical(request).encode()).hexdigest(),
                         physical_host_id="host-test", executor_id="executor-test", store_id="store-test",
                         config_revision="cfg_test", requester_identity=identity, heartbeat_seq=0)

    def row(self, name):
        state, supervisor, key, receipt, source, reason = self.shapes[name]
        return dict(self.base, state=state, supervisor_identity=supervisor,
                    key_json=None if key is None else canonical(key),
                    receipt_json=None if receipt is None else canonical(receipt),
                    confirmation_source=source, reason=reason)

    def second(self, name):
        """The same attempt's next round; only for shapes without a receipt."""
        op = "fleet:{}:2".format(self.attempt)
        request = canonical(dict(self.request, operationId=op))
        return dict(self.row(name), ordinal=2, operation_id=op, request_json=request,
                    request_sha256=hashlib.sha256(request.encode()).hexdigest())

    def plant(self, *rows):
        # enqueue-only has no Fleet owner. Close SQLite before starting any reader.
        conn = sqlite3.connect(str(self.state / "state.db"))
        try:
            with conn:
                conn.execute("DELETE FROM host_rounds")
                for row in rows:
                    conn.execute("INSERT INTO host_rounds ({}) VALUES ({})".format(
                        ",".join(row), ",".join("?" for _ in row)), tuple(row.values()))
        finally:
            conn.close()
        for path in self.state.glob("state.db*"):
            path.chmod(0o600)

    def read(self):
        code, result = self.call("status", self.job)
        self.assertEqual(code, 0, result)
        return result

    def valid(self, row, result=None):
        self.plant(row)
        result = self.read() if result is None else result()
        host = result["observation"]["reservation"]
        self.assertIsNone(host.get("error"), result)
        self.assertEqual((host.get("round") or {}).get("state"), row["state"],
                         "FACT: status must expose the planted round: " + repr(result))
        return result

    def invalid(self, result):
        observation = result["observation"]
        self.assertEqual(observation["reservation"].get("error"), "host_correlation_invalid", result)
        self.assertIsNone(observation["reservation"].get("round"), result)
        self.assertEqual((observation["view"], observation["completion"], observation["reasons"]),
                         ("uncertain", "incomplete", ["host_correlation_invalid"]), result)
        self.assertEqual(result.get("execution_error"), {"code": "host_correlation_invalid"}, result)

    def positive(self, name):
        row = self.row(name)
        result = self.valid(row)
        observation, host = result["observation"], result["observation"]["reservation"]
        key = json.loads(row["key_json"]) if row["key_json"] else {}
        self.assertEqual([host[k] for k in ("storeId", "reservationId", "generation", "physicalHostId", "executorId")],
                         ["store-test", key.get("reservationId"), key.get("generation"), "host-test", "executor-test"])
        self.assertEqual(host["round"], dict(ordinal=1, operation_id=row["operation_id"], state=row["state"],
                                            reason=row["reason"], supervisor_alive=row["supervisor_identity"] is not None))
        self.assertIn("host_round_" + row["state"], observation["dispatch"]["reasons"])
        if row["state"] == "held":
            self.assertEqual((observation["view"], observation["reasons"]), ("uncertain", [row["reason"]]))
            self.assertIn(row["reason"], observation["dispatch"]["reasons"])
        else:
            self.assertEqual(observation["view"], "queued", result)

    def negative(self, name, column, value):
        row = self.row(name)
        self.valid(row)  # The unaltered row must work in this same test.
        changed = value(self, row) if callable(value) else value
        self.plant(dict(row, **(changed if column is None else {column: changed})))
        self.invalid(self.read())

    def dead_owner(self, name):
        row = self.row(name)
        self.valid(row)
        process = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
        try:
            identity = native.identity(process.pid)
        finally:
            process.communicate(timeout=10)
        self.assertIsNotNone(identity["birth"], identity)
        result = self.valid(dict(row, supervisor_identity=canonical(identity)))
        observation = result["observation"]
        self.assertFalse(observation["reservation"]["round"]["supervisor_alive"])
        self.assertEqual((observation["view"], observation["completion"], observation["reasons"]),
                         ("uncertain", "incomplete", ["host_owner_unverified"]))
        self.assertEqual(result.get("execution_error"), {"code": "host_owner_unverified"})

    def test_no_round_preserves_queued_observation(self):
        observation = self.read()["observation"]
        self.assertEqual(observation["view"], "queued")
        self.assertEqual(observation["reservation"], dict(kind="host-reservation@1", round=None, error=None,
                         storeId=None, reservationId=None, generation=None, physicalHostId=None, executorId=None))

    def test_outcome_unknown_dead_supervisor(self):
        self.dead_owner("outcome_unknown")

    def test_confirmed_dead_supervisor(self):
        self.dead_owner("confirmed_reply")

    def test_invalid_open_round_behind_a_later_one(self):
        later = self.second("retired")
        self.plant(self.row("rejected"), later)
        host = self.read()["observation"]["reservation"]
        current = host.get("round") or {}
        self.assertEqual((host.get("error"), current.get("ordinal"), current.get("state")), (None, 2, "retired"))
        self.plant(self.row("held_key_receipt"), later)  # SQL admits it: one open round, at any ordinal.
        self.invalid(self.read())
        self.plant(later)  # Round 2 without its round 1.
        self.invalid(self.read())

    def test_sweep_one_column_with_explicit_oracle(self):
        started = time.monotonic()
        counts = dict(total=0, sqlite_rejects=0, decoder_rejects=0, legitimate=0)
        fast = os.environ.get("FLEET_ROUND_SWEEP") == "view"
        if fast:
            from fleet.query import view
        def read():
            return view(self.state, "status", job=self.job) if fast else self.read()
        for name in self.shapes:
            row = self.row(name)
            self.valid(row, read)
            for column, value in alterations(row):
                legitimate = oracle(name, column, value)
                with self.subTest(shape=name, column=column, value=value):
                    counts["total"] += 1
                    try:
                        self.plant(dict(row, **{column: value}))
                    except sqlite3.IntegrityError:
                        self.assertFalse(legitimate, "oracle-valid alteration rejected by SQLite")
                        counts["sqlite_rejects"] += 1
                        continue
                    result = read()
                    if legitimate:
                        self.assertIsNone(result["observation"]["reservation"].get("error"), result)
                        self.assertIsNotNone(result["observation"]["reservation"].get("round"), result)
                        counts["legitimate"] += 1
                    else:
                        self.invalid(result)
                        counts["decoder_rejects"] += 1
        print("SWEEP_SUMMARY", json.dumps(dict(counts, mode="view" if fast else "cli",
                                               seconds=round(time.monotonic() - started, 3))), flush=True)
        self.assertEqual(counts["total"], 880)


def alterations(row):
    for column in ("request_json", "requester_identity", "supervisor_identity", "key_json", "receipt_json"):
        values = [None, "null", "{}", "[]", "0", '\"\"', "true", "not json"]
        if row[column] is not None:
            values.append(json.dumps(json.loads(row[column]), indent=1))
        for value in values:
            if value != row[column]:
                yield column, value
    for column in ("operation_id", "request_sha256", "physical_host_id", "executor_id", "store_id",
                   "config_revision", "confirmation_source", "reason"):
        for value in (None, "", "x", "reply", "same-channel-replay"):
            if value != row[column]:
                yield column, value
    for state in ("prepared", "outcome_unknown", "confirmed", "held", "rejected", "retired"):
        if state != row["state"]:
            yield "state", state
    yield "ordinal", 2
    yield "heartbeat_seq", 7


def oracle(shape, column, value):
    """Independent allowlist: every other single-column alteration must retain doubt."""
    if column == "heartbeat_seq":
        return value == 7
    if column in ("store_id", "config_revision"):
        return shape in ("prepared", "outcome_unknown", "retired", "held_without_receipt") and value in (
            "x", "reply", "same-channel-replay")
    if column == "reason":
        return shape in ("retired", "held_key_receipt", "held_uncertain_replay", "held_denial_replay",
                         "held_without_receipt") and value in ("x", "reply", "same-channel-replay")
    if column == "state":
        return (shape, value) in (("prepared", "retired"), ("retired", "prepared"))
    return shape == "held_denial_replay" and column == "receipt_json" and value is None


def changed_receipt(**changes):
    return lambda case, row: canonical(dict(json.loads(row["receipt_json"]), **changes))


def rewritten_request(**changes):
    """The request and its hash change together, so only the attempt's job can contradict them."""
    def columns(case, row):
        request = canonical(dict(json.loads(row["request_json"]), **changes))
        return dict(request_json=request, request_sha256=hashlib.sha256(request.encode()).hexdigest())
    return columns


# Each table entry becomes a separately named CLI test with its own admission and positive control.
NEGATIVES = {
    "other_job": ("confirmed_reply", None, rewritten_request(jobId="j_" + "0" * 32)),
    "other_profile": ("confirmed_reply", None, rewritten_request(profileId="other")),
    "empty_receipt": ("confirmed_reply", "receipt_json", "{}"),
    "other_operation": ("confirmed_reply", "receipt_json", changed_receipt(operationId="other")),
    "other_revision": ("confirmed_reply", "receipt_json", changed_receipt(configRevision="other")),
    "other_key": ("confirmed_reply", "receipt_json", changed_receipt(
        reservationKey=dict(storeId="store-test", reservationId="e_other", generation=1))),
    "shadow_receipt": ("confirmed_reply", "receipt_json", lambda c, r: canonical(
        protocol.reply(r["operation_id"], "cfg_test"))),
    "rejected_grant": ("rejected", "receipt_json", lambda c, r: canonical(c.grant)),
    "null_confirmed_receipt": ("confirmed_reply", "receipt_json", "null"),
    "null_rejected_receipt": ("rejected", "receipt_json", "null"),
    "null_key": ("held_key_receipt", "key_json", "null"),
    "null_supervisor": ("outcome_unknown", "supervisor_identity", "null"),
    "empty_supervisor": ("outcome_unknown", "supervisor_identity", "{}"),
    "boolean_supervisor": ("outcome_unknown", "supervisor_identity", "true"),
    "noncanonical_json": ("confirmed_reply", "key_json", lambda c, r: json.dumps(c.key, indent=1)),
    "held_key_without_receipt": ("held_key_receipt", "receipt_json", None),
    "reply_with_replay_source": ("held_key_receipt", "confirmation_source", "same-channel-replay"),
    "replay_with_reply_source": ("confirmed_replay", "confirmation_source", "reply"),
}


def positive_test(shape):
    def test(self):
        self.positive(shape)
    return test


def negative_test(spec):
    def test(self):
        self.negative(*spec)
    return test


for _shape in ("prepared", "outcome_unknown", "confirmed_reply", "confirmed_replay", "rejected",
               "held_key_receipt", "held_uncertain_replay", "held_denial_replay", "held_without_receipt", "retired"):
    setattr(HostRounds, "test_valid_" + _shape, positive_test(_shape))
for _name, _spec in NEGATIVES.items():
    setattr(HostRounds, "test_invalid_" + _name, negative_test(_spec))
