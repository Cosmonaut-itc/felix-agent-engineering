#!/usr/bin/env bash
# Ronda 9 de U1, corrida dirigida sobre el head corregido. Corre test_r9.py,
# los archivos de prueba que usan cli.py y `bin/pao check`. Corta en la primera
# etapa que falla.
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
sha256sum --quiet -c fleet/deskpolicy/evidence/r9-inputs.sha256
echo "R9 INSUMOS rc=$?"
/usr/bin/taskset -c 0-1 /usr/bin/python3 -I - <<'PY'
import os, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'fleet'))
from deskpolicy.store import code_digest
print('R9 FIX module_sha256', code_digest(), 'CPUs', sorted(os.sched_getaffinity(0)), flush=True)
rc = 0
for pattern in ('test_r9.py', 'test_policy.py', 'test_r2.py', 'test_r3.py', 'test_r4.py',
                'test_r5.py', 'test_r6.py', 'test_r7.py'):
    suite = unittest.defaultTestLoader.discover('fleet/deskpolicy/tests', pattern=pattern, top_level_dir='fleet')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print('R9 FIX RESUMEN', pattern, 'run', result.testsRun, 'failures', len(result.failures),
          'errors', len(result.errors), 'skipped', len(result.skipped), flush=True)
    if result.testsRun == 0 or not result.wasSuccessful():
        rc = 1
        break
raise SystemExit(rc)
PY
rc=$?
echo "R9 FIX pruebas rc=$rc"
[ "$rc" -eq 0 ] || exit "$rc"
/usr/bin/taskset -c 0-1 bin/pao check
rc=$?
echo "R9 FIX pao check rc=$rc"
exit $rc
