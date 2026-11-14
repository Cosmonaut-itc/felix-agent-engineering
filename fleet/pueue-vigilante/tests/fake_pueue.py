#!/usr/bin/python3 -I
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


def main() -> int:
    root = Path(os.environ['FAKE_ROOT'])
    nombre, args = Path(sys.argv[0]).name, sys.argv[1:]
    with (root / 'calls').open('a') as salida:
        salida.write(json.dumps([nombre] + args) + '\n')
    if nombre == 'ssh':
        assert args[:4] == ['-o', 'BatchMode=yes', '-o', 'ControlPath=none']
        assert args[4] in ('mini', 'desk')
        assert args[5:] == ['/usr/bin/python3', '-I', '-']
        programa = sys.stdin.read()
        preambulo = '''
import os, subprocess
from pathlib import Path
original = subprocess.run
def privado(argv, *args, **kwargs):
    if argv[0] == '/opt/homebrew/bin/pueue':
        argv = [str(Path(os.environ['FAKE_ROOT']) / '.local/bin/pueue')] + argv[1:]
    return original(argv, *args, **kwargs)
subprocess.run = privado
'''
        return subprocess.run([sys.executable, '-I', '-'], input=preambulo + '\nexec(' + repr(programa) + ')\n',
                              text=True).returncode
    if nombre in ('systemctl', 'launchctl'):
        if (root / 'gestor-falla').exists():
            return 1
        return 0
    if nombre != 'pueue':
        raise AssertionError('doble desconocido')
    ruta = root / 'pueue.json'
    if args == ['status', '--json']:
        if (root / 'status-falla').exists():
            return 1
        if (root / 'json-invalido').exists():
            print('{')
            return 0
        secuencia = root / 'secuencia.json'
        if secuencia.exists():
            colas = json.loads(secuencia.read_text())
            if colas:
                ruta.write_text(json.dumps(colas.pop(0)))
                secuencia.write_text(json.dumps(colas))
        print(ruta.read_text())
        return 0
    if args[0] != 'kill':
        raise AssertionError('sólo status/kill en este doble')
    ident = args[-1]
    suave = args == ['kill', '-s', 'SIGTERM', ident]
    assert suave or args == ['kill', ident]
    if (root / ('inmortal-' + ident)).exists() or (suave and (root / ('obstinada-' + ident)).exists()):
        return 0
    cola = json.loads(ruta.read_text())
    tarea = cola['tasks'][ident]
    detalle = tarea['status']['Running']
    logs = root / ('Library/Application Support/pueue/task_logs' if os.environ.get('FAKE_HOST') == 'mini'
                   else '.local/share/pueue/task_logs')
    log = logs / (ident + '.log')
    if log.exists():
        assert 'PAO_VIGILANTE_CORTE' not in log.read_text()
        with log.open('a') as salida:
            salida.write('última salida del hijo\n')
    tarea['status'] = {'Done': dict(detalle, result={'Failed': 9})}
    ruta.write_text(json.dumps(cola))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
