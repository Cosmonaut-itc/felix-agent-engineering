"""SEN-7115: tick, Git local y launcher falso, sólo en el host de pruebas."""
import base64
from collections import Counter
from dataclasses import replace
import hashlib
from io import BytesIO
import json
from pathlib import Path
import subprocess
import tarfile
import unittest
from xml.etree import ElementTree

import test_activo as activo
import test_juicio as juicios
from adaptadores.jale import Jale, guardar, leer_recibo
from dominio import Candidato, Certificado, Diagnosticando, Pausa, Promoviendo, Terminal, datos
from test_hasito import F, repeticion_fixture, suite_fixture


REPO = activo.REPO
EVENTO = "intermitente-absuelto"
DIA = 86400
AHORA = 20 * DIA
PASA = ("pasa", "pasa")
PASA_EJECUTADA = ("passed", "passed")
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

    def preparar(self, repeticiones, *, restantes=(), base_vieja=False, firma_base=None,
                 publicar_evidencia=None):
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
            if publicar_evidencia:
                publicar_evidencia(recibo, resultado, es_repeticion)
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

    def absuelve(self, c, b, caso, otras=(), **opciones):
        trabajos = self.preparar({UNIDAD: (c, b), **dict(otras)}, **opciones)
        certificado = self.avanzar(lambda fase: isinstance(fase, Certificado))
        self.assertIsNone(certificado.pausa)
        tren = certificado.trenes[-1]
        cert = tren.fase
        self.assertEqual(cert.B, self.B)
        self.assertEqual(json.loads(Path(cert.suite).read_text())["sha"], cert.C)
        self.assertEqual(self.eventos()[-1], {
            "repo": REPO, "tren": tren.id, "B": self.B, "C": cert.C, "unidad": UNIDAD,
            "firma": F.firma, "caso": caso, "intentos_C": list(c), "intentos_B": list(b)})
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
        self.assertEqual(trabajos["suite"], 1)
        self.assertEqual(self.remoto(), self.B)
        self.assertEqual(self.eventos(), [])


class IntermitentesTests(TrenIntermitentes, unittest.TestCase):
    def test_caso_1_pasa_pasa_en_c_y_b(self):
        self.absuelve(PASA, PASA, 1)

    def test_caso_1_certifica_si_github_conserva_la_base_anterior(self):
        self.absuelve(PASA, PASA, 1, base_vieja=True)

    def test_caso_1_con_base_mixta(self):
        self.absuelve(PASA, MIXTO, 1)

    def test_caso_2_con_base_mixta_misma_firma(self):
        self.absuelve(FALLA, MIXTO, 2)

    def test_intermitente_y_preexistente_certifican_juntos(self):
        self.absuelve(PASA, PASA, 1, ((OTRA, (FALLA, FALLA)),))

    def test_absolucion_de_hace_ocho_dias_y_otro_repo_no_cuentan(self):
        self.antecedente(1)
        self.antecedente(8)
        self.antecedente(2, repo="otro/proyecto")
        self.antecedente(2, unidad=OTRA)
        self.absuelve(PASA, PASA, 1)

    def test_tercera_vez_pausa_y_absolver_manual_corre_suite_nueva(self):
        self.antecedente(1)
        self.antecedente(6)
        trabajos = self.preparar({UNIDAD: (PASA, PASA)})
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
        self.pausa_original({UNIDAD: (PASA, PASA)}, restantes=("no-corrida",))

    def test_pausa_si_hay_tres_unidades(self):
        self.pausa_original({u: (PASA, PASA) for u in (UNIDAD, OTRA, TERCERA)})

    def test_pausa_si_hay_un_fallo_atribuible_junto_al_intermitente(self):
        self.pausa_original({UNIDAD: (PASA, PASA), OTRA: (FALLA, PASA)})

    def test_pausa_si_git_no_puede_leer_el_diff(self):
        transporte = self.contexto.git.ejecutar

        def git(argv, **kwargs):
            if "diff" in argv and "--name-only" in argv:
                return subprocess.CompletedProcess(argv, 128, b"", b"error de lectura")
            return transporte(argv, **kwargs)

        self.contexto.git.ejecutar = git
        self.pausa_original({UNIDAD: (PASA, PASA)})

    def test_snapshot_anterior_se_sigue_leyendo(self):
        self.preparar({UNIDAD: (PASA, PASA)})
        guardado = self.avanzar(lambda fase: isinstance(fase, Diagnosticando))
        with self.ledger() as ledger:
            # Formato operativo anterior a SEN-7115: sólo estado, efectos y ACK.
            ledger.evento(REPO, "operativo", AHORA + 2800,
                          {"estado": datos(guardado), "efectos": [], "consumido": None})
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
        self.pausa_original({UNIDAD: (PASA, PASA)})

    def test_pausa_si_un_pr_renombra_el_archivo_de_la_unidad(self):
        self.pausa_original({RENOMBRADO + "::test_uno": (PASA, PASA)})

    def test_pausa_si_el_lote_toca_la_ruta_relativa_al_paquete(self):
        self.pausa_original({"tests/auth.vitest.ts": (FALLA, MIXTO)}, "confirmacion-base")


class ReportesHasitoTests(TrenIntermitentes, unittest.TestCase):
    def publicar(self, fallo, formato, estados):
        cwd = "packages/mcp-server"
        nombre = "/espejo/" + cwd + "/" + fallo.unidad

        def reporte(estado):
            casos = [(fallo.firma, estado), ("hermano que sí corre", "passed")]
            if formato == "vitest":
                return json.dumps(dict(testResults=[dict(name=nombre, assertionResults=[
                    dict(fullName=caso, status=veredicto) for caso, veredicto in casos if veredicto
                ])])).encode()
            suite = ElementTree.Element("testsuite", name=nombre)
            for caso, veredicto in casos:
                if veredicto:
                    nodo = ElementTree.SubElement(suite, "testcase", name=caso, file=nombre)
                    if veredicto != "passed":
                        ElementTree.SubElement(nodo, "failure" if veredicto == "failed" else "skipped")
            return ElementTree.tostring(suite)

        def publicar(ruta, recibo, repeticion):
            sha = recibo["sha"]
            huella = "a" * 64
            check = dict(id=fallo.check, cwd=cwd, unidades=[
                dict(formato=formato, archivo="{reporte}", reejecutar=dict(cwd=cwd))])
            plan = dict(huella=huella, sha=sha, destino="desk",
                        etapas=[dict(id=fallo.etapa, checks=[check])])
            motor = dict(sha=sha, base=self.B, origen=fallo.parte, huella_plan=huella)
            if repeticion:
                u = recibo["unidades"][0]
                u["cobertura"]["testigo"] = "vitest-json" if formato == "vitest" else "bun-junit"
                objetivo = dict(check=fallo.check, unidad=fallo.unidad, fuente=0, definicion=u["definicion"])
                plan["repeticion"] = dict(objetivos=[objetivo], origen=recibo["origen"])
                motor["repeticion"] = dict(objetivos=[objetivo], intentos=[
                    dict(i, check=fallo.check, unidad=fallo.unidad, fuente=0) for i in u["intentos"]])
                archivos = {f"observaciones/{fallo.check}/f0/{n}/reporte.json": reporte(estado)
                            for n, estado in enumerate(estados, 1)}
            else:
                recibo["partes"][0]["huella_plan"] = huella
                motor["etapas"] = [dict(id=fallo.etapa, checks=[
                    dict(id=fallo.check, corridas={"candidato": {}})])]
                archivos = {f"etapas/01-{fallo.etapa}/{fallo.check}/reporte.json": reporte("failed")}
            ruta.write_text(json.dumps(recibo))
            token = "b" * 32
            guardar(ruta.with_name("despacho.json"), dict(tareas=[
                dict(token=token, estado="recogida", planes=[plan], resultados=[motor])]))
            recogido = ruta.parent / "tareas" / token / "recogido" / ".suite"
            recogido.mkdir(parents=True)
            archivos["plan.json"] = json.dumps(plan).encode()
            with tarfile.open(recogido / f"evidencia-{fallo.parte}.tar", "w") as tar:
                for nombre, contenido in archivos.items():
                    miembro = tarfile.TarInfo(f"desk-{huella[:12]}/{nombre}")
                    miembro.size = len(contenido)
                    tar.addfile(miembro, BytesIO(contenido))

        return publicar

    def verificar_reportes(self, formato, estados, certifica):
        unidad = "tests/auth.vitest.ts" if formato == "vitest" else "tests/auth.test.ts"
        fallo = replace(F, unidad=unidad)
        publicar = self.publicar(fallo, formato, estados)
        self.preparar({unidad: (PASA, PASA)}, publicar_evidencia=publicar)
        estado = self.avanzar(lambda fase: isinstance(fase, Certificado))
        if certifica:
            self.assertIsInstance(estado.trenes[-1].fase, Certificado)
            self.assertEqual([e["unidad"] for e in self.eventos()], [unidad])
        else:
            self.assertEqual(estado.pausa, Pausa("necesita-juicio", detalle="confirmacion-candidato:" + unidad))
            self.assertEqual(self.eventos(), [])

    def test_certifica_si_vitest_ejecuta_ambas_veces_el_caso(self):
        self.verificar_reportes("vitest", PASA_EJECUTADA, True)

    def test_pausa_si_vitest_omite_el_caso_en_un_intento(self):
        self.verificar_reportes("vitest", ("passed", "skipped"), False)

    def test_pausa_si_vitest_deja_de_enumerar_el_caso(self):
        self.verificar_reportes("vitest", (None, "passed"), False)

    def test_certifica_si_bun_ejecuta_ambas_veces_el_caso(self):
        self.verificar_reportes("bun", PASA_EJECUTADA, True)

    def test_pausa_si_junit_marca_omitido_el_caso(self):
        self.verificar_reportes("bun", ("skipped", "skipped"), False)


class PlaywrightTests(TrenIntermitentes, unittest.TestCase):
    def test_pausa_si_playwright_omite_el_caso_que_fallo(self):
        unidad = "apps/web/e2e/disciplinary-measures.spec.ts"
        adaptador = Jale("/sin-lanzador", modos=("e2e",), estado=self.raiz / "jale")
        self.contexto.hasito = adaptador
        self.contexto.config["costo_suite_sana_s"] = 600
        omitidos = []

        def corrida(registro, commit, modo, filtros, intento, permitir_lanzar, tren_id):
            ruta = registro / str(intento)
            ruta.mkdir(parents=True, exist_ok=True)
            guardar(registro / "completo.json", intento)
            guardar(ruta / "solicitud.json", ["fixture"])
            estado = ("passed" if commit == self.B else "skipped" if filtros else "failed")
            if estado == "skipped":
                omitidos.append(registro)
            # El hermano pasa y el recibo jale-recibo@1 agrega ambos por archivo.
            specs = [
                dict(title=titulo, file="e2e/disciplinary-measures.spec.ts", tests=[
                    dict(projectName="chromium", status=veredicto, results=[dict(status=status)])])
                for titulo, veredicto, status in (
                    ("flujo de acta firmada",
                     {"passed": "expected", "failed": "unexpected", "skipped": "skipped"}[estado],
                     estado),
                    ("consulta de historial", "expected", "passed"))]
            fallos = [s for s in specs if s["tests"][0]["status"] == "unexpected"]
            firma = ("sha256:" + hashlib.sha256(b"flujo de acta firmada [chromium]\tExpected closed").hexdigest()
                     if fallos else "")
            codigo = int(bool(fallos))
            recibo = dict(esquema="jale-recibo@1", sha=commit, modo=modo, codigo=codigo,
                          alcance="parcial" if filtros else "completo", filtros=list(filtros),
                          completo=True, huella_entorno=dict(bun="1", node="24", playwright="1"),
                          unidades=[dict(unidad=unidad, resultado="falla" if fallos else "pasa",
                                         firma=firma, fallidas=len(fallos), duracion_s=3)])
            log = ruta / "corrida.log"
            log.write_bytes(b"RECIBO jale-recibo@1 " + base64.b64encode(json.dumps(recibo).encode()))
            return leer_recibo(log, codigo, commit, modo, filtros)

        adaptador.corrida = corrida
        estado = self.avanzar(lambda fase: isinstance(fase, Certificado))
        self.assertEqual(len(omitidos), 2)
        self.assertEqual(estado.pausa, Pausa("necesita-juicio", detalle="confirmacion-candidato:" + unidad))
        self.assertEqual(self.eventos(), [])
