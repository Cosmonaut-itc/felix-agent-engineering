#!/usr/bin/env bash
# Ronda 9 de U1, corrida base. Corre test_r9.py sobre el runtime y el contrato
# de f03aaa4a, con sólo ese archivo superpuesto. Sale con 0 si todas las pruebas
# nuevas fallan por aserción y ninguna por error.
set -uo pipefail
case "$(pwd -P)" in "$HOME"/remote-run/work/*) ;; *) echo 'Se exige el mirror de remote-run' >&2; exit 75 ;; esac
own=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
/usr/bin/taskset -c 0-1 /usr/bin/python3 -I - "$own" <<'PY' || exit $?
import os, sys, time
from pathlib import Path
own = Path(sys.argv[1]).resolve()
starts = []
for entry in (Path.home() / 'remote-run/runs').iterdir():
    if not entry.is_dir() or entry.is_symlink() or entry.resolve() == own:
        continue
    stamp = entry / 'started'
    starts.append((stamp if stamp.is_file() else entry).stat().st_mtime)
age = time.time() - max(starts, default=0)
load = os.getloadavg()[0]
memory = int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines()
                  if line.startswith('MemAvailable:'))) * 1024
print('GATE load1', load, 'since_last_other_start_s', int(age), 'MemAvailable_bytes', memory, flush=True)
raise SystemExit(0 if load < 9 and age >= 120 and memory >= 6 * 1024**3 else 75)
PY
git rev-parse HEAD | sed 's/^/HEAD=/'
git status --short | sed 's/^/SUCIO /'
base=f03aaa4a7ec59955596cba5129693d5c6fa24f0c
rm -rf .audit/r9-base
mkdir -p .audit/r9-base || exit 70
git archive "$base" fleet | tar -x -C .audit/r9-base || { echo 'BASE: no se pudo extraer f03aaa4a' >&2; exit 70; }
git show HEAD:fleet/deskpolicy/tests/test_r9.py > .audit/r9-base/fleet/deskpolicy/tests/test_r9.py || exit 70
/usr/bin/taskset -c 0-1 /usr/bin/python3 -I - <<'PY'
import os, sys, unittest
from pathlib import Path
os.chdir('.audit/r9-base')
sys.path.insert(0, str(Path.cwd() / 'fleet'))
from deskpolicy.store import code_digest
print('R9 BASE module_sha256', code_digest(), 'CPUs', sorted(os.sched_getaffinity(0)), flush=True)
suite = unittest.defaultTestLoader.discover('fleet/deskpolicy/tests', pattern='test_r9.py', top_level_dir='fleet')
result = unittest.TextTestRunner(verbosity=2).run(suite)
print('R9 BASE RESUMEN run', result.testsRun, 'failures', len(result.failures),
      'errors', len(result.errors), 'skipped', len(result.skipped), flush=True)
esperado = result.testsRun > 0 and len(result.failures) == result.testsRun and not result.errors
raise SystemExit(0 if esperado else 1)
PY
rc=$?
rm -rf .audit/r9-base
echo "R9 BASE rc=$rc (0 = todas fallan por aserción)"
exit $rc
