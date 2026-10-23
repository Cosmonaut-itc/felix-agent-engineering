"""Entrada exclusiva del harness: clientes de procesos distintos, SQLite compartido."""
import json
from pathlib import Path
import sys
from hostgate.admission import Hostgate
from hostgate.cli import main
from hostgate.tests.support.world import World, config, request

path, operation = sys.argv[1:]
world = World()
auth = world.auth()
gate = Hostgate(config(Path(path)), world)
import io
raise SystemExit(main(['reservar'], gate=gate, auth=auth,
                      stdin=io.StringIO(json.dumps(request(operation, operation)))))
