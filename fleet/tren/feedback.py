"""Feedback D6: escritor separado, reconciliación del comentario antes del POST.

El flock del tick serializa los escritores del repo. La clave viaja a GitHub,
no depende del ACK local para sobrevivir a una muerte tras publicar.
"""
import hashlib
import json
import re
import shlex
import subprocess

from adaptadores.jale import HOSTS, MODOS
from dominio import LANZADOR_JALE, Rechazado
from observador import sha


STATUS_SOLO = {"avisar-en-tren": ("pending", "En el tren; lote {}"),
               "avisar-aterrizaje": ("success", "Aterrizado; lote {}")}
FEEDBACK = frozenset(("devolver", "avisar-caducidad")) | frozenset(STATUS_SOLO)
CONTEXTO = "pao-tren/resultado"


def pr_feedback(efecto, estado):
    """Ordenar por dueño, incluso si la caducidad aún no conoce su head."""
    recibo = efecto.datos[0 if efecto.tipo == "avisar-caducidad" else 1]
    return next(e.entrega.pr for e in estado.entradas if e.entrega.recibo == recibo)


def comando_publico(fallo):
    argv = shlex.split(fallo["comando"])
    if len(argv) < 3 or argv[0] != "bash":
        raise ValueError("Comando de reproducción desconocido")
    args = argv[2:]
    carril = args[:2] if args[:2] == ["--carril", "rapido"] else []
    posicionales = args[len(carril):]
    if posicionales[:1] and posicionales[0] in HOSTS:
        if len(posicionales) != 5:
            raise ValueError("Comando de reproducción de Jale inválido")
        host, _, modo, commit, unidad = posicionales
        if modo not in MODOS or modo != fallo["check"] or unidad != fallo["unidad"]:
            raise ValueError("Comando de reproducción de Jale inválido")
        if not sha(commit):
            raise ValueError("SHA de reproducción inválido")
        comando = shlex.join(["bash", LANZADOR_JALE, *carril, host, "<log>", modo, commit, unidad])
        # Boat sólo admite corridas con un dueño válido; el lector pone el suyo.
        return ("SEN_RUN_OWNER=" + shlex.quote("<agente>") + " " + comando
                if host == "boat" else comando)
    if args[0] == "--repetir":
        args = args[2:]
    elif args[0] != "--sha":
        raise ValueError("Comando de reproducción desconocido")
    indice = args.index("--sha") + 1
    commit = args[indice] if indice < len(args) else None
    if not sha(commit):
        raise ValueError("SHA de reproducción inválido")
    return shlex.join(["bash", "scripts/mini/run.sh", "--repetir", "<resultado.json>",
        "--sha", commit, "--worktree", "<worktree-en-" + commit + ">",
        "--veces", "2", "--unidad", fallo["check"] + "=" + fallo["unidad"]])


class EscritorGitHub:
    def __init__(self, ejecutar=None):
        self.ejecutar = ejecutar or subprocess.run

    def api(self, endpoint, payload=None, paginado=False):
        argv = ["gh", "api", "--method", "POST" if payload is not None else "GET", endpoint]
        opciones = {}
        if payload is not None:
            argv += ["--input", "-"]
            opciones["input"] = json.dumps(payload, ensure_ascii=True)
        if paginado:
            argv += ["--paginate", "--slurp"]
        return json.loads(self.ejecutar(argv, check=True, capture_output=True,
            text=True, timeout=60, **opciones).stdout)

    def comentario(self, repo, pr, clave, texto):
        marcador = "<!-- pao-tren/efecto:{} -->".format(clave)
        endpoint = "repos/{}/issues/{}/comments".format(repo, pr)
        paginas = self.api(endpoint + "?per_page=100", paginado=True)
        for pagina in paginas:
            for comentario in pagina:
                if marcador in comentario["body"]:
                    return comentario["html_url"]
        return self.api(endpoint, {"body": marcador + "\n" + texto})["html_url"]

    def ejecutar_feedback(self, efecto, estado, frontera):
        def status(head, payload, url=None, reconciliar=False):
            if head is not None:
                publicado = False
                if reconciliar:
                    paginas = self.api("repos/{}/commits/{}/statuses?per_page=100".format(
                        estado.repo, head), paginado=True)
                    ultimo = next((s for pagina in paginas for s in pagina
                                   if s["context"] == CONTEXTO), None)
                    publicado = ultimo is not None and all(
                        ultimo.get(k) == v for k, v in payload.items())
                if not publicado:
                    self.api("repos/{}/statuses/{}".format(estado.repo, head), payload)
            return {"comentario_url": url, "status_sha": head}

        if efecto.tipo in STATUS_SOLO:
            lote, recibo = efecto.datos
            entrega = next(e.entrega for e in estado.entradas if e.entrega.recibo == recibo)
            if not sha(entrega.head) or not re.fullmatch(r"[0-9a-f]{64}", lote):
                raise ValueError("head o lote inválido")
            valor, descripcion = STATUS_SOLO[efecto.tipo]
            return status(entrega.head, {
                "state": valor, "context": CONTEXTO,
                "description": descripcion.format(lote)})
        if efecto.tipo == "devolver":
            payload = json.loads(efecto.datos[2])
            pr, head = payload["pr"], payload["head"]
            # Lista explícita: nunca serializar la configuración ni argv privado.
            publico = {k: payload[k] for k in ("lote", "B", "C", "pr", "head")}
            publico["fallos"] = [{**{k: f[k] for k in (
                "unidad", "check", "conjunto_causal")},
                "firma": "sha256:" + hashlib.sha256(f["firma"].encode("utf-8")).hexdigest()[:16],
                "comando": comando_publico(f)} for f in payload["fallos"]]
            if "juicio" in payload:
                publico["juicio"] = payload["juicio"]
            if "ci_rojos" in payload:
                nombres = payload["ci_rojos"]
                if not isinstance(nombres, list) or not nombres or not all(
                        isinstance(n, str) and n.strip() for n in nombres):
                    raise ValueError("CI rojo sin nombres de checks válidos")
                publico["ci_rojos"] = nombres
            detalle = json.dumps(publico, ensure_ascii=True, sort_keys=True, indent=2)
            texto = ("Entrega devuelta por CI rojo sobre el candidato individual.\n\n"
                     if "ci_rojos" in payload else
                     "Entrega devuelta por juicio.\n\n" if "juicio" in payload else
                     "Entrega devuelta por fallos atribuidos al PR.\n\n") + "\n".join(
                "    " + linea for linea in detalle.splitlines())
            descripcion = "Entrega devuelta; detalle y reproducción en el comentario"
            if "ci_rojos" in payload:
                descripcion = "CI rojo sobre candidato individual; checks en el comentario"
        elif efecto.tipo == "avisar-caducidad":
            entrada = next(e for e in estado.entradas if e.entrega.recibo == efecto.datos[0])
            if not isinstance(entrada.admision, Rechazado):
                raise ValueError("caducidad sin rechazo")
            pr, head = entrada.entrega.pr, None
            texto = ("La entrega {} sobre {} caducó: {}. "
                     "Hay que volver a entregar el head vigente con APTO y dirigida limpia."
                     ).format(entrada.entrega.recibo, entrada.entrega.head,
                              entrada.admision.motivo.value)
            descripcion = "Entrega caducada; volver a entregar. Detalle en el comentario"
        else:
            raise ValueError("feedback desconocido")
        if head is not None and not sha(head):
            raise ValueError("head inválido")
        url = self.comentario(estado.repo, pr, efecto.clave, texto)
        frontera("comentario-publicado", efecto)
        if efecto.tipo == "avisar-caducidad":
            # El observador omite rechazados y trenes terminales. Leer aquí,
            # después del comentario, incluso durante recuperación o pausa.
            actual = self.api("repos/{}/pulls/{}".format(estado.repo, pr))
            if actual["state"] == "open" and not actual["merged"]:
                head = actual["head"]["sha"]
                if not sha(head):
                    raise ValueError("head vigente inválido")
        return status(head, {"state": "failure", "context": CONTEXTO,
                             "description": descripcion, "target_url": url}, url,
                      reconciliar=efecto.tipo == "devolver" and "ci_rojos" in payload)
