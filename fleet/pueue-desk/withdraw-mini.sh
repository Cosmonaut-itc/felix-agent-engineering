#!/usr/bin/env bash
# Corre en la propia Mini: mini.py withdraw [--seco] [--esperar MIN].
set -euo pipefail
pueue_source=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
exec /usr/bin/python3 -I "$pueue_source/mini.py" withdraw "$@"
