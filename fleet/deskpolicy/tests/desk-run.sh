#!/usr/bin/env bash
set -euo pipefail

case "$(pwd -P)" in "$HOME"/remote-run/work/*) ;; *) echo 'Se exige el mirror de remote-run' >&2; exit 75 ;; esac
test_run_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
/usr/bin/python3 -I - "$test_run_dir" <<'PY'
import os
import datetime as dt
from pathlib import Path
import sys
import time

limit = 12 if dt.datetime.now(dt.timezone.utc) < dt.datetime(2026, 11, 5, 22, tzinfo=dt.timezone.utc) else 9
if os.getloadavg()[0] >= limit:
    raise SystemExit("Gate: carga >= " + str(limit))
own = Path(sys.argv[1])
starts = [(Path(entry.path) / "started").stat().st_mtime
          if (Path(entry.path) / "started").is_file() else entry.stat(follow_symlinks=False).st_mtime
          for entry in os.scandir(Path.home() / "remote-run/runs")
          if entry.is_dir(follow_symlinks=False) and Path(entry.path) != own]
if starts and time.time() - max(starts) < 120:
    raise SystemExit("Gate: no pasaron 120 s desde el último arranque ajeno")
print("GATE load1", os.getloadavg()[0], "since_last_other_start_s",
      time.time() - max(starts, default=0), "limit", limit, flush=True)
PY
exec /usr/bin/taskset -c 0-1 /bin/bash -c '
set -euo pipefail
/usr/bin/python3 -I fleet/deskpolicy/tests/run.py
bin/pao check
if command -v shellcheck >/dev/null; then
    shellcheck --severity=warning fleet/deskpolicy/deploy-desk.sh fleet/deskpolicy/withdraw-desk.sh fleet/deskpolicy/tests/desk-run.sh
fi
'
