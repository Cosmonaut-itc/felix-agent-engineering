"""SEN-7280: lecturas concurrentes y cierre ordenado de la observación activa."""
from collections import Counter
from dataclasses import replace
import json
import subprocess
from threading import Barrier, BrokenBarrierError, Lock, get_ident
import unittest
from unittest.mock import patch

import test_activo as fixtures
from dominio import VistaPR
from git_ops import GitTren
from observador import GitHubGit, autorizaciones


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

    def test_relectura_despues_de_las_demas_lecturas_detecta_head_nuevo(self):
        entrega = self.entregas[0]
        pull = self.pulls[0]
        prefijo_ci = "repos/{}/commits/{}/".format(entrega.repo, entrega.head)
        head_nuevo = "f" * 40
        for paralelas in (1, 4):
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
