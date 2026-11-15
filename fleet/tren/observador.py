"""Lectura de GitHub/git. Evidencia estructurada y decisiones fail-closed."""
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
import json
from pathlib import Path
import re
import subprocess
from typing import Protocol, Tuple
from urllib.parse import urlparse

from dominio import EsperandoCI, Observacion, Rechazado, Terminal, VistaPR


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
    return config


def es_sensible(archivos, config):
    return any(fnmatchcase(archivo, patron)
               for patrones in config["sensibles"].values()
               for patron in patrones for archivo in archivos)


# U16. Conclusión de un check run terminado -> lectura; README «Salida del CI
# sobre C». Rojo sólo si el check corrió y juzgó el código. Lo que no deja
# veredicto espera: a los 60 min el tren pausa por infraestructura.
CONCLUSIONES = {
    "success": "verde", "neutral": "verde", "skipped": "verde",
    "failure": "rojo", "timed_out": "rojo",
    "cancelled": "pendiente", "action_required": "pendiente",
    "startup_failure": "pendiente", "stale": "pendiente",
}
ESTADOS_STATUS = {"success": "verde", "failure": "rojo",
                  "error": "pendiente", "pending": "pendiente"}


@dataclass(frozen=True)
class LecturaCI:
    valor: str
    rojos: Tuple[str, ...] = ()
    # Regla anterior a U16 (todo en success), que la sombra conserva.
    estricta: bool = False


def sha(valor):
    return isinstance(valor, str) and re.fullmatch(r"[0-9a-f]{40}", valor) is not None


class GitHubGit:
    def __init__(self, config, ejecutar=None, sombra=False, activo=False):
        self.config = config
        self.ejecutar = ejecutar or subprocess.run
        self.sombra = sombra
        self.activo = activo

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

    def ci(self, repo, head):
        lectura = self.lectura_ci(repo, head)
        if not self.activo:
            # Sombra y --seco conservan su lectura de dos valores: sus
            # admisiones y eventos no cambian con U16.
            return "verde" if lectura.estricta else "pendiente"
        return lectura.valor

    def lectura_ci(self, repo, head):
        # --slurp retains every page of both APIs, including >100 checks/statuses.
        paginas = self.api("repos/{}/commits/{}/check-runs?per_page=100".format(repo, head), True)
        # A re-run leaves the superseded run (often cancelled) on the same head;
        # only the newest run per check name decides.
        checks = {}
        for c in sorted((c for pagina in paginas for c in pagina["check_runs"]),
                        key=lambda c: c["id"]):
            checks[c["name"]] = c
        paginas_status = self.api("repos/{}/commits/{}/statuses?per_page=100".format(repo, head), True)
        statuses = [s for pagina in paginas_status for s in pagina]
        # Status API returns newest first. A newer pending status supersedes green.
        ultimos = {}
        for status in statuses:
            # D6 despierta al dueño; no es CI. Tras caducar, el head actual
            # debe poder reentregarse sin que nuestro aviso lo retenga solo.
            # Sólo la sombra con doble camino conserva la lectura histórica.
            # También el --seco sin doble camino usa el gate del tren activo.
            if not self.sombra and status["context"] == "pao-tren/resultado":
                continue
            ultimos.setdefault(status["context"], status["state"])
        esperados = self.config.get("checks_esperados")
        lecturas = []
        for nombre, c in checks.items():
            valor = (CONCLUSIONES.get(c["conclusion"], "pendiente")
                     if c["status"] == "completed" else "pendiente")
            # Un check exigido debe correr: neutral/skipped no acreditan que corrió.
            if valor == "verde" and c["conclusion"] != "success" and nombre in (esperados or ()):
                valor = "pendiente"
            lecturas.append((nombre, valor))
        lecturas += [(contexto, ESTADOS_STATUS.get(estado, "pendiente"))
                     for contexto, estado in ultimos.items()]
        rojos = tuple(sorted({nombre for nombre, valor in lecturas if valor == "rojo"}))
        if rojos:
            return LecturaCI("rojo", rojos)
        if esperados is not None and any(n not in checks for n in esperados):
            return LecturaCI("pendiente")
        # Zero checks counts as green only when the repo declares an empty CI.
        if not checks and not ultimos and esperados != []:
            return LecturaCI("pendiente")
        verde = all(v == "verde" for _, v in lecturas)
        return LecturaCI("verde" if verde else "pendiente", estricta=verde and all(
            c["conclusion"] == "success" for c in checks.values()))

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
            ancestro = self.git("merge-base", base, head)
            if not sha(ancestro):
                raise ValueError("ancestro inválido")
            # Legacy merge-tree is read-only (no --write-tree / object creation).
            fusion = self.git("merge-tree", ancestro, base, head)
            conflicto = bool(re.search(r"^\+?<<<<<<< ", fusion, re.MULTILINE))
            # Both sources must be certain. A stale/unavailable local object retains.
            if payload.get("mergeable") is not True:
                conflicto = True if payload.get("mergeable") is False else None
            vista = replace(vista,
                apto=apto.get("veredicto") == "APTO" and apto.get("head") == head,
                hallazgos=apto.get("hallazgos_abiertos")
                if type(apto.get("hallazgos_abiertos")) is int else None,
                dirigida=dirigida.get("head") == head and dirigida.get("resultado") == "limpio",
                ci=self.ci(repo, head), conflicto=conflicto,
                sensible=es_sensible(nombres, self.config),
                alto_riesgo=bool(etiquetas.intersection(
                    self.config.get("etiquetas_alto_riesgo", ["alto-riesgo"]))),
                verificado=apto.get("verificado") == "Verificado" and apto.get("head") == head,
                autorizado=head in self.config.get("heads_autorizados", []))
            # Re-read after all reads; a concurrent push cannot use stale evidence.
            final = self.api("repos/{}/pulls/{}".format(repo, pr))
            if final["head"]["sha"] != head:
                return replace(vista, head=final["head"]["sha"], completa=False)
            if (final["base"]["sha"] != base or final["base"]["ref"] != vista.base
                    or final["state"] != payload["state"]):
                return replace(vista, completa=False)
            return vista
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            return replace(vista, completa=False)

    def observar(self, estado, ahora):
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
                C = tren.fase.candidato.C
                try:
                    ci, rojos = self.ci(repo, C), ()
                    if ci == "rojo":
                        # `ci` decide; los nombres que viajan al dueño salen de
                        # una relectura, y si ya no es roja el tren espera.
                        lectura = self.lectura_ci(repo, C)
                        ci, rojos = ((lectura.valor, lectura.rojos) if lectura.valor == "rojo"
                                     else ("pendiente", ()))
                except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                    ci, rojos = "pendiente", ()
                observacion = replace(observacion, tren_id=tren.id, ci_candidato=ci,
                                      ci_recibo="github:" + C, ci_sha=C, ci_rojos=rojos)
        return observacion


class Falso:
    def __init__(self, observacion):
        self.observacion = observacion

    def observar(self, estado, ahora):
        return replace(self.observacion, ahora=ahora)
