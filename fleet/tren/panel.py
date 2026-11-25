"""Lee el ledger y proyecta el contrato público tren-host@1."""
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time

from adaptadores.jale import MODOS
from dominio import Motivo, clave, datos, inicial
from fases import SUITES, medir_fases
from inscripcion import modo
from instalar import atomico
from ledger import directorio_estado, lectura_medicion

ETAPAS = {
    "Congelado": "lote", "Armado": "lote", "EsperandoCI": "ci",
    "Corriendo": "suite", "Diagnosticando": "atribucion",
    "Certificado": "aterrizando", "Promoviendo": "aterrizando",
    "Terminal": None,
}
DIA = 86400
VENTANA = 12 * 3600
RESULTADOS = ("ATERRIZADO", "DEVUELTO", "ABSUELTO", "ABORTADO", "PARTIDO", "REARMAR", "SIMULADO")
MOTIVOS = tuple(m.value for m in Motivo)
REPO = r"[A-Za-z0-9_.-]{1,64}/[A-Za-z0-9_.-]{1,64}"
MODO = r"[a-z0-9-]{1,32}"
REF = r"job-[A-Za-z0-9]{4,32}|[0-9]{8}-[0-9]{6}-[0-9a-f]{6}"
MAX_DOCUMENTO = 256 * 1024
TOPE_BLOQUEO = 2.0
TIPO_TICK = {"activo": "operativo", "sombra": "sombra-doble-camino"}
RUTA_O_CONTROL = re.compile(r"[\\\x00-\x1f\x7f-\x9f\ud800-\udfff]|(?<![\w./-])(?:/|[a-zA-Z]:/)")
HEX_LARGO = re.compile(r"(?i)(?<![0-9a-f])(?=[0-9a-f]*[a-f]|[0-9]{16})([0-9a-f]{7})[0-9a-f]+(?![0-9a-f])")


def coincide(valor, patron):
    return isinstance(valor, str) and re.fullmatch(patron, valor) is not None


def repo_publicable(repo):
    return coincide(repo, REPO) and all(p not in (".", "..") for p in repo.split("/"))


def unidad_publica(valor):
    if (isinstance(valor, str) and 0 < len(valor) <= 300 and ".." not in valor
            and not RUTA_O_CONTROL.search(valor)):
        return HEX_LARGO.sub(r"\1", valor)


def fecha(valor, ahora, defecto=None):
    if type(valor) is int or type(valor) is float and math.isfinite(valor):
        return max(ahora - 30 * DIA, min(valor, ahora + 300))
    return defecto


def miembro(entrada):
    entrega = entrada["entrega"]
    pr, head = entrega["pr"], entrega["head"]
    if type(pr) is int and 1 <= pr <= 10**7 and coincide(head, r"[0-9a-f]{40}"):
        return dict(pr=pr, head=head[:7])


def por_recibo(estado):
    return {clave("entrega", e["entrega"]["repo"], e["entrega"]["pr"], e["entrega"]["head"]): e
            for e in estado["entradas"]}


def espera(entradas, ahora):
    filas = []
    for entrada in entradas:
        if entrada.get("tren") or entrada.get("terminal"):
            continue
        persona = miembro(entrada)
        if persona is None:
            continue
        admision = entrada["admision"]
        motivo = admision.get("motivo")
        etapa = {"Admitido": "admitido", "Rechazado": "caducado"}.get(admision["_tipo"], "retenido")
        if etapa == "retenido" and motivo == "custodia":
            etapa, motivo = "entregado", None
        filas.append(dict(persona, etapa=etapa, motivo=motivo if motivo in MOTIVOS else None,
                          desde=fecha(entrada["recibida"], ahora, ahora)))
    return filas[:50]


def devolucion(repo, pr, registro):
    efecto, resultado = registro
    url = resultado.get("comentario_url")
    if not coincide(url, rf"https://github\.com/{re.escape(repo)}/pull/{pr}#issuecomment-[0-9]+") or len(url) > 512:
        return None
    fallos = efecto.get("fallos", ())
    if efecto.get("ci_rojos") and not fallos:
        return dict(check="ci", unidad="CI", comentario_url=url)
    for fallo in fallos:
        unidad, check = unidad_publica(fallo.get("unidad")), fallo.get("check")
        if unidad and coincide(check, MODO):
            return dict(check=check, unidad=unidad, comentario_url=url)
    return dict(check="devolucion", unidad="Devolución", comentario_url=url)


def recientes(repo, estado, entradas, historia, devoluciones, tomado, ahora):
    filas = []
    for tren in estado["trenes"]:
        ident = tren["id"]
        resultado = tren["fase"].get("resultado") if tren["fase"]["_tipo"] == "Terminal" else None
        if resultado not in RESULTADOS:
            continue
        fases = historia["trenes"][ident]
        fin = fases[-1][1]
        if fin < ahora - VENTANA or fin > ahora + 300:
            continue
        miembros = []
        for recibo in tren["recibos"]:
            entrada = entradas.get(recibo)
            persona = miembro(entrada) if entrada is not None else None
            if persona is None:
                continue
            registro = devoluciones.get((ident, recibo))
            terminal = (entrada.get("terminal") or {}).get("resultado")
            if resultado == "DEVUELTO":
                if registro or entrada.get("tren") == ident and terminal == "DEVUELTO":
                    terminal = "DEVUELTO"
                elif entrada.get("tren") == ident and entrada["admision"]["_tipo"] == "Rechazado":
                    terminal = "CADUCADO"
                else:
                    terminal = "REARMAR"
            elif resultado != "SIMULADO" or terminal != "MERGEADO-FUERA":
                terminal = resultado
            detalle = devolucion(repo, persona["pr"], registro) if registro else None
            miembros.append(dict(pr=persona["pr"], resultado=terminal, devolucion=detalle))
        if miembros:
            filas.append(dict(id=ident[:12], resultado=resultado, miembros=miembros[:5],
                              cierre=fecha(fases[0][1], tomado), fin=fin))
    return sorted(filas, key=lambda f: f["fin"], reverse=True)[:10]


def evento(seq, repo, instante, tipo, payload):
    return dict(seq=seq, repo=repo, instante=instante, tipo=tipo, payload=json.dumps(payload))


def hitos_suite(repo, tren, historia, efectos):
    """Usa la definición de U1 sobre las transiciones compactadas por el cursor."""
    ident = tren["id"]
    minimo = dict(id=ident, recibos=tren["recibos"])
    # En un operativo con transición y consumo, U1 procesa el consumo antes de la fase.
    filas = [evento(seq, repo, instante, "operativo", {"consumido": llave, "estado": {"trenes": []}})
             for llave, (seq, instante) in historia["consumos"].items()]
    filas.extend(evento(seq, repo, instante, "operativo", {"estado": {"trenes": [
        dict(minimo, fase={"_tipo": tipo})]}}) for seq, instante, tipo in historia["trenes"][ident])
    filas.extend(evento(seq, repo, instante, "efecto-iniciado", {"clave": llave})
                 for llave, (seq, instante) in historia["inicios"].items())
    return medir_fases(filas, efectos, repo, "0001-01-01T00:00:00Z", "9999-12-31T00:00:00Z")["trenes"][0]["hitos"]


def lugar(log):
    valores = {}
    for linea in log:
        llave, sep, valor = linea.partition("=")
        if sep and llave in ("HOST", "PUEUE_ID", "PUEUE_REF") and llave not in valores:
            valores[llave] = valor.split(maxsplit=1)[0] if valor.split() else ""
    tipo = {"desktop": "desk", "mini": "mini"}.get(valores.get("HOST"), "desconocido")
    ident = valores.get("PUEUE_ID")
    ref = valores.get("PUEUE_REF")
    return dict(tipo=tipo, vm=None,
                pueue_id=int(ident) if coincide(ident, r"[0-9]{1,20}") else None,
                ref=ref if coincide(ref, REF) else None)


def suite_publica(registros, hitos, ahora):
    filas = []
    for (check, intento), (inicio, fin, codigo, unidades, log) in registros.items():
        if len(registros) == 1:
            inicio = inicio if inicio is not None else hitos.get("suite_iniciada")
            if codigo is not None and codigo != 75 and fin is None:
                fin = hitos.get("suite_terminada")
        estado = ("pendiente-75" if codigo == 75 else "terminada" if fin is not None
                  else "corriendo" if inicio is not None else "en-fila")
        fallas = None if unidades is None else sum(u["resultado"] in ("falla", "no-corrida") for u in unidades)
        resultado = None if fallas is None or estado != "terminada" else "rojo" if fallas else "verde"
        filas.append(dict(modo=check, intento=intento, estado=estado,
                          inicio=fecha(inicio, ahora), fin=fecha(fin, ahora),
                          resultado=resultado, fallas=None if fallas is None else min(fallas, 10**5),
                          lugar=lugar(log)))
    return filas[-8:]


def ensayo_publico(fase, miembros, registros, ahora):
    pendiente = fase.get("pendiente")
    if not pendiente:
        return None
    fallo = pendiente["fallo"]
    unidad, check = unidad_publica(fallo["unidad"]), fallo["check"]
    # C y B comparan el lote completo. Sólo los subconjuntos traen sus PR explícitos.
    prs = pendiente.get("prs") or [m["pr"] for m in miembros]
    prs = [pr for pr in prs if type(pr) is int and 1 <= pr <= 10**7]
    if not (unidad and coincide(check, MODO) and prs):
        return None
    vivos = [r for r in registros if r[0] is not None and r[1] is None]
    inicio, _, _, _, log = (vivos or registros or [(None, None, None, None, ())])[-1]
    return dict(modo=check, unidad=unidad, prs=prs[:5], hechos=len(fase.get("evidencias", ())),
                inicio=fecha(inicio, ahora), lugar=lugar(log))


def proyectar(filas, tomado, ahora, errors=()):
    """Proyecta estados, hitos y registros ya leídos. Sin reloj ni IO."""
    repos = []
    for repo, modo_repo, estado, custodia, historia, efectos, devoluciones, lotes, ensayos in filas:
        entradas = por_recibo(estado)
        entradas.update((r, e) for r, e in custodia.items() if r not in entradas)
        pausa = estado.get("pausa")
        if pausa is not None:
            motivo = pausa["motivo"]
            pr = pausa.get("pr")
            pausa = dict(motivo=motivo if coincide(motivo, r"[a-z0-9][a-z0-9-]{0,47}") else "otra",
                         pr=pr if type(pr) is int and pr > 0 else None,
                         desde=fecha(historia["pausa"][1], tomado, tomado))
        lote = None
        tren = activo(estado)
        if tren:
            ident, fase = tren["id"], tren["fase"]
            miembros = [miembro(entradas[r]) for r in tren["recibos"] if r in entradas]
            miembros = [m for m in miembros if m is not None]
            if miembros:
                fases = historia["trenes"][ident]
                hitos = hitos_suite(repo, tren, historia, efectos) if lotes else {}
                lote = dict(id=ident[:12], fase=ETAPAS.get(fase["_tipo"], "otra"),
                            fase_desde=fecha(fases[-1][1], tomado), cierre=fecha(fases[0][1], tomado),
                            miembros=miembros[:5], suite=suite_publica(lotes, hitos, tomado),
                            ensayo=ensayo_publico(fase, miembros, ensayos, tomado))
        repos.append(dict(repo=repo, modo=modo_repo, pausa=pausa, lote=lote,
                          espera=espera(entradas.values(), tomado),
                          recientes=recientes(repo, estado, entradas, historia, devoluciones,
                                             tomado, ahora)))
    return dict(schema="tren-host@1", taken=tomado, repos=repos, errors=list(errors))


def activo(estado):
    return next((t for t in reversed(estado["trenes"]) if t["fase"]["_tipo"] != "Terminal"), None)


def leer_cursor(ruta, identidad=None):
    try:
        sello, contenido = json.loads(ruta.read_text(encoding="utf-8"))
        if sello == clave(contenido) and (contenido["ledger"] == identidad or identidad is None):
            return contenido
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return dict(ledger=identidad, repos={}, publicados={})


def leer_grafias_publicadas(ruta):
    cursor = leer_cursor(ruta)
    # Los cursores anteriores sólo permiten recuperar las grafías inequívocas.
    variantes = {}
    for llave in cursor["repos"]:
        nombre = llave.rsplit(":", 1)[0]
        variantes.setdefault(nombre.lower(), set()).add(nombre)
    grafias = {normal: next(iter(nombres)) for normal, nombres in variantes.items() if len(nombres) == 1}
    grafias.update(cursor.get("grafias", {}))
    return grafias


def leer_hitos(db, repo, tipo, ultimo, previo, estado, efectos):
    if not previo or previo["seq"] > ultimo:
        previo = dict(seq=0, pausa=[None, None], trenes={}, inicios={}, consumos={})
    vigentes = {t["id"] for t in estado["trenes"]}
    for campo, llaves in (("trenes", vigentes), ("inicios", efectos), ("consumos", efectos)):
        previo[campo] = {k: v for k, v in previo[campo].items() if k in llaves}
    tipos = (tipo, "efecto-iniciado") if efectos else (tipo,)
    consulta = ("SELECT seq, tipo, instante, payload FROM events WHERE repo=? "
                f"AND seq>? AND seq<=? AND tipo IN ({','.join('?' for _ in tipos)}) ORDER BY seq")
    anterior = None
    for seq, clase, instante, raw in db.execute(consulta, (repo, previo["seq"], ultimo, *tipos)):
        if clase == "efecto-iniciado":
            llave = json.loads(raw)["clave"]
            if llave in efectos:
                previo["inicios"].setdefault(llave, [seq, instante])
            continue
        if raw == anterior:
            continue
        anterior = raw
        paso = json.loads(raw)
        foto = paso["estado"]
        pausa = foto.get("pausa")
        marca = None if pausa is None else clave(pausa)
        if marca != previo["pausa"][0]:
            previo["pausa"] = [marca, instante]
        for tren in foto["trenes"]:
            ident, fase = tren["id"], tren["fase"]["_tipo"]
            if ident in vigentes:
                fases = previo["trenes"].setdefault(ident, [])
                if not fases or fase != fases[-1][2]:
                    fases.append([seq, instante, fase])
        consumido = paso.get("consumido")
        if consumido in efectos:
            previo["consumos"][consumido] = [seq, instante]
    previo["seq"] = ultimo
    return previo


def efectos_suite(db, repo, tren):
    if tren is None:
        return {}
    consulta = ("SELECT clave, repo, payload FROM effects WHERE repo=? "
                "AND json_extract(payload, '$.datos[0]')=? "
                "AND json_extract(payload, '$.tipo') IN (?,?)")
    return {fila["clave"]: dict(fila) for fila in db.execute(consulta, (repo, tren["id"], *SUITES))}


def leer_devoluciones(db, estado, historia, ahora):
    devoluciones = {}
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='effect_results'").fetchone():
        return devoluciones
    consulta = ("SELECT e.payload, r.payload FROM effects e LEFT JOIN effect_results r USING (clave) "
                "WHERE e.clave=?")
    for tren in estado["trenes"]:
        ident = tren["id"]
        if (tren["fase"].get("resultado") != "DEVUELTO"
                or historia["trenes"][ident][-1][1] < ahora - VENTANA):
            continue
        for recibo in tren["recibos"]:
            fila = db.execute(consulta, (clave("devolver", ident, recibo),)).fetchone()
            if fila:
                efecto, resultado = fila
                detalle = json.loads(json.loads(efecto)["datos"][2])
                devoluciones[ident, recibo] = (detalle, json.loads(resultado) if resultado else {})
    return devoluciones


def leer_repo(db, repo, modo_repo, cache, ahora):
    tipo = TIPO_TICK[modo_repo]
    ultimo = db.execute("SELECT payload FROM events WHERE repo=? AND tipo=? "
                        "ORDER BY seq DESC LIMIT 1", (repo, tipo)).fetchone()
    estado = json.loads(ultimo["payload"])["estado"] if ultimo else datos(
        inicial(repo) if modo_repo == "activo" else inicial(""))
    efectos = efectos_suite(db, repo, activo(estado))
    secuencia = db.execute("SELECT max(seq) FROM events WHERE repo=?", (repo,)).fetchone()[0] or 0
    llave = repo + ":" + modo_repo
    historia = leer_hitos(db, repo, tipo, secuencia, cache.get(llave), estado, efectos)
    cache[llave] = historia
    custodia = {
        recibo: dict(entrega=dict(pr=pr, head=head), recibida=recibida,
                     admision=dict(_tipo="Retenido", motivo="custodia"))
        for recibo, pr, head, recibida in db.execute(
            "SELECT recibo, pr, head, recibida FROM requests WHERE repo=? ORDER BY recibida, recibo", (repo,))
    }
    devoluciones = leer_devoluciones(db, estado, historia, ahora)
    return repo, modo_repo, estado, custodia, historia, tuple(efectos.values()), devoluciones


def leer_json_local(ruta):
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def mtime(ruta):
    try:
        return ruta.stat().st_mtime
    except FileNotFoundError:
        return None


def leer_log(ruta):
    try:
        with ruta.open("rb") as archivo:
            prefijo = archivo.read(64 * 4096)
            if len(prefijo) == 64 * 4096:
                prefijo = prefijo.rpartition(b"\n")[0]
            return prefijo.decode("utf-8", errors="replace").split("\n")[:64]
    except FileNotFoundError:
        return ()


def leer_corrida(ruta, unidades=None):
    inicio = mtime(ruta / "iniciado.json")
    fin = mtime(ruta / "codigo.json")
    codigo = leer_json_local(ruta / "codigo.json")
    log = leer_log(ruta / "corrida.log")
    return inicio, fin, codigo, unidades, log


def leer_registros(raiz, tren):
    if tren is None:
        return {}, []
    ident, fase = tren["id"], tren["fase"]
    suite = raiz / clave("suite", ident)
    completos = leer_json_local(suite / "suite.json") or []
    corridas = {}
    for check in MODOS:
        carpeta = suite / check
        final = leer_json_local(carpeta / "completo.json")
        recibo = next((r["unidades"] for r in completos if r["modo"] == check), None)
        for ruta in sorted(carpeta.glob("*"), key=lambda p: p.name.zfill(2)):
            if not coincide(ruta.name, r"0|[1-9][0-9]?") or not ruta.is_dir():
                continue
            intento = int(ruta.name)
            corridas[check, intento] = leer_corrida(ruta, recibo if intento == final else None)
    ensayos = []
    pendiente = fase.get("pendiente")
    if pendiente is not None:
        ruta = raiz / clave("repetir", ident, pendiente["id"])
        for intento in sorted(ruta.glob("[12]/*"), key=lambda p: (p.parent.name, p.name.zfill(2))):
            if coincide(intento.name, r"0|[1-9][0-9]?") and intento.is_dir():
                ensayos.append(leer_corrida(intento))
    return corridas, ensayos


def inscritos():
    ruta = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "pao-tren/inscritos"
    repos = {}
    try:
        with ruta.open(encoding="utf-8") as archivo:
            for linea in archivo:
                campos = linea.split("#", 1)[0].split()
                if campos and repo_publicable(campos[0]):
                    repos.setdefault(campos[0].lower(), campos[0])
    except FileNotFoundError:
        pass
    return list(repos.values())


def problema(etapa, exc):
    return dict(stage=etapa, message="Lectura fallida: " + type(exc).__name__)


def grafia_ledger(db, nombres, modo_repo):
    if len(nombres) == 1:
        return nombres[0]
    consulta = "SELECT 1 FROM events WHERE repo=? AND tipo=? ORDER BY seq DESC LIMIT 1"
    return next((nombre for nombre in nombres
                 if db.execute(consulta, (nombre, TIPO_TICK[modo_repo])).fetchone()), nombres[0])


def leer_ledger(raiz, ruta, seleccion, ahora):
    stat = (raiz / "ledger.sqlite3").stat()
    identidad = [1, stat.st_dev, stat.st_ino]
    limite = time.monotonic() + TOPE_BLOQUEO
    for intento in range(2):
        # El lector puede haber actualizado hitos en memoria antes de fallar.
        cursor = leer_cursor(ruta, identidad)
        restante = max(0, limite - time.monotonic())
        try:
            with lectura_medicion(raiz, timeout=restante if intento else min(restante, 0.1)) as db:
                grafias = {}
                for nombre, in db.execute("SELECT DISTINCT repo FROM events ORDER BY repo"):
                    grafias.setdefault(nombre.lower(), []).append(nombre)
                filas = [leer_repo(db, grafia_ledger(db, grafias.get(r.lower(), [r]), m),
                                   m, cursor["repos"], ahora)
                         for r, m in seleccion]
            return filas, cursor
        except sqlite3.OperationalError as exc:
            bloqueo = str(exc).partition(":")[0] in (
                "database is locked", "database table is locked",
                "database schema is locked", "database is busy",
            )
            restante = limite - time.monotonic()
            if intento or not bloqueo or restante <= 0:
                raise
            time.sleep(min(restante, 0.025))


def publicar(repo, ahora=None):
    """Toma una instantánea SQLite y actualiza sólo el cursor derivado."""
    ahora = time.time() if ahora is None else ahora
    raiz = directorio_estado()
    ruta = raiz / "panel-cursor.json"
    solicitados = [repo] if repo is not None else inscritos()
    if not 1 <= len(solicitados) <= 4 or not all(repo_publicable(r) for r in solicitados):
        raise ValueError("panel requiere entre uno y cuatro repos válidos")
    seleccion = [(r, modo(r) or "activo") for r in solicitados]
    errores = []
    try:
        filas, cursor = leer_ledger(raiz, ruta, seleccion, ahora)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        errores.append(problema("ledger", exc))
        cursor = None
        grafias = leer_grafias_publicadas(ruta)
        if repo is not None and repo.lower() not in grafias:
            grafias = {r.lower(): r for r in inscritos()}
        filas = [(grafias.get(r.lower(), r), m, dict(entradas=[], trenes=[]), {}, {}, (), {})
                 for r, m in seleccion]
    registros = []
    for fila in filas:
        try:
            registros.append((*fila, *leer_registros(raiz / "jale", activo(fila[2]))))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            errores.append(problema("suite", exc))
            registros.append((*fila, {}, []))
    salida = None
    if cursor is not None:
        # Mantiene iguales dos lecturas equivalentes. Al vencer una fila de 12 h
        # o el taken del receptor se publica una fecha nueva.
        seleccion_id = clave([fila[0] for fila in filas])
        previa, tomado = cursor["publicados"].get(seleccion_id, (None, ahora))
        if previa and ahora - 23 * 3600 <= tomado <= ahora:
            candidata = proyectar(registros, tomado, ahora, errores)
            fin_en_rango = all(lote["fin"] <= tomado + 300 for fila in candidata["repos"]
                               for lote in fila["recientes"])
            if fin_en_rango and clave(candidata["repos"], candidata["errors"]) == previa:
                salida = candidata
    if salida is None:
        salida = proyectar(registros, ahora, ahora, errores)
    if len(json.dumps(salida, sort_keys=True, ensure_ascii=False).encode("utf-8")) + 1 > MAX_DOCUMENTO:
        raise ValueError("panel supera el tamaño del receptor")
    if cursor is not None:
        cursor["publicados"][seleccion_id] = [clave(salida["repos"], salida["errors"]), salida["taken"]]
        cursor.setdefault("grafias", {}).update((fila[0].lower(), fila[0]) for fila in filas)
        try:
            atomico(ruta, json.dumps([clave(cursor), cursor]).encode("utf-8"))
        except OSError:
            pass  # El cursor es regenerable y puede estar en un directorio de sólo lectura.
    return salida
