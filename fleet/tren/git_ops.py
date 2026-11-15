"""Aterrizaje del tren: armar C, publicarlo para CI, CAS a main y reconciliar.

Opera sobre un clon bare propio del tren y un remoto explícito (URL o ruta).
Nunca toca índice ni working tree, nunca borra refs ni objetos y el único
push a main es el CAS con lease sobre B.
"""
from dataclasses import dataclass, replace
import os
from pathlib import Path
import re
import subprocess

from dominio import (Aterrizado, Candidato, ConflictoCadena, Congelado, EnBase,
                     EscritorExterno, EsperandoCI, Incierto, Observacion,
                     Promoviendo, Publicado, RamaOcupada, clave)

MAIN = "refs/heads/main"
VERSION_MINIMA = (2, 38)
IDENTIDAD = ("pao-tren", "pao-tren@tren.invalid")
# Repository-locating and env-injected config would bypass the explicit repo.
_ENTORNO_AJENO = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                  "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_COMMON_DIR",
                  "GIT_REPLACE_REF_BASE", "GIT_GRAFT_FILE", "GIT_SHALLOW_FILE")


class ErrorGit(Exception):
    """Fallo inesperado de git: no es evidencia sobre el remoto."""


class GitAntiguo(ErrorGit):
    """`merge-tree --write-tree` necesita git 2.38 o posterior."""


@dataclass(frozen=True)
class HeadNoCoincide:
    pr: int
    esperado: str
    observado: str


def sha40(valor):
    return isinstance(valor, str) and re.fullmatch(r"[0-9a-f]{40}", valor) is not None


def directorio_git(repo):
    raiz = Path(os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state"))
    return raiz / "pao-tren" / "git" / (clave("repo", repo) + ".git")


def mensaje(B, i, n, pr, head):
    return "Tren: merge #{} {}\n\nBase: {}\nEslabón: {}/{}\n".format(pr, head, B, i, n)


class GitTren:
    """`ejecutar` imita subprocess.run; las pruebas lo sustituyen."""

    def __init__(self, directorio, remoto, ejecutar=None):
        if not isinstance(remoto, str) or not remoto or remoto.startswith("-"):
            raise ValueError("remoto inválido")
        self.directorio = Path(directorio)
        self.remoto = remoto
        self.ejecutar = ejecutar or subprocess.run

    # -- entorno -----------------------------------------------------------

    def _entorno(self, construccion):
        env = {k: v for k, v in os.environ.items() if k not in _ENTORNO_AJENO}
        if construccion:
            # Building C ignores global, system and env-injected config:
            # same inputs, same SHA. Fetch/push keep user config (credentials).
            env = {k: v for k, v in env.items() if not k.startswith("GIT_CONFIG")}
            env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                       GIT_AUTHOR_NAME=IDENTIDAD[0], GIT_AUTHOR_EMAIL=IDENTIDAD[1],
                       GIT_COMMITTER_NAME=IDENTIDAD[0], GIT_COMMITTER_EMAIL=IDENTIDAD[1],
                       LC_ALL="C", TZ="UTC")
        return env

    def _git(self, args, construccion=True, entrada=None, extra_env=None, timeout=120):
        env = self._entorno(construccion)
        if extra_env:
            env.update(extra_env)
        orden = ["git", "--git-dir=" + str(self.directorio), "-c", "core.hooksPath=" + os.devnull,
                 *args]
        return self.ejecutar(orden, capture_output=True, encoding="utf-8", errors="replace",
                             timeout=timeout, env=env, input=entrada, check=False)

    def _ok(self, args, **kwargs):
        proc = self._git(args, **kwargs)
        if proc.returncode != 0:
            raise ErrorGit("git {} salió con {}".format(args[0], proc.returncode))
        return proc.stdout

    # -- preparación -------------------------------------------------------

    def version(self):
        proc = self.ejecutar(["git", "version"], capture_output=True, encoding="utf-8",
                             errors="replace", timeout=30, env=self._entorno(True), check=False)
        match = re.search(r"(\d+)\.(\d+)", proc.stdout or "")
        if proc.returncode != 0 or not match:
            raise ErrorGit("git no disponible")
        return int(match[1]), int(match[2])

    def preparar(self):
        version = self.version()
        if version < VERSION_MINIMA:
            raise GitAntiguo("git {}.{} < 2.38: falta merge-tree --write-tree".format(*version))
        self.directorio.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not (self.directorio / "HEAD").exists():
            proc = self.ejecutar(["git", "init", "--quiet", "--bare", str(self.directorio)],
                                 capture_output=True, encoding="utf-8", errors="replace",
                                 timeout=60, env=self._entorno(True), check=False)
            if proc.returncode != 0:
                raise ErrorGit("git init salió con {}".format(proc.returncode))

    def _traer(self, refspecs):
        # Explicit refspecs into refs/tren/*; no prune, tags, hooks or auto-gc.
        self._ok(["fetch", "--quiet", "--no-tags", "--no-prune", "--no-recurse-submodules",
                  "--no-write-fetch-head", "--no-auto-maintenance", self.remoto, *refspecs],
                 construccion=False, timeout=600)

    def _resolver(self, ref):
        proc = self._git(["rev-parse", "--verify", "--quiet", ref + "^{commit}"])
        valor = proc.stdout.strip()
        return valor if proc.returncode == 0 and sha40(valor) else None

    def _fecha(self, *commits):
        # Max committer time of the parents, in UTC: a pure function of the inputs
        # (content-addressed) and never older than either parent.
        tiempos = [int(self._ok(["show", "-s", "--format=%ct", c]).strip()) for c in commits]
        return "@{} +0000".format(max(tiempos))

    # -- armar -------------------------------------------------------------

    def armar(self, B, eslabones):
        """C_0 = B, C_i = merge(C_{i-1}, H_i). Devuelve Candidato, ConflictoCadena
        o HeadNoCoincide. No escribe índice, working tree ni el remoto."""
        if not sha40(B) or not eslabones or not all(
                type(pr) is int and pr > 0 and sha40(h) for pr, h in eslabones):
            raise ValueError("entradas de armado inválidas")
        self.preparar()
        self._traer(["+{}:refs/tren/main".format(MAIN)] +
                    ["+refs/pull/{0}/head:refs/tren/pull/{0}".format(pr) for pr, _ in eslabones])
        for pr, head in eslabones:
            observado = self._resolver("refs/tren/pull/{}".format(pr))
            if observado != head:
                return HeadNoCoincide(pr, head, observado or "")
        if self._resolver(B) != B:
            raise ErrorGit("B no disponible en el clon del tren")
        anterior, cadena = B, []
        for i, (pr, head) in enumerate(eslabones, 1):
            proc = self._git(["merge-tree", "--write-tree", "--name-only", "-z", "--no-messages",
                              anterior, head])
            partes = proc.stdout.split("\0")
            if proc.returncode == 1:
                archivos = tuple(sorted({p for p in partes[1:] if p}))
                return ConflictoCadena(B, pr, archivos)
            if proc.returncode != 0 or not sha40(partes[0]):
                raise ErrorGit("merge-tree salió con {}".format(proc.returncode))
            fecha = self._fecha(anterior, head)
            actual = self._ok(["commit-tree", "--no-gpg-sign", partes[0], "-p", anterior,
                               "-p", head], entrada=mensaje(B, i, len(eslabones), pr, head),
                              extra_env={"GIT_AUTHOR_DATE": fecha,
                                         "GIT_COMMITTER_DATE": fecha}).strip()
            if not sha40(actual):
                raise ErrorGit("commit-tree sin SHA")
            cadena.append((pr, head, actual))
            anterior = actual
        # A local ref keeps C reachable, so auto-gc never prunes the candidate.
        self._ok(["update-ref", "refs/tren/candidatos/" + anterior, anterior])
        return Candidato(B, tuple(cadena), anterior, self.arbol(anterior))

    def arbol(self, commit):
        valor = self._ok(["rev-parse", "--verify", commit + "^{tree}"]).strip()
        if not sha40(valor):
            raise ErrorGit("árbol inválido")
        return valor

    # -- remoto ------------------------------------------------------------

    def leer(self, ref):
        """SHA de `ref` en el remoto; "" si no existe; None si la lectura falla."""
        try:
            proc = self._git(["ls-remote", "--refs", self.remoto, ref], construccion=False,
                             timeout=120)
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        for linea in proc.stdout.splitlines():
            partes = linea.split("\t")
            if len(partes) == 2 and partes[1] == ref:
                return partes[0] if sha40(partes[0]) else None
        return ""

    def _push(self, C, ref, lease):
        # The only push shape: one explicit ref, always with a lease.
        if not sha40(C) or not (lease == "" or sha40(lease)):
            raise ValueError("push inválido")
        try:
            proc = self._git(["push", "--porcelain", "--no-verify", "--no-follow-tags",
                              "--no-signed", "--recurse-submodules=no",
                              "--force-with-lease={}:{}".format(ref, lease),
                              self.remoto, "{}:{}".format(C, ref)],
                             construccion=False, timeout=600)
            return proc.returncode
        except (OSError, subprocess.SubprocessError):
            return None

    def publicar(self, tren_id, C):
        """Empuja C a refs/heads/tren/<id> sólo si la rama no existe."""
        if not re.fullmatch(r"[0-9a-f]{64}", tren_id or ""):
            raise ValueError("id de tren inválido")
        ref = "refs/heads/tren/" + tren_id
        self.preparar()
        actual = self.leer(ref)
        if actual == "":
            self._push(C, ref, "")
            actual = self.leer(ref)
        if actual == C:
            return Publicado(C)
        if actual:
            return RamaOcupada(C, actual)
        raise ErrorGit("lectura de la rama del tren fallida")

    def promover(self, B, C, arbol, heads):
        """CAS main: B -> C. Cualquier salida del push termina en reconciliar."""
        if not sha40(B):
            raise ValueError("B inválido")
        self.preparar()
        self._push(C, MAIN, B)
        return self.reconciliar(B, C, arbol, heads)

    def reconciliar(self, B, C, arbol, heads):
        main = self.leer(MAIN)
        if main is None:
            return Incierto("lectura-main")
        if main == B:
            return EnBase(B)
        if main != C:
            return EscritorExterno(main or "0" * 40)
        try:
            if self.arbol(C) != arbol:
                return Incierto("arbol-distinto")
            for head in heads:
                if self._git(["merge-base", "--is-ancestor", head, C]).returncode != 0:
                    return Incierto("head-no-ancestro")
        except (ErrorGit, OSError, subprocess.SubprocessError):
            return Incierto("objetos-locales")
        return Aterrizado(C)


@dataclass(frozen=True)
class Ejecucion:
    observacion: Observacion
    completo: bool


class Ejecutor:
    """Ejecuta armar-candidato, solicitar-ci y promover. Devuelve la observación
    para el siguiente paso; `completo` dice si el outbox puede cerrarlo."""

    def __init__(self, git):
        self.git = git

    def ejecutar(self, estado, efecto, observacion):
        # type: (...) -> Ejecucion
        tren = next((t for t in estado.trenes if t.id == efecto.datos[0]), None)
        if tren is None:
            return Ejecucion(observacion, False)
        por_recibo = {e.entrega.recibo: e.entrega for e in estado.entradas}
        eslabones = tuple((por_recibo[r].pr, por_recibo[r].head) for r in tren.recibos)
        fase = tren.fase
        try:
            if efecto.tipo == "armar-candidato":
                if not isinstance(fase, Congelado):
                    return Ejecucion(observacion, True)
                resultado = self.git.armar(fase.B, eslabones)
                if isinstance(resultado, HeadNoCoincide):
                    # GitHub's PR view decides head changes; a lagging ref retries.
                    return Ejecucion(observacion, False)
                return Ejecucion(replace(observacion, candidato=resultado), True)
            if efecto.tipo == "solicitar-ci":
                if not isinstance(fase, EsperandoCI):
                    return Ejecucion(observacion, True)
                resultado = self.git.publicar(tren.id, fase.candidato.C)
                return Ejecucion(replace(observacion, publicacion=resultado), True)
            if efecto.tipo == "promover":
                if not isinstance(fase, Promoviendo):
                    # Never replay a push for a train that already left Promoviendo.
                    return Ejecucion(observacion, True)
                cert = fase.certificado
                resultado = self.git.promover(cert.B, cert.C, cert.arbol,
                                              tuple(h for _, h in eslabones))
                return Ejecucion(replace(observacion, promocion=resultado),
                                 not isinstance(resultado, Incierto))
        except (ErrorGit, OSError, subprocess.SubprocessError):
            return Ejecucion(observacion, False)
        raise ValueError("efecto no ejecutable aquí: " + efecto.tipo)
