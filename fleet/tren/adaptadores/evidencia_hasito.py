"""Identidades de casos en los reportes que Hasito recoge junto al despacho."""
from contextlib import contextmanager
import json
from pathlib import PurePosixPath
import re
import tarfile
from xml.etree import ElementTree


@contextmanager
def evidencia(ruta, commit, parte, elegir):
    despacho = json.loads(ruta.with_name("despacho.json").read_bytes())
    for tarea in reversed(despacho["tareas"]):
        if tarea["estado"] == "rechazada":
            continue
        for resultado in tarea.get("resultados", ()):
            if (resultado["sha"], resultado["origen"]) != (commit, parte) or not elegir(resultado):
                continue
            plan, = [p for p in tarea["planes"] if p["huella"] == resultado["huella_plan"]]
            token, destino, huella = tarea["token"], plan["destino"], plan["huella"]
            if not (re.fullmatch(r"[0-9a-f]{32}", token) and re.fullmatch(r"[0-9a-f]{64}", huella)
                    and destino in ("desk", "mini")):
                raise ValueError("despacho inválido")
            carpeta = ruta.parent / "tareas" / token / "recogido"
            nombre = f"evidencia-{parte}.tar"
            archivo = carpeta / ".suite" / nombre
            if not archivo.is_file():
                archivo = carpeta / nombre
            with tarfile.open(archivo, "r:") as tar:
                raiz = PurePosixPath(f"{destino}-{huella[:12]}")

                def leer(relativa):
                    miembro = tar.getmember(str(raiz / relativa))
                    if not miembro.isfile():
                        raise ValueError("archivo de evidencia inválido")
                    with tar.extractfile(miembro) as contenido:
                        return contenido.read()

                if json.loads(leer("plan.json")) != plan:
                    raise ValueError("plan de evidencia distinto")
                yield plan, resultado, leer
                return
    raise ValueError("evidencia del despacho ausente")


def casos(formato, contenido, unidad, cwd):
    def corresponde(nombre):
        ruta = PurePosixPath(nombre)
        return (ruta == PurePosixPath(unidad) if not ruta.is_absolute() else
                str(ruta).endswith("/" + str(PurePosixPath(cwd) / unidad)))

    if formato == "vitest":
        suite, = [s for s in json.loads(contenido)["testResults"] if corresponde(s["name"])]
        filas = [(a["fullName"], a["status"]) for a in suite["assertionResults"]]
    else:
        xml = ElementTree.fromstring(contenido)
        filas, archivos = [], set()
        for suite in xml.iter("testsuite"):
            for caso in suite.findall("testcase"):
                archivo = caso.get("file") or suite.get("name") or caso.get("classname") or ""
                if corresponde(archivo):
                    archivos.add(archivo)
                    estado = ("failed" if caso.find("failure") is not None or caso.find("error") is not None
                              else "skipped" if caso.find("skipped") is not None else "passed")
                    filas.append((caso.attrib["name"], estado))
        if len(archivos) != 1:
            raise ValueError("archivo de unidad ambiguo o ausente")
    estados = dict(filas)
    if (len(estados) != len(filas) or not all(isinstance(n, str) and n for n in estados) or
            not set(estados.values()) <= {"passed", "failed", "pending", "todo", "skipped", "disabled"}):
        raise ValueError("casos ambiguos o ilegibles")
    return estados


def casos_ejecutados(origen, repeticion, recibo, fallo):
    """Cruza el fallo de la suite con los dos reportes de su repetición en C."""
    u, = recibo["unidades"]
    testigo = u["cobertura"]["testigo"]
    if testigo not in ("vitest-json", "bun-junit"):
        return False
    formato = "vitest" if testigo == "vitest-json" else "bun"
    try:
        commit = recibo["sha"]
        with evidencia(repeticion, commit, fallo.parte,
                       lambda r: bool(r.get("repeticion"))) as (plan, resultado, leer):
            rep = resultado["repeticion"]
            objetivo, = rep["objetivos"]
            fuente = objetivo["fuente"]
            if (resultado["base"] != recibo["origen"]["base"] or
                    rep["objetivos"] != plan["repeticion"]["objetivos"] or
                    plan["repeticion"]["origen"]["sha256"] != recibo["origen"]["sha256"] or
                    (objetivo["check"], objetivo["unidad"], objetivo["definicion"]) !=
                    (fallo.check, fallo.unidad, u["definicion"]) or type(fuente) is not int or fuente < 0 or
                    len(rep["intentos"]) != 2 or {i["n"] for i in rep["intentos"]} != {1, 2} or
                    any((i["check"], i["unidad"], i["fuente"], i["veredicto"]) !=
                        (fallo.check, fallo.unidad, fuente, "pasa") for i in rep["intentos"])):
                return False
            check, = [c for e in plan["etapas"] for c in e["checks"] if c["id"] == fallo.check]
            f = check["unidades"][fuente]
            if f["formato"] != formato or f["archivo"] != "{reporte}":
                return False
            cwd = f["reejecutar"]["cwd"]
            repeticiones = [casos(formato, leer(f"observaciones/{fallo.check}/f{fuente}/{n}/reporte.json"),
                                   fallo.unidad, cwd) for n in (1, 2)]

        suite = json.loads(origen.read_bytes())
        parte, = [p for p in suite["partes"] if p["origen"] == fallo.parte]
        with evidencia(origen, commit, fallo.parte,
                       lambda r: r["huella_plan"] == parte["huella_plan"]) as (plan, resultado, leer):
            (n, e), = [(n, e) for n, e in enumerate(plan["etapas"], 1) if e["id"] == fallo.etapa]
            check, = [c for c in e["checks"] if c["id"] == fallo.check]
            f = check["unidades"][fuente]
            if (f["formato"], f["archivo"], check["cwd"]) != (formato, "{reporte}", cwd):
                return False
            corridas, = [c["corridas"] for etapa in resultado["etapas"] if etapa["id"] == fallo.etapa
                         for c in etapa["checks"] if c["id"] == fallo.check]
            corrida = "reintento" if "reintento" in corridas else "candidato"
            carpeta = PurePosixPath("etapas", f"{n:02d}-{fallo.etapa}", fallo.check)
            if corrida == "reintento":
                carpeta /= corrida
            estados = casos(formato, leer(carpeta / "reporte.json"), fallo.unidad, cwd)
            fallidos = {nombre for nombre, estado in estados.items() if estado == "failed"}
            return (bool(fallidos) and "\n".join(sorted(fallidos)) == fallo.firma and
                    all(all(vistos.get(nombre) == "passed" for nombre in fallidos)
                        for vistos in repeticiones))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError, EOFError,
            tarfile.TarError, ElementTree.ParseError):
        # Un recibo agregado puede seguir sirviendo para atribución sin este testigo.
        return False
