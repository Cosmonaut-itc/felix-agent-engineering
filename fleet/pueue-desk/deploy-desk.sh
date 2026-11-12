#!/usr/bin/env bash
set -euo pipefail
pueue_source=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
exec /usr/bin/python3 -I "$pueue_source/deploy.py" install "$@"
