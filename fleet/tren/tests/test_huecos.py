"""El CLI registra las órdenes y el tick gobierna la caducidad y el push."""
from dataclasses import replace
import json
import shlex
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import test_activo as activo
from dominio import Admitido, Certificado, Corriendo, Decision, Hueco, Pausa, Promoviendo, Terminal, datos
from test_u18 import AHORA
from test_u18_injerto import ContextoInjerto

REPO, cli = activo.REPO, activo.cli


class HuecosTests(unittest.TestCase):
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

    def ejecutar_cli(self, comando, ahora, *opciones):
        args = cli.parser().parse_args([comando, "--repo", REPO, *opciones])
        return cli.ejecutar(args, ahora=ahora)

    def orden(self, comando, ahora, *opciones):
        codigo, cuerpo = self.ejecutar_cli(comando, ahora, *opciones)
        self.assertEqual(codigo, 0, cuerpo)
        return cuerpo

    def vencimientos(self, tipo):
        with self.ledger() as ledger:
            return [(instante, json.loads(payload)) for instante, payload in ledger.db.execute(
                "SELECT instante, payload FROM events WHERE repo=? AND tipo=?", (REPO, tipo))]

    def test_suite_en_curso_certifica_y_el_hueco_retiene_hasta_el_vencimiento(self):
        (self.raiz / "suite-75").write_text("75")
        self.hasta_fase(Corriendo)
        reanudar = self.estado().trenes[0].fase.esperar_hasta
        self.orden("hueco", reanudar - 2, "--motivo", "merge externo")
        self.contexto.tick(reanudar - 1)
        self.contexto.tick(reanudar)
        self.assertIsInstance(self.estado().trenes[0].fase, Corriendo)
        self.assertEqual(len(self.lineas("trabajos.log")), 2)
        self.contexto.tick(reanudar + 1)
        cert = self.estado().trenes[0].fase
        self.assertIsInstance(cert, Certificado)
        hasta = reanudar - 1 + 3600
        self.contexto.tick(hasta - 1)
        estado = self.orden("estado", hasta - 1, "--juicio")
        self.assertEqual(estado["estado"]["hueco"]["hasta"], hasta)
        self.assertEqual(estado["juicio"]["decisiones"][0]["estado"], "aplicada")
        self.assertEqual(self.estado().trenes[0].fase, cert)
        self.assertEqual(self.remoto(), self.B)
        self.contexto.tick(hasta)
        self.assertIsNone(self.estado().hueco)
        self.assertEqual(self.remoto(), cert.C)
        self.contexto.tick(hasta + 1)
        self.assertEqual(self.estado().trenes[0].fase, Terminal("ATERRIZADO"))
        self.assertEqual(self.vencimientos("hueco-vencido"),
                         [(hasta, {"motivo": "merge externo", "hasta": hasta})])

    def test_hueco_retiene_el_push_ya_en_outbox_y_cerrar_lo_libera(self):
        self.hasta_fase(Certificado)
        cert = self.estado().trenes[0].fase

        def detener(nombre, efecto):
            if nombre == "paso-guardado":
                raise RuntimeError("interrupción")

        with self.assertRaisesRegex(RuntimeError, "interrupción"):
            self.contexto.tick(2000, detener)
        with self.ledger() as ledger:
            self.assertEqual(ledger.outbox(REPO)[0]["estado"], "pendiente")
        self.orden("hueco", 2001, "--motivo", "preparar pool")
        self.contexto.tick(2002)
        self.contexto.tick(2003)
        self.assertIsInstance(self.estado().trenes[0].fase, Promoviendo)
        self.assertEqual(self.remoto(), self.B)
        self.orden("hueco", 2004, "--cerrar", "--motivo", "pool listo")
        self.contexto.tick(2005)
        self.assertIsNone(self.estado().hueco)
        self.assertEqual(self.remoto(), cert.C)
        self.assertEqual(self.vencimientos("hueco-vencido"), [])
        with self.ledger() as ledger:
            self.assertEqual([(d["decision"]["decision"], d["estado"])
                              for d in ledger.decisiones(REPO)],
                             [("abrir-hueco", "aplicada"), ("cerrar-hueco", "aplicada")])

    def test_pausa_de_30_minutos_vence_con_el_motivo_original(self):
        self.hasta_fase(Certificado)
        self.orden("pausa", 2000, "--minutos", "30", "--motivo", "mantenimiento")
        self.contexto.tick(2001)
        self.contexto.tick(3799)
        self.assertEqual(self.orden("estado", 3799)["estado"]["pausa"]["hasta"], 3801)
        self.assertEqual(self.remoto(), self.B)
        self.contexto.tick(3801)
        self.assertIsNone(self.estado().pausa)
        self.assertNotEqual(self.remoto(), self.B)
        self.contexto.tick(3802)
        self.assertEqual(self.vencimientos("pausa-manual-vencida"),
                         [(3801, {"motivo": "mantenimiento", "hasta": 3801})])

    def test_tick_muerto_durante_el_push_reintenta_al_vencer_el_plazo(self):
        cert = self.hasta_fase(Certificado).trenes[0].fase
        with self.hijo("from test_activo import Contexto\n"
                       "Contexto(sys.argv[3], 'antes-push').tick(2000)") as hijo:
            _, error = hijo.communicate(timeout=45)
            self.assertEqual(hijo.returncode, 77, error)
        self.contexto.tick(2200)
        self.contexto.tick(2599)
        self.assertEqual(self.remoto(), self.B)
        self.assertEqual(self.pushes_main(), [])
        self.assertEqual(self.ejecutar_cli("hueco", 2599, "--motivo", "reserva"),
                         (2, {"error": "push-a-main-en-curso"}))
        self.contexto.tick(2600)
        self.assertEqual(self.remoto(), cert.C)
        self.assertEqual(self.pushes_main(), [cert.C + ":refs/heads/main"])
        self.assertEqual(self.estado().trenes[0].fase, Terminal("ATERRIZADO"))

    def test_timeout_del_push_rechaza_el_hueco_y_acredita_sin_segundo_push(self):
        cert = self.hasta_fase(Certificado).trenes[0].fase
        recibido, liberar = self.raiz / "push-recibido", self.raiz / "liberar-push"
        hook = self.origen / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\n"
            f"touch {shlex.quote(str(recibido))}\n"
            f"while [ ! -e {shlex.quote(str(liberar))} ]; do sleep .05; done\n")
        hook.chmod(0o755)
        git = self.contexto.git
        transporte = git.ejecutar
        push_en_remoto = None

        def timeout(argv, **kwargs):
            nonlocal push_en_remoto
            if "push" not in argv or not argv[-1].endswith(":refs/heads/main"):
                return transporte(argv, **kwargs)
            if push_en_remoto is not None:
                # El rechazo de un reintento no resuelve el primer push.
                return subprocess.CompletedProcess(argv, 1,
                    "!\t" + argv[-1] + "\t[remote rejected] (otro push activo)\n", "")
            push_en_remoto = subprocess.Popen(argv, env=kwargs["env"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            limite = time.monotonic() + 20
            while not recibido.exists() and time.monotonic() < limite:
                time.sleep(.05)
            self.assertTrue(recibido.exists(), "el remoto debe haber recibido el push")
            raise subprocess.TimeoutExpired(argv, 120)

        try:
            with patch.object(git, "ejecutar", side_effect=timeout) as transporte_git:
                self.contexto.tick(2000)
                self.assertEqual(self.remoto(), self.B)
                self.assertEqual(self.ejecutar_cli("hueco", 2001, "--motivo", "reserva"),
                                 (2, {"error": "push-a-main-en-curso"}))
                with self.ledger() as ledger:
                    self.assertEqual(ledger.outbox(REPO)[0]["estado"], "ejecutando")
                    self.assertEqual(ledger.decisiones(REPO), [])
                    ledger.registrar_decision(Decision(
                        "pendiente-antes-de-instalar", REPO, None, "abrir-hueco", None,
                        "reserva", 2001, "", None))
                self.contexto.tick(2002)
                self.assertIsNone(self.estado().hueco)
                with self.ledger() as ledger:
                    rechazada = ledger.decisiones(REPO)[0]
                    self.assertEqual(rechazada["estado"], "rechazada")
                    self.assertEqual(rechazada["resultado"]["motivo"], "push-a-main-en-curso")
                    self.assertEqual(ledger.outbox(REPO)[0]["estado"], "ejecutando")
        finally:
            liberar.touch()
            if push_en_remoto is not None:
                _, error = push_en_remoto.communicate(timeout=30)
        self.assertEqual(push_en_remoto.returncode, 0, error)
        self.assertEqual(self.remoto(), cert.C)
        with patch.object(git, "ejecutar", side_effect=timeout) as reconciliacion:
            self.contexto.tick(2003)
        intentos = [llamada.args[0] for llamada in transporte_git.call_args_list +
                    reconciliacion.call_args_list if "push" in llamada.args[0] and
                    llamada.args[0][-1].endswith(":refs/heads/main")]
        self.assertEqual(len(intentos), 1)
        self.assertEqual(self.estado().trenes[0].fase, Terminal("ATERRIZADO"))
        self.orden("hueco", 2004, "--motivo", "reserva")
        self.contexto.tick(2005)
        self.assertEqual(self.estado().hueco.hasta, 5605)

    def recuperar_ack_en_hueco(self, muerto_en="durante-promover"):
        cert = self.hasta_fase(Certificado).trenes[0].fase
        with self.hijo("from test_activo import Contexto\n"
                       "Contexto(sys.argv[3], sys.argv[4]).tick(2000)", muerto_en) as hijo:
            _, error = hijo.communicate(timeout=45)
            self.assertEqual(hijo.returncode, 77, error)
        pushes = self.pushes_main()
        with self.ledger() as ledger:
            self.assertEqual(ledger.outbox(REPO)[0]["estado"], "ejecutando")
            # Foto que podía quedar con la versión anterior.
            ledger.guardar_paso(replace(self.estado(), hueco=Hueco(2061, "mantenimiento")), (), 2001)
        self.contexto.tick(2002)
        self.assertEqual(self.pushes_main(), pushes)
        self.assertEqual(self.estado().hueco.hasta, 2061)
        return cert

    def test_recupera_ack_y_publica_aviso_durante_el_hueco(self):
        cert = self.recuperar_ack_en_hueco()
        self.assertEqual(self.remoto(), cert.C)
        self.assertEqual(self.estado().trenes[0].fase, Terminal("ATERRIZADO"))
        self.assertEqual(len(self.pushes_main()), 1)
        posts = [json.loads(linea)["payload"] for linea in self.lineas("posts.log")]
        self.assertEqual(sum(p.get("state") == "success" for p in posts), len(self.miembros))

    def test_commit_externo_durante_un_hueco_no_revierte_el_aterrizaje(self):
        cert = self.recuperar_ack_en_hueco()
        self.comando(self.fuente, "fetch", str(self.origen), cert.C)
        externo = self.commit({"externo": "siguiente cambio\n"}, (cert.C,))
        self.push(externo, "refs/heads/main")
        self.contexto.tick(2004)
        self.contexto.tick(2061)
        estado = self.estado()
        self.assertEqual(estado.trenes[0].fase, Terminal("ATERRIZADO"))
        self.assertIsNone(estado.pausa)
        self.assertTrue(all(isinstance(e.admision, Admitido) for e in estado.entradas))
        self.assertEqual(self.remoto(), externo)

    def test_en_base_con_ack_incierto_no_empuja_durante_el_hueco(self):
        cert = self.recuperar_ack_en_hueco("antes-push")
        self.assertEqual(self.remoto(), self.B)
        self.assertIsInstance(self.estado().trenes[0].fase, Promoviendo)
        with self.ledger() as ledger:
            self.assertEqual(ledger.outbox(REPO)[0]["estado"], "ejecutando")
        self.contexto.tick(2061)
        self.contexto.tick(2062)
        self.assertEqual(self.remoto(), self.B)
        self.contexto.tick(2600)
        self.assertEqual(self.remoto(), cert.C)
        self.assertEqual(self.estado().trenes[0].fase, Terminal("ATERRIZADO"))

    def test_levantar_registrado_a_tiempo_se_aplica_despues_del_vencimiento(self):
        self.orden("pausa", 2000, "--minutos", "1", "--motivo", "mantenimiento")
        self.contexto.tick(2001)
        self.orden("pausa", 2059, "--levantar", "--motivo", "terminado")
        self.contexto.tick(2062)
        self.assertIsNone(self.estado().pausa)
        self.assertEqual(self.vencimientos("pausa-manual-vencida"), [])
        with self.ledger() as ledger:
            self.assertEqual(ledger.decisiones(REPO)[-1]["estado"], "aplicada")

    def test_minutos_cuentan_desde_el_tick_que_aplica_la_orden(self):
        for comando, ahora in (("pausa", 2000), ("hueco", 2300)):
            with self.subTest(comando=comando):
                self.orden(comando, ahora, "--minutos", "1", "--motivo", "mantenimiento")
                self.contexto.tick(ahora + 120)
                self.assertEqual(getattr(self.estado(), comando).hasta, ahora + 180)
                self.contexto.tick(ahora + 180)
                self.assertIsNone(getattr(self.estado(), comando))

    def test_minutos_cuentan_desde_la_aplicacion_aunque_el_tick_tarde(self):
        self.orden("pausa", 2000, "--minutos", "1", "--motivo", "mantenimiento")
        ahora = 2120

        def demora(nombre, efecto):
            nonlocal ahora
            if nombre == "decision-reducida":
                ahora += 120

        with patch("time.time", side_effect=lambda: ahora):
            codigo, cuerpo = self.contexto.tick(None, demora)
        self.assertEqual(codigo, 0, cuerpo)
        self.assertEqual(self.estado().pausa.hasta, 2300)

    def test_tick_seco_no_vence_la_pausa_y_el_cli_rechaza_levantar_fuera_de_plazo(self):
        self.orden("pausa", 2000, "--minutos", "1", "--motivo", "mantenimiento")
        self.contexto.tick(2001)
        consulta = ("SELECT count(*) FROM events "
                    "WHERE tipo IN ('operativo', 'pausa-manual-vencida')")
        with self.ledger() as ledger:
            antes = ledger.db.execute(consulta).fetchone()[0]
        args = cli.parser().parse_args(["tick", "--repo", REPO, "--seco"])
        codigo, cuerpo = cli.ejecutar(args, self.contexto.observador, 2061)
        self.assertEqual(codigo, 0, cuerpo)
        self.assertEqual(self.estado().pausa.hasta, 2061)
        self.assertEqual(self.ejecutar_cli("pausa", 2061, "--levantar", "--motivo", "tarde"),
                         (2, {"error": "pausa-no-vigente"}))
        with self.ledger() as ledger:
            self.assertEqual(ledger.db.execute(consulta).fetchone()[0], antes)

    def pushes_main(self):
        return [p for p in self.lineas("pushes.log") if p.endswith(":refs/heads/main")]

    def test_reintento_pendiente_queda_retenido_sin_gastar_intento(self):
        self.hasta_fase(Certificado)
        rechazo = self.raiz / "rechazar-push"
        rechazo.write_text("")
        self.contexto.tick(3000)
        fase = self.estado().trenes[0].fase
        self.assertIsInstance(fase, Promoviendo)
        intentos, pushes = fase.intentos, self.pushes_main()
        rechazo.unlink()
        self.orden("hueco", 3001, "--minutos", "10", "--motivo", "sonda")
        for ahora in range(3002, 3006):
            self.contexto.tick(ahora)
        fase = self.estado().trenes[0].fase
        self.assertEqual(self.pushes_main(), pushes)
        self.assertEqual(self.remoto(), self.B)
        self.assertEqual((fase.intentos, fase.reintento_pendiente), (intentos, True))
        for ahora in range(3602, 3606):
            self.contexto.tick(ahora)
        self.assertEqual(self.estado().trenes[0].fase, Terminal("ATERRIZADO"))

    def test_levantar_anterior_a_la_instalacion_conserva_su_identidad(self):
        pausa = Pausa("manual", detalle="versión anterior")
        with self.ledger() as ledger:
            ledger.guardar_paso(replace(self.estado(), pausa=pausa), (), 100)
            decision = Decision("legado", REPO, None, "levantar", None, "listo", 110,
                                ledger.pausa_id(REPO), pausa)
            self.assertNotIn("hasta", datos(decision)["pausa"])
            self.assertNotIn("minutos", datos(decision))
            ledger.registrar_decision(decision)
        self.contexto.tick(200)
        self.assertIsNone(self.estado().pausa)
        with self.ledger() as ledger:
            self.assertEqual(ledger.decisiones(REPO)[0]["estado"], "aplicada")

    def test_snapshot_viejo_recibe_dos_horas_desde_el_primer_tick(self):
        pausa = {"_tipo": "Pausa", "motivo": "manual", "detalle": "pausa anterior"}
        foto = {**datos(self.estado()), "pausa": pausa}
        self.assertNotIn("hueco", foto)
        with self.ledger() as ledger:
            ledger.evento(REPO, "operativo", 100, {"estado": foto, "efectos": [], "consumido": None})
            identidad = ledger.pausa_id(REPO)
        self.assertIsNone(self.estado().pausa.hasta)
        self.assertIsNone(self.estado().hueco)
        self.contexto.tick(200)
        self.contexto.tick(7399)
        with self.ledger() as ledger:
            self.assertEqual(ledger.pausa_id(REPO), identidad)
        self.assertEqual(self.estado().pausa.hasta, 7400)
        self.contexto.tick(7400)
        self.assertIsNone(self.estado().pausa)
        self.assertEqual(self.vencimientos("pausa-manual-vencida"),
                         [(7400, {"motivo": "pausa anterior", "hasta": 7400})])

    def test_pausas_automaticas_no_caducan(self):
        for motivo in ("infraestructura", "necesita-juicio"):
            with self.subTest(motivo=motivo):
                pausa = Pausa(motivo, detalle="ejemplo")
                with self.ledger() as ledger:
                    ledger.guardar_paso(replace(self.estado(), pausa=pausa), (), 100)
                self.contexto.tick(10000)
                self.assertEqual(self.estado().pausa, pausa)
        self.assertEqual(self.vencimientos("pausa-manual-vencida"), [])

    def test_121_minutos_se_rechaza_en_el_cli(self):
        for comando in ("pausa", "hueco"):
            with self.subTest(comando=comando):
                proc = subprocess.run([sys.executable, "-I", str(activo.RAIZ / "pao-tren"),
                                       comando, "--repo", REPO, "--motivo", "prueba",
                                       "--minutos", "121"],
                                      capture_output=True, text=True, timeout=30)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("minutos debe estar entre 1 y 120", proc.stderr)
        with self.ledger() as ledger:
            self.assertEqual(ledger.decisiones(REPO), [])


class GanchoConHuecoTests(ContextoInjerto):
    def test_vence_el_hueco_y_el_gancho_vuelve_a_verificar_sin_otra_orden(self):
        self.ahora = self.hora_tick = AHORA
        self.config["pre_aterrizaje"]["plazo_s"] = 10
        self.preparar()
        cert = self.estado().trenes[0].fase
        self.tick()
        self.assertEqual([l["accion"] for l in self.llamadas()], ["verificar"])
        self.ahora = self.hora_tick = AHORA + 1
        args = cli.parser().parse_args(
            ["hueco", "--repo", REPO, "--motivo", "reserva de promoción"])
        codigo, cuerpo = cli.ejecutar(args, ahora=self.ahora)
        self.assertEqual(codigo, 0, cuerpo)
        self.tick()
        self.ahora = self.hora_tick = AHORA + 2
        self.modo("seguir")
        self.tick()
        self.assertEqual(self.diario()[0][1], 1)
        self.assertEqual(self.remoto(), self.B)
        self.assertEqual([l["accion"] for l in self.llamadas()], ["verificar"])
        self.ahora = self.hora_tick = AHORA + 3601
        self.hasta(lambda: self.estado().trenes[0].fase == Terminal("ATERRIZADO"))
        self.assertEqual(self.remoto(), cert.C)
        self.assertIsNone(self.estado().pausa)
        self.assertEqual([l["accion"] for l in self.llamadas()], ["verificar", "verificar"])
