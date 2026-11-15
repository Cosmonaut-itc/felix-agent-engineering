#!/usr/bin/env bash
set -eu
aislado=$(mktemp -d "${TMPDIR:-/tmp}/pao-tren-remoto.XXXXXX")
export HOME="$aislado/home"
export XDG_STATE_HOME="$aislado/state"
export XDG_CONFIG_HOME="$aislado/config"
export XDG_DATA_HOME="$aislado/data"
export XDG_CACHE_HOME="$aislado/cache"
mkdir -p "$HOME"
bin/pao check
git version
bash fleet/tren/tests/remoto-u13-injerto.sh
bash fleet/tren/tests/remoto-u14-r1.sh
bash fleet/tren/tests/remoto-u14-r2.sh
encontrado=0
resultado=0
for interprete in python3.9 python3.10 python3; do
  if command -v "$interprete" >/dev/null 2>&1; then
    encontrado=1
    "$interprete" --version
    "$interprete" -I -m unittest discover -s fleet/tren/tests -v || resultado=1
  fi
done
if [ "$encontrado" -eq 0 ]; then
  echo 'No hay intérprete Python disponible' >&2
  exit 2
fi
if [ "$resultado" -eq 0 ]; then
  echo 'U13 injerto: cuatro regresiones fallan sobre a8a82602 y pasan después; toda la suite del tren pasa.'
  echo 'U14 R1: seis regresiones fallan sobre 49fd41dc y pasan después; toda la suite del tren pasa.'
  echo 'U14 R2: dos regresiones fallan sobre 0a1aabd8 y pasan después; toda la suite del tren pasa.'
  echo 'U15: juicio y pausa por CLI y tick, incluida test_juicio.py; toda la suite del tren pasa.'
fi
exit "$resultado"
