"""Clientes CLI de procesos distintos; sólo el harness puede inyectar fronteras."""
import io
import json
from pathlib import Path
import sys
import time
from hostgate.admission import Hostgate
from hostgate.cli import main
from hostgate.tests.support.world import World, SharedWorld, config, request

path, operation = sys.argv[1:3]
executor, pid, world_path, command = (sys.argv[3:] if len(sys.argv) > 3 else ('unix', '100', '', 'reservar'))
world = SharedWorld(world_path) if world_path else World()
auth = world.auth(pid=int(pid), executor=executor)
gate = Hostgate(config(Path(path)), world)
payload = request(operation, operation, executorId=executor)
if command == 'estado':
    payload = dict(schema=payload['schema'], operationId=operation, operation='estado')
deadline = time.monotonic() + 15
while True:
    out = io.StringIO()
    code = main([command], gate=gate, auth=auth, stdin=io.StringIO(json.dumps(payload)), stdout=out)
    if code != 4 or time.monotonic() >= deadline: break
    time.sleep(0.05)
print(out.getvalue(), end='')
raise SystemExit(code)
