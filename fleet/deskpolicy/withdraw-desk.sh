#!/usr/bin/env bash
# Retirada posterior al APTO. Conserva claims, locks y binarios para el drenado.
set -euo pipefail
[[ $(uname -s) == Linux ]] || exit 75
[[ $# == 2 && $1 == --apto && -n $2 ]] || { echo 'uso: withdraw-desk.sh --apto REFERENCIA-APTO' >&2; exit 75; }
policy_account_home=$(/usr/bin/python3 -I -c 'import os,pwd; print(pwd.getpwuid(os.getuid()).pw_dir)')
policy_install="$policy_account_home/.local/lib/pao-deskpolicy/v1"
(cd -- "$policy_install" && sha256sum --check SHA256SUMS)
/usr/bin/python3 -I "$policy_install/deskpolicy/cli.py" publicar --desactivar-noche
/usr/bin/python3 -I "$policy_install/deskpolicy/cli.py" estado
echo 'Noche desactivada en la configuración persistente. Drenar y recuperar evidencia antes de retirar consumidores o binarios. No se borró ninguna reserva.'
