#!/usr/bin/env bash
# Preparación posterior al APTO. No habilita noche ni inicia procesos.
set -euo pipefail
[[ $(uname -s) == Linux ]] || { echo 'Sólo WSL desk' >&2; exit 75; }
[[ $# == 2 && $1 == --apto && -n $2 ]] || { echo 'uso: deploy-desk.sh --apto REFERENCIA-APTO' >&2; exit 75; }
[[ -x /usr/bin/taskset && -x /usr/bin/flock ]] || exit 75
umask 077
policy_account_home=$(/usr/bin/python3 -I -c 'import os,pwd; print(pwd.getpwuid(os.getuid()).pw_dir)')
policy_source=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
policy_install="$policy_account_home/.local/lib/pao-deskpolicy/v1"
policy_config="$policy_account_home/.config/pao"
[[ ! -e $policy_install && ! -L $policy_install ]] || { echo 'La instalación ya existe. No se reemplaza un runtime en uso.' >&2; exit 75; }
mkdir -p -- "$policy_account_home/.local/lib/pao-deskpolicy" "$policy_config"
/usr/bin/python3 -I - "$policy_install" "$policy_config" <<'PY'
import os
from pathlib import Path
import stat
import sys
for raw in sys.argv[1:]:
    path = Path(raw)
    if path.resolve() != path.absolute():
        raise SystemExit("Se rechazan symlinks en rutas de instalación")
    parent = path if path.exists() else path.parent
    info = parent.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise SystemExit("Se requiere directorio privado 0700 del uid actual")
PY
[[ ! -e $policy_config/desk-policy.json && ! -L $policy_config/desk-policy.json ]] || { echo 'La configuración existente se conserva. Revisión operativa necesaria.' >&2; exit 75; }
[[ ! -L /tmp/sen-test-slots ]] || exit 75
if [[ -d /tmp/sen-test-slots ]]; then
    [[ $(stat -c '%a' /tmp/sen-test-slots) == 700 ]] || { echo 'Drenar y preparar la raíz 0700 antes del corte. No se cambia automáticamente.' >&2; exit 75; }
fi
mkdir -- "$policy_install"
mkdir -- "$policy_install/deskpolicy"
for policy_file in __init__.py cli.py engine.py model.py store.py testing.py; do
    install -m 600 -- "$policy_source/$policy_file" "$policy_install/deskpolicy/$policy_file"
done
printf '%s\n' "$2" > "$policy_install/APTO"
(
    cd -- "$policy_install"
    sha256sum deskpolicy/*.py > SHA256SUMS
)
cat > "$policy_config/desk-policy.json" <<'JSON'
{"schema":"pao/desk-config@1","night_enabled":false,"night_budget":4,"cpu_weight_budget":8,"max_run_weight":8,"min_start_interval_s":120}
JSON
/usr/bin/python3 -I "$policy_install/deskpolicy/cli.py" preflight --preparar
echo 'Instalado sólo para día. Pendientes el censo vacío, evidencia de consumidores y recursos, preflight --armar y el arranque explícito del writer.'
