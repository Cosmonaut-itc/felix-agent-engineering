#!/usr/bin/python3 -I
"""Medición U17a con instancias privadas de Pueue 4.0.4; nunca usa el socket del usuario.

Cada caso crea su configuración, estado y socket dentro del directorio de la corrida
y detiene su daemon al terminar. Imprime una línea JSON por observación.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

PUEUE = os.environ.get("PAO_PUEUE_BIN", "/opt/homebrew/bin/pueue")
PUEUED = os.environ.get("PAO_PUEUED_BIN", "/opt/homebrew/bin/pueued")
ROOT = Path(tempfile.mkdtemp(prefix=".u17a-diag-", dir=Path.cwd()))
ENV = {"PATH": "/opt/homebrew/bin:/usr/bin:/bin", "HOME": str(ROOT)}


def emit(label, **fields):
    print(label, json.dumps(fields, sort_keys=True, ensure_ascii=False), flush=True)


class Instance:
    def __init__(self, name):
        self.root = ROOT / name
        self.root.mkdir()
        self.data = self.root / "data"
        self.config = self.root / "pueue.yml"
        self.config.write_text(json.dumps({"shared": {
            "pueue_directory": str(self.data), "runtime_directory": str(self.root),
            "unix_socket_path": "s", "alias_file": str(self.root / "aliases.yml")},
            "daemon": {"compress_state_file": False}}))
        self.daemon = None

    def call(self, *args, check=True):
        result = subprocess.run([PUEUE, "-c", str(self.config), *args], cwd=self.root, env=ENV,
                                text=True, capture_output=True, timeout=10)
        if check and result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
        return result.stdout.strip()

    def status(self):
        return json.loads(self.call("status", "--json"))

    def start(self, wait=True):
        self.output = (self.root / "daemon.log").open("a")
        self.daemon = subprocess.Popen([PUEUED, "-c", str(self.config)], cwd=self.root, env=ENV,
                                       stdout=self.output, stderr=subprocess.STDOUT)
        if not wait:
            return None
        for _ in range(100):
            try:
                return self.status()
            except RuntimeError:
                if self.daemon.poll() is not None:
                    raise RuntimeError("pueued salió: " + (self.root / "daemon.log").read_text())
                time.sleep(.05)
        raise RuntimeError((self.root / "daemon.log").read_text())

    def stop(self, sig=signal.SIGTERM):
        self.daemon.send_signal(sig)
        code = self.daemon.wait(timeout=10)
        self.output.close()
        self.daemon = None
        return code

    def disk(self):
        return json.loads((self.data / "state.json").read_text())

    def write_disk(self, value):
        (self.data / "state.json").write_text(json.dumps(value))

    def statuses(self, state):
        return {i: next(iter(t["status"])) if isinstance(t["status"], dict) else t["status"]
                for i, t in state["tasks"].items()}

    def close(self):
        if self.daemon is not None:
            self.stop(signal.SIGKILL)


def wait_for(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.05)
    return False


emit("VERSION", cliente=subprocess.check_output([PUEUE, "--version"], text=True).strip(),
     daemon=subprocess.check_output([PUEUED, "--version"], text=True).strip(), raiz=str(ROOT))

# M1: qué restaura Pueue con Running, Queued y grupos pausados tras SIGKILL.
inst = Instance("m1-restaurar")
try:
    inst.start()
    inst.call("group", "add", "work", "--parallel", "1")
    inst.call("group", "add", "vacio", "--parallel", "1")
    inst.call("group", "add", "manual", "--parallel", "1")
    inst.call("pause", "--wait", "-g", "manual")
    sentinel = inst.root / "m1.centinela"
    running = inst.call("add", "--print-task-id", "-g", "work", "--", f"echo x >> {sentinel}; sleep 600")
    wait_for(lambda: "Running" in inst.status()["tasks"][running]["status"])
    queued = inst.call("add", "--print-task-id", "-g", "work", "--", "true")
    held = inst.call("add", "--print-task-id", "-g", "manual", "--", "true")
    inst.stop(signal.SIGKILL)
    disk = inst.disk()
    emit("M1-DISCO", tareas=inst.statuses(disk), grupos={g: v["status"] for g, v in disk["groups"].items()})
    after = inst.start()
    emit("M1-RESTAURADO", tareas=inst.statuses(after), grupos={g: v["status"] for g, v in after["groups"].items()},
         nota="vacio sigue Running: un add entraría antes de que el host esté listo")
    probe = inst.call("add", "--print-task-id", "-g", "vacio", "--", "true")
    ran = wait_for(lambda: "Done" in inst.status()["tasks"][probe]["status"], 3)
    emit("M1-VACIO-ADMITE", corrio=ran)
finally:
    inst.close()

# M2: una Queued con log ya creado (Pueue la lanzó y murió antes de guardar) se lanza otra vez.
inst = Instance("m2-queued-con-log")
try:
    inst.start()
    inst.call("group", "add", "work", "--parallel", "1")
    inst.call("pause", "--wait", "-g", "work")
    sentinel = inst.root / "m2.centinela"
    task = inst.call("add", "--print-task-id", "-g", "work", "--", f"echo corrida >> {sentinel}")
    inst.stop(signal.SIGTERM)
    log_path = inst.data / "task_logs" / f"{task}.log"
    log_path.parent.mkdir(exist_ok=True)
    log_path.write_text("salida de la primera ejecución\n")
    sentinel.write_text("corrida\n")
    disk = inst.disk()
    emit("M2-DISCO", tarea=inst.statuses(disk)[task], log=log_path.exists(), grupo=disk["groups"]["work"]["status"])
    inst.start()
    inst.call("start", "-g", "work")
    wait_for(lambda: "Done" in inst.status()["tasks"][task]["status"])
    emit("M2-REANUDADO", tarea=inst.status()["tasks"][task]["status"], lineas_centinela=sentinel.read_text().count("corrida"),
         log=log_path.read_text().strip())
finally:
    inst.close()

# M3: un estado editado sin daemon (Running→Done/Killed, grupo nuevo pausado) se restaura tal cual.
inst = Instance("m3-preparado")
try:
    inst.start()
    inst.call("group", "add", "work", "--parallel", "1")
    running = inst.call("add", "--print-task-id", "-g", "work", "--", "sleep 600")
    wait_for(lambda: "Running" in inst.status()["tasks"][running]["status"])
    queued = inst.call("add", "--print-task-id", "-g", "work", "--", "true")
    inst.stop(signal.SIGKILL)
    disk = inst.disk()
    detail = disk["tasks"][running]["status"]["Running"]
    disk["tasks"][running]["status"] = {"Done": {**detail, "end": detail["start"], "result": "Killed"}}
    for group in disk["groups"].values():
        group["status"] = "Paused"
    disk["groups"]["nuevo"] = {"status": "Paused", "parallel_tasks": 3}
    inst.write_disk(disk)
    after = inst.start()
    emit("M3-RESTAURADO", tareas=inst.statuses(after), resultado=after["tasks"][running]["status"]["Done"]["result"],
         grupos=after["groups"])
    probe = inst.call("add", "--print-task-id", "-g", "nuevo", "--", "true")
    time.sleep(1)
    emit("M3-NUEVO-CERRADO", estado=inst.status()["tasks"][probe]["status"])
    inst.call("start", "-g", "work")
    ran = wait_for(lambda: "Done" in inst.status()["tasks"][queued]["status"])
    emit("M3-ABRIR", queued_corre=ran, interrumpida=inst.status()["tasks"][running]["status"]["Done"]["result"])
finally:
    inst.close()

# M4: estado ilegible. Pueue arranca vacío y lo sobrescribe.
inst = Instance("m4-ilegible")
try:
    inst.start()
    inst.call("add", "--print-task-id", "--", "true")
    inst.stop(signal.SIGTERM)
    original = (inst.data / "state.json").read_text()
    (inst.data / "state.json").write_text(original[: len(original) // 2])
    after = inst.start()
    emit("M4-RESTAURADO", tareas=len(after["tasks"]), sobrescrito=(inst.data / "state.json").read_text() != original[: len(original) // 2])
finally:
    inst.close()

# M5: pid file viejo que apunta a un proceso vivo ajeno: pueued no arranca.
inst = Instance("m5-pid")
try:
    inst.start()
    inst.stop(signal.SIGKILL)
    (inst.root / "pueue.pid").write_text(str(os.getpid()))
    inst.start(wait=False)
    code = inst.daemon.wait(timeout=10)
    inst.output.close()
    inst.daemon = None
    emit("M5-PID-AJENO", codigo=code, log=(inst.root / "daemon.log").read_text().strip().splitlines()[-1:])
finally:
    inst.close()

# M6: SIGTERM con una Running y un grupo pausado: qué guarda y qué grupos quedan.
inst = Instance("m6-sigterm")
try:
    inst.start()
    inst.call("group", "add", "work", "--parallel", "1")
    task = inst.call("add", "--print-task-id", "-g", "work", "--", "sleep 600")
    wait_for(lambda: "Running" in inst.status()["tasks"][task]["status"])
    code = inst.stop(signal.SIGTERM)
    disk = inst.disk()
    emit("M6-DISCO", codigo=code, tarea=disk["tasks"][task]["status"], grupos={g: v["status"] for g, v in disk["groups"].items()})
finally:
    inst.close()

emit("FIN", resultado="OK; daemons privados detenidos")
