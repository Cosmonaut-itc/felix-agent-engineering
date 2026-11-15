#!/usr/bin/env bash
set -eu
git version
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
exit "$resultado"
