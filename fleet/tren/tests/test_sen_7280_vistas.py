"""SEN-7280: lecturas concurrentes y cierre ordenado de la observación activa."""
from collections import Counter
from dataclasses import replace
import json
import subprocess
from threading import Barrier, BrokenBarrierError, Lock, current_thread, get_ident
import unittest
from unittest.mock import patch

import test_activo as fixtures
from cache_github import CacheGitHub
from dominio import VistaPR
from git_ops import GitTren
from observador import GitHubGit, LimiteGitHub, autorizaciones
from test_cache_github import AHORA, respuesta


class VistasTests(unittest.TestCase):
    comando = fixtures.ActivoTests.comando
    commit = fixtures.ActivoTests.commit
    push = fixtures.ActivoTests.push
    ledger = fixtures.ActivoTests.ledger

    def setUp(self):
        fixtures.ActivoTests.setUp(self)
        estado = fixtures.ActivoTests.estado(self)
        self.estado = replace(estado, entradas=tuple(reversed(estado.entradas)))
        self.entregas = [entrada.entrega for entrada in self.estado.entradas]
        self.pulls = ["repos/{}/pulls/{}".format(e.repo, e.pr) for e in self.entregas]

    def observar(self, paralelas, ejecutar=None, almacen=None):
        observador = GitHubGit(self.contexto.config, ejecutar=ejecutar or self.contexto.gh,
                              activo=True, almacen=almacen, paralelas=paralelas)
        return observador.observar(self.estado, 100)

    def test_misma_observacion(self):
        resultados = []
        for paralelas in (1, 4):
            almacen = GitTren(str(self.origen), ruta=self.raiz /
                              "observacion-{}.git".format(paralelas))
            with patch("observador.autorizaciones", wraps=autorizaciones) as consultar:
                resultados.append(self.observar(paralelas, almacen=almacen))
                consultar.assert_called_once_with(self.contexto.config)
        serie, paralelo = resultados
        self.assertTrue(all(v.completa for v in serie.prs))
        self.assertEqual([v.pr for v in serie.prs], [e.pr for e in self.entregas])
        self.assertEqual(paralelo, serie)

    def test_mismos_endpoints(self):
        def endpoints(paralelas):
            consultas = []
            candado = Lock()

            def ejecutar(argv, **kwargs):
                if argv[0] == "gh":
                    with candado:
                        consultas.append(argv[4])
                return self.contexto.gh(argv, **kwargs)

            self.observar(paralelas, ejecutar)
            return Counter(consultas)

        self.assertEqual(endpoints(4), endpoints(1))

    def test_misma_observacion_y_endpoints_con_ocho_hilos(self):
        def observar(paralelas):
            consultas = Counter()
            candado = Lock()

            def ejecutar(argv, **kwargs):
                if argv[0] == "gh":
                    with candado:
                        consultas[argv[4]] += 1
                return self.contexto.gh(argv, **kwargs)

            almacen = GitTren(str(self.origen), ruta=self.raiz /
                              "comparacion-{}.git".format(paralelas))
            return self.observar(paralelas, ejecutar, almacen), consultas

        serie = observar(1)
        self.assertTrue(all(v.completa for v in serie[0].prs))
        for paralelas in (4, 8):
            with self.subTest(paralelas=paralelas):
                self.assertEqual(observar(paralelas), serie)

    def test_pr_distintos_alcanzan_la_barrera(self):
        for paralelas, esperado in ((4, self.pulls[:2]), (1, [])):
            with self.subTest(paralelas=paralelas):
                barrera = Barrier(2, timeout=2)
                candado = Lock()
                vistos, coincidieron = set(), []

                def ejecutar(argv, **kwargs):
                    if argv[0] == "gh" and argv[4] in self.pulls[:2]:
                        endpoint = argv[4]
                        with candado:
                            primera = endpoint not in vistos
                            vistos.add(endpoint)
                        if primera:
                            try:
                                barrera.wait()
                            except BrokenBarrierError:
                                pass
                            else:
                                with candado:
                                    coincidieron.append(endpoint)
                    return self.contexto.gh(argv, **kwargs)

                observacion = self.observar(paralelas, ejecutar)
                self.assertTrue(all(v.completa for v in observacion.prs))
                self.assertCountEqual(coincidieron, esperado)

    def test_cierres_en_serie_en_el_orden_del_estado(self):
        cierres = []
        hilo_tick = get_ident()

        def ejecutar(argv, **kwargs):
            if argv[0] != "git":
                return self.contexto.gh(argv, **kwargs)
            # Ambos comandos de cierre deben ejecutarse en el hilo del tick.
            self.assertEqual(get_ident(), hilo_tick)
            cierres.append((argv[3], argv[-1]))
            return self.contexto.gh(argv, **kwargs)

        observacion = self.observar(4, ejecutar)
        self.assertTrue(all(v.completa for v in observacion.prs))
        self.assertEqual(cierres, [(cmd, e.head) for e in self.entregas
                                   for cmd in ("merge-base", "merge-tree")])

    def test_relecturas_se_solapan_despues_de_todos_los_cierres(self):
        heads = [e.head for e in self.entregas]
        for paralelas in (1, 4, 8):
            with self.subTest(paralelas=paralelas):
                barrera = Barrier(2, timeout=2)
                candado = Lock()
                consultas, fusiones, coincidieron = Counter(), [], []
                traza = []

                def ejecutar(argv, **kwargs):
                    if argv[0] == "git":
                        resultado = self.contexto.gh(argv, **kwargs)
                        with candado:
                            traza.append((argv[3], argv[-1]))
                            if argv[3] == "merge-tree":
                                fusiones.append(argv[-1])
                        return resultado
                    endpoint = argv[4]
                    with candado:
                        traza.append(endpoint)
                        consultas[endpoint] += 1
                        relectura = endpoint in self.pulls and consultas[endpoint] == 2
                        if relectura and paralelas > 1:
                            self.assertEqual(fusiones, heads)
                            self.assertNotEqual(get_ident(), hilo_tick)
                    if relectura and endpoint in self.pulls[:2]:
                        try:
                            barrera.wait()
                        except BrokenBarrierError:
                            pass
                        else:
                            with candado:
                                coincidieron.append(endpoint)
                    return self.contexto.gh(argv, **kwargs)

                hilo_tick = get_ident()
                observacion = self.observar(paralelas, ejecutar)
                self.assertTrue(all(v.completa for v in observacion.prs))
                self.assertEqual([consultas[pull] for pull in self.pulls], [2] * len(self.pulls))
                if paralelas > 1:
                    self.assertCountEqual(coincidieron, self.pulls[:2])
                else:
                    self.assertEqual(coincidieron, [])
                    esperada = ["repos/{}/git/ref/heads/main".format(self.estado.repo)]
                    for entrega, pull in zip(self.entregas, self.pulls):
                        prefijo = "repos/{}/commits/{}/".format(entrega.repo, entrega.head)
                        esperada.extend([pull, pull + "/files?per_page=100",
                                         ("merge-base", entrega.head), ("merge-tree", entrega.head),
                                         prefijo + "check-runs?per_page=100",
                                         prefijo + "statuses?per_page=100", pull])
                    self.assertEqual(traza, esperada)

    def test_relectura_despues_de_las_demas_lecturas_detecta_head_nuevo(self):
        entrega = self.entregas[0]
        pull = self.pulls[0]
        prefijo_ci = "repos/{}/commits/{}/".format(entrega.repo, entrega.head)
        head_nuevo = "f" * 40
        for paralelas in (1, 4, 8):
            with self.subTest(paralelas=paralelas):
                consultas = []
                fusiones = []

                def ejecutar(argv, **kwargs):
                    resultado = self.contexto.gh(argv, **kwargs)
                    if argv[0] != "gh":
                        if argv[3] == "merge-tree":
                            fusiones.append(argv[-1])
                        return resultado
                    endpoint = argv[4]
                    if endpoint == pull and consultas:
                        self.assertEqual(consultas, [
                            pull, pull + "/files?per_page=100",
                            prefijo_ci + "check-runs?per_page=100",
                            prefijo_ci + "statuses?per_page=100"])
                        self.assertIn(entrega.head, fusiones)
                        payload = json.loads(resultado.stdout)
                        payload["head"]["sha"] = head_nuevo
                        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
                    if endpoint.startswith((pull + "/", prefijo_ci)) or endpoint == pull:
                        consultas.append(endpoint)
                    return resultado

                vista = self.observar(paralelas, ejecutar).prs[0]
                self.assertFalse(vista.completa)
                self.assertEqual(vista.head, head_nuevo)

    def test_fallo_de_relectura_solo_deja_incompleto_su_pr(self):
        consultas = Counter()
        candado = Lock()
        pull = self.pulls[0]

        def ejecutar(argv, **kwargs):
            if argv[0] == "gh" and argv[4] == pull:
                with candado:
                    consultas[pull] += 1
                    if consultas[pull] == 2:
                        raise subprocess.CalledProcessError(1, argv)
            return self.contexto.gh(argv, **kwargs)

        completas = self.observar(1).prs
        self.assertTrue(all(v.completa for v in completas))
        self.assertEqual(self.observar(8, ejecutar).prs,
                         (replace(completas[0], completa=False), *completas[1:]))

    def test_cierre_incompleto_no_relee_el_pr(self):
        consultas = Counter()
        candado = Lock()

        def ejecutar(argv, **kwargs):
            if argv[0] == "gh" and argv[4] in self.pulls:
                with candado:
                    consultas[argv[4]] += 1
            elif argv[0] == "git" and argv[3] == "merge-tree" and argv[-1] == self.entregas[0].head:
                raise subprocess.CalledProcessError(1, argv)
            return self.contexto.gh(argv, **kwargs)

        vistas = self.observar(8, ejecutar).prs
        self.assertEqual([v.completa for v in vistas], [False, True, True])
        self.assertEqual([consultas[pull] for pull in self.pulls], [1, 2, 2])

    def test_limite_en_relectura_sale_de_observar_y_cierra_el_pool(self):
        for codigo, mensaje in ((429, ""), (403, '{"message":"secondary rate limit"}')):
            for paralelas in (1, 8):
                with self.subTest(codigo=codigo, paralelas=paralelas):
                    limite = respuesta(codigo, mensaje, Retry_After="12")
                    barrera = Barrier(2, timeout=2)
                    candado = Lock()
                    consultas, hilos, hilos_relectura = Counter(), set(), {}
                    hilo_tick = current_thread()

                    def ejecutar(argv, **kwargs):
                        if argv[0] != "gh":
                            return self.contexto.gh(argv, **kwargs)
                        endpoint = argv[4]
                        with candado:
                            hilos.add(current_thread())
                            consultas[endpoint] += 1
                            relectura = endpoint in self.pulls and consultas[endpoint] == 2
                            if relectura:
                                hilos_relectura[endpoint] = current_thread()
                        if relectura and endpoint in self.pulls[:2]:
                            if paralelas > 1:
                                barrera.wait()
                            if endpoint == self.pulls[0]:
                                return limite(argv)
                        return self.contexto.gh(argv, **kwargs)

                    with CacheGitHub(self.raiz) as cache, patch("observador.time.time", return_value=AHORA):
                        observador = GitHubGit(self.contexto.config, ejecutar=ejecutar,
                                              activo=True, paralelas=paralelas, cache=cache)
                        with self.assertRaises(LimiteGitHub) as error:
                            observador.observar(self.estado, 100)
                        self.assertEqual((error.exception.recurso, error.exception.reinicio),
                                         ("secondary", AHORA + 12))
                        if paralelas == 1:
                            self.assertEqual(hilos_relectura, {self.pulls[0]: hilo_tick})
                        else:
                            self.assertIsNot(hilos_relectura[self.pulls[0]],
                                             hilos_relectura[self.pulls[1]])
                            self.assertTrue(all(not hilo.is_alive() for hilo in hilos - {hilo_tick}))

    def test_fallo_del_pr_no_afecta_otras_entradas(self):
        pull = self.pulls[0]

        def ejecutar(argv, **kwargs):
            if argv[0] == "gh" and argv[4] == pull:
                raise subprocess.CalledProcessError(1, argv)
            return self.contexto.gh(argv, **kwargs)

        vistas = self.observar(4, ejecutar).prs
        entrega = self.entregas[0]
        self.assertEqual(vistas[0], VistaPR(entrega.pr, entrega.head, completa=False,
                                           recibo=entrega.recibo))
        self.assertEqual([v.completa for v in vistas], [False, True, True])

    def test_primer_fetch_fallido_agota_transporte_del_tick(self):
        for paralelas in (1, 4):
            with self.subTest(paralelas=paralelas):
                almacen = GitTren(str(self.origen), ruta=self.raiz /
                                  "transporte-{}.git".format(paralelas))
                almacen.preparar()
                self.assertEqual(almacen.traer(self.B), self.B)
                intentos = []
                original = almacen.ejecutar

                def transporte(argv, **kwargs):
                    if "fetch" in argv:
                        intentos.append(argv[-1])
                        return subprocess.CompletedProcess(argv, 128, "", "remoto caído")
                    return original(argv, **kwargs)

                almacen.ejecutar = transporte
                vistas = self.observar(paralelas, almacen=almacen).prs
                self.assertEqual(intentos, ["refs/pull/{}/head".format(self.entregas[0].pr)])
                self.assertEqual([v.completa for v in vistas], [False, False, False])
