"""Esperadores propios de prueba: EOF ligado o supervivencia legada acotada."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

mode, directory = sys.argv[1:]
directory = Path(directory)
if mode == 'parent-linked' or mode == 'parent-legacy':
    child_mode = mode.split('-', 1)[1]
    child = subprocess.Popen([sys.executable, '-B', __file__, child_mode, str(directory)],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(json.dumps(dict(parent=os.getpid(), helper=child.pid)), flush=True)
    # Fin normal del padre: el único escritor del canal desaparece.
    child.stdin.close()
else:
    (directory / 'helper-ready').write_text(str(os.getpid()))
    if mode == 'linked':
        sys.stdin.buffer.read()
    else:
        time.sleep(2)
    (directory / 'helper-ended').write_text(mode)
