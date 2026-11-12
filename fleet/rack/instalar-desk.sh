#!/usr/bin/env bash
set -euo pipefail
if [ "$#" -ne 0 ]; then echo 'uso: instalar-desk.sh' >&2; exit 2; fi
origen=$(cd "$(dirname "$0")" && pwd)
# El alias `desk` de la MacBook entra directo al sshd de Ubuntu en WSL.
ssh -o BatchMode=yes -o ConnectTimeout=10 desk \
  'umask 022; mkdir -p ~/.local/bin && cat > ~/.local/bin/pao-rack-snapshot.nuevo && chmod 755 ~/.local/bin/pao-rack-snapshot.nuevo && mv ~/.local/bin/pao-rack-snapshot.nuevo ~/.local/bin/pao-rack-snapshot' \
  < "$origen/snapshot.py"
