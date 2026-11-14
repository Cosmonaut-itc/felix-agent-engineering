"""Instalador remoto: recibe CORE y FILES del transporte local."""
import os
import pwd
import sys

# Grupos que la tabla ya no gestiona: `suites` pasó a los grupos por proyecto y
# `pg5434` nunca recibió tareas (su lugar pasó a `otros`).
RETIRED_GROUPS = ("suites", "pg5434")
PROC = Path("/proc")


def run(*args, check=True):
    result = subprocess.run(list(args), text=True, capture_output=True)
    if check and result.returncode != 0:
        print(result.stderr.strip() or result.stdout.strip(), file=sys.stderr)
        raise subprocess.CalledProcessError(result.returncode, list(args), result.stdout, result.stderr)
    if check and result.stdout.strip():
        print(result.stdout.strip())
    return result


def remote(action, dry, cuotas=False, pid_file=None):
    home = Path.home()
    # An adopted desk can never return to the bare table, even without --cuotas.
    adopted = has_authority(STATE_DIR)
    cuotas = cuotas or adopted
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
        if cuotas and not dry:
            # Read-only; a missing or corrupt authority stops here, before any write.
            if adopted:
                DeskControl(pueue, STATE_DIR, proc=PROC).view()
            elif active.returncode != 0:
                raise RuntimeError("Adoptar exige pueued.service activo con la tabla vigente; instalar sin --cuotas primero")
            else:
                desk_generation(Path(pid_file or home / ".local/share/pueue/pueue.pid"), PROC)
        if dry:
            print("SECO: copiar control.py y unidades a ~/.local/lib/pao-pueue-desk y ~/.config/systemd/user")
            if manual:
                print("SECO: pueue shutdown (daemon manual vacío)")
            print("SECO: loginctl enable-linger; systemctl --user daemon-reload")
            print("SECO: systemctl --user enable --now pueued.service pueue-desk-mode.timer pueue-desk-guard.timer")
            if adopted:
                print("SECO: bajo control.lock reconciliar el deseo durable del evento vigente; nunca la tabla sola")
            elif cuotas:
                print("SECO: crear slots.disabled; bajo control.lock aplicar la tabla vigente, "
                      "verificarla, adoptar deseo/propiedad y retirar el switch creado")
            else:
                print("SECO: aplicar y comprobar cuotas vigentes")
            print("SECO: retirar suites y pg5434 sólo si vacíos")
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
            install(home, units, library, timers, pueue, cuotas, pid_file)
        except Exception:
            if manual:
                print("ATENCIÓN: el pueued manual quedó detenido y el servicio no quedó instalado. "
                      "Restáuralo con ~/.local/bin/pueued -d o vuelve a correr la instalación.", file=sys.stderr)
            raise
    else:
        if adopted:
            raise RuntimeError("Conservar controlador y guardia: hay deseo durable. "
                               "Para apagar los ajustes, crear slots.disabled (ver README)")
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


def drop_retired_owned():
    """Quita los grupos retirados del estado del guardia. Pide el candado tomado."""
    target = STATE_DIR / "guardia-state.json"
    owned = set(json.loads(target.read_text())) if target.exists() else set()
    if owned & set(RETIRED_GROUPS):
        save_owned(STATE_DIR, owned - set(RETIRED_GROUPS))


def install(home, units, library, timers, pueue, cuotas=False, pid_file=None):
    switch = STATE_DIR / "slots.disabled"
    created = False
    if cuotas and not has_authority(STATE_DIR):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with locked(STATE_DIR):
            # Preserve an operator's switch, including a broken symlink.
            if not os.path.lexists(switch):
                with switch.open("x") as stream:
                    stream.write("instalación en curso\n"); stream.flush(); os.fsync(stream.fileno())
                created = True
    units.mkdir(parents=True, exist_ok=True)
    library.mkdir(parents=True, exist_ok=True)
    for name, content in FILES.items():
        target = library / name if name == "control.py" else units / name
        rendered = content.replace("@HOME@", str(home))
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(rendered)
        temporary.replace(target)
    # Antes de cualquier paso que pueda fallar: el controlador ya publicado
    # aborta si su estado nombra un grupo retirado.
    with locked(STATE_DIR):
        drop_retired_owned()
    run("systemctl", "--user", "daemon-reload")
    run("systemctl", "--user", "enable", "--now", "pueued.service", *timers)
    # Si ya estaba activo, pueue-desk-mode no vuelve a arrancar solo.
    with locked(STATE_DIR):
        engine = DeskControl(pueue, STATE_DIR, proc=PROC)
        if has_authority(STATE_DIR):
            engine.apply()
        else:
            apply_mode(pueue, datetime.now(timezone.utc))
        state = pueue.status()
        for group in RETIRED_GROUPS:
            if group not in state["groups"]:
                continue
            if any(task["group"] == group for task in state["tasks"].values()):
                print(f"Aviso: {group} tiene tareas; se conserva")
            else:
                pueue.call("group", "remove", group)
                print(f"Grupo {group} vacío retirado")
        # Un guardia anterior que aún corría pudo volver a anotarlos.
        drop_retired_owned()
        if cuotas and not has_authority(STATE_DIR):
            # Same lock as the timers: the table just verified becomes the durable desire.
            engine.adopt(pid_file)
            print("Autoridad adoptada: " + json.dumps(engine.view()["desired"]))
        if created:
            switch.unlink()
    print("Servicio y timers habilitados; linger permite arrancar sin login interactivo")
