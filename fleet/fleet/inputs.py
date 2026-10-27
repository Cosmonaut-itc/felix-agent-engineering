import contextlib
import errno
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import select
import stat
import subprocess
import time
import unicodedata

VERSION = "fleet-inputs@1"
CONTEXT_VERSION = "fleet-context@2"
SEARCH_VARIABLES = {
    "PATH": "colon",
    "PYTHONPATH": "colon",
    "LD_LIBRARY_PATH": "colon-or-semicolon",
    "LD_PRELOAD": "colon-or-space",
    "DYLD_LIBRARY_PATH": "colon",
    "DYLD_FALLBACK_LIBRARY_PATH": "colon",
    "DYLD_FRAMEWORK_PATH": "colon",
    "DYLD_FALLBACK_FRAMEWORK_PATH": "colon",
    "DYLD_INSERT_LIBRARIES": "colon",
    "NODE_PATH": "colon",
    "RUBYLIB": "colon",
    "PERL5LIB": "colon",
}
CONTEXT = {"version": CONTEXT_VERSION, "PWD": "materialized-cwd",
           "search_policy": {"version": "fleet-search-paths@1", "variables": SEARCH_VARIABLES,
                             "empty_component": "cwd;empty-preload-tokens-ignored",
                             "relative": "within-capture", "absolute": "disjoint-from-source",
                             "aliases": "components-and-immediate-children", "expansion": "literal-only"}}
DEFAULT_PRIVATE = (".ssh", ".aws", ".azure", ".config/gcloud", ".gnupg",
                   ".kube", ".env", ".env.*", "*.pem", "*.key", ".npmrc",
                   ".pypirc", ".netrc", "credentials", "credentials.json", ".fleet",
                   ".local/state/fleet-pilot")
VCS = (".git", ".hg", ".svn")
MAX_FILES = 20000
MAX_BYTES = 256 * 1024 * 1024
MAX_FILE = 32 * 1024 * 1024
MAX_PATH_BYTES = 16384
MAX_PATH_DEPTH = 128
NOFOLLOW = os.O_NOFOLLOW | os.O_CLOEXEC


class InputError(ValueError):
    pass


class PathIdentityError(InputError):
    def __init__(self, path, number):
        super().__init__("path_identity_unavailable")
        self.path = os.fspath(path)
        self.errno = number


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def relative(raw, allow_dot=False):
    if not isinstance(raw, str) or "\x00" in raw or "\\" in raw:
        raise InputError("invalid_relative_path")
    parts = raw.split("/")
    if allow_dot and raw == ".":
        return raw
    if not parts or any(p in ("", ".", "..") for p in parts) or len(raw) > 2048:
        raise InputError("invalid_relative_path")
    return raw


@contextlib.contextmanager
def directory(path, create=False):
    path = Path(os.path.abspath(path))
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextlib.contextmanager
def open_file(root, rel):
    relative(rel)
    fd = os.dup(root)
    result = None
    try:
        parts = rel.split("/")
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        result = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | NOFOLLOW, dir_fd=fd)
        if not stat.S_ISREG(os.fstat(result).st_mode):
            raise InputError("unsupported_file_type")
        yield result
    finally:
        if result is not None:
            os.close(result)
        os.close(fd)


def fsync_dir(path):
    with directory(path) as fd:
        os.fsync(fd)


def write_new(path, data, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, mode)
    try:
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("short_write")
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)


def stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def read_stable(fd, secrets=(), limit=MAX_FILE):
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise InputError("file_limit_or_type")
    data = bytearray()
    while True:
        chunk = os.read(fd, min(65536, limit + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > limit:
            raise InputError("file_limit")
    if stamp(before) != stamp(os.fstat(fd)) or len(data) != before.st_size:
        raise InputError("source_changed")
    if any(secret in data for secret in secrets):
        raise InputError("declared_credential_in_content")
    return bytes(data), before


def credential_values(env, declared):
    names = set(declared)
    if any(not name or "=" in name or "\x00" in name for name in names):
        raise InputError("invalid_credential_name")
    for name in env:
        upper = name.upper()
        if (upper.endswith(("_TOKEN", "_SECRET", "_PASSWORD", "_API_KEY", "_PRIVATE_KEY"))
                or upper in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "GH_TOKEN",
                             "GITHUB_TOKEN", "OPENAI_API_KEY")):
            names.add(name)
    values = sorted({env[name].encode() for name in names if env.get(name)}, key=len, reverse=True)
    if len(values) > 256 or sum(map(len, values)) > 65536:
        raise InputError("credential_limit")
    return values


def redact(text, secrets):
    for secret in secrets:
        text = text.replace(secret.decode(errors="surrogateescape"), "[REDACTED]")
    return text


def policy_summary(private, secrets):
    return {"version": VERSION, "default_private": list(DEFAULT_PRIVATE),
            "consumer_private": [redact(p, secrets) for p in private],
            "excluded_categories": ["VCS metadata", "Fleet state", "Git ignored files"],
            "git_bytes": "Effective working-tree bytes; HEAD and index are provenance, not executed index content.",
            "redaction": "Known and declared credential values in stdout/stderr; full environment is private.",
            "limit": "Arbitrary file contents are not inspected to discover undeclared secrets.",
            "execution_context": CONTEXT}


def _path_limit(path):
    raw = os.fspath(path)
    if (len(raw) > MAX_PATH_BYTES or raw.count("/") > MAX_PATH_DEPTH
            or len(os.fsencode(raw)) > MAX_PATH_BYTES):
        raise InputError("source_reference_limit")


def _resolved(path):
    _path_limit(path)
    resolved = Path(os.path.realpath(path))
    _path_limit(resolved)
    return resolved


def _ancestors(path):
    _path_limit(path)
    yield path
    yield from path.parents


def _within(path, root):
    _path_limit(path)
    _path_limit(root)
    if path == root or root in path.parents:
        return True
    identity = _object_id(root)
    return identity is not None and any(
        _object_id(parent) == identity for parent in _ancestors(path))


def _object_id(path):
    _path_limit(path)
    try:
        info = path.stat()
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.ENAMETOOLONG):
            return None
        if exc.errno in (errno.EACCES, errno.EPERM, errno.ELOOP):
            raise PathIdentityError(path, exc.errno) from None
        raise
    return info.st_dev, info.st_ino


def _overlaps(path, root):
    return _within(path, root) or _within(root, path)


def _literal_paths(value, whole_path):
    if whole_path and os.path.isabs(value):
        yield value
        for component in re.finditer(r"[^:]+", value):
            if component.group() != value and component.group().startswith("/"):
                yield component.group()
    pattern = r"""'(/[^'\x00\r\n]*)'|"(/[^"\x00\r\n]*)"|(/[^\s\x00'";|<>(){}\[\],=]*)"""
    for match in re.finditer(pattern, value):
        literal = match.group(1) or match.group(2) or match.group(3)
        literals = (literal,)
        if whole_path and match.group(3) is not None:
            suffix = value[match.start(3):]
            if suffix != literal:
                literals += (suffix,)
        for candidate in literals:
            yield candidate
            for component in re.finditer(r"[^:]+", candidate):
                if component.group() != candidate and component.group().startswith("/"):
                    yield component.group()


def _source_reference(value, root, whole_path=False):
    if str(root) in value:
        return True
    for index, literal in enumerate(_literal_paths(value, whole_path)):
        if index >= MAX_FILES * 2:
            raise InputError("source_reference_limit")
        _path_limit(literal)
    for literal in _literal_paths(value, whole_path):
        if _within(_resolved(literal), root):
            return True
    return False


def _check_scope(root, state, env):
    homes = {_resolved(pwd.getpwuid(os.getuid()).pw_dir)}
    if env.get("HOME"):
        homes.add(_resolved(env["HOME"]))
    resolved = _resolved(root)
    if resolved == Path("/") or any(_within(home, resolved) for home in homes):
        raise InputError("unsafe_input_root")
    if _within(resolved, _resolved(state)):
        raise InputError("source_in_state")


def _check_search_paths(env, cwd, root, diagnostics):
    inspected = set()
    entries = 0
    for name, separator in SEARCH_VARIABLES.items():
        if name not in env:
            continue
        value = env[name]
        if len(value) > 65536:
            raise InputError("search_path_limit")
        if separator == "colon-or-space":
            components = value.replace(":", " ").split()
        elif separator == "colon-or-semicolon":
            components = value.replace(";", ":").split(":")
        else:
            components = value.split(":")
        if len(components) > 1024:
            raise InputError("search_path_limit")
        for component in components:
            if "\x00" in component:
                raise InputError("invalid_search_path")
            _path_limit(component)
            absolute = os.path.isabs(component)
            raw = Path(component) if absolute else cwd / component
            _path_limit(raw)
            lexical = Path(os.path.abspath(raw))
            resolved = _resolved(raw)
            if absolute and (_overlaps(lexical, root) or _overlaps(resolved, root)):
                raise InputError("source_reference_in_environment")
            if not absolute and (not _within(lexical, root) or not _within(resolved, root)):
                raise InputError("search_path_escapes_capture")
            if absolute and resolved not in inspected and resolved.is_dir():
                inspected.add(resolved)
                with os.scandir(resolved) as children:
                    for child in children:
                        entries += 1
                        if entries > MAX_FILES * 2:
                            raise InputError("search_alias_limit")
                        if child.is_symlink():
                            target = _resolved(child.path)
                            try:
                                overlaps = _overlaps(target, root)
                                if not overlaps:
                                    target.stat()
                            except (OSError, PathIdentityError) as exc:
                                if exc.errno not in (errno.ENOENT, errno.ENOTDIR, errno.ENAMETOOLONG,
                                                     errno.EACCES, errno.EPERM, errno.ELOOP):
                                    raise
                                failed = exc.path if isinstance(exc, PathIdentityError) else exc.filename or target
                                diagnostics.add(name, child.path, failed, exc.errno)
                                continue
                            if overlaps:
                                raise InputError("source_reference_in_environment")


def _check_executable(argv0, env, cwd, root):
    if "/" in argv0:
        return
    unavailable = None
    for component in env.get("PATH", os.defpath).split(":"):
        candidate = Path(component) / argv0
        if not candidate.is_absolute():
            candidate = cwd / candidate
        target = _resolved(candidate)
        try:
            identity = _object_id(target)
        except PathIdentityError as exc:
            unavailable = unavailable or exc
            continue
        if identity is not None and target.is_file() and os.access(candidate, os.X_OK):
            if os.path.isabs(component) and _within(target, root):
                raise InputError("source_reference_in_environment")
            return
    if unavailable is not None:
        raise unavailable


class SearchDiagnostics:
    def __init__(self):
        self.records = []
        self.bytes = self.omitted = 0

    def add(self, variable, entry, path, number):
        record = {"variable": variable, "entry": os.fspath(entry),
                  "path": os.fspath(path), "errno": number}
        size = len(canonical(record))
        if len(self.records) >= 64 or self.bytes + size > 65536:
            self.omitted += 1
        else:
            self.records.append(record)
            self.bytes += size

    def save(self, state, job):
        if not self.records and not self.omitted:
            return
        with directory(state / "input-diagnostics", create=True) as parent:
            info = os.fstat(parent)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise InputError("unsafe_input_diagnostics")
            fd = os.open(job + ".json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW,
                         0o600, dir_fd=parent)
            with os.fdopen(fd, "wb") as output:
                output.write(canonical({"version": "fleet-input-diagnostics@1", "job": job,
                                        "unselected_aliases": self.records, "omitted": self.omitted}))
                output.flush()
                os.fsync(output.fileno())
            os.fsync(parent)
        fsync_dir(state)


def execution_environment(execution, cwd):
    if (execution.get("context_version") != CONTEXT_VERSION
            or execution.get("context") != CONTEXT):
        raise InputError("unsupported_execution_context")
    return dict(execution["environment"], PWD=str(cwd))


def _git(root, *args):
    env = {"PATH": os.defpath, "LANG": "C", "LC_ALL": "C", "HOME": "/nonexistent",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0",
           "GIT_NO_LAZY_FETCH": "1", "GIT_ATTR_NOSYSTEM": "1"}
    child = None
    try:
        child = subprocess.Popen(["git", "--no-optional-locks", "-c", "core.fsmonitor=false",
                                 "-c", "core.hooksPath=/dev/null", "-c", "core.untrackedCache=false",
                                 "-C", str(root), *args], env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        data = bytearray()
        deadline = time.monotonic() + 10
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([child.stdout], [], [], remaining)[0]:
                raise InputError("git_inspection_timeout")
            chunk = os.read(child.stdout.fileno(), 65536)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > 8 * 1024 * 1024:
                raise InputError("git_metadata_limit")
        if child.wait(timeout=max(0.001, deadline - time.monotonic())):
            raise InputError("git_inspection_failed")
        return bytes(data)
    except (OSError, subprocess.SubprocessError):
        raise InputError("git_inspection_failed") from None
    finally:
        if child is not None:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)
            child.stdout.close()


def collisions(paths):
    seen = {}
    for path in paths:
        parts = path.split("/")
        for end in range(1, len(parts) + 1):
            prefix = "/".join(parts[:end])
            folded = unicodedata.normalize("NFC", prefix).casefold()
            if folded in seen and seen[folded] != prefix:
                raise InputError("path_collision")
            seen[folded] = prefix


def require_absent(root, path):
    with directory(root) as start:
        fd = os.dup(start)
        try:
            parts = path.split("/")
            for index, part in enumerate(parts):
                try:
                    info = os.stat(part, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    return
                if index == len(parts) - 1:
                    raise InputError("artifact_preexisting_or_overlapping")
                if not stat.S_ISDIR(info.st_mode):
                    raise InputError("unsupported_file_type")
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
        finally:
            os.close(fd)


def _origin(cwd):
    with directory(cwd):
        pass
    for parent in _ancestors(cwd):
        marker = parent / ".git"
        if os.path.lexists(marker):
            if marker.is_symlink() or not (marker.is_file() or marker.is_dir()):
                raise InputError("invalid_git_metadata")
            top = Path(os.fsdecode(_git(cwd, "rev-parse", "--show-toplevel")).strip())
            if _object_id(top) != _object_id(parent):
                raise InputError("invalid_git_root")
            return parent, True
    return cwd, False


def _git_inventory(root):
    head = _git(root, "rev-parse", "--verify", "HEAD").strip().decode("ascii")
    index = _git(root, "ls-files", "--stage", "-z")
    names = set()
    for entry in index.split(b"\0"):
        if not entry:
            continue
        meta, name = entry.split(b"\t", 1)
        if meta.split()[0] == b"160000":
            raise InputError("submodule_not_supported")
        names.add(os.fsdecode(name))
    names.update(os.fsdecode(n) for n in
                 _git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if n)
    return head, index, names


def _private(path, extra, state, root):
    import fnmatch
    parts = path.split("/")
    if any(p in VCS for p in parts):
        return True
    if _within(root / path, state):
        return True
    if any(_within(root / path, root / p) for p in extra):
        return True
    return any(fnmatch.fnmatchcase(part, pattern) for part in parts for pattern in DEFAULT_PRIVATE
               if "/" not in pattern) or any(
                   path == p or path.startswith(p + "/") for p in DEFAULT_PRIVATE if "/" in p)


def _walk(fd, prefix="", excluded=None, budget=None):
    budget = [0] if budget is None else budget
    names = []
    with os.scandir(fd) as entries:
        for entry in entries:
            names.append(entry.name)
            if len(names) + budget[0] > MAX_FILES * 2:
                raise InputError("inventory_limit")
    for name in sorted(names):
        path = prefix + name
        budget[0] += 1
        if budget[0] > MAX_FILES * 2 or path.count("/") > 128 or len(path) > 2048:
            raise InputError("inventory_limit")
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        yield path, info
        if (stat.S_ISDIR(info.st_mode) and name not in VCS
                and not (excluded and excluded(path))):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW, dir_fd=fd)
            try:
                yield from _walk(child, path + "/", excluded, budget)
            finally:
                os.close(child)


class Plan:
    def __init__(self, cwd, state, includes, private, artifacts, argv, env, credentials):
        self.cwd = Path(os.path.abspath(cwd))
        self.state = state
        _check_scope(self.cwd, state, env)
        self.root, self.git = _origin(self.cwd)
        _check_scope(self.root, state, env)
        self.cwd_rel = self.cwd.relative_to(self.root).as_posix()
        self.includes = [relative(p, True) for p in includes]
        self.private = [relative(p) for p in private]
        self.artifacts = [relative(p) for p in artifacts]
        if len(self.artifacts) > MAX_FILES:
            raise InputError("artifact_count_limit")
        self.secrets = credential_values(env, credentials)
        if any(secret in os.fsencode(path) for secret in self.secrets
               for path in [str(state), self.cwd_rel, *self.artifacts]):
            raise InputError("declared_credential_in_path")
        self.policy = policy_summary(self.private, self.secrets)
        if any(name.startswith("FLEET_CONTEXT_") for name in env):
            raise InputError("reserved_environment")
        if any(_source_reference(arg, self.root, whole_path=True) for arg in argv):
            raise InputError("source_reference_in_command")
        self.diagnostics = SearchDiagnostics()
        _check_search_paths(env, self.cwd, self.root, self.diagnostics)
        _check_executable(argv[0], env, self.cwd, self.root)
        for include in self.includes:
            if _private(include, self.private, state, self.root):
                raise InputError("private_input")
        self.provenance = _git_inventory(self.root) if self.git else (None, None, None)
        self.files = self.inventory()
        self.records = self.scan()
        for artifact in self.artifacts:
            rel = artifact if self.cwd_rel == "." else self.cwd_rel + "/" + artifact
            if _private(rel, self.private, state, self.root):
                raise InputError("private_artifact_path")
            require_absent(self.root, rel)
            if any(
                    name == rel or name.startswith(rel + "/") or rel.startswith(name + "/")
                    for name in self.files):
                raise InputError("artifact_preexisting_or_overlapping")
        folded = [unicodedata.normalize("NFC", a).casefold() for a in self.artifacts]
        collisions([*self.files, *(a if self.cwd_rel == "." else self.cwd_rel + "/" + a
                                   for a in self.artifacts)])
        if len(folded) != len(set(folded)) or any(
                a != b and (a.startswith(b + "/") or b.startswith(a + "/"))
                for a in folded for b in folded):
            raise InputError("artifact_collision")

    def selected(self, path):
        return not self.includes or any(p == "." or path == p or path.startswith(p + "/")
                                        for p in self.includes)

    def inventory(self):
        result = {}
        candidates = self.provenance[2]
        candidate_dirs = set()
        for path in candidates or ():
            parts = path.split("/")
            candidate_dirs.update("/".join(parts[:i]) for i in range(1, len(parts)))

        def excluded(path):
            relevant = not self.includes or any(
                p == "." or p == path or p.startswith(path + "/") or path.startswith(p + "/")
                for p in self.includes)
            return (not relevant or _private(path, self.private, self.state, self.root)
                    or (candidates is not None and path not in candidates and path not in candidate_dirs))

        with directory(self.root) as fd:
            for path, info in _walk(fd, excluded=excluded):
                if not self.selected(path):
                    continue
                is_private = _private(path, self.private, self.state, self.root)
                if is_private:
                    if path in self.includes:
                        raise InputError("private_input")
                    continue
                if stat.S_ISDIR(info.st_mode):
                    continue
                if candidates is not None and path not in candidates:
                    if path in candidate_dirs:
                        raise InputError("unsupported_file_type")
                    if path in self.includes:
                        raise InputError("ignored_input")
                    continue
                relative(path)
                if not stat.S_ISREG(info.st_mode):
                    raise InputError("unsupported_file_type")
                result[path] = stamp(info)
                if len(result) > MAX_FILES:
                    raise InputError("input_count_limit")
        for include in self.includes:
            if not any(include == "." or p == include or p.startswith(include + "/") for p in result):
                raise InputError("input_not_admitted")
        collisions(result)
        return result

    def scan(self, destination=None):
        records, total = [], 0
        with directory(self.root) as fd:
            for path in sorted(self.files):
                if any(secret in os.fsencode(path) for secret in self.secrets):
                    raise InputError("declared_credential_in_path")
                with open_file(fd, path) as source:
                    data, info = read_stable(source, self.secrets)
                if _source_reference(os.fsdecode(data), self.root):
                    raise InputError("source_reference_in_content")
                if stamp(info) != self.files[path]:
                    raise InputError("source_changed")
                total += len(data)
                if total > MAX_BYTES:
                    raise InputError("input_bytes_limit")
                mode = 0o755 if info.st_mode & 0o111 else 0o644
                records.append({"path": path, "mode": mode, "size": len(data), "sha256": digest(data)})
                if destination is not None:
                    target = destination / path
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    write_new(target, data, mode)
                    os.chmod(target, mode)
                    os.utime(target, (0, 0))
        return records

    def capture(self, capsule):
        try:
            return self._capture(capsule)
        except OSError:
            raise InputError("source_changed") from None

    def _capture(self, capsule):
        capsule.mkdir(mode=0o700)
        payload = capsule / "payload"
        payload.mkdir(mode=0o700)
        records = self.scan(payload)
        if (records != self.records or self.inventory() != self.files
                or self.scan() != records
                or (self.git and _git_inventory(self.root) != self.provenance)):
            raise InputError("source_changed")
        identity = {"contract": VERSION, "commit_sha": self.provenance[0],
                    "effective_sha256": digest(canonical(records)), "cwd_rel": self.cwd_rel}
        manifest = {"identity": identity, "files": records, "normalization": "exec-bit;mtime=0@1"}
        data = canonical(manifest)
        if len(data) > MAX_FILE:
            raise InputError("manifest_limit")
        write_new(capsule / "manifest.json", data)
        sync_tree(payload)
        fsync_dir(capsule)
        fsync_dir(capsule.parent)
        self.diagnostics.save(self.state, capsule.name)
        return identity


def sync_tree(root):
    for base, dirs, files in os.walk(root, topdown=False):
        with directory(base) as fd:
            for name in files:
                with open_file(fd, name) as source:
                    os.fsync(source)
        os.utime(base, (0, 0))
        fsync_dir(base)


def materialize(capsule, workspace, identity):
    with directory(capsule) as fd:
        with open_file(fd, "manifest.json") as source:
            data, _ = read_stable(source)
    manifest = json.loads(data)
    if manifest["identity"] != identity or digest(canonical(manifest["files"])) != identity["effective_sha256"]:
        raise InputError("content_mismatch")
    workspace.mkdir(mode=0o700)
    with directory(capsule / "payload") as fd:
        actual = [p for p, info in _walk(fd) if not stat.S_ISDIR(info.st_mode)]
        if sorted(actual) != [r["path"] for r in manifest["files"]]:
            raise InputError("content_mismatch")
        for record in manifest["files"]:
            with open_file(fd, record["path"]) as source:
                content, info = read_stable(source)
            mode = 0o755 if info.st_mode & 0o111 else 0o644
            if (len(content), digest(content), mode) != (record["size"], record["sha256"], record["mode"]):
                raise InputError("content_mismatch")
            target = workspace / record["path"]
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_new(target, content, mode)
            os.chmod(target, mode)
            os.utime(target, (0, 0))
    cwd = workspace / identity["cwd_rel"]
    cwd.mkdir(mode=0o700, parents=True, exist_ok=True)
    sync_tree(workspace)
    with directory(workspace) as fd:
        records = []
        for path, info in _walk(fd):
            if stat.S_ISDIR(info.st_mode):
                continue
            with open_file(fd, path) as source:
                content, info = read_stable(source)
            records.append({"path": path, "mode": 0o755 if info.st_mode & 0o111 else 0o644,
                            "size": len(content), "sha256": digest(content)})
    if digest(canonical(sorted(records, key=lambda r: r["path"]))) != identity["effective_sha256"]:
        raise InputError("content_mismatch")
    return cwd
