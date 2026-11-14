from __future__ import annotations

import importlib.util
import itertools
from pathlib import Path
import unittest

from test_vigilante import ROOT, v

# Sólo lectura por ruta. Nunca se ejecuta main ni se importa el paquete del rack.
spec = importlib.util.spec_from_file_location('rack_snapshot_paridad', ROOT.parent / 'rack/snapshot.py')
rack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rack)


class ParidadTests(unittest.TestCase):
    def test_reconocida_por_rack_nunca_es_manual(self):
        home = Path.home()
        etiquetas = (None, rack.BODEGA, 'x tope=9', 'apotheca-remote-run-2')
        rutas = (str(home / 'remote-run/runs/20261114-123456-abcdef'),
                 str(home / 'remote-run/runs/otro'), '/tmp', str(home / 'sen-56xx-dbgen'))
        comandos = (f'exec bash {home}/.local/state/jale/pueue/job-Ab123456/task.sh',
                    'exec bash /otro/job-Ab123456/task.sh', 'python3 pueue.py exec x',
                    'bash /home/felix/sen-56xx-dbgen/run.sh')
        for etiqueta, ruta, comando in itertools.product(etiquetas, rutas, comandos):
            with self.subTest(etiqueta=etiqueta, ruta=ruta, comando=comando):
                raw = dict(id=7, label=etiqueta, path=ruta, command=comando)
                control = rack.task_control(raw, home, 'Running')
                clasificacion = v.clasificar(v.Tarea(7, 'h', 'jale', 'Running', 0,
                                                    etiqueta, comando, ruta), home)
                if control['launcher'] is not None or etiqueta == rack.BODEGA:
                    self.assertIsNot(clasificacion.clase, v.Clase.MANUAL)
