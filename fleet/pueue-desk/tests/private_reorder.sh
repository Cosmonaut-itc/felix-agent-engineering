#!/bin/bash
# U3.6 (opus): corrida privada en la Mini. Sólo por remote-run, lanzado desde la raíz del repositorio:
#   ~/.local/bin/remote-run start --carril rapido --name dirigida-u3-6-opus --sha SHA \
#     --script fleet/pueue-desk/tests/private_reorder.sh
# Un pueued 4.0.4 propio dentro de la copia; ningún LaunchAgent; -B: sin bytecode en el host.
set -uo pipefail
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
echo "python=$(/usr/bin/python3 -V 2>&1) sha=$(git rev-parse HEAD 2>/dev/null || echo sin-git)"
/usr/bin/python3 -I -B fleet/pueue-desk/tests/private_reorder.py --run-private
code=$?
echo "salida=$code"
# Evidencia de limpieza: ningún LaunchAgent con nuestro prefijo y ningún proceso bajo nuestra raíz.
echo "launchagents_pao_u36_opus=$(launchctl print "gui/$(id -u)" 2>/dev/null | grep -c 'pao\.u36\.opus' || true)"
echo "procesos_bajo_la_corrida=$(ps -axo command= | grep -F "$PWD/u36o-" | grep -vc grep || true)"
exit $code
