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
    return result.returncode != 0 or not result.stdout.strip().startswith("Z")


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    # Los zombies ya terminaron; killpg(0) también los cuenta en Linux.
    result = subprocess.run(["ps", "-A", "-o", "pgid=,stat="],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        raise OSError("ps no pudo comprobar el grupo {}".format(pgid))
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            raise OSError("ps devolvió un grupo de proceso inválido")
        if int(fields[0]) == pgid and not fields[1].startswith("Z"):
            return True
    return False


def kill_group(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def child_group(out):
    # Codex corre en su propia sesión: su pid también es el pgid.
    try:
        pid = int((out / "child-pid").read_text().strip())
    except FileNotFoundError:
        return None
    if pid <= 1:
        fail("grupo inválido en OUT")
    return pid


def atomic_text(path, content):
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def process_snapshot():
    result = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,lstart="],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=dict(os.environ, LC_ALL="C"))
    if result.returncode:
        raise OSError("ps no pudo tomar la foto de procesos: " + result.stderr.strip())
    processes = {}
    for line in result.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
            raise OSError("ps devolvió una identidad de proceso inválida")
        processes[int(fields[0])] = (int(fields[1]), fields[2])
    return processes


def process_alive(pid, started, snapshot):
    entry = snapshot.get(pid)
    return entry is not None and entry[1] == started and alive(pid)


class ProcessTree:
    def __init__(self, out, root=None):
        self.out = out
        self.root = root if root is not None else child_group(out)
        self.seen = set()
        self.error = False
        try:
            for line in (out / "pids").read_text(encoding="utf-8").splitlines():
                pid, started = line.split(None, 1)
                if int(pid) <= 1:
                    raise ValueError("pid inválido en OUT/pids")
                self.seen.add((int(pid), started))
        except FileNotFoundError:
            pass

    def capture(self, persist=True):
        snapshot = process_snapshot()
        # Un pid conocido con otra fecha ya no es nuestro proceso.
        roots = {pid for pid, started in self.seen
                 if pid in snapshot and snapshot[pid][1] == started}
        if self.root and not any(pid == self.root for pid, _ in self.seen):
            if self.root in snapshot:
                roots.add(self.root)
        pending = set(roots)
        while pending:
            self.seen.update((pid, snapshot[pid][1]) for pid in pending)
            pending = {pid for pid, (parent, _) in snapshot.items()
                       if parent in pending and pid not in roots}
            roots.update(pending)
        if persist:
            self.persist()
        return snapshot

    def persist(self):
        atomic_text(self.out / "pids", "".join(
            "{} {}\n".format(pid, started) for pid, started in sorted(self.seen)))

    def group(self, snapshot):
        # Un líder reutilizado ya no identifica el grupo de este run.
        if self.root in snapshot and any(pid == self.root for pid, _ in self.seen):
            if (self.root, snapshot[self.root][1]) not in self.seen:
                return None
        return self.root

    def survivors(self):
        snapshot = process_snapshot()
        remaining = {pid for pid, started in self.seen
                     if process_alive(pid, started, snapshot)}
        group = self.group(snapshot)
        if group and group_alive(group):
            remaining.add(group)
        return sorted(remaining)


def log_error(error):
    print("codex-run: {}".format(error), file=sys.stderr, flush=True)


def terminate_tree(tree, child=None):
    """Una sola vía de limpieza. None significa confirmación imposible."""
    remaining = None
    for sig, budget in ((signal.SIGTERM, 3), (signal.SIGKILL, 5)):
        try:
            snapshot = tree.capture(persist=False)
            try:
                tree.persist()
            except OSError as error:
                tree.error = True
                log_error(error)
        except Exception as error:
            tree.error = True
            log_error(error)
            snapshot = None
        # Aunque ps falle, todavía podemos limpiar el grupo que lanzamos.
        group = tree.group(snapshot) if snapshot is not None else tree.root
        if group:
            try:
                kill_group(group, sig)
            except OSError as error:
                tree.error = True
                log_error(error)
        if snapshot is not None:
            for pid, started in tree.seen:
                try:
                    if process_alive(pid, started, snapshot):
                        os.kill(pid, sig)
                except ProcessLookupError:
                    pass
                except OSError as error:
                    tree.error = True
                    log_error(error)
        deadline = time.monotonic() + budget
        while True:
            try:
                if child is not None:
                    child.poll()  # Recolectar Codex antes de comprobar su grupo.
                remaining = tree.survivors()
                if not remaining:
                    return []
            except Exception as error:
                tree.error = True
                log_error(error)
                remaining = None
                # Sin inspección no podemos confirmar; pasar al siguiente signal.
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
    return remaining


def publish_result(out, remaining, code, tree):
    if remaining == []:
        finish(out, code)
        try:
            (out / "stuck").unlink()
        except FileNotFoundError:
            pass
    else:
        # Ante un error de inspección, conservar todos los candidatos.
        pids = remaining if remaining is not None else sorted(
            {pid for pid, _ in tree.seen} | ({tree.root} if tree.root else set()))
        atomic_text(out / "stuck", "survivors={}\n".format(
            ",".join(map(str, pids)) if pids else "unknown"))


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
    if (out / "stuck").exists():
        return "stuck"
    if alive(run_pid(out)) or ProcessTree(out).survivors():
        return "running"
    return "lost"


def status(out):
    current = state(out)
    detail = " " + (out / "done").read_text().strip() if current == "done" else ""
    session, tools, _ = summary(out)
    print("state={}{}".format(current, detail))
    if current == "stuck":
        print((out / "stuck").read_text().strip())
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
    if sys.platform not in ("darwin", "linux"):
        fail("codex-run requiere macOS o Linux")
    executable("ps")
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
    tree = ProcessTree(out)
    stopped = False

    def terminate(_signum, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    code = 127
    try:
        with (out / "brief.md").open("rb") as brief, (out / "events.jsonl").open("ab") as output:
            if not stopped:
                env = dict(os.environ, CODEX_HOME=spec["home"])
                launched = time.monotonic()
                child = subprocess.Popen(spec["command"], cwd=spec["cwd"], env=env,
                                         stdin=brief, stdout=output, start_new_session=True)
                tree.root = child.pid
                # Guardar primero la identidad, también si falla child-pid.
                tree.capture()
                (out / "child-pid").write_text(str(child.pid) + "\n", encoding="utf-8")
                deadline = launched + spec["timeout"]
                next_snapshot = launched + 0.1
                while True:
                    now = time.monotonic()
                    if now >= next_snapshot:
                        tree.capture()
                        next_snapshot = now + (0.1 if now - launched < 1 else 1)
                    result = child.poll()
                    if stopped:
                        code = 143
                        break
                    if result is not None:
                        code = result if result >= 0 else 128 - result
                        break
                    if time.monotonic() >= deadline:
                        code = 137
                        break
                    # Durante el primer segundo observar también runs de 0.3 s.
                    time.sleep(0.05)
    except Exception as error:
        code = 127
        log_error(error)
    finally:
        remaining = terminate_tree(tree, child) if child is not None else []
        if tree.error:
            code = 127
        try:
            if spec["resume"]:
                _, _, message = summary(out)
                (out / "last-message.md").write_text(message, encoding="utf-8")
        except Exception as error:
            code = 127
            log_error(error)
        publish_result(out, remaining, 143 if stopped else code, tree)


def stop(out):
    if not (out / "done").exists():
        pid = run_pid(out)
        if alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 15
            while alive(pid) and time.monotonic() < deadline:
                if (out / "done").exists() or (out / "stuck").exists():
                    break
                time.sleep(0.1)
        if not (out / "done").exists():
            # Si el supervisor no respondió, impedir que siga lanzando procesos
            # o publique un resultado mientras hacemos su limpieza.
            if alive(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            tree = ProcessTree(out)
            remaining = terminate_tree(tree)
            publish_result(out, remaining, 143, tree)
    status(out)
    if state(out) == "stuck":
        fail("no se pudo confirmar que el run terminó; revisa OUT/stuck")
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
            if config["resume"]:
                fail("reanuda desde el run original, no desde {}".format(out.name))
            if config["ephemeral"]:
                fail("un run --ephemeral no tiene rollout para resume")
            session, _, _ = summary(out)
            if not session:
                fail("no hay session id para reanudar")
            for previous in [out] + sorted(out.glob("resume-*")):
                if (previous / "pid").is_file() and state(previous) in ("running", "stuck"):
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
