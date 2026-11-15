#!/usr/bin/env bash
set -eu
# Sólo lo invoca remoto.sh en el host de pruebas. Conserva todas las fixtures.
tren_pruebas=$(mktemp -d "${TMPDIR:-/tmp}/pao-tren-u13-antes.XXXXXX")
tren_anterior="$tren_pruebas/anterior"
mkdir -p "$tren_anterior"
git archive a8a826027ddcdafb85cc7c238b6047e65996315c | tar -x -C "$tren_anterior"
cp fleet/tren/tests/test_u13_injerto.py "$tren_anterior/fleet/tren/tests/"
python3 -I -B - "$tren_anterior" <<'PY'
from pathlib import Path
import sys
import unittest

raiz = Path(sys.argv[1])
sys.path.insert(0, str(raiz / "fleet/tren/tests"))
suite = unittest.defaultTestLoader.loadTestsFromName("test_u13_injerto.U13InjertoTests")
resultado = unittest.TextTestRunner(verbosity=2).run(suite)
esperados = {
    "test_u13_injerto.U13InjertoTests.test_u13_1_puerta_estricta_antes_y_dentro_del_candado",
    "test_u13_injerto.U13InjertoTests.test_u13_2_activo_a_sombra_no_importa_estado_operativo",
    "test_u13_injerto.U13InjertoTests.test_u13_3_recupera_recibos_tras_muerte_y_cambio_de_admision_o_ci",
    "test_u13_injerto.U13InjertoTests.test_u13_4_observador_activo_omite_pr_de_trenes_terminales",
}
fallidos = {caso.id().split(" (")[0] for caso, _ in resultado.failures}
if resultado.errors or resultado.testsRun != 4 or fallidos != esperados:
    raise SystemExit("ANTES: no se reprodujeron exactamente los cuatro hallazgos")
for nombre in sorted(esperados):
    print("ANTES a8a82602 FALLO ESPERADO:", nombre, flush=True)
PY
