#!/usr/bin/env bash
set -eu
aislado=$(mktemp -d "${TMPDIR:-/tmp}/pao-tren-remoto.XXXXXX")
limpiar() {
  wait || :
  for log in "$aislado/antes.log" "$aislado/u18-r3.log"; do
    if [ -f "$log" ]; then
      cat "$log" || :
    fi
  done
  rm -rf -- "$aislado"
}
trap limpiar EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
export TMPDIR="$aislado/tmp"
export HOME="$aislado/home"
export XDG_STATE_HOME="$aislado/state"
export XDG_CONFIG_HOME="$aislado/config"
export XDG_DATA_HOME="$aislado/data"
export XDG_CACHE_HOME="$aislado/cache"
mkdir -p "$HOME" "$TMPDIR"
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
  bash fleet/tren/tests/remoto-u18-r1.sh
  bash fleet/tren/tests/remoto-u18-r2.sh
) > "$aislado/antes.log" 2>&1 &
tren_antes_pid=$!
# La matriz U18 R3 usa fixtures propios y no comparte estado con los injertos.
bash fleet/tren/tests/remoto-u18-r3.sh > "$aislado/u18-r3.log" 2>&1 &
tren_r3_pid=$!
encontrado=0
resultado=0
for interprete in python3.9 python3.10 python3; do
  if command -v "$interprete" >/dev/null 2>&1; then
    encontrado=1
    "$interprete" --version
    "$interprete" -I - "$interprete" <<'PY' || resultado=1
import sys
import unittest

def casos(suite):
    for caso in suite:
        if isinstance(caso, unittest.TestSuite):
            yield from casos(caso)
        else:
            yield caso

# En python3 la matriz completa corre arriba en remoto-u18-r3.sh, sobre la
# base anterior y la actual. No duplicarla en el camino crítico de diez min.
# Los demás intérpretes siguen ejecutando también ese módulo por discovery.
suite = unittest.TestSuite(caso for caso in casos(
    unittest.defaultTestLoader.discover('fleet/tren/tests'))
    if sys.argv[1] != 'python3' or not caso.id().startswith('test_u18_r3.'))
raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
PY
  fi
done
if ! wait "$tren_antes_pid"; then
  resultado=1
fi
if ! wait "$tren_r3_pid"; then
  resultado=1
fi
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
  echo 'U16 injerto: siete regresiones fallan sobre 50cb5cda y pasan después; sombra y tick --seco coinciden con 85138706 fuera de la formación de lotes; toda la suite del tren pasa.'
  echo 'U16: tres regresiones fallan sobre 85138706 y pasan después; CI rojo, partición FIFO, timeout, juicio, recuperación y sombra pasan.'
  echo 'U16 R1: cuatro regresiones fallan sobre 48748009 y pasan después; solicitud de CI vigente, recuperación y fallo de preparación del runner pasan.'
  echo 'U17: tres regresiones fallan sobre 0f7fff27 y pasan después; dos lotes sucesivos, main ajeno tras reanudar y falla al traer objetos pasan sin tocar el checkout de admisión; sombra coincide fuera de la formación de lotes.'
  echo 'U17 injerto: dos regresiones fallan sobre d0019396 y pasan después; el fetch de una observación comparte un presupuesto y no se repite tras caer el remoto o la base; identidad de objetos, checkout sucio intacto y composición productiva pasan.'
  echo 'U18 injerto: seis regresiones fallan sobre c06a9c70 y pasan después; aborto y feedback con limpieza fallida, cancelación acotada, espera durable, CLI con ventana y última lectura local pasan; U18 original pasa y sombra coincide con a1d24ec1 fuera de la formación de lotes.'
  echo 'U18 R1: tres regresiones fallan sobre 4498b3dd y pasan después; obligación durable bloquea sólo el push, reanudar no la borra y abortar reconcilia el CAS incierto sin perder el éxito.'
  echo 'U18 R2: dos regresiones fallan sobre 21c93c12 y pasan después; lectura incierta y cancelación fallida una vez se recuperan sin decisión humana; control sin gancho pasa antes y después.'
  echo 'U18 R3: regresión de dos pushes fallidos e invariante de 64 combinaciones fallan sobre c28fa524 y pasan después; toda la suite del tren pasa.'
fi
exit "$resultado"
