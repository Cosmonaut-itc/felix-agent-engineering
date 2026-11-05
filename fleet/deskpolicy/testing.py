"""Explicit, in-process unittest harness. The production CLI has no test seams."""

import datetime as dt
from pathlib import Path
import sys

from .model import UTC, require
from .store import Sample, secure_dir


class Harness:
    def __init__(self, directory):
        self.directory = Path(directory).absolute()
        self.root = self.directory / "slots"
        self.config = self.directory / "config" / "desk-policy.json"
        self.now = dt.datetime(2026, 11, 5, 6, 0, tzinfo=UTC)
        self.bt = 1000
        self.boot = "00112233-4455-6677-8899-aabbccddeeff"
        self.load = 0.0
        self.cpus = (0, 1)
        self.validate()

    def validate(self):
        require("unittest" in sys.modules, "unittest-harness-required")
        self.directory.relative_to(Path.cwd().resolve())
        require(self.root != Path("/tmp/sen-test-slots"), "production-path-forbidden")
        secure_dir(self.directory)

    def sample(self):
        return Sample(self.now, self.bt, self.boot)

    def advance(self, seconds):
        self.bt += seconds
        self.now += dt.timedelta(seconds=seconds)
