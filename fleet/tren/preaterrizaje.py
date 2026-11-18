"""Gancho privado por repo; nunca conserva una autorización de push entre ticks."""
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import time

from dominio import (Admitido, Aterrizado, BloqueoPrevia, EnBase, EscritorExterno,
                     EsperaPrevia, Incierto, Terminal, VentanaAutorizada,
                     admision_entrada, clave)
from git_ops import FalloGit, entorno, sha
from operador import FalloSnapshot


PRESUPUESTO_IO_S = 60
ESPERA_MAX_S = 1800


def autorizaciones(config):
    """Lista histórica de SHA o entradas con ventana UTC ligada al head exacto."""
    resultado = {}
    entradas = config.get("heads_autorizados", [])
    if not isinstance(entradas, list):
        raise ValueError("autorizaciones invalidas")
    for entrada in entradas:
        head, ventana = entrada, None
        if isinstance(entrada, dict):
            if set(entrada) - {"head", "ventana"}:
                raise ValueError("autorizacion invalida")
            head = entrada.get("head")
            valor = entrada.get("ventana")
            if valor is not None:
                if not isinstance(valor, dict) or set(valor) != {"inicio", "fin"}:
                    raise ValueError("ventana invalida")
                fechas = []
                for nombre in ("inicio", "fin"):
                    texto = valor[nombre]
                    if not isinstance(texto, str) or not re.fullmatch(
                            r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", texto):
                        raise ValueError("ventana debe usar UTC con Z")
                    fechas.append(datetime.strptime(texto, "%Y-%m-%dT%H:%M:%SZ")
                                  .replace(tzinfo=timezone.utc).timestamp())
                if fechas[0] >= fechas[1]:
                    raise ValueError("ventana vacia")
                ventana = VentanaAutorizada(*fechas)
        if not sha(head) or head in resultado:
            raise ValueError("head autorizado invalido o duplicado")
        resultado[head] = ventana
    return resultado


# Un push a main de estos repos despliega a producción: sin gancho no hay push.
EXIGEN_GANCHO = frozenset({"jale"})


def exige_gancho(config):
    return config.get("adaptador") in EXIGEN_GANCHO


def gancho(config):
    valor = config.get("pre_aterrizaje")
    if valor is None:
        return None
    if (not isinstance(valor, dict) or not {"comando", "plazo_s"} <= set(valor) or
            set(valor) - {"comando", "plazo_s", "espera_max_s"}):
        raise ValueError("gancho invalido")
    comando, plazo = valor["comando"], valor["plazo_s"]
    if (not isinstance(comando, list) or not comando or
            not all(isinstance(s, str) and s and "\0" not in s for s in comando) or
            not Path(comando[0]).is_absolute() or type(plazo) not in (int, float) or
            not math.isfinite(plazo) or not 0 < plazo <= 60):
        raise ValueError("comando o plazo del gancho invalido")
    espera = valor.get("espera_max_s", ESPERA_MAX_S)
    if type(espera) not in (int, float) or not math.isfinite(espera) or espera <= 0:
        raise ValueError("plazo total de espera invalido")
    return valor


def objeto_sin_duplicados(pares):
    objeto = {}
    for nombre, valor in pares:
        if nombre in objeto:
            raise ValueError("campo duplicado")
        objeto[nombre] = valor
    return objeto


def invocar(config, solicitud, accion):
    """Sin shell ni salida privada en eventos. Mata el grupo al agotar el plazo."""
    env = entorno()
    if config.get("snapshot"):
        env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        with subprocess.Popen(config["comando"], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                env=env, start_new_session=True) as proceso:
            try:
                salida, _ = proceso.communicate(json.dumps(dict(solicitud, accion=accion)),
                                                timeout=config["plazo_s"])
            except subprocess.TimeoutExpired:
                os.killpg(proceso.pid, signal.SIGKILL)
                proceso.communicate()
                return "timeout"
            if proceso.returncode != 0:
                return "error"
        if len(salida) > 16384:
            return "salida-invalida"
        valor = json.loads(salida, object_pairs_hook=objeto_sin_duplicados)
        permitidos = ("seguir", "esperar", "pausar") if accion == "verificar" else ("cancelado",)
        if (not isinstance(valor, dict) or set(valor) != {"version", "id", "resultado"} or
                type(valor["version"]) is not int or valor["version"] != 1 or
                valor["id"] != solicitud["id"] or valor["resultado"] not in permitidos):
            return "salida-invalida"
        return valor["resultado"]
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return "error"


def resumen_cancelaciones(ledger, repo):
    """Lectura sin crear tablas ni exponer argv o evidencia privada."""
    if not ledger.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                             "AND name='pre_aterrizajes'").fetchone():
        return []
    registros = (json.loads(r[0]) for r in ledger.db.execute(
        "SELECT payload FROM pre_aterrizajes WHERE repo=? AND pendiente=1 ORDER BY rowid",
        (repo,)))
    return [dict(id=r["solicitud"]["id"], tren=r["solicitud"]["tren"],
                 B=r["solicitud"]["B"], C=r["solicitud"]["C"],
                 ultimo_error=r.get("ultimo_error", "cancelacion-sin-confirmar"))
            for r in registros if r.get("cancelacion_requerida")]


class Previo:
    def __init__(self, ledger, git, config, frontera, operador=None):
        self.ledger, self.git, self.config = ledger, git, config
        self.operador = operador
        self.fallo_snapshot = None
        self.repo = ""
        self.frontera = frontera
        self.reloj = time.time
        self.reloj_io = time.monotonic
        self.io_s = 0.0
        self.cancelaciones = set()
        # Diario privado separado de eventos: contiene el argv que deberá
        # cancelar incluso si se retira o cambia la configuración del repo.
        ledger.db.execute("CREATE TABLE IF NOT EXISTS pre_aterrizajes ("
                          "id TEXT PRIMARY KEY, repo TEXT NOT NULL, payload TEXT NOT NULL, "
                          "pendiente INTEGER NOT NULL)")

    def llamar(self, registro, accion):
        restante = PRESUPUESTO_IO_S - self.io_s
        if restante <= 0:
            return "timeout"
        config = registro["gancho"]
        snapshot = bool(registro.get("snapshot"))
        if snapshot and self.operador is not None:
            self.operador.validar_guardado(*config["comando"])
        inicio = self.reloj_io()
        try:
            return invocar(dict(config, plazo_s=min(config["plazo_s"], restante), snapshot=snapshot),
                           registro["solicitud"], accion)
        finally:
            self.io_s += max(0, self.reloj_io() - inicio)

    def ventana(self, tren, observacion):
        vistas = {v.recibo or str(v.pr): v for v in observacion.prs}
        ahora = self.reloj()
        for recibo in tren.recibos:
            vista = vistas.get(recibo)
            if vista is None:
                vista = next((v for v in observacion.prs if
                    clave("entrega", self.repo, v.pr, v.head) == recibo), None)
            if vista is None:
                # Las observaciones inyectadas antiguas pueden estar ligadas al PR.
                continue
            v = vista.ventana
            if v is not None:
                if len(tren.recibos) != 1:
                    return "lote-con-ventana"
                if ahora >= v.fin:
                    return "ventana-vencida"
                if ahora < v.inicio:
                    return "esperar"
        return "seguir"

    def necesario(self, tren, observacion):
        return ("pre_aterrizaje" in self.config or exige_gancho(self.config) or
                any(v.ventana is not None for v in observacion.prs))

    def terminar(self, identificador):
        with self.ledger.transaccion():
            self.ledger.db.execute("UPDATE pre_aterrizajes SET pendiente=0 WHERE id=?",
                                   (identificador,))

    def guardar_cancelacion(self, registro, error):
        # La obligación precede al IO y no depende de que la pausa siga viva.
        registro.update(cancelacion_requerida=True, ultimo_error=error)
        with self.ledger.transaccion():
            self.ledger.db.execute("UPDATE pre_aterrizajes SET payload=? WHERE id=?",
                (json.dumps(registro, sort_keys=True), registro["solicitud"]["id"]))

    def reconciliar(self, B, C, arbol, heads, lectura=None):
        lectura = lectura or self.git.reconciliar(B, C, arbol, heads)
        if isinstance(lectura, EscritorExterno):
            try:
                self.git.traer(lectura.main)
                contiene = self.git.git("merge-base", "--is-ancestor", C, lectura.main,
                                        aislado=True).returncode
                if contiene == 0:
                    self.git.verificar(B, C, arbol, heads)
                    return Aterrizado(B, C, arbol)
                if contiene == 1:
                    return lectura
            except FalloGit:
                pass
            return Incierto(B, C, "ascendencia-incierta")
        return lectura

    def cancelar(self, registro, lectura=None):
        solicitud = registro["solicitud"]
        self.guardar_cancelacion(registro, registro.get("ultimo_error", "cancelacion-sin-confirmar"))
        if solicitud["id"] in self.cancelaciones:
            if isinstance(lectura, Aterrizado):
                self.terminar(solicitud["id"])
                return True
            return False
        self.cancelaciones.add(solicitud["id"])
        B, C = solicitud["B"], solicitud["C"]
        lectura = self.reconciliar(B, C, registro["arbol"], registro["heads"], lectura)
        if isinstance(lectura, Aterrizado):
            self.terminar(solicitud["id"])
            return True
        if not isinstance(lectura, (EnBase, EscritorExterno)):
            self.guardar_cancelacion(registro, "reconciliacion-incierta")
            return False  # Nunca deshacer un efecto cuyo push pudo aterrizar.
        try:
            resultado = self.llamar(registro, "cancelar")
        except FalloSnapshot as exc:
            self.fallo_snapshot = exc
            self.guardar_cancelacion(registro, "checkout-operador:" + str(exc))
            return False
        if resultado != "cancelado":
            self.guardar_cancelacion(registro, "cancelar-" + resultado)
            return False
        self.terminar(solicitud["id"])
        return True

    def pendientes(self, repo):
        return [json.loads(r[0]) for r in self.ledger.db.execute(
            "SELECT payload FROM pre_aterrizajes WHERE repo=? AND pendiente=1", (repo,))]

    def bloqueado(self, repo):
        return any(r.get("cancelacion_requerida") for r in self.pendientes(repo))

    def limpiar(self, estado, observacion=None):
        """Cancela al pausar, abortar, caducar, cambiar base/config o salir de ventana."""
        self.repo = estado.repo
        vistas = {v.recibo or str(v.pr): v for v in observacion.prs} if observacion else {}
        limpio = True
        for registro in self.pendientes(estado.repo):
            s = registro["solicitud"]
            tren = next((t for t in estado.trenes if t.id == s["tren"]), None)
            cancelar = (registro.get("cancelacion_requerida") or
                estado.pausa is not None or tren is None or
                isinstance(tren.fase, Terminal) or
                self.config.get("pre_aterrizaje") != registro.get("gancho_original", registro["gancho"]))
            if not cancelar and observacion:
                cancelar = (observacion.base != s["B"] or
                    self.ventana(tren, observacion) != "seguir" or
                    any(not isinstance(admision_entrada(e, vistas, observacion), Admitido)
                        for e in estado.entradas if e.entrega.recibo in tren.recibos))
            if cancelar and not self.cancelar(registro):
                limpio = False
        return limpio

    def verificar(self, estado, tren, observacion, efecto):
        self.repo = estado.repo
        cert = tren.fase.certificado
        B, C = cert.B, cert.C
        if self.bloqueado(estado.repo):
            return EsperaPrevia(B, C)
        ventana = self.ventana(tren, observacion)
        if ventana != "seguir":
            return (EsperaPrevia(B, C) if ventana == "esperar" else
                    BloqueoPrevia(B, C, ventana))
        try:
            config = gancho(self.config)
        except ValueError:
            return BloqueoPrevia(B, C, "configuracion-invalida")
        if config is None and exige_gancho(self.config):
            return BloqueoPrevia(B, C, "configuracion-ausente")
        registro = None
        if config is not None:
            miembros = {e.entrega.recibo: e.entrega for e in estado.entradas}
            solicitud = {"version": 1, "id": clave("pre-aterrizaje", estado.repo, tren.id, B, C),
                "repo": estado.repo, "tren": tren.id, "B": B, "C": C,
                "almacen_git": str(self.git.ruta.resolve()),
                "prs": [{"pr": miembros[r].pr, "head": miembros[r].head}
                        for r in tren.recibos]}
            anterior = self.ledger.db.execute(
                "SELECT payload FROM pre_aterrizajes WHERE id=? AND pendiente=1",
                (solicitud["id"],)).fetchone()
            if anterior:
                # Una reconsulta o recuperación conserva el argv y su plazo.
                registro = json.loads(anterior[0])
                if registro.get("cancelacion_requerida"):
                    return EsperaPrevia(B, C)
                registro.setdefault("vence_espera",
                    self.reloj() + config.get("espera_max_s", ESPERA_MAX_S))
            else:
                registro = {"gancho": config, "solicitud": solicitud, "arbol": cert.arbol,
                            "heads": [p["head"] for p in solicitud["prs"]],
                            "vence_espera": self.reloj() + config.get("espera_max_s", ESPERA_MAX_S)}
                if self.operador is not None and self.operador.checkout is not None:
                    # El EnBase previo al CAS certificó main == B.
                    registro["gancho"], registro["snapshot"] = self.operador.gancho(config, B)
                    registro["gancho_original"] = config
            with self.ledger.transaccion():
                self.ledger.db.execute("INSERT INTO pre_aterrizajes VALUES (?, ?, ?, 1) "
                    "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, pendiente=1",
                    (solicitud["id"], estado.repo, json.dumps(registro, sort_keys=True)))
            self.frontera("antes-pre-aterrizaje", efecto)
            resultado = ("espera-vencida" if self.reloj() >= registro["vence_espera"] else
                         self.llamar(registro, "verificar"))
            if self.reloj() >= registro["vence_espera"]:
                resultado = "espera-vencida"
        else:
            resultado = "seguir"
        # Una espera de IO invalida la hora y la base leídas al abrir el tick.
        heads = tuple(e.entrega.head for e in estado.entradas if e.entrega.recibo in tren.recibos)
        lectura = self.git.reconciliar(B, C, cert.arbol, heads)
        if not isinstance(lectura, EnBase):
            if registro:
                self.cancelar(registro, lectura)
            return lectura
        ventana = self.ventana(tren, observacion)
        impedimento = None
        if resultado not in ("seguir", "esperar"):
            impedimento = BloqueoPrevia(B, C, "gancho-" + resultado)
        elif ventana != "seguir":
            impedimento = (EsperaPrevia(B, C) if ventana == "esperar" else
                           BloqueoPrevia(B, C, ventana))
        elif resultado == "esperar":
            return EsperaPrevia(B, C)
        if impedimento is not None:
            if registro:
                self.cancelar(registro, lectura)
            return impedimento
        self.frontera("pre-aterrizaje-verificado", efecto)
        # Última lectura local antes de devolver el control al push. El CAS
        # protege B, pero no impone un límite horario en el servidor remoto.
        ventana = self.ventana(tren, observacion)
        if ventana != "seguir":
            if registro:
                self.cancelar(registro, lectura)
            return (EsperaPrevia(B, C) if ventana == "esperar" else
                    BloqueoPrevia(B, C, ventana))
        return None

    def resolver(self, estado, tren, lectura):
        # Después del CAS, cancelar sólo con una lectura que pruebe no aterrizaje.
        if isinstance(lectura, EsperaPrevia):
            return lectura
        for registro in self.pendientes(estado.repo):
            if registro["solicitud"]["tren"] == tren.id:
                # El diario bloquea nuevos pushes hasta resolver la obligación;
                # no sustituir la lectura ni su recuperación por una pausa.
                self.cancelar(registro, lectura)
        return lectura
