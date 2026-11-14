#!/bin/bash
set -eu
modo=${1:-instalar}
case "$modo" in instalar|--seco|--retirar) ;; *) echo 'Uso: instalar.sh [--seco|--retirar]' >&2; exit 2;; esac
origen=$(cd "$(dirname "$0")" && pwd)/vigia.py
lib="$HOME/.local/lib/pao-vigia/vigia.py"
bin="$HOME/.local/bin/pao-vigia"
fecha=$(date +%Y%m%d-%H%M%S)-$$
if [ "$modo" = --seco ]; then
    printf 'Conservar anteriores como .bak-<fecha>\nCopiar %s → %s\nEscribir %s (exec /usr/bin/python3 -I)\n' "$origen" "$lib" "$bin"
    exit
fi
if [ "$modo" = --retirar ]; then
    for destino in "$lib" "$bin"; do
        if [ -e "$destino" ] || [ -L "$destino" ]; then
            mv "$destino" "$destino.retirado-$fecha"
            printf 'Retirado: %s.retirado-%s\n' "$destino" "$fecha"
        fi
    done
    exit
fi
mkdir -p "$(dirname "$lib")" "$(dirname "$bin")"
for destino in "$lib" "$bin"; do
    if [ -e "$destino" ] || [ -L "$destino" ]; then
        mv "$destino" "$destino.bak-$fecha"
    fi
done
cp "$origen" "$lib"
# Cita POSIX incluso si HOME contiene una comilla simple.
citado=$(printf '%s' "$lib" | sed "s/'/'\\\\''/g")
printf '#!/bin/sh\nexec /usr/bin/python3 -I '\''%s'\'' "$@"\n' "$citado" > "$bin"
chmod 755 "$bin"
printf 'Instalado: %s\n' "$bin"
