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
# Los injertos usan archivos/procesos temporales propios; no comparten
# snapshots con la suite actual. Solaparlos permite completar el carril rápido.
(
  bash fleet/tren/tests/remoto-u13-injerto.sh
  bash fleet/tren/tests/remoto-u14-r1.sh
  bash fleet/tren/tests/remoto-u14-r2.sh
  bash fleet/tren/tests/remoto-u15-injerto.sh
  bash fleet/tren/tests/remoto-u15-r1.sh
  bash fleet/tren/tests/remoto-u15-r2.sh
  bash fleet/tren/tests/remoto-u16.sh
  bash fleet/tren/tests/remoto-u16-injerto.sh
  bash fleet/tren/tests/remoto-u16-r1.sh
  bash fleet/tren/tests/remoto-u17.sh
  bash fleet/tren/tests/remoto-u17-injerto.sh
  bash fleet/tren/tests/remoto-u18.sh
  bash fleet/tren/tests/remoto-u18-injerto.sh
) > "$aislado/antes.log" 2>&1 &
tren_antes_pid=$!
encontrado=0
resultado=0
for interprete in python3.9 python3.10 python3; do
  if command -v "$interprete" >/dev/null 2>&1; then
    encontrado=1
    "$interprete" --version
    "$interprete" -I -m unittest discover -s fleet/tren/tests -v || resultado=1
  fi
done
if ! wait "$tren_antes_pid"; then
  resultado=1
fi
cat "$aislado/antes.log"
if [ "$encontrado" -eq 0 ]; then
  echo 'No hay intérprete Python disponible' >&2
  exit 2
fi
if [ "$resultado" -eq 0 ]; then
  echo 'U13 injerto: cuatro regresiones fallan sobre a8a82602 y pasan después; toda la suite del tren pasa.'
  echo 'U14 R1: seis regresiones fallan sobre 49fd41dc y pasan después; toda la suite del tren pasa.'
  echo 'U14 R2: dos regresiones fallan sobre 0a1aabd8 y pasan después; toda la suite del tren pasa.'
  echo 'U15 injerto: tres regresiones fallan sobre 185e7a46 y pasan después; sombra, tabla exhaustiva y muerte en reintento de promoción con ACK perdido pasan; toda la suite del tren pasa.'
  echo 'U15 R1: dos regresiones fallan sobre e74bfdb1 y pasan después; pausa con ACK guardado o perdido aterriza sin caducidades ni push adicional; toda la suite del tren pasa.'
  echo 'U15 R2: tres regresiones fallan sobre a7ce4de3 y pasan después; EnBase consumido permite caducidad en pausa sin perderla al abortar o expulsar; reanudar conserva el aviso; toda la suite del tren pasa.'
  echo 'U16 injerto: siete regresiones fallan sobre 50cb5cda y pasan después; sombra y tick --seco idénticos a 85138706; toda la suite del tren pasa.'
  echo 'U16: tres regresiones fallan sobre 85138706 y pasan después; CI rojo, partición FIFO, timeout, juicio, recuperación y sombra pasan.'
  echo 'U16 R1: cuatro regresiones fallan sobre 48748009 y pasan después; solicitud de CI vigente, recuperación y fallo de preparación del runner pasan.'
  echo 'U17: tres regresiones fallan sobre 0f7fff27 y pasan después; dos lotes sucesivos, main ajeno tras reanudar y falla al traer objetos pasan sin tocar el checkout de admisión; sombra idéntica.'
  echo 'U17 injerto: dos regresiones fallan sobre d0019396 y pasan después; el fetch de una observación comparte un presupuesto y no se repite tras caer el remoto o la base; identidad de objetos, checkout sucio intacto y composición productiva pasan.'
  echo 'U18 injerto: seis regresiones fallan sobre c06a9c70 y pasan después; aborto y feedback con limpieza fallida, cancelación acotada, espera durable, CLI con ventana y última lectura local pasan; U18 original y sombra idéntica a a1d24ec1 pasan.'
fi
exit "$resultado"
