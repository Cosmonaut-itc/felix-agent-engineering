#!/usr/bin/env bash
set -eu
# Fixtures aisladas; nunca usar el HOME/config/estado del operador.
tren_pruebas=$(mktemp -d "${TMPDIR:-/tmp}/pao-tren-u12.XXXXXX")
trap 'rm -rf -- "$tren_pruebas"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
export TMPDIR="$tren_pruebas/tmp"
mkdir -p "$TMPDIR"
export HOME="$tren_pruebas/home"
export XDG_CONFIG_HOME="$tren_pruebas/config"
export XDG_STATE_HOME="$tren_pruebas/state"
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
mkdir -p "$HOME" "$XDG_CONFIG_HOME" "$XDG_STATE_HOME"
# Las tres regresiones de U12 deben fallar sobre el head revisado. Usar la
# fixture de U12 conserva el reloj original en el control histórico.
tren_anterior="$tren_pruebas/anterior"
mkdir -p "$tren_anterior"
git archive d810c04717eb7f66ae4396b25c500b1d455efe57 | tar -x -C "$tren_anterior"
git show 34033d6d509eb363a566c6a18163bae7f4ef9b94:fleet/tren/tests/test_u12_fix_r1.py \
  > "$tren_anterior/fleet/tren/tests/test_u12_fix_r1.py"
git version
python3 --version
python3 -I -B - "$tren_anterior" <<'PY'
from pathlib import Path
import sys
import unittest

raiz = Path(sys.argv[1])
sys.path.insert(0, str(raiz / "fleet/tren/tests"))
suite = unittest.defaultTestLoader.discover(str(raiz / "fleet/tren/tests"),
                                          pattern="test_u12_fix_r1.py")
resultado = unittest.TextTestRunner(verbosity=2).run(suite)
esperados = {
    "test_u12_fix_r1.U12FixR1Tests.test_u12_1_separadores_ambiguos_niegan_en_ambos_lectores",
    "test_u12_fix_r1.U12FixR1Tests.test_u12_2_caducidad_sale_de_fila_sin_reescribir_lotes_cerrados",
    "test_u12_fix_r1.U12FixR1Tests.test_u12_3_pasar_a_activo_equivale_a_control_sin_simulacion",
}
# Los subtests pueden aportar varios fallos al mismo método.
fallidos = {caso.id().split(" (")[0] for caso, _ in resultado.failures}
if resultado.errors or resultado.testsRun != 3 or fallidos != esperados:
    raise SystemExit("ANTES: no se reprodujeron exactamente los tres hallazgos")
for nombre in sorted(esperados):
    print("ANTES d810c047 FALLO ESPERADO:", nombre, flush=True)
PY
bash fleet/tren/tests/remoto.sh
bash tests/felix-mode-scripts-test.sh
echo 'U12 R1: tres regresiones fallan antes y pasan después; U1/U2/U4/U12 y scripts pasan.'
