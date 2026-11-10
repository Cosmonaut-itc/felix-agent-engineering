#!/usr/bin/env python3
"""Run only in a remote-run mirror, with all fixtures beneath its cwd."""

from pathlib import Path
import sys
import unittest
import os

fleet = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(fleet))
from deskpolicy.store import code_digest

print("Python {} | CPUs {} | module_sha256 {}".format(
    sys.version.split()[0], sorted(os.sched_getaffinity(0)), code_digest()), flush=True)
suite = unittest.defaultTestLoader.discover(str(Path(__file__).parent), top_level_dir=str(fleet))
sys.exit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
