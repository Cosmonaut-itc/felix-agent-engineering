#!/usr/bin/python3 -I
"""U3.1 contra un pueued PRIVADO en la Mini: su config, su socket y su directorio en /tmp.

Nunca toca el pueued del usuario ni ~/.local: el control se ejercita con un envoltorio
que fuerza `-c <config privada>` y con un directorio de estado temporal. El daemon
privado se detiene por su pid al terminar, pase lo que pase.

Lanzar sólo por remote-run:  /usr/bin/python3 -I pueue-desk/tests/private_slots.py
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
PUEUE, PUEUED = "/opt/homebrew/bin/pueue", "/opt/homebrew/bin/pueued"
spec = importlib.util.spec_from_file_location("control", ROOT / "control.py")
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)
FAILURES = []


def say(step, **fields):
    print(json.dumps(dict(paso=step, **fields), ensure_ascii=False), flush=True)


def check(name, condition, **fields):
    say("comprobación", nombre=name, ok=bool(condition), **fields)
    if not condition:
        FAILURES.append(name)


class Daemon:
    def __init__(self, base):
        self.base, self.process = base, None
        self.config = base / "pueue.yml"
        self.wrapper = base / "bin/pueue"
        self.wrapper.parent.mkdir()
        for folder in ("data", "data/certs", "run"):
            (base / folder).mkdir(mode=0o700)
        self.wrapper.write_text(f"#!/bin/sh\nexec {PUEUE} -c {self.config} \"$@\"\n")
        self.wrapper.chmod(0o700)

    def write_config(self, variant):
        shared = {"pueue_directory": self.base / "data", "runtime_directory": self.base / "run",
                  "unix_socket_path": self.base / "run/pueue.sock", "pid_path": self.base / "run/pueue.pid",
                  "shared_secret_path": self.base / "data/secret",
                  "daemon_cert": self.base / "data/certs/daemon.cert", "daemon_key": self.base / "data/certs/daemon.key"}
        if variant == 0:
            shared["use_unix_socket"] = "true"
        lines = ["shared:"] + [f"  {k}: {v}" for k, v in shared.items()]
        lines += ["daemon:", "  default_parallel_tasks: 1", "  pause_group_on_failure: false",
                  "  pause_all_on_failure: false"]
        self.config.write_text("\n".join(lines) + "\n")

    def pueue(self, *args, check=True):
        if self.process is None or not self.ours():
            raise RuntimeError("sin daemon privado propio; no se envía nada")
        return subprocess.run([str(self.wrapper), *map(str, args)], text=True, capture_output=True,
                              timeout=20, check=check)

    def start(self, again=False):
        """again: reinicio de un daemon propio ya probado; con la misma variante, reintenta una vez."""
        variants = (self.variant,) if again else (0, 1)
        for attempt, variant in enumerate(variants * (2 if again else 1)):
            if again and attempt == 1:
                # Sólo si pueued no arranca solo: archivos propios rancios en nuestro run/.
                stale = [p for p in (self.base / "run/pueue.sock", self.base / "run/pueue.pid") if p.exists()]
                for path in stale:
                    path.unlink()
                say("rancios_retirados", archivos=[str(p) for p in stale])
            self.write_config(variant)
            log = (self.base / f"pueued-{variant}.log").open("a")
            self.process = subprocess.Popen([PUEUED, "-c", str(self.config)], stdout=log, stderr=subprocess.STDOUT,
                                            stdin=subprocess.DEVNULL, start_new_session=True, cwd=str(self.base))
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and self.process.poll() is None:
                if self.ours() and self.pueue("status", "--json", check=False).returncode == 0:
                    self.variant = variant
                    say("daemon_privado", pid=self.process.pid, variante=variant, config=str(self.config))
                    return
                time.sleep(0.3)
            say("daemon_no_arrancó", variante=variant, salida=(self.base / f"pueued-{variant}.log").read_text()[-1500:])
            self.stop(kill=True)
        raise RuntimeError("pueued privado no arrancó con ninguna configuración")

    def ours(self):
        """El cliente sólo habla con este daemon: pidfile y socket privados con nuestro pid."""
        pid, sock = self.base / "run/pueue.pid", self.base / "run/pueue.sock"
        try:
            return pid.read_text().strip() == str(self.process.pid) and sock.is_socket()
        except OSError:
            return False

    def stop(self, kill=False):
        """Sólo nuestro pid; kill=True simula una caída sin guardado ordenado."""
        if self.process is None or self.process.poll() is not None:
            self.process = None
            return
        if kill:
            os.kill(self.process.pid, signal.SIGKILL)
        elif self.ours():
            self.pueue("shutdown", check=False)
        else:
            os.kill(self.process.pid, signal.SIGTERM)
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.kill(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.kill(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=10)
        say("daemon_detenido", pid=self.process.pid, forma="SIGKILL" if kill else "shutdown",
            codigo=self.process.returncode)
        self.process = None


def main():
    base = Path(tempfile.mkdtemp(prefix="u31o-", dir="/tmp"))
    daemon = Daemon(base)
    state = base / "state"
    state.mkdir()
    pueue = control.Pueue(daemon.wrapper, timeout=10)
    serial = [0]

    def vector():
        return control.observed_slots(pueue.status())

    def request(op="slots", group="suites", delta=-1, dry=False, undo_of=None):
        serial[0] += 1
        with control.Store(state / control.STORE, readonly=True) as store:
            view = store.view()
        value = {"schema": control.HOST_SCHEMA, "op": op, "action_id": f"privada-{serial[0]:04d}", "dry_run": dry,
                 "expected": {"revision": view["revision"], "policy_hash": view["policy_hash"], "slots": vector()}}
        value.update({"change": {"group": group, "delta": delta}} if op == "slots" else {"undo_of": undo_of})
        answer = control.rack(json.dumps(value), pueue, state)
        say("rack", op=op, grupo=group if op == "slots" else None, delta=delta if op == "slots" else None,
            ensayo=dry, estado=answer["status"], error=answer["error"], revision=answer["revision"],
            resultado=answer["result"])
        return answer

    try:
        versions = {b: subprocess.run([b, "--version"], text=True, capture_output=True, timeout=10).stdout.strip()
                    for b in (PUEUE, PUEUED)}
        say("versiones", **versions)
        daemon.start()
        fresh = pueue.status()
        # Un daemon recién creado sólo tiene default y ninguna tarea; si no, no es el nuestro.
        if set(fresh["groups"]) != {"default"} or fresh["tasks"]:
            raise RuntimeError(f"El cliente no habla con el daemon privado: {sorted(fresh['groups'])}")
        daemon.pueue("group", "add", "suites", "--parallel", 3)
        daemon.pueue("group", "add", "rapidas", "--parallel", 3)
        check("perfil inicial", vector() == control.MINI_PROFILE, observado=vector())
        check("adopción", control.adopt(state, pueue.status()) is True)
        (state / "control.lock").touch()
        say("capacidades", **control.capabilities(pueue, state))

        # Plan de ensayo: una bajada, una subida y la deriva, sin efectos.
        before = (state / control.STORE).read_bytes()
        down = request(dry=True)
        check("ensayo de bajada", down["status"] == "dry_run" and down["result"]["plan"]["decreases"] == ["suites"])
        up = request(group="rapidas", delta=1, dry=True)
        check("ensayo de subida con tope lleno", up["error"] == {"code": "capacity"})
        check("ensayo sin efectos", vector() == control.MINI_PROFILE and (state / control.STORE).read_bytes() == before)

        applied = request()
        check("bajada aplicada en pueued", applied["status"] == "applied" and vector()["suites"] == 2,
              observado=vector())
        plan = request("undo", undo_of=applied["action_id"], dry=True)
        check("ensayo de deshacer sube", plan["result"]["plan"] == {"decreases": [], "increases": ["suites"]})
        undone = request("undo", undo_of=applied["action_id"])
        check("deshacer aplicado", undone["status"] == "applied" and vector() == control.MINI_PROFILE)
        kept = request(group="rapidas", delta=-1)
        check("ajuste durable rapidas=2", kept["status"] == "applied" and vector()["rapidas"] == 2)

        # Deriva visible: alguien cambia parallel a mano; el ensayo la muestra y no la repara.
        daemon.pueue("parallel", 5, "-g", "rapidas")
        caps = control.capabilities(pueue, state)["control"]
        drift = request(group="suites", dry=True)
        check("deriva visible", caps["reason"] == "drift" and drift["error"] == {"code": "drift"}
              and vector()["rapidas"] == 5, capacidades=caps)
        check("reconciliación de deriva", control.reconcile(pueue, state, pueue.status()["groups"])
              and vector()["rapidas"] == 2, observado=vector())

        # Caída de pueued justo después de un parallel y con una tarea en fila en un grupo pausado.
        changed = request(group="suites", delta=-1)
        check("bajada antes de la caída", changed["status"] == "applied" and vector()["suites"] == 2)
        daemon.pueue("pause", "-g", "suites")
        daemon.pueue("add", "-g", "suites", "--", "/usr/bin/true")
        daemon.stop(kill=True)
        daemon.start(again=True)
        after = pueue.status()
        lost = control.observed_slots(after)
        say("tras_SIGKILL", observado=lost, estados={g: after["groups"][g]["status"] for g in control.MINI_PROFILE})
        with control.Store(state / control.STORE, readonly=True) as store:
            desired = store.view()["desired"]
        check("deseo durable tras la caída", desired == {"suites": 2, "default": 1, "rapidas": 2}, deseo=desired)
        control.save_owned(state, {"suites"})  # el traspaso de `control.py reanudar suites`
        with control.locked(state):
            try:
                control.guard(pueue, None, state, host="mini")
            except Exception as error:
                say("guardia_error", detalle=f"{type(error).__name__}: {error}")
        final = pueue.status()
        records = [json.loads(r) for r in (state / "guardia.log").read_text().splitlines()]
        decision = ([r for r in records if r["evento"] == "decision"] or [{}])[-1]
        say("guardia_tras_reinicio", observado=control.observed_slots(final), decision=decision,
            estado_suites=final["groups"]["suites"]["status"],
            propios=sorted(json.loads((state / "guardia-state.json").read_text())),
            reconciliado=[r for r in records if r["evento"] == "reconciliado"])
        check("la guardia repone el deseo tras reiniciar", control.observed_slots(final) == desired)
        if decision.get("accion") == "reanudar":
            check("abre sólo reconciliado", final["groups"]["suites"]["status"] == "Running")

        # Reinicio ordenado: lo que pueued conserva por sí mismo, y la guardia otra vez.
        deadline = time.monotonic() + 15
        while (time.monotonic() < deadline and final["groups"]["suites"]["status"] == "Running"
               and control.live_tasks(pueue.status())):
            time.sleep(0.5)
        daemon.stop()
        daemon.start(again=True)
        restored = vector()
        say("tras_shutdown_ordenado", observado=restored)
        check("reconciliación tras reinicio ordenado",
              control.reconcile(pueue, state, pueue.status()["groups"]) and vector() == desired, observado=vector())
        running = [t for t in pueue.status()["tasks"].values()
                   if isinstance(t["status"], dict) and "Running" in t["status"]]
        check("sin tareas corriendo al cerrar", not running)
    finally:
        pid = daemon.process.pid if daemon.process else None
        daemon.stop()
        if pid is not None:
            try:
                os.kill(pid, 0)
                alive = True
            except ProcessLookupError:
                alive = False
            check("pueued privado terminado", not alive, pid=pid)
        shutil.rmtree(base, ignore_errors=True)
        say("limpieza", directorio=str(base), existe=base.exists())
    print(f"Resultado privado: {'OK' if not FAILURES else 'FALLAS ' + ', '.join(FAILURES)}", flush=True)
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
