"""Frontera Hasito: argv sin shell y recibos tipados. No importa el tick."""
from dataclasses import dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess

from dominio import (Completo, Fallo, Incompleto, Intento, Repeticion,
                     UnidadRepetida, clave)
from git_ops import FalloGit, sha
from ledger import directorio_estado


@dataclass(frozen=True)
class ErrorResultado:
    codigo: str


class ResultadoInvalido(Exception):
    def __init__(self, codigo):
        super().__init__(codigo)
        self.resultado = ErrorResultado(codigo)


def exigir(condicion, codigo="resultado-malformado"):
    if not condicion:
        raise ResultadoInvalido(codigo)


def numero(valor):
    return type(valor) in (int, float) and math.isfinite(valor) and valor >= 0


def leer_json(ruta):
    try:
        return json.loads(Path(ruta).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        raise ResultadoInvalido("resultado-ilegible") from None


def sobre(r, esquema, commit, base=None):
    exigir(isinstance(r, dict) and r.get("esquema") == esquema, "esquema-desconocido")
    exigir(r.get("sha") == commit and sha(commit), "sha-no-corresponde")
    if base is not None:
        exigir(r.get("base") == base, "base-no-corresponde")
    exigir(type(r.get("veredicto")) is int and r["veredicto"] in (0, 1, 75),
           "veredicto-no-utilizable")


def leer_suite(r, commit, base):
    sobre(r, "suite-resultado@1", commit, base)
    campos = ("solo_candidato", "tambien_base", "sospecha_arnes", "restantes", "partes")
    exigir(all(isinstance(r.get(k), list) for k in campos))
    exigir(all(isinstance(f, dict) for k in campos[:3] for f in r[k]))
    exigir(all(isinstance(f, (dict, str)) for f in r["restantes"]))
    if r["veredicto"] == 75:
        exigir(isinstance(r.get("reanudar"), str) and bool(r["reanudar"]))
        return Incompleto(75, r["reanudar"])
    exigir(not r["sospecha_arnes"], "arnes-sin-75")
    exigir(bool(r["solo_candidato"]) == (r["veredicto"] == 1), "veredicto-incoherente")
    exigir(not r["restantes"] or r["veredicto"] == 1, "suite-incompleta")
    exigir(bool(r["partes"]), "suite-sin-partes")
    tiempos = {}
    for p in r["partes"]:
        exigir(isinstance(p, dict) and isinstance(p.get("etapas"), list))
        sobre(p, "suite-resultado@1", commit, base)
        exigir(p["veredicto"] != 75 and p["veredicto"] <= r["veredicto"], "parte-incompleta")
        exigir(isinstance(p.get("origen"), str) and p["origen"])
        for etapa in p["etapas"]:
            exigir(isinstance(etapa, dict) and isinstance(etapa.get("checks"), list))
            for check in etapa["checks"]:
                exigir(isinstance(check, dict) and numero(check.get("segundos")))
                exigir(isinstance(check.get("id"), str))
                tiempos[(p.get("origen", ""), check["id"])] = check["segundos"]
    fallos = []
    for campo, clase in (("solo_candidato", "solo-candidato"), ("tambien_base", "también-base")):
        for f in r[campo]:
            exigir(all(isinstance(f.get(k), str) and f[k] for k in ("unidad", "check", "etapa", "parte")))
            firma = f.get("firma", f.get("detalle"))
            exigir(isinstance(firma, str))
            fallos.append(Fallo(f["unidad"], firma, clase, f["check"], f["etapa"],
                                f["parte"], tiempos.get((f["parte"], f["check"]), 0)))
    exigir(len({(f.parte, f.check, f.unidad) for f in fallos}) == len(fallos))
    return Completo("verde-contra-base" if r["veredicto"] == 0 and fallos else
                    "verde" if r["veredicto"] == 0 else "rojo", tuple(fallos), sum(tiempos.values()),
                    tuple(f if isinstance(f, str) else json.dumps(f, sort_keys=True)
                          for f in r["restantes"]))


def leer_repeticion(r, commit, origen, origen_sha, base, fallo):
    sobre(r, "suite-repeticion@1", commit)
    exigir(isinstance(r.get("origen"), dict))
    o = r["origen"]
    exigir((o.get("sha"), o.get("base"), o.get("sha256")) ==
           (origen_sha, base, hashlib.sha256(Path(origen).read_bytes()).hexdigest()),
           "origen-no-corresponde")
    exigir(o.get("ruta") == str(Path(origen)), "ruta-origen-no-corresponde")
    exigir(type(r.get("completa")) is bool and type(r.get("veces")) is int and r["veces"] == 2)
    exigir(isinstance(r.get("unidades"), list))
    if r["veredicto"] == 75:
        exigir(not r["completa"] and isinstance(r.get("reanudar"), str) and r["reanudar"])
        return Incompleto(75, r["reanudar"])
    exigir(r["completa"], "repeticion-incompleta")
    unidades = []
    for u in r["unidades"]:
        exigir(isinstance(u, dict) and isinstance(u.get("cobertura"), dict))
        c = u["cobertura"]
        exigir(c.get("completa") is True and c.get("pedidos") == 2 and c.get("observados") == 2)
        exigir(isinstance(c.get("testigo"), str) and bool(c["testigo"]))
        exigir(all(isinstance(u.get(k), str) and u[k] for k in ("check", "unidad", "parte", "definicion")))
        exigir(isinstance(u.get("intentos"), list) and len(u["intentos"]) == 2)
        intentos = []
        for i in u["intentos"]:
            exigir(isinstance(i, dict) and type(i.get("n")) is int and i["n"] in (1, 2))
            exigir(i.get("veredicto") in ("pasa", "falla", "omitida", "ausente"), "intento-sin-evidencia")
            exigir(isinstance(i.get("firma"), str) and numero(i.get("duracion_s")))
            exigir(all(isinstance(i.get(k), str) and i[k] for k in
                        ("parte", "destino", "huella_entorno", "ensayo")))
            exigir(i["parte"] == u["parte"])
            intentos.append(Intento(i["n"], i["veredicto"], i["firma"], i["duracion_s"], i["ensayo"]))
        exigir({i.n for i in intentos} == {1, 2}, "intentos-duplicados")
        unidades.append(UnidadRepetida(u["check"], u["unidad"], u["parte"], u["definicion"],
                                       tuple(sorted(intentos, key=lambda i: i.n))))
    exigir(len(unidades) == 1 and (unidades[0].parte, unidades[0].check, unidades[0].unidad) ==
           (fallo.parte, fallo.check, fallo.unidad), "seleccion-no-corresponde")
    exigir(any(i.veredicto == "falla" for u in unidades for i in u.intentos) == (r["veredicto"] == 1))
    return Repeticion(commit, origen_sha, base, o["sha256"], tuple(unidades))


class Hasito:
    def __init__(self, lanzador, git, runner=None, estado=None):
        self.lanzador = str(Path(lanzador).resolve())
        self.git = git
        self.operador = None
        self.runner = runner or subprocess.run
        self.estado = (Path(estado) if estado is not None else
                       directorio_estado() / "hasito").expanduser().resolve()

    def argv_suite(self, candidato, worktree, evidencia, nombre):
        return ("bash", self.lanzador, "--suite", candidato.C, "--perfil", "final",
                "--base", candidato.B, "--worktree", str(worktree),
                "--evidencia", str(evidencia), "--nombre", nombre)

    def argv_repetir(self, origen, commit, worktree, evidencia, nombre, fallo):
        return ("bash", self.lanzador, "--repetir", str(origen), "--sha", commit,
                "--worktree", str(worktree), "--veces", "2", "--unidad",
                fallo.check + "=" + fallo.unidad, "--evidencia", str(evidencia), "--nombre", nombre)

    def reanudacion(self, comando, anterior):
        """Sólo admite el mismo argv, --reanudar y el traslado documentado a mini."""
        try:
            nuevo = tuple(shlex.split(comando))
        except ValueError:
            raise ResultadoInvalido("reanudar-invalido") from None
        def normalizar(argv):
            salida = []
            indice = 0
            while indice < len(argv):
                a = argv[indice]
                if a == "--hosts":
                    exigir(indice + 1 < len(argv) and argv[indice + 1] in ("auto", "mini"), "reanudar-invalido")
                    indice += 2
                elif a == "--reanudar":
                    indice += 1
                else:
                    salida.append(a)
                    indice += 1
            # El lanzador ordena --perfil y --base de otra forma.
            exigir(len(salida) >= 4 and len(salida[4:]) % 2 == 0, "reanudar-invalido")
            pares = list(zip(salida[4::2], salida[5::2]))
            exigir(len({k for k, _ in pares}) == len(pares), "reanudar-invalido")
            return salida[:4], sorted(pares)
        exigir(nuevo.count("--reanudar") == 1 and normalizar(nuevo) == normalizar(anterior), "reanudar-no-corresponde")
        return nuevo

    def correr(self, argv, registro, repeticion=False, permitir_lanzar=True):
        """Una clave de efecto conserva su recibo; un crash se reengancha al lanzador."""
        registro.mkdir(parents=True, exist_ok=True, mode=0o700)
        recibo = registro / "recibo.json"
        if recibo.exists():
            guardado = leer_json(recibo)
            exigir(isinstance(guardado, dict) and guardado.get("argv") == list(argv), "recibo-no-corresponde")
            exigir(isinstance(guardado.get("ruta"), str) and
                   type(guardado.get("codigo")) is int and guardado["codigo"] in (0, 1, 75), "recibo-invalido")
            return Path(guardado["ruta"]), guardado["codigo"]
        # Recuperar evidencia ya producida no requiere permiso para trabajo nuevo.
        if not permitir_lanzar:
            return None
        if self.operador is not None:
            self.operador.avanzar()
        try:
            resultado = self.runner(list(argv), capture_output=True, text=True, check=False)
        except (OSError, subprocess.SubprocessError):
            raise ResultadoInvalido("lanzador-no-disponible") from None
        exigir(resultado.returncode in (0, 1, 75), "salida-no-utilizable")
        etiqueta = "REPETICION" if repeticion else "SUITE"
        campo = "repeticion" if repeticion else "resultado"
        patron = r"^" + etiqueta + r" veredicto=(0|1|75) " + campo + r"=(.+)$"
        lineas = [re.fullmatch(patron, linea) for linea in resultado.stdout.splitlines()]
        encontrados = [m for m in lineas if m]
        exigir(len(encontrados) == 1 and int(encontrados[0][1]) == resultado.returncode, "salida-sin-recibo")
        ruta = Path(encontrados[0][2])
        exigir(ruta.is_absolute() and ruta.is_file(), "resultado-ilegible")
        # Un recibo truncado por una muerte no debe impedir el reenganche.
        temporal = registro / "recibo.tmp"
        with temporal.open("w", encoding="utf-8") as archivo:
            json.dump({"argv": list(argv), "ruta": str(ruta),
                       "codigo": resultado.returncode}, archivo)
            archivo.flush()
            os.fsync(archivo.fileno())
        os.replace(str(temporal), str(recibo))
        descriptor = os.open(str(registro), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return ruta, resultado.returncode

    def suite(self, candidato, tren_id, intento=0, reanudar="", permitir_lanzar=True):
        try:
            wt = self.git.worktree(candidato.C)
            raiz = self.estado / clave("suite", tren_id)
            argv = self.argv_suite(candidato, wt, raiz / "evidencia", "suite-" + tren_id)
            if reanudar:
                argv = self.reanudacion(reanudar, argv)
            recibo = self.correr(argv, raiz / ("intento-" + str(intento)),
                                 permitir_lanzar=permitir_lanzar)
            if recibo is None:
                return None
            ruta, codigo = recibo
            r = leer_json(ruta)
            exigir(isinstance(r, dict) and r.get("veredicto") == codigo, "salida-no-corresponde")
            return str(ruta), leer_suite(r, candidato.C, candidato.B)
        except FalloGit as exc:
            raise ResultadoInvalido(str(exc)) from None

    def repetir(self, candidato, suite, solicitud, commit, tren_id, intento=0, reanudar="",
                permitir_lanzar=True):
        try:
            wt = self.git.worktree(commit)
            raiz = self.estado / clave("repetir", tren_id, solicitud.id)
            prefijo = "diag-" if commit == candidato.C else "repro-"
            argv = self.argv_repetir(suite, commit, wt, raiz / "evidencia",
                                    prefijo + solicitud.id, solicitud.fallo)
            if reanudar:
                argv = self.reanudacion(reanudar, argv)
            recibo = self.correr(argv, raiz / ("intento-" + str(intento)), True,
                                 permitir_lanzar=permitir_lanzar)
            if recibo is None:
                return None
            ruta, codigo = recibo
            r = leer_json(ruta)
            exigir(isinstance(r, dict) and r.get("veredicto") == codigo, "salida-no-corresponde")
            resultado = leer_repeticion(r, commit, suite, candidato.C, candidato.B, solicitud.fallo)
            if isinstance(resultado, Repeticion):
                resultado = replace(resultado, comando=tuple(a for a in argv if a != "--reanudar"))
            return resultado
        except (FalloGit, OSError) as exc:
            raise ResultadoInvalido(str(exc) if isinstance(exc, FalloGit) else "origen-ilegible") from None
