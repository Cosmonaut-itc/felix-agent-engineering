"""Ejecuta las regresiones del panel contra cambios aislados."""
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory


FLEET = Path(__file__).resolve().parents[2]
CASOS = (
    ("R1-sol-1", "fin > ahora + 300", "fin > tomado + 300",
     "test_fin_nuevo_renueva_taken_con_cursor_y_sin_cursor"),
    ("R1-sol-2", 'return dict(check="devolucion", unidad="Devolución", comentario_url=url)',
     "return None", "test_expulsion_conserva_url_original_con_otra_grafia"),
    ("R1-sol-3", 'terminal = "CADUCADO"', 'terminal = "REARMAR"',
     "test_lote_devuelto_distingue_caducado_de_rearme"),
    ("R1-sol-4", "grafias.get(r.lower(), [r])", "[r]",
     "test_todos_y_repo_publican_la_grafia_del_ledger"),
    ("R1-sol-5", 'fecha(entrada["recibida"], ahora, ahora)',
     'fecha(entrada.get("admitida") if etapa == "admitido" else entrada["recibida"], ahora, ahora)',
     "test_espera_publica_recibida_en_todas_las_etapas"),
    ("N2", "if intento or not bloqueo or restante <= 0:", "if True:",
     "test_ledger_reintenta_solo_los_bloqueos"),
    ("R3-sol-2", 'max(ticks, key=lambda tick: tick["seq"], default=None)',
     "next(ticks, None)", "test_grafia_del_ultimo_tick_sombra_sigue_la_secuencia"),
    ("R3-sol-1-sin-tope", "lectura_medicion(raiz, timeout=restante if intento else min(restante, 0.1))",
     "lectura_medicion(raiz)", "test_presupuesto_de_bloqueo_descuenta_el_tiempo_del_primer_intento_y_la_pausa"),
    ("R4-sol-2", 'custodia[recibo]["admision"] = entrada["admision"]', "pass",
     "test_espera_proyecta_admision_durable_antes_del_tick"),
    ("R4-sol-1", "(?ai:{re.escape(repo)})", "{re.escape(repo)}",
     "test_expulsion_conserva_url_original_con_otra_grafia"),
    ("R4-opus-3", ' and contenido["ledger"] == identidad', "",
     "test_ledger_sustituido_y_legible_reconstruye_hitos_con_la_misma_secuencia"),
)


def ejecutar(tests, caso):
    return subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "-v", f"test_panel.PanelTests.{caso}"],
        cwd=tests, capture_output=True, text=True, timeout=20)


def main():
    for nombre, original, mutado, caso in CASOS:
        with TemporaryDirectory(prefix="mutacion-panel-") as carpeta:
            fleet = Path(carpeta)
            tren = fleet / "tren"
            shutil.copytree(FLEET / "tren", tren, ignore=shutil.ignore_patterns("__pycache__"))
            (fleet / "rack").mkdir()
            shutil.copy2(FLEET / "rack/tren.py", fleet / "rack/tren.py")
            ruta = tren / "panel.py"
            fuente = ruta.read_text(encoding="utf-8")
            if fuente.count(original) != 1:
                raise AssertionError(f"{nombre}: se esperaba un único sitio para mutar")
            control = ejecutar(tren / "tests", caso)
            if control.returncode != 0:
                raise AssertionError(f"{nombre}: falló el control\n{control.stderr}")
            ruta.write_text(fuente.replace(original, mutado), encoding="utf-8")
            resultado = ejecutar(tren / "tests", caso)
            stderr = resultado.stderr
            if (resultado.returncode != 1 or f"FAIL: {caso} (" not in stderr
                    or "AssertionError:" not in stderr or "ERROR:" in stderr):
                raise AssertionError(f"{nombre}: el mutante no falló por aserción\n{stderr}")
            asercion = next(linea for linea in stderr.splitlines() if linea.startswith("AssertionError:"))
            print(f"{nombre}: {asercion}", flush=True)
    print(f"{len(CASOS)}/{len(CASOS)} mutantes detectados")


if __name__ == "__main__":
    main()
