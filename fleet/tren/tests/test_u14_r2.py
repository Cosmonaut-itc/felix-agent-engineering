"""U14 R2: orden de feedback por PR y observador real del --seco activo."""
from dataclasses import replace
import json
import subprocess
import unittest
from unittest.mock import patch

import test_activo as activo
from test_u14_r1 import contexto
from dominio import Admitido, Efecto, Entrega, Motivo, Rechazado, Retenido, Terminal, clave, desde_datos
from observador import GitHubGit


def statuses(c, head):
    return [p for p in c.posts("/statuses/") if p["endpoint"].endswith(head)]


def reentregar(c, pr):
    anterior = dict(c.miembros)[pr]
    nuevo = c.commit({"comun": "base\n", "pr-{}".format(pr): "ticket\n",
                      "nuevo": "head\n"}, (anterior,))
    c.push(nuevo, "refs/pull/{}/head".format(pr))
    c.contexto.config["miembros"][pr - 1][1] = nuevo
    c.contexto.tick(100)
    apto = c.raiz / ("apto-{}.json".format(pr))
    dirigida = c.raiz / ("dirigida-{}.json".format(pr))
    apto.write_text(json.dumps({"head": nuevo, "veredicto": "APTO", "hallazgos_abiertos": 0}))
    dirigida.write_text(json.dumps({"head": nuevo, "resultado": "limpio"}))
    with c.ledger() as ledger:
        ledger.entregar(Entrega(activo.REPO, pr, nuevo, str(apto), str(dirigida)), 101)
    return nuevo


class U14R2Tests(unittest.TestCase):
    def test_pending_fallido_separa_dos_rojos_y_otro_pr_no_espera(self):
        with contexto() as c:
            nuevo = reentregar(c, 2)
            original = c.contexto.escritor.ejecutar
            rechazados = []

            def fallar_pending(argv, **kwargs):
                if (argv[3] == "POST" and argv[4].endswith("/statuses/" + nuevo)
                        and json.loads(kwargs["input"])["state"] == "pending"):
                    rechazados.append("pending")
                    raise subprocess.CalledProcessError(1, argv)
                return original(argv, **kwargs)

            c.contexto.escritor.ejecutar = fallar_pending
            c.contexto.tick(102)
            c.contexto.tick(4000)
            (c.raiz / "producto-rojo").write_text("segundo PR causal")
            c.contexto.config["costo_suite_sana_s"] = 10000
            for n in range(30):
                c.contexto.tick(4100 + n * 120)
                if c.estado().trenes[0].fase == Terminal("DEVUELTO"):
                    break
            self.assertEqual(c.estado().trenes[0].fase, Terminal("DEVUELTO"))
            self.assertTrue(rechazados)
            self.assertEqual([p["payload"]["state"] for p in statuses(c, nuevo)], ["failure"])
            filas = c.filas()
            pending = next(f for f in filas if json.loads(f["payload"])["tipo"] == "avisar-en-tren"
                           and json.loads(f["payload"])["datos"][1] == Entrega(activo.REPO, 2, nuevo, "", "").recibo)
            devolucion = next(f for f in filas if json.loads(f["payload"])["tipo"] == "devolver")
            self.assertEqual(pending["estado"], "fallido")
            self.assertEqual(devolucion["estado"], "pendiente")
            self.assertIsNone(devolucion["resultado"])
            args = activo.cli.parser().parse_args(["estado", "--repo", activo.REPO])
            outbox = activo.cli.ejecutar(args)[1]["outbox"]
            espera = next(f["espera"] for f in outbox if f["clave"] == devolucion["clave"])
            self.assertEqual(espera, {"motivo": "feedback-anterior-incompleto", "pr": 2,
                                      "clave": pending["clave"]})
            with c.ledger() as ledger:
                intentos = [json.loads(r[0])["clave"] for r in ledger.db.execute(
                    "SELECT payload FROM events WHERE tipo IN ('efecto-iniciado', 'efecto-fallido')")]
            self.assertNotIn(devolucion["clave"], intentos)
            # Los sobrevivientes ya reciben pending de su siguiente generación.
            for pr in (1, 3):
                self.assertEqual([p["payload"]["state"] for p in statuses(c, dict(c.miembros)[pr])],
                                 ["pending", "pending"])
            self.assertEqual(len(c.posts("/comments")), 1)  # sólo la caducidad
            c.contexto.escritor.ejecutar = original
            c.contexto.tick(9000)
            c.contexto.tick(9100)
            aceptados = statuses(c, nuevo)
            self.assertEqual([p["payload"]["state"] for p in aceptados],
                             ["failure", "pending", "failure"])
            self.assertEqual(aceptados[-1]["payload"]["target_url"],
                             "https://github.com/" + activo.REPO + "/pull/2#issuecomment-2")
            self.assertIn("Entrega devuelta", c.posts("/comments")[-1]["payload"]["body"])
            self.assertNotEqual(aceptados[0]["payload"]["target_url"], aceptados[-1]["payload"]["target_url"])
            self.assertTrue(all(f["estado"] == "completo" for f in c.filas()
                                if f["clave"] in (pending["clave"], devolucion["clave"])))

    def test_seco_activo_admite_failure_y_pending_sin_posts_y_sombra_igual(self):
        for valor in ("failure", "pending"):
            with self.subTest(status=valor), contexto() as c:
                head = c.miembros[0][1]
                c.contexto.feedback_gh(["gh", "api", "--method", "POST",
                    "repos/{}/statuses/{}".format(activo.REPO, head), "--input", "-"],
                    input=json.dumps({"context": "pao-tren/resultado", "state": valor}))
                privada = c.raiz / "config-seco.json"
                privada.write_text(json.dumps({"repos": {activo.REPO: c.contexto.config}}))
                args = activo.cli.parser().parse_args(["tick", "--repo", activo.REPO,
                    "--seco", "--config", str(privada)])
                antes = c.posts()
                # El CLI construye su observador; sólo se sustituye el transporte.
                def crear(config, **kwargs):
                    return GitHubGit(config, ejecutar=c.contexto.gh, **kwargs)

                with patch.object(activo.cli, "GitHubGit", side_effect=crear):
                    _, salida = activo.cli.ejecutar(args, ahora=100)
                    simulado = desde_datos(salida["plan"]["estado"])
                    self.assertEqual(simulado.entradas[0].admision, Admitido())
                    self.assertEqual([e["tipo"] for e in salida["plan"]["efectos"]], ["armar-candidato"])
                    entregar = activo.cli.parser().parse_args(["entregar", "--repo", activo.REPO,
                        "--pr", "1", "--head", head, "--apto", str(c.raiz / "apto-1.json"),
                        "--dirigida", str(c.raiz / "dirigida-1.json")])
                    self.assertEqual(activo.cli.ejecutar(entregar, ahora=101)[0], 0)
                    c.inscritos.write_text(activo.REPO + " sombra\n")
                    _, sombra = activo.cli.ejecutar(args, ahora=102)
                simulado = desde_datos(sombra["plan"]["estado"])
                self.assertEqual(simulado.entradas[0].admision, Retenido(Motivo.CI))
                self.assertFalse(simulado.trenes)
                self.assertEqual(sombra["plan"]["efectos"], [])
                self.assertEqual(c.posts(), antes)
                with c.ledger() as ledger:
                    self.assertEqual(ledger.outbox(activo.REPO), [])
                    self.assertEqual([r[0] for r in ledger.db.execute(
                        "SELECT tipo FROM events WHERE tipo LIKE 'sombra%' ORDER BY seq")],
                        ["sombra", "sombra-doble-camino"])


class U14R2OrdenTests(unittest.TestCase):
    def test_caducidad_pendiente_ordena_por_pr_aun_con_otro_head_y_recuperacion(self):
        with contexto() as c:
            # El predecesor escribe H1; la caducidad posterior resolverá H2.
            primero = c.sembrar_devoluciones(pausa=True)[0]
            head = c.miembros[0][1]
            nuevo = c.commit({"nuevo": "head\n"}, (head,))
            c.push(nuevo, "refs/pull/1/head")
            c.contexto.config["miembros"][0][1] = nuevo
            with c.ledger() as ledger:
                estado = ledger.estado(activo.REPO)
                entrada = replace(estado.entradas[0], admision=Rechazado(Motivo.HEAD))
                estado = replace(estado, entradas=(entrada,) + estado.entradas[1:])
                segundo = Efecto(clave("caduca", entrada.entrega.recibo), "avisar-caducidad",
                                 (entrada.entrega.recibo,))
                ledger.guardar_paso(estado, (segundo,), 60)
                # Recuperar un ejecutando posterior también espera al pendiente.
                ledger.iniciar_efecto(activo.REPO, segundo, 61)
            original = c.contexto.escritor.ejecutar

            def fallar_primero(argv, **kwargs):
                if argv[3] == "POST" and argv[4].endswith("/statuses/" + head):
                    raise subprocess.CalledProcessError(1, argv)
                return original(argv, **kwargs)

            c.contexto.escritor.ejecutar = fallar_primero
            c.contexto.tick(100)
            self.assertEqual(statuses(c, nuevo), [])
            self.assertEqual(len(c.posts("/comments")), 1)
            self.assertEqual([f["estado"] for f in c.filas()], ["fallido", "ejecutando"])
            c.contexto.escritor.ejecutar = original
            c.contexto.tick(200)
            self.assertEqual([p["endpoint"].rsplit("/", 1)[-1] for p in c.posts("/statuses/")],
                             [head, nuevo])
            self.assertEqual(len(c.posts("/comments")), 2)
            self.assertTrue(all(f["estado"] == "completo" for f in c.filas()))
            with c.ledger() as ledger:
                iniciados = [json.loads(r[0])["clave"] for r in ledger.db.execute(
                    "SELECT payload FROM events WHERE tipo='efecto-iniciado' ORDER BY seq")]
            self.assertEqual(iniciados.count(primero.clave), 2)
            self.assertEqual(iniciados.count(segundo.clave), 2)  # siembra + ejecución real


if __name__ == "__main__":
    unittest.main()
