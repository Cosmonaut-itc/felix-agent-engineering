#!/usr/bin/python3
"""Timbre local de cambios de Pueue. Python 3.9, sólo stdlib."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import selectors
import queue
import threading
import shlex
import signal
import socket
import stat
import subprocess
import sys
import time
from typing import Optional

ALIASES = {"desk": "desk", "mini": "mini"}
Huella = dict
Token = str
Estado = str

BOOT = 'import sys;b=sys.stdin.buffer;n=int(b.readline());exec(compile(b.read(n),"vigia.py","exec"))'


def intervalo(nombre, defecto):
    return float(os.environ.get("PAO_VIGIA_" + nombre, defecto))


def huella_de(task: dict) -> Huella:
    status = task["status"]
    estado = status if isinstance(status, str) else next(iter(status))
    datos = status[estado] if isinstance(status, dict) else {}
    datos = datos if isinstance(datos, dict) else {}
    return dict(grupo=task.get("group"), comando=task.get("command"), estado=estado,
                encolada=datos.get("enqueued_at"), inicio=datos.get("start"),
                fin=datos.get("end"), resultado=datos.get("result"))


def token_de(huella: Optional[Huella]) -> Token:
    if huella is None:
        return "ausente"
    raw = json.dumps(huella, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "v1-" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def leer_pueue(pueue: str) -> dict[int, tuple[Token, Estado]]:
    env = dict(os.environ)
    env["PATH"] = env.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin:" + str(Path.home()/".local/bin") + ":" + str(Path.home()/".cargo/bin")
    result = subprocess.run([pueue, "status", "--json"], capture_output=True,
                            check=True, timeout=10, env=env)
    tasks = json.loads(result.stdout)["tasks"]
    return {int(k): (token_de(huella_de(v)), huella_de(v)["estado"]) for k, v in tasks.items()}


def estado_archivo():
    if "PAO_VIGIA_STATE_FILE" in os.environ:
        return Path(os.environ["PAO_VIGIA_STATE_FILE"])
    if sys.platform == "darwin":
        return Path.home()/"Library/Application Support/pueue/state.json"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home()/".local/share")))/"pueue/state.json"


def flujo():
    entradas = queue.Queue()

    def leer_entrada():
        for line in sys.stdin.buffer:
            entradas.put(line)
        entradas.put(None)

    threading.Thread(target=leer_entrada, daemon=True).start()
    subs, enviados, datos = set(), {}, {}
    pendiente, ultima, latido, sano = None, -float("inf"), time.monotonic(), False
    firma = None
    tick = intervalo("TICK", 15)
    while True:
        now = time.monotonic()
        time.sleep(.05)
        while not entradas.empty():
            line = entradas.get_nowait()
            if line is None:
                return
            parts = line.split()
            if len(parts) != 2 or not parts[1].isdigit():
                continue
            task = int(parts[1])
            if parts[0] == b"S":
                subs.add(task)
                enviados.pop(task, None)
                pendiente = now if pendiente is None else min(pendiente, now)
            elif parts[0] == b"U":
                subs.discard(task)
                enviados.pop(task, None)
        try:
            st = estado_archivo().stat()
            nueva = (st.st_mtime_ns, st.st_size)
        except OSError:
            nueva = None
        if nueva != firma:
            firma = nueva
            pendiente = now + .3
        if subs and now - ultima >= tick and pendiente is None:
            pendiente = now
        if subs and pendiente is not None and now >= pendiente and now - ultima >= 1:
            ultima, pendiente = now, None
            try:
                datos = leer_pueue("pueue")
                sano = True
                for task in sorted(subs):
                    valor = datos.get(task, ("ausente", "ausente"))
                    if enviados.get(task) != valor:
                        print("T", task, *valor, flush=True)
                        enviados[task] = valor
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                sano = False
                enviados.clear()  # Tras E, el corredor descarta su caché.
                print("E lectura-pueue", flush=True)
        if sano and now - latido >= tick:
            print("L", flush=True)
            latido = now


def directorio():
    path = Path(os.environ.get("PAO_VIGIA_STATE_DIR", str(Path.home()/".local/state/pao-vigia")))
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def conectar(path):
    sock = socket.socket(socket.AF_UNIX)
    sock.settimeout(.3)
    try:
        sock.connect(str(path))
        return sock
    except OSError:
        sock.close()
        return None


def candado(root, host):
    fd = os.open(str(root/(host + ".lock")), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        os.close(fd)
        return None


def timbre(host, task, visto, plazo):
    root = directorio()
    limite = time.monotonic() + 12
    sock = conectar(root/(host + ".sock"))
    while sock is None and time.monotonic() < limite:
        fd = candado(root, host)
        if fd is not None:
            try:
                sock = conectar(root/(host + ".sock"))
                if sock is None:
                    env = dict(os.environ, PAO_VIGIA_LOCK_FD=str(fd))
                    with (root/(host + ".log")).open("ab") as log:
                        subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()), "corredor", host],
                                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                         start_new_session=True, pass_fds=(fd,), env=env)
            finally:
                os.close(fd)
        if sock is None:
            time.sleep(.05)
            sock = conectar(root/(host + ".sock"))
    respuesta = "sin-vigia"
    if sock is not None:
        try:
            sock.settimeout(plazo + 13)
            sock.sendall(("ESPERA 1 %s %s %s\n" % (task, visto, plazo)).encode())
            with sock.makefile("rb") as stream:
                respuesta = stream.readline(512).decode().strip()
        except (OSError, ValueError):
            pass
        finally:
            sock.close()
    parts = respuesta.split()
    if len(parts) == 3 and parts[0] in ("cambio", "plazo"):
        print("VIGIA=" + parts[0] + "\nVIGIA_TOKEN=" + parts[1] + "\nVIGIA_ESTADO=" + parts[2])
    else:
        print("VIGIA=sin-vigia")


def corredor(host):
    root = directorio()
    inherited = os.environ.pop("PAO_VIGIA_LOCK_FD", None)
    fd = int(inherited) if inherited is not None else candado(root, host)
    if fd is None:
        return
    path = root/(host + ".sock")
    sel = selectors.DefaultSelector()
    server = socket.socket(socket.AF_UNIX)
    proc, clients, tokens, suscritas = None, {}, {}, set()
    contacto, reconectar, retraso, inactivo = -float("inf"), 0, 1, time.monotonic()
    buffer = b""
    vivo = True

    def parar(*_):
        nonlocal vivo
        vivo = False

    def log(msg):
        print(time.strftime("%Y-%m-%dT%H:%M:%S"), host, msg, flush=True)

    def enviar(line):
        if proc is not None:
            try:
                proc.stdin.write(line.encode() + b"\n")
                proc.stdin.flush()
            except (OSError, ValueError):
                pass

    def cerrar_ssh():
        nonlocal proc, contacto
        contacto = -float("inf")
        if proc is not None:
            try:
                sel.unregister(proc.stdout)
            except KeyError:
                pass
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                # Dejar que SSH entregue EOF y el flujo salga antes de cortar transporte.
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            proc.stdout.close()
            proc = None

    signal.signal(signal.SIGTERM, parar)
    signal.signal(signal.SIGINT, parar)
    try:
        try:
            residual = path.lstat()
        except FileNotFoundError:
            residual = None
        if residual is not None:
            if not stat.S_ISSOCK(residual.st_mode) or residual.st_uid != os.getuid():
                raise OSError("La ruta del socket contiene un archivo ajeno al corredor")
            path.unlink()  # Socket propio residual, bajo el candado heredado.
        server.bind(str(path))
        os.chmod(path, 0o600)
        server.listen()
        server.setblocking(False)
        sel.register(server, selectors.EVENT_READ)
        while vivo:
            now = time.monotonic()
            if not clients and now - inactivo >= intervalo("IDLE", 600):
                break
            if proc is None and now >= reconectar:
                try:
                    cmd = shlex.split(os.environ.get("PAO_VIGIA_SSH", "ssh"))
                    cmd += ["-T", "-o", "BatchMode=yes", "-o", "ControlPath=none", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", ALIASES[host],
                            "/usr/bin/python3 -I -c " + shlex.quote(BOOT) + " flujo"]
                    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
                    codigo = Path(__file__).read_bytes()
                    proc.stdin.write(str(len(codigo)).encode() + b"\n" + codigo)
                    proc.stdin.flush()
                    sel.register(proc.stdout, selectors.EVENT_READ)
                    buffer = b""
                    for task in suscritas:
                        enviar("S " + task)
                    log("conectado")
                except OSError:
                    if proc is not None:
                        cerrar_ssh()
                    reconectar = now + retraso
                    retraso = min(60, retraso * 2)
            for key, _ in sel.select(.05):
                if key.fileobj is server:
                    client, _ = server.accept()
                    client.setblocking(False)
                    clients[client] = {"buffer": b"", "creada": now, "peticion": None}
                    sel.register(client, selectors.EVENT_READ)
                elif proc is not None and key.fileobj is proc.stdout:
                    chunk = os.read(proc.stdout.fileno(), 65536)
                    if not chunk:
                        cerrar_ssh()
                        reconectar = now + retraso
                        retraso = min(60, retraso * 2)
                        log("caída; reconectando")
                        continue
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        parts = line.decode(errors="replace").split()
                        contacto = now
                        retraso = 1
                        if len(parts) == 4 and parts[0] == "T":
                            tokens[parts[1]] = (parts[2], parts[3])
                        elif parts and parts[0] == "E":
                            tokens.clear()
                            contacto = -float("inf")
                else:
                    client = key.fileobj
                    info = clients[client]
                    try:
                        chunk = client.recv(4096)
                    except (ConnectionError, OSError):
                        chunk = b""
                    if not chunk:
                        sel.unregister(client)
                        client.close()
                        del clients[client]
                        continue
                    info["buffer"] += chunk
                    if b"\n" in info["buffer"] and info["peticion"] is None:
                        parts = info["buffer"].split(b"\n", 1)[0].decode(errors="replace").split()
                        try:
                            if len(parts) != 5 or parts[:2] != ["ESPERA", "1"]:
                                raise ValueError()
                            task, visto, plazo = validar(*parts[2:])
                            info["peticion"] = (task, visto, plazo)
                            info["creada"] = now
                            if task not in suscritas:
                                tokens.pop(task, None)
                                suscritas.add(task)
                                enviar("S " + task)
                        except ValueError:
                            info["error"] = True
            now = time.monotonic()
            for client, info in list(clients.items()):
                respuesta = None
                if info.get("error") or (info["peticion"] is None and now - info["creada"] >= 12):
                    respuesta = "error peticion"
                elif info["peticion"] is not None:
                    task, visto, plazo = info["peticion"]
                    valor = tokens.get(task)
                    sano = now - contacto <= intervalo("CONTACT", 45)
                    if valor is not None and sano and valor[0] != visto:
                        respuesta = "cambio " + " ".join(valor)
                    elif now - info["creada"] >= plazo and valor is not None:
                        respuesta = ("plazo " + " ".join(valor)) if sano else "sin-vigia"
                    elif now - info["creada"] >= min(12, plazo) and valor is None:
                        # Primera suscripción dispone de hasta 12 s incluso con plazo 0.
                        if now - info["creada"] >= 12:
                            respuesta = "sin-vigia"
                if respuesta:
                    try:
                        client.sendall((respuesta + "\n").encode())
                    except OSError:
                        pass
                    sel.unregister(client)
                    client.close()
                    del clients[client]
            necesarias = {v["peticion"][0] for v in clients.values() if v["peticion"]}
            for task in suscritas - necesarias:
                enviar("U " + task)
            suscritas = necesarias
            if clients:
                inactivo = now
    finally:
        cerrar_ssh()
        for client in clients:
            client.close()
        server.close()
        sel.close()
        os.close(fd)


def validar(task, visto, plazo):
    if not task.isascii() or not task.isdigit():
        raise ValueError("TAREA debe ser numérica")
    if visto != "-" and visto != "ausente" and not (len(visto) == 19 and visto.startswith("v1-") and all(c in "0123456789abcdef" for c in visto[3:])):
        raise ValueError("VISTO debe ser un token o -")
    if not plazo.isascii() or not plazo.isdigit() or not 0 <= int(plazo) <= 900:
        raise ValueError("PLAZO debe estar entre 0 y 900")
    return str(int(task)), visto, int(plazo)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("timbre")
    t.add_argument("host", choices=ALIASES)
    for arg in ("task", "visto", "plazo"):
        t.add_argument(arg)
    c = sub.add_parser("corredor")
    c.add_argument("host", choices=ALIASES)
    sub.add_parser("flujo")
    args = parser.parse_args()
    if args.cmd == "timbre":
        try:
            task, visto, plazo = validar(args.task, args.visto, args.plazo)
        except ValueError as exc:
            parser.error(str(exc))
        try:
            timbre(args.host, task, visto, plazo)
        except OSError:
            print("VIGIA=sin-vigia")
    elif args.cmd == "corredor":
        corredor(args.host)
    else:
        flujo()


if __name__ == "__main__":
    main()
