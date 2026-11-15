#!/usr/bin/env bash
# Sólo el gate nocturno de Hasito desde main puede ejecutar trabajos aquí.
set -euo pipefail
# Salir con 1 no basta: el runner marca el trabajo como fallido y sigue con los
# pasos `if: always()`. El rechazo termina al proceso del trabajo, y con él todos sus pasos.
rechazar() {
  printf 'gha-runner: %s no aceptado: %s\n' "$1" "$2" >&2
  pid=$PPID
  for _ in 1 2 3 4 5 6; do
    case "$(ps -o comm= -p "$pid" 2>/dev/null)" in
      *Runner.Worker)
        # Da tiempo a que la línea llegue al log. KILL: con TERM o INT el runner cancela y corre `always()`.
        sleep 2
        kill -KILL "$pid"
        exit 1 ;;
    esac
    pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ') || break
    [ "${pid:-0}" -gt 1 ] || break
  done
  echo 'gha-runner: no se encontró el proceso del trabajo; no se pudo detener' >&2
  exit 1
}
[ "${GITHUB_REPOSITORY:-}" = SEN-HASO/Hasito ] \
  || rechazar GITHUB_REPOSITORY "${GITHUB_REPOSITORY-<ausente>}"
[ "${GITHUB_WORKFLOW_REF:-}" = SEN-HASO/Hasito/.github/workflows/gate-noche.yml@refs/heads/main ] \
  || rechazar GITHUB_WORKFLOW_REF "${GITHUB_WORKFLOW_REF-<ausente>}"
case "${GITHUB_EVENT_NAME:-}" in
  schedule|workflow_dispatch) ;;
  *) rechazar GITHUB_EVENT_NAME "${GITHUB_EVENT_NAME-<ausente>}" ;;
esac
