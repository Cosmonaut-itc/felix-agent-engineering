"""Instalador remoto aditivo: recibe el controlador y los archivos del transporte."""
import sys


def run(*args):
    return subprocess.run(list(args), text=True, capture_output=True, check=True)


def remote(action, dry):
    home = Path.home()
    library = home / '.local/lib/pao-pueue-desk'
    if not has_authority(STATE_DIR):
        # Includes --seco. Never stop or adopt a manual daemon incidentally.
        manager = manager_environment(run('systemctl', '--user', 'show-environment').stdout)
        validate_configuration(home=home, env=manager, cwd=home)
        raise ValueError('Instalación abortada sin cambios: U3.4 requiere desk adoptado')
    if action == 'install':
        namespace = dict(__file__=str(library / 'install-desk.py'), __name__='embedded_install')
        exec(INSTALLER, namespace)
        from types import SimpleNamespace
        engine = MiniControl(Pueue(), STATE_DIR, host='desk')
        with engine.connect() as db:
            pid_file = Path(engine.state(db)['pid_file'])
        namespace['install'](home, pid_file, SimpleNamespace(**globals()), dry_run=dry, sources=FILES)
        return
    if dry:
        print('SECO: marcar mantenimiento; parar timers, guardia/modo y daemon al final; conservar autoridad/cola')
        return
    with locked(STATE_DIR):
        atomic_json(STATE_DIR / 'maintenance', dict(reason='retiro autorizado'))
    timers = ('pueue-desk-mode.timer', 'pueue-desk-guard.timer')
    run('systemctl', '--user', 'stop', *timers)
    run('systemctl', '--user', 'stop', 'pueue-desk-guard.service', 'pueue-desk-mode.service')
    run('systemctl', '--user', 'disable', 'pueued.service', *timers)
    run('systemctl', '--user', 'stop', 'pueued.service')
    print('Retirado en mantenimiento; unidades, autoridad y cola conservadas para reversión')
