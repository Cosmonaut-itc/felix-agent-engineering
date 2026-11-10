#!/usr/bin/env python3
"""Installed entrypoint: python3 -I /fixed/install/deskpolicy/cli.py."""

import argparse
import contextlib
import ctypes
import fcntl
import gc
import json
import os
from pathlib import Path
import select
import subprocess
import signal
import stat
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskpolicy import VERSION
from deskpolicy.engine import DeskPolicy, admission_receipt, merge_receipt, validate_record
from deskpolicy.model import Invalid, finite_load, require, strict_json, timestamp, validate_policy
from deskpolicy.store import RECEIPT_DIR_MODES, Sample, atomic_json, mutex, open_file, read_json, secure_dir


def output(value):
    print(json.dumps(value, sort_keys=True, allow_nan=False), flush=True)


_WRITER_SIGNALS = {signal.SIGTERM, signal.SIGINT, signal.SIGHUP}
_WORKER_SIGNALS = _WRITER_SIGNALS | {signal.SIGALRM}
_FRAME_LIMIT = 128 * 1024
_PRCTL = None


class _WriterTransportError(BaseException):
    pass


def _parent_death_signal():
    global _PRCTL
    if _PRCTL is None:
        _PRCTL = ctypes.CDLL(None, use_errno=True).prctl
        _PRCTL.argtypes = [ctypes.c_int, *([ctypes.c_ulong] * 4)]
        _PRCTL.restype = ctypes.c_int
    return _PRCTL


def _writer_send(fd, kind, **payload):
    try:
        data = (json.dumps(dict(kind=kind, **payload), allow_nan=False) + "\n").encode()
        require(len(data) <= _FRAME_LIMIT, "writer-frame-size")
        while data:
            count = os.write(fd, data)
            require(count > 0, "writer-pipe-closed")
            data = data[count:]
    except Exception as exc:
        raise _WriterTransportError(str(exc)) from exc


def _writer_child(engine, writer_fd, channel, parent, deadline, mask, prctl):
    try:
        # A collected file object from the supervisor's heap would close a
        # descriptor number that this child may reuse for admission.lock.
        gc.disable()
        if prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
        if os.getppid() != parent:
            os._exit(75)
        for signum in _WORKER_SIGNALS:
            signal.signal(signum, signal.SIG_DFL)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _writer_send(channel, "error", detail="writer-cycle-timeout")
            os._exit(75)
        signal.setitimer(signal.ITIMER_REAL, remaining)
        signal.pthread_sigmask(signal.SIG_SETMASK, set(mask) - _WORKER_SIGNALS)
        # CLOEXEC does not close forked copies of open file descriptions.
        keep = {0, 1, 2, writer_fd, channel}
        for entry in os.listdir("/proc/self/fd"):
            fd = int(entry)
            if fd not in keep:
                with contextlib.suppress(OSError):
                    os.close(fd)
        engine.receipt_diagnostic = lambda key, value: _writer_send(
            channel, "receipt-diagnostic", key=key, value=value)
        engine.prepare()
        value = engine.publish(before_commit=lambda: _writer_send(channel, "commit-start"))
        _writer_send(channel, "result", value=value)
        signal.setitimer(signal.ITIMER_REAL, 0)
        os._exit(0)
    except Exception as exc:
        try:
            _writer_send(channel, "error", detail=str(exc))
        except BaseException:
            pass
    except BaseException:
        pass
    os._exit(75)


class _WriterMessages:
    def __init__(self):
        self.buffer = b""
        self.invalid = False
        self.committing = False
        self.result = None
        self.error = None
        self.diagnostics = {}

    def feed(self, data):
        if self.invalid:
            return
        self.buffer += data
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            try:
                require(len(line) + 1 <= _FRAME_LIMIT, "writer-frame-size")
                def pairs(items):
                    value = {}
                    for key, part in items:
                        require(key not in value, "writer-frame-duplicate")
                        value[key] = part
                    return value
                message = json.loads(line, object_pairs_hook=pairs)
                require(type(message) is dict, "writer-frame-type")
                strict_json(json.dumps(message.get("value", message), allow_nan=False).encode())
                kind = message.get("kind")
                if kind == "commit-start":
                    require(set(message) == {"kind"} and not self.committing
                            and self.result is None and self.error is None, "writer-frame-order")
                    self.committing = True
                elif kind == "result":
                    require(set(message) == {"kind", "value"} and self.committing
                            and self.result is None and self.error is None, "writer-frame-order")
                    value = message["value"]
                    require(type(value) is dict and value.get("schema") == "pao/desk-policy@1"
                            and type(value.get("revision")) is int, "writer-result-type")
                    validate_policy(value, Sample(timestamp(value["issued_at"]),
                        value["issued_boottime_s"], value["boot_id"]))
                    self.result = value
                elif kind == "error":
                    require(set(message) == {"kind", "detail"}
                            and type(message["detail"]) is str and self.error is None, "writer-error-type")
                    self.error = message["detail"]
                elif kind == "receipt-diagnostic":
                    require(set(message) == {"kind", "key", "value"} and not self.committing
                            and self.error is None and type(message["key"]) is list
                            and len(message["key"]) == 7
                            and all(type(part) is str for part in message["key"]), "writer-diagnostic-key")
                    value = message["value"]
                    require(type(value) is dict and set(value) == {
                        "schema", "request_id", "run_id", "reason", "detail"}
                        and all(type(part) is str for part in value.values())
                        and value["schema"] == "pao/desk-reconcile-diagnostic@1"
                        and value["reason"] == "receipt-error", "writer-diagnostic-type")
                    self.diagnostics[tuple(message["key"])] = value
                else:
                    raise Invalid("writer-frame-kind")
            except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
                self.invalid = True
                self.buffer = b""
                return
        if len(self.buffer) >= _FRAME_LIMIT:
            self.invalid = True
            self.buffer = b""

    def outcome(self, status, timed_out):
        unknown = self.committing or self.invalid or bool(self.buffer)
        if self.result is not None and not self.invalid and not self.buffer:
            return self.result
        suffix = "-publication-unknown" if unknown else ""
        if timed_out or (os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGALRM):
            detail = "writer-cycle-timeout" + suffix
        elif self.error is not None:
            detail = "writer-cycle-error" + suffix + ": " + self.error if unknown else self.error
        else:
            detail = "writer-cycle-worker-exit" + suffix + ": " + _worker_status(status)
        return {"schema": "pao/desk-error@1", "reason": "census-unknown",
                "detail": detail, "exit_code": 75}


def _worker_status(status):
    code = os.waitstatus_to_exitcode(status)
    if code >= 0:
        return "exit-" + str(code)
    try:
        return "signal-" + signal.Signals(-code).name
    except ValueError:
        return "signal-" + str(-code)


def writer_tick(engine, writer_fd, *, _timeout=10, _diagnostics=None):
    deadline = time.monotonic() + _timeout
    prctl = _parent_death_signal()
    parent = os.getpid()
    child = None
    spawned = False
    reader = writer = None
    messages = _WriterMessages()
    timed_out = False
    eof = False
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, _WORKER_SIGNALS)
    previous_chld = signal.getsignal(signal.SIGCHLD)
    try:
        try:
            # SIG_IGN auto-reaps on Linux, allowing PID reuse before our waitpid.
            signal.signal(signal.SIGCHLD, signal.SIG_DFL)
            reader, writer = os.pipe()
            child = os.fork()
            if child == 0:
                _writer_child(engine, writer_fd, writer, parent, deadline, mask, prctl)
                os._exit(75)
            spawned = True
            os.close(writer)
            writer = None
            os.set_blocking(reader, False)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)

        def reap(options):
            nonlocal child
            previous = signal.pthread_sigmask(signal.SIG_BLOCK, _WRITER_SIGNALS)
            try:
                pid, status = os.waitpid(child, options)
                if pid:
                    child = None
                    return status
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous)
            return None

        status = None
        while status is None:
            status = reap(os.WNOHANG)
            if status is not None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                with contextlib.suppress(ProcessLookupError):
                    os.kill(child, signal.SIGKILL)
                status = reap(0)
                break
            ready, _, _ = select.select([] if eof else [reader], [], [], min(remaining, .05))
            if ready:
                data = os.read(reader, 65536)
                if data:
                    messages.feed(data)
                else:
                    eof = True
        while not eof:
            data = os.read(reader, 65536)
            if not data:
                eof = True
            else:
                messages.feed(data)
        seen = _diagnostics if _diagnostics is not None else set()
        for key, value in messages.diagnostics.items():
            if key not in seen:
                try:
                    print(json.dumps(value, sort_keys=True), file=sys.stderr, flush=True)
                except OSError:
                    continue
                seen.add(key)
        return messages.outcome(status, timed_out)
    except (OSError, ValueError, TypeError) as exc:
        if not spawned:
            raise
        messages.invalid = True
        messages.error = str(exc)
        return messages.outcome(0, timed_out)
    finally:
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, _WRITER_SIGNALS)
        try:
            if child not in (None, 0):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(child, signal.SIGKILL)
                os.waitpid(child, 0)
                child = None
            for fd in (reader, writer):
                if fd is not None:
                    os.close(fd)
        finally:
            try:
                signal.signal(signal.SIGCHLD, previous_chld)
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _writer_loop(engine):
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        if not stopping:
            stopping = True
            raise SystemExit(128 + signum)

    handlers = {}
    diagnostics = set()
    try:
        for signum in _WRITER_SIGNALS:
            handlers[signum] = signal.signal(signum, stop)
        secure_dir(engine.root, create=True)
        with mutex(engine.root, name="writer.lock") as writer_fd:
            while True:
                started = time.monotonic()
                try:
                    value = writer_tick(engine, writer_fd, _diagnostics=diagnostics)
                except (OSError, ValueError, TypeError) as exc:
                    value = {"schema": "pao/desk-error@1", "reason": "census-unknown",
                             "detail": str(exc), "exit_code": 75}
                output(value)
                time.sleep(max(0, 20 - (time.monotonic() - started)))
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)


def parser():
    result = argparse.ArgumentParser(description="PAO desk policy " + VERSION)
    sub = result.add_subparsers(dest="command", required=True)
    publish = sub.add_parser("publicar")
    publish.add_argument("--una-vez", action="store_true")
    publish.add_argument("--desactivar-noche", action="store_true")
    pending = sub.add_parser("pendiente")
    pending.add_argument("--request-id", required=True)
    pending.add_argument("--project", choices=("jale", "hasito"), required=True)
    pending.add_argument("--kind", choices=("chain", "verify", "exclusive", "script"), required=True)
    pending.add_argument("--owner", required=True)
    pending.add_argument("--weight", type=int, required=True)
    pending.add_argument("--retirar", action="store_true")
    pending.add_argument("--lane", type=int, help="Carril fijo del lock local Hasito ya tomado")
    for name in ("admitir", "traspasar", "cerrar", "ejecutar"):
        item = sub.add_parser(name)
        item.add_argument("--request-id", required=True)
        item.add_argument("--run-id", required=True)
        if name in ("admitir", "ejecutar"):
            item.add_argument("--lane", type=lambda raw: "any" if raw == "any" else int(raw), required=True)
            item.add_argument("--local-limit", type=int)
        if name != "ejecutar":
            item.add_argument("--pid", type=int, required=True)
            item.add_argument("--mutex-fd", type=int, default=9)
            item.add_argument("--life-fd", type=int, default=8)
            if name == "admitir":
                item.add_argument("--life-fds", nargs="+", metavar="LANE:FD")
            if name in ("traspasar", "cerrar"):
                item.add_argument("--claim-token-fd", type=int, required=True)
            if name == "cerrar":
                item.add_argument("--script-exit-code", type=int)
                item.add_argument("--cause", choices=("completed", "stopped", "sync-timeout"), default="completed")
        else:
            item.add_argument("--recibo", action="store_true",
                              help="Guardar runs/<run-id>/pao-admission.json antes del guion")
            item.add_argument("--sync-ready-fd", type=int,
                              help="Pipe heredado: esperar byte 1 del transporte antes del guion")
            item.add_argument("--sync-timeout-s", type=float, default=900,
                              help="Plazo de sync tras admitir, en segundos (máximo y omisión: 900, 15 min)")
            item.add_argument("argv", nargs=argparse.REMAINDER)
    sub.add_parser("reconciliar")
    status = sub.add_parser("estado")
    status.add_argument("--request-id")
    preflight = sub.add_parser("preflight")
    preflight.add_argument("--preparar", action="store_true")
    preflight.add_argument("--armar", action="store_true")
    preflight.add_argument("--evidencia", type=Path)
    return result


def wait_sync(fd, deadline, stopped):
    while not stopped():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        ready, _, _ = select.select([fd], [], [], min(remaining, .2))
        if ready:
            signal_byte = os.read(fd, 1)
            if signal_byte == b"1":
                return time.monotonic() < deadline and not stopped()
            return False
    return False


def execute(engine, args):
    if args.argv and args.argv[0] == "--":
        args.argv.pop(0)
    if not args.argv:
        raise Invalid("command-required")
    require(not args.argv[0].startswith("-"), "command-executable")
    require(finite_load(args.sync_timeout_s) and 0 < args.sync_timeout_s <= 900, "sync-timeout")
    if args.sync_ready_fd is not None:
        require(args.sync_ready_fd >= 0 and stat.S_ISFIFO(os.fstat(args.sync_ready_fd).st_mode),
                "sync-pipe-required")
        require(fcntl.fcntl(args.sync_ready_fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_WRONLY,
                "sync-pipe-readable")
    supervisor = os.getsid(0)
    runner_group = os.getpgrp()
    require(supervisor in (os.getpid(), os.getppid()) and runner_group == supervisor,
            "wrapper-session-required")
    # remote-run kills its process group. Keep the control process alive to
    # record the script's signal exit; only the script joins the runner group.
    if supervisor != os.getpid():
        os.setpgid(0, 0)
    receipt_path = engine.runtime.receipt_path(args.run_id)
    if args.recibo:
        secure_dir(receipt_path.parent, modes=RECEIPT_DIR_MODES)
    life = None
    code = None
    sync_timed_out = False
    previous_handlers = {}

    def bind_life(path):
        nonlocal life
        life = open_file(path, create=True)
        return life

    def release_life():
        nonlocal life
        if life is not None:
            fd, life = life, None
            os.close(fd)

    def diagnostic(exc):
        value = {"schema": "pao/desk-close-diagnostic@1", "reason": "close-unproven"
                 if str(exc) == "close-unproven" else "close-error",
                 "detail": str(exc), "script_exit_code": code}
        print(json.dumps(value, sort_keys=True), file=sys.stderr, flush=True)
        return value

    def publish_receipt(value):
        # All callers hold the admission mutex, including retries after a
        # failed close. Preserve the admitted identity and any known result.
        if not args.recibo:
            return value
        try:
            prior = read_json(receipt_path, directory_modes=RECEIPT_DIR_MODES)
        except FileNotFoundError:
            prior = None
        if prior and prior["state"] == "admitted":
            value = merge_receipt(prior, value)
        atomic_json(receipt_path, value, directory_modes=RECEIPT_DIR_MODES)
        return value

    def record_result(error=None):
        record = validate_record(read_json(engine.record_path(args.request_id)), args.request_id)
        claim = record["claim"]
        require(claim is not None and claim["run_id"] == args.run_id, "request-conflict")
        if claim["state"] != "closed":
            claim.update(state="uncertain", script_exit_code=code)
            engine.write_record(record)
        receipt.update(claim_state=claim["state"], script_exit_code=code,
                       close_cause=claim["close_cause"])
        if error is not None:
            receipt["close_error"] = error
        return receipt
    try:
        with mutex(engine.root) as lock:
            previous = None
            if args.recibo:
                try:
                    previous = read_json(receipt_path, directory_modes=RECEIPT_DIR_MODES)
                except FileNotFoundError:
                    pass
                if previous is not None:
                    require(type(previous) is dict and previous.get("schema") == "pao/desk-admission@1"
                            and previous.get("state") in ("admitted", "denied"), "receipt-conflict")
                    require(previous["request_id"] == args.request_id and previous["run_id"] == args.run_id,
                            "receipt-conflict")
                    if previous["state"] == "admitted":
                        current = read_json(engine.record_path(args.request_id))
                        require(current["state"] == "admitted" and current["admitted_run_id"] == args.run_id,
                                "receipt-conflict")
            receipt = engine.admit(args.request_id, args.run_id, args.lane, supervisor,
                                   bind_life, lock, args.local_limit)
            sync_deadline = time.monotonic() + args.sync_timeout_s
            publish_receipt(receipt)
            if not receipt["execute"]:
                release_life()
        output({**receipt, "claim_token": None})
        if not receipt["execute"]:
            return 75 if receipt["state"] == "denied" else 0
        stopped = None
        child = None

        def stop(signum, frame):
            nonlocal stopped
            stopped = signum
            if child is not None and child.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    child.send_signal(signum)

        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            previous_handlers[signum] = signal.signal(signum, stop)
        command = ["/usr/bin/taskset", "-c", ",".join(map(str, receipt["cpus"])), *args.argv]
        if args.sync_ready_fd is not None:
            sync_timed_out = not wait_sync(args.sync_ready_fd, sync_deadline, lambda: stopped)
        if stopped is None and not sync_timed_out:
            child = subprocess.Popen(command, close_fds=True,
                                     preexec_fn=lambda: os.setpgid(0, runner_group))
            if stopped is not None:
                stop(stopped, None)
            code = child.wait()
            if code < 0:
                stopped = stopped or -code
                code = 128 - code
        elif stopped is not None:
            code = 128 + stopped
        close_error = None
        try:
            with mutex(engine.root) as lock:
                try:
                    record = engine.close(args.request_id, args.run_id, os.getpid(), life, lock,
                                          claim_token=receipt["claim_token"], script_exit_code=code,
                                          cause="stopped" if stopped else
                                          "sync-timeout" if sync_timed_out else "completed")
                    receipt.update(claim_state=record["claim"]["state"], script_exit_code=code,
                                   close_cause=record["claim"]["close_cause"])
                finally:
                    # No censo can observe a closed claim with its life fd held.
                    release_life()
                publish_receipt(receipt)
        except Exception as exc:
            close_error = diagnostic(exc)
            receipt.update(claim_state="uncertain", script_exit_code=code, close_error=close_error)
            try:
                # A timed-out or failed transaction may not touch the claim
                # until it owns the mutex again. Retry only the result journal.
                with mutex(engine.root):
                    try:
                        record_result(close_error)
                    except Exception as journal_error:
                        diagnostic(journal_error)
                    finally:
                        release_life()
                    publish_receipt(receipt)
            except Exception as recovery_error:
                diagnostic(recovery_error)
        if not args.recibo:
            try:
                output({**receipt, "claim_token": None})
            except Exception as exc:
                diagnostic(exc)
        return 75 if sync_timed_out and stopped is None else code
    finally:
        for signum, handler in previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except Exception as exc:
                if code is None:
                    raise
                diagnostic(exc)
        try:
            release_life()
        except Exception as exc:
            if code is None:
                raise
            diagnostic(exc)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command in ("admitir", "traspasar", "cerrar"):
            engine = DeskPolicy(prepare=False)
            engine.prepare(inherited=args.mutex_fd)
        elif args.command == "publicar" and not (args.una_vez or args.desactivar_noche):
            engine = DeskPolicy(prepare=False)
        else:
            engine = DeskPolicy()
        if args.command == "pendiente":
            require(args.project != "hasito" or args.lane is not None, "hasito-lane-required")
            output(engine.register(args.request_id, args.project, args.kind, args.owner,
                                   args.weight, args.retirar, lane=args.lane))
        elif args.command == "admitir":
            life_fds = args.life_fd
            if args.life_fds:
                pairs = [tuple(map(int, raw.split(":"))) for raw in args.life_fds]
                require(all(len(pair) == 2 and 1 <= pair[0] <= 12 and pair[1] >= 0 for pair in pairs)
                        and len({pair[0] for pair in pairs}) == len(pairs), "life-fds")
                life_fds = dict(pairs)
            receipt = engine.admit(args.request_id, args.run_id, args.lane, args.pid,
                                   life_fds, args.mutex_fd, args.local_limit)
            output(receipt)
            return 75 if receipt["state"] == "denied" else 0
        elif args.command == "ejecutar":
            return execute(engine, args)
        elif args.command == "traspasar":
            token = os.read(args.claim_token_fd, 66).decode("ascii").strip()
            record = engine.handoff(args.request_id, args.run_id, args.pid, args.life_fd,
                                    args.mutex_fd, claim_token=token)
            output({"schema": "pao/desk-handoff@1", "request_id": args.request_id,
                    "run_id": args.run_id, "keeper": next(p for p in reversed(record["claim"]["keepers"])
                                                         if p["pid"] == args.pid)})
        elif args.command == "cerrar":
            token = os.read(args.claim_token_fd, 66).decode("ascii").strip()
            output(engine.close(args.request_id, args.run_id, args.pid, args.life_fd, args.mutex_fd,
                                claim_token=token, script_exit_code=args.script_exit_code, cause=args.cause))
        elif args.command == "reconciliar":
            output(engine.reconcile())
        elif args.command == "estado":
            output(engine.status(args.request_id))
        elif args.command == "preflight":
            if args.preparar:
                engine.prepare()
            evidence = read_json(args.evidencia) if args.evidencia else None
            output(engine.preflight(evidence, args.armar))
        elif args.command == "publicar":
            if args.desactivar_noche:
                output(engine.publish(disable_night=True))
                return 0
            if args.una_vez:
                output(engine.publish())
            else:
                _writer_loop(engine)
        return 0
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        if args.command == "admitir":
            try:
                policy, _ = engine.read_policy(engine.runtime.sample())
            except (OSError, ValueError, UnboundLocalError):
                policy = {"mode": "day", "revision": None}
            receipt = admission_receipt(args.request_id, args.run_id, policy, args.lane,
                                        reason="census-unknown")
            receipt["detail"] = str(exc)
            output(receipt)
            return 75
        reason = "weight-too-large" if str(exc) == "weight-too-large" else "census-unknown"
        output({"schema": "pao/desk-error@1", "reason": reason,
                "detail": str(exc), "exit_code": 75})
        return 75


if __name__ == "__main__":
    sys.exit(main())
