"""One admission transaction owns FIFO, counts, weights, CPUs and lifecycle."""

import datetime as dt
import fcntl
import json
import os
import re
import secrets
import sys

from . import VERSION
from .model import (BOOT, INTERVAL, KINDS, PROJECTS, TTL, WAIT_TTL,
                    Invalid, allocate, config, finite_load, identifier,
                    integer, keys, night, require, timestamp, utc_text, validate_policy)
from .store import (RECEIPT_DIR_MODES, Runtime, atomic_json, assert_held, code_digest,
                    digest, held, mutex, process, read_json, secure_dir, secure_fd, session_members)

RECORD_KEYS = set("schema request_id project kind owner boot_id seq state "
                  "admitted_run_id renewed_boottime_s expires_boottime_s "
                  "weight claim last_reason".split())
CLAIM_KEYS = set("run_id lane weight cpus boot_id process state admitted_boottime_s "
                 "closed_boottime_s close_evidence mode revision claim_token script_exit_code close_cause".split())
REASONS = set("policy-invalid policy-expired window-closed load-high quota-full "
              "budget-full weight-full cpu-full start-interval fifo census-unknown "
              "resource-busy legacy-freeze request-expired request-terminal "
              "request-conflict weight-invalid weight-too-large local-limit already-admitted "
              "close-unproven mutex-error internal-error".split())


def validate_record(value, name):
    keys(value, RECORD_KEYS)
    require(value["schema"] == "pao/desk-wait@1" and value["request_id"] == name,
            "request-schema")
    identifier(name)
    require(value["project"] in PROJECTS and value["kind"] in KINDS[value["project"]],
            "request-project-kind")
    require(type(value["owner"]) is str and 0 < len(value["owner"]) <= 256,
            "request-owner")
    require(type(value["boot_id"]) is str and BOOT.fullmatch(value["boot_id"]), "request-boot")
    integer(value["seq"], 1)
    integer(value["weight"], 1, 12)
    renewed = integer(value["renewed_boottime_s"])
    require(integer(value["expires_boottime_s"]) == renewed + WAIT_TTL, "request-ttl")
    require(value["state"] in ("pending", "admitted", "withdrawn", "expired"), "request-state")
    require(value["last_reason"] is None or value["last_reason"] in REASONS, "request-reason")
    claim = value["claim"]
    if value["state"] != "admitted":
        require(claim is None and value["admitted_run_id"] is None, "request-claim")
        return value
    keys(claim, CLAIM_KEYS | ({"keeper_sid"} if "keeper_sid" in claim else set())
         | ({"keepers"} if "keepers" in claim else set()))
    if "keeper_sid" in claim:
        integer(claim["keeper_sid"], 1)
    if "keepers" in claim:
        require(type(claim["keepers"]) is list, "claim-keepers")
        seen = set()
        for keeper in claim["keepers"]:
            keys(keeper, ("pid", "start", "pgid", "sid", "zombie"))
            for key in ("pid", "start", "pgid", "sid"):
                integer(keeper[key], 1)
            require(keeper["pid"] == keeper["sid"] == keeper["pgid"]
                    and keeper["zombie"] is False, "claim-keeper")
            identity = (keeper["pid"], keeper["start"])
            require(identity not in seen, "claim-keeper-duplicate")
            seen.add(identity)
    identifier(claim["run_id"])
    require(claim["run_id"] == value["admitted_run_id"] and claim["boot_id"] == value["boot_id"]
            and claim["weight"] == value["weight"], "claim-identity")
    integer(claim["lane"], 1, 12)
    require(type(claim["claim_token"]) is str
            and re.fullmatch(r"[0-9a-f]{64}", claim["claim_token"]), "claim-token")
    width = value["weight"] if claim["mode"] == "night" else 12
    require(type(claim["cpus"]) is list and len(claim["cpus"]) == width, "claim-cpus")
    for cpu in claim["cpus"]:
        integer(cpu, 0, 11)
    require(len(set(claim["cpus"])) == len(claim["cpus"]), "claim-cpus-duplicate")
    require(claim["state"] in ("active", "uncertain", "closed"), "claim-state")
    if claim["script_exit_code"] is not None:
        integer(claim["script_exit_code"], 0, 255)
    require(claim["mode"] in ("day", "night"), "claim-mode")
    require(claim["revision"] is None or type(claim["revision"]) is int
            and claim["revision"] >= 1, "claim-revision")
    admitted = integer(claim["admitted_boottime_s"])
    require(admitted >= renewed and admitted < value["expires_boottime_s"], "claim-time")
    keys(claim["process"], ("pid", "start", "pgid", "sid", "zombie"))
    for key in ("pid", "start", "pgid", "sid"):
        integer(claim["process"][key], 1)
    require(claim["process"]["zombie"] is False, "claim-process")
    if claim["state"] == "closed":
        require(integer(claim["closed_boottime_s"]) >= admitted
                and claim["close_evidence"] == "session-empty"
                and claim["close_cause"] in ("completed", "stopped", "reconciled", "sync-timeout"), "claim-close")
    else:
        require(claim["closed_boottime_s"] is None and claim["close_evidence"] is None
                and claim["close_cause"] is None,
                "claim-live-close")
    return value


def admission_receipt(request, run_id, policy, lane, reason=None, claim=None, replay=False):
    return {"schema": "pao/desk-admission@1", "request_id": request,
            "run_id": run_id, "state": "denied" if reason else "admitted",
            "mode": policy["mode"], "revision": policy["revision"],
            "lane": lane if type(lane) is int else None,
            "reason": reason, "exit_code": 75 if reason else None,
            "cpus": claim["cpus"] if claim else [],
            "weight": claim["weight"] if claim else None,
            "replay": replay, "execute": not reason and not replay,
            "claim_state": claim["state"] if claim else None,
            "claim_token": claim["claim_token"] if claim else None,
            "script_exit_code": claim["script_exit_code"] if claim else None,
            "close_cause": claim["close_cause"] if claim else None}


def merge_receipt(prior, current):
    """Keep admitted identity and advance only state or unknown terminal fields."""
    merged = dict(prior)
    states = {None: -1, "active": 0, "uncertain": 1, "closed": 2}
    if states[current.get("claim_state")] > states[prior.get("claim_state")]:
        merged["claim_state"] = current["claim_state"]
    for field in ("script_exit_code", "close_cause", "close_error"):
        if prior.get(field) is None and current.get(field) is not None:
            merged[field] = current[field]
    return merged


class DeskPolicy:
    receipt = staticmethod(admission_receipt)

    def __init__(self, runtime=None, *, prepare=True):
        self.runtime = runtime or Runtime()
        self.root = self.runtime.root
        if prepare:
            self.prepare()

    def record_path(self, request_id):
        return self.root / "pao-wait" / (identifier(request_id) + ".json")

    def life_path(self, project, lane):
        require(project in PROJECTS, "project")
        integer(lane, 1, 12)
        if project == "jale":
            return self.root / ("slot-" + str(lane - 1) + ".lock")
        return self.root / "pao-hasito" / ("lane-" + str(lane) + ".lock")

    def prepare(self, inherited=None):
        secure_dir(self.root, create=True)
        with mutex(self.root, inherited=inherited):
            self.prepare_directories()

    def prepare_directories(self):
        for name in ("pao-wait", "pao-hasito"):
            secure_dir(self.root / name, create=True)

    def records(self, missing_ok=False):
        directory = self.root / "pao-wait"
        if missing_ok and not directory.exists() and not directory.is_symlink():
            return []
        secure_dir(directory)
        records = []
        sequences = set()
        for path in sorted(directory.iterdir()):
            if path.name.startswith(".pao-"):
                continue
            require(path.suffix == ".json", "unknown-wait-file")
            record = validate_record(read_json(path), path.stem)
            sequence = (record["boot_id"], record["seq"])
            require(sequence not in sequences, "duplicate-sequence")
            sequences.add(sequence)
            records.append(record)
        return records

    def write_record(self, record):
        validate_record(record, record["request_id"])
        atomic_json(self.record_path(record["request_id"]), record)

    def census(self, records, sample, persist=False, missing_ok=False):
        allowed = {"admission.lock", "writer.lock", "pao-policy-v1.json", "pao-window.json",
                   "pao-wait", "pao-hasito", "pao-arm.json", "pao-control.json"}
        secure_dir(self.root)
        slot_dirs = []
        life_files = []
        for path in self.root.iterdir():
            match = re.fullmatch(r"slot-([0-9]+)(\.lock)?", path.name)
            if match:
                lane = int(match[1]) + 1
                require(1 <= lane <= 12 and str(lane - 1) == match[1], "noncanonical-slot")
                if match[2]:
                    life_files.append(path)
                else:
                    slot_dirs.append((path, lane))
            else:
                require(path.name in allowed or path.name.startswith(".pao-"), "legacy-or-unknown-resource")
        claimed = {}
        live = []
        cpus = set()
        runs = set()
        for record in records:
            claim = record["claim"]
            if claim is None:
                continue
            require(claim["run_id"] not in runs, "duplicate-run")
            runs.add(claim["run_id"])
            if claim["boot_id"] == sample.boot:
                require(claim["admitted_boottime_s"] <= sample.bt, "census-clock")
                if claim["closed_boottime_s"] is not None:
                    require(claim["closed_boottime_s"] <= sample.bt, "census-clock")
            if claim["state"] == "closed":
                continue
            require(claim["boot_id"] == sample.boot, "census-boot")
            require(claim["admitted_boottime_s"] <= sample.bt, "census-clock")
            path = self.life_path(record["project"], claim["lane"])
            require(path not in claimed, "duplicate-resource")
            claimed[path] = record
            if claim["mode"] == "night":
                require(not cpus.intersection(claim["cpus"]), "overlapping-cpus")
                cpus.update(claim["cpus"])
            if not held(path) and claim["state"] == "active":
                claim["state"] = "uncertain"
                if persist:
                    self.write_record(record)
            live.append(record)
        for directory, lane in slot_dirs:
            secure_dir(directory)
            require(self.life_path("jale", lane) in claimed, "legacy-slot-unaccounted")
        directory = self.root / "pao-hasito"
        missing = missing_ok and not directory.exists() and not directory.is_symlink()
        if not missing:
            secure_dir(directory)
        for path in (() if missing else directory.iterdir()):
            match = re.fullmatch(r"lane-([0-9]+)\.lock", path.name)
            require(match is not None and self.life_path("hasito", int(match[1])) == path,
                    "noncanonical-resource")
            life_files.append(path)
        for path in life_files:
            require(not held(path) or path in claimed, "untracked-life-lock")
        return live

    def waiting(self, records, sample, max_weight=12):
        result = {p: [] for p in PROJECTS}
        for record in records:
            if record["state"] == "pending" and record["boot_id"] == sample.boot:
                require(record["renewed_boottime_s"] <= sample.bt, "wait-clock")
                if sample.bt < record["expires_boottime_s"] and record["weight"] <= max_weight:
                    result[record["project"]].append(record)
        for items in result.values():
            items.sort(key=lambda item: item["seq"])
        return result

    def read_policy(self, sample):
        try:
            policy = validate_policy(read_json(self.root / "pao-policy-v1.json"), sample)
            return policy, None
        except (OSError, ValueError, TypeError, OverflowError) as exc:
            reason = str(exc)
            reason = reason if reason in ("policy-expired", "window-closed") else "policy-invalid"
            return {"mode": "day", "revision": None, "limits": dict.fromkeys(PROJECTS, 2),
                    "night_budget": 4, "cpu_weight_budget": None, "max_run_weight": 4,
                    "min_start_interval_s": INTERVAL, "expires_at": None}, reason

    def register(self, request_id, project, kind, owner, weight, withdraw=False):
        identifier(request_id)
        require(project in PROJECTS and kind in KINDS[project], "request-project-kind")
        integer(weight, 1)
        require(type(owner) is str and 0 < len(owner) <= 256, "request-owner")
        with mutex(self.root):
            sample = self.runtime.sample()
            records = self.records()
            existing = next((r for r in records if r["request_id"] == request_id), None)
            policy, _ = self.read_policy(sample)
            if existing:
                require((project, kind, owner, weight, sample.boot) == (
                    existing["project"], existing["kind"], existing["owner"],
                    existing["weight"], existing["boot_id"]), "request-conflict")
                if existing["state"] != "pending":
                    return existing
                if not withdraw and weight > policy["max_run_weight"]:
                    existing.update(state="expired", last_reason="weight-too-large")
                    self.write_record(existing)
                    raise Invalid("weight-too-large")
                require(existing["renewed_boottime_s"] <= sample.bt, "wait-clock")
                if sample.bt >= existing["expires_boottime_s"]:
                    existing["state"] = "expired"
                elif withdraw:
                    existing["state"] = "withdrawn"
                else:
                    existing["renewed_boottime_s"] = sample.bt
                    existing["expires_boottime_s"] = sample.bt + WAIT_TTL
                self.write_record(existing)
                return existing
            require(not withdraw, "request-terminal")
            require(weight <= policy["max_run_weight"], "weight-too-large")
            record = {"schema": "pao/desk-wait@1", "request_id": request_id,
                      "project": project, "kind": kind, "owner": owner,
                      "weight": weight, "boot_id": sample.boot,
                      "seq": 1 + max((r["seq"] for r in records if r["boot_id"] == sample.boot), default=0),
                      "state": "pending", "admitted_run_id": None,
                      "renewed_boottime_s": sample.bt, "expires_boottime_s": sample.bt + WAIT_TTL,
                      "claim": None, "last_reason": None}
            self.write_record(record)
            return record

    def legacy_veto(self, kind, sample, jale_live):
        path = self.root / "pao-window.json"
        try:
            value = read_json(path, file_modes=None)
        except FileNotFoundError:
            return False
        except (OSError, ValueError):
            return True
        try:
            require(type(value) is dict and type(value["until"]) is str
                    and type(value.get("reason", "unknown")) is str, "legacy-window")
            until = value["until"]
            require(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z", until),
                    "legacy-until")
            expires = dt.datetime.fromisoformat(until[:-1] + "+00:00")
            allowance = value.get("jale_slots", 0)
            allowance = allowance if type(allowance) is int and allowance in (0, 1) else 0
            return sample.utc < expires and (jale_live >= allowance or kind == "exclusive")
        except (ValueError, TypeError, KeyError):
            return True

    def admit(self, request_id, run_id, lane, pid, life_fd, mutex_fd, local_limit=None):
        identifier(request_id)
        identifier(run_id)
        if lane != "any":
            integer(lane, 1, 12)
        integer(pid, 1)
        with mutex(self.root, inherited=mutex_fd):
            sample = self.runtime.sample()
            policy, fallback = self.read_policy(sample)
            records = self.records()
            live = self.census(records, sample, persist=True)
            record = next((r for r in records if r["request_id"] == request_id), None)
            require(record is not None, "request-terminal")
            if record["state"] == "admitted":
                claim = record["claim"]
                require(claim["run_id"] == run_id and (lane == "any" or claim["lane"] == lane), "request-conflict")
                return self.receipt(request_id, run_id, claim, claim["lane"], claim=claim, replay=True)
            require(not any(r["claim"] is not None and r["claim"]["run_id"] == run_id
                            for r in records), "request-conflict")

            def deny(reason):
                record["last_reason"] = reason
                self.write_record(record)
                result = self.receipt(request_id, run_id, policy, lane, reason)
                result["policy_reason"] = fallback
                return result

            if record["state"] != "pending" or record["boot_id"] != sample.boot:
                return deny("request-terminal")
            if sample.bt >= record["expires_boottime_s"]:
                record["state"] = "expired"
                return deny("request-expired")
            project = record["project"]
            limit = policy["night_budget"] if policy["mode"] == "night" else 2
            if lane == "any":
                occupied = {r["claim"]["lane"] for r in live if r["project"] == project}
                lane = next((n for n in range(1, limit + 1) if n not in occupied), None)
                if lane is None:
                    return deny("resource-busy")
            blocker = next((r for r in live if r["project"] == project and r["claim"]["lane"] == lane), None)
            if blocker is not None:
                if not self.reconcile_claim(blocker, sample):
                    return deny("resource-busy")
                live.remove(blocker)
            if record["weight"] > policy["max_run_weight"]:
                record["state"] = "expired"
                return deny("weight-too-large")
            waiting = self.waiting(records, sample, policy["max_run_weight"])
            if waiting[project][0]["request_id"] != request_id:
                return deny("fifo")
            if lane > limit:
                return deny("resource-busy")
            project_live = [r for r in live if r["project"] == project]
            if len(live) >= (policy["night_budget"] if policy["mode"] == "night" else 4):
                return deny("budget-full")
            if len(project_live) >= policy["limits"][project]:
                return deny("quota-full")
            if local_limit is not None:
                integer(local_limit, 1, 12)
                if len(project_live) >= local_limit:
                    return deny("local-limit")
            if project == "jale":
                if (project_live and record["kind"] == "exclusive"
                        or any(r["kind"] == "exclusive" for r in project_live)):
                    return deny("resource-busy")
                if policy["mode"] == "day" and any(r["kind"] == record["kind"] for r in project_live):
                    return deny("resource-busy")
                if self.legacy_veto(record["kind"], sample, len(project_live)):
                    return deny("legacy-freeze")
            try:
                load = self.runtime.load()
            except OSError:
                load = None
            if policy["mode"] == "night" or project == "jale":
                if not finite_load(load) or load >= (12 if policy["mode"] == "night" else 9):
                    return deny("load-high")
            weight = sum(r["weight"] for r in live)
            if policy["mode"] == "night" and weight + record["weight"] > policy["cpu_weight_budget"]:
                return deny("weight-full")
            last = max((r["claim"]["admitted_boottime_s"] for r in records
                        if r["claim"] is not None and r["boot_id"] == sample.boot), default=None)
            if last is not None:
                require(last <= sample.bt, "census-clock")
                if policy["mode"] == "night" and sample.bt - last < policy["min_start_interval_s"]:
                    return deny("start-interval")
            used = {cpu for r in live for cpu in r["claim"]["cpus"]}
            available = sorted(set(self.runtime.cpus()) - used) if policy["mode"] == "night" else list(self.runtime.cpus())
            if policy["mode"] == "night" and len(available) < record["weight"]:
                return deny("cpu-full")
            identity = process(pid)
            require(identity["pid"] == identity["pgid"] == identity["sid"]
                    and not identity["zombie"] and os.getsid(0) == identity["sid"]
                    and pid in (os.getpid(), os.getppid()), "wrapper-session-required")
            path = self.life_path(project, lane)
            if callable(life_fd):
                life_fd = life_fd(path)
            elif isinstance(life_fd, dict):
                require(lane in life_fd, "life-fd-required")
                life_fd = life_fd[lane]
            secure_fd(life_fd, path)
            try:
                fcntl.flock(life_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return deny("resource-busy")
            # Re-read after resource/process probes. A lease expiring during this
            # transaction cannot confer a night admission at its end.
            final = self.runtime.sample()
            try:
                validate_policy(policy, final)
            except Invalid:
                if policy["revision"] is not None:
                    return deny("policy-expired")
            require(final.boot == sample.boot and final.bt >= sample.bt, "clock-changed")
            if final.bt >= record["expires_boottime_s"]:
                record["state"] = "expired"
                return deny("request-expired")
            claim = {"run_id": run_id, "lane": lane, "weight": record["weight"],
                     "cpus": available[:record["weight"]] if policy["mode"] == "night" else available,
                     "boot_id": sample.boot,
                     "process": identity, "keepers": [], "state": "active", "admitted_boottime_s": final.bt,
                     "claim_token": secrets.token_hex(32),
                     "closed_boottime_s": None, "close_evidence": None,
                     "script_exit_code": None, "close_cause": None,
                     "mode": policy["mode"], "revision": policy["revision"]}
            record.update(state="admitted", admitted_run_id=run_id, claim=claim, last_reason=None)
            self.write_record(record)
            return self.receipt(request_id, run_id, policy, lane, claim=claim)

    def handoff(self, request_id, run_id, pid, life_fd, mutex_fd, *, claim_token=None):
        """Register each keeper before its launcher releases life or starts load."""
        with mutex(self.root, inherited=mutex_fd):
            record = validate_record(read_json(self.record_path(request_id)), request_id)
            claim = record["claim"]
            require(claim is not None and claim["run_id"] == run_id, "request-conflict")
            require(type(claim_token) is str and secrets.compare_digest(claim_token, claim["claim_token"]),
                    "claim-token")
            sample = self.runtime.sample()
            require(claim["state"] != "closed" and claim["boot_id"] == sample.boot
                    and sample.bt >= claim["admitted_boottime_s"], "handoff-unproven")
            assert_held(life_fd, self.life_path(record["project"], claim["lane"]))
            identity = process(pid)
            require(not identity["zombie"] and identity["pid"] == identity["sid"] == identity["pgid"]
                    and os.getsid(0) == identity["sid"] and pid in (os.getpid(), os.getppid())
                    and identity["sid"] != claim["process"]["sid"], "handoff-unproven")
            controls = [identity, process(os.getpid())]
            require(session_members(identity["sid"], exclude=controls) == [],
                    "handoff-load-before-registration")
            keepers = claim.setdefault("keepers", [])
            if identity not in keepers:
                keepers.append(identity)
                self.write_record(record)
            return record

    def close(self, request_id, run_id, pid, life_fd, mutex_fd, *, claim_token=None,
              script_exit_code=None, cause="completed"):
        with mutex(self.root, inherited=mutex_fd):
            record = validate_record(read_json(self.record_path(request_id)), request_id)
            claim = record["claim"]
            require(claim is not None and claim["run_id"] == run_id, "request-conflict")
            require(type(claim_token) is str and secrets.compare_digest(claim_token, claim["claim_token"]),
                    "claim-token")
            if claim["state"] == "closed":
                return record
            sample = self.runtime.sample()
            require(claim["boot_id"] == sample.boot and sample.bt >= claim["admitted_boottime_s"],
                    "close-unproven")
            assert_held(life_fd, self.life_path(record["project"], claim["lane"]))
            identity = process(pid)
            require(not identity["zombie"] and os.getsid(0) == identity["sid"]
                    and pid in (os.getpid(), os.getppid()), "close-unproven")
            allowed = [identity, process(os.getpid())]
            original = claim["process"]
            if identity["sid"] != original["sid"]:
                require(identity in claim.get("keepers", []), "close-unproven")
            try:
                current = process(original["pid"])
                if current["start"] == original["start"]:
                    allowed.append(current)
            except (FileNotFoundError, ProcessLookupError):
                pass
            for sid in self.claim_sessions(claim):
                if session_members(sid, exclude=allowed) != []:
                    claim["state"] = "uncertain"
                    if script_exit_code is not None:
                        claim["script_exit_code"] = integer(script_exit_code, 0, 255)
                    self.write_record(record)
                    raise Invalid("close-unproven")
            require(cause in ("completed", "stopped", "sync-timeout"), "close-cause")
            if script_exit_code is not None:
                claim["script_exit_code"] = integer(script_exit_code, 0, 255)
            claim.update(state="closed", closed_boottime_s=sample.bt, close_evidence="session-empty",
                         close_cause=cause)
            self.write_record(record)
            return record

    @staticmethod
    def claim_sessions(claim):
        sessions = {claim["process"]["sid"]}
        sessions.update(p["sid"] for p in claim.get("keepers", []))
        if "keeper_sid" in claim:
            sessions.add(claim["keeper_sid"])
        return sessions

    def reconcile_receipt(self, record):
        path = self.runtime.receipt_path(record["claim"]["run_id"])
        try:
            receipt = read_json(path, directory_modes=RECEIPT_DIR_MODES)
        except FileNotFoundError:
            return
        require(receipt.get("schema") == "pao/desk-admission@1"
                and receipt.get("state") == "admitted" and receipt.get("request_id") == record["request_id"]
                and receipt.get("run_id") == record["claim"]["run_id"], "receipt-conflict")
        recovered = merge_receipt(receipt, {"claim_state": "closed",
            "script_exit_code": record["claim"]["script_exit_code"],
            "close_cause": record["claim"]["close_cause"]})
        if recovered != receipt:
            atomic_json(path, recovered, directory_modes=RECEIPT_DIR_MODES)

    def reconcile_claim(self, record, sample):
        """Caller owns the mutex and has validated the complete census."""
        claim = record["claim"]
        if claim["state"] != "uncertain":
            return False
        try:
            free = not held(self.life_path(record["project"], claim["lane"]))
            empty = free and all(session_members(sid) == [] for sid in self.claim_sessions(claim))
        except (OSError, ValueError):
            return False
        if not empty:
            return False
        # The keeper must register before starting children. An unregistered
        # live keeper retains life; a dead one cannot have started any load.
        if record["project"] == "jale":
            metadata = self.root / ("slot-" + str(claim["lane"] - 1))
            if metadata.exists() or metadata.is_symlink():
                secure_dir(metadata)
                archive = self.root / (".pao-reconciled-" + record["request_id"])
                require(not archive.exists() and not archive.is_symlink(), "reconcile-archive-conflict")
                os.rename(metadata, archive)
        claim.update(state="closed", closed_boottime_s=sample.bt,
                     close_evidence="session-empty", close_cause="reconciled")
        self.write_record(record)
        self.recover_receipt(record)
        return True

    def recover_receipt(self, record):
        try:
            self.reconcile_receipt(record)
        except Exception as exc:
            print(json.dumps({"schema": "pao/desk-reconcile-diagnostic@1",
                              "request_id": record["request_id"], "run_id": record["claim"]["run_id"],
                              "reason": "receipt-error", "detail": str(exc)}, sort_keys=True),
                  file=sys.stderr, flush=True)

    def reconcile(self, mutex_fd=None):
        """Close uncertain reservations only with an empty session and free life lock."""
        with mutex(self.root, inherited=mutex_fd):
            sample = self.runtime.sample()
            records = self.records()
            live = self.census(records, sample, persist=True)
            closed = []
            for record in records:
                if record["claim"] is not None and record["claim"]["state"] == "closed":
                    self.recover_receipt(record)
            for record in live:
                if self.reconcile_claim(record, sample):
                    closed.append(record["request_id"])
            return {"schema": "pao/desk-reconcile@1", "closed": closed}

    def arm_valid(self, configuration, sample):
        try:
            arm = read_json(self.root / "pao-arm.json")
            keys(arm, ("schema", "boot_id", "config_sha256", "configuration", "version",
                       "module_sha256", "evidence_sha256"))
            approved = config(arm["configuration"])
            return (arm["schema"] == "pao/desk-arm@1" and arm["boot_id"] == sample.boot
                    and arm["config_sha256"] == digest(approved) and arm["version"] == VERSION
                    and configuration["night_enabled"] == approved["night_enabled"]
                    and all(configuration[key] <= approved[key] for key in (
                        "night_budget", "cpu_weight_budget", "max_run_weight"))
                    and arm["module_sha256"] == code_digest()
                    and type(arm["evidence_sha256"]) is str and len(arm["evidence_sha256"]) == 64)
        except (OSError, ValueError):
            return False

    def preflight(self, evidence=None, arm=False):
        with mutex(self.root, create=False):
            sample = self.runtime.sample()
            configuration = config(read_json(self.runtime.config))
            require(self.runtime.cpus(), "cpu-topology")
            records = self.records()
            live = self.census(records, sample)
            self.waiting(records, sample)
            require(os.path.isfile("/usr/bin/taskset") and os.path.isfile("/usr/bin/flock"), "linux-tools")
            if arm:
                require(not live, "preflight-requires-drain")
                keys(evidence, ("schema", "boot_id", "checked_at", "consumers", "resources_checked", "legacy_drained"))
                require(evidence["schema"] == "pao/desk-preflight@1"
                        and evidence["boot_id"] == sample.boot
                        and evidence["resources_checked"] is True and evidence["legacy_drained"] is True,
                        "preflight-evidence")
                checked = timestamp(evidence["checked_at"])
                require(0 <= (sample.utc - checked).total_seconds() <= 300, "preflight-age")
                keys(evidence["consumers"], PROJECTS)
                for sha in evidence["consumers"].values():
                    require(type(sha) is str and len(sha) == 40 and all(c in "0123456789abcdef" for c in sha),
                            "consumer-sha")
                atomic_json(self.root / "pao-arm.json", {
                    "schema": "pao/desk-arm@1", "boot_id": sample.boot,
                    "config_sha256": digest(configuration), "configuration": configuration,
                    "version": VERSION, "module_sha256": code_digest(),
                    "evidence_sha256": digest(evidence)})
            return {"schema": "pao/desk-preflight-result@1", "boot_id": sample.boot,
                    "live": len(live), "armed": self.arm_valid(configuration, sample),
                    "night_enabled": configuration["night_enabled"], "version": VERSION,
                    "module_sha256": code_digest()}

    def publish(self, disable_night=False):
        with mutex(self.root) as lock:
            configuration = config(read_json(self.runtime.config))
            if disable_night:
                configuration["night_enabled"] = False
                atomic_json(self.runtime.config, configuration)
            self.reconcile(mutex_fd=lock)
            sample = self.runtime.sample()
            records = self.records()
            live = self.census(records, sample, persist=True)
            waiting = self.waiting(records, sample, configuration["max_run_weight"])
            for record in records:
                if (record["state"] == "pending" and record["boot_id"] == sample.boot
                        and record["weight"] > configuration["max_run_weight"]):
                    record.update(state="expired", last_reason="weight-too-large")
                    self.write_record(record)
                if (record["state"] == "pending" and record["boot_id"] == sample.boot
                        and record["expires_boottime_s"] <= sample.bt):
                    record["state"] = "expired"
                    self.write_record(record)
            mode = "night" if (configuration["night_enabled"] and night(sample.utc)
                               and self.arm_valid(configuration, sample)) else "day"
            pending = {p: len(waiting[p]) for p in PROJECTS}
            quota = allocate(configuration["night_budget"], pending,
                             {p: sum(r["project"] == p for r in live) for p in PROJECTS},
                             {p: waiting[p][0]["seq"] if waiting[p] else 2**63 for p in PROJECTS})
            control_path = self.root / "pao-control.json"
            try:
                control = read_json(control_path)
                keys(control, ("schema", "revision"))
                require(control["schema"] == "pao/desk-control@1", "control-version")
                revision = integer(control["revision"], 1) + 1
            except FileNotFoundError:
                policy_path = self.root / "pao-policy-v1.json"
                require(not policy_path.exists() and not policy_path.is_symlink(), "control-missing")
                revision = 1
            duration = TTL
            issued = sample.utc.replace(microsecond=0)
            if mode == "night":
                duration = min(TTL, int((issued.replace(hour=14, minute=0, second=0) - issued).total_seconds()))
            policy = {"schema": "pao/desk-policy@1", "revision": revision, "writer": "pao",
                      "boot_id": sample.boot, "issued_at": utc_text(issued),
                      "expires_at": utc_text(issued + dt.timedelta(seconds=duration)),
                      "issued_boottime_s": sample.bt, "expires_boottime_s": sample.bt + duration,
                      "mode": mode, "night_budget": configuration["night_budget"],
                      "cpu_weight_budget": configuration["cpu_weight_budget"],
                      "max_run_weight": configuration["max_run_weight"],
                      "min_start_interval_s": configuration["min_start_interval_s"],
                      "load1_lt": 12 if mode == "night" else 9,
                      "limits": quota if mode == "night" else dict.fromkeys(PROJECTS, 2),
                      "pending": pending, "algorithm": "pending-divisor-min1@1"}
            validate_policy(policy, sample)
            atomic_json(control_path, {"schema": "pao/desk-control@1", "revision": revision})
            atomic_json(self.root / "pao-policy-v1.json", policy)
            return policy

    def status(self, request_id=None):
        with mutex(self.root, create=False):
            sample = self.runtime.sample()
            policy, reason = self.read_policy(sample)
            records = self.records()
            live = self.census(records, sample)
            waiting = self.waiting(records, sample, policy["max_run_weight"])
            claims = [{"request_id": r["request_id"], "project": r["project"], **r["claim"]} for r in live]
            last = max((r["claim"]["admitted_boottime_s"] for r in records
                        if r["claim"] is not None and r["boot_id"] == sample.boot), default=None)
            try:
                load = self.runtime.load()
            except OSError:
                load = None
            result = {"schema": "pao/desk-view@1", "version": VERSION, "boot_id": sample.boot,
                      "mode": policy["mode"], "revision": policy["revision"],
                      "expires_at": policy["expires_at"], "limits": policy["limits"],
                      "budget": policy["night_budget"] if policy["mode"] == "night" else None,
                      "max_lane": policy["night_budget"] if policy["mode"] == "night" else 2,
                      "cpu_weight_budget": policy["cpu_weight_budget"] if policy["mode"] == "night" else None,
                      "pending": {p: len(waiting[p]) for p in PROJECTS},
                      "head": {p: waiting[p][0]["request_id"] if waiting[p] else None for p in PROJECTS},
                      "live": {p: sum(r["project"] == p for r in live) for p in PROJECTS},
                      "active": sum(c["state"] == "active" for c in claims),
                      "uncertain": sum(c["state"] == "uncertain" for c in claims),
                      "weight": sum(r["weight"] for r in live), "claims": claims,
                      "last_admitted_boottime_s": last, "load1": load if finite_load(load) else None,
                      "policy_reason": reason,
                      "rejections": {r["request_id"]: r["last_reason"] for r in records if r["last_reason"]}}
            if request_id:
                result["request"] = next((r for r in records if r["request_id"] == request_id), None)
            return result
