"""U4: sólo fixtures, runner falso y orígenes git locales. No ejecutar en la MacBook."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import unittest

import test_aterrizaje as git_fixtures
from test_tren import Aislado
from adaptadores.hasito import (ErrorResultado, Hasito, ResultadoInvalido,
                                leer_repeticion, leer_suite)
from atribucion import planificar
from dominio import (Admitido, Candidato, Certificado, Completo, Congelado, Corriendo,
                     Diagnosticando, Efecto, Entrada, Entrega, EsperandoCI, Estado,
                     Evidencia, Fallo, Incompleto, Intento, Observacion, Repeticion,
                     Terminal, Tren, UnidadRepetida, VistaPR, clave, datos, desde_datos, paso)
from efectos import EfectoFallido, ejecutar

B, C, T = "b" * 40, "c" * 40, "d" * 40
F = Fallo("tests/test_producto.py::test_caso[a, b=1]", "AssertionError: valor", "solo-candidato",
          "prueba", "validar", "parte-1", 10)
CANDIDATO = Candidato(B, tuple((i, str(i) * 40, str(i + 3) * 40) for i in (1, 2, 3)), C, T)


def suite_fixture(candidato=CANDIDATO, codigo=1, fallos=(F,)):
    filas = [dict(unidad=f.unidad, check=f.check, etapa=f.etapa, parte=f.parte,
                  clase=f.clase, firma=f.firma, detalle=f.firma) for f in fallos]
    parte = dict(esquema="suite-resultado@1", sha=candidato.C, base=candidato.B,
        origen="parte-1", destino="destino-1", huella_plan="e" * 64,
        veredicto=codigo, etapas=[dict(id="validar", estado="producto" if codigo == 1 else "verde",
            checks=[dict(id="prueba", segundos=10, estado="producto" if codigo == 1 else "verde")])],
        solo_candidato=filas if codigo == 1 else [], tambien_base=filas if codigo == 0 else [],
        sospecha_arnes=[], restantes=[])
    return dict(esquema="suite-resultado@1", sha=candidato.C, base=candidato.B,
        veredicto=codigo, partes=[parte], solo_candidato=parte["solo_candidato"],
        tambien_base=parte["tambien_base"], sospecha_arnes=[], restantes=[], reanudar=None)


def repeticion_fixture(origen, commit, candidato=CANDIDATO, fallo=F, valores=("falla", "falla")):
    return dict(esquema="suite-repeticion@1", sha=commit,
        origen=dict(ruta=str(origen), sha256=hashlib.sha256(Path(origen).read_bytes()).hexdigest(),
                    sha=candidato.C, base=candidato.B), veces=2, nombre="diag-fixture", veredicto=
                    1 if "falla" in valores else 0, completa=True, reanudar=None,
        unidades=[dict(unidad=fallo.unidad, check=fallo.check, etapa=fallo.etapa, parte=fallo.parte,
            firma_origen=fallo.firma, definicion="f" * 64,
            cobertura=dict(pedidos=2, observados=2, testigo="junit", completa=True),
            intentos=[dict(n=n, veredicto=v, firma=fallo.firma if v == "falla" else "", duracion_s=3,
                parte=fallo.parte, destino="destino-1", huella_entorno="a" * 64, motivo="", ensayo=str(n) * 16)
                for n, v in enumerate(valores, 1)])])


class GitFalso:
    def __init__(self, raiz):
        self.raiz = raiz
        self.armados = []
        self.commits = {}

    def worktree(self, commit):
        return self.raiz / "worktrees" / commit

    def armar(self, base, miembros):
        miembros = tuple(miembros)
        self.armados.append(miembros)
        commit = clave("subconjunto", base, miembros)[:40]
        self.commits[commit] = tuple(pr for pr, _ in miembros)
        return Candidato(base, tuple((pr, head, commit) for pr, head in miembros), commit, T)


class RunnerFalso:
    """Ninguna rama alcanza run.sh, red ni Pueue."""
    def __init__(self, raiz, candidato=CANDIDATO):
        self.raiz = raiz
        self.candidato = candidato
        self.llamadas = []
        self.responder = lambda argv: suite_fixture(candidato, 0, ())

    def __call__(self, argv, **kwargs):
        self.llamadas.append(tuple(argv))
        r = self.responder(argv)
        rep = "--repetir" in argv
        carpeta = self.raiz / clave(argv[argv.index("--evidencia") + 1], rep)
        carpeta.mkdir(exist_ok=True)
        archivo = carpeta / ("repeticion.json" if rep else "resultado.json")
        if r["veredicto"] == 75:
            r["reanudar"] = shlex.join([a for a in argv if a != "--reanudar"] + ["--reanudar"])
        archivo.write_text(json.dumps(r), encoding="utf-8")
        etiqueta, campo = ("REPETICION", "repeticion") if rep else ("SUITE", "resultado")
        return subprocess.CompletedProcess(argv, r["veredicto"],
            "{} veredicto={} {}={}\n".format(etiqueta, r["veredicto"], campo, archivo), "")


class HasitoTests(Aislado):
    def setUp(self):
        super().setUp()
        self.git = GitFalso(self.raiz)
        self.runner = RunnerFalso(self.raiz)
        self.adaptador = Hasito(self.raiz / "run.sh", self.git, self.runner)
        entradas = tuple(Entrada(Entrega("example/project", pr, head, "apto", "dirigida"), pr,
                                 Admitido(), pr, tren="lote")
                         for pr, head, _ in CANDIDATO.eslabones)
        self.estado = Estado("example/project", entradas,
            (Tren("lote", tuple(e.entrega.recibo for e in entradas), EsperandoCI(CANDIDATO)),))
        self.obs = Observacion(100, B, tuple(VistaPR(e.entrega.pr, e.entrega.head,
            apto=True, hallazgos=0, dirigida=True, ci="verde", conflicto=False) for e in entradas),
            ci_candidato="verde", costo_suite_sana_s=10000, ci_recibo="recibo-ci")

    def iniciar(self, codigo=1):
        self.runner.responder = lambda argv: suite_fixture(codigo=codigo, fallos=(F,) if codigo == 1 else ())
        esperando, efectos = paso(self.estado, self.obs)
        llegada = ejecutar(efectos[0], esperando, self.obs, self.git, self.adaptador)
        self.assertTrue(llegada.completo)
        estado, efectos = paso(esperando, llegada.observacion)
        return estado, efectos, llegada.observacion

    def diagnosticar(self, funcion):
        estado, efectos, obs = self.iniciar()
        original = estado
        solicitudes = []
        def responder(argv):
            origen = Path(argv[argv.index("--repetir") + 1])
            commit = argv[argv.index("--sha") + 1]
            objetivo = "C" if commit == C else "B" if commit == B else self.git.commits[commit]
            solicitudes.append(objetivo)
            return repeticion_fixture(origen, commit, valores=funcion(objetivo))
        self.runner.responder = responder
        for _ in range(40):
            if estado.pausa or isinstance(estado.trenes[0].fase, (Certificado, Terminal)):
                break
            self.assertTrue(efectos)
            e = efectos[0]
            llegada = ejecutar(e, estado, obs, self.git, self.adaptador)
            anterior = estado
            estado, efectos = paso(estado, llegada.observacion)
            self.assertEqual(paso(anterior, llegada.observacion), (estado, efectos))
            estado = desde_datos(datos(estado))
            obs = replace(self.obs, ahora=obs.ahora + 1)
        else:
            self.fail("el diagnóstico no converge")
        self.assertIsInstance(original.trenes[0].fase, Diagnosticando)
        return estado, efectos, solicitudes

    def test_suite_verde_certifica_y_captura_base_arbol_suite_ci(self):
        estado, efectos, obs = self.iniciar(0)
        cert = estado.trenes[0].fase
        self.assertIsInstance(cert, Certificado)
        self.assertEqual((cert.B, cert.C, cert.arbol, cert.ci), (B, C, T, "recibo-ci"))
        self.assertTrue(Path(cert.suite).is_file())
        self.assertEqual(estado.costo_suite_sana_s, 10)
        self.assertFalse(any(e.tipo == "devolver" for e in efectos))
        self.assertIn(("--suite", C), tuple(zip(self.runner.llamadas[0], self.runner.llamadas[0][1:])))

    def test_verde_contra_base_directo_certifica(self):
        r = suite_fixture(codigo=0, fallos=(replace(F, clase="también-base"),))
        resultado = leer_suite(r, C, B)
        self.assertEqual(resultado.veredicto, "verde-contra-base")
        estado = replace(self.estado, trenes=(replace(self.estado.trenes[0], fase=Corriendo(CANDIDATO, "suite")),))
        final, efectos = paso(estado, replace(self.obs, tren_id="lote", suite="suite", resultado_suite=resultado))
        self.assertIsInstance(final.trenes[0].fase, Certificado)
        self.assertEqual(efectos, ())

    def test_preexistente_confirmado_no_culpa_y_certifica(self):
        estado, efectos, solicitudes = self.diagnosticar(lambda _: ("falla", "falla"))
        self.assertEqual(solicitudes, ["C", "B"])
        self.assertIsInstance(estado.trenes[0].fase, Certificado)
        self.assertEqual(efectos, ())
        self.assertFalse(self.git.armados)

    def test_rojo_unico_dos_mas_dos_devuelve_y_sobreviven_dos(self):
        estado, efectos, solicitudes = self.diagnosticar(lambda objetivo:
            ("falla", "falla") if objetivo in ("C", (2,)) else ("pasa", "pasa"))
        self.assertEqual(solicitudes, ["C", "B", (1,), (2,), (3,)])
        self.assertEqual(estado.trenes[0].fase, Terminal("DEVUELTO"))
        self.assertEqual(estado.entradas[1].terminal, Terminal("DEVUELTO"))
        self.assertEqual(estado.trenes[1].recibos, tuple(estado.entradas[i].entrega.recibo for i in (0, 2)))
        self.assertEqual(estado.trenes[1].fase, Congelado(B))
        devoluciones = [e for e in efectos if e.tipo == "devolver"]
        self.assertEqual(len(devoluciones), 1)
        payload = json.loads(devoluciones[0].datos[2])
        self.assertEqual((payload["B"], payload["C"], payload["pr"]), (B, C, 2))
        self.assertEqual(payload["fallos"][0]["conjunto_causal"], [2])
        self.assertIn("--unidad", shlex.split(payload["fallos"][0]["comando"]))
        self.assertEqual(efectos[-1].tipo, "armar-candidato")

    def test_intermitente_pausa_sin_devolver(self):
        estado, efectos, solicitudes = self.diagnosticar(lambda objetivo:
            ("falla", "pasa") if objetivo == "C" else ("pasa", "pasa"))
        self.assertEqual(estado.pausa.motivo, "necesita-juicio")
        self.assertIn("confirmacion-candidato", estado.pausa.detalle)
        self.assertEqual(solicitudes, ["C", "B"])
        self.assertEqual(efectos, ())

    def test_interaccion_ab_devuelve_a_ambos_y_c_sobrevive(self):
        estado, efectos, solicitudes = self.diagnosticar(lambda objetivo:
            ("falla", "falla") if objetivo in ("C", (1, 2)) else ("pasa", "pasa"))
        self.assertEqual(solicitudes, ["C", "B", (1,), (2,), (3,), (1, 2), (1, 3), (2, 3)])
        self.assertEqual(estado.trenes[1].recibos, (estado.entradas[2].entrega.recibo,))
        self.assertEqual(len([e for e in efectos if e.tipo == "devolver"]), 2)
        for efecto in efectos[:-1]:
            self.assertEqual(json.loads(efecto.datos[2])["fallos"][0]["conjunto_causal"], [1, 2])

    def test_presupuesto_no_despacha_si_no_cabe(self):
        self.obs = replace(self.obs, costo_suite_sana_s=30)
        estado, efectos, _ = self.iniciar()
        self.assertEqual(estado.pausa.motivo, "necesita-juicio")
        self.assertIn("presupuesto", estado.pausa.detalle)
        self.assertEqual(efectos, ())
        self.assertEqual(len(self.runner.llamadas), 1)

    def test_sin_medicion_sana_no_inventa_presupuesto(self):
        self.obs = replace(self.obs, costo_suite_sana_s=0)
        estado, efectos, _ = self.iniciar()
        self.assertEqual(estado.pausa.detalle, "sin-costo-de-suite-sana")
        self.assertEqual(efectos, ())

    def test_75_tres_reanudaciones_esperas_y_pausa_infraestructura(self):
        self.runner.responder = lambda argv: suite_fixture(codigo=75, fallos=())
        estado, efectos = paso(self.estado, self.obs)
        obs = ejecutar(efectos[0], estado, self.obs, self.git, self.adaptador).observacion
        estado, efectos = paso(estado, obs)
        self.assertIsInstance(estado.trenes[0].fase, Corriendo)
        claves = set()
        for intento, espera in enumerate((300, 900, 3600), 1):
            fase = estado.trenes[0].fase
            self.assertEqual(fase.esperar_hasta, obs.ahora + espera)
            antes = replace(self.obs, ahora=fase.esperar_hasta - 1)
            self.assertEqual(paso(estado, antes)[1], ())
            estado, efectos = paso(estado, replace(antes, ahora=fase.esperar_hasta))
            self.assertEqual(efectos[0].tipo, "reanudar-suite")
            self.assertNotIn(efectos[0].clave, claves)
            claves.add(efectos[0].clave)
            obs = ejecutar(efectos[0], estado, replace(self.obs, ahora=fase.esperar_hasta), self.git,
                           self.adaptador).observacion
            self.assertEqual(obs.suite_intento, intento)
            estado, efectos = paso(estado, obs)
        self.assertEqual(estado.pausa.motivo, "infraestructura")
        self.assertEqual(efectos, ())
        self.assertEqual(len(self.runner.llamadas), 4)
        self.assertTrue(all("--reanudar" in a for a in self.runner.llamadas[1:]))
        self.assertFalse(any(e.terminal for e in estado.entradas))

    def test_75_repeticion_no_es_evidencia_y_reanuda(self):
        estado, efectos, obs = self.iniciar()
        def responder(argv):
            r = repeticion_fixture(Path(argv[argv.index("--repetir") + 1]), C)
            r.update(veredicto=75, completa=False)
            return r
        self.runner.responder = responder
        llegada = ejecutar(efectos[0], estado, self.obs, self.git, self.adaptador)
        estado, efectos = paso(estado, llegada.observacion)
        fase = estado.trenes[0].fase
        self.assertEqual(fase.evidencias, ())
        self.assertEqual(fase.esperar_hasta, 400)
        self.assertEqual(efectos, ())
        estado, efectos = paso(estado, replace(self.obs, ahora=400))
        self.assertEqual(efectos[0].tipo, "repetir")
        llegada = ejecutar(efectos[0], estado, replace(self.obs, ahora=400), self.git, self.adaptador)
        self.assertIsInstance(llegada.observacion.repeticion, Incompleto)
        self.assertIn("--reanudar", self.runner.llamadas[-1])

    def test_formatos_y_repeticiones_incompletas_nunca_evidencia(self):
        origen = self.raiz / "origen.json"
        origen.write_text(json.dumps(suite_fixture()))
        correcto = repeticion_fixture(origen, C)
        mutaciones = [lambda r: r.update(esquema="suite-repeticion@99"),
            lambda r: r.update(completa=False),
            lambda r: r["unidades"][0]["cobertura"].update(completa=False),
            lambda r: r["unidades"][0]["intentos"][0].update(veredicto="sin-observacion"),
            lambda r: r["unidades"][0]["intentos"][1].update(n=1),
            lambda r: r["origen"].update(sha256="0" * 64)]
        for mutar in mutaciones:
            r = deepcopy(correcto)
            mutar(r)
            with self.assertRaises(ResultadoInvalido):
                leer_repeticion(r, C, origen, C, B, F)
        for r in (dict(esquema="desconocido"), dict(suite_fixture(), veredicto=2),
                  dict(suite_fixture(codigo=0, fallos=()), partes=[])):
            with self.assertRaises(ResultadoInvalido):
                leer_suite(r, C, B)
        estado, efectos = paso(self.estado, self.obs)
        self.runner.responder = lambda argv: dict(suite_fixture(), esquema="otro@1")
        with self.assertRaises(EfectoFallido) as error:
            ejecutar(efectos[0], estado, self.obs, self.git, self.adaptador)
        self.assertIsInstance(error.exception.resultado, ErrorResultado)

    def test_recibo_idempotente_y_unidad_literal_y_reanudar_validado(self):
        ruta, resultado = self.adaptador.suite(CANDIDATO, "lote")
        self.assertEqual(self.adaptador.suite(CANDIDATO, "lote"), (ruta, resultado))
        self.assertEqual(len(self.runner.llamadas), 1)
        argv = self.adaptador.argv_repetir(ruta, B, self.git.worktree(B), self.raiz, "repro-caso", F)
        self.assertIn(F.check + "=" + F.unidad, argv)
        valido = shlex.join(list(argv) + ["--hosts", "mini", "--reanudar"])
        self.assertEqual(self.adaptador.reanudacion(valido, argv), tuple(shlex.split(valido)))
        for comando in (valido + "; echo rojo", valido.replace(B, C), "bash /otro --reanudar"):
            with self.assertRaises(ResultadoInvalido):
                self.adaptador.reanudacion(comando, argv)

    def test_firmas_legacy_sin_firma_sigue_leyendo_detalle(self):
        r = suite_fixture()
        r["solo_candidato"][0].pop("firma")
        self.assertEqual(leer_suite(r, C, B).fallos[0].firma, F.firma)

    def test_resultados_ajenos_no_cambian_estado(self):
        estado, _, obs = self.iniciar()
        llegada = replace(obs, tren_id="ajeno", resultado_suite=Completo("verde"))
        self.assertEqual(paso(estado, llegada), (estado, ()))

    def test_eventos_u1_u2_con_defaults_siguen_leyendo(self):
        r = datos(Corriendo(CANDIDATO, "suite"))
        for k in ("ci", "reanudaciones", "esperar_hasta", "reanudar"):
            r.pop(k)
        self.assertEqual(desde_datos(r), Corriendo(CANDIDATO, "suite"))
        self.assertEqual(desde_datos({"_tipo": "Fallo", "unidad": "u", "firma": "f", "clase": "c"}),
                         Fallo("u", "f", "c"))

    def evidencia_pura(self, solicitud, valores, firma=None, duracion=3):
        fallo = solicitud.fallo
        u = UnidadRepetida(fallo.check, fallo.unidad, fallo.parte, "f" * 64,
            tuple(Intento(n, v, (fallo.firma if firma is None else firma) if v == "falla" else "", duracion)
                  for n, v in enumerate(valores, 1)))
        return Evidencia(solicitud, Repeticion(C, C, B, "a" * 64, (u,)))

    def test_puro_archivos_solo_ordenan_no_deciden_culpa(self):
        evidencias = []
        orden = []
        for _ in range(10):
            plan = planificar((F,), (1, 2, 3), tuple(evidencias), 3600,
                              ((3, ("tests/test_producto.py",)),))
            self.assertEqual(plan, planificar((F,), (1, 2, 3), tuple(evidencias), 3600,
                              ((3, ("tests/test_producto.py",)),)))
            if plan.terminado:
                self.assertEqual(plan.causas[0].minimos, ((2,),))
                break
            self.assertFalse(plan.incierto)
            s = plan.siguiente
            orden.append((s.objetivo, s.prs))
            valores = ("falla", "falla") if s.objetivo == "C" or s.prs == (2,) else ("pasa", "pasa")
            evidencias.append(self.evidencia_pura(s, valores))
        self.assertEqual(orden[2:], [("subconjunto", (3,)), ("subconjunto", (1,)), ("subconjunto", (2,))])

    def test_minimo_triple_y_varios_minimos_sin_atajos_por_archivos(self):
        for fallan in (((1, 2), (1, 3)), ()):
            evidencias = []
            for _ in range(12):
                plan = planificar((F,), (1, 2, 3), tuple(evidencias), 3600)
                if plan.terminado:
                    esperados = fallan or ((1, 2, 3),)
                    self.assertEqual(plan.causas[0].minimos, esperados)
                    break
                s = plan.siguiente
                valores = ("falla", "falla") if s.objetivo == "C" or s.prs in fallan else ("pasa", "pasa")
                evidencias.append(self.evidencia_pura(s, valores))
            else:
                self.fail("no encontró todos los mínimos")

    def test_duracion_actual_aumenta_estimacion_y_presupuesto_limita_siguiente(self):
        primero = planificar((F,), (1, 2, 3), (), 60)
        self.assertEqual(primero.siguiente.costo_s, 20)
        evidencia = self.evidencia_pura(primero.siguiente, ("falla", "falla"), duracion=20)
        siguiente = planificar((F,), (1, 2, 3), (evidencia,), 60)
        self.assertIsNone(siguiente.siguiente)
        self.assertIn("presupuesto-agotado", siguiente.incierto)
        exacto = planificar((F,), (1, 2, 3), (evidencia,), 80)
        self.assertEqual(exacto.siguiente.costo_s, 40)

    def test_ausente_omitida_firma_distinta_y_base_mixta_pasan_a_juicio(self):
        c = planificar((F,), (1, 2, 3), (), 3600).siguiente
        ec = self.evidencia_pura(c, ("falla", "falla"))
        b = planificar((F,), (1, 2, 3), (ec,), 3600).siguiente
        for valores, firma in ((("ausente", "ausente"), None), (("omitida", "omitida"), None),
                               (("falla", "pasa"), None), (("falla", "falla"), "otra firma")):
            plan = planificar((F,), (1, 2, 3), (ec, self.evidencia_pura(b, valores, firma)), 3600)
            self.assertTrue(plan.incierto)
            self.assertFalse(plan.terminado)

    def test_firma_origen_vacia_usa_confirmacion_y_no_atribuye_firma_vacia(self):
        f = replace(F, firma="")
        c = planificar((f,), (1,), (), 3600).siguiente
        ec = self.evidencia_pura(c, ("falla", "falla"), firma="firma-nueva")
        b = planificar((f,), (1,), (ec,), 3600).siguiente
        plan = planificar((f,), (1,), (ec, self.evidencia_pura(b, ("pasa", "pasa"))), 3600)
        self.assertEqual(plan.causas[0].fallo.firma, "firma-nueva")
        self.assertEqual(plan.causas[0].minimos, ((1,),))

    def test_observacion_incompleta_guarda_resultado_y_retiene_efectos(self):
        esperando, efectos = paso(self.estado, self.obs)
        self.runner.responder = lambda argv: suite_fixture()
        llegada = ejecutar(efectos[0], esperando, self.obs, self.git, self.adaptador).observacion
        incompleta = replace(llegada, prs=(replace(self.obs.prs[0], completa=False),) + self.obs.prs[1:])
        # El recibo completo llega con la admisión retenida.
        diagnosticando, efectos = paso(esperando, incompleta)
        self.assertIsInstance(diagnosticando.trenes[0].fase, Diagnosticando)
        self.assertEqual(efectos, ())
        recuperado, efectos = paso(diagnosticando, self.obs)
        self.assertEqual(efectos[0].tipo, "repetir")
        self.assertEqual(len(self.runner.llamadas), 1)

    def test_reanudar_puede_cambiar_carpeta_por_traslado_sin_perder_resultado(self):
        fase = Corriendo(CANDIDATO, "recibo-anterior", reanudaciones=1, reanudar="comando")
        estado = replace(self.estado, trenes=(replace(self.estado.trenes[0], fase=fase),))
        final, _ = paso(estado, replace(self.obs, tren_id="lote", suite_intento=1,
                         suite="recibo-trasladado", resultado_suite=Completo("verde")))
        self.assertIsInstance(final.trenes[0].fase, Certificado)
        self.assertEqual(final.trenes[0].fase.suite, "recibo-trasladado")

    def test_sobrevivientes_no_agregan_ticket_nuevo_y_reciben_suite_completa(self):
        extra = Entrada(Entrega("example/project", 4, "9" * 40, "apto", "dirigida"), 4)
        self.estado = replace(self.estado, entradas=self.estado.entradas + (extra,))
        self.obs = replace(self.obs, prs=self.obs.prs + (VistaPR(4, "9" * 40, apto=True,
                           hallazgos=0, dirigida=True, ci="verde", conflicto=False),))
        estado, efectos, _ = self.diagnosticar(lambda objetivo:
            ("falla", "falla") if objetivo in ("C", (2,)) else ("pasa", "pasa"))
        self.assertIsNone(estado.entradas[3].tren)
        for _ in range(4):
            efecto = next(e for e in efectos if e.tipo != "devolver")
            if efecto.tipo == "iniciar-suite":
                break
            llegada = ejecutar(efecto, estado, self.obs, self.git, self.adaptador)
            estado, efectos = paso(estado, llegada.observacion)
            if not efectos:  # Armado conserva el resultado antes de solicitar CI.
                estado, efectos = paso(estado, self.obs)
        else:
            self.fail("no llegó a la suite completa")
        candidato = estado.trenes[1].fase.candidato
        self.runner.responder = lambda argv: suite_fixture(candidato, 0, ())
        llegada = ejecutar(efecto, estado, self.obs, self.git, self.adaptador)
        final, _ = paso(estado, llegada.observacion)
        self.assertIsInstance(final.trenes[1].fase, Certificado)
        self.assertEqual(final.trenes[1].recibos, estado.trenes[1].recibos)
        self.assertIsNone(final.entradas[3].tren)
        self.assertIn("--suite", self.runner.llamadas[-1])
        self.assertIn("final", self.runner.llamadas[-1])


class IntegracionGitTests(git_fixtures.AterrizajeTests):
    def test_verde_adaptador_hasta_cas_aterrizado_y_worktrees_durables(self):
        estado, candidato, obs = self.armar()
        estado, _ = paso(estado, obs)
        obs = replace(obs, ci_candidato="verde", ci_recibo="ci-real")
        estado, efectos = paso(estado, obs)
        runner = RunnerFalso(self.raiz, candidato)
        hasito = Hasito(self.raiz / "run.sh", self.git, runner)
        llegada = ejecutar(efectos[0], estado, obs, self.git, hasito)
        estado, _ = paso(estado, llegada.observacion)
        self.assertIsInstance(estado.trenes[0].fase, Certificado)
        wt = self.git.worktree(candidato.C)
        self.assertTrue(wt.is_dir())
        self.assertEqual(self.git.worktree(candidato.C), wt)
        self.assertEqual(self.comando(wt, "rev-parse", "HEAD"), candidato.C)
        self.assertEqual(self.comando(wt, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")
        self.assertEqual(self.comando(wt, "rev-parse", "--git-common-dir"), str(self.git.ruta))
        self.assertTrue(self.git.worktree(candidato.B).is_dir())
        estado, efectos = paso(estado, obs)
        llegada = ejecutar(efectos[0], estado, obs, self.git)
        final, _ = paso(estado, llegada.observacion)
        self.assertEqual(final.trenes[0].fase, Terminal("ATERRIZADO"))
        self.assertEqual(self.remoto(), candidato.C)
        self.assertTrue(wt.is_dir())


def load_tests(loader, tests, pattern):
    return unittest.TestSuite([loader.loadTestsFromTestCase(HasitoTests),
        unittest.TestSuite(IntegracionGitTests(n) for n in sorted(IntegracionGitTests.__dict__)
                          if n.startswith("test_"))])
