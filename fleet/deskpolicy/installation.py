"""Validate a drained cutover before publishing an idempotent day installation."""

import argparse
import hashlib
import os
from pathlib import Path
import stat
import sys
import tempfile

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskpolicy.engine import DeskPolicy
from deskpolicy.model import CONFIG, config, require
from deskpolicy.store import (SOURCE_FILES, Runtime, account_home, atomic_json, code_digest,
                              held, mutex, open_file, read_json, secure_dir)


def destination_path(path, private=False):
    require(path.is_absolute() and path.resolve() == path, "installation-path")
    for parent in [*reversed(path.parents), path]:
        if not parent.exists():
            continue
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode), "installation-directory")
    if path.exists():
        info = path.stat()
        require(info.st_uid == os.getuid() and not info.st_mode & 0o022, "installation-owner-mode")
        if private:
            secure_dir(path)


def check_state(engine):
    if not engine.root.exists() and not engine.root.is_symlink():
        return
    secure_dir(engine.root)
    for path in engine.root.glob("*.lock"):
        fd = open_file(path, readonly=True)
        os.close(fd)
        if path.name != "admission.lock":
            require(not held(path), "preflight-requires-drain")
    sample = engine.runtime.sample()
    records = engine.records(missing_ok=True)
    require(not engine.census(records, sample, missing_ok=True), "preflight-requires-drain")
    require(not any(engine.waiting(records, sample).values()), "preflight-requires-drain")
    require(engine.read_policy(sample)[0]["mode"] == "day", "cutover-requires-day")


def install(engine, source, destination, reference):
    require(type(reference) is str and 0 < len(reference) <= 256 and "\n" not in reference,
            "apto-reference")
    require(source.resolve() == source and source.is_dir(), "installation-source")
    payload = {}
    for name in SOURCE_FILES:
        path = source / name
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid(), "installation-source")
        payload[name] = path.read_bytes()
    manifest = "".join(hashlib.sha256(payload[name]).hexdigest() + "  deskpolicy/" + name + "\n"
                       for name in sorted(payload))
    destination_path(destination.parent, private=True)
    destination_path(destination, private=True)
    destination_path(engine.runtime.config.parent, private=True)
    if engine.runtime.config.exists() or engine.runtime.config.is_symlink():
        configuration = config(read_json(engine.runtime.config))
        require(configuration["night_enabled"] is False, "cutover-requires-day")
    else:
        configuration = dict(CONFIG)
    if destination.exists():
        secure_dir(destination / "deskpolicy")
        for name, expected in payload.items():
            fd = open_file(destination / "deskpolicy" / name, readonly=True)
            try:
                require(os.read(fd, len(expected) + 1) == expected, "installation-conflict")
            finally:
                os.close(fd)
        for name, expected in (("APTO", reference + "\n"), ("SHA256SUMS", manifest)):
            fd = open_file(destination / name, readonly=True)
            try:
                require(os.read(fd, len(expected.encode()) + 1) == expected.encode(), "installation-conflict")
            finally:
                os.close(fd)
    check_state(engine)
    secure_dir(engine.root, create=True)
    with mutex(engine.root):
        check_state(engine)
        engine.prepare_directories()
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        engine.runtime.config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        stage = None
        config_stage = None
        installed = False
        configured = False
        try:
            if not destination.exists():
                stage = Path(tempfile.mkdtemp(prefix=".pao-install-", dir=destination.parent))
                (stage / "deskpolicy").mkdir(mode=0o700)
                for name, data in payload.items():
                    write_private(stage / "deskpolicy" / name, data)
                write_private(stage / "APTO", (reference + "\n").encode())
                write_private(stage / "SHA256SUMS", manifest.encode())
                sync_directory(stage / "deskpolicy")
                sync_directory(stage)
            if not engine.runtime.config.exists():
                fd, raw = tempfile.mkstemp(prefix=".pao-config-", dir=engine.runtime.config.parent)
                os.close(fd)
                config_stage = Path(raw)
                atomic_json(config_stage, configuration)
            if stage is not None:
                os.replace(stage, destination)
                installed = True
                sync_directory(destination.parent)
            if config_stage is not None:
                os.replace(config_stage, engine.runtime.config)
                configured = True
                sync_directory(engine.runtime.config.parent)
        except BaseException:
            if configured:
                os.rename(engine.runtime.config, config_stage)
            if installed:
                os.rename(destination, stage)
            raise
    return {"schema": "pao/desk-install@1", "mode": "day", "module_sha256": code_digest()}


def write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--apto", required=True)
    args = parser.parse_args()
    try:
        require(sys.platform == "linux" and os.access("/usr/bin/taskset", os.X_OK)
                and os.access("/usr/bin/flock", os.X_OK), "linux-tools")
        require(not os.environ.get("RUN_SUITE_HARNESS"), "deployment-harness-forbidden")
        engine = DeskPolicy(Runtime(check_installation=False), prepare=False)
        require(engine.runtime.cpus(), "cpu-topology")
        result = install(engine, Path(__file__).resolve().parent,
                         account_home() / ".local/lib/pao-deskpolicy/v1", args.apto)
        print(result)
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 75


if __name__ == "__main__":
    sys.exit(main())
