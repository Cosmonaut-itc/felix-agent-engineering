"""SEN-7280: un tick activo encadena el siguiente mientras el carril avance."""
import contextlib
import io
import itertools
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import test_activo as activo
import test_feedback as feedback
from dominio import EsperandoCI, ResultadoCI, Terminal

cli = activo.cli
REPO = activo.REPO


def cadena(huellas, codigos=None, seguir=lambda: True, tope=cli.ENCADENADOS):
    """Corre una cadena sobre huellas dadas; devuelve los ticks que ejecutó."""
    huellas, llamados = iter(huellas), []

    def un_tick():
        llamados.append(len(llamados))
        return (codigos[len(llamados) - 1] if codigos else 0), {"tick": len(llamados)}

    return list(cli.encadenar(un_tick, lambda: next(huellas), seguir, tope)), llamados


class EncadenarTests(unittest.TestCase):
    def test_sin_avance_corre_un_solo_tick(self):
        ticks, llamados = cadena(["a", "a"])
        self.assertEqual(ticks, [(0, {"tick": 1})])
        self.assertEqual(len(llamados), 1)

    def test_con_avance_sigue_hasta_que_la_huella_se_repite(self):
        ticks, _ = cadena(["a", "b", "c", "c"])
        self.assertEqual([r["tick"] for _, r in ticks], [1, 2, 3])

    def test_el_tope_corta_aunque_siga_avanzando(self):
        ticks, _ = cadena(itertools.count(), tope=4)
        self.assertEqual(len(ticks), 4)

    def test_un_tick_fallido_corta_aunque_haya_avance(self):
        ticks, _ = cadena(itertools.count(), codigos=[0, 2, 0])
        self.assertEqual([c for c, _ in ticks], [0, 2])

    def test_ledger_ilegible_corta(self):
        ticks, _ = cadena(["a", None, "b"])
        self.assertEqual(len(ticks), 1)

    def test_no_sigue_si_el_programa_cambio_o_falta_cuota(self):
        ticks, _ = cadena(itertools.count(), seguir=lambda: False)
        self.assertEqual(len(ticks), 1)

    def test_no_consulta_si_seguir_tras_el_ultimo_tick(self):
        consultas = []
        cadena(itertools.count(), seguir=lambda: consultas.append(1) or True, tope=3)
        self.assertEqual(len(consultas), 2)


class CuotaTests(unittest.TestCase):
    def responder(self, salida=None, error=None):
        def ejecutar(argv, **kwargs):
            self.assertEqual(argv[:3], ["gh", "api", "rate_limit"])
            if error is not None:
                raise error
            return subprocess.CompletedProcess(argv, 0, salida, "")
        return ejecutar

    def test_con_cuota_holgada_sigue(self):
        self.assertTrue(cli.cuota(self.responder(str(cli.CUOTA_MINIMA) + "\n")))

    def test_bajo_el_minimo_corta(self):
        self.assertFalse(cli.cuota(self.responder(str(cli.CUOTA_MINIMA - 1) + "\n")))

    def test_sin_respuesta_util_corta(self):
        for caso in (self.responder("no es un número\n"),
                     self.responder(error=subprocess.TimeoutExpired("gh", 10)),
                     self.responder(error=subprocess.CalledProcessError(1, "gh")),
                     self.responder(error=FileNotFoundError("gh"))):
            self.assertFalse(cli.cuota(caso))


class ProgramaTests(unittest.TestCase):
    def setUp(self):
        self.raiz = Path(tempfile.mkdtemp(prefix="pao-tren-cadena-")).resolve()
        (self.raiz / "adaptadores").mkdir()
        (self.raiz / "tests").mkdir()
        for nombre in ("pao-tren", "dominio.py", "adaptadores/jale.py", "tests/test_x.py"):
            (self.raiz / nombre).write_text("uno\n")
        parche = patch.object(cli, "__file__", str(self.raiz / "pao-tren"))
        parche.start()
        self.addCleanup(parche.stop)

    def test_cambia_al_instalar_un_modulo_o_el_cli(self):
        for nombre in ("pao-tren", "dominio.py", "adaptadores/jale.py"):
            antes = cli.programa()
            (self.raiz / nombre).write_text("otro contenido\n")
            self.assertNotEqual(cli.programa(), antes, nombre)

    def test_las_pruebas_no_cuentan(self):
        antes = cli.programa()
        (self.raiz / "tests/test_x.py").write_text("otro contenido\n")
        self.assertEqual(cli.programa(), antes)

    def test_instalacion_a_medias_no_es_igual_a_nada(self):
        with patch.object(Path, "stat", side_effect=OSError("a medias")):
            self.assertIsNone(cli.programa())


class MainTests(unittest.TestCase):
    def correr(self, argv, huellas, intentos, seguir=True, programa=lambda: "igual", cuota=None):
        huellas, intentos = iter(huellas), iter(intentos)
        salida = io.StringIO()
        with patch("sys.argv", ["pao-tren", *argv]), \
                patch.object(cli, "intento", lambda args: next(intentos)), \
                patch.object(cli, "huella", lambda repo: next(huellas)), \
                patch.object(cli, "programa", programa), \
                patch.object(cli, "cuota", cuota or (lambda: seguir)), \
                contextlib.redirect_stdout(salida):
            codigo = cli.main()
        return codigo, [json.loads(l) for l in salida.getvalue().splitlines()]

    def test_tick_activo_escribe_una_linea_por_tick_y_sale_con_el_ultimo_codigo(self):
        codigo, lineas = self.correr(["tick", "--repo", REPO], ["a", "b", "c", "c"],
                                     [(0, {"n": 1}), (0, {"n": 2}), (0, {"n": 3})])
        self.assertEqual(lineas, [{"n": 1}, {"n": 2}, {"n": 3}])
        self.assertEqual(codigo, 0)
        codigo, lineas = self.correr(["tick", "--repo", REPO], itertools.count(),
                                     [(0, {"n": 1}), (2, {"error": "x"})])
        self.assertEqual((codigo, len(lineas)), (2, 2))

    def test_sin_cuota_no_encadena(self):
        _, lineas = self.correr(["tick", "--repo", REPO], itertools.count(),
                                [(0, {"n": 1}), (0, {"n": 2})], seguir=False)
        self.assertEqual(lineas, [{"n": 1}])

    def test_una_instalacion_durante_la_consulta_de_cuota_corta(self):
        instalado = ["igual"]

        def cuota():
            instalado[0] = "nuevo"
            return True

        _, lineas = self.correr(["tick", "--repo", REPO], itertools.count(),
                                [(0, {"n": 1}), (0, {"n": 2})],
                                programa=lambda: instalado[0], cuota=cuota)
        self.assertEqual(lineas, [{"n": 1}])

    def test_tick_seco_y_los_demas_comandos_no_encadenan(self):
        for argv in (["tick", "--repo", REPO, "--seco"], ["estado", "--repo", REPO]):
            codigo, lineas = self.correr(argv, itertools.count(), [(0, {"n": 1}), (0, {"n": 2})])
            self.assertEqual((codigo, lineas), (0, [{"n": 1}]), argv)


class CadenaActivaTests(unittest.TestCase):
    """La cadena sobre el ledger real: mismo IO que los ticks de uno en uno."""
    comando = activo.ActivoTests.comando
    commit = activo.ActivoTests.commit
    push = activo.ActivoTests.push
    remoto = activo.ActivoTests.remoto
    setUp = activo.ActivoTests.setUp
    ledger = activo.ActivoTests.ledger
    estado = activo.ActivoTests.estado
    terminar = activo.ActivoTests.terminar
    lineas = activo.ActivoTests.lineas
    assert_aterrizado_una_vez = activo.ActivoTests.assert_aterrizado_una_vez

    def cadena(self, reloj):
        return list(cli.encadenar(lambda: self.contexto.tick(next(reloj)),
                                  lambda: cli.huella(REPO), lambda: True))

    def retener_ci_del_candidato(self):
        """Deja pendiente el CI de todo commit que no sea el head de un PR."""
        original, heads = self.contexto.observador.ci, {head for _, head in self.miembros}
        self.contexto.observador.ci = lambda repo, commit, rama=None: (
            original(repo, commit, rama) if commit in heads else ResultadoCI("pendiente"))
        return lambda: setattr(self.contexto.observador, "ci", original)

    def test_un_lote_pide_ci_en_una_cadena_y_espera_sin_girar(self):
        soltar = self.retener_ci_del_candidato()
        reloj = itertools.count(100)
        primera = self.cadena(reloj)
        self.assertTrue(all(codigo == 0 for codigo, _ in primera))
        self.assertGreater(len(primera), 2)
        self.assertLess(len(primera), cli.ENCADENADOS)
        self.assertIsInstance(self.estado().trenes[0].fase, EsperandoCI)
        self.assertEqual(sum(":refs/heads/tren/" in p for p in self.lineas("pushes.log")), 1)
        # Con el CI pendiente no hay nada que avanzar: un tick por disparo.
        self.assertEqual(len(self.cadena(reloj)), 1)
        soltar()
        for _ in range(3):
            self.cadena(reloj)
            if isinstance(self.estado().trenes[0].fase, Terminal):
                break
        self.assert_aterrizado_una_vez()

    def test_la_huella_no_cambia_mientras_el_tren_espera(self):
        self.retener_ci_del_candidato()
        reloj = itertools.count(100)
        self.cadena(reloj)
        antes = cli.huella(REPO)
        self.assertIsInstance(self.estado().trenes[0].fase, EsperandoCI)
        self.contexto.tick(next(reloj) + 600)
        self.assertEqual(cli.huella(REPO), antes)

    def test_un_efecto_que_falla_no_encadena_su_reintento(self):
        feedback.FeedbackTests.sembrar_devoluciones(self, pausa=True)
        (self.raiz / "fallo-post").write_text("/issues/1/comments")
        self.assertEqual(len(self.cadena(itertools.count(100))), 1)
        intentos = [p for p in self.lineas("posts.log") if "/issues/1/comments" in p]
        self.assertEqual(len(intentos), 1)

    def test_ledger_ausente_no_tiene_huella(self):
        with patch.object(cli, "Ledger", side_effect=OSError("sin ledger")):
            self.assertIsNone(cli.huella(REPO))


if __name__ == "__main__":
    unittest.main()
