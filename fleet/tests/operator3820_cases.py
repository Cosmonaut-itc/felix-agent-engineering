"""Public operator scenarios; genuine local producer proof, no injected closure API."""
import concurrent.futures
import json
import os
from pathlib import Path
import plistlib
import pwd
import select
import signal
import stat
import subprocess
import sys
import tempfile
import time


class Operator3820Cases:
    def op(self, action, operation_id, *options, reason="fixture capacity investigation", code=0, **kwargs):
        return self.raw("operator", action, "--operation-id", operation_id, "--reason", reason,
                        *options, code=code, **kwargs)

    def selectors(self, value, drain_id):
        row = value["observation"]
        reservation = row["reservation"]
        return (value["job"], "--attempt", value["attempt"], "--reservation", reservation["id"],
                "--generation", str(reservation["generation"]), "--drain-id", drain_id)

    def resolution(self, key="operator-job", state=None):
        if state is None:
            first, final, argv, counter = self.incomplete_job(key)
        else:
            argv, counter, ready, gate = self.counted()
            gate.touch()
            first = self.send(key, argv, options=("--artifact", "required/report.json"), state=state)
            final = self.terminal(key, state=state)
        drain = self.op("drain", key + "-drain", state=state)
        drain_id = drain["receipt"]["drain_id"]
        selectors = self.selectors(final, drain_id)
        assessed = self.op("assess", key + "-assess", *selectors, state=state)
        self.assertIs(assessed["receipt"]["release_eligible"], True)
        proof = assessed["receipt"]["proof_id"]
        return first, final, argv, counter, drain_id, selectors, proof

    def test_r2_a2_conflict_audits_entire_denied_request_without_rebinding(self):
        secret = "r2-persisted-fictitious-credential"
        self.send("r2-a2-secret", [sys.executable, "-c", "pass"],
                  env=dict(self.env, FICTITIOUS_SECRET=secret))
        self.terminal("r2-a2-secret")
        _, g, _, _, d1, g_selectors, g_proof = self.resolution("r2-a2-g")
        original = self.op("release", "r2-a2-bound", *g_selectors, "--proof", g_proof)
        original_status = self.raw("operator", "status", "--operation-id", "r2-a2-bound")
        self.op("reopen", "r2-a2-open", "--drain-id", d1)
        _, h, _, counter, d2, h_selectors, h_proof = self.resolution("r2-a2-h")
        for field in ("job", "attempt"):
            self.assertNotEqual(g[field], h[field])
        self.assertNotEqual(g_proof, h_proof)
        self.assertNotEqual(d1, d2)
        self.until(lambda: not self.process_present(h["observation"]["supervisor"]["pid"]))
        before = self.status_key("r2-a2-h")["observation"]
        reservation = before["reservation"]
        self.assertNotEqual(g["observation"]["reservation"]["id"], reservation["id"])
        self.assertGreater(reservation["generation"], g["observation"]["reservation"]["generation"])
        reason = "attributed denied request for the genuine second assignment"
        target = {"job": h["job"], "attempt": h["attempt"], "reservation": reservation["id"],
                  "generation": reservation["generation"]}
        stimuli = (("release", (*h_selectors, "--proof", h_proof), dict(target, proof=h_proof), "pilot_slot"),
                   ("assess", h_selectors, dict(target, proof=None), "pilot_slot"),
                   ("reopen", ("--drain-id", d2, "--backend", secret),
                    dict(job=None, attempt=None, reservation=None, generation=None, proof=None), "[REDACTED]"))
        last_revision = original["receipt"]["decision_revision"]
        for action, options, fields, backend in stimuli:
            with self.subTest(action=action):
                conflict = self.op(action, "r2-a2-bound", *options, reason=reason, code=5)
                self.assertEqual(conflict["error"], {"code": "operation_conflict", "effects": "no_capacity_change"})
                self.assertTrue(conflict["audit_recorded"])
                listing = self.raw("operator", "status", "--limit", "100")
                event = next(e for e in listing["audit_events"] if e["seq"] == conflict["audit_seq"])
                expected = dict(fields, action=action, operation_id="r2-a2-bound", reason=reason,
                                drain_id=d2, backend=backend)
                self.assertEqual(event["request"], expected)
                self.assertEqual(event["action"], action)
                self.assertEqual(event["decision"], "operation_conflict")
                self.assertEqual(event["operation_id"], "r2-a2-bound")
                self.assertEqual(event["reason"], reason)
                self.assertEqual(event["authority_id"], original["authority_id"])
                self.assertEqual(event["principal_uid"], os.getuid())
                self.assertEqual(event["actor"]["uid"], os.getuid())
                self.assertTrue(event["actor"]["birth"])
                self.assertTrue(event["at"].endswith("Z"))
                self.assertGreater(event["decision_revision"], last_revision)
                last_revision = event["decision_revision"]
                self.assertNotIn(secret, json.dumps(listing))
                current = self.status_key("r2-a2-h")["observation"]
                for name in ("reservation", "capacity_resolution", "transitions", "collection", "command", "control"):
                    self.assertEqual(current[name], before[name])
                saved = self.raw("operator", "status", "--operation-id", "r2-a2-bound")
                self.assertEqual(saved["receipt"], original_status["receipt"])
        # Known secret in reason is rejected/audited without a new binding. Its
        # full sanitized request is still inspectable using persisted declarations.
        denied = self.op("release", "r2-a2-secret-denied", *h_selectors, "--proof", h_proof,
                         reason=secret, code=2)
        events = self.raw("operator", "status", "--limit", "100")["audit_events"]
        event = next(e for e in events if e["seq"] == denied["audit_seq"])
        self.assertEqual(event["decision"], "credential_in_operator_request")
        self.assertEqual(event["request"], dict(target, action="release", operation_id="r2-a2-secret-denied",
                                               reason="[REDACTED]", drain_id=d2, proof=h_proof, backend="pilot_slot"))
        self.assertNotIn(secret, json.dumps(events))
        self.raw("operator", "status", "--operation-id", "r2-a2-secret-denied", code=2)
        replay = self.op("release", "r2-a2-bound", *g_selectors, "--proof", g_proof)
        self.assertEqual(replay["binding"], "replay")
        self.assertEqual(replay["receipt"], original["receipt"])
        self.assertEqual(self.status_key("r2-a2-h")["observation"]["reservation"]["disposition"], "retained")
        self.assertEqual(self.entries(counter), ["entry"])
        before_invalid = self.inventory(self.state)
        for backend in ("", "b" * 129, "line\nbreak"):
            with self.subTest(invalid_backend=backend):
                invalid = self.op("drain", "r2-a2-malformed", "--backend", backend, code=2)
                self.assertEqual(invalid["error"]["code"], "invalid_operator_backend")
                self.assertIs(invalid["audit_recorded"], False)
        self.assertEqual(self.inventory(self.state), before_invalid)

    def test_r2_a3_pages_exhaust_divergent_operations_and_audit(self):
        self.send("r2-a3-init", [sys.executable, "-c", "pass"])
        final = self.terminal("r2-a3-init")
        self.until(lambda: not self.process_present(final["observation"]["supervisor"]["pid"]))
        original = self.op("drain", "r2-a3-only-operation")
        conflicts = [self.op("drain", "r2-a3-only-operation", reason="conflict " + str(index), code=5)
                     for index in range(3)]
        before = self.inventory(self.state)
        expected = self.raw("operator", "status", "--limit", "100")
        self.assertEqual([e["seq"] for e in expected["audit_events"]],
                         [original["receipt"]["audit_seq"], *(r["audit_seq"] for r in conflicts)])
        operations, events, cursors = [], [], []
        after, after_audit = 0, 0
        for _ in range(5):
            page = self.raw("operator", "status", "--after-seq", str(after),
                            "--after-audit-seq", str(after_audit), "--limit", "2")
            for stream, offset in ((page["operations"], after), (page["audit_events"], after_audit)):
                self.assertLessEqual(len(stream), 2)
                self.assertTrue(all(r["seq"] > offset for r in stream))
                self.assertEqual([r["seq"] for r in stream], sorted(r["seq"] for r in stream))
            operations.extend(page["operations"])
            events.extend(page["audit_events"])
            cursor = (page["next_after_seq"], page["next_after_audit_seq"])
            self.assertEqual(cursor, (page["operations"][-1]["seq"] if page["operations"] else after,
                                      page["audit_events"][-1]["seq"] if page["audit_events"] else after_audit))
            if not page["has_more"]:
                break
            self.assertTrue(cursor[0] > after or cursor[1] > after_audit,
                            "both cursors cannot stall while has_more is true")
            self.assertNotIn(cursor, cursors)
            cursors.append(cursor)
            after, after_audit = cursor
        else:
            self.fail("pagination did not terminate")
        self.assertEqual(operations, expected["operations"])
        self.assertEqual(events, expected["audit_events"])
        self.assertEqual(len(operations), 1)
        self.assertEqual(len({e["seq"] for e in events}), 4)
        self.assertEqual(operations[0]["receipt"], original["receipt"])
        end = self.raw("operator", "status", "--after-seq", str(expected["next_after_seq"]),
                       "--after-audit-seq", str(expected["next_after_audit_seq"]), "--limit", "2")
        self.assertEqual((end["operations"], end["audit_events"], end["has_more"]), ([], [], False))
        self.assertEqual((end["next_after_seq"], end["next_after_audit_seq"]),
                         (expected["next_after_seq"], expected["next_after_audit_seq"]))
        exact = self.raw("operator", "status", "--operation-id", "r2-a3-only-operation")
        self.assertEqual(exact["receipt"], original["receipt"])
        self.assertNotIn("operations", exact)
        self.assertEqual(self.inventory(self.state), before)

    def r3_public_operator(self, *args, secrets, code=0, **kwargs):
        """Inspect both actual CLI channels before retaining any public response."""
        process = self.call("operator", *args, check=False, **kwargs)
        # A failing confidentiality oracle must not repeat the leaked bytes in
        # unittest's diagnostic or in the external public evidence.
        self.assertFalse(any(secret in process.stdout + process.stderr for secret in secrets),
                         "declared credential in public operator output")
        self.assertNotIn("Traceback", process.stderr)
        self.assertEqual(process.returncode, code, (process.stdout, process.stderr))
        value = json.loads(process.stdout)
        self.assertEqual(value["api"], "fleet-job-api@1")
        return value

    def test_r3_a01_new_denial_sanitizes_all_selectors_and_preserves_raw_binding(self):
        declared = {"FICTITIOUS_JOB_A": "j_" + "1" * 32, "FICTITIOUS_JOB_B": "j_" + "2" * 32,
                    "FICTITIOUS_ATTEMPT": "a_" + "3" * 32, "FICTITIOUS_RESERVATION": "r_" + "4" * 32,
                    "FICTITIOUS_DRAIN": "d_" + "5" * 32, "FICTITIOUS_PROOF": "p_" + "6" * 32}
        secrets = list(declared.values())
        credentials = tuple(part for name in declared for part in ("--credential-env", name))
        argv, counter, ready, gate = self.counted("r3-credential-holder")
        first = self.send("r3-a01-holder", argv, options=("--enqueue-only", *credentials),
                          env=dict(self.env, **declared))
        before = self.status_key("r3-a01-holder")["observation"]
        self.assertNotIn(first["job"], secrets)
        reason = "new attributable denial with declared selectors"

        def options(job):
            return (job, "--attempt", declared["FICTITIOUS_ATTEMPT"],
                    "--reservation", declared["FICTITIOUS_RESERVATION"], "--generation", "7",
                    "--drain-id", declared["FICTITIOUS_DRAIN"], "--proof", declared["FICTITIOUS_PROOF"])

        def public(*args, code=0):
            # This env does not carry the declarations/values used at admission.
            return self.r3_public_operator(*args, secrets=secrets, code=code, env=self.env)

        absent = public("status", "--operation-id", "r3-a01-new", code=2)
        self.assertEqual(absent["error"]["code"], "operation_not_found")
        request = ("release", "--operation-id", "r3-a01-new", "--reason", reason,
                   *options(declared["FICTITIOUS_JOB_A"]))
        denied = public(*request, code=2)
        self.assertEqual(denied["error"], {"code": "unknown_job", "effects": "no_capacity_change"})
        self.assertEqual((denied["binding"], denied["status"]), ("new", "committed"))
        receipt = denied["receipt"]
        target = {"job": "[REDACTED]", "attempt": "[REDACTED]", "reservation": "[REDACTED]", "generation": 7}
        self.assertEqual(receipt["target"], target)
        self.assertEqual((receipt["drain_id"], receipt["proof_id"]), ("[REDACTED]", "[REDACTED]"))
        self.assertEqual((receipt["decision"], receipt["action"], receipt["reason"]), ("unknown_job", "release", reason))
        self.assertIs(receipt["release_eligible"], False)
        self.assertIs(receipt["validation_processed"], True)
        self.assertIs(receipt["audit_recorded"], True)
        self.assertEqual(receipt["actor"]["uid"], os.getuid())
        self.assertTrue(receipt["actor"]["birth"])
        self.assertTrue(receipt["at"].endswith("Z"))
        self.assertIsNone(receipt["reservation_before"])
        self.assertIsNone(receipt["reservation_after"])
        exact = public("status", "--operation-id", "r3-a01-new")
        self.assertEqual(exact["receipt"], receipt)
        replay = public(*request, code=2)
        self.assertEqual(replay["binding"], "replay")
        self.assertEqual(replay["receipt"], receipt)
        # Both job values project to the same marker. They MUST remain different
        # private bindings, so the second raw request conflicts instead of replay.
        changed = public("release", "--operation-id", "r3-a01-new", "--reason", reason,
                         *options(declared["FICTITIOUS_JOB_B"]), code=5)
        self.assertEqual(changed["error"], {"code": "operation_conflict", "effects": "no_capacity_change"})
        self.assertIs(changed["audit_recorded"], True)
        for operation_id, why in (("r3-a01-secret-reason", declared["FICTITIOUS_JOB_A"]),
                                  (declared["FICTITIOUS_JOB_B"], "known credential in operation id")):
            with self.subTest(credential_rejection="reason" if operation_id.startswith("r3-") else "id"):
                rejected = public("release", "--operation-id", operation_id, "--reason", why,
                                  *options(declared["FICTITIOUS_JOB_A"]), code=2)
                self.assertEqual(rejected["error"], {"code": "credential_in_operator_request",
                                                     "effects": "no_capacity_change"})
                self.assertIs(rejected["audit_recorded"], True)
                unbound = public("status", "--operation-id", operation_id, code=2)
                self.assertEqual(unbound["error"]["code"], "operation_not_found")
        # Nonsecret, well-formed selectors keep their original decision/fields.
        control = public("assess", first["job"], "--attempt", first["attempt"],
                         "--reservation", "r_" + "0" * 32, "--generation", "1",
                         "--drain-id", "d_" + "0" * 32, "--operation-id", "r3-a01-control",
                         "--reason", "nonsecret selector control", code=5)
        self.assertEqual(control["error"]["code"], "generation_mismatch")
        self.assertEqual(control["receipt"]["target"]["job"], first["job"])
        self.assertEqual(control["receipt"]["target"]["generation"], 1)
        original = self.inventory(self.state)
        listing = public("status", "--limit", "100")
        self.assertEqual(len(listing["operations"]), 2)
        self.assertEqual(len(listing["audit_events"]), 5)
        event = next(e for e in listing["audit_events"] if e["seq"] == receipt["audit_seq"])
        for field in ("target", "drain_id", "proof_id", "decision", "action", "reason", "actor", "decision_revision"):
            self.assertEqual(event[field], receipt[field])
        conflict_event = next(e for e in listing["audit_events"] if e["seq"] == changed["audit_seq"])
        self.assertEqual(conflict_event["request"], dict(target, action="release", operation_id="r3-a01-new",
                                                        reason=reason, drain_id="[REDACTED]", proof="[REDACTED]",
                                                        backend="pilot_slot"))
        operations, events = [], []
        after, audit_after = 0, 0
        for _ in range(10):
            page = public("status", "--after-seq", str(after), "--after-audit-seq", str(audit_after), "--limit", "1")
            operations.extend(page["operations"])
            events.extend(page["audit_events"])
            cursor = (page["next_after_seq"], page["next_after_audit_seq"])
            if not page["has_more"]:
                break
            self.assertTrue(cursor[0] > after or cursor[1] > audit_after)
            after, audit_after = cursor
        else:
            self.fail("r3 public projection pagination did not terminate")
        self.assertEqual((operations, events), (listing["operations"], listing["audit_events"]))
        self.assertEqual(public("status", "--operation-id", "r3-a01-new")["receipt"], receipt)
        self.assertEqual(self.inventory(self.state), original, "all status projections must be read-only")
        self.assertEqual(public(*request, code=2)["receipt"], receipt)
        current = self.status_key("r3-a01-holder")["observation"]
        for name in ("state", "dispatch", "reservation", "capacity_resolution", "transitions", "command", "control"):
            self.assertEqual(current[name], before[name])
        self.assertEqual(self.entries(counter), [])
        self.assertFalse(ready.exists())
        self.assertFalse((self.state / "supervisor.log").exists())
        self.external_evidence.append({"case": "r3-A01-new", "response": denied, "replay": replay,
                                       "exact": exact, "operations": operations, "audit_events": events})

    def test_r3_a01_readers_resanitize_history_with_later_persisted_declarations(self):
        argv, counter, ready, gate = self.counted("r3-history-holder")
        self.send("r3-a01-history-init", argv, options=("--enqueue-only",))
        declared = {"FICTITIOUS_HISTORY_JOB": "j_" + "7" * 32, "FICTITIOUS_HISTORY_ATTEMPT": "a_" + "8" * 32,
                    "FICTITIOUS_HISTORY_RESERVATION": "r_" + "9" * 32,
                    "FICTITIOUS_HISTORY_DRAIN": "d_" + "a" * 32, "FICTITIOUS_HISTORY_PROOF": "p_" + "b" * 32}
        request = ("release", "--operation-id", "r3-a01-history", "--reason", "historical denied selectors",
                   declared["FICTITIOUS_HISTORY_JOB"], "--attempt", declared["FICTITIOUS_HISTORY_ATTEMPT"],
                   "--reservation", declared["FICTITIOUS_HISTORY_RESERVATION"], "--generation", "9",
                   "--drain-id", declared["FICTITIOUS_HISTORY_DRAIN"], "--proof", declared["FICTITIOUS_HISTORY_PROOF"])
        # These fictitious values are not yet credentials in this authority. This
        # authentic CLI producer stores history that later readers must sanitize;
        # no stored object, SQL, mock, old executable or private hook is injected.
        historical = self.raw("operator", *request, code=2)["receipt"]
        self.assertEqual(historical["decision"], "unknown_job")
        expected = dict(historical, target={"job": "[REDACTED]", "attempt": "[REDACTED]",
                                           "reservation": "[REDACTED]", "generation": 9},
                        drain_id="[REDACTED]", proof_id="[REDACTED]")
        credentials = tuple(part for name in declared for part in ("--credential-env", name))
        self.send("r3-a01-history-declare", argv, options=("--enqueue-only", *credentials),
                  env=dict(self.env, **declared))
        original = self.inventory(self.state)
        secrets = list(declared.values())
        exact = self.r3_public_operator("status", "--operation-id", "r3-a01-history", secrets=secrets)
        self.assertEqual(exact["receipt"], expected)
        page = self.r3_public_operator("status", "--after-seq", "0", "--after-audit-seq", "0", "--limit", "1",
                                      secrets=secrets)
        self.assertEqual(page["operations"][0]["receipt"], expected)
        self.assertEqual(page["audit_events"][0]["target"], expected["target"])
        self.assertEqual(page["audit_events"][0]["drain_id"], "[REDACTED]")
        self.assertEqual(page["audit_events"][0]["proof_id"], "[REDACTED]")
        self.assertIs(page["has_more"], False)
        end = self.r3_public_operator("status", "--after-seq", str(page["next_after_seq"]),
                                     "--after-audit-seq", str(page["next_after_audit_seq"]), "--limit", "1", secrets=secrets)
        self.assertEqual((end["operations"], end["audit_events"], end["has_more"]), ([], [], False))
        self.assertEqual(self.inventory(self.state), original, "reader must project, never rewrite stored history")
        replay = self.r3_public_operator(*request, secrets=secrets, code=2)
        self.assertEqual(replay["binding"], "replay")
        self.assertEqual(replay["receipt"], expected)
        after = self.r3_public_operator("status", secrets=secrets)
        self.assertEqual(after["operations"], page["operations"])
        self.assertEqual(after["audit_events"], page["audit_events"])
        self.assertEqual(after["as_of_revision"], exact["as_of_revision"])
        self.assertEqual(self.entries(counter), [])
        self.assertFalse(ready.exists())
        self.external_evidence.append({"case": "r3-A01-history", "exact": exact, "page": page, "replay": replay})

    def test_r3_a03_both_cursor_ranges_return_json_without_mutation(self):
        self.send("r3-a03-holder", [sys.executable, "-c", "pass"], options=("--enqueue-only",))
        bound = self.op("drain", "r3-a03-bound")
        self.op("drain", "r3-a03-bound", reason="extra audit-only event", code=5)
        original = self.inventory(self.state)
        maximum = 9223372036854775807
        for flag in ("--after-seq", "--after-audit-seq"):
            for cursor in (-1, maximum + 1, 18446744073709551615):
                for selection in ((), ("--operation-id", "r3-a03-bound")):
                    with self.subTest(flag=flag, invalid=cursor, selection=selection):
                        process = self.call("operator", "status", flag, str(cursor), *selection, check=False)
                        self.assertNotIn("Traceback", process.stdout + process.stderr)
                        self.assertEqual(process.returncode, 2)
                        self.assertEqual(len(process.stdout.splitlines()), 1)
                        value = json.loads(process.stdout)
                        self.assertEqual(value["api"], "fleet-job-api@1")
                        self.assertEqual(value["operation"], "operator.status")
                        self.assertEqual(value["error"], {"code": "invalid_pagination", "effects": "read_only"})
                        self.assertIs(value["audit_recorded"], False)
                        self.assertNotIn("observation", value)
                        self.assertNotIn("operations", value)
                        self.assertEqual(self.inventory(self.state), original)
            for cursor in (0, maximum - 1, maximum):
                with self.subTest(flag=flag, valid=cursor):
                    page = self.raw("operator", "status", flag, str(cursor), "--limit", "1")
                    exact = self.raw("operator", "status", flag, str(cursor), "--operation-id", "r3-a03-bound")
                    self.assertEqual(exact["receipt"], bound["receipt"])
                    self.assertNotIn("operations", exact)
                    if cursor == 0:
                        self.assertEqual(len(page["operations"]), 1)
                        self.assertEqual(len(page["audit_events"]), 1)
                        self.assertIs(page["has_more"], True)
                    else:
                        self.assertEqual(page["audit_events"], [])
                        self.assertEqual(page["next_after_audit_seq"], cursor)
                        self.assertIs(page["has_more"], False)
                        self.assertEqual(len(page["operations"]), 0 if flag == "--after-seq" else 1)
                        self.assertEqual(page["next_after_seq"], cursor if flag == "--after-seq" else 1)
                    self.assertEqual(self.inventory(self.state), original)
        # A maximum operation offset does not exhaust the independent audit stream.
        page = self.raw("operator", "status", "--after-seq", str(maximum), "--after-audit-seq", "0", "--limit", "1")
        self.assertEqual(page["operations"], [])
        self.assertEqual(page["next_after_seq"], maximum)
        self.assertEqual([e["seq"] for e in page["audit_events"]], [1])
        self.assertIs(page["has_more"], True)
        last = self.raw("operator", "status", "--after-seq", str(page["next_after_seq"]),
                        "--after-audit-seq", str(page["next_after_audit_seq"]), "--limit", "1")
        self.assertEqual([e["seq"] for e in last["audit_events"]], [2])
        self.assertIs(last["has_more"], False)
        self.assertEqual(self.inventory(self.state), original)

    def r4_operator_pages(self, public):
        expected = public("status", "--limit", "100")
        operations, events = [], []
        after, audit_after = 0, 0
        for _ in range(len(expected["operations"]) + len(expected["audit_events"]) + 1):
            page = public("status", "--after-seq", str(after), "--after-audit-seq", str(audit_after), "--limit", "1")
            for field in ("authority_id", "principal_uid", "as_of_revision", "control"):
                self.assertEqual(page[field], expected[field])
            for stream, offset in ((page["operations"], after), (page["audit_events"], audit_after)):
                self.assertLessEqual(len(stream), 1)
                self.assertTrue(all(row["seq"] > offset for row in stream))
            operations.extend(page["operations"])
            events.extend(page["audit_events"])
            cursor = (page["next_after_seq"], page["next_after_audit_seq"])
            self.assertEqual(cursor, (page["operations"][-1]["seq"] if page["operations"] else after,
                                      page["audit_events"][-1]["seq"] if page["audit_events"] else audit_after))
            if not page["has_more"]:
                break
            self.assertTrue(cursor[0] > after or cursor[1] > audit_after)
            after, audit_after = cursor
        else:
            self.fail("r4 operator pagination did not terminate")
        self.assertEqual((operations, events), (expected["operations"], expected["audit_events"]))
        self.assertEqual(cursor, (expected["next_after_seq"], expected["next_after_audit_seq"]))
        end = public("status", "--after-seq", str(cursor[0]), "--after-audit-seq", str(cursor[1]), "--limit", "1")
        self.assertEqual((end["operations"], end["audit_events"], end["has_more"]), ([], [], False))
        self.assertEqual((end["next_after_seq"], end["next_after_audit_seq"]), cursor)
        return expected

    def r4_replay_boundary(self, public, request, keys, state, env, code=0):
        """Pure readers keep the full image; writer replay keeps durable facts."""
        def jobs():
            result = []
            for key in keys:
                value = self.status_key(key, state=state, env=env)
                value["observation"] = {name: item for name, item in value["observation"].items()
                                        if name != "observed_at"}
                result.append(value)
            return result

        saved = self.inventory(state)
        before = self.r4_operator_pages(public)
        before_jobs = jobs()
        self.assertEqual(self.inventory(state), saved, "pre-replay readers changed the full source image")
        replay = public(*request, code=code)
        # A writer may touch SQLite shared memory. Do not infer logical change
        # from it, or remove sidecars from inventory() for any reader.
        saved = self.inventory(state)
        after = self.r4_operator_pages(public)
        after_jobs = jobs()
        self.assertEqual(self.inventory(state), saved, "post-replay readers changed the full source image")
        for field in ("api", "operation", "authority_id", "principal_uid", "as_of_revision", "control",
                      "operations", "audit_events", "next_after_seq", "next_after_audit_seq", "has_more"):
            self.assertEqual(after[field], before[field], "replay changed durable " + field)
        self.assertEqual(after_jobs, before_jobs, "replay changed job identity/history/capacity/dispatch")
        self.external_evidence.append({"case": "r4-replay-boundary", "before": before, "after": after,
                                       "jobs_before": before_jobs, "jobs_after": after_jobs})
        return replay

    def test_r4_a01_denial_literals_and_copied_data_before_and_after_declaration(self):
        from datetime import datetime

        self.assertEqual(list(self.project.iterdir()), [])
        self.assertFalse(any((p / ".git").exists() for p in (self.project, *self.project.parents)))
        declared = {"R4_API": "fleet-job-api@1", "R4_CODE": "unknown_job", "R4_EFFECT": "no_capacity_change",
                    "R4_JOB_A": "j_" + "1" * 32, "R4_JOB_B": "j_" + "2" * 32,
                    "R4_ATTEMPT": "a_" + "3" * 32, "R4_RESERVATION": "r_" + "4" * 32,
                    "R4_DRAIN": "d_" + "5" * 32, "R4_PROOF": "p_" + "6" * 32}
        selectors = (declared["R4_JOB_A"], "--attempt", declared["R4_ATTEMPT"],
                     "--reservation", declared["R4_RESERVATION"], "--generation", "7",
                     "--drain-id", declared["R4_DRAIN"], "--proof", declared["R4_PROOF"])
        credentials = tuple(part for name in declared for part in ("--credential-env", name))
        secret_selectors = [value for name, value in declared.items() if name not in ("R4_API", "R4_CODE", "R4_EFFECT")]
        reader_env = {name: value for name, value in self.env.items() if name not in declared}
        for timing in ("before", "after"):
            with self.subTest(declared=timing):
                state = self.root / ("r4-" + timing)
                argv, counter, ready, gate = self.counted("r4-holder")
                self.send("r4-holder", argv, state=state, options=("--enqueue-only", *credentials)
                          if timing == "before" else ("--enqueue-only",),
                          env=dict(reader_env, **declared) if timing == "before" else reader_env)
                before = self.status_key("r4-holder", state=state, env=reader_env)["observation"]

                def public(*args, code=0):
                    # Only copied selector secrets have a global absence oracle.
                    # Protocol-colliding credentials are checked by field below.
                    return self.r3_public_operator(*args, secrets=secret_selectors, code=code, state=state, env=reader_env)

                producer = public if timing == "before" else lambda *a, **kw: self.raw("operator", *a, state=state, env=reader_env, **kw)
                request = ("release", "--operation-id", "r4-bound", "--reason", "fixture audit", *selectors)
                denied = producer(*request, code=2)
                changed = producer("release", "--operation-id", "r4-bound", "--reason", "fixture audit",
                                   declared["R4_JOB_B"], *selectors[1:], "--backend", declared["R4_API"], code=5)
                recorded_event_at = next(e for e in producer("status", "--limit", "100")["audit_events"]
                                         if e["seq"] == denied["receipt"]["audit_seq"])["at"]
                if timing == "after":
                    # Genuine earlier ledger history, never seeded or rewritten.
                    self.assertEqual(denied["receipt"]["target"]["job"], declared["R4_JOB_A"])
                    self.send("r4-secrets", argv, state=state, options=("--enqueue-only", *credentials),
                              env=dict(reader_env, **declared))
                saved = self.inventory(state)
                exact = public("status", "--operation-id", "r4-bound")
                self.r4_operator_pages(public)
                self.assertEqual(self.inventory(state), saved, "reading later declarations must not rewrite history")
                keys = ("r4-holder", "r4-secrets") if timing == "after" else ("r4-holder",)
                replay = self.r4_replay_boundary(public, request, keys, state, reader_env, code=2)
                saved = self.inventory(state)
                receipt = exact["receipt"]
                self.assertEqual(receipt["at"], denied["receipt"]["at"])
                self.assertEqual((exact["status"], replay["status"], replay["binding"]), ("committed", "committed", "replay"))
                self.assertEqual(replay["error"], {"code": "unknown_job", "effects": "no_capacity_change"})
                self.assertEqual(replay["receipt"], receipt)
                if timing == "before":
                    self.assertEqual((denied["binding"], denied["status"]), ("new", "committed"))
                    self.assertEqual(denied["error"], replay["error"])
                    self.assertEqual(denied["receipt"], receipt)
                target = {"job": "[REDACTED]", "attempt": "[REDACTED]", "reservation": "[REDACTED]", "generation": 7}
                self.assertEqual(receipt["target"], target)
                self.assertEqual((receipt["drain_id"], receipt["proof_id"]), ("[REDACTED]", "[REDACTED]"))
                self.assertEqual((receipt["decision"], receipt["action"], receipt["reason"]), ("unknown_job", "release", "fixture audit"))
                self.assertIs(receipt["validation_processed"], True)
                self.assertIs(receipt["release_eligible"], False)
                self.assertIs(receipt["audit_recorded"], True)
                self.assertEqual(receipt["actor"]["uid"], os.getuid())
                listing = self.r4_operator_pages(public)
                event = next(e for e in listing["audit_events"] if e["seq"] == receipt["audit_seq"])
                for field in ("target", "decision", "action", "actor", "reason", "decision_revision"):
                    self.assertEqual(event[field], receipt[field])
                # These are two independent now() captures. Validate each value
                # in this fixture and preserve its own history, never compare clocks.
                for timestamp in (event["at"], receipt["at"]):
                    self.assertIsInstance(timestamp, str)
                    parsed = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ")
                    self.assertEqual(parsed.isoformat(timespec="microseconds") + "Z", timestamp)
                self.assertEqual(event["at"], recorded_event_at)
                conflict = next(e for e in listing["audit_events"] if e["seq"] == changed["audit_seq"])
                self.assertEqual(conflict["decision"], "operation_conflict")
                self.assertEqual(conflict["request"], dict(target, action="release", operation_id="r4-bound",
                                                         reason="fixture audit", drain_id="[REDACTED]",
                                                         proof="[REDACTED]", backend="[REDACTED]"))
                self.assertEqual(listing["operations"][0]["receipt"], receipt)
                self.assertEqual(self.inventory(state), saved, "reading later declarations must not rewrite history")
                # Change ONLY a secret selector: equal public redaction is not replay.
                conflict = public("release", "--operation-id", "r4-bound", "--reason", "fixture audit",
                                  declared["R4_JOB_B"], *selectors[1:], code=5)
                self.assertEqual(conflict["error"], {"code": "operation_conflict", "effects": "no_capacity_change"})
                rejected = public("release", "--operation-id", "unknown_job", "--reason", "unknown_job",
                                  *selectors, code=2)
                self.assertEqual(rejected["error"], {"code": "credential_in_operator_request", "effects": "no_capacity_change"})
                later = self.r4_operator_pages(public)
                self.assertEqual(next(e for e in later["audit_events"]
                                      if e["seq"] == receipt["audit_seq"])["at"], recorded_event_at)
                self.assertEqual(later["operations"][0]["receipt"], receipt)
                event = next(e for e in later["audit_events"] if e["seq"] == rejected["audit_seq"])
                self.assertEqual((event["operation_id"], event["reason"], event["request"]["operation_id"],
                                  event["request"]["reason"]), ("[REDACTED]",) * 4)
                self.assertEqual(public("status", "--operation-id", "unknown_job", code=2)["error"]["code"], "operation_not_found")
                self.assertEqual(public("status", "--operation-id", "r4-bound")["receipt"], receipt)
                current = self.status_key("r4-holder", state=state, env=reader_env)["observation"]
                for field in ("state", "reservation", "capacity_resolution", "transitions", "command", "control"):
                    self.assertEqual(current[field], before[field])
                self.assertEqual(self.entries(counter), [])
                self.assertFalse(ready.exists())
                self.assertFalse((state / "supervisor.log").exists())
                self.external_evidence.append({"case": "r4-denial-" + timing, "exact": exact, "replay": replay,
                                               "listing": listing})

    def test_r4_a01_effective_control_preserves_committed_and_historical_replay(self):
        self.assertEqual(list(self.project.iterdir()), [])
        self.assertFalse(any((p / ".git").exists() for p in (self.project, *self.project.parents)))
        argv, counter, ready, gate = self.counted("r4-control")
        declared = {"R4_STATUS": "committed", "R4_API": "fleet-job-api@1"}
        reader_env = {name: value for name, value in self.env.items() if name not in declared}
        self.send("r4-control-holder", argv, options=("--enqueue-only", "--credential-env", "R4_STATUS",
                                                     "--credential-env", "R4_API"), env=dict(reader_env, **declared))

        def public(*args, code=0):
            return self.r3_public_operator(*args, secrets=(), code=code, env=reader_env)

        request = ("drain", "--operation-id", "r4-control", "--reason", "fixture audit")
        drained = public(*request)
        receipt = drained["receipt"]
        self.assertEqual((drained["binding"], drained["status"], receipt["decision"], receipt["action"]),
                         ("new", "committed", "committed", "drain"))
        self.assertIs(drained["control"]["drained"], True)
        self.assertIs(receipt["target"], None)
        self.assertIs(receipt["audit_recorded"], True)
        drain_id = receipt["drain_id"]
        opened = public("reopen", "--operation-id", "r4-open", "--reason", "fixture audit", "--drain-id", drain_id)
        self.assertEqual((opened["status"], opened["receipt"]["decision"]), ("committed", "committed"))
        self.assertIs(opened["control"]["drained"], False)
        saved = self.inventory(self.state)
        self.r4_operator_pages(public)
        public("status", "--operation-id", "r4-control")
        self.assertEqual(self.inventory(self.state), saved, "historical readers must not rewrite history")
        replay = self.r4_replay_boundary(public, request, ("r4-control-holder",), self.state, reader_env)
        saved = self.inventory(self.state)
        exact = public("status", "--operation-id", "r4-control")
        for value in (replay, exact):
            self.assertEqual(value["status"], "committed")
            self.assertEqual(value["receipt"], receipt)
            self.assertEqual(value["control"], opened["control"])
        self.assertEqual(replay["binding"], "replay")
        listing = self.r4_operator_pages(public)
        self.assertEqual([r["status"] for r in listing["operations"]], ["committed", "committed"])
        self.assertEqual([e["decision"] for e in listing["audit_events"]], ["committed", "committed"])
        self.assertEqual(listing["operations"][0]["receipt"], receipt)
        self.assertEqual(self.inventory(self.state), saved, "post-replay historical readers must preserve the full image")
        rejected = public("drain", "--operation-id", "r4-secret", "--reason", "committed", code=2)
        self.assertEqual(rejected["error"], {"code": "credential_in_operator_request", "effects": "no_capacity_change"})
        event = next(e for e in self.r4_operator_pages(public)["audit_events"] if e["seq"] == rejected["audit_seq"])
        self.assertEqual((event["reason"], event["request"]["reason"]), ("[REDACTED]", "[REDACTED]"))
        self.assertIs(public("status")["control"]["drained"], False)
        pending = self.status_key("r4-control-holder", env=reader_env)["observation"]
        self.assertEqual(pending["state"], "queued")
        self.assertIsNone(pending["reservation"])
        self.assertEqual(self.entries(counter), [])
        self.assertFalse(ready.exists())
        self.assertFalse((self.state / "supervisor.log").exists())
        self.external_evidence.append({"case": "r4-effective-control", "drain": drained, "open": opened,
                                       "replay": replay, "exact": exact, "listing": listing})

    def test_o01_positive_local_resolution_keeps_failed_evidence_and_drained_fifo(self):
        first, final, argv, counter = self.incomplete_job("o01")
        next_argv, next_counter, ready, gate = self.counted("next")
        self.send("o01-next", next_argv)
        self.raw("recover", code=3)
        self.assertFalse(ready.exists())
        drain = self.op("drain", "o01-drain")["receipt"]["drain_id"]
        selectors = self.selectors(final, drain)
        assessed = self.op("assess", "o01-assess", *selectors)
        self.assertTrue(assessed["receipt"]["release_eligible"])
        proof = assessed["receipt"]["proof_id"]
        release = self.op("release", "o01-release", *selectors, "--proof", proof)
        self.assertEqual(release["receipt"]["reservation_before"], "retained")
        self.assertEqual(release["receipt"]["reservation_after"], "released")
        self.assertIs(release["receipt"]["audit_recorded"], True)
        self.assertEqual(release["receipt"]["actor"]["uid"], os.getuid())
        current = self.status_key("o01")["observation"]
        self.assertEqual(current["collection"], final["observation"]["collection"])
        self.assertEqual(current["recorded_outcome"], "incomplete")
        self.assertEqual(current["capacity_resolution"]["operation_id"], "o01-release")
        self.assertTrue(current["control"]["drained"])
        self.wait_key("o01", 3)
        self.raw("recover")
        self.assertFalse(ready.exists())
        self.same_receipt(first, self.send("o01", argv, options=("--artifact", "required/report.json")))
        self.op("reopen", "o01-reopen", "--drain-id", drain)
        self.assertFalse(ready.exists(), "reopen itself must not dispatch")
        self.raw("recover")
        self.ready("o01-next", ready)
        gate.touch()
        self.terminal("o01-next")
        self.assertEqual(self.entries(counter), ["entry"])
        self.assertEqual(self.entries(next_counter), ["next"])
        events = self.raw("operator", "status")["audit_events"]
        self.assertEqual(sum(e["decision"] == "committed" and e["action"] == "release" for e in events), 1)

    def test_o02_empty_group_without_original_terminal_and_self_assertions_cannot_release(self):
        first, argv, counter = self.lost_owner("o02")
        next_argv, next_counter, next_ready, next_gate = self.counted("blocked")
        self.send("o02-next", next_argv)
        current = self.status_key("o02")
        drain = self.op("drain", "o02-drain")["receipt"]["drain_id"]
        selectors = self.selectors(current, drain)
        assessed = self.op("assess", "o02-assess", *selectors, code=3)
        self.assertEqual(assessed["error"]["code"], "proof_insufficient")
        self.assertIs(assessed["receipt"]["release_eligible"], False)
        forged = "p_" + "f" * 32
        rejected = self.op("release", "o02-forged", *selectors, "--proof", forged, code=2)
        self.assertEqual(rejected["error"]["code"], "proof_invalid")
        asserted = self.root / "asserted-closure.json"
        asserted.write_text(json.dumps({"closed": True, "contained": True, "pid": current["observation"]["child_pid"],
                                        "ttl": 0, "done": True}))
        self.raw("operator", "release", "--operation-id", "o02-json", "--reason", "asserted closure",
                 *selectors, "--proof-file", str(asserted), code=2)
        self.op("release", "o02-negative-proof", *selectors, "--proof", assessed["receipt"]["proof_id"], code=3)
        self.assertEqual(self.status_key("o02")["observation"]["reservation"]["disposition"], "retained")
        self.wait_key("o02", 3)
        self.wait_key("o02-next", 3)
        self.assertFalse(next_ready.exists())
        self.assertEqual(self.entries(next_counter), [])
        self.assertEqual(self.entries(counter), ["entry"])

    def test_o03_reason_and_declared_actor_rejections_with_real_os_actor(self):
        self.send("o03-init", [sys.executable, "-c", "pass"])
        self.terminal("o03-init")
        for index, reason in enumerate(("", "   ", "line\nbreak", "x" * 513)):
            self.op("drain", "o03-invalid-" + str(index), reason=reason, code=2)
        self.raw("operator", "drain", "--operation-id", "o03-missing", code=2)
        for flag in ("--actor", "--uid"):
            self.raw("operator", "drain", "--operation-id", "o03-fake", "--reason", "fixture", flag, "0", code=2)
        actual = self.op("drain", "o03-valid", env=dict(self.env, USER="fictitious-other-account"))
        self.assertEqual(actual["receipt"]["actor"]["uid"], os.getuid())
        self.assertNotEqual(actual["receipt"]["actor"]["account"], "fictitious-other-account")
        self.assertTrue(actual["receipt"]["actor"]["birth"])
        # Real other-uid and denied native observer require preexisting external
        # executors. Labels/env are deliberately not counted as those conditions.

    def test_o04_new_old_generation_release_is_processed_and_rejected_before_proof(self):
        first, final, argv, counter, drain, selectors, proof = self.resolution("o04-g")
        released = self.op("release", "o04-g-release", *selectors, "--proof", proof)
        self.op("reopen", "o04-open", "--drain-id", drain)
        h_argv, h_counter, h_ready, h_gate = self.counted("H")
        h = self.send("o04-h", h_argv)
        self.ready("o04-h", h_ready)
        c_argv, c_counter, c_ready, c_gate = self.counted("C")
        self.send("o04-c", c_argv)
        d2 = self.op("drain", "o04-drain-2")["receipt"]["drain_id"]
        new_selectors = self.selectors(final, d2)
        rejected = self.op("release", "o04-stale-new", *new_selectors, "--proof", proof, code=5)
        self.assertEqual(rejected["error"]["code"], "generation_mismatch")
        self.assertIs(rejected["receipt"]["validation_processed"], True)
        replay = self.op("release", "o04-g-release", *selectors, "--proof", proof)
        self.assertEqual(replay["receipt"], released["receipt"])
        self.assertEqual(replay["binding"], "replay")
        self.assertTrue(replay["control"]["drained"])
        self.assertEqual(self.status_key("o04-h")["observation"]["reservation"]["disposition"], "retained")
        self.assertEqual(self.entries(h_counter), ["H"])
        self.assertFalse(c_ready.exists())
        h_gate.touch()
        self.terminal("o04-h")
        self.op("reopen", "o04-open-2", "--drain-id", d2)
        self.raw("recover")
        self.ready("o04-c", c_ready)
        c_gate.touch()
        self.terminal("o04-c")

    def test_o05_operation_binding_replay_conflict_and_concurrent_release(self):
        first, final, argv, counter, drain, selectors, proof = self.resolution("o05")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.op("release", "o05-release", *selectors, "--proof", proof), range(2)))
        self.assertEqual(sorted(r["binding"] for r in responses), ["new", "replay"])
        self.assertEqual(responses[0]["receipt"], responses[1]["receipt"])
        conflict = self.op("release", "o05-release", *selectors, "--proof", proof, reason="different reason", code=5)
        self.assertEqual(conflict["error"]["code"], "operation_conflict")
        self.assertIs(conflict["audit_recorded"], True)
        self.op("reopen", "o05-open", "--drain-id", drain)
        _, second, _, _, d2, selectors2, proof2 = self.resolution("o05-second")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda opid: self.op("release", opid, *selectors2, "--proof", proof2),
                                     ("o05-distinct-a", "o05-distinct-b")))
        self.assertEqual(sorted(r["receipt"]["decision"] for r in outcomes), ["already_released", "committed"])
        self.assertEqual(self.status_key("o05-second")["observation"]["reservation"]["disposition"], "released")

    def quota_base_inventory(self, base, result=None):
        # Immediate metadata only. Never descend into somebody else's history.
        result = {} if result is None else result
        with os.scandir(base) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                result[entry.name] = {name: getattr(info, name) for name in (
                    "st_dev", "st_ino", "st_uid", "st_gid", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")}
        return result

    def quota_error(self, exc):
        detail = {"type": type(exc).__name__}
        try:
            detail["message"] = str(exc)
            for name in ("errno", "filename", "filename2", "returncode", "stdout", "stderr"):
                value = getattr(exc, name, None)
                if isinstance(value, bytes):
                    value = value.decode(errors="replace")
                if value is not None and isinstance(value, (str, int)):
                    detail[name] = value
        except BaseException as diagnostic:
            detail["diagnostic_error"] = type(diagnostic).__name__
        return detail

    def quota_record(self, label, event, attempts):
        # Memory/snapshots do not certify a failed journal write or fsync.
        persistence = {"status": "unconfirmed", "phase": "open", "errors": []}
        attempts.append(persistence)
        self.external_evidence.append(dict(case=label, **event, persistence=persistence))
        stream = descriptor = None
        failure = None
        try:
            stream = (self.root / (label + "-quota.jsonl")).open("a")
            persistence["phase"] = "write"
            stream.write(json.dumps(event, sort_keys=True) + "\n")
            persistence["phase"] = "flush"
            stream.flush()
            persistence["phase"] = "file-fsync"
            os.fsync(stream.fileno())
            persistence["phase"] = "directory-open"
            descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            persistence["phase"] = "directory-fsync"
            os.fsync(descriptor)
        except BaseException as exc:
            failure = (exc, exc.__traceback__)
            persistence["errors"].append(dict(phase=persistence["phase"], **self.quota_error(exc)))
        finally:
            for stage, resource in (("stream-close", stream), ("directory-close", descriptor)):
                if resource is None:
                    continue
                try:
                    resource.close() if resource is stream else os.close(resource)
                except BaseException as exc:
                    persistence["errors"].append(dict(phase=stage, **self.quota_error(exc)))
                    if failure is None:
                        failure = (exc, exc.__traceback__)
        if failure is not None:
            raise failure[0].with_traceback(failure[1])
        persistence["status"] = "synced"
        persistence["phase"] = "complete"

    def quota_owned_inventory(self, root, result=None):
        # Unlike os.walk's default error handling, scandir failures propagate.
        # Only this exact newly attributed root is traversed; no symlink follows.
        result = {} if result is None else result

        def visit(directory):
            with os.scandir(directory) as entries:
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    result[str(Path(entry.path).relative_to(root))] = {
                        name: getattr(info, name) for name in (
                            "st_dev", "st_ino", "st_uid", "st_gid", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")}
                    if stat.S_ISDIR(info.st_mode):
                        visit(Path(entry.path))

        visit(root)
        return result

    def quota_case(self, label, operator_phase=False):
        """Certified setup only until PAO approves a causal discriminator.

        No amount of fill, rc3 or operation_not_found proves that a capacity
        transaction was reached. C20 also needs its own terminal acknowledgement.
        Do not run the old ambiguous experiment and report it as coverage.
        """
        self.retain_administrative_root = True
        target = operator_phase or "terminal-publication"
        physical = Path("/Volumes/pao-cs/fleet-tests")
        quota_root = base_identity = owned_identity = None
        base_verified = False
        failures = []
        report = {"event": "setup-final", "phase": "initial-record", "target_phase": target,
                  "status": "BLOCKED", "target_boundary_proven": False,
                  "administrative_root": str(self.root), "administrative_retention_requested": True,
                  "root": None, "root_attributed": False, "primary_error": None, "secondary_errors": [],
                  "fill_created": False, "fill_removed": False, "configuration": {},
                  "certificate_queries": [], "journal_attempts": [],
                  "inventories": {name: {"status": "not_performed", "reason": "setup has not reached this inventory"}
                                  for name in ("base_before", "own_before", "base_after", "own_after")},
                  "foreign_comparison": {"status": "not_performed", "reason": "complete baseline unavailable"}}

        def remember(stage, exc):
            failures.append((exc, exc.__traceback__))
            detail = dict(phase=stage, **self.quota_error(exc))
            if report["primary_error"] is None:
                report["primary_error"] = detail
            else:
                report["secondary_errors"].append(detail)

        def metadata(root):
            info = root.lstat()
            return {name: getattr(info, name) for name in (
                "st_dev", "st_ino", "st_uid", "st_gid", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")}

        def check_base():
            self.assertEqual(physical, physical.resolve(strict=True), "BLOCKED: quota base changed")
            current = metadata(physical)
            for name in ("st_dev", "st_ino", "st_uid", "st_mode"):
                self.assertEqual(current[name], base_identity[name], "BLOCKED: quota base identity changed: " + name)

        def inventory(name, own=False):
            observed = {"status": "incomplete", "entries": {}}
            report["inventories"][name] = observed
            try:
                check_base()
                if own:
                    current = metadata(quota_root)
                    observed["root_metadata"] = current
                    for field in ("st_dev", "st_ino", "st_uid", "st_mode"):
                        self.assertEqual(current[field], owned_identity[field],
                                         "BLOCKED: owned quota root changed: " + field)
                    self.assertEqual(quota_root, quota_root.resolve(strict=True))
                    self.quota_owned_inventory(quota_root, observed["entries"])
                else:
                    self.quota_base_inventory(physical, observed["entries"])
            except BaseException as exc:
                observed["error"] = self.quota_error(exc)
                raise
            observed["status"] = "complete"
            return observed["entries"]

        def diskutil(*args):
            query = {"argv": ["/usr/sbin/diskutil", *args], "returncode": None}
            report["certificate_queries"].append(query)
            try:
                response = subprocess.run(query["argv"], capture_output=True, timeout=15)
            except BaseException as exc:
                query["error"] = self.quota_error(exc)
                raise
            query.update(returncode=response.returncode, stderr=response.stderr.decode(errors="replace"))
            if response.returncode != 0:
                query["stdout"] = response.stdout.decode(errors="replace")
            self.assertEqual(response.returncode, 0, "BLOCKED: disk certificate query failed: " + repr(query))
            return response.stdout

        try:
            self.quota_record(label, {"phase": report["phase"], "target_phase": target,
                                      "status": "BLOCKED", "target_boundary_proven": False}, report["journal_attempts"])
            report["phase"] = "configuration-base"
            report["configuration"]["base"] = os.environ.get("FLEET_TEST_QUOTA_BASE")
            self.assertEqual(report["configuration"]["base"], str(physical),
                             "BLOCKED C20/O06: exact authorized pao-cs base required; no fallback")
            report["phase"] = "configuration-isolation"
            report["configuration"]["isolated"] = os.environ.get("FLEET_TEST_ISOLATED_QUOTA")
            self.assertEqual(report["configuration"]["isolated"], "1",
                             "BLOCKED C20/O06: exclusive future quota turn required")
            report["phase"] = "configuration-fill-bound"
            limit_text = os.environ.get("FLEET_TEST_QUOTA_FILL_LIMIT", "")
            report["configuration"]["fill_limit"] = limit_text
            self.assertTrue(limit_text.isascii() and limit_text.isdecimal(), "BLOCKED: explicit fill bound required")
            limit = int(limit_text)
            report["fill_limit"] = limit
            self.assertTrue(0 < limit <= 536870912, "BLOCKED: fill bound must be at most 512 MiB")
            report["phase"] = "base-identification"
            self.assertEqual(physical, physical.resolve(strict=True), "BLOCKED: physical quota base required")
            base_identity = metadata(physical)
            report["base_identity"] = base_identity
            self.assertTrue(stat.S_ISDIR(base_identity["st_mode"]))
            self.assertEqual(base_identity["st_uid"], os.getuid())
            self.assertEqual(pwd.getpwuid(base_identity["st_uid"]).pw_name, "cosmonaut")
            self.assertEqual(stat.S_IMODE(base_identity["st_mode"]), 0o700)
            self.assertEqual(base_identity["st_dev"], physical.parent.stat().st_dev)
            self.assertNotEqual(self.root.stat().st_dev, base_identity["st_dev"],
                                "BLOCKED: administrative run evidence must be outside the quota volume")
            base_verified = True
            report["phase"] = "certificate-info"
            fields = [line.partition(":") for line in diskutil("info", str(physical.parent)).decode().splitlines()]
            certificate = {name.strip(): value.strip() for name, separator, value in fields if separator}
            report["certificate"] = {name: certificate.get(name) for name in (
                "Device Identifier", "Volume UUID", "File System Personality", "Mount Point", "Owners")}
            for name, expected in (("Device Identifier", "disk3s7"),
                                   ("Volume UUID", "F4FF2E7E-751F-48B3-BA0D-F5F2656C23BC"),
                                   ("File System Personality", "Case-sensitive APFS"),
                                   ("Mount Point", "/Volumes/pao-cs"), ("Owners", "Enabled")):
                self.assertEqual(certificate.get(name), expected, "BLOCKED: certificate mismatch: " + name)
            report["phase"] = "certificate-quota"
            containers = plistlib.loads(diskutil("apfs", "list", "-plist")).get("Containers", [])
            volumes = [volume for container in containers for volume in container.get("Volumes", [])
                       if volume.get("DeviceIdentifier") == "disk3s7"]
            self.assertEqual(len(volumes), 1, "BLOCKED: quota certificate volume is not unique")
            report["capacity_quota"] = volumes[0].get("CapacityQuota")
            self.assertEqual(report["capacity_quota"], 536870912, "BLOCKED: exact 512 MiB quota required")
            report["phase"] = "base-inventory-before"
            initial = inventory("base_before")
            report["phase"] = "space-preflight"
            space = os.statvfs(physical)
            available = space.f_bavail * space.f_frsize
            report["available_bytes"] = available
            report["phase"] = "preflight-record"
            self.quota_record(label, {"phase": report["phase"], "base_before": initial,
                                      "available_bytes": available, "fill_limit": limit,
                                      "certificate": report["certificate"]}, report["journal_attempts"])
            report["phase"] = "space-validation"
            self.assertGreater(available, 0, "BLOCKED: no prior free quota space; never reclaim foreign files")
            self.assertLessEqual(available, limit, "BLOCKED: available space exceeds the authorized fill bound")
            report["phase"] = "mkdtemp"
            quota_root = Path(tempfile.mkdtemp(prefix=label + "-", dir=physical))
            report["root"] = str(quota_root)  # Creation returned; identity is not yet confirmed.
            self.quota_roots.append(quota_root)
            report["phase"] = "owned-root-identification"
            observed = metadata(quota_root)
            report["own_root_metadata"] = observed
            self.assertTrue(stat.S_ISDIR(observed["st_mode"]))
            self.assertEqual(stat.S_IMODE(observed["st_mode"]), 0o700)
            self.assertEqual(observed["st_uid"], os.getuid())
            self.assertEqual(observed["st_dev"], base_identity["st_dev"])
            owned_identity = observed
            report["root_attributed"] = True
            self.owned_bases.append(quota_root)
            report["phase"] = "owned-inventory-before"
            inventory("own_before", own=True)
            report["phase"] = "owned-root-record"
            self.quota_record(label, {"phase": "owned-root-created", "root": str(quota_root),
                                      "identity": owned_identity, "own_inventory_before": report["inventories"]["own_before"]},
                              report["journal_attempts"])
            for name in ("state", "project", "home", "fixtures"):
                report["phase"] = "mkdir-" + name
                (quota_root / name).mkdir(mode=0o700)
            for directory in (quota_root, physical):
                report["phase"] = "sync-" + ("owned-root" if directory == quota_root else "base")
                descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                except BaseException as exc:
                    remember(report["phase"], exc)
                finally:
                    try:
                        os.close(descriptor)
                    except BaseException as exc:
                        remember(report["phase"] + "-close", exc)
                if failures:
                    break
            if failures:
                raise failures[0][0].with_traceback(failures[0][1])
            report["phase"] = "causal-boundary-unproven"
            self.fail("BLOCKED C20/O06: certified setup retained; no PAO-approved discriminator for "
                      + target + "; no admission or fill attempted; C20 requires an own terminal ack")
        except BaseException as exc:
            if not failures or exc is not failures[0][0]:
                remember(report["phase"], exc)
        finally:
            for name, allowed, own in (("base_after", base_verified, False),
                                       ("own_after", owned_identity is not None, True)):
                if not allowed:
                    report["inventories"][name]["reason"] = "base not verified" if not own else "own root not attributed"
                    continue
                try:
                    inventory(name, own=own)
                except BaseException as exc:
                    remember(name, exc)
            before, after = (report["inventories"][name] for name in ("base_before", "base_after"))
            if before["status"] == after["status"] == "complete":
                comparison = {"status": "incomplete"}
                report["foreign_comparison"] = comparison
                try:
                    foreign = dict(after["entries"])
                    if quota_root is not None:
                        self.assertIsNotNone(owned_identity, "BLOCKED: created root identity unconfirmed")
                        current = foreign.pop(quota_root.name)
                        for field in ("st_dev", "st_ino", "st_uid", "st_mode"):
                            self.assertEqual(current[field], owned_identity[field], "BLOCKED: own root identity changed")
                    self.assertEqual(foreign, before["entries"], "BLOCKED: foreign base metadata changed; do not fill or clean")
                except BaseException as exc:
                    comparison["error"] = self.quota_error(exc)
                    remember("foreign-comparison", exc)
                else:
                    comparison["status"] = "complete"
            try:
                self.quota_record(label, report, report["journal_attempts"])
            except BaseException as exc:
                # The event is already in external_evidence with unconfirmed
                # persistence. Preserve it and the same primary exception.
                remember("final-record", exc)
        if failures:
            raise failures[0][0].with_traceback(failures[0][1])

    def test_o06_real_quota_failure_cannot_commit_capacity_without_audit(self):
        for phase in ("release", "reopen"):
            with self.subTest(conditional_quota_phase=phase):
                self.quota_case("o06-" + phase, operator_phase=phase)

    def discard_operator(self, action, operation_id, *options):
        from test_cli import CLI
        response = subprocess.run([sys.executable, "-B", str(CLI), "--state-dir", str(self.state),
                                   "operator", action, "--operation-id", operation_id,
                                   "--reason", "lost-ack fixture", *options], env=self.env, cwd=self.project,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=15)
        self.assertEqual(response.returncode, 0, response.stderr)
        return self.raw("operator", "status", "--operation-id", operation_id)

    def test_o07_lost_stdout_recovers_each_atomic_operator_binding(self):
        first, final, argv, counter = self.incomplete_job("o07")
        drain = self.discard_operator("drain", "o07-drain")["receipt"]["drain_id"]
        selectors = self.selectors(final, drain)
        proof = self.discard_operator("assess", "o07-assess", *selectors)["receipt"]["proof_id"]
        released = self.discard_operator("release", "o07-release", *selectors, "--proof", proof)
        self.assertEqual(released["status"], "committed")
        replay = self.op("release", "o07-release", *selectors, "--proof", proof, reason="lost-ack fixture")
        self.assertEqual(released["receipt"], replay["receipt"])
        self.raw("recover")
        self.assertTrue(self.raw("operator", "status")["control"]["drained"])
        self.discard_operator("reopen", "o07-open", "--drain-id", drain)
        self.wait_key("o07", 3)
        self.assertEqual(self.entries(counter), ["entry"])
        # A second real terminal assignment permits a client-death campaign too.
        # Import acknowledgement is external and does not locate a transaction.
        self.op("reopen", "o07-already-open", "--drain-id", drain, code=3)
        _, dying, _, _ = self.incomplete_job("o07-crash")
        d2 = self.crash_operator("drain", "o07-crash-drain")["receipt"]["drain_id"]
        second_selectors = self.selectors(dying, d2)
        p2 = self.crash_operator("assess", "o07-crash-assess", *second_selectors)["receipt"]["proof_id"]
        self.crash_operator("release", "o07-crash-release", *second_selectors, "--proof", p2)
        self.crash_operator("reopen", "o07-crash-open", "--drain-id", d2)
        # Neither ACK loss nor this campaign proves a kill at commit/fsync.

    def crash_operator(self, action, operation_id, *options):
        from test_cli import CLI
        process = subprocess.Popen([sys.executable, "-B", "-X", "importtime", str(CLI),
                                    "--state-dir", str(self.state), "operator", action,
                                    "--operation-id", operation_id, "--reason", "crash-campaign fixture", *options],
                                   env=self.env, cwd=self.project, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        self.processes.append(process)
        trace = b""
        deadline = time.monotonic() + 5
        while not any(line.rsplit(b"|", 1)[-1].strip() == b"fleet.cli" for line in trace.splitlines()):
            remaining = deadline - time.monotonic()
            self.assertGreater(remaining, 0, "operator import acknowledgement missing")
            self.assertTrue(select.select([process.stderr], [], [], remaining)[0])
            chunk = os.read(process.stderr.fileno(), 4096)
            self.assertTrue(chunk)
            trace += chunk
        signalled = process.poll() is None
        if signalled:
            process.send_signal(signal.SIGKILL)
        stdout, stderr = process.communicate(timeout=15)
        response = self.call("operator", "status", "--operation-id", operation_id, check=False)
        self.assertIn(response.returncode, (0, 2), (response.stdout, response.stderr))
        observed = json.loads(response.stdout)
        if response.returncode == 2:
            self.assertEqual(observed["error"]["code"], "operation_not_found")
        self.external_evidence.append({"case": "O07", "operation_id": operation_id,
                                       "signal_sent": signalled, "client_returncode": process.returncode,
                                       "observed_binding": observed, "exact_commit_window_proven": False})
        repeated = self.op(action, operation_id, *options, reason="crash-campaign fixture")
        if response.returncode == 0:
            self.assertEqual(repeated["binding"], "replay")
            self.assertEqual(repeated["receipt"], observed["receipt"])
        self.assertEqual(repeated["status"], "committed")
        return repeated

    def test_o08_only_explicit_current_epoch_reopen_allows_dispatch(self):
        _, _, _, _, d1, selectors, proof = self.resolution("o08")
        self.op("release", "o08-release", *selectors, "--proof", proof)
        argv, counter, ready, gate = self.counted("next")
        self.send("o08-next", argv)
        self.raw("evidence", "--idempotency-key", "o08")
        self.raw("recover")
        self.assertFalse(ready.exists())
        old_open = self.op("reopen", "o08-open", "--drain-id", d1)
        d2 = self.op("drain", "o08-drain-2")["receipt"]["drain_id"]
        replay = self.op("reopen", "o08-open", "--drain-id", d1)
        self.assertEqual(replay["receipt"], old_open["receipt"])
        self.assertEqual(replay["control"]["drain_id"], d2)
        self.assertTrue(replay["control"]["drained"])
        rejected = self.op("reopen", "o08-stale-open", "--drain-id", d1, code=5)
        self.assertEqual(rejected["error"]["code"], "drain_mismatch")
        self.raw("recover")
        self.assertFalse(ready.exists())
        self.op("reopen", "o08-current-open", "--drain-id", d2)
        self.raw("recover")
        self.ready("o08-next", ready)
        gate.touch()
        self.terminal("o08-next")
        self.assertEqual(self.entries(counter), ["next"])

    def test_o09_old_epoch_proof_and_cross_authority_proof_cannot_change_current(self):
        _, final, _, _, d1, selectors, proof = self.resolution("o09")
        self.op("release", "o09-release", *selectors, "--proof", proof)
        self.op("reopen", "o09-open", "--drain-id", d1)
        d2 = self.op("drain", "o09-drain-2")["receipt"]["drain_id"]
        historical = self.op("release", "o09-historical-free", *self.selectors(final, d2), "--proof", proof)
        self.assertEqual(historical["receipt"]["decision"], "already_released")
        other = self.root / "o09-other"
        _, other_final, _, _, other_drain, other_selectors, other_proof = self.resolution("o09-other", state=other)
        rejected = self.op("release", "o09-cross-proof", *other_selectors, "--proof", proof, state=other, code=2)
        self.assertEqual(rejected["error"]["code"], "proof_invalid")
        self.assertEqual(self.status_key("o09-other", state=other)["observation"]["reservation"]["disposition"], "retained")
        self.op("release", "o09-other-release", *other_selectors, "--proof", other_proof, state=other)

    def test_o10_host_side_operator_backend_is_explicitly_unsupported(self):
        self.send("o10-init", [sys.executable, "-c", "pass"])
        self.terminal("o10-init")
        rejected = self.op("drain", "o10-host", "--backend", "host-reservation@1", code=2)
        self.assertEqual(rejected["error"]["code"], "operator_backend_unsupported")
        self.assertTrue(rejected["receipt"]["audit_recorded"])
        self.assertFalse(self.raw("operator", "status")["control"]["drained"])

    def test_a_w1_live_drain_blocks_reopen_and_ordinary_supervisor_handoff(self):
        a, a_counter, a_ready, a_gate = self.counted("A")
        b, b_counter, b_ready, b_gate = self.counted("B")
        self.send("a-w1-A", a)
        self.ready("a-w1-A", a_ready)
        original_b = self.send("a-w1-B", b)
        drain = self.op("drain", "a-w1-drain")["receipt"]["drain_id"]
        rejected = self.op("reopen", "a-w1-premature-open", "--drain-id", drain, code=3)
        self.assertEqual(rejected["error"]["code"], "reopen_blocked")
        a_gate.touch()
        self.terminal("a-w1-A")
        self.wait_key("a-w1-A")
        self.assertEqual(self.status_key("a-w1-A")["observation"]["reservation"]["disposition"], "released")
        current_b = self.status_key("a-w1-B")
        self.assertEqual(current_b["observation"]["state"], "queued")
        self.assertTrue(current_b["observation"]["control"]["drained"])
        self.assertFalse(b_ready.exists())
        self.raw("recover")
        self.assertFalse(b_ready.exists())
        self.op("reopen", "a-w1-open", "--drain-id", drain)
        self.assertFalse(b_ready.exists())
        self.raw("recover")
        self.ready("a-w1-B", b_ready)
        b_gate.touch()
        final_b = self.terminal("a-w1-B")
        self.assertEqual(final_b["job"], original_b["job"])
        self.assertEqual(self.entries(a_counter), ["A"])
        self.assertEqual(self.entries(b_counter), ["B"])
        events = self.raw("operator", "status")["audit_events"]
        self.assertEqual([e["decision"] for e in events], ["committed", "reopen_blocked", "committed"])

    def test_b_w1_sigint_dispatch_campaign_records_actual_observable_window(self):
        from test_cli import CLI
        # Public starting/unbound is an observed claim, NOT proof that Popen has
        # not occurred. This bounded campaign exercises handled SIGINT; the exact
        # claim-pre-spawn interval remains blocked without external causal control.
        observations = []
        for index in range(3):
            state = self.root / ("b-w1-" + str(index))
            key = "b-w1-" + str(index)
            argv, counter, ready, gate = self.counted()
            admission = self.admit(state)
            process = subprocess.Popen([sys.executable, "-B", str(CLI), "--state-dir", str(state),
                                        "submit", "--client", "contract", "--idempotency-key", key, "--", *argv],
                                       cwd=self.project, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.processes.append(process)
            value = self.until(lambda: self.lookup_if_admitted(key, state))
            admission.attribute(value)
            observation = value["observation"]
            observations.append({"key": key, "state": observation["state"], "view": observation["view"],
                                 "supervisor_bound": observation["supervisor"] is not None,
                                 "exact_pre_spawn_proven": False})
            signalled = process.poll() is None
            if signalled:
                process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=15)
            observations[-1].update(signal_sent=signalled, client_returncode=process.returncode,
                                     client_response=json.loads(stdout) if stdout else None)
            self.assertIn(process.returncode, (0, 130), (stdout, stderr))
            current = self.status_key(key, state=state)
            self.assertIs(current["accepted"], True)
            gate.touch()
            self.terminal(key, state=state)
            self.assertLessEqual(len(self.entries(counter)), 1)
            self.assertEqual(self.status_key(key, state=state)["observation"]["reservation"]["disposition"], "released")
        self.external_evidence.append({"case": "B-W1", "observed_windows": observations})

    def lookup_if_admitted(self, key, state):
        response = self.call("status", "--idempotency-key", key, state=state, check=False)
        value = json.loads(response.stdout)
        return value if response.returncode == 0 else None

    def test_b_w2_atomic_local_binding_not_found_committed_ack_loss_and_conflict(self):
        self.send("b-w2-init", [sys.executable, "-c", "pass"])
        self.terminal("b-w2-init")
        unknown = self.raw("operator", "status", "--operation-id", "b-w2-drain", code=2)
        self.assertEqual(unknown["error"]["code"], "operation_not_found")
        committed = self.discard_operator("drain", "b-w2-drain")
        self.assertEqual(committed["status"], "committed")
        self.assertIs(committed["receipt"]["audit_recorded"], True)
        self.assertTrue(committed["control"]["drained"])
        replay = self.op("drain", "b-w2-drain", reason="lost-ack fixture")
        self.assertEqual(replay["receipt"], committed["receipt"])
        self.assertEqual(replay["binding"], "replay")
        self.op("drain", "b-w2-drain", reason="binding change", code=5)
        recovered = self.raw("recover")
        self.assertEqual(recovered["operator_pending"], [])
        self.assertTrue(self.raw("operator", "status")["control"]["drained"])
        repeated = self.op("drain", "b-w2-already-drained")
        self.assertEqual(repeated["receipt"]["decision"], "already_drained")
        self.assertEqual(repeated["receipt"]["drain_id"], committed["control"]["drain_id"])
