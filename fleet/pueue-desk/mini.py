#!/usr/bin/python3 -I
"""Relevo de LaunchAgents de Pueue en la propia Mini: instalar, retirar, vigilar y parada de mantenimiento.

El relevo entre el agente de brew y io.pao.pueue se anota en guardia-state.json («relevo»)
antes de tocar servicios. vigilar, que corre cada 30 s desde io.pao.pueue-guardia, revierte un
relevo que no llegó a tocar servicios y completa uno que sí, de modo que matar el instalador o
el retiro en cualquier punto deja un solo daemon y ninguna pausa sin dueño.
"""
import argparse
import importlib.util
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("control", ROOT / "control.py")
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)
LABEL = "io.pao.pueue"
GUARD = "io.pao.pueue-guardia"
BREW = os.environ.get("PAO_BREW_LABEL", "sh.brew.pueue")
TEMPLATES = ROOT / "launchagents"


class Mini:
    def __init__(self, home=None):
        self.home = Path(home or Path.home())
        self.state = self.home / ".local/state/pao/pueue-desk"
        self.library = self.home / ".local/lib/pao-pueue-desk"
        self.agents = self.home / "Library/LaunchAgents"
        self.domain = f"gui/{os.getuid()}"
        self.launchctl = os.environ.get("PAO_LAUNCHCTL", "/bin/launchctl")
        bins = Path(os.environ.get("PAO_PUEUE_BIN_DIR", "/opt/homebrew/bin"))
        self.pueue = control.Pueue(bins / "pueue")
        self.pueued = bins / "pueued"
        self.wait_seconds = float(os.environ.get("PAO_MINI_ESPERA_S", "20"))

    def launch(self, *args, check=True):
        result = subprocess.run([self.launchctl, *args], text=True, capture_output=True)
        if check and result.returncode:
            raise RuntimeError(f"launchctl {' '.join(args)}: {result.stderr.strip() or result.returncode}")
        return result

    def loaded(self, label):
        return self.launch("print", f"{self.domain}/{label}", check=False).returncode == 0

    def plist(self, label):
        return self.agents / f"{label}.plist"

    def bootstrap(self, label):
        if not self.loaded(label):
            self.launch("bootstrap", self.domain, str(self.plist(label)))

    def bootout(self, label):
        if self.loaded(label):
            self.launch("bootout", f"{self.domain}/{label}")

    def place(self, label):
        """Copia el plist ya renderizado de la biblioteca a LaunchAgents."""
        control.atomic_text(self.plist(label), (self.library / "launchagents" / f"{label}.plist").read_text())

    def settled(self, target):
        own, brew = self.loaded(LABEL), self.loaded(BREW)
        services = own and not brew if target == "propio" else brew and not own
        return services and self.pueue.responds()


def render(label, home):
    def fill(value):
        if isinstance(value, str):
            return value.replace("@HOME@", str(home))
        if isinstance(value, list):
            return [fill(v) for v in value]
        if isinstance(value, dict):
            return {k: fill(v) for k, v in value.items()}
        return value
    return plistlib.dumps(fill(plistlib.loads((TEMPLATES / f"{label}.plist").read_bytes()))).decode()


def live(pueue):
    return [t["id"] for t in pueue.status()["tasks"].values() if control.status_name(t) in ("Running", "Paused")]


def reopen(m, state):
    """Abre los grupos propios no retenidos del daemon que responde; suelta cada uno tras su start."""
    groups = m.pueue.status()["groups"]
    for group in sorted(set(state["owned"]) - state["retenidos"].keys()):
        if groups.get(group, {}).get("status") == "Paused":
            m.pueue.call("start", "-g", group)
            control.log(m.state, "reanudado", grupo=group, motivo="relevo")
        state["owned"] = sorted(set(state["owned"]) - {group})
        control.save_control(m.state, state)


def drain(m, state, target, minutes):
    """Con el candado tomado: cierra los grupos abiertos con dueño y espera a que no corra nada."""
    groups = m.pueue.status()["groups"]
    running = sorted(g for g, v in groups.items() if v["status"] == "Running")
    state["relevo"] = {"objetivo": target, "fase": "preparando"}
    state["owned"] = sorted(set(state["owned"]) | set(running))
    control.save_control(m.state, state)
    control.log(m.state, "relevo_iniciado", objetivo=target, grupos=running)
    for group in running:
        m.pueue.call("pause", "--wait", "-g", group)
    deadline = time.monotonic() + minutes * 60
    while live(m.pueue):
        if time.monotonic() >= deadline:
            state["relevo"] = None
            control.save_control(m.state, state)
            reopen(m, state)
            control.log(m.state, "relevo_abortado", objetivo=target, motivo="tareas en marcha")
            raise RuntimeError("Abortado sin tocar el daemon: siguen tareas en marcha; grupos reabiertos. "
                               "Reintentar con --esperar MINUTOS")
        time.sleep(5)
    state["relevo"]["fase"] = "relevando"
    control.save_control(m.state, state)


def handover(m, target):
    """Con el candado tomado e idempotente: deja cargado sólo el servicio objetivo."""
    if target == "propio":
        m.place(LABEL)
        m.launch("enable", f"{m.domain}/{LABEL}")
        # Su preparar espera el candado y rechaza arrancar mientras responda el daemon de brew.
        m.bootstrap(LABEL)
        m.launch("disable", f"{m.domain}/{BREW}")
        m.bootout(BREW)
    else:
        m.bootout(LABEL)
        m.plist(LABEL).unlink(missing_ok=True)
        m.launch("enable", f"{m.domain}/{BREW}")
        m.bootstrap(BREW)


def wait_settled(m, target):
    """Sin el candado, porque el preparar de io.pao.pueue lo necesita."""
    label = LABEL if target == "propio" else BREW
    deadline = time.monotonic() + m.wait_seconds
    kicked = None
    while not m.settled(target):
        now = time.monotonic()
        if now >= deadline:
            return False
        if kicked is None or now - kicked >= 5:
            # Salta ThrottleInterval tras un preparar rechazado, o arranca brew si un pid vivo lo frenó.
            m.launch("kickstart", f"{m.domain}/{label}", check=False)
            kicked = now
        time.sleep(0.5)
    return True


def finish(m, target):
    """Reabre las pausas propias en el daemon objetivo y sólo entonces cierra el relevo."""
    with control.locked(m.state):
        state = control.load_control(m.state)
        if state["relevo"] is None:
            return True
        if not m.settled(target):
            control.log(m.state, "relevo_pendiente", objetivo=target)
            return False
        reopen(m, state)
        if target == "brew":
            m.plist(GUARD).unlink(missing_ok=True)
        state["relevo"] = None
        control.save_control(m.state, state)
        control.log(m.state, "relevo_completado", objetivo=target)
    if target == "brew":
        m.bootout(GUARD)
    return True


def preflight(m, action):
    version = subprocess.run([str(m.pueued), "--version"], text=True, capture_output=True).stdout.strip()
    if version != control.PUEUED_VERSION:
        raise RuntimeError(f"Se esperaba {control.PUEUED_VERSION} y hay {version or 'nada'}")
    pending = control.load_control(m.state)["relevo"]
    if pending and pending["objetivo"] != ("propio" if action == "install" else "brew"):
        raise RuntimeError(f"Hay un relevo hacia {pending['objetivo']} sin terminar; vigilar lo completa")
    if not m.plist(BREW).exists():
        raise RuntimeError(f"Falta {m.plist(BREW)}: sin él no hay a dónde volver")
    if action == "install":
        brew = os.environ.get("PAO_BREW_BIN", "/opt/homebrew/bin/brew")
        pinned = subprocess.run([brew, "list", "--pinned"], text=True, capture_output=True)
        if "pueue" not in pinned.stdout.split():
            raise RuntimeError("pueue no está fijado en brew (brew pin pueue); preparar rechaza otra versión")
        paths = control.pueue_paths("mini")
        control.check_config(paths["config"])
        disk = control.read_queue(paths["data"] / "state.json") or {"tasks": {}}
        if sorted(disk["tasks"]) != sorted(m.pueue.status()["tasks"]):
            raise RuntimeError("state.json no coincide con pueue status; la ruta de estado no es la esperada")


def install(m, minutes, dry):
    preflight(m, "install")
    if dry:
        print("SECO: copiar control.py, mini.py y plists a ~/.local/lib/pao-pueue-desk; cargar io.pao.pueue-guardia")
        print(f"SECO: anotar relevo y pausar con dueño los grupos abiertos; esperar hasta {minutes} min sin tareas en marcha")
        print(f"SECO: cargar io.pao.pueue; disable y bootout {BREW}; reabrir las pausas propias")
        return
    m.library.mkdir(parents=True, exist_ok=True)
    for name in ("control.py", "mini.py"):
        control.atomic_text(m.library / name, (ROOT / name).read_text())
    for label in (LABEL, GUARD):
        control.atomic_text(m.library / "launchagents" / f"{label}.plist", render(label, m.home))
    # El vigilante va primero y se retira el último: es quien completa un relevo interrumpido.
    m.place(GUARD)
    m.launch("enable", f"{m.domain}/{GUARD}")
    m.bootstrap(GUARD)
    with control.locked(m.state):
        state = control.load_control(m.state)
        if state["relevo"] != {"objetivo": "propio", "fase": "relevando"}:
            drain(m, state, "propio", minutes)
        handover(m, "propio")
    if not (wait_settled(m, "propio") and finish(m, "propio")):
        raise RuntimeError("io.pao.pueue no respondió a tiempo; io.pao.pueue-guardia completa el relevo")
    print("io.pao.pueue instalado: preparar antes de cada arranque, KeepAlive y cola conservada")


def withdraw(m, minutes, dry):
    preflight(m, "withdraw")
    if not (m.loaded(LABEL) or m.plist(LABEL).exists() or m.loaded(GUARD)):
        print("io.pao.pueue no está instalado; nada que retirar")
        return
    if dry:
        print(f"SECO: anotar relevo y pausar con dueño los grupos abiertos; esperar hasta {minutes} min sin tareas en marcha")
        print(f"SECO: bootout io.pao.pueue y quitar su plist; enable y bootstrap {BREW}")
        print("SECO: reabrir las pausas propias; quitar y descargar io.pao.pueue-guardia; conservar cola y estado")
        return
    with control.locked(m.state):
        state = control.load_control(m.state)
        if state["relevo"] != {"objetivo": "brew", "fase": "relevando"}:
            drain(m, state, "brew", minutes)
        handover(m, "brew")
    if not (wait_settled(m, "brew") and finish(m, "brew")):
        raise RuntimeError(f"{BREW} no respondió a tiempo; io.pao.pueue-guardia completa el relevo")
    print(f"Retirado io.pao.pueue; {BREW} atiende la cola, conservada")


def vigilar(m):
    """Lo que corre io.pao.pueue-guardia cada 30 s: completar un relevo, acotar host.log y reabrir."""
    control.cap_log(m.state / "host.log")
    with control.locked(m.state):
        state = control.load_control(m.state)
        pending = state["relevo"]
        if pending and pending["fase"] == "preparando":
            # El instalador murió antes de tocar servicios: se revierte y el guardia reabre.
            state["relevo"] = None
            control.save_control(m.state, state)
            control.log(m.state, "relevo_revertido", objetivo=pending["objetivo"])
            pending = None
        elif pending:
            handover(m, pending["objetivo"])
    if pending:
        wait_settled(m, pending["objetivo"])
        finish(m, pending["objetivo"])
        return
    if not m.plist(GUARD).exists():
        # Retiro terminado salvo la descarga del propio vigilante.
        m.bootout(GUARD)
        return
    with control.locked(m.state):
        control.guard(m.pueue, None, m.state, host="mini")


def stop(m):
    """Parada de mantenimiento: primero el vigilante, que si no reabriría y reiniciaría; después el daemon."""
    if control.load_control(m.state)["relevo"]:
        raise RuntimeError("Hay un relevo sin terminar; esperar a que vigilar lo complete")
    m.bootout(GUARD)
    m.bootout(LABEL)
    print("Detenidos io.pao.pueue-guardia e io.pao.pueue; cola conservada. Volver con: mini.py arrancar")


def start(m):
    m.bootstrap(LABEL)
    m.bootstrap(GUARD)
    print("Cargados io.pao.pueue e io.pao.pueue-guardia")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "withdraw", "vigilar", "parar", "arrancar"])
    parser.add_argument("--seco", action="store_true")
    parser.add_argument("--esperar", type=float, default=0, metavar="MIN",
                        help="minutos de espera a que terminen las tareas en marcha antes del relevo")
    args = parser.parse_args(argv)
    m = Mini()
    try:
        if args.action == "install":
            install(m, args.esperar, args.seco)
        elif args.action == "withdraw":
            withdraw(m, args.esperar, args.seco)
        elif args.action == "vigilar":
            vigilar(m)
        elif args.action == "parar":
            stop(m)
        else:
            start(m)
    except Exception as error:
        control.log(m.state, "error", accion=args.action, detalle=str(error))
        if args.action == "vigilar":
            return 1
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
