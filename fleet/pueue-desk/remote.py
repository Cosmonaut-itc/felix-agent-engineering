"""Instalador remoto: recibe CORE y FILES del transporte local."""
import os
import pwd
import sys


def run(*args, check=True):
    result = subprocess.run(list(args), text=True, capture_output=True)
    if check and result.returncode != 0:
        print(result.stderr.strip() or result.stdout.strip(), file=sys.stderr)
        raise subprocess.CalledProcessError(result.returncode, list(args), result.stdout, result.stderr)
    if check and result.stdout.strip():
        print(result.stdout.strip())
    return result


def remote(action, dry):
    home = Path.home()
    units = home / ".config/systemd/user"
    library = home / ".local/lib/pao-pueue-desk"
    timers = ["pueue-desk-mode.timer", "pueue-desk-guard.timer"]
    names = ["pueued.service", "pueue-desk-mode.service", *timers,
             "pueue-desk-guard.service"]
    pueue = Pueue()
    manual = False
    if action == "install":
        # Todo este bloque es de lectura: incluso --seco rechaza una cola viva.
        active = run("systemctl", "--user", "is-active", "pueued.service", check=False)
        process = run("pgrep", "-u", str(os.getuid()), "-x", "pueued", check=False)
        if active.returncode not in (0, 3, 4) or process.returncode not in (0, 1):
            raise RuntimeError("No se pudo determinar quién administra pueued")
        manual = active.returncode != 0 and process.returncode == 0
        if manual and live_tasks(pueue.status()):
            raise RuntimeError("Instalación abortada sin cambios: pueued manual tiene tareas vivas")
        for binary in ("pueue", "pueued"):
            if not os.access(home / ".local/bin" / binary, os.X_OK):
                raise RuntimeError(f"Falta el binario ejecutable {binary}; no se instala automáticamente")
        print("Preflight: cola manual vacía" if manual else "Preflight: sin daemon manual")
        if dry:
            print("SECO: copiar control.py y unidades a ~/.local/lib/pao-pueue-desk y ~/.config/systemd/user")
            if manual:
                print("SECO: pueue shutdown (daemon manual vacío)")
            print("SECO: loginctl enable-linger; systemctl --user daemon-reload")
            print("SECO: systemctl --user enable --now pueued.service pueue-desk-mode.timer pueue-desk-guard.timer")
            print("SECO: aplicar y comprobar cuotas vigentes; retirar suites sólo si vacío")
            return
        # Linger antes de detener nada: si se rechaza, el daemon manual sigue vivo.
        run("loginctl", "enable-linger", pwd.getpwuid(os.getuid()).pw_name)
        # Releer inmediatamente antes de detener; nunca matar tareas de usuarios.
        if manual:
            if live_tasks(pueue.status()):
                raise RuntimeError("Instalación abortada sin cambios: aparecieron tareas vivas")
            pueue.call("shutdown")
            for attempt in range(50):
                if run("pgrep", "-u", str(os.getuid()), "-x", "pueued", check=False).returncode == 1:
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("pueued manual no terminó; no se reemplazó")
        try:
            install(home, units, library, timers, pueue)
        except Exception:
            if manual:
                print("ATENCIÓN: el pueued manual quedó detenido y el servicio no quedó instalado. "
                      "Restáuralo con ~/.local/bin/pueued -d o vuelve a correr la instalación.", file=sys.stderr)
            raise
    else:
        if dry:
            print("SECO: detener y deshabilitar timers y pueued.service; quitar unidades; daemon-reload")
            print("SECO: conservar cola, configuración y estado de Pueue y del guardia")
            return
        for name in [*timers, "pueue-desk-guard.service", "pueue-desk-mode.service", "pueued.service"]:
            if (units / name).exists():
                if name.endswith(".timer") or name == "pueued.service":
                    run("systemctl", "--user", "disable", "--now", name)
                else:
                    run("systemctl", "--user", "stop", name)
        for name in names:
            (units / name).unlink(missing_ok=True)
        run("systemctl", "--user", "daemon-reload")
        print("Retiro completo; cola y estado de Pueue conservados. Linger se conserva")


def install(home, units, library, timers, pueue):
    units.mkdir(parents=True, exist_ok=True)
    library.mkdir(parents=True, exist_ok=True)
    for name, content in FILES.items():
        target = library / name if name == "control.py" else units / name
        rendered = content.replace("@HOME@", str(home))
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(rendered)
        temporary.replace(target)
    run("systemctl", "--user", "daemon-reload")
    run("systemctl", "--user", "enable", "--now", "pueued.service", *timers)
    # Si ya estaba activo, pueue-desk-mode no vuelve a arrancar solo.
    with locked(STATE_DIR):
        apply_mode(pueue, datetime.now(timezone.utc))
        state = pueue.status()
        if "suites" in state["groups"]:
            if any(task["group"] == "suites" for task in state["tasks"].values()):
                print("Aviso: suites tiene tareas; se conserva")
            else:
                pueue.call("group", "remove", "suites")
                print("Grupo suites vacío retirado")
    print("Servicio y timers habilitados; linger permite arrancar sin login interactivo")
