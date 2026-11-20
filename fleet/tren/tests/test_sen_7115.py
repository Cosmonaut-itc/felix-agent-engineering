"""SEN-7115: tick, Git local y launcher falso, sólo en el host de pruebas."""
from collections import Counter
from dataclasses import replace
import json
from pathlib import Path
import shlex
import subprocess
import sys
import unittest

import test_activo as activo
import test_juicio as juicios
from adaptadores.jale import Jale
from atribucion import Absolucion, Plan, planificar
from dominio import (Candidato, Certificado, Diagnosticando, Evidencia, Intento, Pausa,
                     Promoviendo, Repeticion, Reproduccion, Terminal, UnidadRepetida, datos)
from test_hasito import F, repeticion_fixture, suite_fixture
from test_jale import FIRMA as FIRMA_JALE, PRODUCTOR


REPO = activo.REPO
EVENTO = "intermitente-absuelto"
DIA = 86400
AHORA = 20 * DIA
PASA = ("pasa", "pasa")
FALLA = ("falla", "falla")
MIXTO = ("pasa", "falla")
ARCHIVO = "tests/test_intermitente.py"
UNIDAD = ARCHIVO + "::test_uno"
OTRA = "tests/test_vecina.py"
TERCERA = "tests/test_tercera.py"
RENOMBRADO = "tests/test_renombrado.py"


class TrenIntermitentes:
    def setUp(self):
        activo.ActivoTests.setUp(self, self.preparar_git)

    def preparar_git(self):
        self.B_pr = self.B
        self.B = self.commit({"comun": "base\n", ARCHIVO: "prueba original\n"},
                             padres=(self.B,), mensaje="main avanza con los PR abiertos")
        self.push(self.B, "refs/heads/main")

    comando = activo.ActivoTests.comando
    commit = activo.ActivoTests.commit
    push = activo.ActivoTests.push
    remoto = activo.ActivoTests.remoto
    ledger = activo.ActivoTests.ledger
    estado = activo.ActivoTests.estado
    cli = juicios.JuicioTests.cli
    juicio = juicios.JuicioTests.juicio

    def preparar(self, repeticiones, *, restantes=(), base_vieja=False, firma_base=None):
        self.contexto.config["costo_suite_sana_s"] = 600
        trabajos = Counter()
        gh = self.contexto.observador.ejecutar

        def github(argv, **kwargs):
            resultado = gh(argv, **kwargs)
            if base_vieja and argv[0] == "gh" and argv[4].rsplit("/", 1)[-1] in ("1", "2", "3"):
                payload = json.loads(resultado.stdout)
                payload["base"]["sha"] = self.B_pr
                resultado.stdout = json.dumps(payload)
            return resultado

        def lanzador(argv, **kwargs):
            es_repeticion = "--repetir" in argv
            tipo = "repeticion" if es_repeticion else "suite"
            trabajos[tipo] += 1
            evidencia = Path(argv[argv.index("--evidencia") + 1])
            evidencia.mkdir(parents=True, exist_ok=True)
            recibo = evidencia / "resultado.json"
            if es_repeticion:
                origen = Path(argv[argv.index("--repetir") + 1])
                commit = argv[argv.index("--sha") + 1]
                unidad = argv[argv.index("--unidad") + 1].split("=", 1)[1]
                candidato = Candidato(self.B, (), json.loads(origen.read_text())["sha"], "")
                valores = repeticiones[unidad][int(commit == self.B)]
                fallo = replace(F, unidad=unidad)
                resultado = repeticion_fixture(origen, commit, candidato, fallo, valores)
                if firma_base is not None and commit == self.B:
                    for intento in resultado["unidades"][0]["intentos"]:
                        if intento["veredicto"] == "falla":
                            intento["firma"] = firma_base
            else:
                candidato = Candidato(self.B, (), argv[argv.index("--suite") + 1], "")
                rojo = trabajos[tipo] == 1
                fallos = tuple(replace(F, unidad=u) for u in repeticiones) if rojo else ()
                resultado = suite_fixture(candidato, int(rojo), fallos, restantes if rojo else ())
            recibo.write_text(json.dumps(resultado))
            codigo = resultado["veredicto"]
            prefijo = "REPETICION" if es_repeticion else "SUITE"
            campo = "repeticion" if es_repeticion else "resultado"
            return subprocess.CompletedProcess(argv, codigo,
                f"{prefijo} veredicto={codigo} {campo}={recibo}\n", "")

        self.contexto.hasito.runner = lanzador
        self.contexto.observador.ejecutar = github
        return trabajos

    def avanzar(self, detener, inicio=AHORA):
        for ciclo in range(20):
            self.contexto.tick(inicio + ciclo * 120)
            estado = self.estado()
            if estado.pausa or (estado.trenes and detener(estado.trenes[-1].fase)):
                return estado
        self.fail("el tren no convergió: " + str(datos(estado)))

    def eventos(self):
        with self.ledger() as ledger:
            return [json.loads(fila[0]) for fila in ledger.db.execute(
                "SELECT payload FROM events WHERE repo=? AND tipo=? ORDER BY seq", (REPO, EVENTO))]

    def antecedente(self, hace_dias, unidad=UNIDAD, repo=REPO):
        with self.ledger() as ledger:
            ledger.evento(repo, EVENTO, AHORA - hace_dias * DIA,
                         {"repo": repo, "unidad": unidad, "tren": f"anterior-{hace_dias}",
                          "B": "b" * 40, "C": "a" * 40, "firma": F.firma, "caso": 1,
                          "intentos_C": PASA, "intentos_B": PASA})

    def absuelve_caso_2(self, otras=(), **opciones):
        trabajos = self.preparar({UNIDAD: (FALLA, MIXTO), **dict(otras)}, **opciones)
        certificado = self.avanzar(lambda fase: isinstance(fase, Certificado))
        self.assertIsNone(certificado.pausa)
        tren = certificado.trenes[-1]
        cert = tren.fase
        self.assertEqual(cert.B, self.B)
        self.assertEqual(json.loads(Path(cert.suite).read_text())["sha"], cert.C)
        self.assertEqual(self.eventos()[-1], {
            "repo": REPO, "tren": tren.id, "B": self.B, "C": cert.C, "unidad": UNIDAD,
            "firma": F.firma, "caso": 2, "intentos_C": list(FALLA), "intentos_B": list(MIXTO)})
        promoviendo = self.avanzar(lambda fase: isinstance(fase, Promoviendo), AHORA + 3000)
        self.assertIsNone(promoviendo.pausa)
        self.assertEqual(promoviendo.trenes[-1].fase.certificado, cert)
        aterrizado = self.avanzar(lambda fase: isinstance(fase, Terminal), AHORA + 6000)
        self.assertIsNone(aterrizado.pausa)
        self.assertEqual(aterrizado.trenes[-1].fase, Terminal("ATERRIZADO"))
        self.assertEqual(self.remoto(), cert.C)
        self.assertEqual(trabajos, {"suite": 1, "repeticion": 2 * (1 + len(otras))})
        self.assertEqual(sum(e["tren"] == tren.id for e in self.eventos()), 1)

    def pausa_original(self, repeticiones, detalle="confirmacion-candidato", **opciones):
        trabajos = self.preparar(repeticiones, **opciones)
        estado = self.avanzar(lambda fase: isinstance(fase, (Certificado, Terminal)))
        self.assertEqual(estado.pausa, Pausa("necesita-juicio", detalle=f"{detalle}:{next(iter(repeticiones))}"))
        self.assertIsInstance(estado.trenes[-1].fase, Diagnosticando)
        self.assertEqual(trabajos["suite"], 1)
        self.assertEqual(self.remoto(), self.B)
        self.assertEqual(self.eventos(), [])


class IntermitentesTests(TrenIntermitentes, unittest.TestCase):
    def test_caso_1_pausa_con_ambos_commits_en_verde(self):
        self.pausa_original({UNIDAD: (PASA, PASA)})

    def test_caso_2_certifica_si_github_conserva_la_base_anterior(self):
        self.absuelve_caso_2(base_vieja=True)

    def test_caso_1_pausa_con_base_mixta(self):
        self.pausa_original({UNIDAD: (PASA, MIXTO)})

    def test_caso_2_con_base_mixta_misma_firma(self):
        self.absuelve_caso_2()

    def test_intermitente_y_preexistente_certifican_juntos(self):
        self.absuelve_caso_2(((OTRA, (FALLA, FALLA)),))

    def test_absolucion_de_hace_ocho_dias_y_otro_repo_no_cuentan(self):
        self.antecedente(1)
        self.antecedente(8)
        self.antecedente(2, repo="otro/proyecto")
        self.antecedente(2, unidad=OTRA)
        self.absuelve_caso_2()

    def test_tercera_vez_pausa_y_absolver_manual_corre_suite_nueva(self):
        self.antecedente(1)
        self.antecedente(6)
        trabajos = self.preparar({UNIDAD: (FALLA, MIXTO)})
        pausado = self.avanzar(lambda fase: False)
        self.assertEqual(pausado.pausa, Pausa("necesita-juicio",
                         detalle="intermitente-reincidente:" + UNIDAD))
        self.assertEqual(len(self.eventos()), 2)
        self.assertEqual(trabajos, {"suite": 1, "repeticion": 2})
        candidato = pausado.trenes[-1].fase.candidato
        self.juicio("absolver")
        final = self.avanzar(lambda fase: fase == Terminal("ATERRIZADO"), AHORA + 3000)
        self.assertIsNone(final.pausa)
        self.assertEqual(self.remoto(), candidato.C)
        self.assertEqual(trabajos, {"suite": 2, "repeticion": 2})
        self.assertEqual(len(self.eventos()), 2)

    def test_pausa_si_b_falla_con_distinta_firma(self):
        self.pausa_original({UNIDAD: (FALLA, MIXTO)}, "confirmacion-base", firma_base="otra")

    def test_pausa_si_c_es_mixto(self):
        self.pausa_original({UNIDAD: (MIXTO, PASA)})

    def test_pausa_si_c_tiene_un_intento_ausente(self):
        self.pausa_original({UNIDAD: (("pasa", "ausente"), PASA)})

    def test_pausa_si_c_tiene_un_intento_omitido(self):
        self.pausa_original({UNIDAD: (("pasa", "omitida"), PASA)})

    def test_pausa_si_quedan_etapas(self):
        self.pausa_original({UNIDAD: (FALLA, MIXTO)}, "confirmacion-base", restantes=("no-corrida",))

    def test_pausa_si_hay_tres_unidades(self):
        self.pausa_original({u: (FALLA, MIXTO) for u in (UNIDAD, OTRA, TERCERA)}, "confirmacion-base")

    def test_pausa_si_hay_un_fallo_atribuible_junto_al_intermitente(self):
        self.pausa_original({UNIDAD: (FALLA, MIXTO), OTRA: (FALLA, PASA)}, "confirmacion-base")

    def test_pausa_si_git_no_puede_leer_el_diff(self):
        transporte = self.contexto.git.ejecutar

        def git(argv, **kwargs):
            if "diff" in argv and "--name-only" in argv:
                return subprocess.CompletedProcess(argv, 128, b"", b"error de lectura")
            return transporte(argv, **kwargs)

        self.contexto.git.ejecutar = git
        self.pausa_original({UNIDAD: (FALLA, MIXTO)}, "confirmacion-base")

    def test_no_hay_inventario_si_falta_un_eslabon_de_la_cadena(self):
        self.preparar({UNIDAD: (FALLA, MIXTO)})
        estado = self.avanzar(lambda fase: isinstance(fase, Diagnosticando))
        tren = estado.trenes[-1]
        fase = tren.fase
        primero, _, ultimo = fase.candidato.eslabones
        incompleto = replace(fase.candidato, eslabones=(primero, ultimo))
        tren_incompleto = replace(tren, fase=replace(fase, candidato=incompleto))
        estado_incompleto = replace(estado, trenes=estado.trenes[:-1] + (tren_incompleto,))

        observacion = self.contexto.observador.observar(estado_incompleto, AHORA)
        self.assertIsNone(observacion.archivos_lote)

    def test_snapshot_anterior_se_sigue_leyendo(self):
        self.preparar({UNIDAD: (PASA, PASA)})
        guardado = self.avanzar(lambda fase: isinstance(fase, Diagnosticando) and fase.evidencias)
        anterior = datos(guardado)
        evidencias = anterior["trenes"][-1]["fase"]["evidencias"]
        for evidencia in evidencias:
            for unidad in evidencia["resultado"]["unidades"]:
                for intento in unidad["intentos"]:
                    self.assertIs(intento.pop("caso_ejecutado"), False)
        with self.ledger() as ledger:
            # Formato operativo anterior a SEN-7115: sólo estado, efectos y ACK.
            ledger.evento(REPO, "operativo", AHORA + 2800,
                          {"estado": anterior, "efectos": [], "consumido": None})
        self.assertEqual(self.estado(), guardado)


class ArchivosDelLoteTests(TrenIntermitentes, unittest.TestCase):
    def preparar_git(self):
        self.B = self.commit({"comun": "base\n", ARCHIVO: "prueba original\n",
                              RENOMBRADO: "prueba que cambia de nombre\n"}, padres=(self.B,))
        self.push(self.B, "refs/heads/main")
        pr, previo = self.miembros[1]
        head = self.commit({
            "comun": "base\n", f"pr-{pr}": "ticket\n", ARCHIVO: "prueba editada\n",
            OTRA: "prueba que cambia de nombre\n",
            "packages/mcp-server/tests/auth.vitest.ts": "prueba de autenticación\n",
        }, padres=(previo, self.B))
        self.push(head, f"refs/pull/{pr}/head")
        self.miembros[1] = pr, head

    def test_pausa_si_un_pr_toca_el_archivo_de_la_unidad(self):
        self.pausa_original({UNIDAD: (FALLA, MIXTO)}, "confirmacion-base")

    def test_pausa_si_un_pr_renombra_el_archivo_de_la_unidad(self):
        self.pausa_original({RENOMBRADO + "::test_uno": (FALLA, MIXTO)}, "confirmacion-base")

    def test_pausa_si_el_lote_toca_la_ruta_relativa_al_paquete(self):
        self.pausa_original({"tests/auth.vitest.ts": (FALLA, MIXTO)}, "confirmacion-base")


class ArchivoRestauradoEnLoteTests(TrenIntermitentes, unittest.TestCase):
    def preparar_git(self):
        super().preparar_git()
        prueba = "prueba original\n"
        cambios = {"comun": "base\n", "src/nomina.py": "cálculo cambiado\n"}
        primero = self.commit({**cambios, OTRA: prueba}, padres=(self.B,))
        segundo = self.commit({**cambios, ARCHIVO: prueba, "pr-2": "ticket\n"},
                              padres=(primero,))
        for pr, head in ((1, primero), (2, segundo)):
            self.comando(self.fuente, "push", str(self.origen), f"+{head}:refs/pull/{pr}/head")
            self.miembros[pr - 1] = pr, head

    def test_pausa_si_un_pr_restaura_el_archivo_renombrado_por_otro(self):
        config = self.contexto.config
        config["sensibles"]["pruebas"] = [ARCHIVO]
        config["sensibles_en_lote"] = ["pruebas"]
        config["alto_riesgo_en_lote"] = True
        config["heads_autorizados"] = [head for _, head in self.miembros]
        apto = self.raiz / "apto-1.json"
        evidencia = json.loads(apto.read_text())
        apto.write_text(json.dumps({**evidencia, "verificado": "Verificado"}))

        gh = self.contexto.observador.ejecutar
        archivos_por_pr = {
            "1": [{"filename": OTRA, "previous_filename": ARCHIVO, "status": "renamed"},
                  {"filename": "src/nomina.py"}],
            "2": [{"filename": "src/nomina.py"}, {"filename": "pr-2"}],
            "3": [{"filename": "pr-3"}],
        }

        def github(argv, **kwargs):
            resultado = gh(argv, **kwargs)
            if argv[0] != "gh":
                return resultado
            ruta = argv[4].split("/")
            if ruta[3] != "pulls":
                return resultado
            pr = ruta[4]
            archivos = archivos_por_pr[pr]
            if len(ruta) == 5:
                datos_pr = json.loads(resultado.stdout)
                datos_pr["changed_files"] = len(archivos)
                if pr == "1":
                    datos_pr["labels"] = [{"name": "alto-riesgo"}]
                resultado.stdout = json.dumps(datos_pr)
            elif ruta[5].startswith("files?"):
                resultado.stdout = json.dumps([archivos])
            return resultado

        self.contexto.observador.ejecutar = github
        trabajos = self.preparar({UNIDAD: (FALLA, MIXTO)})
        diagnostico = self.avanzar(lambda fase: isinstance(fase, Diagnosticando))
        self.assertIsNone(diagnostico.pausa)
        candidato = diagnostico.trenes[-1].fase.candidato
        self.assertEqual([pr for pr, _, _ in candidato.eslabones], [1, 2, 3])
        git = self.contexto.git
        primero, segundo, _ = (commit for _, _, commit in candidato.eslabones)
        self.assertIn(ARCHIVO, git.archivos_entre(candidato.B, primero))
        self.assertIn(ARCHIVO, git.archivos_entre(primero, segundo))
        self.assertNotIn(ARCHIVO, git.archivos_entre(candidato.B, candidato.C))

        final = self.avanzar(lambda fase: isinstance(fase, Terminal), AHORA + 2400)
        self.assertIsInstance(final.trenes[-1].fase, Diagnosticando)
        self.assertEqual(final.pausa, Pausa("necesita-juicio", detalle="confirmacion-base:" + UNIDAD))
        self.assertEqual(self.eventos(), [])
        self.assertEqual(self.remoto(), candidato.B)
        self.assertEqual(trabajos, {"suite": 1, "repeticion": 2})


class PlanificarTests(unittest.TestCase):
    def test_caso_1_exige_que_ambos_intentos_de_c_acrediten_el_caso(self):
        b, c = "b" * 40, "c" * 40

        def evidencia(objetivo, *intentos):
            unidad = UnidadRepetida(F.check, F.unidad, F.parte, "definicion", intentos)
            solicitud = Reproduccion(objetivo, F, objetivo, costo_s=20)
            resultado = Repeticion(c if objetivo == "C" else b, c, b, "huella", (unidad,))
            return Evidencia(solicitud, resultado)

        c1 = Intento(1, "pasa", "", 3, caso_ejecutado=True)
        c2 = Intento(2, "pasa", "", 3, caso_ejecutado=True)
        base = evidencia("B", Intento(1, "pasa", "", 3), Intento(2, "falla", F.firma, 3))

        plan = planificar((F,), (1,), (evidencia("C", c1, c2), base), 600, absolver=True)
        self.assertEqual(plan, Plan(terminado=True, absoluciones=(Absolucion(F, 1, PASA, MIXTO),)))
        for n, intentos in enumerate(((replace(c1, caso_ejecutado=False), c2),
                                      (c1, replace(c2, caso_ejecutado=False))), 1):
            with self.subTest(sin_evidencia=n):
                plan = planificar((F,), (1,), (evidencia("C", *intentos), base), 600, absolver=True)
                self.assertEqual(plan, Plan(incierto="confirmacion-candidato:" + F.unidad))


class IntermitentesJaleTests(TrenIntermitentes, unittest.TestCase):
    unidad = "apps/web/e2e/a.spec.ts"

    def preparar_recibos(self, intentos_c):
        self.contexto.config["costo_suite_sana_s"] = 600
        productor = self.raiz / "productor.py"
        productor.write_text(PRODUCTOR)
        lanzador = self.raiz / "jale.sh"
        lanzador.write_text("#!/bin/bash\nexec " + shlex.join(
            [sys.executable, str(productor), str(self.raiz)]) + ' "$@"\n')
        (self.raiz / "plan.json").write_text(json.dumps([
            {"estados": [v]} for v in ("falla", *intentos_c, *MIXTO)
        ]))
        self.contexto.hasito = Jale(lanzador, modos=("e2e",), estado=self.raiz / "jale")

    def verificar_repeticiones(self, candidato):
        lanzamientos = (self.raiz / "lanzamientos.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(linea)[2:] for linea in lanzamientos], [
            ["e2e", candidato],
            ["e2e", candidato, self.unidad],
            ["e2e", candidato, self.unidad],
            ["e2e", self.B, self.unidad],
            ["e2e", self.B, self.unidad],
        ])

    def test_caso_1_pausa_con_base_mixta(self):
        self.preparar_recibos(PASA)
        estado = self.avanzar(lambda fase: isinstance(fase, Certificado))
        fase = estado.trenes[-1].fase
        self.assertIsInstance(fase, Diagnosticando)
        self.assertEqual(estado.pausa,
                         Pausa("necesita-juicio", detalle="confirmacion-candidato:" + self.unidad))
        self.assertEqual(self.eventos(), [])
        self.assertEqual(self.remoto(), self.B)
        self.verificar_repeticiones(fase.candidato.C)

    def test_caso_2_con_base_mixta_misma_firma(self):
        self.preparar_recibos(FALLA)
        estado = self.avanzar(lambda fase: isinstance(fase, Certificado))
        self.assertIsNone(estado.pausa)
        tren = estado.trenes[-1]
        self.assertIsInstance(tren.fase, Certificado)
        self.assertEqual(self.eventos(), [{
            "repo": REPO, "tren": tren.id, "B": self.B, "C": tren.fase.C, "unidad": self.unidad,
            "firma": FIRMA_JALE, "caso": 2, "intentos_C": list(FALLA), "intentos_B": list(MIXTO),
        }])
        self.verificar_repeticiones(tren.fase.C)
