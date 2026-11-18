#!/usr/bin/env bash
set -euo pipefail

export PATH="/opt/homebrew/bin:$PATH"
gh=${GATE_NOCHE_GH:-gh}
espera=${GATE_NOCHE_ESPERA:-120}

case "${1:-}" in
  --en-seco)
    [ "$#" -eq 1 ] || { echo 'uso: disparar.sh [--en-seco]' >&2; exit 2; }
    if ! command -v "$gh" >/dev/null 2>&1; then
      echo "Falta gh ($gh) en PATH o no es ejecutable" >&2
      exit 1
    fi
    set +e
    estado=$("$gh" api repos/SEN-HASO/Hasito/actions/workflows/gate-noche.yml --jq .state 2>&1)
    codigo=$?
    set -e
    if [ "$codigo" -eq 0 ] && [ "$estado" = active ]; then
      echo 'OK: sesión de gh ve activo gate-noche.yml'
      exit 0
    fi
    echo "Falta acceso activo al workflow mediante gh (resultado: ${estado:-sin respuesta})" >&2
    exit 1
    ;;
  '') ;;
  *) echo 'uso: disparar.sh [--en-seco]' >&2; exit 2 ;;
esac

if ! command -v "$gh" >/dev/null 2>&1; then
  echo "Falta gh ($gh) en PATH o no es ejecutable" >&2
  exit 1
fi

for intento in 1 2 3; do
  set +e
  "$gh" workflow run gate-noche.yml --repo SEN-HASO/Hasito --ref main 2>&1
  codigo=$?
  set -e
  fecha=$(date '+%Y-%m-%dT%H:%M:%S%z')
  if [ "$codigo" -eq 0 ]; then
    printf '%s intento=%s resultado=OK\n' "$fecha" "$intento"
    exit 0
  fi
  printf '%s intento=%s resultado=FALLO codigo=%s\n' "$fecha" "$intento" "$codigo"
  if [ "$intento" -lt 3 ]; then sleep "$espera"; fi
done
exit 1
