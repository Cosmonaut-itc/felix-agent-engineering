"""U17: el tick activo ve el main que acaba de aterrizar; sólo host remoto.

config["git"] es aquí un clon no bare, separado del origen y del bare del
tren. Se sincroniza una sola vez, antes del primer tick, con main y los heads
de todos los PR; después nadie le trae objetos. Es un checkout humano: rama
propia, índice distinto del árbol y un archivo no versionado, que no cambian
byte a byte. Cada tick pasa por la construcción real de `pao-tren`: sólo se
sustituye el transporte de gh.
"""
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch
import unittest

import test_activo as activo
import test_juicio
from dominio import (Admitido, Certificado, Congelado, Entrega, Motivo, Retenido,
                     Terminal, datos)
from git_ops import ErrorGit, FalloGit, GitTren
from ledger import directorio_estado
import observador
from observador import GitHubGit

REPO = activo.REPO
ATERRIZADO = Terminal("ATERRIZADO")
# Antes del injerto no existe: el valor documenta el presupuesto exigido.
PRESUPUESTO = getattr(observador, "PRESUPUESTO_TRANSPORTE_S", 120)


class RelojFalso:
    def __init__(self):
        self.ahora = 0.0

    def __call__(self):
        return self.ahora


class AlmacenFalso:
    """Bare y remoto falsos: cada fetch tarda `demora` s del reloj falso."""
    def __init__(self, demora, refs, presentes=(), caidas=()):
        self.reloj = RelojFalso()
        self.demora = demora
        self.refs = refs
        self.presentes = set(presentes)
        self.caidas = set(caidas)
        self.fetches = []

    def preparar(self):
        pass

    def git(self, *args, aislado=False):
        objeto = args[-1].split("^")[0]
        return subprocess.CompletedProcess(args, 0 if objeto in self.presentes else 1)

    def traer(self, ref, timeout=None):
        self.fetches.append((ref, timeout))
        if ref in self.caidas or (timeout is not None and self.demora > timeout):
            self.reloj.ahora += self.demora if timeout is None else min(self.demora, timeout)
            raise FalloGit("git-no-disponible")
        self.reloj.ahora += self.demora
        traido = self.refs.get(ref, ref)
        self.presentes.add(traido)
        return traido

    def texto(self, *args):
        raise AssertionError("la prueba no lee objetos")


class ContextoU17(unittest.TestCase):
    comando = activo.ActivoTests.comando
    commit = activo.ActivoTests.commit
    push = activo.ActivoTests.push
    remoto = activo.ActivoTests.remoto
    ledger = activo.ActivoTests.ledger
    estado = activo.ActivoTests.estado
    lineas = activo.ActivoTests.lineas
    cli = test_juicio.JuicioTests.cli
    juicio = test_juicio.JuicioTests.juicio
    posts = test_juicio.JuicioTests.posts

    def setUp(self):
        activo.ActivoTests.setUp(self)
        # El lote siguiente ya existe en GitHub; se entrega tras el primer aterrizaje.
        self.siguientes = []
        for pr in (4, 5, 6):
            head = self.commit({"comun": "base\n", "pr-{}".format(pr): "ticket\n"},
                               padres=(self.B,), mensaje="ticket {}".format(pr))
            self.push(head, "refs/pull/{}/head".format(pr))
            self.siguientes.append((pr, head))
        self.checkout = (self.raiz / "checkout").resolve()
        self.comando(self.raiz, "clone", "--no-hardlinks", str(self.origen), str(self.checkout))
        self.comando(self.checkout, "fetch", "origin", "+refs/pull/*/head:refs/remotes/origin/pull/*")
        for _, head in self.miembros + self.siguientes:
            self.assertTrue(self.tiene(self.checkout, head))
        self.assertFalse((self.checkout / ".git/objects/info/alternates").exists())
        # Uso humano en curso: rama propia, índice distinto del árbol y un
        # archivo no versionado.
        self.comando(self.checkout, "switch", "-c", "trabajo-humano")
        (self.checkout / "comun").write_text("cambio en el índice\n")
        self.comando(self.checkout, "add", "comun")
        (self.checkout / "comun").write_text("cambio sin guardar\n")
        (self.checkout / "nota").write_text("no versionado\n")
        self.git_tick = self.contexto.git
        self.reloj = None
        self.observadores = []
        self.contexto.config["git"] = str(self.checkout)
        self.privada = self.raiz / "config-privada.json"
        self.privada.write_text(json.dumps({"repos": {REPO: self.contexto.config}}))
        self.huella_inicial = self.huella()

    def tiene(self, ruta, commit):
        return subprocess.run(["git", "-C", str(ruta), "cat-file", "-e", commit + "^{commit}"],
                              capture_output=True, timeout=30).returncode == 0

    def huella(self):
        """HEAD, ramas, índice, árbol y FETCH_HEAD; además, los bytes de cada
        archivo del checkout humano, .git incluido."""
        return {"refs": self.comando(self.checkout, "for-each-ref",
                                     "--format=%(objectname) %(refname)"),
                "simbolica": self.comando(self.checkout, "symbolic-ref", "HEAD"),
                "head": self.comando(self.checkout, "rev-parse", "HEAD"),
                "estado": self.comando(self.checkout, "--no-optional-locks", "status",
                                       "--porcelain", "--untracked-files=all"),
                "fetch_head": (self.checkout / ".git" / "FETCH_HEAD").read_text(),
                "bytes": {str(p.relative_to(self.checkout)): p.read_bytes()
                          for p in sorted(self.checkout.rglob("*")) if p.is_file()}}

    def crear(self, config, **kwargs):
        # Sólo se sustituye gh; pao-tren decide modo, almacén y lector.
        creado = GitHubGit(config, ejecutar=self.contexto.gh, **kwargs)
        if self.reloj is not None:
            creado.reloj = self.reloj
        self.observadores.append(creado)
        return creado

    def tick(self, ahora):
        args = activo.cli.parser().parse_args(["tick", "--repo", REPO,
                                               "--config", str(self.privada)])
        with patch.object(activo.cli, "GitHubGit", side_effect=self.crear):
            codigo, salida = activo.cli.ejecutar(args, None, ahora, self.git_tick,
                                                 self.contexto.hasito, None,
                                                 self.contexto.escritor)
        self.assertEqual(codigo, 0, salida)
        return salida

    def hasta(self, condicion, inicio, limite=30):
        for n in range(limite):
            self.tick(inicio + n * 120)
            estado = self.estado()
            self.assertIsNone(estado.pausa, datos(estado.pausa))
            if estado.trenes and condicion(estado):
                return estado
        self.fail("no convergió: " + json.dumps(datos(self.estado()))[:4000])

    def primer_lote(self):
        estado = self.hasta(lambda e: e.trenes[0].fase == ATERRIZADO, 100)
        C1 = self.remoto()
        self.assertNotEqual(C1, self.B)
        self.assertFalse(self.tiene(self.checkout, C1))
        return estado, C1

    def entregar_siguientes(self, ahora):
        for pr, head in self.siguientes:
            apto = self.raiz / "apto-{}.json".format(pr)
            dirigida = self.raiz / "dirigida-{}.json".format(pr)
            apto.write_text(json.dumps({"head": head, "veredicto": "APTO",
                                        "hallazgos_abiertos": 0}))
            dirigida.write_text(json.dumps({"head": head, "resultado": "limpio"}))
            self.contexto.config["miembros"].append([str(pr), head])
            args = activo.cli.parser().parse_args(["entregar", "--repo", REPO,
                "--pr", str(pr), "--head", head, "--apto", str(apto),
                "--dirigida", str(dirigida)])
            self.assertEqual(activo.cli.ejecutar(args, ahora=ahora)[0], 0)

    def entregar(self, pr, head, ahora):
        """Entrega directa al ledger: el head puede faltar en el checkout."""
        apto = self.raiz / "apto-{}-{}.json".format(pr, head[:7])
        dirigida = self.raiz / "dirigida-{}-{}.json".format(pr, head[:7])
        apto.write_text(json.dumps({"head": head, "veredicto": "APTO", "hallazgos_abiertos": 0}))
        dirigida.write_text(json.dumps({"head": head, "resultado": "limpio"}))
        # La API del PR informa el último head registrado aquí.
        self.contexto.config["miembros"].append([str(pr), head])
        with self.ledger() as ledger:
            ledger.entregar(Entrega(REPO, pr, head, str(apto), str(dirigida)), ahora)

    def entrada(self, estado, head):
        return next(e for e in estado.entradas if e.entrega.head == head)

    def registrar(self):
        """argv de cada comando git del bare del tren, sin cambiar su efecto."""
        argv = []
        original = self.contexto.git.ejecutar

        def ejecutar(comando, **kwargs):
            argv.append(list(comando))
            return original(comando, **kwargs)
        self.contexto.git.ejecutar = ejecutar
        self.addCleanup(setattr, self.contexto.git, "ejecutar", original)
        return argv

    def lote(self, estado):
        prs = {pr for pr, _ in self.siguientes}
        return [e for e in estado.entradas if e.entrega.pr in prs]

    def assert_lote_siguiente_aterriza(self, C1, inicio):
        final = self.hasta(lambda e: len(e.trenes) == 2 and e.trenes[1].fase == ATERRIZADO,
                           inicio)
        self.assertEqual([t.fase for t in final.trenes], [ATERRIZADO, ATERRIZADO])
        C2 = self.remoto()
        # Tres eslabones sobre C1: el lote siguiente se armó sobre el main aterrizado.
        self.assertEqual(self.comando(self.origen, "rev-parse", C2 + "~3"), C1)
        for _, head in self.siguientes:
            self.comando(self.origen, "merge-base", "--is-ancestor", head, C2)
        self.assertEqual(sum(p.endswith(":refs/heads/main") for p in self.lineas("pushes.log")), 2)
        self.assertEqual(len(self.lineas("trabajos.log")), 2)
        self.assertEqual(self.posts("/comments"), [])
        self.assertFalse(any(p["payload"]["state"] == "failure" for p in self.posts("/statuses/")))
        self.assertFalse(self.tiene(self.checkout, C2))
        self.assertEqual(self.huella(), self.huella_inicial)


class RegresionesTests(ContextoU17):
    def test_dos_lotes_sucesivos_sin_sincronizar_el_checkout(self):
        _, C1 = self.primer_lote()
        self.entregar_siguientes(5000)
        # El tick siguiente admite y arma el lote sobre C1, sin acción externa.
        self.tick(5100)
        estado = self.estado()
        self.assertIsNone(estado.pausa, datos(estado.pausa))
        self.assertEqual([e.admision for e in self.lote(estado)], [Admitido()] * 3)
        self.assertEqual(len(estado.trenes), 2)
        self.assertEqual(set(estado.trenes[1].recibos),
                         {e.entrega.recibo for e in self.lote(estado)})
        self.assert_lote_siguiente_aterriza(C1, 5220)

    def test_main_ajeno_tras_reanudar_se_admite_sin_sincronizar(self):
        self.hasta(lambda e: isinstance(e.trenes[0].fase, Certificado), 100)
        ajeno = self.commit({"comun": "base\n", "externo": "cambio\n"}, (self.B,))
        self.push(ajeno, "refs/heads/ajeno")
        (self.raiz / "escritor").write_text(ajeno)
        for n in range(30):
            self.tick(5000 + n * 120)
            if self.estado().pausa:
                break
        self.assertEqual(self.estado().pausa.motivo, "escritor-externo")
        self.assertEqual(self.remoto(), ajeno)
        self.assertFalse(self.tiene(self.checkout, ajeno))
        self.juicio("reanudar")
        self.tick(8000)
        self.assertEqual(len(self.estado().trenes), 1)
        (self.raiz / "escritor").rename(self.raiz / "escritor-retirado")
        self.contexto.config["base"] = ajeno
        # Felix reanudó: el tick siguiente readmite y rearma sobre el main ajeno.
        self.tick(8200)
        self.assertEqual(self.estado().trenes[-1].fase, Congelado(ajeno))
        self.hasta(lambda e: len(e.trenes) == 2 and e.trenes[1].fase == ATERRIZADO, 8320)
        self.comando(self.origen, "merge-base", "--is-ancestor", ajeno, self.remoto())
        self.assertEqual(self.posts("/comments"), [])
        self.assertFalse(self.tiene(self.checkout, ajeno))
        self.assertEqual(self.huella(), self.huella_inicial)

    def test_falla_al_traer_objetos_retiene_sin_feedback_y_reintenta(self):
        _, C1 = self.primer_lote()
        self.tick(4900)
        posts = self.posts()
        pushes = self.lineas("pushes.log")
        self.entregar_siguientes(5000)
        original = self.contexto.git.ejecutar
        intentos = []

        def transporte(argv, **kwargs):
            if "fetch" in argv:
                intentos.append(len(intentos))
                if len(intentos) % 2:
                    return subprocess.CompletedProcess(argv, 128, "", "fatal: dato privado")
                raise subprocess.TimeoutExpired(argv, 120, stderr="dato privado")
            return original(argv, **kwargs)

        self.contexto.git.ejecutar = transporte
        try:
            for n in range(3):
                antes = len(intentos)
                salida = self.tick(5100 + n * 120)
                self.assertNotIn("dato privado", json.dumps(salida, ensure_ascii=False))
                estado = self.estado()
                self.assertIsNone(estado.pausa, datos(estado.pausa))
                self.assertEqual(len(estado.trenes), 1)
                self.assertEqual([e.admision for e in self.lote(estado)],
                                 [Retenido(Motivo.INCIERTO)] * 3)
                self.assertTrue(all(e.terminal is None and e.tren is None
                                    for e in self.lote(estado)))
                # Cada tick vuelve a intentar traer los heads que faltan.
                self.assertGreater(len(intentos), antes)
        finally:
            self.contexto.git.ejecutar = original
        self.assertEqual(self.posts(), posts)
        self.assertEqual(self.lineas("pushes.log"), pushes)
        with self.ledger() as ledger:
            texto = json.dumps([tuple(r) for r in ledger.db.execute(
                "SELECT * FROM events")], ensure_ascii=False)
        self.assertNotIn("dato privado", texto)
        # Vuelve el transporte: el tick siguiente admite y arma sin más acción.
        self.tick(5500)
        estado = self.estado()
        self.assertEqual([e.admision for e in self.lote(estado)], [Admitido()] * 3)
        self.assertEqual(len(estado.trenes), 2)
        self.assert_lote_siguiente_aterriza(C1, 5620)


class PresupuestoTests(ContextoU17):
    """Una caída del remoto no se multiplica por la cola bajo el candado.

    `remoto-u17-injerto.sh` exige que ambas fallen sobre d0019396 y pasen
    después. Transporte y reloj falsos: ninguna espera real.
    """

    def test_fetch_de_una_observacion_comparte_presupuesto_y_corta_tras_caer(self):
        refs = {"refs/pull/{}/head".format(pr): head for pr, head in self.siguientes}

        def leer(almacen):
            lector = GitHubGit(self.contexto.config, activo=True, almacen=almacen)
            lector.reloj = almacen.reloj
            vistas = []
            for pr, head in self.siguientes:
                try:
                    lector.lector(pr, self.B, head)
                    vistas.append(pr)
                except FalloGit:
                    pass
            return vistas

        # Remoto lento: cada fetch tarda 50 s y la cola entera dispone del
        # presupuesto; el que lo agota se da por caído y el resto no trae.
        lento = AlmacenFalso(50, refs)
        self.assertEqual(leer(lento), [4])
        self.assertEqual(lento.fetches, [(self.B, PRESUPUESTO),
                                         ("refs/pull/4/head", PRESUPUESTO - 50),
                                         ("refs/pull/5/head", PRESUPUESTO - 100)])
        self.assertEqual(lento.reloj.ahora, PRESUPUESTO)
        # Falla común de la base: ninguna otra vista la repite ni trae heads.
        caida = AlmacenFalso(1, refs, caidas={self.B})
        self.assertEqual(leer(caida), [])
        self.assertEqual(caida.fetches, [(self.B, PRESUPUESTO)])
        # Un head que cae corta también los heads de las vistas siguientes.
        cae_head = AlmacenFalso(1, refs, presentes={self.B}, caidas={"refs/pull/4/head"})
        self.assertEqual(leer(cae_head), [])
        self.assertEqual(cae_head.fetches, [("refs/pull/4/head", PRESUPUESTO)])
        # Con base y heads presentes no se consulta el remoto.
        presentes = AlmacenFalso(1, refs, presentes={self.B} | set(refs.values()))
        self.assertEqual(leer(presentes), [4, 5, 6])
        self.assertEqual(presentes.fetches, [])

    def test_remoto_caido_trae_una_vez_por_tick_sin_feedback(self):
        _, C1 = self.primer_lote()
        self.tick(4900)
        posts = self.posts()
        pushes = self.lineas("pushes.log")
        self.entregar_siguientes(5000)
        self.reloj = RelojFalso()
        original = self.contexto.git.ejecutar
        plazos = []

        def caido(argv, **kwargs):
            if "fetch" in argv:
                # Cada fetch agota el plazo que recibe, sin esperarlo.
                plazos.append(kwargs["timeout"])
                self.reloj.ahora += kwargs["timeout"]
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"], stderr="dato privado")
            return original(argv, **kwargs)

        self.contexto.git.ejecutar = caido
        try:
            for n in range(2):
                del plazos[:]
                inicio = self.reloj.ahora
                salida = self.tick(5100 + n * 120)
                # Faltan tres heads, pero tras la primera caída nadie más trae.
                self.assertEqual(plazos, [PRESUPUESTO])
                self.assertLessEqual(self.reloj.ahora - inicio, PRESUPUESTO)
                self.assertNotIn("dato privado", json.dumps(salida, ensure_ascii=False))
                estado = self.estado()
                self.assertIsNone(estado.pausa, datos(estado.pausa))
                self.assertEqual(len(estado.trenes), 1)
                self.assertEqual([e.admision for e in self.lote(estado)],
                                 [Retenido(Motivo.INCIERTO)] * 3)
        finally:
            self.contexto.git.ejecutar = original
        self.assertEqual(self.posts(), posts)
        self.assertEqual(self.lineas("pushes.log"), pushes)
        # Vuelve el remoto: el tick siguiente admite y arma sin más acción.
        self.tick(5500)
        estado = self.estado()
        self.assertEqual([e.admision for e in self.lote(estado)], [Admitido()] * 3)
        self.assert_lote_siguiente_aterriza(C1, 5620)


class ObjetosTests(ContextoU17):
    """Identidad de los objetos que lee la admisión activa y composición real."""

    def test_base_ausente_falla_al_traer_y_el_tick_siguiente_se_recupera(self):
        original = self.contexto.git.ejecutar
        fetches = []

        def sin_base(argv, **kwargs):
            if "fetch" in argv:
                fetches.append(argv[-1])
                if argv[-1] == self.B:
                    return subprocess.CompletedProcess(argv, 128, "", "fatal: dato privado")
            return original(argv, **kwargs)

        # Bare nuevo: le faltan la base y los tres heads del primer lote.
        self.assertFalse((directorio_estado() / "git").exists())
        self.contexto.git.ejecutar = sin_base
        try:
            for n in range(2):
                del fetches[:]
                self.tick(100 + n * 120)
                self.assertEqual(fetches, [self.B])
                estado = self.estado()
                self.assertIsNone(estado.pausa, datos(estado.pausa))
                self.assertEqual(estado.trenes, ())
                self.assertEqual([e.admision for e in estado.entradas],
                                 [Retenido(Motivo.INCIERTO)] * 3)
        finally:
            self.contexto.git.ejecutar = original
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.lineas("trabajos.log"), [])
        self.hasta(lambda e: e.trenes[0].fase == ATERRIZADO, 340)
        self.assertFalse(self.tiene(self.checkout, self.remoto()))
        self.assertFalse(any(p["payload"]["state"] == "failure" for p in self.posts("/statuses/")))
        self.assertEqual(self.posts("/comments"), [])
        self.assertEqual(self.huella(), self.huella_inicial)

    def test_ref_del_pr_adelantada_retiene_sin_leer_otro_objeto(self):
        _, C1 = self.primer_lote()
        self.tick(4900)
        posts = self.posts()
        pr, viejo = self.siguientes[0]
        nuevo = self.commit({"comun": "base\n", "pr-4": "otro\n"}, padres=(viejo,))
        self.push(nuevo, "refs/pull/4/head")
        # La API sigue en viejo, ausente del bare; la ref ya apunta a nuevo.
        self.entregar(pr, viejo, 5000)
        argv = self.registrar()
        self.tick(5100)
        estado = self.estado()
        self.assertEqual(self.entrada(estado, viejo).admision, Retenido(Motivo.INCIERTO))
        self.assertEqual(len(estado.trenes), 1)
        self.assertEqual([a[-1] for a in argv if "fetch" in a], ["refs/pull/4/head"])
        # Retiene sin leer el objeto traído ni ningún otro.
        self.assertFalse(any("merge-base" in a or "merge-tree" in a or nuevo in a
                             for a in argv), argv)
        self.assertEqual(self.posts(), posts)
        # La entrega nueva admite el head que sólo existe en la ref del PR.
        self.assertFalse(self.tiene(self.checkout, nuevo))
        self.entregar(pr, nuevo, 5200)
        self.tick(5300)
        self.assertEqual(self.entrada(self.estado(), nuevo).admision, Admitido())
        self.assertFalse(self.tiene(self.checkout, nuevo))
        # Único aviso: la entrega de viejo caducó porque la API ya informa nuevo.
        comentarios = self.posts("/comments")[len([p for p in posts if "/comments" in p["endpoint"]]):]
        self.assertEqual(len(comentarios), 1, comentarios)
        self.assertIn("caducó: head-cambiado", comentarios[0]["payload"]["body"])
        self.assertFalse(any(p["payload"].get("state") == "failure" for p in self.posts("/statuses/")))
        self.assertEqual(self.huella(), self.huella_inicial)

    def test_head_presente_no_consulta_la_ref_y_el_armado_retiene(self):
        _, C1 = self.primer_lote()
        self.tick(4900)
        pushes = self.lineas("pushes.log")
        pr, viejo = self.siguientes[0]
        # El bare ya tiene viejo; después la ref del PR avanza a nuevo.
        self.assertEqual(self.contexto.git.traer("refs/pull/4/head"), viejo)
        nuevo = self.commit({"comun": "base\n", "pr-4": "otro\n"}, padres=(viejo,))
        self.push(nuevo, "refs/pull/4/head")
        self.entregar(pr, viejo, 5000)
        argv = self.registrar()
        # Un PR solo forma tren una hora después de admitirse.
        for ahora in (5100, 8800, 8920):
            self.tick(ahora)
        estado = self.estado()
        self.assertEqual(self.entrada(estado, viejo).admision, Admitido())
        # La admisión leyó el SHA exacto de la API, sin fetch ni ls-remote.
        lectura = next(n for n, a in enumerate(argv)
                       if "merge-tree" in a and "--write-tree" not in a)
        self.assertEqual(argv[lectura][-3:], [self.B, C1, viejo])
        self.assertTrue(any(a[-3:] == ["merge-base", C1, viejo] for a in argv[:lectura]))
        self.assertFalse(any("fetch" in a or "ls-remote" in a for a in argv[:lectura]),
                         argv[:lectura])
        self.assertFalse(any(nuevo in a for a in argv[:lectura]))
        # El armado sí trae la ref, ve otro head y no arma ni publica.
        self.assertEqual(len(estado.trenes), 2)
        self.assertIsInstance(estado.trenes[1].fase, Congelado)
        self.assertEqual(self.contexto.git.armar(C1, ((pr, viejo),)),
                         ErrorGit("head-no-coincide", pr))
        self.assertEqual(self.lineas("pushes.log"), pushes)
        self.assertEqual(self.posts("/comments"), [])
        self.assertEqual(self.huella(), self.huella_inicial)

    def test_composicion_productiva_sin_gittren_inyectado(self):
        # pao-tren construye su GitTren(remoto) y lo entrega como almacén.
        self.git_tick = None
        _, C1 = self.primer_lote()
        self.entregar_siguientes(5000)
        self.hasta(lambda e: len(e.trenes) == 2 and e.trenes[1].fase == ATERRIZADO, 5100)
        C2 = self.remoto()
        self.assertEqual(self.comando(self.origen, "rev-parse", C2 + "~3"), C1)
        ruta = self.contexto.git.ruta
        self.assertTrue(self.observadores)
        for creado in self.observadores:
            self.assertIsInstance(creado.almacen, GitTren)
            self.assertIsNot(creado.almacen, self.contexto.git)
            self.assertEqual(creado.almacen.ruta, ruta)
            self.assertTrue(creado.activo)
        self.assertEqual(self.posts("/comments"), [])
        self.assertFalse(self.tiene(self.checkout, C2))
        self.assertEqual(self.huella(), self.huella_inicial)


class SombraTests(ContextoU17):
    """Sombra, entregar y --seco leen config["git"] sin fetch; `remoto-u17.sh`
    compara sus eventos byte a byte con los de main anterior a U17."""

    def setUp(self):
        # Fechas fijas: mismos SHA, recibos e ids en ambos árboles.
        fechas = patch.dict(os.environ, {"GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
                                         "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"})
        fechas.start()
        self.addCleanup(fechas.stop)
        ContextoU17.setUp(self)
        self.git_argv = []

    def crear(self, config, **kwargs):
        def ejecutar(argv, **opciones):
            if argv[0] == "git":
                self.git_argv.append(list(argv))
            return self.contexto.gh(argv, **opciones)
        return GitHubGit(config, ejecutar=ejecutar, **kwargs)

    def cli_local(self, *argv, ahora):
        args = activo.cli.parser().parse_args([argv[0], "--repo", REPO,
                                               "--config", str(self.privada), *argv[1:]])
        with patch.object(activo.cli, "GitHubGit", side_effect=self.crear):
            codigo, salida = activo.cli.ejecutar(args, ahora=ahora)
        self.assertEqual(codigo, 0, salida)
        return salida

    def test_sombra_entregar_y_seco_sin_fetch_ni_cambios(self):
        pr, head = self.siguientes[0]
        apto = self.raiz / "apto-4.json"
        dirigida = self.raiz / "dirigida-4.json"
        apto.write_text(json.dumps({"head": head, "veredicto": "APTO", "hallazgos_abiertos": 0}))
        dirigida.write_text(json.dumps({"head": head, "resultado": "limpio"}))
        self.contexto.config["miembros"].append([str(pr), head])
        self.inscritos.write_text(REPO + " sombra\n")
        self.cli_local("entregar", "--pr", str(pr), "--head", head, "--apto", str(apto),
                       "--dirigida", str(dirigida), ahora=50)
        for inscripcion in ("sombra", "activo"):
            self.inscritos.write_text(REPO + " " + inscripcion + "\n")
            for ahora in (100, 3700):
                self.cli_local("tick", "--seco", ahora=ahora)
        # main avanza con un objeto ausente del checkout: la sombra retiene, no trae.
        externo = self.commit({"comun": "base\n", "externo": "cambio\n"}, (self.B,))
        self.push(externo, "refs/heads/main")
        for inscripcion in ("sombra", "activo"):
            self.inscritos.write_text(REPO + " " + inscripcion + "\n")
            self.cli_local("tick", "--seco", ahora=7300)
        with self.ledger() as ledger:
            eventos = [(r["tipo"], r["instante"], json.loads(r["payload"])) for r in ledger.db.execute(
                "SELECT tipo, instante, payload FROM events WHERE tipo IN "
                "('sombra', 'sombra-doble-camino', 'admision-entrega') ORDER BY seq")]
        self.assertEqual([t for t, _, _ in eventos],
                         ["admision-entrega"] + ["sombra-doble-camino"] * 2 + ["sombra"] * 2 +
                         ["sombra-doble-camino", "sombra"])
        # Toda lectura git fue en el checkout configurado y ninguna trajo objetos.
        self.assertTrue(self.git_argv)
        self.assertTrue(all(a[:3] == ["git", "-C", str(self.checkout)] and "fetch" not in a
                            for a in self.git_argv), self.git_argv)
        self.assertFalse(self.tiene(self.checkout, externo))
        self.assertEqual(self.huella(), self.huella_inicial)
        self.assertFalse((directorio_estado() / "git").exists())
        texto = json.dumps(eventos, sort_keys=True).replace(str(self.raiz), "<raiz>")
        destino = os.environ.get("PAO_TREN_U17_SOMBRA")
        if destino:
            Path(destino).write_text(texto)


if __name__ == "__main__":
    unittest.main()
