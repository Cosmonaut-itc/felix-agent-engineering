"""SEN-7115: tick, Git local y launcher falso, sólo en el host de pruebas."""
from collections import Counter
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import unittest

import test_activo as activo
import test_juicio as juicios
from dominio import Candidato, Certificado, Diagnosticando, Pausa, Promoviendo, Terminal, datos
from test_hasito import F, repeticion_fixture, suite_fixture


REPO = activo.REPO
EVENTO = "intermitente-absuelto"
DIA = 86400
AHORA = 20 * DIA
PASA = ("pasa", "pasa")
FALLA = ("falla", "falla")
MIXTO = ("pasa", "falla")
UNIDAD = "tests/test_intermitente.py"
OTRA = "tests/test_vecina.py"
TERCERA = "tests/test_tercera.py"


class IntermitentesTests(unittest.TestCase):
    setUp = activo.ActivoTests.setUp
    comando = activo.ActivoTests.comando
    commit = activo.ActivoTests.commit
    push = activo.ActivoTests.push
    remoto = activo.ActivoTests.remoto
    ledger = activo.ActivoTests.ledger
    estado = activo.ActivoTests.estado
    cli = juicios.JuicioTests.cli
    juicio = juicios.JuicioTests.juicio

    def preparar(self, repeticiones, *, restantes=(), cambio=None, firma_base=None):
        self.contexto.config["costo_suite_sana_s"] = 600
        trabajos = Counter()
        gh = self.contexto.observador.ejecutar

        def github(argv, **kwargs):
            if cambio and argv[0] == "gh" and "/pulls/2/files?" in argv[4]:
                return subprocess.CompletedProcess(argv, 0, json.dumps([[cambio]]), "")
            return gh(argv, **kwargs)

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

    def absuelve(self, c, b, caso, otras=()):
        trabajos = self.preparar({UNIDAD: (c, b), **dict(otras)})
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

    def test_caso_1_pasa_pasa_en_c_y_b(self):
        self.absuelve(PASA, PASA, 1)

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

    def pausa_original(self, repeticiones, detalle="confirmacion-candidato", **opciones):
        trabajos = self.preparar(repeticiones, **opciones)
        estado = self.avanzar(lambda fase: isinstance(fase, (Certificado, Terminal)))
        self.assertEqual(estado.pausa, Pausa("necesita-juicio", detalle=f"{detalle}:{next(iter(repeticiones))}"))
        self.assertEqual(trabajos["suite"], 1)
        self.assertEqual(self.remoto(), self.B)
        self.assertEqual(self.eventos(), [])

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

    def test_pausa_si_un_pr_toca_el_archivo_de_la_unidad(self):
        self.pausa_original({UNIDAD + "::test_uno": (PASA, PASA)},
                            cambio={"filename": UNIDAD})

    def test_pausa_si_un_pr_renombra_el_archivo_de_la_unidad(self):
        self.pausa_original({UNIDAD: (PASA, PASA)}, cambio={
            "filename": OTRA, "status": "renamed", "previous_filename": UNIDAD})

    def test_pausa_si_hay_un_fallo_atribuible_junto_al_intermitente(self):
        self.pausa_original({UNIDAD: (PASA, PASA), OTRA: (FALLA, PASA)})

    def test_snapshot_anterior_se_sigue_leyendo(self):
        self.preparar({UNIDAD: (PASA, PASA)})
        guardado = self.avanzar(lambda fase: isinstance(fase, Diagnosticando))
        with self.ledger() as ledger:
            # Formato operativo anterior a SEN-7115: sólo estado, efectos y ACK.
            ledger.evento(REPO, "operativo", AHORA + 2800,
                          {"estado": datos(guardado), "efectos": [], "consumido": None})
        self.assertEqual(self.estado(), guardado)
