"""Mide observaciones en sombra sobre PR abiertos, con GET únicamente."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from threading import Lock
import time


def clase(endpoint):
    ruta = endpoint.partition("?")[0]
    if re.search(r"/pulls/\d+$", ruta):
        return "pulls/N"
    if "/issues/comments/" in ruta:
        return "issues/comments/ID"
    if "/actions/jobs/" in ruta:
        return "actions/jobs/ID"
    if ruta.endswith("/git/ref/heads/main"):
        return "git/ref/heads/main"
    return ruta.rsplit("/", 1)[-1]


def medir(args, temporal):
    sys.path.insert(0, str(args.tren.resolve()))
    from dominio import Entrada, Entrega, Estado
    from observador import GitHubGit

    git = args.git or temporal / "sin-objetos"
    hacer_get = ["gh", "api", "--method", "GET"]
    listado = subprocess.run([*hacer_get, f"repos/{args.repo}/pulls?state=open&per_page=100",
                              "--paginate", "--slurp"], capture_output=True, text=True, timeout=60)
    if listado.returncode != 0:
        raise SystemExit("Falló el GET para enumerar los PR abiertos")
    ahora = time.time()
    evidencia = temporal / "evidencia.json"
    evidencia.write_text("{}")
    prs = [pr for pagina in json.loads(listado.stdout) for pr in pagina]
    estado = Estado(args.repo, tuple(Entrada(
        Entrega(args.repo, pr["number"], pr["head"]["sha"], str(evidencia), str(evidencia)), ahora)
        for pr in prs))
    if not estado.entradas:
        raise SystemExit("No hay PR abiertos para medir")

    conteo = Counter()
    candado = Lock()

    def ejecutar(argv, **opciones):
        if argv[0] == "git":
            return subprocess.run(argv, **opciones)
        if argv[:4] != hacer_get:
            raise RuntimeError("La medición sólo admite GET")
        con_cabeceras = "-i" in argv
        env = dict(os.environ)
        if not con_cabeceras:
            env["GH_DEBUG"] = "api"
        check = opciones.pop("check", False)
        resultado = subprocess.run(argv, env=env, **opciones)
        # El diagnóstico del gh original permite contar cada página sin alterar su stdout.
        estados = re.findall(r"^HTTP/\S+ (\d{3})", resultado.stdout, re.M) if con_cabeceras else (
            re.findall(r"^\s*<\s*HTTP/\S+ (\d{3})", resultado.stderr, re.M))
        with candado:
            for codigo in estados or ["sin-estado"]:
                conteo[clase(argv[4]), codigo] += 1
        if check:
            resultado.check_returncode()
        return resultado

    opciones = {"ejecutar": ejecutar, "sombra": True, "paralelas": 4}
    config = {"git": str(git), "sensibles": {}}
    print(json.dumps({"pr_abiertos": len(prs), "pr_base_main": sum(
        pr["base"]["ref"] == "main" for pr in prs), "cache": args.cache}), flush=True)
    posteriores = []
    for tick in range(1, args.ticks + 1):
        if tick > 1:
            # El régimen real deja pasar el intervalo de launchd entre ticks.
            time.sleep(args.intervalo)
        conteo.clear()
        if args.cache:
            from cache_github import CacheGitHub
            with CacheGitHub(temporal) as cache:
                vista = GitHubGit(config, cache=cache, **opciones).observar(estado, ahora)
        else:
            vista = GitHubGit(config, **opciones).observar(estado, ahora)
        resumen = {tipo: {codigo: n for (t, codigo), n in sorted(conteo.items()) if t == tipo}
                   for tipo in sorted({t for t, _ in conteo})}
        pagadas = sum(n for (_, codigo), n in conteo.items() if codigo == "200")
        gratuitas = sum(n for (_, codigo), n in conteo.items() if codigo == "304")
        print(json.dumps({"tick": tick, "endpoints": resumen, "200": pagadas, "304": gratuitas,
                          "core_hora_120s": pagadas * 30, "core_hora_600s": pagadas * 6,
                          "vistas_completas": sum(v.completa for v in vista.prs)}), flush=True)
        if not pagadas + gratuitas or pagadas + gratuitas != sum(conteo.values()):
            raise SystemExit("La medición contiene estados inesperados")
        if tick > 1:
            posteriores.append(pagadas)
    if posteriores:
        print(json.dumps({"intervalo_s": args.intervalo, "ticks_posteriores": len(posteriores),
                          "200_min": min(posteriores), "200_max": max(posteriores),
                          "200_media": round(sum(posteriores) / len(posteriores), 1)}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo")
    parser.add_argument("--tren", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--git", type=Path)
    parser.add_argument("--cache", action="store_true")
    parser.add_argument("--ticks", type=int, default=2)
    parser.add_argument("--intervalo", type=float, default=0, help="segundos entre ticks")
    with tempfile.TemporaryDirectory() as ruta:
        medir(parser.parse_args(), Path(ruta))
