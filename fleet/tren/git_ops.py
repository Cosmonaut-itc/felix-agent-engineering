"""Cadena reproducible, publicación con lease y CAS; nunca usa un checkout.

Los objetos se construyen sin configuración global/sistema. El transporte
conserva esa configuración para credenciales, pero desactiva hooks y tags.
No se guardan argv ni stderr de transporte: pueden contener datos privados.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

from dominio import (Aterrizado, Candidato, ConflictoCadena, EnBase,
                     EscritorExterno, Incierto, RamaOcupada, clave)
from ledger import directorio_estado


@dataclass(frozen=True)
class ErrorGit:
    codigo: str
    pr: int = 0


@dataclass(frozen=True)
class Publicado:
    ref: str
    C: str


class FalloGit(Exception):
    """Sólo lleva un código público, nunca comandos o credenciales."""


def sha(valor):
    return isinstance(valor, str) and re.fullmatch(r"[0-9a-f]{40}", valor) is not None


def entorno(aislado=False):
    env = os.environ.copy()
    # Never inherit another checkout's object store, index, namespace or replace refs.
    for nombre in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                   "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                   "GIT_NAMESPACE", "GIT_SHALLOW_FILE", "GIT_REPLACE_REF_BASE",
                   "GIT_CONFIG"):
        env.pop(nombre, None)
    if aislado:
        for nombre in tuple(env):
            if nombre.startswith(("GIT_CONFIG_", "GIT_AUTHOR_", "GIT_COMMITTER_")):
                env.pop(nombre, None)
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                   LC_ALL="C", TZ="UTC")
    return env


class GitTren:
    """Un almacén bare propio por remoto; serializar con el candado del carril.

    ejecutar tiene el contrato de subprocess.run y permite simular ACK perdido.
    Métodos públicos devuelven datos tipados, incluidos errores de git/versión.
    """
    def __init__(self, remoto, ruta=None, ejecutar=None):
        if not isinstance(remoto, str) or not remoto or remoto.startswith("-"):
            raise ValueError("remoto explícito inválido")
        url = urlsplit(remoto)
        if url.scheme in ("http", "https") and (url.username or url.password or url.query):
            raise ValueError("usar credenciales de git, nunca en el remoto")
        self.remoto = remoto
        self.ruta = Path(ruta) if ruta is not None else (
            directorio_estado() / "git" / (clave("git", remoto) + ".git"))
        self.ejecutar = ejecutar or subprocess.run

    def git(self, *args, aislado=False, input=None, env_extra=None, timeout=120):
        env = entorno(aislado)
        env.update(env_extra or {})
        comando = ["git", "--no-replace-objects", "-c", "core.hooksPath=" + os.devnull,
                   "--git-dir=" + str(self.ruta), *args]
        try:
            return self.ejecutar(comando, capture_output=True, text=True,
                                 timeout=timeout, check=False, input=input, env=env)
        except (OSError, subprocess.SubprocessError):
            raise FalloGit("git-no-disponible") from None

    def texto(self, *args, aislado=True, **kwargs):
        resultado = self.git(*args, aislado=aislado, **kwargs)
        if resultado.returncode:
            raise FalloGit("operacion-git-fallida")
        return resultado.stdout.strip()

    def preparar(self):
        version = self.texto("version")
        numeros = re.search(r"git version (\d+)\.(\d+)", version)
        if not numeros or tuple(map(int, numeros.groups())) < (2, 38):
            raise FalloGit("requiere-git-2.38-merge-tree-write-tree")
        self.ruta.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not (self.ruta / "HEAD").exists():
            self.texto("init", "--bare", "--object-format=sha1", str(self.ruta))
        # Sólo configuración escrita por init, incluidos sus probes de filesystem.
        # No seguir includes: su propia clave se rechaza antes de leer su destino.
        permitidos = {"core.repositoryformatversion": {"0"},
                      "core.filemode": {"true", "false"}, "core.bare": {"true"},
                      "core.ignorecase": {"true", "false"},
                      "core.precomposeunicode": {"true", "false"}}
        config = self.texto("config", "--local", "--no-includes", "--null", "--list")
        vistos = set()
        for entrada in config.split("\0"):
            if not entrada:
                continue
            nombre, separador, valor = entrada.partition("\n")
            if (not separador or nombre in vistos or
                    valor not in permitidos.get(nombre, ())):
                raise FalloGit("config-local-no-permitida")
            vistos.add(nombre)
        atributos = self.ruta / "info" / "attributes"
        if atributos.exists() or atributos.is_symlink():
            raise FalloGit("atributos-locales-no-permitidos")
        if self.texto("rev-parse", "--is-bare-repository") != "true":
            raise FalloGit("repositorio-del-tren-debe-ser-bare")

    def traer(self, ref, timeout=120):
        self.texto("fetch", "--no-tags", "--no-recurse-submodules",
                   "--no-auto-maintenance", self.remoto, ref,
                   aislado=False, timeout=timeout)
        return self.texto("rev-parse", "--verify", "FETCH_HEAD^{commit}")

    def armar(self, B, miembros):
        """Fecha UTC = max(committer timestamp de B y todos los H) + índice (1..N).

        Los timestamps son parte de los objetos identificados por SHA; ningún
        reloj local, identidad/configuración del usuario o firma participa.
        """
        pr_actual = 0
        try:
            miembros = tuple(miembros)
            if (not sha(B) or not miembros or
                    any(type(pr) is not int or pr <= 0 or not sha(head)
                        for pr, head in miembros) or
                    len({pr for pr, _ in miembros}) != len(miembros)):
                return ErrorGit("entradas-invalidas")
            self.preparar()
            if self.traer(B) != B:
                return ErrorGit("base-no-coincide")
            for pr_actual, head in miembros:
                if self.traer("refs/pull/{}/head".format(pr_actual)) != head:
                    return ErrorGit("head-no-coincide", pr_actual)
            fecha = max(int(self.texto("show", "-s", "--format=%ct", commit))
                        for commit in (B,) + tuple(h for _, h in miembros))
            C = B
            eslabones = []
            for indice, (pr_actual, head) in enumerate(miembros, 1):
                fusion = self.git("-c", "core.attributesFile=" + os.devnull,
                    "-c", "merge.renormalize=false", "-c", "merge.renames=true",
                    "merge-tree", "--write-tree", "--name-only", "-z", C, head,
                    aislado=True)
                campos = fusion.stdout.split("\0")
                if fusion.returncode == 1:
                    # -z emits tree NUL paths NUL NUL structured conflict messages.
                    archivos = []
                    for archivo in campos[1:]:
                        if not archivo:
                            break
                        archivos.append(archivo)
                    return ConflictoCadena(pr_actual, tuple(archivos))
                if fusion.returncode or not sha(campos[0]):
                    return ErrorGit("merge-tree-fallido", pr_actual)
                arbol = campos[0]
                instante = "{} +0000".format(fecha + indice)
                identidad = {"GIT_AUTHOR_NAME": "PAO Tren",
                    "GIT_AUTHOR_EMAIL": "tren@example.invalid", "GIT_AUTHOR_DATE": instante,
                    "GIT_COMMITTER_NAME": "PAO Tren",
                    "GIT_COMMITTER_EMAIL": "tren@example.invalid", "GIT_COMMITTER_DATE": instante}
                mensaje = "pao-tren merge PR {}\n\nbase {}\nhead {}\neslabon {}\n".format(
                    pr_actual, B, head, indice)
                C = self.texto("-c", "commit.gpgsign=false", "-c", "i18n.commitEncoding=UTF-8",
                    "commit-tree", arbol, "-p", C, "-p", head, input=mensaje, env_extra=identidad)
                if not sha(C):
                    return ErrorGit("commit-tree-fallido", pr_actual)
                eslabones.append((pr_actual, head, C))
            self.texto("update-ref", "refs/tren/candidatos/" + C, C)
            return Candidato(B, tuple(eslabones), C, arbol)
        except (FalloGit, ValueError) as exc:
            return ErrorGit(str(exc) if isinstance(exc, FalloGit) else "entradas-invalidas", pr_actual)

    def leer_ref(self, ref):
        lectura = self.git("ls-remote", "--refs", self.remoto, ref)
        if lectura.returncode:
            raise FalloGit("lectura-remota-fallida")
        lineas = lectura.stdout.splitlines()
        if not lineas:
            return ""
        if len(lineas) != 1:
            raise FalloGit("lectura-remota-invalida")
        campos = lineas[0].split("\t")
        if len(campos) != 2 or campos[1] != ref or not sha(campos[0]):
            raise FalloGit("lectura-remota-invalida")
        return campos[0]

    def worktree(self, commit):
        """Checkout desacoplado durable por almacén/SHA; sin borrar ni moverlo."""
        if not sha(commit):
            raise FalloGit("worktree-sha-invalido")
        self.preparar()
        ruta = directorio_estado() / "worktrees" / clave(str(self.ruta.resolve())) / commit
        ruta.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not ruta.exists():
            self.texto("worktree", "add", "--detach", str(ruta), commit)
        def leer(*args):
            resultado = self.ejecutar(["git", "--no-replace-objects", "-c",
                "core.hooksPath=" + os.devnull, "-C", str(ruta), *args],
                capture_output=True, text=True, timeout=120, check=False,
                env=entorno(True))
            if resultado.returncode:
                raise FalloGit("worktree-no-valido")
            return resultado.stdout.strip()
        try:
            if (leer("rev-parse", "HEAD") != commit or
                    Path(leer("rev-parse", "--git-common-dir")).resolve() != self.ruta.resolve() or
                    leer("status", "--porcelain", "--untracked-files=no") or
                    leer("rev-parse", "--abbrev-ref", "HEAD") != "HEAD"):
                raise FalloGit("worktree-no-valido")
        except (OSError, subprocess.SubprocessError):
            raise FalloGit("worktree-no-disponible") from None
        return ruta

    def publicar(self, tren_id, C):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", tren_id) or not sha(C):
            return ErrorGit("publicacion-invalida")
        ref = "refs/heads/tren/" + tren_id
        try:
            self.preparar()
            actual = self.leer_ref(ref)
            if not actual:
                try:
                    self.git("push", "--porcelain", "--no-verify", "--no-follow-tags",
                        "--recurse-submodules=no", "--force-with-lease=" + ref + ":",
                        self.remoto, C + ":" + ref)
                except FalloGit:
                    pass
                actual = self.leer_ref(ref)
            if actual == C:
                return Publicado(ref, C)
            if actual:
                return RamaOcupada(ref, C, actual)
            return ErrorGit("publicacion-rechazada")
        except FalloGit as exc:
            return ErrorGit(str(exc))

    def verificar(self, B, C, arbol, heads):
        if not all(sha(s) for s in (B, C, arbol, *heads)):
            raise FalloGit("postcondicion-invalida")
        if self.texto("rev-parse", "--verify", C + "^{tree}") != arbol:
            raise FalloGit("arbol-no-coincide")
        for head in (B, *heads):
            self.texto("merge-base", "--is-ancestor", head, C)

    def reconciliar(self, B, C, arbol, heads):
        try:
            main = self.leer_ref("refs/heads/main")
            if main == C:
                self.verificar(B, C, arbol, tuple(heads))
                if self.texto("rev-parse", "--verify", main + "^{tree}") != arbol:
                    raise FalloGit("postcondicion-arbol")
                return Aterrizado(B, C, arbol)
            if main == B:
                return EnBase(B, C)
            if main:
                return EscritorExterno(B, C, main)
            return Incierto(B, C, "main-ausente")
        except FalloGit as exc:
            return Incierto(B, C, str(exc))

    def promover(self, B, C, arbol, heads):
        heads = tuple(heads)
        try:
            self.preparar()
            # A lease alone would allow a non-descendant C: prove fast-forward first.
            self.verificar(B, C, arbol, heads)
        except FalloGit as exc:
            return Incierto(B, C, str(exc))
        try:
            self.git("push", "--porcelain", "--no-verify", "--no-follow-tags",
                "--recurse-submodules=no", "--force-with-lease=refs/heads/main:" + B,
                self.remoto, C + ":refs/heads/main")
        except FalloGit:
            pass
        # Never infer success/failure from push's exit/ACK. Always read the remote.
        return self.reconciliar(B, C, arbol, heads)
