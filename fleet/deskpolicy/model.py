"""Wire validation and pending-divisor-min1@1."""

import datetime as dt
import json
import math
import re
from fractions import Fraction

PROJECTS = ("jale", "hasito")
DAY_LIMITS = {"jale": 2, "hasito": 3}
DAY_BUDGET = sum(DAY_LIMITS.values())
DAY_MAX_LANE = max(DAY_LIMITS.values())
KINDS = {"jale": ("chain", "verify", "exclusive"), "hasito": ("script",)}
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
BOOT = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
UTC = dt.timezone.utc
CPUS = tuple(range(12))
TTL = 60
WAIT_TTL = 120
INTERVAL = 120
CONFIG = {"schema": "pao/desk-config@1", "night_enabled": False,
          "night_budget": 4, "cpu_weight_budget": 8,
          "max_run_weight": 4, "min_start_interval_s": INTERVAL}
POLICY_KEYS = set("schema revision writer boot_id issued_at expires_at "
                  "issued_boottime_s expires_boottime_s mode night_budget "
                  "cpu_weight_budget max_run_weight min_start_interval_s "
                  "load1_lt limits pending algorithm".split())


class Invalid(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise Invalid(message)


def integer(value, low=0, high=2**63 - 1):
    require(type(value) is int and low <= value <= high, "integer-range")
    return value


def keys(value, expected):
    require(type(value) is dict and set(value) == set(expected), "object-fields")


def identifier(value):
    require(type(value) is str and ID.fullmatch(value) is not None, "identifier")
    return value


def timestamp(value):
    require(type(value) is str and re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value), "utc-format")
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise Invalid("utc-calendar") from exc


def utc_text(value):
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def night(now):
    return 5 <= now.astimezone(UTC).hour < 14


def strict_json(data):
    require(len(data) <= 65536 and b"\x00" not in data, "json-size-or-nul")

    def pairs(items):
        result = {}
        for name, value in items:
            require(name not in result, "duplicate-field")
            require("\x00" not in name, "json-nul")
            result[name] = value
        return result

    def invalid_number(value):
        raise Invalid("non-finite-json")

    def float_number(value):
        parsed = float(value)
        require(math.isfinite(parsed), "non-finite-json")
        return parsed

    def strings(value):
        if isinstance(value, str):
            require("\x00" not in value, "json-nul")
        elif isinstance(value, dict):
            for item in value.values():
                strings(item)
        elif isinstance(value, list):
            for item in value:
                strings(item)

    try:
        result = json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                            parse_constant=invalid_number, parse_float=float_number)
        strings(result)
        return result
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Invalid(str(exc)) from exc


def config(value):
    keys(value, CONFIG)
    require(value["schema"] == CONFIG["schema"], "config-version")
    require(type(value["night_enabled"]) is bool, "night-enabled")
    budgets(value)
    return value


def budgets(value):
    integer(value["night_budget"], 1, 12)
    integer(value["cpu_weight_budget"], 1, 12)
    integer(value["max_run_weight"], 1, min(4, value["cpu_weight_budget"]))
    require(type(value["min_start_interval_s"]) is int
            and value["min_start_interval_s"] == INTERVAL, "start-interval")


def counts(value):
    keys(value, PROJECTS)
    for item in value.values():
        integer(item)


def validate_policy(value, sample):
    keys(value, POLICY_KEYS)
    require(value["schema"] == "pao/desk-policy@1" and value["writer"] == "pao"
            and value["algorithm"] == "pending-divisor-min1@1", "policy-version")
    integer(value["revision"], 1)
    require(type(value["boot_id"]) is str and BOOT.fullmatch(value["boot_id"])
            and value["boot_id"] == sample.boot, "policy-boot")
    budgets(value)
    counts(value["limits"])
    counts(value["pending"])
    issued = timestamp(value["issued_at"])
    expires = timestamp(value["expires_at"])
    start = integer(value["issued_boottime_s"])
    end = integer(value["expires_boottime_s"], 1)
    duration = (expires - issued).total_seconds()
    require(0 < duration <= TTL and end - start == duration, "policy-duration")
    require(issued <= sample.utc and start <= sample.bt, "policy-future")
    require(sample.utc < expires and sample.bt < end, "policy-expired")
    # A UTC jump cannot extend a lease relative to BOOTTIME.
    require(abs((sample.utc - issued).total_seconds() - (sample.bt - start)) < 2,
            "policy-clock")
    require(value["mode"] in ("day", "night"), "policy-mode")
    integer(value["load1_lt"], 9, 12)
    if value["mode"] == "night":
        require(night(issued) and expires <= issued.replace(hour=14, minute=0, second=0),
                "policy-window")
        require(night(sample.utc), "window-closed")
        require(value["load1_lt"] == 12 and sum(value["limits"].values())
                <= value["night_budget"], "night-limits")
    else:
        require(value["load1_lt"] == 9 and value["limits"] == DAY_LIMITS,
                "day-limits")
    return value


def allocate(budget, pending, live, head):
    quota = dict.fromkeys(PROJECTS, 0)
    eligible = [p for p in PROJECTS if pending[p]]
    order = lambda p: (head[p], p)
    for project in sorted(eligible, key=order):
        if sum(quota.values()) < budget:
            quota[project] += 1
    while sum(quota.values()) < budget:
        choices = [p for p in eligible if quota[p] < live[p] + pending[p]]
        if not choices:
            break
        project = min(choices, key=lambda p: (
            -Fraction(pending[p], quota[p] + 1), *order(p)))
        quota[project] += 1
    return quota


def finite_load(value):
    return type(value) in (float, int) and math.isfinite(value) and value >= 0
