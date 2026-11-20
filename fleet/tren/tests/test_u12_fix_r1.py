"""Regresiones R1 de U12; sin red, sólo ejecutar en el host de pruebas."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

from test_tren import Aislado, HEAD, REPO, cli, observacion, vista
from dominio import Admitido, Terminal, datos, desde_datos, paso
from inscripcion import modo
from ledger import filas_medicion
from medicion import medir
from observador import Falso
from sombra import DURACION_SUITE_ESTIMADA


class U12FixR1Tests(Aislado):
    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.raiz / "config")})
        env.start()
        self.addCleanup(env.stop)
        self.registry = self.raiz / "config/pao-tren/inscritos"
        self.registry.parent.mkdir(parents=True)
        self.registry.write_text(REPO + " sombra\n")

    def entregar(self, head=HEAD, cuando=100):
        args = cli.parser().parse_args(["entregar", "--repo", REPO, "--pr", "1",
            "--head", head, "--apto", "/evidencia/apto.json",
            "--dirigida", "/evidencia/dirigida.json"])
        return cli.ejecutar(args, Falso(replace(observacion(), prs=(vista(head=head),))), cuando)

    def tick(self, cuando, **cambios):
        args = cli.parser().parse_args(["tick", "--repo", REPO, "--seco"])
        return cli.ejecutar(args, Falso(replace(observacion(), prs=(vista(**cambios),))), cuando)

    def test_u12_1_separadores_ambiguos_niegan_en_ambos_lectores(self):
        guard = Path(cli.__file__).parents[2] / "skills/felix-mode/scripts/merge-guard"
        for separador in ("\v", "\u2028", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2029"):
            for comentario in ("", " # comentario"):
                self.registry.write_text("other/project sombra" + comentario + separador + REPO + "\n")
                for repo in (REPO, "other/project"):
                    with self.subTest(separador=repr(separador), comentario=comentario, repo=repo):
                        self.assertEqual(modo(repo), "activo")
                        for command in ("git push https://github.com/" + repo + ".git HEAD:main",
                                        "gh pr merge 1 --repo " + repo +
                                        " --squash --match-head-commit " + HEAD):
                            result = subprocess.run(["bash", str(guard)], text=True, capture_output=True,
                                input=json.dumps({"cwd": str(self.raiz),
                                                  "tool_input": {"command": command}}), timeout=20)
                            self.assertEqual(result.returncode, 0, result.stderr)
                            self.assertTrue(result.stdout, "el guard permitió " + command)
                            finding = json.loads(result.stdout)["hookSpecificOutput"]
                            self.assertEqual(finding["permissionDecision"], "deny")
                            self.assertIn("repo inscrito en el tren", finding["permissionDecisionReason"])
        for separador in ("\n", "\r", "\r\n"):
            self.registry.write_text("other/project sombra" + separador + REPO + separador)
            self.assertEqual(modo("other/project"), "sombra")
            self.assertEqual(modo(REPO), "activo")

    def test_u12_2_caducidad_sale_de_fila_sin_reescribir_lotes_cerrados(self):
        casos = ((100, 100, {"head": "c" * 40}),
                 (0, 1, {"head": "c" * 40}),
                 (100, 101, {"abierto": False}))
        for indice, (inicio, caduca, cambios) in enumerate(casos):
            with self.subTest(inicio=inicio, caduca=caduca, cambios=cambios), patch.dict(
                    os.environ, {"XDG_STATE_HOME": str(self.raiz / ("state-%d" % indice))}):
                self.entregar(cuando=inicio)
                self.tick(caduca, **cambios)
                self.entregar("c" * 40, caduca)
                despacho = inicio + DURACION_SUITE_ESTIMADA if inicio < caduca else caduca
                fin = despacho + 1
                _, resultado = self.tick(fin, head="c" * 40)
                estado = desde_datos(resultado["plan"]["estado"])
                viejo, nuevo = estado.entradas
                esperado = [viejo.entrega.recibo] if caduca > inicio else []
                esperado += [nuevo.entrega.recibo]
                self.assertEqual([r for t in estado.trenes for r in t.recibos], esperado)
                informe = medir(*filas_medicion(REPO), REPO, -1, fin + 1)
                self.assertEqual([r for t in informe["lotes"] for r in t["recibos"]], esperado)
                self.assertTrue(all(t["tamano"] == 1 and t["prs"] == [1] for t in informe["lotes"]))
                self.assertEqual(informe["lotes"][-1]["instante"], despacho)
                self.assertEqual(informe["suites_ahorradas_estimadas"], 0)
                self.assertEqual(informe["admitidas"], 2)  # Historial inicial conservado.
                self.assertTrue(all(e["admision"]["estado"] == "admitido" for e in informe["entregas"]))
                antes = medir(*filas_medicion(REPO), REPO, -1, caduca)
                self.assertEqual(antes["admitidas"], int(inicio < caduca))
                # Los eventos anteriores al campo temporal se reconstruyen con
                # el instante de la primera observación que muestra el rechazo.
                requests, events = filas_medicion(REPO)
                legados = []
                for evento in events:
                    evento = dict(evento)
                    payload = json.loads(evento["payload"])
                    if evento["tipo"] == "sombra-doble-camino":
                        evento["tipo"] = "sombra"
                        for entrada in payload["estado"]["entradas"]:
                            entrada.pop("caducada", None)
                    evento["payload"] = json.dumps(payload)
                    legados.append(evento)
                self.assertEqual(medir(requests, legados, REPO, -1, fin + 1), informe)

    def test_u12_3_pasar_a_activo_equivale_a_control_sin_simulacion(self):
        self.entregar()
        _, resultado = self.tick(3700)
        simulado = desde_datos(resultado["plan"]["estado"])
        self.assertEqual(simulado.trenes[0].fase, Terminal("SIMULADO"))
        ledger = self.ledger()
        control = ledger.estado(REPO)
        control = replace(control, entradas=tuple(replace(e, admision=Admitido(), admitida=100)
                                                 for e in control.entradas))
        esperado = paso(control, replace(observacion(), ahora=3800))
        self.registry.write_text(REPO + "\n")
        # Reentregar no debe necesitar un nuevo head ni alterar el recibo.
        self.entregar(cuando=3750)
        _, resultado = self.tick(3800)
        self.assertEqual([e["tipo"] for e in resultado["plan"]["efectos"]], ["armar-candidato"])
        # Las nuevas señales sólo van al outbox operativo, no al evento sombra.
        efectos_historicos = tuple(e for e in esperado[1] if e.tipo not in
                                   ("avisar-en-tren", "avisar-aterrizaje"))
        self.assertEqual(resultado["plan"], {"estado": datos(esperado[0]),
                                            "efectos": datos(efectos_historicos)})
        self.assertEqual(ledger.db.execute(
            "SELECT tipo FROM events WHERE repo=? AND tipo LIKE 'sombra%' ORDER BY seq", (REPO,)
        ).fetchall()[0][0], "sombra-doble-camino")
