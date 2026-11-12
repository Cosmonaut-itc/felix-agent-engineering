#!/usr/bin/env bash
set -euo pipefail
retirement_source=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
exec /usr/bin/python3 -I "$retirement_source/deploy.py" "$@"
