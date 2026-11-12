#!/usr/bin/env bash
set -euo pipefail
if [ "$#" -ne 0 ]; then echo 'uso: instalar-desk.sh' >&2; exit 2; fi
origen=$(cd "$(dirname "$0")" && pwd)
ssh_args=(-o BatchMode=yes -o ConnectTimeout=10 -o ControlMaster=no -o ControlPath=none)
ssh "${ssh_args[@]}" desk 'wsl -d Ubuntu -u felix -- mkdir -p /home/felix/.local/bin'
ssh "${ssh_args[@]}" desk 'wsl -d Ubuntu -u felix -- sh -c "umask 022; cat > /home/felix/.local/bin/pao-rack-snapshot; chmod 755 /home/felix/.local/bin/pao-rack-snapshot"' < "$origen/snapshot.py"
