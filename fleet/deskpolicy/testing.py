"""Private fixtures for unittest and the explicit RUN_SUITE_HARNESS CLI."""

import datetime as dt
import os
from pathlib import Path
import sys

from .model import BOOT, UTC, integer, keys, require, timestamp
from .store import Sample, read_json, secure_dir


class Harness:
    def __init__(self, directory, *, from_cli=False):
        self.from_cli = from_cli
        self.directory = Path(directory).absolute()
        self.root = self.directory / "slots"
        self.config = self.directory / "config" / "desk-policy.json"
        self.now = dt.datetime(2026, 11, 5, 6, 0, tzinfo=UTC)
        self.bt = 1000
        self.boot = "00112233-4455-6677-8899-aabbccddeeff"
        self.load = 0.0
        self.cpus = (0, 1)
        self.clock_file = None
        self.loadavg_file = None
        self.port_listeners = None
        self.validate()

    @classmethod
    def from_environment(cls):
        require(bool(os.environ.get("RUN_SUITE_HARNESS")), "explicit-harness-required")
        require(bool(os.environ.get("LOCK_ROOT")), "harness-lock-root-required")
        root = Path(os.environ["LOCK_ROOT"])
        directory = os.environ.get("PAO_DESK_HARNESS", str(root.parent))
        harness = cls(directory, from_cli=True)
        harness.root = root
        harness.cpus = tuple(range(12))
        for name, field in (("PAO_DESK_CLOCK_FILE", "clock_file"), ("LOADAVG_FILE", "loadavg_file"),
                            ("PORT_LISTENERS", "port_listeners")):
            raw = os.environ.get(name)
            if raw:
                path = Path(raw)
                require(path.is_absolute() and path.resolve() == path, "harness-path")
                path.relative_to(Path.cwd().resolve())
                setattr(harness, field, path)
        harness.validate()
        return harness

    def validate(self):
        require(bool(os.environ.get("RUN_SUITE_HARNESS")) if self.from_cli else "unittest" in sys.modules,
                "explicit-harness-required" if self.from_cli else "unittest-harness-required")
        self.directory.relative_to(Path.cwd().resolve())
        require(self.root.is_absolute() and self.root.resolve() == self.root, "harness-path")
        self.root.relative_to(self.directory)
        require(self.root != Path("/tmp/sen-test-slots"), "production-path-forbidden")
        secure_dir(self.directory)

    def sample(self):
        if self.clock_file:
            value = read_json(self.clock_file)
            keys(value, ("utc", "bt", "boot"))
            require(type(value["boot"]) is str and BOOT.fullmatch(value["boot"]), "boot-id")
            return Sample(timestamp(value["utc"]), integer(value["bt"]), value["boot"])
        return Sample(self.now, self.bt, self.boot)

    def load_sample(self):
        if self.loadavg_file:
            try:
                return float(self.loadavg_file.read_text().split()[0])
            except (ValueError, IndexError):
                return None
        return self.load

    def advance(self, seconds):
        self.bt += seconds
        self.now += dt.timedelta(seconds=seconds)
