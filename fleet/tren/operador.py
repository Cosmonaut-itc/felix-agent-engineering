"""Worktrees del operador fijados al main que leyó el tren."""
import os
from pathlib import Path
import shutil
import subprocess

from dominio import Terminal, clave
from git_ops import FalloGit, entorno, sha


class FalloSnapshot(Exception):
    """El detalle es un código público, sin rutas ni salida de Git."""


def checkout_configurado(config):
    if "checkout_operador" not in config:
        return None
    ruta = config["checkout_operador"]
    if not isinstance(ruta, str) or "\0" in ruta or not Path(ruta).is_absolute():
        raise ValueError("checkout_operador debe ser ruta absoluta")
    return Path(ruta)


class Snapshots:
    def __init__(self, ledger, repo, git, config):
        self.ledger, self.repo, self.git, self.config = ledger, repo, git, config
        self.checkout = checkout_configurado(config)
        self.raiz = ledger.directorio.resolve() / "snapshots-operador" / clave("repo", repo)
        self.punta = None
        self.lanzador_configurado = config.get("lanzador") or config.get("lanzador_hasito")

    def entorno_lanzador(self, ruta):
        # Los módulos Python importados por el lanzador viven en el worktree.
        if Path(ruta).is_relative_to(self.raiz):
            return dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        return None

    def comando_git(self, ruta, *args, verificar=True):
        env = dict(entorno(True), GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
        comando = ["git", "--no-replace-objects", "-c", "core.hooksPath=" + os.devnull,
                   "-c", "core.fsmonitor=false", "-c", "core.symlinks=true", "-C", str(ruta)]
        fd = self.ledger.fd_candado
        r = subprocess.run([*comando, *args], capture_output=True, text=True,
                           timeout=120, check=False, env=env, pass_fds=() if fd is None else (fd,))
        if verificar and r.returncode:
            raise FalloSnapshot("git-del-operador")
        return r

    def main(self):
        if self.punta is None:
            try:
                self.punta = self.git.leer_ref("refs/heads/main")
            except FalloGit:
                raise FalloSnapshot("main-no-disponible") from None
        if not sha(self.punta):
            raise FalloSnapshot("main-no-disponible")
        return self.punta

    def traer(self, checkout, commit):
        ref = "refs/pao-tren/operador/" + commit
        objeto = commit + "^{commit}"
        if self.comando_git(checkout, "rev-parse", "--verify", objeto, verificar=False).returncode:
            try:
                self.git.preparar()
                if self.git.git("rev-parse", "--verify", objeto, aislado=True).returncode:
                    self.git.texto("fetch", "--no-tags", "--no-recurse-submodules",
                        "--no-auto-maintenance", "--no-write-fetch-head", self.git.remoto,
                        commit + ":" + ref, aislado=False)
                else:
                    self.git.texto("update-ref", ref, commit)
                # fetch-pack habla con upload-pack por la ruta local. No consulta
                # remotos ni aplica las reglas url.*.insteadOf del operador.
                self.comando_git(checkout, "fetch-pack", "--no-progress",
                                 str(self.git.ruta.resolve()), ref)
            except (FalloGit, FalloSnapshot):
                raise FalloSnapshot("sha-no-disponible") from None
        if self.comando_git(checkout, "rev-parse", "--verify", objeto).stdout.strip() != commit:
            raise FalloSnapshot("sha-no-disponible")
        self.comando_git(checkout, "update-ref", ref, commit)

    def relativa(self, valor, checkout):
        if not isinstance(valor, str) or not Path(valor).is_absolute():
            return None
        for raiz in (self.checkout, checkout):
            ruta = Path(valor)
            if ruta.is_relative_to(raiz):
                return ruta.relative_to(raiz)
        return None

    def mapear(self, valor, checkout, arbol):
        relativa = self.relativa(valor, checkout)
        if relativa is None:
            return valor
        ruta = arbol / relativa
        try:
            destino = ruta.resolve()
            if not destino.is_relative_to(arbol):
                raise FalloSnapshot("ruta-fuera-del-snapshot")
            if not destino.exists():
                raise FalloSnapshot("ruta-ausente-del-snapshot")
        except (OSError, RuntimeError):
            raise FalloSnapshot("ruta-no-verificable") from None
        return str(ruta)

    def limpio(self, arbol):
        return not self.comando_git(
            arbol, "status", "--porcelain", "--untracked-files=all", "--ignored").stdout

    def verificar_arbol(self, arbol, checkout, commit):
        def leer(ruta, *args):
            return self.comando_git(ruta, *args).stdout.strip()

        raiz = Path(leer(arbol, "rev-parse", "--show-toplevel")).resolve()
        if (raiz != arbol or leer(arbol, "rev-parse", "HEAD") != commit or
                leer(arbol, "rev-parse", "--abbrev-ref", "HEAD") != "HEAD" or
                leer(arbol, "rev-parse", "--path-format=absolute", "--git-common-dir") !=
                leer(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir") or
                not self.limpio(arbol)):
            raise FalloSnapshot("snapshot-no-verificable")

    def descartar_incompleto(self, checkout, arbol):
        # La ruta está reservada por el tren. Git puede morir antes de registrar
        # el worktree, o después de registrarlo y antes de terminar el checkout.
        registrados = self.comando_git(checkout, "worktree", "list", "--porcelain", "-z").stdout
        if "worktree " + str(arbol) in registrados.split("\0"):
            self.comando_git(checkout, "worktree", "remove", "--force", str(arbol))
        elif arbol.is_dir() and not arbol.is_symlink():
            shutil.rmtree(arbol)
        else:
            arbol.unlink(missing_ok=True)

    def preparar(self, commit, lanzador=None):
        try:
            checkout = self.checkout.resolve()
            raiz = self.comando_git(checkout, "rev-parse", "--show-toplevel").stdout.strip()
            if Path(raiz).resolve() != checkout:
                raise FalloSnapshot("checkout-no-valido")
            arbol = self.raiz / clave(str(checkout)) / commit
            completo = self.ledger.db.execute(
                "SELECT 1 FROM snapshots_operador WHERE ruta=?", (str(arbol),)).fetchone()
            if not completo:
                arbol.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                self.descartar_incompleto(checkout, arbol)
                self.traer(checkout, commit)
                self.comando_git(checkout, "worktree", "add", "--detach", str(arbol), commit)
            self.verificar_arbol(arbol, checkout, commit)
            if lanzador is not None:
                self.mapear(lanzador, checkout, arbol)
            gancho = self.config.get("pre_aterrizaje")
            if isinstance(gancho, dict) and isinstance(gancho.get("comando"), list):
                for arg in gancho["comando"]:
                    self.mapear(arg, checkout, arbol)
            if not completo:
                self.ledger.db.execute("INSERT INTO snapshots_operador VALUES (?, ?, ?)",
                                       (str(arbol), self.repo, str(checkout)))
            return checkout, arbol
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            raise FalloSnapshot("snapshot-no-disponible") from None

    def lanzador(self, ruta, tren):
        checkout, arbol = self.preparar(self.main(), ruta)
        lanzador = self.mapear(ruta, checkout, arbol)
        if lanzador != ruta:
            # La referencia precede a escribir el argv y a toda ejecución.
            self.ledger.db.execute("INSERT OR IGNORE INTO snapshots_usados VALUES (?, ?)",
                                   (str(arbol), tren))
        return lanzador

    def gancho(self, config, commit):
        checkout, arbol = self.preparar(commit, self.lanzador_configurado)
        return dict(config, comando=[self.mapear(arg, checkout, arbol)
                                     for arg in config["comando"]]), str(arbol)

    def validar_guardado(self, *argv):
        """Comprueba el worktree registrado sólo si el argv requiere ejecutarlo."""
        arboles = set()
        try:
            for arg in argv:
                ruta = Path(arg)
                if not ruta.is_relative_to(self.raiz):
                    continue
                relativa = ruta.relative_to(self.raiz)
                if len(relativa.parts) < 2:
                    raise FalloSnapshot("snapshot-no-verificable")
                arbol = self.raiz.joinpath(*relativa.parts[:2])
                if arbol not in arboles:
                    fila = self.ledger.db.execute(
                        "SELECT checkout FROM snapshots_operador WHERE ruta=? AND repo=?",
                        (str(arbol), self.repo)).fetchone()
                    if fila is None:
                        raise FalloSnapshot("snapshot-no-verificable")
                    self.verificar_arbol(arbol, Path(fila[0]), arbol.name)
                    arboles.add(arbol)
                if not ruta.resolve(strict=True).is_relative_to(arbol):
                    raise FalloSnapshot("ruta-fuera-del-snapshot")
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            raise FalloSnapshot("snapshot-no-verificable") from None

    def limpiar(self, estado, diarios):
        filas = self.ledger.db.execute("SELECT ruta, checkout FROM snapshots_operador "
                                       "WHERE repo=? ORDER BY rowid DESC", (self.repo,)).fetchall()
        if len(filas) <= 1:
            return
        vivos = {t.id for t in estado.trenes if not isinstance(t.fase, Terminal)}
        usadas = {d.get("snapshot") for d in diarios}
        usadas.update(ruta for ruta, tren in self.ledger.db.execute(
            "SELECT s.ruta, u.tren FROM snapshots_operador s JOIN snapshots_usados u "
            "ON u.ruta=s.ruta WHERE s.repo=?", (self.repo,)) if tren in vivos)
        for ruta, checkout in filas[1:]:
            if ruta in usadas:
                continue
            arbol = Path(ruta)
            try:
                if arbol.exists():
                    if not self.limpio(arbol):
                        continue
                    self.comando_git(checkout, "worktree", "remove", ruta)
                self.comando_git(checkout, "worktree", "prune")
            except (FalloSnapshot, OSError, ValueError, subprocess.SubprocessError):
                continue
            self.ledger.db.execute("DELETE FROM snapshots_operador WHERE ruta=?", (ruta,))
