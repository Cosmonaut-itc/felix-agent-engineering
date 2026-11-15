"""U14: feedback sin red, origen git local y POST durables en gh falso."""
from dataclasses import replace
import json
import os
import shlex
import subprocess
import sys
import unittest

import test_activo as activo
from dominio import (Efecto, Motivo, Pausa, Rechazado, Retenido, Terminal,
                     Tren, clave)


class FeedbackTests(unittest.TestCase):
    setUp = activo.ActivoTests.setUp
    comando = activo.ActivoTests.comando
    commit = activo.ActivoTests.commit
    push = activo.ActivoTests.push
    remoto = activo.ActivoTests.remoto
    ledger = activo.ActivoTests.ledger
    estado = activo.ActivoTests.estado
    terminar = activo.ActivoTests.terminar
    hasta_fase = activo.ActivoTests.hasta_fase
    lineas = activo.ActivoTests.lineas
    hijo = activo.ActivoTests.hijo

    def posts(self, tipo=None):
        todos = [json.loads(s) for s in self.lineas("posts.log")]
        return [p for p in todos if tipo is None or tipo in p["endpoint"]]

    def filas(self):
        with self.ledger() as ledger:
            return [dict(r) for r in ledger.db.execute(
                "SELECT e.clave, e.payload, o.estado, r.payload AS resultado FROM effects e "
                "JOIN outbox o ON o.clave=e.clave LEFT JOIN effect_results r ON r.clave=e.clave")]

    def sembrar_devoluciones(self, prs=(1,), pausa=False):
        with self.ledger() as ledger:
            estado = ledger.estado(activo.REPO)
            tren = Tren("lote-rojo", tuple(e.entrega.recibo for e in estado.entradas),
                        Terminal("DEVUELTO"))
            estado = replace(estado, trenes=(tren,), pausa=Pausa("necesita-juicio") if pausa else None,
                entradas=tuple(replace(e, tren=tren.id, terminal=Terminal("DEVUELTO"))
                               for e in estado.entradas))
            efectos = []
            for e in estado.entradas:
                entrega = e.entrega
                if entrega.pr not in prs:
                    continue
                # El argv contiene datos privados que jamás deben viajar al PR.
                payload = {"lote": tren.id, "B": self.B, "C": self.B, "pr": entrega.pr,
                    "head": entrega.head, "fallos": [{"unidad": "caso", "check": "check",
                    "firma": "firma-estable", "conjunto_causal": list(prs),
                    "comando": shlex.join(["bash", "/private/launcher", "--sha", self.B,
                        "--worktree", "/private/tree", "--evidencia", "/private/evidence",
                        "--token", "secreto-no-publicable"])}]}
                efectos.append(Efecto(clave("devolver", tren.id, entrega.recibo), "devolver",
                                      (tren.id, entrega.recibo, json.dumps(payload))))
            ledger.guardar_paso(estado, tuple(efectos), 50)
        return efectos

    def test_rojo_completo_solo_culpable_y_status_apunta_al_comentario(self):
        activo.ActivoTests.test_rojo_atribuye_repite_subconjuntos_y_aterriza_sobrevivientes(self)
        comentarios, statuses = self.posts("/comments"), self.posts("/statuses/")
        self.assertEqual(len(comentarios), 1)
        self.assertEqual(len(statuses), 1)
        self.assertIn("/issues/2/comments", comentarios[0]["endpoint"])
        status = statuses[0]
        self.assertTrue(status["endpoint"].endswith(self.miembros[1][1]))
        self.assertEqual(status["payload"]["context"], "pao-tren/resultado")
        self.assertEqual(status["payload"]["state"], "failure")
        self.assertEqual(status["payload"]["target_url"],
            "https://github.com/" + activo.REPO + "/pull/2#issuecomment-1")
        self.assertEqual(["/comments" in p["endpoint"] for p in self.posts()], [True, False])
        cuerpo = comentarios[0]["payload"]["body"]
        for dato in ("lote", '"B"', '"C"', "unidad", "firma", "conjunto_causal", "--worktree"):
            self.assertIn(dato, cuerpo)
        self.assertNotIn(str(self.raiz), cuerpo)

    def test_terminal_pausa_y_cierre_directo_sin_resultado_para_reductor(self):
        efectos = self.sembrar_devoluciones(pausa=True)
        self.contexto.tick()
        fila = self.filas()[0]
        self.assertEqual(fila["estado"], "completo")
        resultado = json.loads(fila["resultado"])
        self.assertEqual(resultado["status_sha"], self.miembros[0][1])
        self.assertIn("#issuecomment-", resultado["comentario_url"])
        self.assertNotIn("observacion", resultado)
        with self.ledger() as ledger:
            self.assertEqual(ledger.outbox(activo.REPO), [])
            evento = json.loads(ledger.db.execute(
                "SELECT payload FROM events WHERE tipo='efecto-resultado'").fetchone()[0])
        self.assertEqual(evento["clave"], efectos[0].clave)
        cuerpo = self.posts("/comments")[0]["payload"]["body"]
        self.assertNotIn("/private/", cuerpo)
        self.assertNotIn("secreto-no-publicable", cuerpo)
        antes = self.estado()
        self.contexto.tick(200)
        self.assertEqual(self.estado(), antes)
        self.assertEqual(len(self.posts()), 2)
        self.assertEqual(self.lineas("pushes.log"), [])
        self.assertEqual(self.lineas("trabajos.log"), [])

    def muerte_y_recuperacion(self, punto):
        self.sembrar_devoluciones(pausa=True)
        # El comentario nuevo queda en la segunda página.
        (self.raiz / "comentarios.json").write_text(json.dumps({"1": [
            {"body": "comentario anterior", "html_url": "https://github.com/example/old"}
            for _ in range(100)]}))
        hijo = self.hijo("""
from test_activo import Contexto
import os
c = Contexto(sys.argv[3])
def frontera(nombre, efecto):
    if nombre == sys.argv[4]:
        os._exit(77)
c.tick(frontera=frontera)
""", punto)
        salida, error = hijo.communicate(timeout=60)
        self.assertEqual(hijo.returncode, 77, salida + error)
        self.assertEqual(self.filas()[0]["estado"], "ejecutando")
        self.contexto.tick(200)
        self.contexto.tick(300)
        self.assertEqual(len(self.posts("/comments")), 1)
        self.assertEqual(self.filas()[0]["estado"], "completo")
        lecturas = [json.loads(s) for s in self.lineas("feedback-lecturas.log")]
        self.assertTrue(all(a[-2:] == ["--paginate", "--slurp"] for a in lecturas))
        self.assertTrue(self.posts("/statuses/")[-1]["payload"]["target_url"].endswith("-101"))

    def test_muerte_tras_post_comentario_recupera_segunda_pagina(self):
        self.muerte_y_recuperacion("comentario-publicado")
        self.assertEqual(len(self.posts("/statuses/")), 1)

    def test_muerte_tras_status_antes_de_registrar_repite_contexto(self):
        self.muerte_y_recuperacion("antes-registrar")
        self.assertEqual(len(self.posts("/statuses/")), 2)
        self.assertEqual(self.posts("/statuses/")[0], self.posts("/statuses/")[1])

    def test_caducidad_head_cambiado_sin_tren_status_en_head_actual(self):
        nuevo = self.commit({"comun": "base\n", "nuevo": "head\n"}, (self.miembros[0][1],))
        self.push(nuevo, "refs/pull/1/head")
        self.contexto.config["miembros"][0][1] = nuevo
        self.contexto.tick()
        self.assertEqual(self.estado().entradas[0].admision, Rechazado(Motivo.HEAD))
        self.assertEqual(len(self.posts("/comments")), 1)
        self.assertIn("head-cambiado", self.posts("/comments")[0]["payload"]["body"])
        self.assertTrue(self.posts("/statuses/")[0]["endpoint"].endswith(nuevo))
        self.contexto.tick(200)
        self.assertEqual(len(self.posts()), 2)
        # El aviso failure despierta al dueño, pero no invalida CI de su head.
        self.assertEqual(self.contexto.observador.ci(activo.REPO, nuevo), "verde")

    def test_caducidad_pr_cerrado_comenta_sin_status(self):
        self.hasta_fase(activo.Congelado)
        (self.raiz / "pr-cerrado-1").write_text("cerrado")
        self.contexto.tick(500)
        self.assertEqual(self.estado().entradas[0].admision, Rechazado(Motivo.CERRADO))
        self.assertEqual(len(self.posts("/comments")), 1)
        self.assertIn("pr-no-abierto", self.posts()[0]["payload"]["body"])
        self.assertEqual(self.posts("/statuses/"), [])
        fila = next(f for f in self.filas() if "avisar-caducidad" in f["payload"])
        self.assertIsNone(json.loads(fila["resultado"])["status_sha"])

    def test_caducidad_reintenta_con_pr_ya_mergeado_no_status(self):
        self.sembrar_devoluciones(pausa=True)
        with self.ledger() as ledger:
            estado = ledger.estado(activo.REPO)
            entrada = replace(estado.entradas[0], admision=Rechazado(Motivo.HEAD))
            estado = replace(estado, entradas=(entrada,) + estado.entradas[1:])
            efecto = Efecto(clave("caduca", entrada.entrega.recibo), "avisar-caducidad",
                            (entrada.entrega.recibo,))
            ledger.guardar_paso(estado, (efecto,), 60)
        self.comando(self.origen, "update-ref", "refs/heads/main", self.miembros[0][1])
        self.contexto.tick()
        # Sólo el devolver previamente sembrado escribe status; caducidad no.
        self.assertEqual(len(self.posts("/statuses/")), 1)
        fila = next(f for f in self.filas() if f["clave"] == efecto.clave)
        self.assertIsNone(json.loads(fila["resultado"])["status_sha"])

    def test_post_fallido_visible_no_bloquea_y_reintenta(self):
        self.sembrar_devoluciones((1, 2), pausa=True)
        fallo = self.raiz / "fallo-post"
        fallo.write_text("/issues/1/comments")
        self.contexto.tick()
        self.assertEqual([f["estado"] for f in self.filas()], ["fallido", "completo"])
        args = activo.cli.parser().parse_args(["estado", "--repo", activo.REPO])
        fila = activo.cli.ejecutar(args)[1]["outbox"][0]
        self.assertEqual(fila["estado"], "fallido")
        self.assertTrue(fila["ejecutor"])
        self.assertNotIn("dato privado", json.dumps(fila))
        fallo.rename(self.raiz / "fallo-post-retirado")
        self.contexto.tick(200)
        self.assertTrue(all(f["estado"] == "completo" for f in self.filas()))
        comentarios = json.loads((self.raiz / "comentarios.json").read_text())
        self.assertEqual([len(v) for v in comentarios.values()], [1, 1])

    def test_status_fallido_conserva_comentario_y_cambia_a_head_vigente_al_reintentar(self):
        # Caducidad pendiente sin tren, incluso después de otro push del dueño.
        self.sembrar_devoluciones(pausa=True)
        with self.ledger() as ledger:
            estado = ledger.estado(activo.REPO)
            entrada = replace(estado.entradas[0], admision=Rechazado(Motivo.HEAD))
            estado = replace(estado, entradas=(entrada,) + estado.entradas[1:])
            efecto = Efecto(clave("caduca", entrada.entrega.recibo), "avisar-caducidad",
                            (entrada.entrega.recibo,))
            ledger.guardar_paso(estado, (efecto,), 60)
        fallo = self.raiz / "fallo-post"
        fallo.write_text("/statuses/")
        self.contexto.tick()
        self.assertTrue(all(f["estado"] == "fallido" for f in self.filas()))
        nuevo = self.commit({"nuevo": "push\n"}, (self.miembros[0][1],))
        self.push(nuevo, "refs/pull/1/head")
        self.contexto.config["miembros"][0][1] = nuevo
        fallo.rename(self.raiz / "fallo-post-retirado")
        self.contexto.tick(200)
        self.assertEqual(len(self.posts("/comments")), 2)  # uno por efecto
        self.assertEqual(json.loads(self.filas()[-1]["resultado"])["status_sha"], nuevo)
        self.assertTrue(all(f["estado"] == "completo" for f in self.filas()))

    def test_contexto_feedback_no_es_ci_en_activo_y_sombra_conserva_su_regla(self):
        self.sembrar_devoluciones(pausa=True)
        self.contexto.tick()
        head = self.miembros[0][1]
        self.assertEqual(self.contexto.observador.ci(activo.REPO, head), "verde")
        sombra = activo.GitHubGit(self.contexto.config, ejecutar=self.contexto.gh, sombra=True)
        self.assertEqual(sombra.ci(activo.REPO, head), "pendiente")
        self.contexto.feedback_gh(["gh", "api", "--method", "POST",
            "repos/{}/statuses/{}".format(activo.REPO, head), "--input", "-"],
            input=json.dumps({"context": "otro-check", "state": "failure"}))
        self.assertEqual(self.contexto.observador.ci(activo.REPO, head), "pendiente")

    def test_seco_entregar_y_sombra_no_publican_aun_con_feedback_pendiente(self):
        self.sembrar_devoluciones(pausa=True)
        args = activo.cli.parser().parse_args(["tick", "--repo", activo.REPO, "--seco"])
        for modo in ("activo", "sombra"):
            self.inscritos.write_text(activo.REPO + " " + modo + "\n")
            self.assertEqual(activo.cli.ejecutar(args, self.contexto.observador, 200,
                escritor=self.contexto.escritor)[0], 0)
            entregar = activo.cli.parser().parse_args(["entregar", "--repo", activo.REPO,
                "--pr", "1", "--head", self.miembros[0][1], "--apto", str(self.raiz / "apto-1.json"),
                "--dirigida", str(self.raiz / "dirigida-1.json")])
            self.assertEqual(activo.cli.ejecutar(entregar, self.contexto.observador, 300,
                escritor=self.contexto.escritor)[0], 0)
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.filas()[0]["estado"], "pendiente")

    def test_apto_retirado_sigue_retenido_sin_inventar_caducidad(self):
        (self.raiz / "apto-1.json").write_text("{}")
        self.contexto.tick()
        self.assertEqual(self.estado().entradas[0].admision, Retenido(Motivo.APTO))
        self.assertEqual(self.posts(), [])

    def test_cli_real_inyecta_escritor_por_defecto(self):
        self.sembrar_devoluciones(pausa=True)
        ejecutables = self.raiz / "bin-feedback"
        ejecutables.mkdir()
        helper = self.raiz / "gh-feedback.py"
        helper.write_text("import sys\nsys.path[:0] = " + repr([str(activo.RAIZ), str(activo.RAIZ / "tests")]) +
            "\nfrom test_activo import Contexto\nc = Contexto(" + repr(str(self.ruta_contexto)) + ")\n"
            "kwargs = {'input': sys.stdin.read()} if '--input' in sys.argv else {}\n"
            "r = c.feedback_gh(['gh'] + sys.argv[1:], **kwargs)\nprint(r.stdout, end='')\n")
        gh = ejecutables / "gh"
        gh.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, "-I", str(helper)]) + ' "$@"\n')
        gh.chmod(0o700)
        privada = self.raiz / "config-feedback.json"
        privada.write_text(json.dumps({"repos": {activo.REPO: self.contexto.config}}))
        proc = subprocess.run([sys.executable, "-I", str(activo.RAIZ / "pao-tren"),
            "tick", "--repo", activo.REPO, "--config", str(privada)], capture_output=True,
            text=True, timeout=60, env=dict(os.environ,
                PATH=str(ejecutables) + os.pathsep + os.environ["PATH"]))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.filas()[0]["estado"], "completo", proc.stdout)
        self.assertEqual(len(self.posts()), 2)


if __name__ == "__main__":
    unittest.main()
