#!/usr/bin/env bash
# Ambos nombres realizan el mismo retiro; ninguno reinstala la política vieja.
set -euo pipefail
retirement_source=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
exec bash "$retirement_source/deploy-desk.sh" "$@"
