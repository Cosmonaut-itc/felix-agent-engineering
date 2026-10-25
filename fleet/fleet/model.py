"""Versioned local job model. No process identity or selector grants authority."""
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import json
import re
import uuid

API = "fleet-job-api@1"
CONTENT = "fleet-submission-content@1"
SCHEMA_VERSION = 4
APP_ID = 0x464C5431
KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", re.ASCII)
ID = re.compile(r"[jarpd]_[0-9a-f]{32}", re.ASCII)


class Gate(str, Enum):
    AVAILABLE = "available"
    CONSUMED = "consumed"
    CLOSED = "closed"


class Disposition(str, Enum):
    RETAINED = "retained"
    RELEASED = "released"


@dataclass(frozen=True)
class Assignment:
    job: str
    attempt: str
    reservation: str
    generation: int


@dataclass(frozen=True)
class Control:
    drained: bool
    epoch: int
    drain_id: object


class Fault(ValueError):
    def __init__(self, code, exit_code=2, effects=None, **fields):
        super().__init__(code)
        self.code = code
        self.exit_code = exit_code
        self.effects = effects
        self.fields = fields


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def opaque(kind):
    return kind + "_" + uuid.uuid4().hex


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def dump(value):
    return canonical(value).decode("utf-8")


def load(value):
    return json.loads(value) if value is not None else None


def safe_queued(row):
    return (row["state"] == "queued" and row["gate"] == "available"
            and all(row[name] is None for name in
                    ("generation", "reservation", "dispatcher", "supervisor", "child", "command",
                     "closure", "closure_body", "recorded_outcome", "disposition", "capacity_resolution",
                     "evidence_root", "started_at", "ended_at"))
            and load(row["collection"]) == {"state": "pending", "errors": []}
            and load(row["publication"]) == {"state": "pending", "root_sha256": None})


def key(value):
    if not isinstance(value, str) or KEY.fullmatch(value) is None:
        raise Fault("invalid_key")
    return value


def identifier(value, prefix):
    if not isinstance(value, str) or not ID.fullmatch(value) or not value.startswith(prefix + "_"):
        raise Fault("invalid_selector")
    return value


def envelope(operation, authority=None, **fields):
    result = {"api": API, "operation": operation}
    if authority:
        result.update(authority_id=authority["id"], principal_uid=authority["uid"])
    result.update(fields)
    return result


def error(operation, fault, authority=None, effects="read_only", **fields):
    result = envelope(operation, authority, error={"code": fault.code,
                                                 "effects": fault.effects or effects})
    result.update(fault.fields)
    result.update(fields)
    return result
