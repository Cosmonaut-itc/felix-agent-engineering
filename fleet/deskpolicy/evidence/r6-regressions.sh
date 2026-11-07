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
raise SystemExit(0 if load < limit and age >= 120 else 75)
PY
set +e
/usr/bin/taskset -c 0-1 /usr/bin/python3 -I - <<'PY'
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path.cwd()/'fleet'))
from deskpolicy.store import code_digest
print('R6 module_sha256', code_digest(), flush=True)
suite = unittest.defaultTestLoader.discover('fleet/deskpolicy/tests', pattern='test_r6.py', top_level_dir='fleet')
raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
PY
r6_code=$?
echo "RESULT r6=$r6_code"
/usr/bin/taskset -c 0-1 /usr/bin/python3 -I fleet/deskpolicy/evidence/r6-original-sync-late.py
r6_sync=$?
/usr/bin/taskset -c 0-1 /usr/bin/python3 -I fleet/deskpolicy/evidence/r6-original-receipt-recovery.py
r6_receipt=$?
echo "RESULT regressions=$r6_code original_sync=$r6_sync original_receipt=$r6_receipt"
exit "$((r6_code || r6_sync || r6_receipt))"
