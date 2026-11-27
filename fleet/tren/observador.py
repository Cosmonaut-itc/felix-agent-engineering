"""Lectura de GitHub/git. Evidencia estructurada y decisiones fail-closed."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from fnmatch import fnmatchcase
from functools import partial
import json
from pathlib import Path
import re
import subprocess
import time
from typing import Protocol
from urllib.parse import urlparse

from dominio import (Certificado, Diagnosticando, EsperandoCI, Observacion, Promoviendo, Rechazado,
                     ResultadoCI, Terminal, VistaPR, certificado_de, recibo_ci)
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
ERRORES_LECTURA = (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError)
SIN_LEER = object()


class LimiteGitHub(Exception):
    def __init__(self, recurso, reinicio):
        super().__init__(recurso, reinicio)
        self.recurso = recurso
        self.reinicio = reinicio


def segundos(valor):
    try:
        numero = int(valor)
        if 0 <= numero <= 253402300799:
            return numero
    except (TypeError, ValueError):
        pass
    return None


def limite_http(codigo, cabeceras, mensaje):
    if codigo not in (403, 429):
        return None
    agotado = cabeceras.get("x-ratelimit-remaining") == "0"
    if codigo == 403 and not (agotado or "retry-after" in cabeceras or "rate limit" in mensaje):
        return None
    recurso = cabeceras.get("x-ratelimit-resource", "core")
    if recurso not in ("core", "graphql"):
        recurso = "core"
    if "secondary rate limit" in mensaje or (not agotado and
            ("retry-after" in cabeceras or codigo == 429)):
        recurso = "secondary"
    reinicio = segundos(cabeceras.get("x-ratelimit-reset"))
    espera = segundos(cabeceras.get("retry-after"))
    if espera is not None:
        reinicio = min(int(time.time()) + espera, 253402300799)
    elif reinicio is None or (recurso == "secondary" and not agotado):
        # GitHub indica esperar al menos un minuto si el límite secundario no da plazo.
        reinicio = int(time.time()) + 60
    return LimiteGitHub(recurso, reinicio)


def siguiente_pagina(link):
    siguiente = re.search(r'<([^>]+)>\s*;\s*rel="next"', link)
    if siguiente is not None:
        url = urlparse(siguiente[1])
        if url.scheme == "https" and url.netloc == "api.github.com" and url.path:
            return url._replace(scheme="", netloc="", fragment="").geturl().lstrip("/")
        raise ValueError("página siguiente inválida")
    return None


class Observador(Protocol):
    def observar(self, estado, ahora):
        # type: (...) -> Observacion
        ...


def configuracion(ruta, repo):
    contenido = json.loads(Path(ruta).read_text())
    config = contenido["repos"][repo]
    # All three categories are mandatory; an omitted category cannot bypass a gate.
    sensibles = config["sensibles"]
    if not isinstance(sensibles, dict) or not {"ci", "gobierno", "lockfiles"} <= set(sensibles):
        raise ValueError("patrones sensibles incompletos")
    for patrones in sensibles.values():
        if not isinstance(patrones, list) or not patrones or not all(
                isinstance(p, str) and p for p in patrones):
            raise ValueError("patrones sensibles incompletos")
    sensibles_en_lote = config.get("sensibles_en_lote", [])
    if not isinstance(sensibles_en_lote, list) or not all(
            isinstance(c, str) and c in sensibles for c in sensibles_en_lote):
        raise ValueError("sensibles_en_lote debe listar categorías de sensibles")
    if not isinstance(config.get("git"), str):
        raise ValueError("falta ruta git local")
    if not isinstance(config.get("excluir_alto_riesgo", True), bool):
        raise ValueError("política de piloto inválida")
    if not isinstance(config.get("alto_riesgo_en_lote", False), bool):
        raise ValueError("alto_riesgo_en_lote debe ser booleano")
    from operador import checkout_configurado
    checkout_configurado(config)
    identidad_commits(config)
    return config


def categorias_sensibles(archivos, config):
    return {categoria for categoria, patrones in config["sensibles"].items()
            if any(fnmatchcase(archivo, patron)
                   for patron in patrones for archivo in archivos)}


def sha(valor):
    return isinstance(valor, str) and re.fullmatch(r"[0-9a-f]{40}", valor) is not None


class GitHubGit:
    def __init__(self, config, ejecutar=None, sombra=False, activo=False, almacen=None,
                 paralelas=1, cache=None):
        # La sombra y --seco nunca traen objetos: sólo el tick activo recibe almacén.
        if almacen is not None and (sombra or not activo):
            raise ValueError("almacén sólo para el tick activo")
        self.config = config
        self.ejecutar = ejecutar or subprocess.run
        self.sombra = sombra
        self.activo = activo
        self.almacen = almacen
        self.paralelas = paralelas
        self.cache = cache
        self._autorizados_observacion = SIN_LEER
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
        if self.cache is not None:
            paginas = []
            while endpoint is not None:
                valor, siguiente = self._pagina(endpoint)
                if not paginado:
                    return valor
                paginas.append(valor)
                endpoint = siguiente
            return paginas
        args = ["gh", "api", "--method", "GET", endpoint]
        if paginado:
            args += ["--paginate", "--slurp"]
        try:
            return json.loads(self.comando(args))
        except subprocess.CalledProcessError as exc:
            mensaje = (exc.stderr or "").lower()
            estado = re.search(r"\bhttp (403|429)\b", mensaje)
            if estado is None or (estado[1] == "403" and "rate limit" not in mensaje):
                raise
            if "secondary rate limit" in mensaje:
                raise LimiteGitHub("secondary", int(time.time()) + 60) from None
            reinicio = None
            try:
                recursos = json.loads(self.comando(
                    ["gh", "api", "--method", "GET", "rate_limit"]))["resources"]
                reinicio = segundos(recursos["core"]["reset"])
            except ERRORES_LECTURA:
                pass
            raise LimiteGitHub("core", reinicio if reinicio is not None else
                              int(time.time()) + 60) from None

    def _pagina(self, endpoint):
        args = ["gh", "api", "--method", "GET", endpoint, "-i"]
        fila = self.cache.leer(endpoint)
        if fila is not None:
            try:
                anterior = json.loads(fila[1])
            except ValueError:
                fila = None
        try:
            salida = self.comando(args + ["-H", "If-None-Match: " + fila[0]] if fila else args)
            error = None
        except subprocess.CalledProcessError as exc:
            salida, error = exc.output or "", exc
        bloque, separador, cuerpo = salida.replace("\r\n", "\n").partition("\n\n")
        estado, _, encabezados = bloque.partition("\n")
        match = re.fullmatch(r"HTTP/\S+ (\d{3})(?: .*)?", estado)
        if match is not None and separador:
            codigo = int(match[1])
            cabeceras = {k.lower(): v.strip() for k, _, v in
                         (linea.partition(":") for linea in encabezados.splitlines())}
            if codigo == 200 and error is None:
                valor = json.loads(cuerpo)
                siguiente = siguiente_pagina(cabeceras.get("link", ""))
                self.cache.guardar(endpoint, cabeceras.get("etag"), cuerpo, siguiente)
                return valor, siguiente
            if codigo == 304 and fila is not None:
                self.cache.tocar(endpoint, fila[0])
                return anterior, fila[2]
            stderr = (error.stderr or "") if error else ""
            limite = limite_http(codigo, cabeceras, (cuerpo + "\n" + stderr).lower())
            if limite is not None:
                raise limite from None
        if error is not None:
            raise error
        raise subprocess.CalledProcessError(1, args, output=salida)

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
            if rama is not None and (self.sombra or not self.activo):
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
        # Un status identifica C por SHA. Sólo los checks deciden el rojo de la ref.
        estados = ({n: STATUS_CI.get(s, "pendiente") for n, s in ultimos.items()}
                   if rama is None else
                   {n: "pendiente" for n, s in ultimos.items() if s != "success"})
        rojos = tuple(sorted({n for n, v in list(valores.items()) + list(estados.items())
                              if v == "rojo"}))
        # Un esperado ausente impide verde, pero no oculta un rojo presente.
        if rojos:
            return ResultadoCI("rojo", rojos)
        if faltan or (not checks and not estados and (esperados != [] or rama is not None)):
            return ResultadoCI("pendiente")
        return ResultadoCI("verde" if all(v == "verde" for v in valores.values())
                           and all(v == "verde" for v in estados.values()) else "pendiente")

    def vista(self, entrega):
        vista, cerrar = self._leer_vista(entrega)
        return cerrar(self._autorizados_observacion) if cerrar else vista

    def _leer_vista(self, entrega, paralela=False):
        repo, pr = entrega.repo, entrega.pr
        payload = self.api("repos/{}/pulls/{}".format(repo, pr))
        head = payload["head"]["sha"]
        if not sha(head):
            raise ValueError("head inválido")
        vista = VistaPR(pr, head, base=payload["base"]["ref"],
                        abierto=payload["state"] == "open", recibo=entrega.recibo,
                        mergeado=payload.get("merged") is True)
        if head != entrega.head or vista.base != "main" or not vista.abierto:
            return vista, None
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
        except (FalloGit, *ERRORES_LECTURA):
            return replace(vista, completa=False), None

        ci = SIN_LEER
        if paralela:
            try:
                ci = self.ci(repo, head).estado
            except (FalloGit, *ERRORES_LECTURA):
                # El cierre aún debe pasar por Git, como cuando el fallo se lee en serie.
                ci = None
        return vista, partial(self._cerrar_vista, vista, repo, payload, base, archivos,
                              nombres, etiquetas, apto, dirigida, ci)

    def _cerrar_vista(self, vista, repo, payload, base, archivos, nombres, etiquetas,
                      apto, dirigida, ci, autorizados):
        pr, head = vista.pr, vista.head
        try:
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
            if autorizados is SIN_LEER:
                autorizados = self._autorizados()
            if self.activo and autorizados is None:
                raise ValueError("autorizaciones inválidas")
            # Un renombre también saca el archivo de su ruta anterior.
            anteriores = [a["previous_filename"] for pagina in archivos for a in pagina
                          if a.get("status") == "renamed"]
            categorias = categorias_sensibles(nombres + anteriores, self.config)
            if ci is SIN_LEER:
                ci = self.ci(repo, head).estado
            if ci is None:
                return replace(vista, completa=False)
            vista = replace(vista,
                apto=apto.get("veredicto") == "APTO" and apto.get("head") == head,
                hallazgos=apto.get("hallazgos_abiertos")
                if type(apto.get("hallazgos_abiertos")) is int else None,
                dirigida=dirigida.get("head") == head and dirigida.get("resultado") == "limpio",
                ci=ci, conflicto=conflicto,
                sensible=bool(categorias),
                sensible_en_lote=bool(categorias) and categorias.issubset(
                    self.config.get("sensibles_en_lote", [])),
                alto_riesgo=bool(etiquetas.intersection(
                    self.config.get("etiquetas_alto_riesgo", ["alto-riesgo"]))),
                alto_riesgo_en_lote=self.config.get("alto_riesgo_en_lote", False),
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
        except (FalloGit, *ERRORES_LECTURA):
            return replace(vista, completa=False)

    def _autorizados(self):
        return (autorizaciones(self.config) if self.activo else
                self.config.get("heads_autorizados", []))

    def _leer_entrega(self, entrega):
        try:
            return self._leer_vista(entrega, paralela=True)
        except ERRORES_LECTURA:
            return self._vista_incompleta(entrega), None

    @staticmethod
    def _vista_incompleta(entrega):
        return VistaPR(entrega.pr, entrega.head, completa=False, recibo=entrega.recibo)

    @staticmethod
    def tren_en_ci(estado):
        return next((t for t in estado.trenes if isinstance(t.fase, EsperandoCI)), None)

    def ci_candidato(self, estado):
        tren = next((t for t in estado.trenes
                     if isinstance(t.fase, (EsperandoCI, Certificado, Promoviendo))), None)
        if tren is None:
            return None
        cert = certificado_de(tren.fase)
        commit = cert.C if cert is not None else tren.fase.candidato.C
        _, _, rama = recibo_ci(tren).partition(":tren/")
        try:
            ci = self.ci(estado.repo, commit, "tren/" + rama if rama else None)
        except ERRORES_LECTURA:
            ci = ResultadoCI("pendiente")
        return tren, ci

    def observar(self, estado, ahora):
        # El bare se valida y el presupuesto de fetch corre una vez por
        # observación, no por PR: el tick siguiente reintenta desde cero.
        self.reiniciar_transporte()
        repo = estado.repo
        rama = self.api("repos/{}/git/ref/heads/main".format(repo))
        base = rama["object"]["sha"]
        if not sha(base):
            raise ValueError("main inválido")
        entregas = []
        terminales = {t.id for t in estado.trenes if isinstance(t.fase, Terminal)}
        for entrada in estado.entradas:
            # En sombra se sigue un rechazo hasta observar su posible merge externo.
            if ((self.sombra and entrada.terminal) or
                    (isinstance(entrada.admision, Rechazado) and not self.sombra) or
                    (self.activo and entrada.tren in terminales)):
                continue
            entregas.append(entrada.entrega)
        try:
            autorizados = self._autorizados()
        except ERRORES_LECTURA:
            # Sólo quedan incompletas las vistas que necesitan autorización.
            autorizados = None
        if self.paralelas > 1:
            with ThreadPoolExecutor(max_workers=self.paralelas) as pool:
                lecturas = list(pool.map(self._leer_entrega, entregas))
            vistas = [cerrar(autorizados) if cerrar else vista for vista, cerrar in lecturas]
        else:
            vistas = []
            # El camino serial sigue pasando por el método público vista.
            self._autorizados_observacion = autorizados
            try:
                for entrega in entregas:
                    try:
                        vistas.append(self.vista(entrega))
                    except ERRORES_LECTURA:
                        vistas.append(self._vista_incompleta(entrega))
            finally:
                self._autorizados_observacion = SIN_LEER
        observacion = Observacion(ahora, base, tuple(vistas),
                                 self.config.get("excluir_alto_riesgo", True),
                                 costo_suite_sana_s=self.config.get("costo_suite_sana_s", 0)
                                 if self.activo else 0)
        if self.activo:
            candidato = self.ci_candidato(estado)
            if candidato is not None:
                tren, ci = candidato
                observacion = replace(observacion, tren_id=tren.id, ci_candidato=ci.estado,
                                      ci_rojos=ci.rojos,
                                      ci_recibo=recibo_ci(tren))
            if self.almacen is not None and not estado.pausa:
                diagnostico = next((t.fase for t in estado.trenes
                                    if isinstance(t.fase, Diagnosticando)), None)
                if diagnostico is not None:
                    try:
                        archivos = self.almacen.archivos_lote(diagnostico.candidato)
                    except FalloGit:
                        archivos = None
                    observacion = replace(observacion, archivos_lote=archivos)
        return observacion


class Falso:
    def __init__(self, observacion):
        self.observacion = observacion

    def observar(self, estado, ahora):
        return replace(self.observacion, ahora=ahora)
