#!/usr/bin/env bash
# Sólo el gate nocturno de Hasito desde main puede ejecutar trabajos aquí.
set -euo pipefail
rechazar() { printf 'gha-runner: %s no aceptado: %s\n' "$1" "$2" >&2; exit 1; }
[ "${GITHUB_REPOSITORY:-}" = SEN-HASO/Hasito ] \
  || rechazar GITHUB_REPOSITORY "${GITHUB_REPOSITORY-<ausente>}"
[ "${GITHUB_WORKFLOW_REF:-}" = SEN-HASO/Hasito/.github/workflows/gate-noche.yml@refs/heads/main ] \
  || rechazar GITHUB_WORKFLOW_REF "${GITHUB_WORKFLOW_REF-<ausente>}"
case "${GITHUB_EVENT_NAME:-}" in
  schedule|workflow_dispatch) ;;
  *) rechazar GITHUB_EVENT_NAME "${GITHUB_EVENT_NAME-<ausente>}" ;;
esac
