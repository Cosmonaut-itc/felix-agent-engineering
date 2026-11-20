"""U2: sólo en el host de pruebas, sin red; fixtures conservadas."""
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
from dominio import (Aterrizado, Armado, Candidato, Certificado, ConflictoCadena,
                     Congelado, EnBase, Entrada, Entrega, EscritorExterno, Estado,
                     Incierto, LIMITE_PROMOCION, Observacion, Pausa, Promoviendo,
                     Terminal, VistaPR, clave, datos, desde_datos, paso)
from efectos import EfectoFallido, ejecutar
from git_ops import ErrorGit, GitTren, Publicado, RamaOcupada
from ledger import Ledger, codificar


class AterrizajeTests(unittest.TestCase):
    def setUp(self):
        # Canónica como las referencias que congela `entregar` (macOS: /var -> /private/var).
        self.raiz = Path(tempfile.mkdtemp(prefix="pao-tren-u2-")).resolve()
        self.home = self.raiz / "home"
        self.home.mkdir()
        self.env = patch.dict(os.environ, {"HOME": str(self.home),
            "XDG_STATE_HOME": str(self.raiz / "state"),
            "GIT_CONFIG_GLOBAL": str(self.home / ".gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        version = subprocess.run(["git", "version"], capture_output=True,
                                 text=True, check=True).stdout
        numeros = re.search(r"git version (\d+)\.(\d+)", version)
        self.assertIsNotNone(numeros, version)
        self.assertGreaterEqual(tuple(map(int, numeros.groups())), (2, 38),
            "U2 requiere git >=2.38 con merge-tree --write-tree; instalado: " + version)
        self.origen = self.raiz / "origen.git"
        self.fuente = self.raiz / "fuente.git"
        for ruta in (self.origen, self.fuente):
            subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(ruta)],
                           capture_output=True, check=True, text=True)
        self.B = self.commit({"comun": "base\n"}, mensaje="base")
        self.push(self.B, "refs/heads/main")
        self.miembros = []
        for pr in (1, 2, 3):
            head = self.commit({"comun": "base\n", "pr-{}".format(pr): "ticket\n"},
                               padres=(self.B,), mensaje="ticket {}".format(pr))
            self.push(head, "refs/pull/{}/head".format(pr))
            self.miembros.append((pr, head))
        self.git = GitTren(str(self.origen))

    def comando(self, ruta, *args, input=None):
        return subprocess.run(["git", "-C", str(ruta), *args], input=input,
            capture_output=True, text=True, check=True, timeout=30).stdout.strip()

    def commit(self, archivos, padres=(), mensaje="fixture"):
        def armar_arbol(archivos):
            entradas, carpetas = [], {}
            for ruta, texto in archivos.items():
                nombre, barra, resto = ruta.partition("/")
                if barra:
                    carpetas.setdefault(nombre, {})[resto] = texto
                else:
                    blob = self.comando(self.fuente, "hash-object", "-w", "--stdin", input=texto)
                    entradas.append(f"100644 blob {blob}\t{nombre}\0")
            for nombre, hijos in carpetas.items():
                entradas.append(f"040000 tree {armar_arbol(hijos)}\t{nombre}\0")
            return self.comando(self.fuente, "mktree", "-z", input="".join(entradas))

        arbol = armar_arbol(archivos)
        args = ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                "-c", "commit.gpgsign=false", "commit-tree", arbol]
        for padre in padres:
            args += ["-p", padre]
        return self.comando(self.fuente, *args, input=mensaje + "\n")

    def push(self, commit, ref):
        self.comando(self.fuente, "push", str(self.origen), commit + ":" + ref)

    def remoto(self, ref="refs/heads/main"):
        return self.comando(self.origen, "rev-parse", ref)

    def congelar(self, miembros=None):
        miembros = self.miembros if miembros is None else miembros
        entradas = tuple(Entrada(Entrega("example/project", pr, head, "apto", "dirigida"), pr)
                         for pr, head in miembros)
        vistas = tuple(VistaPR(pr, head, apto=True, hallazgos=0, dirigida=True,
                              ci="verde", conflicto=False) for pr, head in miembros)
        obs = Observacion(100, self.B, vistas)
        estado, efectos = paso(Estado("example/project", entradas), obs)
        if not efectos:
            obs = replace(obs, ahora=3700)
            estado, efectos = paso(estado, obs)
        self.assertEqual(efectos[0].tipo, "armar-candidato")
        return estado, efectos[0], obs

    def armar(self):
        estado, efecto, obs = self.congelar()
        obs = ejecutar(efecto, estado, obs, self.git).observacion
        estado, _ = paso(estado, obs)
        self.assertIsInstance(estado.trenes[0].fase, Armado)
        return estado, obs.candidato, obs

    def certificar(self, estado, candidato, obs):
        cert = Certificado(candidato.C, candidato.arbol, "recibo-suite", "recibo-ci", candidato.B)
        estado = replace(estado, trenes=(replace(estado.trenes[0], fase=cert),))
        estado, efectos = paso(estado, obs)
        self.assertIsInstance(estado.trenes[0].fase, Promoviendo)
        return estado, efectos[0]

    def assert_status_aterrizaje(self, estado, efectos):
        tren = estado.trenes[-1]
        self.assertEqual([(e.tipo, e.clave, e.datos) for e in efectos], [
            ("avisar-aterrizaje", clave("avisar-aterrizaje", tren.id, r), (tren.id, r))
            for r in tren.recibos])

    def test_feliz_tres_pr_ci_cas_postcondicion_y_reductor(self):
        estado, candidato, obs = self.armar()
        self.assertEqual(candidato.B, self.B)
        self.assertEqual(self.remoto(), self.B)
        self.assertFalse((self.git.ruta / "index").exists())
        self.assertEqual(self.comando(self.git.ruta, "rev-parse", "--is-bare-repository"), "true")
        estado, efectos = paso(estado, replace(obs, candidato=None))
        self.assertEqual(efectos[0].tipo, "solicitar-ci")
        publicado = ejecutar(efectos[0], estado, obs, self.git).observacion
        self.assertEqual(publicado.ci_candidato, "pendiente")
        self.assertEqual(self.remoto("refs/heads/tren/" + estado.trenes[0].id), candidato.C)
        estado, efecto = self.certificar(estado, candidato, obs)
        llegada = ejecutar(efecto, estado, obs, self.git).observacion
        self.assertEqual(llegada.main, Aterrizado(self.B, candidato.C, candidato.arbol))
        self.assertEqual(self.remoto(), candidato.C)
        self.assertEqual(self.comando(self.origen, "rev-parse", "main^{tree}"), candidato.arbol)
        for _, head in self.miembros:
            self.comando(self.origen, "merge-base", "--is-ancestor", head, "main")
        final, efectos = paso(estado, replace(llegada, prs=()))
        self.assertEqual(final.trenes[0].fase, Terminal("ATERRIZADO"))
        self.assert_status_aterrizaje(final, efectos)

    def test_reproducible_otro_bare_global_sistema_fechas_y_firma(self):
        primero = self.git.armar(self.B, self.miembros)
        self.assertIsInstance(primero, Candidato)
        self.assertEqual(self.git.armar(self.B, self.miembros), primero)
        global_config = self.home / "otra.config"
        global_config.write_text("[user]\n name = Otra identidad\n email = otra@example.invalid\n"
            "[commit]\n gpgsign = true\n[merge]\n renormalize = true\n"
            "[core]\n attributesFile = /ruta/inexistente\n")
        segundo = GitTren(str(self.origen), self.raiz / "segundo.git")
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(global_config),
            "GIT_CONFIG_SYSTEM": str(global_config), "GIT_CONFIG_NOSYSTEM": "0",
            "GIT_AUTHOR_DATE": "2001-01-01T12:00:00-0700", "TZ": "Pacific/Honolulu"}):
            self.assertEqual(segundo.armar(self.B, self.miembros), primero)
        max_fecha = max(int(self.comando(self.fuente, "show", "-s", "--format=%ct", c))
                        for c in [self.B] + [h for _, h in self.miembros])
        for i, (_, head, commit) in enumerate(primero.eslabones, 1):
            metadatos = self.comando(segundo.ruta, "cat-file", "-p", commit)
            self.assertIn("committer PAO Tren <tren@example.invalid> {} +0000".format(max_fecha + i), metadatos)
            self.assertNotIn("gpgsig", metadatos)
            padres = self.comando(segundo.ruta, "show", "-s", "--format=%P", commit).split()
            self.assertEqual(padres, [self.B if i == 1 else primero.eslabones[i - 2][2], head])

    def test_identidad_configurada_firma_los_merges_y_reproduce(self):
        identidad = ("Felix", "1+felix@users.noreply.github.com")
        propio = GitTren(str(self.origen), self.raiz / "propio.git", identidad=identidad)
        primero = propio.armar(self.B, self.miembros)
        self.assertIsInstance(primero, Candidato)
        otro = GitTren(str(self.origen), self.raiz / "otro.git", identidad=identidad)
        self.assertEqual(otro.armar(self.B, self.miembros), primero)
        self.assertNotEqual(self.git.armar(self.B, self.miembros).C, primero.C)
        for _, _, commit in primero.eslabones:
            metadatos = self.comando(propio.ruta, "cat-file", "-p", commit)
            for rol in ("author", "committer"):
                self.assertIn("{} Felix <1+felix@users.noreply.github.com> ".format(rol), metadatos)
            self.assertNotIn("tren@example.invalid", metadatos)

    def test_escritor_externo_conservado_rearmado_y_fifo(self):
        estado, candidato, obs = self.armar()
        ajeno = self.commit({"comun": "base\n", "ajeno": "conservar\n"}, (self.B,), "externo")
        self.push(ajeno, "refs/heads/main")
        estado, efecto = self.certificar(estado, candidato, obs)
        llegada = ejecutar(efecto, estado, obs, self.git).observacion
        self.assertEqual(llegada.main, EscritorExterno(self.B, candidato.C, ajeno))
        final, efectos = paso(estado, llegada)
        self.assertEqual(self.remoto(), ajeno)
        self.assertEqual(final.trenes[0].fase, Terminal("REARMAR"))
        self.assertEqual(final.pausa, Pausa("escritor-externo", main=ajeno))
        self.assertEqual(efectos, ())
        self.assertEqual(tuple((e.recibida, e.admitida) for e in final.entradas),
                         tuple((e.recibida, e.admitida) for e in estado.entradas))
        self.assertTrue(all(e.tren is None for e in final.entradas))
        rearmado, efectos = paso(replace(final, pausa=None), replace(obs, base=ajeno, candidato=None))
        self.assertEqual(rearmado.trenes[-1].fase, Congelado(ajeno))
        self.assertEqual(rearmado.trenes[-1].recibos, estado.trenes[0].recibos)
        nueva = ejecutar(efectos[0], rearmado, obs, self.git).observacion.candidato
        self.assertIsInstance(nueva, Candidato)
        self.comando(self.git.ruta, "merge-base", "--is-ancestor", ajeno, nueva.C)

    def test_ack_perdido_reconcilia_y_repetir_no_cambia_main(self):
        estado, candidato, obs = self.armar()
        estado, efecto = self.certificar(estado, candidato, obs)
        llamadas = []
        def perder_ack(args, **kwargs):
            llamadas.append(args)
            salida = subprocess.run(args, **kwargs)
            if "push" in args:
                raise subprocess.TimeoutExpired(args, 120)
            return salida
        self.git.ejecutar = perder_ack
        llegada = ejecutar(efecto, estado, obs, self.git).observacion
        self.assertEqual(self.remoto(), candidato.C)
        self.assertIsInstance(llegada.main, Aterrizado)
        repetida = ejecutar(efecto, estado, obs, self.git).observacion
        self.assertEqual(repetida, llegada)
        self.assertEqual(self.remoto(), candidato.C)
        self.assertTrue(any("ls-remote" in args for args in llamadas))
        cerrado = replace(llegada, prs=tuple(replace(v, abierto=False) for v in obs.prs))
        final, _ = paso(estado, cerrado)
        self.assertEqual(final.trenes[0].fase, Terminal("ATERRIZADO"))

    def test_rechazo_servidor_limite_tres_intentos(self):
        estado, candidato, obs = self.armar()
        hook = self.origen / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        estado, efecto = self.certificar(estado, candidato, obs)
        claves = []
        for intento in range(1, LIMITE_PROMOCION + 1):
            claves.append(efecto.clave)
            self.assertEqual(estado.trenes[0].fase.intentos, intento)
            llegada = ejecutar(efecto, estado, obs, self.git).observacion
            self.assertEqual(llegada.main, EnBase(self.B, candidato.C))
            siguiente, efectos = paso(estado, llegada)
            self.assertEqual(paso(estado, llegada), (siguiente, efectos))
            estado = siguiente
            if efectos:
                efecto = efectos[0]
        self.assertEqual(len(set(claves)), LIMITE_PROMOCION)
        self.assertEqual(estado.pausa, Pausa("promocion-rechazada"))
        self.assertEqual(efectos, ())
        self.assertEqual(self.remoto(), self.B)

    def test_conflicto_entre_pr_limpios_con_base(self):
        miembros = []
        for pr in (4, 5):
            head = self.commit({"comun": "ticket {}\n".format(pr)}, (self.B,))
            self.push(head, "refs/pull/{}/head".format(pr))
            miembros.append((pr, head))
            self.assertIsInstance(self.git.armar(self.B, [(pr, head)]), Candidato)
        resultado = self.git.armar(self.B, miembros)
        self.assertEqual(resultado, ConflictoCadena(5, ("comun",)))
        estado, efecto, obs = self.congelar(miembros)
        obs = ejecutar(efecto, estado, obs, self.git).observacion
        final, efectos = paso(estado, obs)
        self.assertEqual(final.pausa, Pausa("conflicto-en-cadena", pr=5, archivos=("comun",)))
        self.assertEqual(efectos, ())
        self.assertEqual(self.remoto(), self.B)

    def test_publicar_idempotente_y_rama_ocupada_no_se_sobrescribe(self):
        candidato = self.git.armar(self.B, self.miembros)
        esperado = Publicado("refs/heads/tren/lote", candidato.C)
        self.assertEqual(self.git.publicar("lote", candidato.C), esperado)
        self.assertEqual(self.git.publicar("lote", candidato.C), esperado)
        self.push(self.B, "refs/heads/tren/ocupada")
        self.assertEqual(self.git.publicar("ocupada", candidato.C),
            RamaOcupada("refs/heads/tren/ocupada", candidato.C, self.B))
        self.assertEqual(self.remoto("refs/heads/tren/ocupada"), self.B)

    def test_lease_creacion_no_sobrescribe_rama_creada_entre_lectura_y_push(self):
        candidato = self.git.armar(self.B, self.miembros)
        def carrera(args, **kwargs):
            if "push" in args:
                self.push(self.B, "refs/heads/tren/carrera")
            return subprocess.run(args, **kwargs)
        self.git.ejecutar = carrera
        self.assertEqual(self.git.publicar("carrera", candidato.C),
            RamaOcupada("refs/heads/tren/carrera", candidato.C, self.B))
        self.assertEqual(self.remoto("refs/heads/tren/carrera"), self.B)

    def test_head_distinto_no_se_arma_ni_produce_observacion_positiva(self):
        miembros = [(1, self.miembros[1][1]), *self.miembros[1:]]
        self.assertEqual(self.git.armar(self.B, miembros), ErrorGit("head-no-coincide", 1))
        estado, efecto, obs = self.congelar(miembros)
        with self.assertRaises(EfectoFallido) as error:
            ejecutar(efecto, estado, obs, self.git)
        self.assertEqual(error.exception.resultado, ErrorGit("head-no-coincide", 1))
        self.assertEqual(self.remoto(), self.B)

    def test_incierto_conserva_estado_y_observacion_atrasada_no_gasta_intento(self):
        estado, candidato, obs = self.armar()
        estado, efecto = self.certificar(estado, candidato, obs)
        def sin_transporte(args, **kwargs):
            if "push" in args or "ls-remote" in args:
                raise subprocess.TimeoutExpired(args, 120)
            return subprocess.run(args, **kwargs)
        self.git.ejecutar = sin_transporte
        incierta = ejecutar(efecto, estado, obs, self.git).observacion
        self.assertIsInstance(incierta.main, Incierto)
        self.assertEqual(paso(estado, incierta), (estado, ()))
        en_base = replace(incierta, main=EnBase(self.B, candidato.C))
        siguiente, _ = paso(estado, en_base)
        self.assertEqual(siguiente.trenes[0].fase.intentos, 2)
        self.assertEqual(paso(siguiente, en_base), (siguiente, ()))
        with self.assertRaises(ValueError):
            ejecutar(efecto, siguiente, obs, self.git)

    def test_no_promueve_candidato_sin_base_ancestro_o_arbol_certificado(self):
        candidato = self.git.armar(self.B, self.miembros)
        ajeno = self.commit({"independiente": "no desciende\n"})
        self.push(ajeno, "refs/heads/independiente")
        self.git.traer("refs/heads/independiente")
        for B, C, arbol in ((self.B, candidato.C, "a" * 40),
                            (self.B, ajeno, self.comando(self.fuente, "rev-parse", ajeno + "^{tree}"))):
            self.assertIsInstance(self.git.promover(B, C, arbol, [h for _, h in self.miembros]), Incierto)
            self.assertEqual(self.remoto(), self.B)

    def test_promocion_sin_hooks_locales_ni_tags_y_comando_cas_exacto(self):
        estado, candidato, obs = self.armar()
        hook = self.git.ruta / "hooks/pre-push"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.comando(self.git.ruta, "config", "--global", "push.followTags", "true")
        self.comando(self.git.ruta, "-c", "user.name=Fixture", "-c",
            "user.email=fixture@example.invalid", "tag", "-a", "no-publicar", candidato.C, "-m", "tag")
        llamadas = []
        def registrar(args, **kwargs):
            llamadas.append((args, kwargs["env"]))
            return subprocess.run(args, **kwargs)
        self.git.ejecutar = registrar
        estado, efecto = self.certificar(estado, candidato, obs)
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(self.home / ".gitconfig")}):
            llegada = ejecutar(efecto, estado, obs, self.git).observacion
        self.assertIsInstance(llegada.main, Aterrizado)
        pushes = [(a, env) for a, env in llamadas if "push" in a]
        self.assertEqual(len(pushes), 1)
        args, env = pushes[0]
        self.assertIn("--force-with-lease=refs/heads/main:" + self.B, args)
        self.assertEqual(args[-2:], [str(self.origen), candidato.C + ":refs/heads/main"])
        self.assertIn("--no-verify", args)
        self.assertIn("--no-follow-tags", args)
        self.assertNotIn("--force", args)
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], str(self.home / ".gitconfig"))
        self.assertEqual(self.comando(self.origen, "tag", "--list"), "")

    def test_git_viejo_error_tipado_y_sin_fetch(self):
        llamadas = []
        def viejo(args, **kwargs):
            llamadas.append(args)
            return subprocess.CompletedProcess(args, 0, "git version 2.37.6\n", "")
        git = GitTren(str(self.origen), self.raiz / "viejo.git", ejecutar=viejo)
        self.assertEqual(git.armar(self.B, self.miembros),
                         ErrorGit("requiere-git-2.38-merge-tree-write-tree"))
        self.assertFalse(any("fetch" in args for args in llamadas))

    def test_encoding_u1_defaults_ledger_reabre_sin_migracion(self):
        antiguo = {"_tipo": "Certificado", "C": "a" * 40, "arbol": "b" * 40,
                   "suite": "suite-u1", "ci": "ci-u1"}
        certificado = desde_datos(antiguo)
        self.assertEqual(certificado.B, "")
        self.assertEqual(desde_datos({"_tipo": "Promoviendo", "certificado": antiguo}),
                         Promoviendo(certificado, 1))
        self.assertEqual(desde_datos({"_tipo": "Pausa", "motivo": "etapa-1"}), Pausa("etapa-1"))
        ledger = Ledger()
        with ledger.transaccion():
            ledger.evento("example/project", "u1", 1, antiguo)
        ruta = ledger.directorio
        ledger.close()
        reabierto = Ledger(ruta)
        self.addCleanup(reabierto.close)
        evento = reabierto.ultimos_eventos("example/project")[0]
        self.assertEqual(json.loads(evento["payload"]), antiguo)
        self.assertEqual(desde_datos(json.loads(evento["payload"])), certificado)
        self.assertEqual(desde_datos(json.loads(codificar(certificado))), certificado)
        estado, _, obs = self.congelar()
        estado = replace(estado, trenes=(replace(estado.trenes[0], fase=certificado),))
        final, efectos = paso(estado, obs)
        self.assertEqual(final.pausa, Pausa("certificado-sin-base"))
        self.assertEqual(efectos, ())


if __name__ == "__main__":
    unittest.main()
