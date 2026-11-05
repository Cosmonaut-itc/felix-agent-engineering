#!/usr/bin/env bash
# Posterior al APTO. Valida todo antes de publicar una instalación de día.
set -euo pipefail
[[ $(uname -s) == Linux ]] || { echo 'Sólo WSL desk' >&2; exit 75; }
[[ $# == 2 && $1 == --apto && -n $2 ]] || { echo 'uso: deploy-desk.sh --apto REFERENCIA-APTO' >&2; exit 75; }
umask 077
policy_source=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
exec /usr/bin/python3 -I "$policy_source/installation.py" --apto "$2"
