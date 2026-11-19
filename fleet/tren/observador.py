"""Lectura de GitHub/git. Evidencia estructurada y decisiones fail-closed."""
from dataclasses import replace
from fnmatch import fnmatchcase
import json
from pathlib import Path
import re
import subprocess
import time
from typing import Protocol
from urllib.parse import urlparse

from dominio import (EsperandoCI, Observacion, Rechazado, ResultadoCI, Terminal,
                     VistaPR, rama_ci, recibo_ci)
from git_ops import FalloGit, identidad_commits
from preaterrizaje import autorizaciones


CONCLUSIONES_CI = {
    "success": "verde",
    "failure": "rojo", "timed_out": "rojo",
    "cancelled": "pendiente", "action_required": "pendiente",
    "startup_failure": "pendiente", "stale": "pendiente",
    "skipped": "verde", "neutral": "verde",
}
STATUS_CI = {"success": "verde", "failure": "rojo", "error": "pendiente",
             "pending": "pendiente"}
# Tiempo total de fetch de una observación activa, sumado sobre toda la cola:
# una caída del remoto no suma un timeout por PR bajo el candado del carril.
PRESUPUESTO_TRANSPORTE_S = 120


class Observador(Protocol):
    def observar(self, estado, ahora):
        # type: (...) -> Observacion
        ...


def configuracion(ruta, repo):
    contenido = json.loads(Path(ruta).read_text())
    config = contenido["repos"][repo]
    # All three categories are mandatory; an omitted category cannot bypass a gate.
    sensibles = config["sensibles"]
    for categoria in ("ci", "gobierno", "lockfiles"):
        patrones = sensibles[categoria]
        if not isinstance(patrones, list) or not patrones or not all(
                isinstance(p, str) and p for p in patrones):
            raise ValueError("patrones sensibles incompletos")
    if not isinstance(config.get("git"), str):
        raise ValueError("falta ruta git local")
    if not isinstance(config.get("excluir_alto_riesgo", True), bool):
        raise ValueError("política de piloto inválida")
    from operador import checkout_configurado
    checkout_configurado(config)
    identidad_commits(config)
    return config


def es_sensible(archivos, config):
    return any(fnmatchcase(archivo, patron)
               for patrones in config["sensibles"].values()
               for patron in patrones for archivo in archivos)


def sha(valor):
    return isinstance(valor, str) and re.fullmatch(r"[0-9a-f]{40}", valor) is not None


class GitHubGit:
    def __init__(self, config, ejecutar=None, sombra=False, activo=False, almacen=None):
        # La sombra y --seco nunca traen objetos: sólo el tick activo recibe almacén.
        if almacen is not None and (sombra or not activo):
            raise ValueError("almacén sólo para el tick activo")
        self.config = config
        self.ejecutar = ejecutar or subprocess.run
        self.sombra = sombra
        self.activo = activo
        self.almacen = almacen
        self.reloj = time.monotonic
        self.reiniciar_transporte()

    def reiniciar_transporte(self):
        self.preparado = False
        self.transporte_s = 0.0
        self.caido = False

    def comando(self, args):
        return self.ejecutar(args, check=True, capture_output=True, text=True,
                            timeout=60).stdout

    def api(self, endpoint, paginado=False):
        args = ["gh", "api", "--method", "GET", endpoint]
        if paginado:
            args += ["--paginate", "--slurp"]
        valor = json.loads(self.comando(args))
        return valor

    def git(self, *args):
        return self.comando(["git", "-C", self.config["git"], *args]).strip()

    def lector(self, pr, base, head):
        """Dónde leer merge-base/merge-tree de la admisión.

        Sombra, `entregar` y `--seco` leen config["git"] sin fetch. El tick
        activo lee el bare del tren, el mismo GitTren que arma y promueve bajo
        el candado del carril, y trae sólo lo que falte: la base por SHA y el
        head por la ref del PR. Así ve el C que acaba de aterrizar y el main
        ajeno tras reanudar, sin tocar el checkout humano de config["git"].
        Un fallo lanza FalloGit: la vista queda incompleta y el tick reintenta.
        Los fetch de una observación comparten PRESUPUESTO_TRANSPORTE_S. Tras
        el primer fetch fallido, de la base o de un head, el remoto se da por
        caído: ninguna otra vista trae en esta observación. Un fetch que trae
        otro SHA es propio de ese PR y no corta los demás.
        """
        if self.almacen is None:
            return self.git
        if not self.preparado:
            self.almacen.preparar()
            self.preparado = True
        for objeto, ref in ((base, base), (head, "refs/pull/{}/head".format(pr))):
            if not self.almacen.git("cat-file", "-e", objeto + "^{commit}",
                                    aislado=True).returncode:
                continue
            restante = PRESUPUESTO_TRANSPORTE_S - self.transporte_s
            if self.caido or restante <= 0:
                raise FalloGit("transporte-agotado")
            inicio = self.reloj()
            try:
                traido = self.almacen.traer(ref, timeout=restante)
            except FalloGit:
                self.caido = True
                raise
            finally:
                self.transporte_s += self.reloj() - inicio
            if traido != objeto:
                raise FalloGit("objeto-no-disponible")
        return self.almacen.texto

    def evidencia(self, referencia, repo, pr):
        if referencia.startswith("https://"):
            url = urlparse(referencia)
            match = re.fullmatch(r"/" + re.escape(repo) + r"/pull/" + str(pr), url.path)
            comentario = re.fullmatch(r"issuecomment-(\d+)", url.fragment)
            if url.netloc != "github.com" or not match or not comentario or url.query:
                raise ValueError("URL de evidencia no soportada")
            respuesta = self.api("repos/{}/issues/comments/{}".format(repo, comentario[1]))
            # An issue comment URL is not sufficient: verify its actual PR binding.
            if respuesta.get("issue_url", "").rsplit("/", 1)[-1] != str(pr):
                raise ValueError("comentario ajeno al PR")
            return json.loads(respuesta["body"])
        return json.loads(Path(referencia).read_text())

    def fallo_actions(self, repo, head, check):
        """Una sola lectura del job; sin pasos verificables no atribuir rojo."""
        try:
            url = urlparse(check.get("details_url") or "")
            job = re.fullmatch(r"/" + re.escape(repo) +
                               r"/actions/runs/\d+/job/(\d+)", url.path)
            if url.scheme != "https" or url.netloc != "github.com" or not job:
                return "pendiente"
            datos = self.api("repos/{}/actions/jobs/{}".format(repo, job[1]))
            if (datos["head_sha"] != head or datos["check_run_url"] !=
                    "https://api.github.com/repos/{}/check-runs/{}".format(repo, check["id"])):
                return "pendiente"
            pasos = sorted(datos["steps"], key=lambda p: p["number"])
            if not pasos:
                return "pendiente"
            workflow_iniciado = False
            for p in pasos:
                if p["status"] != "completed" or p["conclusion"] is None:
                    return "pendiente"
                if p["name"] in ("Set up job", "Set up runner"):
                    if p["conclusion"] == "failure" and not workflow_iniciado:
                        # Los fallos sintéticos posteriores no prueban ejecución.
                        return "pendiente"
                elif p["name"] != "Complete job" and p["conclusion"] != "skipped":
                    workflow_iniciado = True
            return "rojo" if workflow_iniciado else "pendiente"
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            return "pendiente"

    def ci(self, repo, head, rama=None):
        # --slurp retains every page of both APIs, including >100 checks/statuses.
        endpoint = "repos/{}/commits/{}/check-runs?per_page=100".format(repo, head)
        # latest se filtra por completed_at en GitHub: una finalización vieja
        # tardía no debe ocultar los checks de la ref que acabamos de solicitar.
        paginas = self.api(endpoint + ("&filter=all" if rama is not None else ""), True)
        suites = None
        if rama is not None:
            # El check-run sólo garantiza check_suite.id; head_branch pertenece
            # al recurso check-suite. No inferir una ref a partir del SHA.
            paginas_suites = self.api(
                "repos/{}/commits/{}/check-suites?per_page=100".format(repo, head), True)
            suites = {s["id"] for p in paginas_suites for s in p["check_suites"]
                      if s["head_sha"] == head and s["head_branch"] == rama}
        # A re-run leaves the superseded run (often cancelled) on the same head;
        # only the newest run per check name decides.
        checks = {}
        for c in sorted((c for pagina in paginas for c in pagina["check_runs"]),
                        key=lambda c: c["id"]):
            if not self.sombra and c["name"] == "pao-tren/resultado":
                continue
            if suites is not None and (c.get("head_sha") != head or
                    (c.get("check_suite") or {}).get("id") not in suites):
                continue
            checks[c["name"]] = c
        paginas_status = self.api("repos/{}/commits/{}/statuses?per_page=100".format(repo, head), True)
        statuses = [s for pagina in paginas_status for s in pagina]
        # Status API returns newest first. A newer pending status supersedes green.
        ultimos = {}
        for status in statuses:
            # Los commit statuses no identifican la ref que los solicitó.
            if rama is not None:
                continue
            # D6 despierta al dueño; no es CI. Tras caducar, el head actual
            # debe poder reentregarse sin que nuestro aviso lo retenga solo.
            # Sólo la sombra con doble camino conserva la lectura histórica.
            # --seco sin doble camino conserva la exclusión de este contexto.
            if not self.sombra and status["context"] == "pao-tren/resultado":
                continue
            ultimos.setdefault(status["context"], status["state"])
        esperados = self.config.get("checks_esperados")
        faltan = esperados is not None and any(n not in checks for n in esperados)
        # Sólo el tick activo interpreta la tabla U16. Sombra y --seco
        # conservan success como único verde, incluido el doble camino.
        if self.sombra or not self.activo:
            verde = (not faltan and
                     (bool(checks or ultimos) or esperados == []) and
                     all(c["status"] == "completed" and c.get("conclusion") == "success"
                         for c in checks.values()) and
                     all(s == "success" for s in ultimos.values()))
            return ResultadoCI("verde" if verde else "pendiente")
        valores = {n: CONCLUSIONES_CI.get(c.get("conclusion"), "pendiente")
                   if c["status"] == "completed" else "pendiente"
                   for n, c in checks.items()}
        for n, c in checks.items():
            if (valores[n] == "rojo" and c.get("conclusion") == "failure" and
                    (c.get("app") or {}).get("slug") == "github-actions"):
                valores[n] = self.fallo_actions(repo, head, c)
        estados = {n: STATUS_CI.get(s, "pendiente") for n, s in ultimos.items()}
        rojos = tuple(sorted({n for n, v in list(valores.items()) + list(estados.items())
                              if v == "rojo"}))
        # Un esperado ausente impide verde, pero no oculta un rojo presente.
        if rojos:
            return ResultadoCI("rojo", rojos)
        if faltan or (not checks and not ultimos and (esperados != [] or rama is not None)):
            return ResultadoCI("pendiente")
        return ResultadoCI("verde" if all(v == "verde" for v in valores.values())
                           and all(v == "verde" for v in estados.values()) else "pendiente")

    def vista(self, entrega):
        repo, pr = entrega.repo, entrega.pr
        payload = self.api("repos/{}/pulls/{}".format(repo, pr))
        head = payload["head"]["sha"]
        if not sha(head):
            raise ValueError("head inválido")
        vista = VistaPR(pr, head, base=payload["base"]["ref"],
                        abierto=payload["state"] == "open", recibo=entrega.recibo,
                        mergeado=payload.get("merged") is True)
        if head != entrega.head or vista.base != "main" or not vista.abierto:
            return vista
        try:
            archivos = self.api("repos/{}/pulls/{}/files?per_page=100".format(repo, pr), True)
            nombres = [a["filename"] for pagina in archivos for a in pagina]
            # GitHub caps this endpoint at 3000; never classify a truncated PR.
            if len(nombres) != payload["changed_files"]:
                raise ValueError("archivos incompletos")
            apto = self.evidencia(entrega.apto, repo, pr)
            dirigida = self.evidencia(entrega.dirigida, repo, pr)
            if not isinstance(apto, dict) or not isinstance(dirigida, dict):
                raise ValueError("evidencia debe ser objeto JSON")
            etiquetas = {l["name"] for l in payload["labels"]}
            base = payload["base"]["sha"]
            if not sha(base):
                raise ValueError("base inválida")
            leer = self.lector(pr, base, head)
            ancestro = leer("merge-base", base, head)
            if not sha(ancestro):
                raise ValueError("ancestro inválido")
            # Legacy merge-tree is read-only (no --write-tree / object creation).
            fusion = leer("merge-tree", ancestro, base, head)
            conflicto = bool(re.search(r"^\+?<<<<<<< ", fusion, re.MULTILINE))
            # Both sources must be certain. A stale/unavailable local object retains.
            if payload.get("mergeable") is not True:
                conflicto = True if payload.get("mergeable") is False else None
            autorizados = (autorizaciones(self.config) if self.activo else
                           self.config.get("heads_autorizados", []))
            vista = replace(vista,
                apto=apto.get("veredicto") == "APTO" and apto.get("head") == head,
                hallazgos=apto.get("hallazgos_abiertos")
                if type(apto.get("hallazgos_abiertos")) is int else None,
                dirigida=dirigida.get("head") == head and dirigida.get("resultado") == "limpio",
                ci=self.ci(repo, head).estado, conflicto=conflicto,
                sensible=es_sensible(nombres, self.config),
                alto_riesgo=bool(etiquetas.intersection(
                    self.config.get("etiquetas_alto_riesgo", ["alto-riesgo"]))),
                verificado=apto.get("verificado") == "Verificado" and apto.get("head") == head,
                autorizado=head in autorizados,
                ventana=autorizados.get(head) if self.activo else None)
            # Re-read after all reads; a concurrent push cannot use stale evidence.
            final = self.api("repos/{}/pulls/{}".format(repo, pr))
            if final["head"]["sha"] != head:
                return replace(vista, head=final["head"]["sha"], completa=False)
            if (final["base"]["sha"] != base or final["base"]["ref"] != vista.base
                    or final["state"] != payload["state"]):
                return replace(vista, completa=False)
            return vista
        except (FalloGit, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            return replace(vista, completa=False)

    def observar(self, estado, ahora):
        # El bare se valida y el presupuesto de fetch corre una vez por
        # observación, no por PR: el tick siguiente reintenta desde cero.
        self.reiniciar_transporte()
        repo = estado.repo
        rama = self.api("repos/{}/git/ref/heads/main".format(repo))
        base = rama["object"]["sha"]
        if not sha(base):
            raise ValueError("main inválido")
        vistas = []
        terminales = {t.id for t in estado.trenes if isinstance(t.fase, Terminal)}
        for entrada in estado.entradas:
            # En sombra se sigue un rechazo hasta observar su posible merge externo.
            if ((self.sombra and entrada.terminal) or
                    (isinstance(entrada.admision, Rechazado) and not self.sombra) or
                    (self.activo and entrada.tren in terminales)):
                continue
            try:
                vistas.append(self.vista(entrada.entrega))
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                vistas.append(VistaPR(entrada.entrega.pr, entrada.entrega.head,
                                      completa=False, recibo=entrada.entrega.recibo))
        observacion = Observacion(ahora, base, tuple(vistas),
                                 self.config.get("excluir_alto_riesgo", True),
                                 costo_suite_sana_s=self.config.get("costo_suite_sana_s", 0)
                                 if self.activo else 0)
        if self.activo:
            tren = next((t for t in estado.trenes if isinstance(t.fase, EsperandoCI)), None)
            if tren:
                # La publicación sólo solicita CI; leer checks sobre el C exacto.
                # GitHub no conoce un C aún no publicado: CI pendiente, y el
                # tick sigue hasta reintentar la publicación.
                try:
                    ci = self.ci(repo, tren.fase.candidato.C,
                                 "tren/" + rama_ci(tren) if tren.fase.solicitud else None)
                except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                    ci = ResultadoCI("pendiente")
                observacion = replace(observacion, tren_id=tren.id, ci_candidato=ci.estado,
                                      ci_rojos=ci.rojos,
                                      ci_recibo=recibo_ci(tren))
        return observacion


class Falso:
    def __init__(self, observacion):
        self.observacion = observacion

    def observar(self, estado, ahora):
        return replace(self.observacion, ahora=ahora)
