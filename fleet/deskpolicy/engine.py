"""One admission transaction owns FIFO, counts, weights, CPUs and lifecycle."""

import datetime as dt
import fcntl
import os
import re

from . import VERSION
from .model import (BOOT, INTERVAL, KINDS, PROJECTS, TTL, WAIT_TTL,
                    Invalid, allocate, config, finite_load, identifier,
                    integer, keys, night, require, timestamp, utc_text, validate_policy)
from .store import (Runtime, atomic_json, assert_held, code_digest, digest, held, mutex,
                    process, read_json, secure_dir, secure_fd, session_members)

RECORD_KEYS = set("schema request_id project kind owner boot_id seq state "
                  "admitted_run_id renewed_boottime_s expires_boottime_s "
                  "weight claim last_reason".split())
CLAIM_KEYS = set("run_id lane weight cpus boot_id process state admitted_boottime_s "
                 "closed_boottime_s close_evidence mode revision".split())
REASONS = set("policy-invalid policy-expired window-closed load-high quota-full "
              "budget-full weight-full cpu-full start-interval fifo census-unknown "
              "resource-busy legacy-freeze request-expired request-terminal "
              "request-conflict weight-invalid local-limit already-admitted "
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
    keys(claim, CLAIM_KEYS)
    identifier(claim["run_id"])
    require(claim["run_id"] == value["admitted_run_id"] and claim["boot_id"] == value["boot_id"]
            and claim["weight"] == value["weight"], "claim-identity")
    integer(claim["lane"], 1, 12)
    require(type(claim["cpus"]) is list and len(claim["cpus"]) == value["weight"], "claim-cpus")
    for cpu in claim["cpus"]:
        integer(cpu, 0, 11)
    require(len(set(claim["cpus"])) == len(claim["cpus"]), "claim-cpus-duplicate")
    require(claim["state"] in ("active", "uncertain", "closed"), "claim-state")
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
                and claim["close_evidence"] == "session-empty", "claim-close")
    else:
        require(claim["closed_boottime_s"] is None and claim["close_evidence"] is None,
                "claim-live-close")
    return value


def admission_receipt(request, run_id, policy, lane, reason=None, claim=None, replay=False):
    return {"schema": "pao/desk-admission@1", "request_id": request,
            "run_id": run_id, "state": "denied" if reason else "admitted",
            "mode": policy["mode"], "revision": policy["revision"], "lane": lane,
            "reason": reason, "exit_code": 75 if reason else None,
            "cpus": claim["cpus"] if claim else [],
            "weight": claim["weight"] if claim else None,
            "replay": replay, "execute": not reason and not replay,
            "claim_state": claim["state"] if claim else None}


class DeskPolicy:
    receipt = staticmethod(admission_receipt)

    def __init__(self, runtime=None):
        self.runtime = runtime or Runtime()
        self.root = self.runtime.root
        self.prepare()

    def record_path(self, request_id):
        return self.root / "pao-wait" / (identifier(request_id) + ".json")

    def life_path(self, project, lane):
        require(project in PROJECTS, "project")
        integer(lane, 1, 12)
        if project == "jale":
            return self.root / ("slot-" + str(lane - 1) + ".lock")
        return self.root / "pao-hasito" / ("lane-" + str(lane) + ".lock")

    def prepare(self):
        secure_dir(self.root, create=True)
        with mutex(self.root):
            for name in ("pao-wait", "pao-hasito"):
                secure_dir(self.root / name, create=True)

    def records(self):
        directory = self.root / "pao-wait"
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

    def census(self, records, sample, persist=False):
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
        secure_dir(directory)
        for path in directory.iterdir():
            match = re.fullmatch(r"lane-([0-9]+)\.lock", path.name)
            require(match is not None and self.life_path("hasito", int(match[1])) == path,
                    "noncanonical-resource")
            life_files.append(path)
        for path in life_files:
            require(not held(path) or path in claimed, "untracked-life-lock")
        return live

    def waiting(self, records, sample):
        result = {p: [] for p in PROJECTS}
        for record in records:
            if record["state"] == "pending" and record["boot_id"] == sample.boot:
                require(record["renewed_boottime_s"] <= sample.bt, "wait-clock")
                if sample.bt < record["expires_boottime_s"]:
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
                    "night_budget": 4, "cpu_weight_budget": 12, "max_run_weight": 8,
                    "min_start_interval_s": INTERVAL, "expires_at": None}, reason

    def register(self, request_id, project, kind, owner, weight, withdraw=False):
        identifier(request_id)
        require(project in PROJECTS and kind in KINDS[project], "request-project-kind")
        integer(weight, 1, 12)
        require(type(owner) is str and 0 < len(owner) <= 256, "request-owner")
        with mutex(self.root):
            sample = self.runtime.sample()
            records = self.records()
            existing = next((r for r in records if r["request_id"] == request_id), None)
            if existing:
                require((project, kind, owner, weight, sample.boot) == (
                    existing["project"], existing["kind"], existing["owner"],
                    existing["weight"], existing["boot_id"]), "request-conflict")
                if existing["state"] != "pending":
                    return existing
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
                require(claim["run_id"] == run_id and claim["lane"] == lane, "request-conflict")
                return self.receipt(request_id, run_id, claim, lane, claim=claim, replay=True)
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
            waiting = self.waiting(records, sample)
            project = record["project"]
            if any(r["project"] == project and r["claim"]["lane"] == lane for r in live):
                return deny("resource-busy")
            if waiting[project][0]["request_id"] != request_id:
                return deny("fifo")
            if record["weight"] > policy["max_run_weight"]:
                return deny("weight-invalid")
            limit = policy["night_budget"] if policy["mode"] == "night" else 2
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
            if weight + record["weight"] > (policy["cpu_weight_budget"] if policy["mode"] == "night" else 12):
                return deny("weight-full")
            last = max((r["claim"]["admitted_boottime_s"] for r in records
                        if r["claim"] is not None and r["boot_id"] == sample.boot), default=None)
            if last is not None:
                require(last <= sample.bt, "census-clock")
                if policy["mode"] == "night" and sample.bt - last < policy["min_start_interval_s"]:
                    return deny("start-interval")
            used = {cpu for r in live for cpu in r["claim"]["cpus"]}
            available = sorted(set(self.runtime.cpus()) - used)
            if len(available) < record["weight"]:
                return deny("cpu-full")
            identity = process(pid)
            require(identity["pid"] == identity["pgid"] == identity["sid"]
                    and not identity["zombie"] and os.getsid(0) == identity["sid"]
                    and pid in (os.getpid(), os.getppid()), "wrapper-session-required")
            path = self.life_path(project, lane)
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
                     "cpus": available[:record["weight"]], "boot_id": sample.boot,
                     "process": identity, "state": "active", "admitted_boottime_s": final.bt,
                     "closed_boottime_s": None, "close_evidence": None,
                     "mode": policy["mode"], "revision": policy["revision"]}
            record.update(state="admitted", admitted_run_id=run_id, claim=claim, last_reason=None)
            self.write_record(record)
            return self.receipt(request_id, run_id, policy, lane, claim=claim)

    def close(self, request_id, run_id, pid, life_fd, mutex_fd):
        with mutex(self.root, inherited=mutex_fd):
            record = validate_record(read_json(self.record_path(request_id)), request_id)
            claim = record["claim"]
            require(claim is not None and claim["run_id"] == run_id, "request-conflict")
            if claim["state"] == "closed":
                return record
            sample = self.runtime.sample()
            require(claim["boot_id"] == sample.boot and sample.bt >= claim["admitted_boottime_s"],
                    "close-unproven")
            assert_held(life_fd, self.life_path(record["project"], claim["lane"]))
            identity = process(pid)
            require(identity["sid"] == claim["process"]["sid"]
                    and not identity["zombie"] and os.getsid(0) == identity["sid"]
                    and pid in (os.getpid(), os.getppid()), "close-unproven")
            require(set(session_members(identity["sid"])) <= {pid, os.getpid()}, "close-unproven")
            claim.update(state="closed", closed_boottime_s=sample.bt, close_evidence="session-empty")
            self.write_record(record)
            return record

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
        with mutex(self.root):
            configuration = config(read_json(self.runtime.config))
            if disable_night:
                configuration["night_enabled"] = False
                atomic_json(self.runtime.config, configuration)
            sample = self.runtime.sample()
            records = self.records()
            live = self.census(records, sample, persist=True)
            waiting = self.waiting(records, sample)
            for record in records:
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
            waiting = self.waiting(records, sample)
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
                      "budget": policy["night_budget"] if policy["mode"] == "night" else 4,
                      "cpu_weight_budget": policy["cpu_weight_budget"] if policy["mode"] == "night" else 12,
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
