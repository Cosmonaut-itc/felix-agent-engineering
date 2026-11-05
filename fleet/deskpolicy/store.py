"""Fixed host paths, secure atomic files, Linux identity and bounded flock."""

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import stat
import tempfile
import time
from dataclasses import dataclass

from .model import BOOT, CPUS, Invalid, UTC, require, strict_json

RECEIPT_DIR_MODES = (0o700, 0o755, 0o775)


@dataclass(frozen=True)
class Sample:
    utc: dt.datetime
    bt: int
    boot: str


class Runtime:
    def __init__(self, harness=None):
        if harness is None and os.environ.get("RUN_SUITE_HARNESS"):
            from .testing import Harness
            harness = Harness.from_environment()
        if harness is not None:
            from .testing import Harness
            require(type(harness) is Harness, "explicit-harness-required")
            harness.validate()
        self.harness = harness
        self.root = harness.root if harness else Path("/tmp/sen-test-slots")
        self.config = harness.config if harness else account_home() / ".config/pao/desk-policy.json"
        if harness is None:
            installed = account_home() / ".local/lib/pao-deskpolicy/v1/deskpolicy"
            require(Path(__file__).absolute().parent == installed, "fixed-installation-required")
            secure_dir(installed)
            for name in SOURCE_FILES:
                fd = open_file(installed / name)
                os.close(fd)

    def sample(self):
        if self.harness:
            return self.harness.sample()
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        require(BOOT.fullmatch(boot), "boot-id")
        return Sample(dt.datetime.now(UTC), int(time.clock_gettime(time.CLOCK_BOOTTIME)), boot)

    def load(self):
        return self.harness.load_sample() if self.harness else os.getloadavg()[0]

    def cpus(self):
        if self.harness:
            return self.harness.cpus
        online = set()
        for part in Path("/sys/devices/system/cpu/online").read_text().strip().split(","):
            ends = [int(n) for n in part.split("-")]
            online.update(range(ends[0], ends[-1] + 1))
        require(set(CPUS) <= online, "cpu-topology")
        return CPUS

    def receipt_path(self, run_id):
        directory = self.harness.directory / "runs" if self.harness else account_home() / "remote-run/runs"
        return directory / run_id / "pao-admission.json"


def secure_dir(path, create=False, modes=(0o700,)):
    if create:
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) in modes, "directory-security")
    require(path.resolve() == path.absolute(), "directory-symlink")


def account_home():
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def secure_fd(fd, path=None, modes=(0o600,)):
    info = os.fstat(fd)
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and (modes is None or stat.S_IMODE(info.st_mode) in modes) and info.st_nlink == 1,
            "file-security")
    if path is not None:
        require(os.stat(path, follow_symlinks=False).st_ino == info.st_ino
                and os.stat(path, follow_symlinks=False).st_dev == info.st_dev,
                "descriptor-path")


def open_file(path, create=False, *, directory_modes=(0o700,), file_modes=(0o600,), readonly=False):
    secure_dir(path.parent, modes=directory_modes)
    fd = os.open(path, (os.O_RDONLY if readonly else os.O_RDWR) | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
                 | (os.O_CREAT if create else 0), 0o600)
    try:
        secure_fd(fd, path, modes=file_modes)
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_json(path, *, directory_modes=(0o700,), file_modes=(0o600,)):
    fd = open_file(path, directory_modes=directory_modes, file_modes=file_modes, readonly=True)
    try:
        data = bytearray()
        while len(data) < 65537:
            chunk = os.read(fd, 65537 - len(data))
            if not chunk:
                break
            data.extend(chunk)
        return strict_json(bytes(data))
    finally:
        os.close(fd)


def atomic_json(path, value, *, directory_modes=(0o700,)):
    secure_dir(path.parent, modes=directory_modes)
    data = (json.dumps(value, sort_keys=True, allow_nan=False) + "\n").encode()
    require(len(data) <= 65536, "json-size")
    fd, tmp = tempfile.mkstemp(prefix=".pao-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


SOURCE_FILES = ("__init__.py", "cli.py", "engine.py", "model.py", "store.py", "testing.py")


def code_digest():
    result = hashlib.sha256()
    for name in SOURCE_FILES:
        result.update(name.encode() + b"\x00" + (Path(__file__).parent / name).read_bytes())
    return result.hexdigest()


@contextlib.contextmanager
def mutex(root, inherited=None, name="admission.lock", create=True):
    path = root / name
    if inherited is not None:
        assert_held(inherited, path)
        yield inherited
        return
    fd = open_file(path, create=create)
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                require(time.monotonic() < deadline, "mutex-timeout")
                time.sleep(0.02)
        yield fd
    finally:
        os.close(fd)


def assert_held(fd, path):
    secure_dir(path.parent)
    secure_fd(fd, path)
    probe = open_file(path)
    try:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise Invalid("mutex-not-held")
        try:
            # flock may convert an owned shared lock to exclusive here. All
            # clients must hold exclusive from before this validation.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Invalid("mutex-not-owned") from exc
    finally:
        os.close(probe)


def held(path):
    fd = open_file(path)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True
    finally:
        os.close(fd)


def process(pid, owned=True):
    directory = Path("/proc/{}".format(pid))
    if owned:
        require(directory.stat().st_uid == os.getuid(), "process-owner")
    text = (directory / "stat").read_text()
    tail = text[text.rfind(")") + 2:].split()
    return {"pid": pid, "start": int(tail[19]), "pgid": int(tail[2]),
            "sid": int(tail[3]), "zombie": tail[0] == "Z"}


def session_members(sid):
    members = []
    for item in Path("/proc").iterdir():
        if not item.name.isdigit():
            continue
        try:
            identity = process(int(item.name), owned=False)
        except (FileNotFoundError, ProcessLookupError):
            continue
        if identity["sid"] == sid and not identity["zombie"]:
            members.append(identity["pid"])
    return members
