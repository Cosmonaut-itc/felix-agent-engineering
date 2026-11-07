#!/usr/bin/env bash
set -euo pipefail
case "$(pwd -P)" in "$HOME"/remote-run/work/*) ;; *) exit 75 ;; esac
r3_own=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
/usr/bin/python3 -I - "$r3_own" <<'PY'
import os, sys, time
from pathlib import Path
own = Path(sys.argv[1])
starts = [(p / 'started').stat().st_mtime if (p / 'started').is_file() else p.stat().st_mtime
          for p in (Path.home() / 'remote-run/runs').iterdir()
          if p.is_dir() and not p.is_symlink() and p != own]
age = time.time() - max(starts, default=0)
load = os.getloadavg()[0]
print('GATE load1', load, 'since_last_other_start_s', age, flush=True)
raise SystemExit(0 if load < 9 and age >= 120 else 75)
PY
exec /usr/bin/taskset -c 0-1 /usr/bin/python3 -I - <<'PY'
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'fleet'))
suite = unittest.defaultTestLoader.discover('fleet/deskpolicy/tests', pattern='test_r3.py', top_level_dir='fleet')
raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
PY
