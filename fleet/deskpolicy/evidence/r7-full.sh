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
exec /usr/bin/taskset -c 0-1 /bin/bash -c '
set -euo pipefail
/usr/bin/python3 -I fleet/deskpolicy/evidence/r7-fixture-schedules.py
/usr/bin/python3 -I fleet/deskpolicy/tests/run.py
/usr/bin/python3 -I fleet/deskpolicy/evidence/r7-hasito-protocol.py .audit/r7-hasito-estado.py
bin/pao check
if command -v shellcheck >/dev/null; then
    shellcheck --severity=warning fleet/deskpolicy/deploy-desk.sh fleet/deskpolicy/withdraw-desk.sh fleet/deskpolicy/tests/desk-run.sh fleet/deskpolicy/evidence/r7-full.sh fleet/deskpolicy/evidence/r7-regressions.sh
fi
'
