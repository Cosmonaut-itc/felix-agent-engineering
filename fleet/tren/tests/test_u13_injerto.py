"""Regresiones del injerto U13; ejecutar sólo en el host de pruebas."""
from contextlib import contextmanager
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

import test_activo as activo
from dominio import Certificado, Diagnosticando, Terminal, desde_datos
from inscripcion import modo
from observador import GitHubGit


@contextmanager
def contexto():
    # Composición: no redescubrir los tests del fixture por herencia/importación.
    caso = activo.ActivoTests()
    try:
        caso.setUp()
        yield caso
    finally:
        caso.doCleanups()


class U13InjertoTests(unittest.TestCase):
    def test_u13_1_puerta_estricta_antes_y_dentro_del_candado(self):
        with contexto() as c:
            repo = activo.REPO
            args = activo.cli.parser().parse_args(["tick", "--repo", repo])
            ambiguos = [repo + " desconocido\n", repo + " activo extra\n",
                        repo + " sombra\n" + repo + " activo\n",
                        repo + "\n" + repo + " sombra\n"]
            for separador in ("\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"):
                ambiguos.extend(("other/project sombra # nota" + separador + repo + "\n",
                                 repo + " activo\nother/project" + separador + repo + " sombra\n"))
            for texto in ambiguos:
                with self.subTest(texto=repr(texto)):
                    c.inscritos.write_text(texto)
                    self.assertEqual(modo(repo), "activo")  # guard conserva su política.
                    self.assertEqual(c.contexto.tick()[0], 2)
            for texto in (repo + "\n", repo.upper() + " ACTIVO # permitido\n",
                          repo + "\r\n" + repo + " activo\r\n"):
                with self.subTest(legitima=repr(texto)):
                    c.inscritos.write_text(texto)
                    self.assertEqual(c.contexto.tick()[0], 0)
            from ledger import Ledger
            candado = Ledger.candado

            @contextmanager
            def cambiar(ledger, repo):
                with candado(ledger, repo):
                    c.inscritos.write_text(repo + " sombra\n" + repo + " activo\n")
                    yield

            c.inscritos.write_text(repo + " activo\n")
            with patch.object(Ledger, "candado", cambiar):
                self.assertEqual(activo.cli.ejecutar(args, c.contexto.observador, 200,
                    c.contexto.git, c.contexto.hasito)[0], 2)

    def test_u13_2_activo_a_sombra_no_importa_estado_operativo(self):
        with contexto() as c:
            with c.ledger() as ledger:
                custodia = [tuple(r) for r in ledger.db.execute("SELECT * FROM entries ORDER BY rowid")]
            c.contexto.tick(100)
            operativo = c.estado()
            self.assertTrue(all(e.tren for e in operativo.entradas))
            c.inscritos.write_text(activo.REPO + " sombra\n")
            args = activo.cli.parser().parse_args(["tick", "--repo", activo.REPO, "--seco"])
            sombra = GitHubGit(c.contexto.config, ejecutar=c.contexto.gh, sombra=True)
            _, salida = activo.cli.ejecutar(args, sombra, 101)
            simulado = desde_datos(salida["plan"]["estado"])
            self.assertEqual(len(simulado.trenes), 1, "sombra debe formar su propio lote")
            self.assertEqual(simulado.trenes[0].fase, Terminal("SIMULADO"))
            with c.ledger() as ledger:
                self.assertEqual([tuple(r) for r in ledger.db.execute("SELECT * FROM entries ORDER BY rowid")], custodia)
                self.assertEqual(ledger.estado(activo.REPO), operativo)
                self.assertEqual(ledger.db.execute("SELECT count(*) FROM events WHERE tipo='admision-entrega'").fetchone()[0], 0)

    def test_u13_3_recupera_recibos_tras_muerte_y_cambio_de_admision_o_ci(self):
        for tipo in ("iniciar-suite", "reanudar-suite", "repetir"):
            for cambio in ("head", "APTO", "CI-rojo", "CI-pendiente"):
                with self.subTest(tipo=tipo, cambio=cambio), contexto() as c:
                    if tipo == "reanudar-suite":
                        (c.raiz / "suite-75").write_text("75")
                    if tipo == "repetir":
                        (c.raiz / "producto-rojo").write_text("rojo")
                        config = c.contexto.config
                        config["costo_suite_sana_s"] = 10000
                        c.ruta_contexto.write_text(json.dumps(config))
                    codigo = '''
from test_activo import Contexto
import os
c = Contexto(sys.argv[3])
def frontera(nombre, efecto):
    if nombre == 'antes-registrar' and efecto.tipo == sys.argv[4]:
        os._exit(77)
for n in range(30):
    c.tick(1000 + n * 400, frontera)
sys.exit(9)
'''
                    hijo = c.hijo(codigo, tipo)
                    salida, error = hijo.communicate(timeout=90)
                    self.assertEqual(hijo.returncode, 77, salida + error)
                    with c.ledger() as ledger:
                        filas = [f for f in ledger.outbox(activo.REPO) if f["estado"] == "ejecutando"]
                        self.assertEqual(len(filas), 1)
                        clave = filas[0]["clave"]
                    anterior = c.estado()
                    candidato = anterior.trenes[0].fase.candidato
                    original = c.contexto.observador.observar

                    def observar(estado, ahora):
                        obs = original(estado, ahora)
                        if cambio == "head":
                            return replace(obs, prs=(replace(obs.prs[0], head="a" * 40),) + obs.prs[1:])
                        return obs

                    c.contexto.observador.observar = observar
                    if cambio == "APTO":
                        (c.raiz / "apto-1.json").write_text("{}")
                    if cambio.startswith("CI-"):
                        ci_original = c.contexto.observador.ci
                        c.contexto.observador.ci = lambda repo, sha: (
                            "rojo" if cambio == "CI-rojo" else "pendiente") if (
                                sha == candidato.C or tipo == "repetir") else ci_original(repo, sha)
                    trabajos = c.lineas("trabajos.log")
                    pushes = c.lineas("pushes.log")
                    # Incluso invocar otra vez el lanzador sería un error aquí.
                    c.contexto.hasito.runner = lambda *a, **k: self.fail("relanzó un recibo existente")
                    c.contexto.tick(20000)
                    with c.ledger() as ledger:
                        fila = ledger.db.execute("SELECT estado FROM outbox WHERE clave=?", (clave,)).fetchone()
                        self.assertEqual(fila[0], "completo", "el tick debe consumir el recibo")
                        self.assertIsNotNone(ledger.db.execute("SELECT payload FROM effect_results WHERE clave=?", (clave,)).fetchone())
                    final = c.estado()
                    if tipo == "repetir":
                        self.assertIsInstance(final.trenes[0].fase, Diagnosticando)
                        self.assertEqual(len(final.trenes[0].fase.evidencias), 1)
                    else:
                        self.assertIsInstance(final.trenes[0].fase, Certificado)
                        self.assertTrue(final.trenes[0].fase.ci)
                    if cambio == "head":
                        self.assertEqual(final.pausa.motivo, "head-cambiado-en-tren")
                    self.assertEqual(c.lineas("trabajos.log"), trabajos)
                    self.assertEqual(c.lineas("pushes.log"), pushes)
                    self.assertEqual(c.remoto(), c.B)

    def test_u13_4_observador_activo_omite_pr_de_trenes_terminales(self):
        with contexto() as c:
            estado = c.terminar()
            self.assertEqual(estado.trenes[0].fase, Terminal("ATERRIZADO"))
            with patch.object(c.contexto.observador, "vista", return_value=None) as vista:
                obs = c.contexto.observador.observar(estado, 5000)
                self.assertEqual(vista.call_count, 0, "consultó PR terminal")
                self.assertEqual(obs.prs, ())
            # Sólo el modo activo recibe el filtro; sombra conserva su lectura.
            sombra = GitHubGit(c.contexto.config, ejecutar=c.contexto.gh, sombra=True)
            with patch.object(sombra, "vista", return_value=None) as vista:
                sombra.observar(estado, 5000)
                self.assertEqual(vista.call_count, len(estado.entradas))


class SeguridadRecuperacionTests(unittest.TestCase):
    def test_sin_recibo_no_lanza_con_admision_retirada(self):
        for tipo in ("iniciar-suite", "reanudar-suite", "repetir"):
            with self.subTest(tipo=tipo), contexto() as c:
                if tipo == "reanudar-suite":
                    (c.raiz / "suite-75").write_text("75")
                if tipo == "repetir":
                    (c.raiz / "producto-rojo").write_text("rojo")
                    c.contexto.config["costo_suite_sana_s"] = 10000
                # Interrumpir antes del IO deja clave durable sin recibo.
                class Corte(Exception):
                    pass

                def cortar(nombre, efecto):
                    if nombre == "antes-ejecutar" and efecto.tipo == tipo:
                        raise Corte()

                with self.assertRaises(Corte):
                    for n in range(30):
                        c.contexto.tick(1000 + n * 400, cortar)
                (c.raiz / "apto-1.json").write_text("{}")
                trabajos = c.lineas("trabajos.log")
                c.contexto.hasito.runner = lambda *a, **k: self.fail("lanzó sin admisión")
                c.contexto.tick(20000)
                self.assertEqual(c.lineas("trabajos.log"), trabajos)
                self.assertEqual(c.remoto(), c.B)

    def test_conserva_muestra_de_admision_al_activar(self):
        # También protege la parte de la base que el otro candidato perdía.
        with contexto() as c:
            c.test_activar_conserva_muestras_de_admision_sin_importar_trenes_simulados()
