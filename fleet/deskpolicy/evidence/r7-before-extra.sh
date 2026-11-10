#!/usr/bin/env bash
set -euo pipefail
case "$(pwd -P)" in "$HOME"/remote-run/work/*) ;; *) exit 75 ;; esac
r7_own=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
/usr/bin/taskset -c 0-1 /usr/bin/python3 -I - "$r7_own" <<'PY'
import datetime as dt, os, sys, time
from pathlib import Path
own = Path(sys.argv[1])
starts = [(p/'started').stat().st_mtime if (p/'started').is_file() else p.stat().st_mtime
          for p in (Path.home()/'remote-run/runs').iterdir()
          if p.is_dir() and not p.is_symlink() and p != own]
age = time.time() - max(starts, default=0)
limit = 12 if dt.datetime.now(dt.timezone.utc) < dt.datetime(2026,11,5,22,tzinfo=dt.timezone.utc) else 9
load = os.getloadavg()[0]
print('GATE load1', load, 'since_last_other_start_s', age, 'limit', limit, flush=True)
for entry in sorted((Path.home()/'remote-run/runs').iterdir()):
    if entry == own or (entry/'exit').exists() or not (entry/'pid').is_file():
        continue
    try:
        pid = int((entry/'pid').read_text().strip())
        if Path('/proc', str(pid)).exists():
            print('OTHER_RUN', entry.name, 'pid', pid, flush=True)
    except (OSError, ValueError):
        pass
raise SystemExit(0 if load < limit and age >= 120 else 75)
PY
cd .audit/r7-baseline
/usr/bin/taskset -c 0-1 /usr/bin/python3 -I - <<'PYHASH'
from pathlib import Path
import sys
sys.path.insert(0, str(Path.cwd()/'fleet'))
from deskpolicy.store import code_digest
print('BASELINE sha256', code_digest(), flush=True)
assert code_digest() == '912e872a4a49c287b77f1753f010eafac4b941e6ef3727a45ee19ddd34c08cfd'
PYHASH
set +e
/usr/bin/taskset -c 0-1 /usr/bin/python3 -I fleet/deskpolicy/evidence/r7-fixture-schedules.py
r7_timing=$?
/usr/bin/taskset -c 0-1 /usr/bin/python3 -I fleet/deskpolicy/evidence/r7-hasito-protocol.py hasito-estado.py
r7_client=$?
echo "BASELINE timing=$r7_timing client=$r7_client"
exit "$((r7_timing || r7_client))"
