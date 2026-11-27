from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_activo as activo
from cache_github import CacheGitHub
from dominio import EsperandoCI, PLAZO_CI, ResultadoCI
from observador import GitHubGit, LimiteGitHub


EP = "repos/example/project/pulls?state=open&per_page=1"
EP2 = "repositories/123/pulls?state=open&per_page=1&page=2"
LINK = f'<https://api.github.com/{EP2}>; rel="next"'
AHORA = 1_800_000_000
REINICIO = '2027-01-15T08:00:00+00:00'


def respuesta(codigo, cuerpo="", stderr="", **cabeceras):
    salida = f"HTTP/2.0 {codigo}\r\n" + "".join(
        f"{k.replace('_', '-')}: {v}\r\n" for k, v in cabeceras.items()) + "\r\n" + cuerpo

    def ejecutar(argv):
        if codigo != 200:
            raise subprocess.CalledProcessError(1, argv, output=salida, stderr=stderr)
        return subprocess.CompletedProcess(argv, 0, salida, "")

    return ejecutar


class CacheTests(unittest.TestCase):
    def setUp(self):
        temporal = tempfile.TemporaryDirectory()
        self.addCleanup(temporal.cleanup)
        self.ruta = Path(temporal.name)
        self.cache = CacheGitHub(self.ruta)
        self.addCleanup(self.cache.__exit__)
        self.respuestas = []
        self.llamadas = []
        self.lector = GitHubGit({}, ejecutar=self.ejecutar, cache=self.cache)

    def ejecutar(self, argv, **opciones):
        self.assertEqual(argv[:4], ["gh", "api", "--method", "GET"])
        self.assertEqual(argv[5], "-i")
        self.llamadas.append(argv[4:])
        return self.respuestas.pop(0)(argv)

    def test_200_guarda_y_304_usa_la_cache_reabierta(self):
        self.respuestas.extend([respuesta(200, '[1]', ETag='"uno"'), respuesta(304)])
        self.assertEqual(self.lector.api(EP), [1])
        self.assertEqual(self.cache.leer(EP), ('"uno"', '[1]', None))
        self.cache.__exit__()
        with CacheGitHub(self.ruta) as nueva:
            self.lector.cache = nueva
            nueva.db.execute("UPDATE respuestas SET usado=usado-1")
            previo = nueva.db.execute("SELECT usado FROM respuestas").fetchone()[0]
            self.assertEqual(self.lector.api(EP), [1])
            self.assertGreater(nueva.db.execute("SELECT usado FROM respuestas").fetchone()[0], previo)
        self.assertEqual(self.llamadas, [[EP, "-i"], [EP, "-i", "-H", 'If-None-Match: "uno"']])

    def test_304_sin_link_continua_y_200_sin_next_corta(self):
        self.respuestas.extend([
            respuesta(200, '[1]', ETag='"uno"', Link=LINK),
            respuesta(200, '[2]', ETag='"dos"'),
            respuesta(304), respuesta(304),
            respuesta(200, '[3]', ETag='"tres"'),
            respuesta(304),
        ])
        self.assertEqual(self.lector.api(EP, True), [[1], [2]])
        self.assertEqual(self.cache.leer(EP)[2], EP2)
        self.assertEqual(self.lector.api(EP, True), [[1], [2]])
        self.assertEqual(self.lector.api(EP, True), [[3]])
        self.assertEqual(self.lector.api(EP, True), [[3]])
        self.assertEqual(self.llamadas, [
            [EP, "-i"], [EP2, "-i"],
            [EP, "-i", "-H", 'If-None-Match: "uno"'], [EP2, "-i", "-H", 'If-None-Match: "dos"'],
            [EP, "-i", "-H", 'If-None-Match: "uno"'], [EP, "-i", "-H", 'If-None-Match: "tres"'],
        ])

    def test_poda_solo_respuestas_fuera_de_plazo(self):
        self.cache.guardar(EP, '"uno"', "[]", None)
        self.cache.guardar(EP2, '"dos"', "[]", None)
        self.cache.db.execute("UPDATE respuestas SET usado=usado-? WHERE endpoint=?",
                              (8 * 86400, EP2))
        with CacheGitHub(self.ruta) as nueva:
            self.assertIsNotNone(nueva.leer(EP))
            self.assertIsNone(nueva.leer(EP2))

    def test_archivo_corrupto_y_fallo_de_sql_no_impiden_leer(self):
        with tempfile.TemporaryDirectory() as temporal:
            (Path(temporal) / "github-cache.sqlite3").write_text("no es SQLite")
            with CacheGitHub(temporal) as corrupta:
                self.lector.cache = corrupta
                self.respuestas.append(respuesta(200, '[1]', ETag='"uno"'))
                self.assertEqual(self.lector.api(EP), [1])

        self.cache.db.execute("DROP TABLE respuestas")
        self.lector.cache = self.cache
        self.respuestas.append(respuesta(200, '[2]', ETag='"dos"'))
        self.assertEqual(self.lector.api(EP), [2])
        self.assertEqual(self.llamadas, [[EP, "-i"], [EP, "-i"]])

    def test_limites_http(self):
        casos = (
            (respuesta(403, X_Ratelimit_Remaining="0", X_Ratelimit_Reset=str(AHORA),
                       X_Ratelimit_Resource="core"), "core", AHORA),
            (respuesta(429, Retry_After="32", X_Ratelimit_Reset=str(AHORA + 300)),
             "secondary", AHORA + 32),
            (respuesta(403, Retry_After="12"), "secondary", AHORA + 12),
            (respuesta(403, '{"message":"You have exceeded a secondary rate limit."}'),
             "secondary", AHORA + 60),
            (respuesta(403, X_Ratelimit_Remaining="0", X_Ratelimit_Reset=str(AHORA),
                       X_Ratelimit_Resource="graphql"), "graphql", AHORA),
            (respuesta(403, X_Ratelimit_Remaining="0", X_Ratelimit_Reset=str(AHORA + 300),
                       Retry_After="30"), "core", AHORA + 30),
            (respuesta(403, '{"message":"You have exceeded a secondary rate limit."}',
                       X_Ratelimit_Remaining="4000", X_Ratelimit_Reset=str(AHORA + 3000)),
             "secondary", AHORA + 60),
            (respuesta(429), "secondary", AHORA + 60),
            (respuesta(403, '{"message":"You have exceeded a secondary rate limit."}',
                       X_Ratelimit_Remaining="0", X_Ratelimit_Reset=str(AHORA + 1800)),
             "secondary", AHORA + 1800),
        )
        for fake, recurso, reinicio in casos:
            with self.subTest(recurso=recurso, reinicio=reinicio), \
                    patch("observador.time.time", return_value=AHORA):
                self.respuestas.append(fake)
                with self.assertRaises(LimiteGitHub) as error:
                    self.lector.api(EP)
                self.assertEqual((error.exception.recurso, error.exception.reinicio),
                                 (recurso, reinicio))

    def test_errores_ordinarios_conservan_called_process_error(self):
        for codigo in (403, 404):
            with self.subTest(codigo=codigo):
                self.respuestas.append(respuesta(codigo, '{"message":"Resource not accessible"}',
                                                 stderr="dato privado", X_Ratelimit_Remaining="1"))
                with self.assertRaises(subprocess.CalledProcessError) as error:
                    self.lector.api(EP)
                self.assertIn(f"HTTP/2.0 {codigo}", error.exception.output)
                self.assertEqual(error.exception.stderr, "dato privado")

    def test_archivo_solo_para_el_usuario(self):
        for previos in (("",), ("", "-wal", "-shm"), ()):
            with tempfile.TemporaryDirectory() as temporal:
                for sufijo in previos:
                    ruta = Path(temporal) / f"github-cache.sqlite3{sufijo}"
                    ruta.touch()
                    os.chmod(ruta, 0o644)
                with CacheGitHub(temporal) as cache:
                    cache.guardar(EP, '"uno"', "[]", None)
                    archivos = sorted(Path(temporal).iterdir())
                    self.assertEqual(len(archivos), 3)
                    for ruta in archivos:
                        with self.subTest(previos=previos, archivo=ruta.name):
                            self.assertEqual(ruta.stat().st_mode & 0o777, 0o600)
        # Una base válida con su WAL vivo y modos ajenos, también de sólo lectura.
        with tempfile.TemporaryDirectory() as temporal, CacheGitHub(temporal) as abierta:
            abierta.guardar(EP, '"uno"', "[]", None)
            archivos = sorted(Path(temporal).iterdir())
            self.assertEqual(len(archivos), 3)
            self.assertGreater(Path(temporal, "github-cache.sqlite3-wal").stat().st_size, 0)
            for ruta in archivos:
                os.chmod(ruta, 0o444 if ruta.name.endswith("sqlite3") else 0o644)
            with CacheGitHub(temporal) as reabierta:
                self.assertEqual(reabierta.leer(EP), ('"uno"', "[]", None))
                for ruta in archivos:
                    with self.subTest(archivo=ruta.name, previo="valido"):
                        self.assertEqual(ruta.stat().st_mode & 0o777, 0o600)

    def test_sin_cache_clasifica_por_estado_http(self):
        casos = (
            ("gh: You have exceeded a secondary rate limit. (HTTP 403)", ("secondary", AHORA + 60)),
            ("gh: API rate limit exceeded (HTTP 429)", ("core", AHORA)),
            ("gh: rate limit backend unavailable (HTTP 503)", None),
            ("gh: Too Many Requests (HTTP 429)", ("core", AHORA)),
            ("gh: Resource not accessible by integration (HTTP 403)", None),
        )
        for stderr, esperado in casos:
            with self.subTest(stderr=stderr), patch("observador.time.time", return_value=AHORA):
                ejecutar = Mock(side_effect=[
                    subprocess.CalledProcessError(1, ["gh"], stderr=stderr),
                    subprocess.CompletedProcess(["gh"], 0, json.dumps(
                        {"resources": {"core": {"reset": AHORA}}}), ""),
                ])
                lector = GitHubGit({}, ejecutar=ejecutar)
                if esperado is None:
                    with self.assertRaises(subprocess.CalledProcessError):
                        lector.api(EP)
                    continue
                with self.assertRaises(LimiteGitHub) as error:
                    lector.api(EP)
                self.assertEqual((error.exception.recurso, error.exception.reinicio), esperado)
                self.assertEqual(ejecutar.call_count, 1 if esperado[0] == "secondary" else 2)

    def test_sin_cache_lee_reinicio_de_rate_limit(self):
        ejecutar = Mock(side_effect=[
            subprocess.CalledProcessError(
                1, ["gh"], stderr="gh: API RATE LIMIT exceeded for user ID 1. (HTTP 403)"),
            subprocess.CompletedProcess(["gh"], 0, json.dumps(
                {"resources": {"core": {"reset": AHORA}}}), ""),
        ])
        with self.assertRaises(LimiteGitHub) as error:
            GitHubGit({}, ejecutar=ejecutar).api(EP, True)
        self.assertEqual((error.exception.recurso, error.exception.reinicio), ("core", AHORA))
        self.assertEqual(ejecutar.call_args_list[0].args[0][5:], ["--paginate", "--slurp"])
        self.assertEqual(ejecutar.call_args_list[1].args[0][4:], ["rate_limit"])


class TickLimiteTests(unittest.TestCase):
    comando = activo.ActivoTests.comando
    commit = activo.ActivoTests.commit
    push = activo.ActivoTests.push
    remoto = activo.ActivoTests.remoto
    setUp = activo.ActivoTests.setUp
    ledger = activo.ActivoTests.ledger
    estado = activo.ActivoTests.estado
    hasta_fase = activo.ActivoTests.hasta_fase

    def operativos(self):
        with self.ledger() as ledger:
            return ledger.db.execute("SELECT count(*) FROM events WHERE tipo='operativo'").fetchone()[0]

    def configurar(self):
        ruta = self.raiz / "repos.json"
        ruta.write_text(json.dumps({"repos": {activo.REPO: self.contexto.config}}))
        return ["pao-tren", "tick", "--repo", activo.REPO, "--config", str(ruta)]

    def eventos(self):
        with self.ledger() as ledger:
            return ledger.db.execute("SELECT count(*) FROM events").fetchone()[0]

    def limite_en(self, objetivo):
        def ejecutar(argv, **opciones):
            if argv[0] == "gh" and objetivo in argv[4]:
                return respuesta(403, stderr="dato privado", X_Ratelimit_Remaining="0",
                                 X_Ratelimit_Reset=str(AHORA))(argv)
            return self.contexto.gh(argv, **opciones)
        return ejecutar

    def test_seco_sale_75_con_limite_en_main(self):
        cli = activo.cli
        args = cli.parser().parse_args(self.configurar()[1:] + ["--seco"])
        crear = lambda config, **kwargs: GitHubGit(
            config, ejecutar=self.limite_en("/git/ref/heads/main"), **kwargs)
        antes = self.eventos()
        with patch.object(cli, "GitHubGit", side_effect=crear):
            self.assertEqual(list(cli.ticks(args)), [(75, {
                "error": "limite-github", "recurso": "core", "reinicio": REINICIO})])
        self.assertEqual(self.eventos(), antes)

    def test_tick_y_cadena_preservan_estado_y_eventos_ante_un_limite(self):
        antes = self.hasta_fase(EsperandoCI)
        fase = antes.trenes[0].fase
        self.assertIsNotNone(fase.desde)
        self.assertTrue(any(e.admitida is not None for e in antes.entradas))
        objetivos = ("/git/ref/heads/main", "/pulls/1",
                     f"/commits/{fase.candidato.C}/check-runs?")
        cli = activo.cli
        args = cli.parser().parse_args(self.configurar()[1:])
        ejecutar = cli.ejecutar
        for objetivo in objetivos:
            with self.subTest(objetivo=objetivo):
                eventos = self.operativos()
                crear = lambda config, **kwargs: GitHubGit(config,
                    ejecutar=self.limite_en(objetivo), **kwargs)
                def tick(args, **kwargs):
                    return ejecutar(args, ahora=fase.desde + PLAZO_CI + 1, git=self.contexto.git,
                        adaptador=self.contexto.hasito, escritor=self.contexto.escritor, **kwargs)
                with patch.object(cli, "ejecutar", side_effect=tick) as un_tick, \
                        patch.object(cli, "GitHubGit", side_effect=crear), \
                        patch.object(cli, "cuota") as cuota:
                    self.assertEqual(list(cli.ticks(args)), [(75, {
                        "error": "limite-github", "recurso": "core", "reinicio": REINICIO})])
                    un_tick.assert_called_once()
                    cuota.assert_not_called()
                self.assertEqual(self.operativos(), eventos)
                self.assertEqual(self.estado(), antes)

    def test_espera_del_ci_emite_limite_y_sale_sin_otro_tick(self):
        antes = self.hasta_fase(EsperandoCI)
        fase = antes.trenes[0].fase
        objetivo = f"/commits/{fase.candidato.C}/check-runs?"
        ci = self.contexto.observador.ci
        self.contexto.observador.ci = lambda repo, head, rama=None, **kwargs: (
            ResultadoCI("pendiente") if head == fase.candidato.C else ci(repo, head, rama, **kwargs))
        self.contexto.tick(fase.desde + 1)
        antes = self.estado()
        fase = antes.trenes[0].fase
        cli = activo.cli
        run = subprocess.run
        reloj = [0]
        salida = io.StringIO()
        pasos = []

        def ejecutar(argv, **opciones):
            if argv[0] == "gh":
                return self.limite_en(objetivo)(argv, **opciones)
            return run(argv, **opciones)

        def tick(args, instalando=False):
            resultado = self.contexto.tick(fase.desde + 2)
            pasos.append(self.operativos())
            return resultado

        def dormir(segundos):
            reloj[0] += segundos

        with patch.object(sys, "argv", self.configurar()), \
                patch.object(cli, "intento", side_effect=tick), \
                patch.object(cli, "huella", return_value=("igual",)), \
                patch.object(cli, "cuota", return_value=True), \
                patch.object(cli, "ARRANQUE", 0), \
                patch.object(cli, "time", SimpleNamespace(monotonic=lambda: reloj[0], sleep=dormir)), \
                patch.object(subprocess, "run", ejecutar), redirect_stdout(salida):
            self.assertEqual(cli.main(), 75)
        self.assertEqual(reloj[0], 20)
        self.assertEqual(pasos, [self.operativos()])
        self.assertEqual(self.estado().entradas, antes.entradas)
        self.assertEqual(self.estado().trenes[0].fase, fase)
        self.assertEqual(json.loads(salida.getvalue().splitlines()[-1]),
                         {"error": "limite-github", "recurso": "core", "reinicio": REINICIO})
