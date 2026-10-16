#!/usr/bin/env python3
"""Persistencia y supervisión del lanzador Bash codex-run."""

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time


def fail(message):
    raise ValueError(message)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        for original, translated in (
            ("the following arguments are required", "faltan argumentos obligatorios"),
            ("unrecognized arguments", "argumentos desconocidos"),
            ("expected one argument", "falta un valor"),
            ("invalid choice", "valor no permitido"),
            ("choose from", "opciones"),
            ("argument ", "argumento "),
        ):
            message = message.replace(original, translated)
        fail(message + ". Consulta codex-run --help.")


def seconds(value):
    if not value.isascii() or not value.isdigit() or int(value) <= 0:
        raise argparse.ArgumentTypeError("SECS debe ser un entero positivo")
    return int(value)


def events(out):
    try:
        with (out / "events.jsonl").open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                    if isinstance(event, dict):
                        yield event
                except ValueError:
                    pass  # Una línea parcial es normal mientras Codex escribe.
    except FileNotFoundError:
        pass


def summary(out):
    session, tools, message = "", 0, ""
    for event in events(out):
        if event.get("type") == "thread.started":
            session = event.get("thread_id", "")
        item = event.get("item", {})
        if event.get("type") != "item.completed" or not isinstance(item, dict):
            continue
        kind = item.get("type", "")
        if kind in ("command_execution", "mcp_tool_call", "web_search", "file_change", "tool_call"):
            tools += 1
        if kind == "agent_message":
            message = item.get("text", "")
    return session, tools, message


def finish(out, code):
    # El enlace publica el resultado completo y conserva el primer escritor.
    fd, temporary = tempfile.mkstemp(prefix=".done-", dir=str(out))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("exit={}\n".format(code))
        try:
            os.link(temporary, str(out / "done"))
        except FileExistsError:
            pass
    finally:
        os.unlink(temporary)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except OSError:
        return True  # Algunos sandboxes permiten kill(0), pero impiden ejecutar ps.
    return result.returncode == 0 and not result.stdout.strip().startswith("Z")


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def kill_group(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def child_group(out):
    # timeout corre en su propia sesión, así que su pid es el grupo de Codex.
    try:
        return int((out / "child-pid").read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def run_dir(value):
    out = Path(value).resolve()
    if not (out / "meta").is_file() or not (out / "pid").is_file():
        fail("OUT no contiene un run válido")
    return out


def run_pid(out):
    pid = int((out / "pid").read_text().strip())
    if pid <= 1:
        fail("pid inválido en OUT")
    return pid


def state(out):
    if (out / "done").exists():
        return "done"
    child = child_group(out)
    if alive(run_pid(out)) or (child and group_alive(child)):
        return "running"
    return "lost"


def status(out):
    current = state(out)
    detail = " " + (out / "done").read_text().strip() if current == "done" else ""
    session, tools, _ = summary(out)
    print("state={}{}".format(current, detail))
    print("session={}".format(session))
    print("tools={}".format(tools))
    message = out / "last-message.md"
    if message.is_file() and message.stat().st_size:
        print("last_message={}".format(message))


def executable(value):
    path = shutil.which(value)
    if not path:
        fail("no se encontró el ejecutable {}".format(value))
    return str(Path(path).resolve())


def launch(out, brief, config, session=None):
    brief = Path(brief).resolve()
    if not brief.is_file():
        fail("BRIEF debe ser un archivo legible")
    content = brief.read_bytes()
    timeout_bin = shutil.which("timeout") or shutil.which("gtimeout")
    if not timeout_bin:
        fail("se requiere timeout o gtimeout con --signal=KILL")
    codex = executable(os.environ.get("CODEX_RUN_BIN", "codex"))
    home = str(Path(os.environ.get("CODEX_RUN_HOME", str(Path.home() / ".config/codex-cli"))).resolve())
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        fail("OUT debe estar vacío para conservar la evidencia anterior")
    (out / "brief.md").write_bytes(content)
    command = [codex, "exec"]
    if session:
        command += ["resume", session, "-c", "sandbox_mode=" + json.dumps(config["sandbox"])]
    else:
        command += ["-C", config["cwd"], "-s", config["sandbox"], "-m", config["model"]]
    command += ["-c", "model_reasoning_effort=" + json.dumps(config["effort"]),
                "--skip-git-repo-check", "--json"]
    if not session:
        command += ["-o", str(out / "last-message.md")]
    if config["ephemeral"]:
        command.append("--ephemeral")
    if config["roots"]:
        command += ["-c", "sandbox_workspace_write.writable_roots=" + json.dumps(config["roots"], ensure_ascii=False)]
    if config["network"]:
        command += ["-c", "sandbox_workspace_write.network_access=true"]
    command.append("-")
    command = [str(Path(timeout_bin).resolve()), "--signal=KILL", str(config["timeout"])] + command
    spec = dict(config, command=command, home=home, resume=bool(session))
    (out / "run.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    with (out / "meta").open("w", encoding="utf-8") as stream:
        for key in ("cwd", "model", "effort", "sandbox", "ephemeral", "timeout"):
            value = config[key]
            if isinstance(value, bool):
                value = int(value)
            stream.write("{}={}\n".format(key, value))
    audit = "cd {} && CODEX_HOME={} {} < {}\n".format(
        shlex.quote(config["cwd"]), shlex.quote(home),
        " ".join(shlex.quote(arg) for arg in command), shlex.quote(str(out / "brief.md")))
    (out / "cmd").write_text(audit, encoding="utf-8")
    (out / "events.jsonl").touch()
    with (out / "stderr.log").open("ab") as log:
        worker = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_worker", str(out)],
                                  start_new_session=True, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=log, close_fds=True)
    (out / "pid").write_text(str(worker.pid) + "\n", encoding="utf-8")
    print("out={}".format(out))
    print("pid={}".format(worker.pid))


def worker(out):
    spec = json.loads((out / "run.json").read_text(encoding="utf-8"))
    child = None
    stopped = False

    def terminate(_signum, _frame):
        nonlocal stopped
        stopped = True
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    code = 127
    try:
        with (out / "brief.md").open("rb") as brief, (out / "events.jsonl").open("ab") as output:
            if not stopped:
                env = dict(os.environ, CODEX_HOME=spec["home"])
                # timeout tiene su propio grupo; el supervisor sobrevive a su SIGKILL.
                child = subprocess.Popen(spec["command"], cwd=spec["cwd"], env=env,
                                         stdin=brief, stdout=output, start_new_session=True)
                (out / "child-pid").write_text(str(child.pid) + "\n", encoding="utf-8")
                if stopped:
                    terminate(signal.SIGTERM, None)
                code = child.wait()
                if code < 0:
                    code = 128 - code
    except OSError as error:
        print("codex-run: {}".format(error), file=sys.stderr)
    finally:
        if spec["resume"]:
            _, _, message = summary(out)
            (out / "last-message.md").write_text(message, encoding="utf-8")
        finish(out, 143 if stopped else code)


def stop(out):
    if not (out / "done").exists():
        pid = run_pid(out)
        if alive(pid):
            kill_group(pid, signal.SIGTERM)
            for _ in range(50):
                if (out / "done").exists() or not alive(pid):
                    break
                time.sleep(0.1)
        groups = [group for group in (child_group(out), pid) if group]
        for group in groups:
            kill_group(group, signal.SIGKILL)
        for _ in range(50):
            if not any(group_alive(group) for group in groups):
                break
            time.sleep(0.1)
        else:
            fail("no se pudo confirmar que el run terminó; revisa los grupos {}".format(groups))
        finish(out, 143)
    status(out)
    print("Detenido: {}".format(out))


def main():
    parser = Parser(prog="codex-run", add_help=False, description="codex-run --help muestra el uso")
    commands = parser.add_subparsers(dest="action", required=True)
    start = commands.add_parser("start", add_help=False)
    start.add_argument("--model", required=True)
    start.add_argument("--effort", required=True, choices=("low", "medium", "high"))
    start.add_argument("--cwd", default=os.getcwd())
    start.add_argument("--out")
    start.add_argument("--write", action="store_true")
    start.add_argument("--root", action="append", default=[])
    start.add_argument("--network", action="store_true")
    start.add_argument("--timeout", type=seconds, default=3600)
    start.add_argument("--ephemeral", action="store_true")
    start.add_argument("brief")
    for name in ("status", "wait", "resume", "stop", "_worker"):
        command = commands.add_parser(name, add_help=False)
        command.add_argument("out")
        if name == "wait":
            command.add_argument("--timeout", type=seconds)
        if name == "resume":
            command.add_argument("brief")
    args = parser.parse_args()
    if args.action == "start":
        if not args.model.startswith("gpt-"):
            fail("--model exige el nombre completo, que debe empezar por gpt-")
        if (args.root or args.network) and not args.write:
            fail("--root y --network requieren --write")
        cwd = Path(args.cwd).resolve(strict=True)
        if not cwd.is_dir():
            fail("--cwd debe ser un directorio")
        roots = [str(Path(root).resolve(strict=True)) for root in args.root]
        if any(not Path(root).is_dir() for root in roots):
            fail("--root debe ser un directorio")
        if any("\n" in value or "\r" in value for value in [str(cwd), args.model]):
            fail("cwd y model deben ocupar una sola línea en meta")
        if args.out:
            out = Path(args.out).resolve()
        else:
            base = Path(os.environ.get("TMPDIR") or "/tmp") / "codex-run"
            base.mkdir(parents=True, exist_ok=True)
            out = Path(tempfile.mkdtemp(prefix=time.strftime("%Y%m%d-%H%M%S-"), dir=str(base))).resolve()
        config = dict(cwd=str(cwd), model=args.model, effort=args.effort,
                      sandbox="workspace-write" if args.write else "read-only",
                      roots=roots, network=args.network, ephemeral=args.ephemeral, timeout=args.timeout)
        launch(out, args.brief, config)
    elif args.action == "_worker":
        worker(Path(args.out))
    else:
        out = run_dir(args.out)
        if args.action == "status":
            status(out)
        elif args.action == "stop":
            stop(out)
        elif args.action == "wait":
            deadline = time.monotonic() + args.timeout if args.timeout else None
            while state(out) == "running":
                if deadline is not None and time.monotonic() >= deadline:
                    status(out)
                    return 124
                time.sleep(min(2, max(0, deadline - time.monotonic())) if deadline else 2)
            status(out)
        elif args.action == "resume":
            config = json.loads((out / "run.json").read_text(encoding="utf-8"))
            if config["ephemeral"]:
                fail("un run --ephemeral no tiene rollout para resume")
            session, _, _ = summary(out)
            if not session:
                fail("no hay session id para reanudar")
            for previous in [out] + sorted(out.glob("resume-*")):
                if (previous / "pid").is_file() and state(previous) == "running":
                    fail("detén o espera a {} antes de reanudar".format(previous))
            number = 1
            while True:
                target = out / "resume-{}".format(number)
                try:
                    target.mkdir()
                    break
                except FileExistsError:
                    number += 1
            launch(target, args.brief, config, session=session)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError) as exc:
        print("codex-run: {}".format(exc), file=sys.stderr)
        sys.exit(1)
