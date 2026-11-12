#!/usr/bin/python3 -I
"""Ciclo de vida de pueued: cerrar admisión, recuperar y recién entonces liberar."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

spec = importlib.util.spec_from_file_location("control", Path(__file__).with_name("control.py"))
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)


def boot_id(host):
    if host == "desk":
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    return subprocess.check_output(["/usr/sbin/sysctl", "-n", "kern.boottime"], text=True).strip()


def ensure_stopped(pueue, pid_file):
    # Un launcher muerto puede dejar al daemon vivo en macOS. Nunca editar su estado.
    stale_pid = False
    if pid_file.exists():
        pid = int(pid_file.read_text().strip())
        if pid <= 1:
            raise ValueError("Archivo PID inválido; arranque abortado")
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        else:
            process = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "comm="], text=True, capture_output=True, check=False)
            if process.returncode == 0 and Path(process.stdout.strip()).name.startswith("pueued"):
                raise RuntimeError(f"El PID {pid} aún existe; no se altera la cola ni se inicia otro daemon")
            if process.returncode not in (0, 1):
                raise RuntimeError("No se pudo verificar el PID del daemon anterior")
            stale_pid = True
    try:
        pueue.status()
    except (subprocess.CalledProcessError, FileNotFoundError):
        if stale_pid:
            pid_file.unlink()  # PID reutilizado por otro programa; jamás se le envía una señal de terminación.
        return
    raise RuntimeError("Ya responde un daemon en el socket administrado; arranque abortado")


def supervise(pueue, daemon_binary, config, directory, host, docker, paths=None):
    settings = json.loads((paths or config).read_text())
    shared = settings["shared"]
    data = Path(shared["pueue_directory"])
    pid_file = Path(shared["pid_path"])
    if settings.get("daemon", {}).get("compress_state_file", False):
        raise ValueError("El supervisor requiere estado JSON sin compresión")
    for binary, expected in ((pueue.binary, "pueue 4.0.4"), (daemon_binary, "pueued 4.0.4")):
        if subprocess.check_output([str(binary), "--version"], text=True).strip() != expected:
            raise ValueError("El ciclo de vida está validado sólo para Pueue 4.0.4")
    stop = []
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, lambda number, frame: stop.append(number))
    daemon = None
    audit = None
    try:
        with control.locked(directory / "lifecycle"):
            ensure_stopped(pueue, pid_file)
            with control.locked(directory):
                control.prepare_start(data / "state.json", directory, host, boot_id(host))
            # Archivo regular: si muere el supervisor, el daemon conserva su stderr
            # y la evidencia de shutdown; no depende de un lector de pipe vivo.
            audit_path = directory / "daemon-audit.log"
            if audit_path.exists():
                audit_path.replace(directory / "daemon-audit.log.1")
            fd = os.open(audit_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.fchmod(fd, 0o600)
            audit = os.fdopen(fd, "w")
            with control.locked(directory):
                state = control.load_control(directory)
                state["audit_active"] = True
                control.save_control(directory, state)
            env = dict(os.environ, RUST_LOG="warn,pueue_lib::network::protocol=debug,"
                       "pueue::daemon::process_handler::kill=info,pueue::daemon::process_handler::finish=info,"
                       "pueue::daemon::process_handler::spawn=info")
            daemon = subprocess.Popen([str(daemon_binary), "--config", str(config)], stderr=audit, env=env)
            for _ in range(100):
                if daemon.poll() is not None:
                    raise RuntimeError("pueued terminó durante el arranque")
                try:
                    pueue.status()
                    break
                except subprocess.CalledProcessError:
                    time.sleep(.1)
            else:
                raise RuntimeError("pueued no respondió en 10 segundos")
            with control.locked(directory):
                queue = pueue.status()
                if any(g["status"] != "Paused" for g in queue["groups"].values()):
                    raise RuntimeError("Pueue no restauró el cierre de admisión; no se libera la cola")
                control.recover(pueue, directory, data / "state.json")
                control.apply_mode(pueue, control.datetime.now(control.timezone.utc), host)
                control.guard(pueue, Path("/proc"), directory, docker, host)
            while daemon.poll() is None and not stop:
                try:
                    daemon.wait(timeout=.25)
                except subprocess.TimeoutExpired:
                    pass
            if stop and daemon.poll() is None:
                with control.locked(directory):
                    control.capture_stop(pueue, directory, host)
                daemon.terminate()
                try:
                    daemon.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait(timeout=5)
                return 0
            with control.locked(directory):
                control.log(directory, "daemon_terminado", codigo=daemon.returncode,
                            motivo="El gestor volverá a iniciar; recuperar cruzará la auditoría del daemon")
            return 1
    finally:
        # Nunca dejar un daemon arrancado después de fallar recuperar/guardia.
        if daemon is not None and daemon.poll() is None:
            try:
                with control.locked(directory):
                    control.capture_stop(pueue, directory, host)
            except Exception as error:
                with control.locked(directory):
                    control.log(directory, "error_captura", detalle=str(error))
                # El registro del daemon permite reconocer el shutdown aun si falla el socket.
            daemon.terminate()
            try:
                daemon.wait(timeout=20)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait(timeout=5)
        if audit is not None:
            audit.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=["desk", "mini"], required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--paths", type=Path, help="Metadatos JSON cuando --config es YAML")
    parser.add_argument("--pueue-bin", type=Path, required=True)
    parser.add_argument("--pueued-bin", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, default=control.STATE_DIR)
    parser.add_argument("--docker-bin", default=os.environ.get("PAO_DOCKER_BIN", "/usr/bin/docker"))
    args = parser.parse_args()
    pueue = control.Pueue(args.pueue_bin, args.config)
    try:
        return supervise(pueue, args.pueued_bin, args.config, args.state_dir, args.host, args.docker_bin, args.paths)
    except Exception as error:
        with control.locked(args.state_dir):
            control.log(args.state_dir, "error_arranque", detalle=str(error))
        print(f"Arranque cerrado: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
