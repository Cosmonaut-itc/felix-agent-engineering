#!/usr/bin/env bash
set -euo pipefail
case "$(pwd -P)" in "$HOME"/remote-run/work/*) ;; *) exit 75 ;; esac
r6_own=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
/usr/bin/python3 -I - "$r6_own" <<'PY'
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
        proc = Path('/proc') / str(pid)
        if proc.exists():
            print('OTHER_RUN', entry.name, 'pid', pid, 'cwd', (proc/'cwd').resolve(), flush=True)
    except (OSError, ValueError):
        pass
raise SystemExit(0 if load < limit and age >= 120 else 75)
PY
if [ -f .audit/r6-baseline/fleet/deskpolicy/tests/test_r6.py ]; then
    (
        cd .audit/r6-baseline
        /usr/bin/taskset -c 0-1 /usr/bin/python3 -I - <<'PYBASE'
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path.cwd()/'fleet'))
try:
    from deskpolicy.store import code_digest
    print('R6 BASELINE runtime', code_digest(), flush=True)
    if code_digest() != '77a369608532867e785aa7eff8f496e9a2480ff7a3b8d3b98a4d826901e7200d':
        raise ValueError('baseline-runtime-mismatch')
    suite = unittest.defaultTestLoader.discover('fleet/deskpolicy/tests', pattern='test_r6.py', top_level_dir='fleet')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.errors or len(result.failures) != 13:
        raise ValueError('baseline-requires-thirteen-failures-and-no-errors')
except Exception as exc:
    print('R6 BASELINE FIXTURE ERROR', repr(exc), flush=True)
    raise SystemExit(2)
raise SystemExit(1)
PYBASE
    ) && r6_before=0 || r6_before=$?
    echo "R6 BASELINE RC=$r6_before (expected 1)"
    test "$r6_before" -eq 1
fi
exec /usr/bin/taskset -c 0-1 /bin/bash -c '
set -euo pipefail
/usr/bin/python3 -I fleet/deskpolicy/evidence/r5-fixture-schedules.py
/usr/bin/python3 -I fleet/deskpolicy/tests/run.py
/usr/bin/python3 -I fleet/deskpolicy/evidence/r6-original-sync-late.py
/usr/bin/python3 -I fleet/deskpolicy/evidence/r6-original-receipt-recovery.py
/usr/bin/python3 -I fleet/deskpolicy/evidence/r6-hasito-protocol.py .audit/r6-hasito-estado.py
/usr/bin/python3 -I fleet/deskpolicy/evidence/r5-original-c3.py
bin/pao check
if command -v shellcheck >/dev/null; then
    shellcheck --severity=warning fleet/deskpolicy/deploy-desk.sh fleet/deskpolicy/withdraw-desk.sh fleet/deskpolicy/tests/desk-run.sh fleet/deskpolicy/evidence/r5-regressions.sh
fi
'
