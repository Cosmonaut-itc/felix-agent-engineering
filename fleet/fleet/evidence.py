import hashlib
import json
import os
from pathlib import Path
import selectors

from . import inputs

VERSION = "fleet-evidence@1"
MAX_STREAM = 256 * 1024 * 1024


class Redactor:
    def __init__(self, secrets):
        self.secrets = secrets
        self.keep = max((len(s) for s in secrets), default=1) - 1
        self.pending = b""

    def feed(self, chunk, final=False):
        self.pending += chunk
        result = bytearray()
        while self.pending:
            safe = len(self.pending) if final else len(self.pending) - self.keep
            if safe <= 0:
                break
            matches = [(self.pending.find(s), s) for s in self.secrets]
            matches = [(pos, s) for pos, s in matches if 0 <= pos < safe]
            if matches:
                pos, secret = min(matches, key=lambda item: (item[0], -len(item[1])))
                result.extend(self.pending[:pos])
                result.extend(b"[REDACTED]")
                self.pending = self.pending[pos + len(secret):]
            else:
                result.extend(self.pending[:safe])
                self.pending = self.pending[safe:]
        return bytes(result)


class Capture:
    def __init__(self, directory, cwd, artifacts, secrets):
        self.directory = directory
        self.cwd = cwd
        self.artifacts = artifacts
        self.secrets = secrets
        self.errors = []
        self.logs = {}
        self.written = {name: {"size": 0, "hash": hashlib.sha256()} for name in ("stdout", "stderr")}
        self.closed = {}
        self.stream_bytes = {"stdout": 0, "stderr": 0}
        for artifact in artifacts:
            target = cwd / artifact
            if os.path.lexists(target):
                raise inputs.InputError("artifact_preexisting")
            with inputs.directory(target.parent, create=True):
                pass
        try:
            for name in ("stdout", "stderr"):
                self.logs[name] = os.open(directory / name,
                                          os.O_WRONLY | os.O_CREAT | os.O_EXCL | inputs.NOFOLLOW,
                                          0o600)
        except OSError:
            self.close()
            raise

    def error(self, code):
        if code not in self.errors:
            self.errors.append(code)

    def write(self, name, chunk):
        self.stream_bytes[name] += len(chunk)
        if self.stream_bytes[name] > MAX_STREAM:
            self.error("stream_limit")
            return
        if "stream_write_failed" in self.errors:
            return
        try:
            view = memoryview(chunk)
            while view:
                count = os.write(self.logs[name], view)
                if count <= 0:
                    raise OSError("short_write")
                self.written[name]["hash"].update(view[:count])
                self.written[name]["size"] += count
                view = view[count:]
        except OSError:
            self.error("stream_write_failed")

    def observe(self, child, draining):
        filters = {name: Redactor(self.secrets) for name in self.logs}
        selector = selectors.DefaultSelector()
        notified = False
        try:
            for name in ("stdout", "stderr"):
                pipe = getattr(child, name)
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            while selector.get_map():
                for key, mask in selector.select(0.2):
                    name = key.data
                    try:
                        chunk = os.read(key.fileobj.fileno(), 4096)
                    except OSError:
                        self.error("stream_read_failed")
                        chunk = b""
                    self.write(name, filters[name].feed(chunk, final=not chunk))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        try:
                            key.fileobj.close()
                        except OSError:
                            self.error("pipe_close_failed")
                if child.poll() is not None and not notified:
                    draining()
                    notified = True
        finally:
            selector.close()
            self.close()

    def close(self):
        for name, fd in list(self.logs.items()):
            try:
                os.fsync(fd)
                self.closed[name] = inputs.stamp(os.fstat(fd))
            except OSError:
                self.error("stream_fsync_failed")
            try:
                os.close(fd)
            except OSError:
                self.error("stream_close_failed")
            del self.logs[name]


def seal(directory, job, attempt, identity, command, capture=None, errors=()):
    errors = list(errors) + (list(capture.errors) if capture else [])
    bundle = directory / "bundle"
    entries = []
    try:
        bundle.mkdir(mode=0o700)
        targets = []
        if capture and command["kind"] != "never_created":
            targets += [(directory, name, name, "log") for name in ("stdout", "stderr")]
            targets += [(capture.cwd, path, "artifacts/" + path, "artifact") for path in capture.artifacts]
        for root, source_path, dest_path, kind in targets:
            try:
                with inputs.directory(root) as root_fd:
                    with inputs.open_file(root_fd, source_path) as source:
                        content, info = inputs.read_stable(
                            source, capture.secrets if kind == "artifact" else (),
                            limit=MAX_STREAM if kind == "log" else inputs.MAX_FILE)
                    with inputs.open_file(root_fd, source_path) as current:
                        if inputs.stamp(os.fstat(current)) != inputs.stamp(info):
                            raise ValueError("source_changed")
                if kind == "log" and (
                        inputs.stamp(info) != capture.closed.get(source_path)
                        or len(content) != capture.written[source_path]["size"]
                        or inputs.digest(content) != capture.written[source_path]["hash"].hexdigest()):
                    raise ValueError("log_transfer_mismatch")
                target = bundle / dest_path
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                inputs.write_new(target, content)
                written = target.stat()
                entries.append({"path": dest_path, "kind": kind, "size": len(content),
                                "sha256": inputs.digest(content), "device": written.st_dev,
                                "inode": written.st_ino, "mtime_ns": written.st_mtime_ns,
                                "ctime_ns": written.st_ctime_ns})
            except (OSError, ValueError):
                errors.append("artifact_collection_failed" if kind == "artifact" else "log_collection_failed")
        manifest = {"contract": VERSION, "job": job, "attempt": attempt, "input": identity,
                    "command": command, "collection": "valid" if not errors else "incomplete",
                    "errors": sorted(set(errors)), "entries": entries}
        data = inputs.canonical(manifest)
        inputs.write_new(bundle / "manifest.json", data)
        inputs.sync_tree(bundle)
        inputs.fsync_dir(directory)
        return inputs.digest(data), manifest
    except (OSError, ValueError):
        return None, {"collection": "incomplete", "errors": sorted(set(errors + ["bundle_seal_failed"]))}


def verify(directory, root_hash, job, attempt, identity, command):
    result = {"valid": False, "manifest": str(directory / "bundle/manifest.json"),
              "root_sha256": root_hash, "files": [], "errors": []}
    if root_hash is None:
        result["errors"] = ["evidence_not_sealed"]
        return result
    try:
        with inputs.directory(directory / "bundle") as fd:
            with inputs.open_file(fd, "manifest.json") as source:
                data, _ = inputs.read_stable(source)
            if inputs.digest(data) != root_hash:
                raise ValueError("manifest_mismatch")
            manifest = json.loads(data)
            if (manifest["contract"], manifest["job"], manifest["attempt"], manifest["input"],
                    manifest["command"]) != (VERSION, job, attempt, identity, command):
                raise ValueError("evidence_identity_mismatch")
            errors = list(manifest["errors"])
            for entry in manifest["entries"]:
                item = {"path": str(directory / "bundle" / entry["path"]),
                        "kind": entry["kind"], "size": entry["size"],
                        "sha256": entry["sha256"], "valid": False}
                result["files"].append(item)
                with inputs.open_file(fd, entry["path"]) as source:
                    content, info = inputs.read_stable(source, limit=MAX_STREAM)
                item["valid"] = (len(content), inputs.digest(content), info.st_dev, info.st_ino,
                                 info.st_mtime_ns, info.st_ctime_ns) == (
                                     entry["size"], entry["sha256"], entry["device"], entry["inode"],
                                     entry["mtime_ns"], entry["ctime_ns"])
                if not item["valid"]:
                    errors.append("payload_mismatch")
            result.update(valid=not errors and manifest["collection"] == "valid", errors=errors)
    except (OSError, ValueError, KeyError, TypeError):
        result["errors"].append("evidence_missing_or_mismatched")
    return result
