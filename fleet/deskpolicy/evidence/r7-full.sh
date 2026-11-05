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
        proc = Path('/proc', str(pid))
        if (proc/'cwd').resolve() != entry:
            continue  # Ignore an obsolete run file whose PID has been reused.
        print('OTHER_RUN', entry.name, 'pid', pid, 'cwd', (proc/'cwd').resolve(), flush=True)
        parents = {pid}
        processes = {}
        for candidate in Path('/proc').iterdir():
            if not candidate.name.isdigit():
                continue
            try:
                stat = (candidate/'stat').read_text().rsplit(')', 1)[1].split()
                processes[int(candidate.name)] = (int(stat[1]), candidate)
            except (OSError, ValueError):
                pass
        seen = set()
        while parents:
            children = {child for child, (parent, _) in processes.items() if parent in parents and child not in seen}
            for child in sorted(children):
                candidate = processes[child][1]
                try:
                    print('OTHER_WORK', entry.name, child, (candidate/'comm').read_text().strip(),
                          (candidate/'cwd').resolve(), flush=True)
                except OSError:
                    pass
            seen.update(children)
            parents = children
    except (OSError, ValueError):
        pass
raise SystemExit(0 if load < limit and age >= 120 else 75)
PY
exec /usr/bin/taskset -c 0-1 /bin/bash -c '
set -euo pipefail
sha256sum --quiet -c fleet/deskpolicy/evidence/r7-inputs.sha256
/usr/bin/python3 -I fleet/deskpolicy/evidence/r7-fixture-schedules.py
/usr/bin/python3 -I fleet/deskpolicy/tests/run.py
/usr/bin/python3 -I fleet/deskpolicy/evidence/r7-hasito-protocol.py .audit/r7-hasito-estado.py
bin/pao check
/usr/bin/python3 -I fleet/deskpolicy/evidence/r7-cleanup.py
if command -v shellcheck >/dev/null; then
    shellcheck --severity=warning fleet/deskpolicy/deploy-desk.sh fleet/deskpolicy/withdraw-desk.sh fleet/deskpolicy/tests/desk-run.sh fleet/deskpolicy/evidence/r7-full.sh fleet/deskpolicy/evidence/r7-regressions.sh
fi
'
