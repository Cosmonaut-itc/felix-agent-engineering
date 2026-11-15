"""U2: armar C, CAS a main y reconciliación contra un origen bare local.

Sin red; sólo ejecutar en el host de pruebas. Fixtures retenidas.
"""
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
from dominio import (Armado, Aterrizado, Candidato, Certificado, ConflictoCadena,
                     Congelado, Corriendo, EnBase, Entrada, Entrega, EscritorExterno,
                     EsperandoCI, Estado, Observacion, Pausa, Promoviendo, Publicado,
                     RamaOcupada, Terminal, VistaPR, datos, desde_datos, paso)
from git_ops import (Ejecutor, GitAntiguo, GitTren, HeadNoCoincide, VERSION_MINIMA,
                     directorio_git)
from ledger import Ledger

REPO = "example/project"
EJECUTABLES = ("armar-candidato", "solicitar-ci", "promover")


def version_git():
    salida = subprocess.run(["git", "version"], capture_output=True, text=True).stdout
    match = re.search(r"(\d+)\.(\d+)", salida)
    return (int(match[1]), int(match[2])) if match else (0, 0)


class Base(unittest.TestCase):
    def setUp(self):
        self.raiz = Path(tempfile.mkdtemp(prefix="pao-tren-u2-"))
        self.home = self.raiz / "home"
        self.home.mkdir()
        self.env = patch.dict(os.environ, {"HOME": str(self.home),
                             "XDG_STATE_HOME": str(self.raiz / "state"),
                             "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
        self.env.start()
        self.addCleanup(self.env.stop)


class Origen(Base):
    """Origen bare con main = B y refs/pull/<n>/head creadas por la prueba."""

    def setUp(self):
        super().setUp()
        version = version_git()
        if version < VERSION_MINIMA:
            self.skipTest("git {}.{} en este host; U2 necesita git >= 2.38 "
                          "(merge-tree --write-tree)".format(*version))
        self.reloj = 1700000000
        self.origen = self.raiz / "origen.git"
        self.trabajo = self.raiz / "trabajo"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origen)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.trabajo)], check=True)
        self.B = self.commit(None, {"base.txt": "base\n", "compartido.txt": "uno\ndos\ntres\n"})
        self.git("push", "-q", str(self.origen), self.B + ":refs/heads/main")
        self.ordenes = []
        self.heads = ()

    def git(self, *args):
        self.reloj += 100
        fecha = "@{} +0000".format(self.reloj)
        env = dict(os.environ, GIT_AUTHOR_DATE=fecha, GIT_COMMITTER_DATE=fecha)
        return subprocess.run(["git", "-C", str(self.trabajo), "-c", "user.name=Fixture",
                               "-c", "user.email=fixture@example.invalid", *args],
                              check=True, capture_output=True, text=True, env=env,
                              timeout=60).stdout.strip()

    def remoto(self, *args, check=True):
        return subprocess.run(["git", "--git-dir=" + str(self.origen), *args], check=check,
                              capture_output=True, text=True, timeout=60)

    def main(self):
        return self.remoto("rev-parse", "refs/heads/main").stdout.strip()

    def commit(self, padre, archivos):
        if padre:
            self.git("checkout", "-q", "--detach", padre)
        for nombre, contenido in archivos.items():
            (self.trabajo / nombre).write_text(contenido)
        self.git("add", *archivos)
        self.git("commit", "-q", "-m", "fixture " + ",".join(archivos))
        return self.git("rev-parse", "HEAD")

    def pr(self, n, archivos, padre=None):
        head = self.commit(padre or self.B, archivos)
        self.git("push", "-q", str(self.origen), "{}:refs/pull/{}/head".format(head, n))
        return head

    def registrar(self, args, **kwargs):
        self.ordenes.append(list(args))
        return subprocess.run(args, **kwargs)

    def tren_git(self, directorio=None, ejecutar=None):
        return GitTren(directorio or directorio_git(REPO), str(self.origen),
                       ejecutar=ejecutar or self.registrar)

    def pushes(self):
        return [a for a in self.ordenes if "push" in a]

    def verificar_pushes(self):
        """Ningún push a main sin lease sobre B; ningún --force solo."""
        for args in self.pushes():
            self.assertFalse(any(a == "--force" or a == "-f" or a.startswith("+") for a in args), args)
            destino = args[-1].split(":", 1)[1]
            if destino == "refs/heads/main":
                self.assertIn("--force-with-lease=refs/heads/main:" + self.B, args)
            else:
                self.assertTrue(destino.startswith("refs/heads/tren/"), args)
                self.assertIn("--force-with-lease={}:".format(destino), args)
            self.assertIn("--no-verify", args)
            self.assertIn("--no-follow-tags", args)

    # -- reductor -------------------------------------------------------

    def obs(self, ahora=4000, **kwargs):
        vistas = tuple(VistaPR(pr, head, apto=True, hallazgos=0, dirigida=True, ci="verde",
                               conflicto=False) for pr, head in self.heads)
        return Observacion(ahora, self.main(), vistas, **kwargs)

    def estado(self):
        return Estado(REPO, tuple(Entrada(Entrega(REPO, pr, head, "/e/apto.json", "/e/dirigida.json"),
                                          float(i)) for i, (pr, head) in enumerate(self.heads, 1)))

    @staticmethod
    def activo(estado):
        return [t for t in estado.trenes if not isinstance(t.fase, Terminal)][0]

    def ejecutar(self, ejecutor, estado, efectos, obs):
        for efecto in efectos:
            if efecto.tipo in EJECUTABLES:
                obs = ejecutor.ejecutar(estado, efecto, obs).observacion
        return obs

    def formar(self, estado):
        estado, efectos = paso(estado, self.obs(100))
        if not estado.trenes:
            estado, efectos = paso(estado, self.obs(3800))
        self.assertIsInstance(self.activo(estado).fase, Congelado)
        self.assertEqual([e.tipo for e in efectos], ["armar-candidato"])
        return estado, efectos

    def certificar(self, ejecutor, estado=None, efectos=None):
        """Congelado -> Certificado. U4 construye Certificado; aquí se simula."""
        if estado is None:
            estado, efectos = self.formar(self.estado())
        estado, _ = paso(estado, self.ejecutar(ejecutor, estado, efectos, self.obs()))
        self.assertIsInstance(self.activo(estado).fase, Armado)
        estado, efectos = paso(estado, self.obs())
        self.assertEqual([e.tipo for e in efectos], ["solicitar-ci"])
        obs = self.ejecutar(ejecutor, estado, efectos, self.obs())
        self.assertIsInstance(obs.publicacion, Publicado)
        estado, _ = paso(estado, replace(obs, ci_candidato="verde", suite="suite-1"))
        tren = self.activo(estado)
        self.assertIsInstance(tren.fase, Corriendo)
        cand = tren.fase.candidato
        cert = Certificado(cand.C, cand.arbol, "suite-1", "verde", cand.B)
        return replace(estado, trenes=tuple(replace(t, fase=cert) if t.id == tren.id else t
                                            for t in estado.trenes)), cand

    def promover(self, ejecutor, estado):
        estado, efectos = paso(estado, self.obs())
        self.assertEqual([e.tipo for e in efectos], ["promover"])
        return estado, self.ejecutar(ejecutor, estado, efectos, self.obs())

    def tres_prs(self):
        self.heads = ((11, self.pr(11, {"a.txt": "a\n"})),
                      (12, self.pr(12, {"b.txt": "b\n"})),
                      (13, self.pr(13, {"compartido.txt": "uno\ndos\ntres\ncuatro\n"})))


class AterrizajeTests(Origen):
    def test_1_camino_feliz_tres_prs(self):
        self.tres_prs()
        ejecutor = Ejecutor(self.tren_git())
        estado, cand = self.certificar(ejecutor)
        self.assertEqual([pr for pr, _, _ in cand.eslabones], [11, 12, 13])
        self.assertEqual(self.remoto("rev-parse", "refs/heads/tren/" + self.activo(estado).id)
                         .stdout.strip(), cand.C)
        self.assertEqual(self.main(), self.B)
        estado, obs = self.promover(ejecutor, estado)
        self.assertEqual(obs.promocion, Aterrizado(cand.C))
        estado, efectos = paso(estado, obs)
        self.assertEqual(estado.trenes[0].fase, Terminal("ATERRIZADO"))
        self.assertIsNone(estado.pausa)
        self.assertEqual(efectos, ())
        self.assertEqual(self.main(), cand.C)
        self.assertEqual(self.remoto("rev-parse", "refs/heads/main^{tree}").stdout.strip(), cand.arbol)
        for _, head in self.heads:
            self.assertEqual(self.remoto("merge-base", "--is-ancestor", head, "refs/heads/main",
                                         check=False).returncode, 0)
        self.assertEqual(self.remoto("rev-parse", cand.C + "^1").stdout.strip(),
                         cand.eslabones[1][2])
        self.verificar_pushes()
        self.assertEqual(len([a for a in self.pushes() if a[-1].endswith(":refs/heads/main")]), 1)
        # Reconciliation always follows the promotion push.
        indice = max(i for i, a in enumerate(self.ordenes) if "push" in a)
        self.assertIn("ls-remote", self.ordenes[indice + 1])

    def test_2_reproducible_en_otro_clon_con_otra_configuracion(self):
        self.tres_prs()
        uno = self.tren_git().armar(self.B, self.heads)
        dos = self.tren_git().armar(self.B, self.heads)
        self.assertIsInstance(uno, Candidato)
        self.assertEqual(uno, dos)
        hooks = self.raiz / "hooks-hostiles"
        hooks.mkdir()
        for nombre in ("reference-transaction", "pre-push", "post-checkout"):
            (hooks / nombre).write_text("#!/bin/sh\nexit 1\n")
            os.chmod(str(hooks / nombre), 0o755)
        global_ = self.raiz / "gitconfig-otro"
        global_.write_text("[user]\n\tname = Otra Persona\n\temail = otra@example.invalid\n"
                           "[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = false\n"
                           "[i18n]\n\tcommitEncoding = ISO-8859-1\n"
                           "[core]\n\thooksPath = {}\n".format(hooks))
        otro = self.raiz / "otro-clon.git"
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(global_),
                                     "GIT_AUTHOR_NAME": "Intrusa", "GIT_COMMITTER_DATE": "@1 +0000",
                                     "TZ": "America/Mexico_City"}):
            tres = self.tren_git(otro).armar(self.B, self.heads)
        self.assertEqual(tres, uno)
        crudo = subprocess.run(["git", "--git-dir=" + str(otro), "cat-file", "commit", uno.C],
                               capture_output=True, text=True, check=True).stdout
        self.assertNotIn("gpgsig", crudo)
        self.assertNotIn("encoding", crudo)
        self.assertIn("author pao-tren <pao-tren@tren.invalid>", crudo)
        # Date = max committer time of both parents, in UTC.
        def ct(commit):
            return int(subprocess.run(["git", "--git-dir=" + str(otro), "show", "-s", "--format=%ct",
                                       commit], capture_output=True, text=True, check=True).stdout)
        anterior = self.B
        for _, head, c in uno.eslabones:
            self.assertEqual(ct(c), max(ct(anterior), ct(head)))
            anterior = c
        self.assertIn("+0000", crudo)

    def test_3_main_avanza_lease_rechaza_y_rearma(self):
        self.tres_prs()
        ejecutor = Ejecutor(self.tren_git())
        estado, cand = self.certificar(ejecutor)
        ajeno = self.commit(self.B, {"ajeno.txt": "ajeno\n"})
        self.git("push", "-q", str(self.origen), ajeno + ":refs/heads/main")
        tren_id = self.activo(estado).id
        estado, obs = self.promover(ejecutor, estado)
        self.assertEqual(obs.promocion, EscritorExterno(ajeno))
        self.assertEqual(self.main(), ajeno)
        self.assertEqual(self.remoto("rev-parse", "refs/heads/main^{tree}").stdout.strip(),
                         self.remoto("rev-parse", ajeno + "^{tree}").stdout.strip())
        antes = {e.entrega.recibo: e.admitida for e in estado.entradas}
        estado, efectos = paso(estado, obs)
        self.assertEqual(estado.trenes[0].fase, Terminal("REARMAR"))
        self.assertEqual(estado.pausa, Pausa("escritor-externo", "main=" + ajeno))
        self.assertTrue(all(e.tren is None for e in estado.entradas))
        self.assertEqual({e.entrega.recibo: e.admitida for e in estado.entradas}, antes)
        # Paused lane: nothing forms until the pause is lifted.
        self.assertEqual(paso(estado, self.obs())[0].trenes, estado.trenes)
        estado, efectos = paso(replace(estado, pausa=None), self.obs())
        nuevo = self.activo(estado)
        self.assertNotEqual(nuevo.id, tren_id)
        self.assertEqual(nuevo.fase, Congelado(ajeno))
        self.assertEqual(nuevo.recibos, estado.trenes[0].recibos)
        obs = self.ejecutar(ejecutor, estado, efectos, self.obs())
        self.assertIsInstance(obs.candidato, Candidato)
        self.assertEqual(obs.candidato.B, ajeno)
        self.verificar_pushes()

    def test_4_ack_perdido_reconcilia_y_repetir_no_cambia_main(self):
        self.tres_prs()
        estado, cand = self.certificar(Ejecutor(self.tren_git()))

        def ack_perdido(args, **kwargs):
            proc = self.registrar(args, **kwargs)
            if "push" in args and args[-1].endswith(":refs/heads/main"):
                return subprocess.CompletedProcess(args, 128, "", "fatal: the remote end hung up")
            return proc
        prom, efectos = paso(estado, self.obs())
        self.assertIsInstance(self.activo(prom).fase, Promoviendo)
        perdido = Ejecutor(self.tren_git(ejecutar=ack_perdido)).ejecutar(prom, efectos[0], self.obs())
        self.assertEqual(perdido.observacion.promocion, Aterrizado(cand.C))
        self.assertTrue(perdido.completo)
        self.assertEqual(self.main(), cand.C)
        # The tick died before recording: replay the same effect on the same state.
        repetido = Ejecutor(self.tren_git()).ejecutar(prom, efectos[0], self.obs())
        self.assertEqual(repetido.observacion.promocion, Aterrizado(cand.C))
        self.assertEqual(self.main(), cand.C)
        final, _ = paso(prom, repetido.observacion)
        self.assertEqual(final.trenes[0].fase, Terminal("ATERRIZADO"))
        self.verificar_pushes()

    def test_5_rechazo_reintenta_con_limite_y_pausa(self):
        self.tres_prs()
        ejecutor = Ejecutor(self.tren_git())
        estado, cand = self.certificar(ejecutor)
        cuenta = self.raiz / "pre-receive.cuenta"
        hook = self.origen / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho rechazo >> '{}'\nexit 1\n".format(cuenta))
        os.chmod(str(hook), 0o755)
        estado, efectos = paso(estado, self.obs())
        claves = []
        for _ in range(10):
            claves += [e.clave for e in efectos]
            obs = self.ejecutar(ejecutor, estado, efectos, self.obs())
            self.assertEqual(obs.promocion, EnBase(self.B))
            estado, efectos = paso(estado, obs)
            if estado.pausa:
                break
        self.assertEqual(estado.pausa, Pausa("promocion-rechazada", "intentos=3"))
        self.assertEqual(len(set(claves)), 3)
        self.assertEqual(cuenta.read_text().count("rechazo"), 3)
        self.assertEqual(self.main(), self.B)
        self.assertIsInstance(estado.trenes[0].fase, Promoviendo)
        self.verificar_pushes()

    def test_6_conflicto_en_cadena_nombra_el_pr(self):
        self.heads = ((21, self.pr(21, {"compartido.txt": "uno\nDOS-21\ntres\n"})),
                      (22, self.pr(22, {"compartido.txt": "uno\nDOS-22\ntres\n"})))
        git = self.tren_git()
        for _, head in self.heads:
            limpio = subprocess.run(["git", "--git-dir=" + str(self.origen), "merge-tree",
                                     "--write-tree", self.B, head], capture_output=True)
            self.assertEqual(limpio.returncode, 0)
        estado, efectos = self.formar(self.estado())
        obs = self.ejecutar(Ejecutor(git), estado, efectos, self.obs())
        self.assertEqual(obs.candidato, ConflictoCadena(self.B, 22, ("compartido.txt",)))
        estado, _ = paso(estado, obs)
        self.assertEqual(estado.pausa, Pausa("conflicto-en-cadena", "pr=22 archivos=compartido.txt"))
        self.assertIsInstance(estado.trenes[0].fase, Congelado)
        self.assertEqual(self.main(), self.B)
        self.assertEqual(self.pushes(), [])

    def test_7_publicar_idempotente_y_rama_ajena_no_se_sobrescribe(self):
        self.tres_prs()
        git = self.tren_git()
        cand = git.armar(self.B, self.heads)
        tren_id = "1" * 64
        self.assertEqual(git.publicar(tren_id, cand.C), Publicado(cand.C))
        self.assertEqual(git.publicar(tren_id, cand.C), Publicado(cand.C))
        self.assertEqual(self.remoto("rev-parse", "refs/heads/tren/" + tren_id).stdout.strip(), cand.C)
        ocupada = "2" * 64
        self.remoto("update-ref", "refs/heads/tren/" + ocupada, self.B)
        self.assertEqual(git.publicar(ocupada, cand.C), RamaOcupada(cand.C, self.B))
        self.assertEqual(self.remoto("rev-parse", "refs/heads/tren/" + ocupada).stdout.strip(), self.B)
        self.assertEqual(self.main(), self.B)
        self.verificar_pushes()
        # In the reducer, an occupied branch pauses the lane instead of waiting forever.
        estado, _ = paso(self.estado(), self.obs())
        estado = replace(estado, trenes=(replace(estado.trenes[0], fase=EsperandoCI(cand)),))
        estado, _ = paso(estado, self.obs(publicacion=RamaOcupada(cand.C, self.B)))
        self.assertEqual(estado.pausa, Pausa("rama-tren-ocupada", "sha=" + self.B))

    def test_8_head_distinto_de_refs_pull_no_se_arma(self):
        self.tres_prs()
        otro = self.commit(self.B, {"tarde.txt": "tarde\n"})
        heads = (self.heads[0], (12, otro), self.heads[2])
        git = self.tren_git()
        self.assertEqual(git.armar(self.B, heads), HeadNoCoincide(12, otro, self.heads[1][1]))
        refs = subprocess.run(["git", "--git-dir=" + str(directorio_git(REPO)), "for-each-ref",
                               "refs/tren/candidatos"], capture_output=True, text=True).stdout
        self.assertEqual(refs, "")
        self.heads = heads
        estado, efectos = self.formar(self.estado())
        ejecucion = Ejecutor(git).ejecutar(estado, efectos[0], self.obs())
        self.assertFalse(ejecucion.completo)
        self.assertIsNone(ejecucion.observacion.candidato)
        self.assertEqual(paso(estado, ejecucion.observacion)[0], estado)


class GitAntiguoTests(Base):
    def test_git_anterior_a_2_38_falla_tipado(self):
        def viejo(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, "git version 2.30.2\n", "")
        git = GitTren(self.raiz / "clon.git", str(self.raiz / "origen.git"), ejecutar=viejo)
        with self.assertRaises(GitAntiguo):
            git.armar("b" * 40, ((1, "a" * 40),))
        self.assertFalse((self.raiz / "clon.git").exists())


class CompatibilidadTests(Base):
    """9: los eventos guardados por U1 se leen igual."""
    CERT_U1 = {"_tipo": "Certificado", "C": "c" * 40, "arbol": "d" * 40,
               "suite": "s", "ci": "verde"}

    def u1(self, fase, pausa=None):
        return {"_tipo": "Estado", "repo": REPO, "entradas": [], "pausa": pausa,
                "trenes": [{"_tipo": "Tren", "id": "t", "recibos": [], "fase": fase}]}

    def test_9_encoding_u1(self):
        estado = desde_datos(json.loads(json.dumps(self.u1(self.CERT_U1,
                                                           {"_tipo": "Pausa", "motivo": "etapa-1"}))))
        self.assertEqual(estado.pausa, Pausa("etapa-1"))
        self.assertEqual(estado.trenes[0].fase.B, "")
        prom = desde_datos({"_tipo": "Promoviendo", "certificado": self.CERT_U1})
        self.assertEqual(prom.intentos, 1)
        self.assertEqual(desde_datos(datos(estado)), estado)
        # A pre-U2 Certificado cannot promote: it pauses, never pushes.
        nuevo, efectos = paso(replace(estado, pausa=None), Observacion(1, "b" * 40, ()))
        self.assertEqual(nuevo.pausa, Pausa("certificado-sin-base"))
        self.assertEqual(efectos, ())

    def test_9_evento_sombra_u1_en_ledger(self):
        ledger = Ledger()
        self.addCleanup(ledger.close)
        with ledger.transaccion():
            ledger.evento(REPO, "sombra", 1, {"estado": self.u1(
                {"_tipo": "Congelado", "B": "b" * 40}, {"_tipo": "Pausa", "motivo": "etapa-1"}),
                "efectos": []})
        estado = ledger.estado(REPO, sombra=True)
        self.assertEqual(estado.pausa, Pausa("etapa-1"))
        self.assertEqual(estado.trenes[0].fase, Congelado("b" * 40))


class GuardaEstaticaTests(unittest.TestCase):
    def test_push_unico_con_lease(self):
        fuente = (RAIZ / "git_ops.py").read_text()
        self.assertEqual(fuente.count('"push"'), 1)
        self.assertIsNone(re.search(r'"--force"|"-f"', fuente))
        self.assertEqual(fuente.count("--force-with-lease="), 1)
        self.assertIn("self._push(C, MAIN, B)", fuente)
        self.assertEqual(len(re.findall(r"self\._push\(", fuente)), 2)


if __name__ == "__main__":
    unittest.main()
