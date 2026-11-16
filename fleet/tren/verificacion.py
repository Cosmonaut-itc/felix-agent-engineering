"""Verificación previa al aterrizaje (U18): gancho por repo y ventana autorizada.

El gancho es un ejecutable privado del repo. PAO sólo fija el contrato: una
solicitud JSON por stdin y una respuesta JSON por stdout, con plazo. Nada de
argv, stdout ni stderr del gancho llega a eventos o feedback: sólo el
resultado y un motivo corto validado.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess

from dominio import PAUSAS_VERIFICACION, Verificacion


CONTRATO = "pao-tren/verificacion-previa@1"
PLAZO_POR_DEFECTO_S = 600
PLAZO_MAXIMO_S = 3600
MOTIVO = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
INSTANTE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
SHA = re.compile(r"[0-9a-f]{40}")


class CancelacionFallida(Exception):
    def __init__(self, motivo):
        super().__init__(motivo)
        self.resultado = {"clase": "CancelacionFallida", "motivo": motivo}


def instante(valor):
    if not isinstance(valor, str) or not INSTANTE.fullmatch(valor):
        raise ValueError("ventana autorizada: instante UTC AAAA-MM-DDTHH:MM:SSZ")
    return datetime.strptime(valor, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc).timestamp()


def autorizaciones(config):
    """head -> ventana (desde, hasta) o None. Un SHA suelto conserva U1."""
    resultado = {}
    for entrada in config.get("heads_autorizados", []):
        if isinstance(entrada, dict):
            if set(entrada) not in ({"head"}, {"head", "desde", "hasta"}):
                raise ValueError("ventana autorizada: claves head, desde y hasta")
            head = entrada["head"]
            if not isinstance(head, str) or not SHA.fullmatch(head):
                raise ValueError("ventana autorizada: head SHA40")
            ventana = None
            if "desde" in entrada:
                ventana = (instante(entrada["desde"]), instante(entrada["hasta"]))
                if ventana[0] >= ventana[1]:
                    raise ValueError("ventana autorizada vacía")
        elif isinstance(entrada, str):
            head, ventana = entrada, None
        else:
            # Como U1: un valor que no es SHA nunca coincide con un head.
            continue
        if resultado.get(head, ventana) != ventana:
            raise ValueError("ventana autorizada contradictoria")
        resultado[head] = ventana
    return resultado


def gancho(config):
    """None si el repo no declara gancho: el tick se comporta como antes."""
    declarado = config.get("verificacion_previa")
    if declarado is None:
        return None
    if not isinstance(declarado, dict) or not set(declarado) <= {"argv", "plazo_s"}:
        raise ValueError("verificacion_previa: claves argv y plazo_s")
    argv = declarado.get("argv")
    if (not isinstance(argv, list) or not argv or
            not all(isinstance(a, str) and a for a in argv) or
            not Path(argv[0]).is_absolute()):
        raise ValueError("verificacion_previa.argv: lista con ruta absoluta")
    plazo = declarado.get("plazo_s", PLAZO_POR_DEFECTO_S)
    if type(plazo) is not int or not 1 <= plazo <= PLAZO_MAXIMO_S:
        raise ValueError("verificacion_previa.plazo_s: entero entre 1 y 3600")
    return Gancho(tuple(argv), plazo)


class Gancho:
    def __init__(self, argv, plazo_s, ejecutar=None):
        self.argv = tuple(argv)
        self.plazo_s = plazo_s
        self.ejecutar = ejecutar or subprocess.run

    def llamar(self, operacion, solicitud):
        """(respuesta, "") o (None, motivo). Nunca propaga la salida del gancho."""
        entrada = dict(solicitud, contrato=CONTRATO, operacion=operacion,
                       plazo_s=self.plazo_s)
        try:
            proceso = self.ejecutar(list(self.argv) + [operacion],
                                    input=json.dumps(entrada, sort_keys=True),
                                    capture_output=True, text=True,
                                    timeout=self.plazo_s, check=False)
        except subprocess.TimeoutExpired:
            return None, "plazo-agotado"
        except (OSError, subprocess.SubprocessError, ValueError):
            return None, "gancho-no-ejecutable"
        if proceso.returncode != 0:
            return None, "salida-no-cero"
        try:
            respuesta = json.loads(proceso.stdout)
        except ValueError:
            return None, "salida-mal-formada"
        if (not isinstance(respuesta, dict) or respuesta.get("B") != solicitud["B"] or
                respuesta.get("C") != solicitud["C"]):
            return None, "salida-mal-formada"
        return respuesta, ""

    def verificar(self, solicitud):
        respuesta, error = self.llamar("verificar", solicitud)
        B, C = solicitud["B"], solicitud["C"]
        prefijo = PAUSAS_VERIFICACION[0]
        if respuesta is None:
            return Verificacion(B, C, "pausar", prefijo + error)
        resultado = respuesta.get("resultado")
        motivo = respuesta.get("motivo", "")
        if not isinstance(motivo, str) or (motivo and not MOTIVO.fullmatch(motivo)):
            return Verificacion(B, C, "pausar", prefijo + "salida-mal-formada")
        if resultado == "seguir":
            return Verificacion(B, C, "seguir", prefijo + (motivo or "seguir"))
        if resultado in ("esperar", "pausar"):
            if not motivo:
                return Verificacion(B, C, "pausar", prefijo + "salida-mal-formada")
            return Verificacion(B, C, resultado, prefijo + motivo)
        return Verificacion(B, C, "pausar", prefijo + "resultado-desconocido")

    def cancelar(self, solicitud):
        respuesta, error = self.llamar("cancelar", solicitud)
        if respuesta is None:
            raise CancelacionFallida(error)
        if respuesta.get("resultado") != "cancelado":
            raise CancelacionFallida("resultado-desconocido")
        return respuesta


def solicitud(repo, tren_id, intento, B, C, arbol, miembros, almacen):
    """Lo que recibe el gancho; miembros es ((pr, head), ...) en orden del lote."""
    return {"repo": repo, "tren": tren_id, "intento": intento, "B": B, "C": C,
            "arbol": arbol, "prs": [{"pr": pr, "head": head} for pr, head in miembros],
            "almacen": almacen}


def verificar_aterrizaje(ventanas, ahora, gancho_repo, pedir):
    """Ventana autorizada y gancho, en ese orden, justo antes del push.

    ventanas: (desde, hasta) de los miembros que la tienen; ahora(): instante
    actual en la base de Observacion.ahora; pedir(): la solicitud del gancho.
    None sin ventana ni gancho: el push sigue exactamente como antes de U18.
    """
    if not ventanas and gancho_repo is None:
        return None
    datos = pedir()
    B, C = datos["B"], datos["C"]
    desde = max((d for d, _ in ventanas), default=None)
    hasta = min((h for _, h in ventanas), default=None)

    def fuera():
        if hasta is not None and ahora() >= hasta:
            return Verificacion(B, C, "pausar", PAUSAS_VERIFICACION[1])
        if desde is not None and ahora() < desde:
            return Verificacion(B, C, "esperar", "ventana-autorizada-no-abierta")
        return None

    previa = fuera()
    if previa is not None or gancho_repo is None:
        return previa or Verificacion(B, C, "seguir", "ventana-autorizada")
    resultado = gancho_repo.verificar(datos)
    if resultado.resultado != "seguir":
        return resultado
    # El gancho pudo tardar su plazo: la ventana se comprueba otra vez.
    return fuera() or resultado
