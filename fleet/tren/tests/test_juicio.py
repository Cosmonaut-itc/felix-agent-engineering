"""U15: CLI, tick, git local y muertes reales; sin red ni borrar evidencia."""
from dataclasses import replace
import json
import os
import sqlite3
import subprocess
import sys
import unittest

import test_activo as activo
from dominio import (Armado, Certificado, ConflictoCadena, Congelado, Corriendo,
                     Diagnosticando, EsperandoCI, Pausa, Reproduccion,
                     Terminal, clave, datos)
from juicios import politica, tren_pausado

REPO = activo.REPO


class JuicioTests(unittest.TestCase):
    setUp = activo.ActivoTests.setUp
    comando = activo.ActivoTests.comando
    commit = activo.ActivoTests.commit
    push = activo.ActivoTests.push
    remoto = activo.ActivoTests.remoto
    ledger = activo.ActivoTests.ledger
    estado = activo.ActivoTests.estado
    hasta_fase = activo.ActivoTests.hasta_fase
    lineas = activo.ActivoTests.lineas
    hijo = activo.ActivoTests.hijo

    def cli(self, *argv):
        proc = subprocess.run([sys.executable, "-I", str(activo.RAIZ / "pao-tren"),
                               argv[0], "--repo", REPO, *argv[1:]],
                              capture_output=True, text=True, timeout=30)
        return proc.returncode, json.loads(proc.stdout)

    def juicio(self, accion, pr=None):
        tren = tren_pausado(self.estado())
        argv = ["juicio", "--decision", accion, "--motivo", "decisión de prueba"]
        if tren:
            argv += ["--tren", tren.id]
        if pr is not None:
            argv += ["--pr", str(pr)]
        codigo, resultado = self.cli(*argv)
        self.assertEqual(codigo, 0, resultado)
        return resultado["decision"]

    def hasta_pausa(self):
        for n in range(30):
            self.contexto.tick(5000 + n * 120)
            if self.estado().pausa:
                return self.estado()
        self.fail("no se pausó: " + str(datos(self.estado())))

    def rojo(self):
        (self.raiz / "producto-rojo").write_text("rojo sin presupuesto de atribución")
        estado = self.hasta_pausa()
        self.assertEqual(estado.pausa, Pausa("necesita-juicio", detalle="sin-costo-de-suite-sana"))
        self.assertIsInstance(estado.trenes[-1].fase, Diagnosticando)
        self.assertEqual(self.remoto(), self.B)
        return estado

    def aterrizar_ultimo(self):
        for n in range(30):
            self.contexto.tick(10000 + n * 120)
            estado = self.estado()
            self.assertIsNone(estado.pausa, datos(estado.pausa))
            if estado.trenes[-1].fase == Terminal("ATERRIZADO"):
                return estado
        self.fail("no aterrizó: " + str(datos(self.estado())))

    def posts(self, tipo=None):
        posts = [json.loads(s) for s in self.lineas("posts.log")]
        return [p for p in posts if tipo is None or tipo in p["endpoint"]]

    def decisiones(self):
        return self.cli("estado", "--juicio")[1]["juicio"]["decisiones"]

    def test_absolver_mismo_c_suite_nueva_ci_verde_y_un_aterrizaje(self):
        antes = self.rojo()
        candidato = antes.trenes[0].fase.candidato
        io = [self.lineas(n) for n in ("github.log", "trabajos.log", "posts.log", "pushes.log")]
        decision = self.juicio("absolver")
        self.assertEqual(self.estado(), antes)
        self.assertEqual(io, [self.lineas(n) for n in ("github.log", "trabajos.log", "posts.log", "pushes.log")])
        self.assertEqual(self.decisiones()[0]["estado"], "pendiente")
        self.contexto.tick(8000)
        nuevo = self.estado().trenes[-1]
        self.assertIsInstance(nuevo.fase, Armado)
        self.assertEqual(nuevo.fase.candidato, candidato)
        self.assertNotEqual(nuevo.id, antes.trenes[0].id)
        # Una absolución no inventa CI verde ni un certificado.
        ci_original = self.contexto.observador.ci
        def ci(repo, sha, rama=None):
            return (type(ci_original(repo, sha, rama))("pendiente") if sha == candidato.C else
                    ci_original(repo, sha, rama))
        self.contexto.observador.ci = ci
        for n in range(3):
            self.contexto.tick(8200 + n)
        self.assertIsInstance(self.estado().trenes[-1].fase, EsperandoCI)
        self.assertEqual(self.estado().trenes[-1].fase.solicitud, 1)
        self.assertEqual(len(self.lineas("trabajos.log")), 1)
        self.contexto.observador.ci = ci_original
        (self.raiz / "producto-rojo").rename(self.raiz / "rojo-retirado")
        final = self.aterrizar_ultimo()
        self.assertEqual(self.remoto(), candidato.C)
        self.assertEqual(len(self.lineas("trabajos.log")), 2)
        self.assertEqual(sum(p.endswith(":refs/heads/main") for p in self.lineas("pushes.log")), 1)
        self.assertEqual([t.fase for t in final.trenes], [Terminal("ABSUELTO"), Terminal("ATERRIZADO")])
        self.assertEqual(self.posts("/comments"), [])
        self.assertFalse(any(p["payload"]["state"] == "failure" for p in self.posts("/statuses/")))
        self.assertEqual(self.decisiones()[0]["decision"]["id"], decision["id"])
        self.assertEqual(self.decisiones()[0]["estado"], "aplicada")

    def test_absolver_otro_rojo_vuelve_a_pausar_con_identidad_nueva(self):
        antes = self.rojo()
        expediente = self.cli("estado", "--juicio")[1]["juicio"]
        self.juicio("absolver")
        self.contexto.tick(8000)
        despues = self.hasta_pausa()
        self.assertEqual(despues.pausa, antes.pausa)
        self.assertEqual(despues.trenes[-1].fase.candidato.C, antes.trenes[0].fase.candidato.C)
        self.assertNotEqual(self.cli("estado", "--juicio")[1]["juicio"]["pausa_id"], expediente["pausa_id"])
        self.assertEqual(len(self.lineas("trabajos.log")), 2)
        self.assertEqual(self.remoto(), self.B)

    def test_expulsar_uno_de_tres_feedback_y_nueva_generacion_sin_culpable(self):
        self.rojo()
        self.juicio("expulsar", 2)
        self.contexto.tick(8000)
        intermedio = self.estado()
        self.assertEqual(intermedio.trenes[0].fase, Terminal("DEVUELTO"))
        self.assertIsInstance(intermedio.trenes[1].fase, Congelado)
        self.assertEqual(len(intermedio.trenes[1].recibos), 2)
        final = self.aterrizar_ultimo()
        self.assertEqual(final.entradas[1].terminal, Terminal("DEVUELTO"))
        self.assertEqual(len(self.posts("/comments")), 1)
        self.assertIn("/issues/2/comments", self.posts("/comments")[0]["endpoint"])
        self.assertIn("decisi", self.posts("/comments")[0]["payload"]["body"])
        fallos = [p for p in self.posts("/statuses/") if p["payload"]["state"] == "failure"]
        self.assertEqual(len(fallos), 1)
        self.assertTrue(fallos[0]["endpoint"].endswith(self.miembros[1][1]))
        self.assertIn("target_url", fallos[0]["payload"])
        for indice in (0, 2):
            self.comando(self.origen, "merge-base", "--is-ancestor", self.miembros[indice][1], self.remoto())
        self.assertNotEqual(self.comando(self.origen, "merge-base", self.miembros[1][1], self.remoto()),
                            self.miembros[1][1])
        self.assertEqual([p["payload"]["state"] for p in self.posts("/statuses/")],
                         ["pending"] * 3 + ["failure"] + ["pending"] * 2 + ["success"] * 2)

    def test_abortar_terminal_sin_rojo_ni_push_custodia_intacta_readmision_siguiente_tick(self):
        self.rojo()
        with self.ledger() as ledger:
            custodia = [tuple(r) for r in ledger.db.execute("SELECT * FROM entries")]
        self.juicio("abortar")
        self.contexto.tick(8000)
        estado = self.estado()
        self.assertEqual([t.fase for t in estado.trenes], [Terminal("ABORTADO")])
        self.assertTrue(all(e.tren is None and e.terminal is None for e in estado.entradas))
        self.assertEqual(self.remoto(), self.B)
        self.assertEqual(self.posts("/comments"), [])
        self.assertFalse(any(p["payload"]["state"] == "failure" for p in self.posts("/statuses/")))
        with self.ledger() as ledger:
            self.assertEqual([tuple(r) for r in ledger.db.execute("SELECT * FROM entries")], custodia)
        self.contexto.tick(8200)
        self.assertEqual(len(self.estado().trenes), 2)
        self.assertIsInstance(self.estado().trenes[-1].fase, Congelado)

    def test_escritor_externo_reanudar_rearma_sobre_main_ajeno(self):
        self.hasta_fase(Certificado)
        ajeno = self.commit({"comun": "base\n", "externo": "cambio\n"}, (self.B,))
        self.push(ajeno, "refs/heads/ajeno")
        (self.raiz / "escritor").write_text(ajeno)
        self.assertEqual(self.hasta_pausa().pausa.motivo, "escritor-externo")
        self.juicio("reanudar")
        self.contexto.tick(8000)
        self.assertEqual(self.remoto(), ajeno)
        self.assertEqual(len(self.estado().trenes), 1)
        (self.raiz / "escritor").rename(self.raiz / "escritor-retirado")
        self.contexto.config["base"] = ajeno
        self.contexto.tick(8200)
        self.assertEqual(self.estado().trenes[-1].fase, Congelado(ajeno))
        self.assertEqual(self.remoto(), ajeno)
        self.aterrizar_ultimo()
        self.comando(self.origen, "merge-base", "--is-ancestor", ajeno, self.remoto())

    def test_cli_invalido_terminal_y_sombra_no_registran_ni_cambian_estado(self):
        antes = self.rojo()
        tren = antes.trenes[0].id
        for args in (
                ["--tren", tren, "--decision", "expulsar", "--pr", "999"],
                ["--tren", tren, "--decision", "expulsar"],
                ["--tren", tren, "--decision", "absolver", "--pr", "1"],
                ["--tren", tren, "--decision", "reanudar"],
                ["--decision", "abortar"],
                ["--tren", "otro", "--decision", "abortar"]):
            self.assertEqual(self.cli("juicio", *args, "--motivo", "prueba")[0], 2)
        self.assertEqual(self.cli("pausa", "--levantar", "--motivo", "prueba")[0], 2)
        self.assertEqual(self.cli("pausa", "--motivo", "prueba")[0], 2)
        self.assertEqual(self.estado(), antes)
        self.assertEqual(self.decisiones(), [])
        self.inscritos.write_text(REPO + " sombra\n")
        self.assertEqual(self.cli("juicio", "--tren", tren, "--decision", "absolver", "--motivo", "prueba")[0], 2)
        self.assertEqual(self.cli("pausa", "--motivo", "prueba")[0], 2)
        self.assertEqual(self.estado(), antes)
        self.inscritos.write_text(REPO + " activo\n")
        self.juicio("abortar")
        self.contexto.tick(8000)
        terminal = self.estado()
        self.assertEqual(self.cli("juicio", "--tren", tren, "--decision", "abortar", "--motivo", "prueba")[0], 2)
        self.assertEqual(self.estado(), terminal)

    def test_decision_obsoleta_se_rechaza_durable_sin_efecto(self):
        antes = self.rojo()
        self.juicio("absolver")
        # Otra observación ha sustituido la pausa antes del consumo.
        with self.ledger() as ledger:
            ledger.guardar_paso(replace(antes, pausa=Pausa("head-cambiado-en-tren")), (), 7900)
        cambiado = self.estado()
        trabajos = self.lineas("trabajos.log")
        self.contexto.tick(8000)
        self.assertEqual(self.estado(), cambiado)
        self.assertEqual(self.lineas("trabajos.log"), trabajos)
        self.assertEqual(self.decisiones()[0]["estado"], "rechazada")
        self.assertEqual(self.decisiones()[0]["resultado"]["motivo"], "pausa-no-vigente")

    def test_mismo_texto_de_pausa_en_otra_aparicion_no_reutiliza_decision(self):
        antes = self.rojo()
        decision = self.juicio("absolver")
        with self.ledger() as ledger:
            ledger.guardar_paso(replace(antes, pausa=None), (), 7900)
            ledger.guardar_paso(antes, (), 7910)
            self.assertNotEqual(ledger.pausa_id(REPO), decision["pausa_id"])
        self.contexto.tick(8000)
        self.assertEqual(self.estado(), antes)
        self.assertEqual(self.decisiones()[0]["estado"], "rechazada")

    def test_estado_expediente_resultado_comandos_e_historial(self):
        antes = self.rojo()
        codigo, payload = self.cli("estado", "--juicio")
        self.assertEqual(codigo, 0)
        expediente = payload["juicio"]
        self.assertEqual(expediente["pausa"], datos(antes.pausa))
        self.assertEqual(expediente["tren"], antes.trenes[0].id)
        self.assertEqual(expediente["miembros"], [{"pr": pr, "head": head} for pr, head in self.miembros])
        self.assertEqual(expediente["fase"]["_tipo"], "Diagnosticando")
        self.assertEqual(expediente["resultados"][0]["resultado"]["observacion"]["resultado_suite"]["veredicto"], "rojo")
        self.assertEqual({a["decision"] for a in expediente["admitidas"]}, {"absolver", "expulsar", "abortar"})
        import shlex
        for accion in expediente["admitidas"]:
            args = activo.cli.parser().parse_args(shlex.split(accion["comando"])[1:])
            self.assertEqual(args.repo, REPO)
            self.assertEqual(args.tren, antes.trenes[0].id)
            self.assertEqual(args.decision, accion["decision"])
        self.juicio("absolver")
        self.assertEqual(self.decisiones()[0]["estado"], "pendiente")
        self.contexto.tick(8000)
        self.assertEqual(self.decisiones()[0]["estado"], "aplicada")

    def test_pausa_manual_bloquea_recuperacion_git_suite_y_deja_feedback(self):
        # Un efecto de git quedó iniciado sin IO; debe esperar el levantamiento.
        class Muerte(Exception):
            pass
        def detener(nombre, efecto):
            if nombre == "antes-ejecutar" and efecto.tipo == "armar-candidato":
                raise Muerte()
        with self.assertRaises(Muerte):
            self.contexto.tick(100, detener)
        self.assertEqual(self.cli("pausa", "--motivo", "mantenimiento")[0], 0)
        self.contexto.tick(200)
        self.assertEqual(self.estado().pausa, Pausa("manual", detalle="mantenimiento"))
        self.assertEqual(self.lineas("pushes.log"), [])
        self.assertEqual(self.lineas("trabajos.log"), [])
        self.assertEqual(len(self.posts("/statuses/")), 3)
        for n in range(3):
            self.contexto.tick(300 + n)
        self.assertEqual(self.lineas("pushes.log"), [])
        expediente = self.cli("estado", "--juicio")[1]["juicio"]
        self.assertEqual([a["decision"] for a in expediente["admitidas"]], ["levantar"])
        self.assertEqual(self.cli("pausa", "--levantar", "--motivo", "terminado")[0], 0)
        self.contexto.tick(500)
        self.assertIsNone(self.estado().pausa)
        self.aterrizar_ultimo()

    def test_manual_con_resultado_suite_guardado_no_lo_pierde(self):
        self.hasta_fase(EsperandoCI)
        # Suite ejecutada, resultado aún no consumido.
        self.contexto.tick(2000)
        self.assertEqual(len(self.lineas("trabajos.log")), 1)
        self.assertEqual(self.cli("pausa", "--motivo", "revisión")[0], 0)
        self.contexto.tick(2200)
        self.assertIsNotNone(self.estado().pausa)
        self.assertEqual(self.remoto(), self.B)
        self.assertEqual(self.cli("pausa", "--levantar", "--motivo", "seguir")[0], 0)
        self.contexto.tick(2400)
        self.aterrizar_ultimo()
        self.assertEqual(len(self.lineas("trabajos.log")), 1)

    def test_manual_impide_recuperar_lanzamiento_de_suite_hasta_levantar(self):
        self.hasta_fase(EsperandoCI)
        class Muerte(Exception):
            pass
        def detener(nombre, efecto):
            if nombre == "antes-ejecutar" and efecto.tipo == "iniciar-suite":
                raise Muerte()
        with self.assertRaises(Muerte):
            self.contexto.tick(2000, detener)
        self.assertEqual(self.lineas("trabajos.log"), [])
        self.assertEqual(self.cli("pausa", "--motivo", "esperar")[0], 0)
        self.contexto.tick(2100)
        self.contexto.tick(2200)
        self.assertEqual(self.lineas("trabajos.log"), [])
        self.assertEqual(self.remoto(), self.B)
        self.assertEqual(self.cli("pausa", "--levantar", "--motivo", "seguir")[0], 0)
        self.contexto.tick(2300)
        self.aterrizar_ultimo()
        self.assertEqual(len(self.lineas("trabajos.log")), 1)

    def test_rollback_consumo_y_estado_y_efectos_juntos(self):
        antes = self.rojo()
        self.juicio("expulsar", 2)
        with self.ledger() as ledger:
            ledger.db.execute("CREATE TRIGGER fallar BEFORE INSERT ON events "
                              "WHEN NEW.tipo='operativo' BEGIN SELECT RAISE(ABORT, 'inyectado'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.contexto.tick(8000)
        self.assertEqual(self.estado(), antes)
        self.assertEqual(self.decisiones()[0]["estado"], "pendiente")
        self.assertEqual(self.posts("/comments"), [])
        with self.ledger() as ledger:
            ledger.db.execute("DROP TRIGGER fallar")
        self.contexto.tick(8200)
        self.aterrizar_ultimo()
        self.assertEqual(len(self.posts("/comments")), 1)
        self.assertEqual(self.decisiones()[0]["estado"], "aplicada")

    def test_conflicto_en_cadena_solo_expulsa_pr_en_conflicto(self):
        original = self.contexto.git.armar
        self.contexto.git.armar = lambda base, miembros: ConflictoCadena(2, ("archivo",))
        self.assertEqual(self.hasta_pausa().pausa.motivo, "conflicto-en-cadena")
        tren = self.estado().trenes[0].id
        self.assertEqual(self.cli("juicio", "--tren", tren, "--decision", "expulsar",
                                  "--pr", "1", "--motivo", "prueba")[0], 2)
        self.juicio("expulsar", 2)
        self.contexto.git.armar = original
        self.contexto.tick(8000)
        self.aterrizar_ultimo()
        self.assertEqual(len(self.posts("/comments")), 1)
        self.assertIn("conflicto-en-cadena", self.posts("/comments")[0]["payload"]["body"])

    def test_reanudar_candidato_incorrecto_repite_armado_no_certifica(self):
        original = self.contexto.git.armar
        self.contexto.git.armar = lambda base, miembros: replace(original(base, miembros), B="a" * 40)
        self.assertEqual(self.hasta_pausa().pausa.motivo, "candidato-no-corresponde")
        self.contexto.git.armar = original
        self.juicio("reanudar")
        self.contexto.tick(8000)
        self.assertIsInstance(self.estado().trenes[0].fase, Congelado)
        self.assertEqual(self.remoto(), self.B)
        self.aterrizar_ultimo()
        self.assertEqual(len(self.lineas("trabajos.log")), 1)

    def test_reanudar_rama_ocupada_nunca_la_sobrescribe(self):
        self.hasta_fase(Armado)
        tren = self.estado().trenes[0]
        ref = "refs/heads/tren/" + tren.id
        self.comando(self.origen, "update-ref", ref, self.B)
        self.assertEqual(self.hasta_pausa().pausa.motivo, "rama-tren-ocupada")
        self.juicio("reanudar")
        self.contexto.tick(8000)
        self.assertEqual(self.remoto(ref), self.B)
        self.assertEqual(self.remoto(), self.B)
        self.assertEqual(self.hasta_pausa().pausa.motivo, "rama-tren-ocupada")
        # Intervención explícita que arregla la rama; el tren jamás la fuerza.
        intervencion = self.contexto.git.git("push", str(self.origen), tren.fase.candidato.C + ":" + ref)
        self.assertEqual(intervencion.returncode, 0, intervencion.stderr)
        self.juicio("reanudar")
        self.contexto.tick(9000)
        self.aterrizar_ultimo()

    def test_reanudar_promocion_conserva_certificado_y_agrega_intento_cas(self):
        self.hasta_fase(Certificado)
        (self.raiz / "rechazar-push").write_text("rechazar")
        self.assertEqual(self.hasta_pausa().pausa.motivo, "promocion-rechazada")
        self.juicio("reanudar")
        (self.raiz / "rechazar-push").rename(self.raiz / "rechazo-retirado")
        self.contexto.tick(8000)
        self.aterrizar_ultimo()
        self.assertEqual(len(self.lineas("trabajos.log")), 1)
        with self.ledger() as ledger:
            promociones = [json.loads(r[0]) for r in ledger.db.execute("SELECT payload FROM effects")
                           if json.loads(r[0])["tipo"] == "promover"]
        self.assertEqual(len(promociones), 4)

    def test_reanudar_infraestructura_otorga_un_intento_nuevo_sin_certificado(self):
        (self.raiz / "suite-75").write_text("75")
        lanzador = self.contexto.lanzador
        def siempre_75(argv, **kwargs):
            resultado = lanzador(argv, **kwargs)
            import shlex
            # El transporte conserva evidencia separada por intento, como el
            # coordinador real; cada retorno 75 guarda su propio reenganche.
            ruta = self.raiz / (clave(*argv) + ".json")
            candidato = self.estado().trenes[0].fase.candidato
            from test_hasito import suite_fixture
            payload = suite_fixture(candidato, 75)
            payload["reanudar"] = shlex.join([a for a in argv if a != "--reanudar"] + ["--reanudar"])
            ruta.write_text(json.dumps(payload))
            return subprocess.CompletedProcess(argv, 75, "SUITE veredicto=75 resultado=" + str(ruta) + "\n", "")
        self.contexto.hasito.runner = siempre_75
        for n in range(15):
            estado = self.estado()
            espera = estado.trenes[0].fase.esperar_hasta if estado.trenes and isinstance(
                estado.trenes[0].fase, Corriendo) else None
            self.contexto.tick(espera if espera is not None else 10000 + n * 4000)
            if self.estado().pausa:
                break
        self.assertEqual(self.estado().pausa.motivo, "infraestructura")
        self.juicio("reanudar")
        self.contexto.tick(80000)
        self.assertEqual(self.estado().trenes[0].fase.reanudaciones, 4)
        self.assertEqual(self.remoto(), self.B)
        self.contexto.tick(81000)
        self.assertEqual(self.estado().pausa.motivo, "infraestructura")

    def test_infraestructura_de_diagnostico_respeta_presupuesto_d5(self):
        estado = self.rojo()
        tren = estado.trenes[0]
        fase = replace(tren.fase, reanudaciones=3, reanudar="comando guardado",
            presupuesto_s=39, pendiente=Reproduccion("reproduccion", tren.fase.fallos[0], "C", costo_s=20))
        with self.ledger() as ledger:
            ledger.guardar_paso(replace(estado, trenes=(replace(tren, fase=fase),),
                pausa=Pausa("infraestructura", detalle="tres-reanudaciones-agotadas")), (), 7000)
        antes = self.estado()
        codigo, resultado = self.cli("juicio", "--tren", tren.id, "--decision", "reanudar", "--motivo", "prueba")
        self.assertEqual(codigo, 2)
        self.assertEqual(resultado["error"], "presupuesto-de-diagnostico-agotado")
        self.assertEqual(self.estado(), antes)
        self.assertEqual(self.decisiones(), [])
        self.assertEqual([a["decision"] for a in self.cli("estado", "--juicio")[1]["juicio"]["admitidas"]],
                         ["absolver", "abortar"])

    def test_head_cambiado_se_expulsa_sin_usar_su_suite_para_sobrevivientes(self):
        self.hasta_fase(Armado)
        nuevo = self.commit({"comun": "base\n", "nuevo": "head\n"}, (self.miembros[0][1],))
        self.push(nuevo, "refs/pull/1/head")
        self.contexto.config["miembros"][0][1] = nuevo
        self.assertEqual(self.hasta_pausa().pausa.motivo, "head-cambiado-en-tren")
        self.juicio("expulsar", 1)
        self.contexto.tick(8000)
        self.aterrizar_ultimo()
        self.assertEqual(self.estado().entradas[0].terminal, Terminal("DEVUELTO"))
        self.assertNotEqual(self.comando(self.origen, "merge-base", nuevo, self.remoto()), nuevo)
        self.assertEqual(len(self.lineas("trabajos.log")), 1)

    def test_expulsar_head_cambiado_despues_del_certificado_conserva_base(self):
        self.hasta_fase(Certificado)
        nuevo = self.commit({"comun": "base\n", "nuevo": "head\n"}, (self.miembros[0][1],))
        self.push(nuevo, "refs/pull/1/head")
        self.contexto.config["miembros"][0][1] = nuevo
        self.assertEqual(self.hasta_pausa().pausa.motivo, "head-cambiado-en-tren")
        self.juicio("expulsar", 1)
        self.contexto.tick(8000)
        self.assertEqual(self.estado().trenes[-1].fase, Congelado(self.B))
        self.aterrizar_ultimo()
        self.assertEqual(len(self.lineas("trabajos.log")), 2)

    def test_tabla_cierra_detalles_desconocidos_y_certificado_sin_base(self):
        self.hasta_fase(Certificado)
        estado = self.estado()
        tren = estado.trenes[0]
        with self.ledger() as ledger:
            sin_base = replace(tren, fase=replace(tren.fase, B=""))
            ledger.guardar_paso(replace(estado, trenes=(sin_base,)), (), 7000)
        self.assertEqual(self.hasta_pausa().pausa.motivo, "certificado-sin-base")
        self.assertEqual({a["decision"] for a in self.cli("estado", "--juicio")[1]["juicio"]["admitidas"]}, {"abortar"})
        self.juicio("abortar")
        self.contexto.tick(8000)
        self.assertEqual(self.remoto(), self.B)
        self.assertEqual(politica(Pausa("necesita-juicio", detalle="nuevo-no-revisado")), {})
        for detalle in ("sin-duracion:unidad", "presupuesto-agotado:unidad", "confirmacion-candidato:unidad",
                        "definicion-distinta:unidad", "confirmacion-base:unidad", "reproduccion-incierta:unidad"):
            self.assertEqual(set(politica(Pausa("necesita-juicio", detalle=detalle))), {"absolver", "expulsar", "abortar"})

    def muerte_decision(self, punto):
        self.rojo()
        if punto != "decision-guardada":
            self.juicio("absolver")
        hijo = self.hijo('''
from test_activo import Contexto, cli, REPO
from ledger import Ledger
from juicios import tren_pausado
import os
c = Contexto(sys.argv[3])
def frontera(nombre, efecto):
    if nombre == sys.argv[4]:
        os._exit(77)
if sys.argv[4] == 'decision-guardada':
    l = Ledger()
    tren = tren_pausado(l.estado(REPO)).id
    l.close()
    args = cli.parser().parse_args(['juicio', '--repo', REPO, '--tren', tren,
        '--decision', 'absolver', '--motivo', 'prueba de muerte'])
    cli.ejecutar(args, ahora=8000, frontera=frontera)
else:
    c.tick(8000, frontera)
''', punto)
        salida, error = hijo.communicate(timeout=60)
        self.assertEqual(hijo.returncode, 77, salida + error)
        self.assertEqual(self.decisiones()[0]["estado"],
                         "aplicada" if punto == "paso-guardado" else "pendiente")
        (self.raiz / "producto-rojo").rename(self.raiz / "rojo-retirado")
        self.aterrizar_ultimo()
        self.assertEqual(len(self.estado().trenes), 2)
        self.assertEqual(len(self.lineas("trabajos.log")), 2)
        self.assertEqual(sum(p.endswith(":refs/heads/main") for p in self.lineas("pushes.log")), 1)
        with self.ledger() as ledger:
            self.assertEqual(ledger.db.execute("SELECT count(*) FROM events WHERE tipo='juicio-resuelto'").fetchone()[0], 1)
        self.assertEqual(self.decisiones()[0]["estado"], "aplicada")


def prueba_muerte(punto):
    def probar(self):
        self.muerte_decision(punto)
    return probar


for punto in ("decision-guardada", "decision-observada", "decision-reducida", "paso-guardado"):
    setattr(JuicioTests, "test_muerte_" + punto.replace("-", "_"), prueba_muerte(punto))


if __name__ == "__main__":
    unittest.main()
