"""Private effective-content equality and transactional historical admission."""
import os
from pathlib import Path
import pwd
import shutil
import stat

from . import inputs, native
from .model import CONTENT, Fault, canonical, dump, key, load, now, opaque

MAX_METADATA = 1024 * 1024


def normalized(values, dot=False):
    return sorted({inputs.relative(value, dot) for value in values}, key=os.fsencode)


def describe(plan, argv, environment, client, credentials):
    env = dict(environment)
    raw_pwd_secret = environment.get("PWD") if "PWD" in credentials else None
    # Only a present PWD is transformed; absent is distinct from empty.
    if "PWD" in env:
        env["PWD"] = {"materialized_cwd": plan.cwd_rel}
    value = {"contract": CONTENT, "execution_version": "fleet-execution@1",
             "context": inputs.CONTEXT, "input_version": inputs.VERSION,
             "normalization": "exec-bit;mtime=0@1", "cwd_rel": plan.cwd_rel,
             "commit_sha": plan.provenance[0], "files": plan.records,
             "selection": plan.includes or ["."], "private": plan.private,
             "default_private": list(inputs.DEFAULT_PRIVATE),
             "argv": argv, "environment": env, "credential_pwd": raw_pwd_secret,
             "credential_names": credentials, "credential_policy": "known-and-declared@1",
             "outputs": {"contract": "fleet-outputs@1", "required": plan.artifacts},
             "client": client, "policy": {"slots": 1, "queue": "FIFO", "interruptible": False}}
    extra = {k: v for k, v in value.items() if k not in ("argv", "environment", "credential_pwd")}
    if len(canonical(extra)) > MAX_METADATA or len(canonical(
            {"argv": argv, "environment": env, "credential_pwd": raw_pwd_secret})) > MAX_METADATA:
        raise Fault("submission_metadata_limit")
    return value


def manifest(state, capsule, expected):
    try:
        with inputs.directory(state / "capsules" / capsule) as fd:
            with inputs.open_file(fd, "manifest.json") as source:
                data, _ = inputs.read_stable(source)
        value = load(data)
        if (value["normalization"] != "exec-bit;mtime=0@1" or
                value["identity"] != expected or
                inputs.digest(canonical(value["files"])) != expected["effective_sha256"]):
            raise Fault("content_unavailable", 3, "unknown")
        return value
    except (OSError, ValueError, KeyError, TypeError):
        raise Fault("content_unavailable", 3, "unknown") from None


def equal(db, existing, candidate, content, identity):
    """Validate both capsules, then compare actual bytes, even with matching hashes."""
    old_input = load(existing["input"])
    left = manifest(db.state, existing["capsule"], old_input)
    right = manifest(db.state, candidate, identity)
    for capsule, value in ((existing["capsule"], left), (candidate, right)):
        with inputs.directory(db.state / "capsules" / capsule / "payload") as fd:
            actual = [path for path, info in inputs._walk(fd) if not stat.S_ISDIR(info.st_mode)]
            if sorted(actual) != [record["path"] for record in value["files"]]:
                raise Fault("content_unavailable", 3, "unknown")
            for record in value["files"]:
                with inputs.open_file(fd, record["path"]) as source:
                    data, info = inputs.read_stable(source)
                mode = 0o755 if info.st_mode & 0o111 else 0o644
                if (len(data), inputs.digest(data), mode) != (
                        record["size"], record["sha256"], record["mode"]):
                    raise Fault("content_unavailable", 3, "unknown")
    if load(existing["content"]) != content or left != right:
        return False
    with inputs.directory(db.state / "capsules" / existing["capsule"] / "payload") as a:
        with inputs.directory(db.state / "capsules" / candidate / "payload") as b:
            for record in left["files"]:
                with inputs.open_file(a, record["path"]) as source:
                    first, _ = inputs.read_stable(source)
                with inputs.open_file(b, record["path"]) as source:
                    second, _ = inputs.read_stable(source)
                if first != second:
                    return False
    return True


def receipt(db, row, binding):
    return {"job": row["id"], "attempt": row["attempt"], "accepted": True,
            "binding": binding, "admission": load(row["admission"]),
            "input": load(row["input"]), "observation": None}


def retire_preparation(db, preparation, owner):
    """Only the creator retires its resolved, unreferenced physical preparation.

    Unresolved/captured/bound preparations and any failed retirement are retained.
    No replay client, reader, recover or operator sweeps another preparation.
    """
    try:
        with db.transaction():
            row = db.execute("SELECT * FROM preparations WHERE id=?", (preparation,)).fetchone()
            referenced = db.execute("SELECT 1 FROM jobs WHERE capsule=?", (preparation,)).fetchone()
            if (not row or row["owner"] != dump(owner) or row["status"] != "resolved" or referenced
                    or not native.confirmed(owner)):
                return
            path = db.state / "capsules" / preparation
            with inputs.directory(path) as fd:
                original = os.fstat(fd)
                if original.st_uid != os.getuid() or original.st_mode & 0o077:
                    raise Fault("preparation_identity_unconfirmed")
                inventory = list(inputs._walk(fd))
                for relative, info in inventory:
                    if (info.st_uid != os.getuid() or not
                            (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))):
                        raise Fault("preparation_identity_unconfirmed")
            with inputs.directory(path) as fd:
                current = os.fstat(fd)
                if inputs.stamp(current) != inputs.stamp(original):
                    raise Fault("preparation_identity_unconfirmed")
                if [(p, inputs.stamp(i)) for p, i in inputs._walk(fd)] != [
                        (p, inputs.stamp(i)) for p, i in inventory]:
                    raise Fault("preparation_identity_unconfirmed")
            shutil.rmtree(path)
            inputs.fsync_dir(path.parent)
            db.execute("UPDATE preparations SET status='retired' WHERE id=?", (preparation,))
    except (OSError, ValueError):
        # No cleanup fallback, no public path or content diagnostic.
        pass


def admit(db, argv, cwd, client, includes, private, artifacts, credentials, idempotency_key,
          enqueue_only):
    if idempotency_key is not None:
        key(idempotency_key)
    environment = dict(os.environ)
    if len(canonical({"argv": argv, "environment": environment})) > MAX_METADATA:
        raise Fault("submission_metadata_limit")
    includes, private, artifacts = (normalized(includes, True), normalized(private), normalized(artifacts))
    credentials = sorted(set(credentials), key=os.fsencode)
    if "." in includes:
        includes = ["."]
    try:
        plan = inputs.Plan(cwd, db.state, includes, private, artifacts, argv, environment, credentials)
    except OSError:
        raise Fault("input_unavailable") from None
    if idempotency_key and any(secret in idempotency_key.encode() for secret in plan.secrets):
        raise Fault("credential_in_key")
    content = describe(plan, argv, environment, client, credentials)
    preparation = opaque("j")
    owner = native.identity(os.getpid())
    if not native.verifiable(owner):
        raise Fault("launcher_identity_unverifiable")
    with db.transaction():
        db.execute("INSERT INTO preparations VALUES (?,?, 'capturing',NULL)",
                   (preparation, dump(owner)))
    # The creator identity is durable before the first capsule directory appears.
    identity = plan.capture(db.state / "capsules" / preparation)
    execution = {"contract": "fleet-execution@1", "input": identity, "argv": argv,
                 "environment": environment, "outputs": content["outputs"],
                 "context_version": inputs.CONTEXT_VERSION, "context": inputs.CONTEXT,
                 "credential_names": credentials}
    conflict = None
    result = None
    with db.transaction():
        existing = None if idempotency_key is None else db.execute(
            "SELECT * FROM jobs WHERE uid=? AND key=?", (os.getuid(), idempotency_key)).fetchone()
        if existing is not None:
            try:
                same = equal(db, existing, preparation, content, identity)
            except (OSError, ValueError, KeyError, TypeError):
                raise Fault("content_unavailable", 3, "unknown") from None
            db.execute("UPDATE preparations SET status='resolved' WHERE id=?", (preparation,))
            if same:
                result = receipt(db, existing, "replay")
            else:
                conflict = Fault("idempotency_conflict", 5, "no_new_admission",
                                 authority_id=db.authority["id"], principal_uid=db.authority["uid"],
                                 key_binding="bound", existing={"job": existing["id"],
                                                                "attempt": existing["attempt"]})
        else:
            job, attempt = opaque("j"), opaque("a")
            revision = db.revision()
            submitted_by = dict(owner, user=pwd.getpwuid(os.getuid()).pw_name)
            db.execute("INSERT INTO jobs (id,uid,key,attempt,capsule,content,execution,input,client,"
                       "submitted_by,admission) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (job, os.getuid(), idempotency_key, attempt, preparation, dump(content),
                        dump(execution), dump(identity), inputs.redact(client, plan.secrets),
                        dump(submitted_by), "{}"))
            seq = db.execute("SELECT seq FROM jobs WHERE id=?", (job,)).fetchone()[0]
            admission = {"seq": seq, "revision": revision, "accepted_at": now(), "state": "queued"}
            db.execute("UPDATE jobs SET admission=? WHERE id=?", (dump(admission), job))
            (db.state / "attempts" / attempt).mkdir(mode=0o700)
            inputs.fsync_dir(db.state / "attempts")
            db.execute("INSERT INTO attempts (id,job,state,gate,collection,publication,"
                       "relevant_revision,enqueue_only) VALUES (?,?,'queued','available',?,?,?,?)",
                       (attempt, job, dump({"state": "pending", "errors": []}),
                        dump({"state": "pending", "root_sha256": None}), revision, int(enqueue_only)))
            db.execute("INSERT INTO transitions (revision,job,attempt,from_state,to_state,actor,cause,"
                       "generation,at) VALUES (?,?,?,NULL,'queued',?,'admission',NULL,?)",
                       (revision, job, attempt, "client:" + inputs.redact(client, plan.secrets), now()))
            db.execute("UPDATE preparations SET status='bound' WHERE id=?", (preparation,))
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
            result = receipt(db, row, "new" if idempotency_key is not None else "unkeyed")
    if result is None or result["binding"] == "replay":
        retire_preparation(db, preparation, owner)
    if conflict:
        raise conflict
    return result
