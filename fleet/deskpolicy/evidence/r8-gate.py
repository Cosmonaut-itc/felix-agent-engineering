"""Read-only Estado 60 admission gate, also used before remote-run start."""
import os
from pathlib import Path
import shutil
import sys
import time

own = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
roots = [Path.home() / 'remote-run/runs', *Path('/tmp/sen-test').glob('slot-*/runs')]
starts = []
for root in roots:
    if not root.exists():
        continue
    for entry in root.iterdir():
        if not entry.is_dir() or entry.is_symlink() or entry.resolve() == own:
            continue
        stamp = entry / 'started'
        starts.append((stamp if stamp.is_file() else entry).stat().st_mtime)
age = time.time() - max(starts, default=0)
load = os.getloadavg()[0]
memory = int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines()
                  if line.startswith('MemAvailable:'))) * 1024
root_free = shutil.disk_usage('/').free
c_free = shutil.disk_usage('/mnt/c').free
print('GATE load1', load, 'since_last_other_start_s', age, 'MemAvailable_bytes', memory,
      'root_free_bytes', root_free, 'C_free_bytes', c_free, flush=True)
raise SystemExit(0 if load < 12 and age >= 120 and memory >= 6 * 1024**3
                 and root_free >= 100 * 10**9 and c_free >= 60 * 10**9 else 75)
