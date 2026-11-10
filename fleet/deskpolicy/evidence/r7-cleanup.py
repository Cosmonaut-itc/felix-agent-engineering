"""Report non-zombie test processes left under this run's mirror."""
import os
from pathlib import Path

root = str(Path.cwd().resolve())
ancestors = set()
pid = os.getpid()
while pid > 0 and pid not in ancestors:
    ancestors.add(pid)
    try:
        pid = int(Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()[1])
    except (OSError, ValueError):
        break
remaining = []
for candidate in Path('/proc').iterdir():
    if not candidate.name.isdigit() or int(candidate.name) in ancestors:
        continue
    try:
        cwd = os.readlink(candidate/'cwd')
        state = (candidate/'stat').read_text().rsplit(')', 1)[1].split()[0]
        if state != 'Z' and (cwd == root or cwd.startswith(root + '/')):
            remaining.append((int(candidate.name), state, cwd))
    except (OSError, ValueError):
        pass
print('LIVE_OWN_MIRROR_PROCESSES', remaining, flush=True)
raise SystemExit(bool(remaining))
