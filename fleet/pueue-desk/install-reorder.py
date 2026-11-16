#!/usr/bin/python3 -I
"""Actualizar U3.6 en una Mini adoptada, sin reiniciar daemon ni jobs."""
import argparse
import os
from pathlib import Path
import sys
import types

ROOT=Path(__file__).resolve().parent
c=types.ModuleType('u36_install');c.__file__=str(ROOT/'control.py')
# -I ignores PYTHONDONTWRITEBYTECODE: even the preflight's local import is pure.
exec(compile((ROOT/'control.py').read_bytes(),c.__file__,'exec'),c.__dict__)


def publish(path, raw):
    temporary=path.with_name(path.name+'.u36-tmp')
    fd=os.open(str(temporary),os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'wb') as stream:
        stream.write(raw);c.sync_file(stream)
    temporary.replace(path);c.sync_directory(path.parent)


def install(home=None, dry=False, withdraw=False, engine=None, config=None):
    home=Path.home() if home is None else Path(home)
    library=home/'.local/lib/pao-pueue-mini'
    directory=home/'.local/state/pao/pueue-mini'
    if not library.is_dir() or not (directory/'control.lock').is_file():
        raise ValueError('Se requiere la Mini adoptada y el relevo U3.5 instalado')
    engine=engine or c.MiniControl(c.Pueue('/opt/homebrew/bin/pueue',env=c.mini_environment(home),cwd=home),directory)
    with c.locked(directory,create=False):
        with engine.connect() as db:
            state=engine.state(db)
            actions=[__import__('json').loads(row[0]) for row in db.execute('SELECT body FROM host_actions')]
        if (state.get('mini_handoff',{}).get('phase')!='done'
                or state.get('startup',{}).get('phase') not in (None,'ready')
                or any(a['response']['status'] in ('applying','partial','unknown') or
                       a.get('kind')=='reorder' and a['phase']!='terminal' for a in actions)):
            raise ValueError('Hay una intención pendiente; recuperar antes de publicar o revertir')
        # Shared validation is deliberately before even backups or temporary files.
        config=config or c.validate_configuration(home=home,pueue='/opt/homebrew/bin/pueue',daemon='/opt/homebrew/bin/pueued',env={},cwd=home)
        if Path(state['pid_file'])!=config['pid_file'] or engine.generation(state)!=state['generation']:
            raise ValueError('Generación o configuración distinta; no se publica')
        observed,_=engine.observed()
        if observed!=state['desired']:
            raise ValueError('Cuotas sin reconciliar; no se publica')
        target=library/'control.py';backup=library/'control.py.u36-before'
        if withdraw and not backup.is_file():
            raise ValueError('No hay copia U3.6 para revertir')
        for name in ('control.py','editor_prioridad.py'):
            compile((ROOT/name).read_text(),name,'exec')
        if dry:
            print('SECO: configuración, autoridad, generación e intenciones verificadas; cero efectos')
            print('SECO: '+('restaurar control.py.u36-before; conservar helper inerte' if withdraw else
                  'conservar copia anterior → publicar helper fijo → publicar control.py bajo control.lock'))
            print('SECO: sin cambios de cuotas/modo, daemon, LaunchAgents ni fila; reorder queda bloqueado por contrato de clientes')
            return
        if withdraw:
            publish(target,backup.read_bytes())
        else:
            if not backup.exists():publish(backup,target.read_bytes())
            publish(library/'editor_prioridad.py',(ROOT/'editor_prioridad.py').read_bytes())
            publish(target,(ROOT/'control.py').read_bytes())


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seco',action='store_true')
    parser.add_argument('--retirar',action='store_true')
    args=parser.parse_args()
    if sys.platform!='darwin':parser.error('Este actualizador es sólo para la Mini macOS')
    install(dry=args.seco,withdraw=args.retirar)


if __name__=='__main__':main()
