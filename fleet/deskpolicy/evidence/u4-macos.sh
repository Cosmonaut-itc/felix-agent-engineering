#!/usr/bin/env bash
# Static checks only: the process suite requires the desk's Linux runtime.
set -euo pipefail
export PATH="/opt/homebrew/bin:$PATH"
python3 - <<'PY'
import ast
from pathlib import Path
import py_compile
paths = sorted(Path('fleet/deskpolicy').rglob('*.py'))
for path in paths:
    ast.parse(path.read_text(), filename=str(path), feature_version=(3, 9))
    py_compile.compile(str(path), doraise=True)
print('Python 3.9 syntax + py_compile: OK ({} files)'.format(len(paths)))
PY
bash -n fleet/deskpolicy/deploy-desk.sh fleet/deskpolicy/withdraw-desk.sh fleet/deskpolicy/tests/desk-run.sh fleet/deskpolicy/evidence/u4-macos.sh fleet/deskpolicy/evidence/u4-red.sh
shellcheck --severity=warning fleet/deskpolicy/deploy-desk.sh fleet/deskpolicy/withdraw-desk.sh fleet/deskpolicy/tests/desk-run.sh fleet/deskpolicy/evidence/u4-macos.sh fleet/deskpolicy/evidence/u4-red.sh
bin/pao check
printf 'U4 macOS: syntax, py_compile, ShellCheck and pao check OK\n'
